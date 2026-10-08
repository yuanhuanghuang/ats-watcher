"""
differ.py — diff current jobs against last run's state.

State schema (JSON):
{
  "initialized": true,
  "companies": { "<company>": ["<job_id>", ...], ... },
  "fail_streak": { "<company>": <int consecutive fetch failures> },
  "updated_at": "<iso>"
}

First run (initialized != true): seed state from current jobs, emit NO new jobs
(avoids a first-email flood of the entire backlog).
"""

from __future__ import annotations

import datetime as _dt
import logging

logger = logging.getLogger("ats-watcher.differ")

# Consecutive empty reads required before believing a board really has no jobs.
# Lower = faster to accept a genuine emptying; higher = more tolerant of blips.
EMPTY_CONFIRM_RUNS = 3


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def diff_jobs(state: dict, jobs_by_company: dict[str, list[dict]],
              failed_companies: list[str]) -> tuple[list[dict], dict]:
    """Compute newly-appeared jobs and return (new_jobs, new_state).

    new_jobs is empty on the very first run (state is only seeded).
    """
    state = dict(state or {})
    companies_state: dict[str, list[str]] = dict(state.get("companies", {}))
    fail_streak: dict[str, int] = dict(state.get("fail_streak", {}))
    empty_streak: dict[str, int] = dict(state.get("empty_streak", {}))
    first_run = not state.get("initialized")

    new_jobs: list[dict] = []
    for company, jobs in jobs_by_company.items():
        current_ids = [j["job_id"] for j in jobs]
        prev_ids = set(companies_state.get(company, []))

        # A board that answers 200 with zero jobs after previously listing some
        # is usually a transient upstream glitch, not a mass delisting.
        # Overwriting the IDs with [] would make every job look new once the
        # board recovers, blasting the whole backlog as "new".
        #
        # But a board CAN genuinely empty out, so this can't hold the old IDs
        # forever. Require EMPTY_CONFIRM_RUNS consecutive empty reads before
        # accepting it. Counted separately from fail_streak so a real emptying
        # never shows up as a chronic fetch failure.
        if not first_run and prev_ids and not current_ids:
            streak = empty_streak.get(company, 0) + 1
            if streak < EMPTY_CONFIRM_RUNS:
                logger.warning("%s returned 0 jobs (had %d) — unconfirmed (%d/%d), keeping IDs",
                               company, len(prev_ids), streak, EMPTY_CONFIRM_RUNS)
                companies_state[company] = sorted(prev_ids)
                empty_streak[company] = streak
                fail_streak[company] = 0
                continue
            logger.info("%s empty for %d consecutive runs — accepting as genuinely cleared",
                        company, streak)

        if not first_run:
            fresh = [j for j in jobs if j["job_id"] not in prev_ids]
            # A company seen for the first time (added to watchlist later) is
            # seeded silently, not blasted as "all new".
            if company in companies_state:
                new_jobs.extend(fresh)

        companies_state[company] = current_ids
        fail_streak[company] = 0   # successful fetch resets the streak
        empty_streak[company] = 0  # so does any non-empty read, or a confirmed emptying

    # Track consecutive failures for companies that errored this run.
    for company in failed_companies:
        fail_streak[company] = fail_streak.get(company, 0) + 1

    new_state = {
        "initialized": True,
        "companies": companies_state,
        "fail_streak": fail_streak,
        "empty_streak": empty_streak,
        "updated_at": _now_iso(),
    }
    return ([] if first_run else new_jobs), new_state


def chronic_failures(state: dict, threshold: int = 24) -> list[str]:
    """Companies whose fetch has failed >= threshold consecutive runs (~1 day)."""
    return sorted(c for c, n in (state.get("fail_streak") or {}).items() if n >= threshold)
