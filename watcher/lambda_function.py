"""
lambda_function.py — ATS Watcher main handler.

Flow per run (EventBridge rate(1 hour)):
  1. load watchlist.json (bundled in the deploy zip) + state (S3)
  2. async fan-out fetch every company's jobs (fetchers.fetch_all)
  3. diff current job ids vs state  -> newly appeared jobs
  4. first run: only seed state, send nothing (avoid a backlog flood)
  5. filter new jobs by title/location (filters.job_matches)
  6. email matches grouped by tier (notifier.send_jobs)
  7. save state to S3 (+ daily backup); on Sundays include a weekly report

Local dev:
  python -m watcher.lambda_function --local --once
    -> state in ./state.local.json, notifications printed to stdout (no email).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import datetime as _dt

from watcher import fetchers, differ, notifier
from watcher.filters import job_matches
from watcher.storage import get_storage

# basicConfig is a no-op under Lambda: the runtime installs a root handler
# before the module is imported, so INFO records were being dropped and the
# per-run funnel counts never reached CloudWatch. Set the level explicitly.
logging.basicConfig(level=logging.INFO)
logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("ats-watcher")

HERE = os.path.dirname(os.path.abspath(__file__))
# watchlist.json is bundled at the package root (one level up from watcher/).
WATCHLIST_PATH = os.environ.get(
    "WATCHLIST_PATH", os.path.join(os.path.dirname(HERE), "watchlist.json"))


def load_watchlist(path: str = WATCHLIST_PATH) -> list[dict]:
    with open(path) as f:
        wl = json.load(f)
    # Only monitor boards we could actually resolve.
    return [e for e in wl if e.get("ats") and e.get("slug")]


def _filter_new(new_jobs: list[dict]) -> list[dict]:
    """Apply title/location filters; annotate needs_review on survivors."""
    matched = []
    dropped = {"title": 0, "location": 0}
    for j in new_jobs:
        r = job_matches(j.get("title", ""), j.get("location", ""), j.get("tier", ""))
        if r.keep:
            jj = dict(j)
            jj["needs_review"] = r.needs_review
            matched.append(jj)
        else:
            dropped[r.reason] = dropped.get(r.reason, 0) + 1
    # Which stage is discarding volume is the first question whenever the digest
    # looks too thin, so record the split rather than just the total.
    logger.info("filter funnel: %d in -> %d kept (dropped: title=%d, location=%d)",
                len(new_jobs), len(matched), dropped["title"], dropped["location"])
    return matched


def run_once(local: bool = False, dry_run: bool = False) -> dict:
    watchlist = load_watchlist()
    logger.info("watchlist: %d boards", len(watchlist))

    storage = get_storage(local=local)
    state = storage.load()
    first_run = not state.get("initialized")

    jobs_by_company, failed = asyncio.run(fetchers.fetch_all(watchlist))
    logger.info("fetched %d companies ok, %d failed", len(jobs_by_company), len(failed))

    failed_companies = [e["company"] for e in failed]
    new_jobs, new_state = differ.diff_jobs(state, jobs_by_company, failed_companies)

    summary = {"first_run": first_run, "companies_ok": len(jobs_by_company),
               "companies_failed": len(failed), "new_jobs": len(new_jobs)}

    # differ builds new_state from scratch, so bookkeeping keys it doesn't know
    # about have to be carried across explicitly or they reset every run.
    if state.get("last_weekly_report"):
        new_state["last_weekly_report"] = state["last_weekly_report"]

    if first_run:
        logger.info("first run — seeding state, no notifications")
    else:
        matched = _filter_new(new_jobs)
        summary["matched"] = len(matched)
        logger.info("%d new jobs -> %d after filter", len(new_jobs), len(matched))
        if matched:
            delivered = notifier.send_jobs(matched, dry_run=(local or dry_run))
            summary["delivered"] = bool(delivered)
            if not delivered:
                # Committing now would mark these jobs seen and never retry them.
                # Leaving the old state means the next run re-detects and resends;
                # the cost is one repeated fetch cycle.
                logger.error("delivery failed — state not committed, retrying next run")
                summary["state_committed"] = False
                return summary

    # The schedule fires hourly, so gating on the weekday alone sent ~24 reports
    # every Sunday. Gate on the ISO week already reported instead.
    week = _iso_week()
    if not first_run and _is_sunday() and new_state.get("last_weekly_report") != week:
        if _send_weekly_report(new_state, jobs_by_company, dry_run=(local or dry_run)):
            new_state["last_weekly_report"] = week

    storage.backup(state or new_state)  # back up the pre-write state once/day
    storage.save(new_state)
    summary["state_committed"] = True

    chronic = differ.chronic_failures(new_state)
    if chronic:
        logger.warning("chronic-fail companies (>=24 runs): %s", chronic)
        summary["chronic_failures"] = chronic
    return summary


def run_backlog(tiers: list[str] | None = None, limit: int | None = None,
                local: bool = False, dry_run: bool = False) -> dict:
    """One-off digest of everything currently open that matches the filters.

    The hourly job only reports the diff, so every posting that was already open
    at seed time is marked seen and never mailed. This sweep surfaces that
    backlog. It reads and writes no state, so it can be run at any time without
    disturbing the diff — the tradeoff is that repeat runs resend the same jobs.

    One email per tier, so Tier A is readable on its own instead of buried in a
    single thousand-row message.
    """
    watchlist = load_watchlist()
    if tiers:
        want = {t.strip().upper() for t in tiers}
        watchlist = [e for e in watchlist if (e.get("tier") or "").upper() in want]
    logger.info("backlog sweep: %d boards (tiers=%s, limit/tier=%s)",
                len(watchlist), tiers or "all", limit or "none")

    jobs_by_company, failed = asyncio.run(fetchers.fetch_all(watchlist))
    all_jobs = [j for v in jobs_by_company.values() for j in v]
    matched = _filter_new(all_jobs)

    by_tier: dict[str, list[dict]] = {}
    for j in matched:
        by_tier.setdefault((j.get("tier") or "?").upper(), []).append(j)

    sent: dict[str, dict] = {}
    for tier in sorted(by_tier, key=lambda t: (t != "A", t)):
        batch = notifier._sort_jobs(by_tier[tier])
        if limit:
            batch = batch[:limit]
        ok = notifier.send_jobs(batch, dry_run=(local or dry_run),
                                label=f"Open roles · Tier {tier}")
        sent[tier] = {"sent": len(batch), "available": len(by_tier[tier]), "ok": bool(ok)}
        logger.info("backlog tier %s: %d/%d sent ok=%s",
                    tier, len(batch), len(by_tier[tier]), ok)

    return {"mode": "backlog", "boards": len(watchlist),
            "companies_failed": len(failed), "open_jobs": len(all_jobs),
            "matched": len(matched), "by_tier": sent}


def _is_sunday() -> bool:
    return _dt.datetime.now(_dt.timezone.utc).weekday() == 6


def _iso_week() -> str:
    """ISO year-week key, e.g. '2026-W33' — one weekly report per key."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%G-W%V")


