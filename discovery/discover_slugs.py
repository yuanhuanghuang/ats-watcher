"""
discover_slugs.py — Phase 1 of ATS Watcher.

Given a seed CSV of ~700 companies, automatically discover which public ATS
(Greenhouse / Ashby / Lever) each company uses and what its board *slug* is,
by probing the three ATSs' public, unauthenticated JSON APIs.

Public APIs probed (no auth, no scraping):
  Greenhouse: GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
  Ashby:      GET https://api.ashbyhq.com/posting-api/job-board/{slug}
  Lever:      GET https://api.lever.co/v0/postings/{slug}?mode=json

For each company we generate a prioritized list of slug candidates and probe
them (Greenhouse -> Ashby -> Lever) until one returns HTTP 200 with a valid
jobs payload. First hit wins; the company is recorded in watchlist.json and no
further candidates are tried.

Design notes:
  * Concurrency: companies are processed by a thread pool. Requests to any one
    ATS domain are throttled by a per-domain limiter (<=MAX_CONCURRENCY_PER_DOMAIN
    in flight, >=MIN_INTERVAL_S between successive requests).
  * Retries: 429/5xx are retried up to MAX_RETRIES with exponential backoff.
  * Resumable: every finished company is written to checkpoint.json. Re-running
    skips companies already in the checkpoint (use --reset to start fresh).
  * Outputs: watchlist.json (hits) and unresolved.csv (misses) are (re)written
    at the end and reflect the full checkpoint.

Usage:
  python discovery/discover_slugs.py --seed companies_seed.csv
  python discovery/discover_slugs.py --seed companies_seed.csv --reset
  python discovery/discover_slugs.py --seed companies_seed.csv --workers 12
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HERE = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT_PATH = os.path.join(HERE, "checkpoint.json")
DEFAULT_WATCHLIST = os.path.join(os.path.dirname(HERE), "watchlist.json")
DEFAULT_UNRESOLVED = os.path.join(HERE, "unresolved.csv")

MAX_CONCURRENCY_PER_DOMAIN = 5      # in-flight requests per ATS domain
MIN_INTERVAL_S = 0.20               # >=200ms between requests to the same domain
MAX_RETRIES = 3                     # retries on 429/5xx
REQUEST_TIMEOUT = 20                # per-request timeout (seconds)
USER_AGENT = "ats-watcher-discovery/1.0 (+job-search personal project)"

# ATS probe definitions. Each entry: name -> (url_template, domain, extractor)
# extractor(json) -> jobs list if the payload is a valid board, else None.
def _gh_jobs(data):
    return data.get("jobs") if isinstance(data, dict) and isinstance(data.get("jobs"), list) else None

def _ashby_jobs(data):
    return data.get("jobs") if isinstance(data, dict) and isinstance(data.get("jobs"), list) else None

def _lever_jobs(data):
    return data if isinstance(data, list) else None

ATS_PROBES = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
     "boards-api.greenhouse.io", _gh_jobs),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}",
     "api.ashbyhq.com", _ashby_jobs),
    ("lever", "https://api.lever.co/v0/postings/{slug}?mode=json",
     "api.lever.co", _lever_jobs),
]


# --------------------------------------------------------------------------- #
# Per-domain rate limiter
# --------------------------------------------------------------------------- #

class DomainLimiter:
    """Bounds concurrency and enforces a minimum interval per domain."""

    def __init__(self, max_concurrency: int, min_interval: float):
        self._sems: dict[str, threading.Semaphore] = defaultdict(
            lambda: threading.Semaphore(max_concurrency))
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last: dict[str, float] = defaultdict(float)
        self._min_interval = min_interval
        self._guard = threading.Lock()

    def _sem(self, domain: str) -> threading.Semaphore:
        with self._guard:
            return self._sems[domain]

    def acquire(self, domain: str):
        self._sem(domain).acquire()
        # spacing: hold the domain's lock, sleep until min_interval elapsed
        lock = self._locks[domain]
        with lock:
            wait = self._min_interval - (time.monotonic() - self._last[domain])
            if wait > 0:
                time.sleep(wait)
            self._last[domain] = time.monotonic()

    def release(self, domain: str):
        self._sem(domain).release()


# --------------------------------------------------------------------------- #
# Slug candidate generation
# --------------------------------------------------------------------------- #

_CORP_WORDS = ("inc", "llc", "ltd", "corp", "co", "company", "lab", "labs", "technologies", "technology")
# Trailing "marketing" tokens many startups drop from their board slug,
# e.g. "Fireworks AI" -> "fireworks", "Rad AI" -> "rad".
_MARKETING_WORDS = ("ai", "ml", "hq", "app", "io")
# Tokens that a company keeps as a dotted TLD suffix in its slug,
# e.g. "Mistral AI" -> "mistral.ai", "Bill.com" -> "bill.com", "Fly.io" -> "fly.io".
_TLD_SUFFIXES = ("ai", "io", "com", "co", "xyz", "dev", "app", "ml", "hq")


def _tokens(text: str) -> list[str]:
    """Split into alphanumeric tokens, PRESERVING original case."""
    return [t for t in re.split(r"[^A-Za-z0-9]+", text) if t]


def _render(tokens: list[str]) -> list[str]:
    """Render a token list into every slug style we want to probe.

    ATS slugs are case-sensitive (esp. Ashby) and sometimes keep a dotted TLD,
    so we must try lowercased, original-case, PascalCase and dotted variants —
    not just a lowercased-and-stripped form.
    """
    if not tokens:
        return []
    lower = [t.lower() for t in tokens]
    styles = [
        "".join(lower),                                        # mistralai
        "-".join(lower),                                       # mistral-ai
        "".join(tokens),                                       # original case: ThinkingMachines, Deel
        "".join(t[:1].upper() + t[1:] for t in tokens),        # PascalCase: DbtLabs, MistralAI
    ]
    # dotted TLD variant: "Mistral AI" -> "mistral.ai" (+ front-case-preserved)
    if len(tokens) > 1 and lower[-1] in _TLD_SUFFIXES:
        styles.append("".join(lower[:-1]) + "." + lower[-1])   # mistral.ai
        styles.append("".join(tokens[:-1]) + "." + lower[-1])  # Mistral.ai
    return styles


def generate_candidates(company: str, slug_hint: str = "") -> list[str]:
    """Prioritized, de-duplicated slug candidates for a company.

    Preserves case and special characters (the root-cause bug was lowercasing
    every candidate, which missed case-sensitive slugs like ``ThinkingMachines``
    and dotted slugs like ``mistral.ai``).
    """
    cands: list[str] = []

    def add(c: str):
        c = c.strip().strip("-")
        if c and c not in cands:
            cands.append(c)

    # 1. explicit hint first, both as-given (case preserved) and lowercased
    if slug_hint:
        add(slug_hint.strip())
        add(slug_hint.strip().lower())

    name_toks = _tokens(company)

    # 2. full-name renderings (all case/dotted styles)
    for c in _render(name_toks):
        add(c)

    # 3. drop a trailing corp word (Inc/Labs/Co/...) and re-render
    if name_toks and name_toks[-1].lower() in _CORP_WORDS:
        for c in _render(name_toks[:-1]):
            add(c)

    # 4. drop a trailing marketing word (AI/ML/HQ/...) and re-render
    #    ("Fireworks AI" -> "fireworks"); the dotted "mistral.ai" already came
    #    from the full-name rendering in step 2.
    if len(name_toks) > 1 and name_toks[-1].lower() in _MARKETING_WORDS:
        for c in _render(name_toks[:-1]):
            add(c)

    return cands


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #

@dataclass
class SeedRow:
    company: str
    slug_hint: str = ""
    tier: str = ""
    category: str = ""
    notes: str = ""


@dataclass
class Result:
    company: str
    tier: str = ""
    category: str = ""
    hit: bool = False
    ats: Optional[str] = None
    slug: Optional[str] = None
    jobs_count: Optional[int] = None
    tried: list[str] = field(default_factory=list)


def _request_json(session: requests.Session, url: str, domain: str, limiter: DomainLimiter):
    """GET with per-domain throttling and retry on 429/5xx.

    Returns (status_code, parsed_json_or_None). status_code None means the
    request never got a usable HTTP response.
    """
    backoff = 0.5
    for attempt in range(MAX_RETRIES + 1):
        limiter.acquire(domain)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT,
                               headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        except requests.RequestException:
            resp = None
        finally:
            limiter.release(domain)

        if resp is None:
            # network error — retry
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None, None

        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
                continue
            return resp.status_code, None

        if resp.status_code == 200:
            try:
                return 200, resp.json()
            except ValueError:
                return 200, None  # 200 but not JSON -> not a valid board

        # 404 / 403 / other -> definitive miss for this candidate
        return resp.status_code, None

    return None, None


def probe_company(row: SeedRow, session: requests.Session, limiter: DomainLimiter) -> Result:
    """Probe all candidates x all ATSs, preferring a *populated* board.

    A populated board (jobs_count > 0) wins immediately. A valid-but-empty
    board (HTTP 200, jobs == []) is only remembered as a fallback and does NOT
    short-circuit — otherwise an empty board (e.g. a stale lowercase Ashby slug)
    would shadow the correct case-sensitive slug that actually has jobs.
    """
    result = Result(company=row.company, tier=row.tier, category=row.category)
    candidates = generate_candidates(row.company, row.slug_hint)
    empty_fallback: Optional[tuple[str, str]] = None  # (ats, slug) of a 0-job board

    for slug in candidates:
        for ats_name, url_tmpl, domain, extractor in ATS_PROBES:
            result.tried.append(f"{ats_name}:{slug}")
            status, data = _request_json(session, url_tmpl.format(slug=slug), domain, limiter)
            if status == 200 and data is not None:
                jobs = extractor(data)
                if jobs is None:
                    continue
                if len(jobs) > 0:
                    result.hit = True
                    result.ats = ats_name
                    result.slug = slug
                    result.jobs_count = len(jobs)
                    return result
                if empty_fallback is None:
                    empty_fallback = (ats_name, slug)

    if empty_fallback is not None:
        result.hit = True
        result.ats, result.slug = empty_fallback
        result.jobs_count = 0
    return result


# --------------------------------------------------------------------------- #
# Checkpoint I/O
# --------------------------------------------------------------------------- #

def load_checkpoint(path: str) -> dict[str, dict]:
    if os.path.exists(path):
        with open(path, "r") as f:
            try:
                return json.load(f)
            except ValueError:
                return {}
    return {}


def save_checkpoint(path: str, data: dict[str, dict], lock: threading.Lock):
    with lock:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def read_seed(path: str) -> list[SeedRow]:
    rows: list[SeedRow] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # tolerate different header casings / missing optional columns
        field_map = {k.lower().strip(): k for k in (reader.fieldnames or [])}
        if "company" not in field_map:
            raise SystemExit(f"seed CSV must have a 'company' column; got {reader.fieldnames}")
        for r in reader:
            def g(key):
                col = field_map.get(key)
                return (r.get(col, "") or "").strip() if col else ""
            company = g("company")
            if not company:
                continue
            rows.append(SeedRow(
                company=company,
                slug_hint=g("slug_hint"),
                tier=g("tier"),
                category=g("category"),
                notes=g("notes"),
            ))
    return rows


# --------------------------------------------------------------------------- #
# Output generation
# --------------------------------------------------------------------------- #

def write_outputs(checkpoint: dict[str, dict], watchlist_path: str, unresolved_path: str):
    hits, misses = [], []
    for company, res in checkpoint.items():
        if res.get("hit"):
            hits.append({
                "company": company,
                "ats": res["ats"],
                "slug": res["slug"],
                "jobs_count": res.get("jobs_count"),
                "tier": res.get("tier", ""),
                "category": res.get("category", ""),
            })
        else:
            misses.append(res)

    hits.sort(key=lambda h: (h.get("tier", "z"), h["company"].lower()))
    with open(watchlist_path, "w") as f:
        json.dump(hits, f, indent=2)

    with open(unresolved_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "tier", "category"])
        for m in sorted(misses, key=lambda x: (x.get("tier", "z"), x["company"].lower())):
            w.writerow([m["company"], m.get("tier", ""), m.get("category", "")])

    return hits, misses


def print_report(checkpoint: dict[str, dict], hits: list, misses: list):
    total = len(checkpoint)
    n_hit = len(hits)
    print("\n" + "=" * 60)
    print("DISCOVERY REPORT")
    print("=" * 60)
    print(f"Companies processed : {total}")
    print(f"Resolved (hits)     : {n_hit}  ({(n_hit/total*100 if total else 0):.1f}%)")
    print(f"Unresolved (misses) : {len(misses)}")

    by_ats = defaultdict(int)
    for h in hits:
        by_ats[h["ats"]] += 1
    print("\nATS distribution (hits):")
    for ats in ("greenhouse", "ashby", "lever"):
        print(f"  {ats:<11}: {by_ats.get(ats, 0)}")

    # Per-tier hit rate
    tier_total, tier_hit = defaultdict(int), defaultdict(int)
    for company, res in checkpoint.items():
        t = res.get("tier", "") or "(none)"
        tier_total[t] += 1
        if res.get("hit"):
            tier_hit[t] += 1
    print("\nHit rate by tier:")
    for t in sorted(tier_total):
        tt, th = tier_total[t], tier_hit[t]
        print(f"  tier {t:<8}: {th}/{tt}  ({(th/tt*100 if tt else 0):.0f}%)")

    # A-tier misses need manual handling
    a_misses = [m["company"] for m in misses if (m.get("tier", "").upper() == "A")]
    if a_misses:
        print(f"\n⚠️  A-tier UNRESOLVED (need manual handling): {len(a_misses)}")
        for c in sorted(a_misses):
            print(f"     - {c}")
    print("=" * 60 + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Discover ATS slugs for seed companies.")
    ap.add_argument("--seed", default=os.path.join(os.path.dirname(HERE), "companies_seed.csv"),
                    help="path to companies_seed.csv")
    ap.add_argument("--watchlist", default=DEFAULT_WATCHLIST)
    ap.add_argument("--unresolved", default=DEFAULT_UNRESOLVED)
    ap.add_argument("--checkpoint", default=CHECKPOINT_PATH)
    ap.add_argument("--workers", type=int, default=10, help="thread pool size")
    ap.add_argument("--reset", action="store_true", help="ignore existing checkpoint")
    ap.add_argument("--limit", type=int, default=0, help="process at most N companies (debug)")
    args = ap.parse_args()

    if not os.path.exists(args.seed):
        raise SystemExit(f"seed CSV not found: {args.seed}")

    seed = read_seed(args.seed)
    if args.limit:
        seed = seed[: args.limit]

    checkpoint = {} if args.reset else load_checkpoint(args.checkpoint)
    if args.reset and os.path.exists(args.checkpoint):
        os.remove(args.checkpoint)

    todo = [r for r in seed if r.company not in checkpoint]
    print(f"Seed rows: {len(seed)} | already done: {len(seed) - len(todo)} | to probe: {len(todo)}")
    if not todo:
        print("Nothing to probe (all in checkpoint). Regenerating outputs...")

    limiter = DomainLimiter(MAX_CONCURRENCY_PER_DOMAIN, MIN_INTERVAL_S)
    ckpt_lock = threading.Lock()
    session = requests.Session()

    start = time.time()
    done_count = 0
    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(probe_company, r, session, limiter): r for r in todo}
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # never let one company kill the run
                    res = Result(company=r.company, tier=r.tier, category=r.category)
                    print(f"  [error] {r.company}: {e}", file=sys.stderr)
                checkpoint[res.company] = {
                    "company": res.company, "tier": res.tier, "category": res.category,
                    "hit": res.hit, "ats": res.ats, "slug": res.slug,
                    "jobs_count": res.jobs_count,
                }
                save_checkpoint(args.checkpoint, checkpoint, ckpt_lock)
                done_count += 1
                flag = f"✓ {res.ats}:{res.slug} ({res.jobs_count} jobs)" if res.hit else "✗ unresolved"
                print(f"[{done_count}/{len(todo)}] {res.company:<28} {flag}")

    hits, misses = write_outputs(checkpoint, args.watchlist, args.unresolved)
    print_report(checkpoint, hits, misses)
    print(f"watchlist -> {args.watchlist}")
    print(f"unresolved -> {args.unresolved}")
    print(f"elapsed: {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