def _send_weekly_report(state: dict, jobs_by_company: dict, dry_run: bool) -> bool:
    total_boards = len(state.get("companies", {}))
    total_open = sum(len(v) for v in jobs_by_company.values())
    chronic = differ.chronic_failures(state)
    lines = [
        "ATS Watcher — Weekly report",
        f"Boards monitored: {total_boards}",
        f"Open roles right now: {total_open}",
        f"Companies failing (>=24 runs): {len(chronic)}",
    ]
    if chronic:
        lines += [f"  - {c}" for c in chronic]
    body = "\n".join(lines)
    return notifier.send_email("ATS Watcher Weekly report", html_body="", text_body=body,
                               dry_run=dry_run)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def handler(event, context):
    """AWS Lambda entry point.

    EventBridge delivers its own schema (version/id/detail-type/...), which has
    no "mode" key, so the scheduled path is the default and manual invocations
    opt in explicitly:

        {"mode": "backlog", "tiers": ["A"], "limit": 150}
        {"mode": "backlog", "dry_run": true}     # count only, sends nothing
    """
    event = event or {}
    if event.get("mode") == "backlog":
        return run_backlog(tiers=event.get("tiers"), limit=event.get("limit"),
                           dry_run=bool(event.get("dry_run")))
    return run_once(local=False)


def _cli():
    ap = argparse.ArgumentParser(description="ATS Watcher")
    ap.add_argument("--local", action="store_true",
                    help="use local file state, print instead of email")
    ap.add_argument("--once", action="store_true", help="run a single cycle")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't send email even against S3 state")
    ap.add_argument("--backlog", action="store_true",
                    help="one-off sweep of all currently-open matching jobs")
    ap.add_argument("--tiers", help="comma-separated tiers for --backlog, e.g. A,B")
    ap.add_argument("--limit", type=int, help="max jobs per tier for --backlog")
    args = ap.parse_args()
    if args.backlog:
        summary = run_backlog(tiers=args.tiers.split(",") if args.tiers else None,
                              limit=args.limit, local=args.local,
                              dry_run=args.dry_run)
    else:
        summary = run_once(local=args.local, dry_run=args.dry_run)
    print("\nSUMMARY:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
