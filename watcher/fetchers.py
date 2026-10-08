"""
fetchers.py — fetch + parse jobs from the three ATSs into a unified schema.

Two layers:
  * Pure parsers  parse_greenhouse / parse_ashby / parse_lever  take an
    already-decoded JSON payload and return List[Job]. No network -> unit-test
    them with fixtures.
  * Async fan-out  fetch_all  pulls every company in the watchlist concurrently
    with aiohttp, bounded per ATS domain, and calls the right parser.

Unified job schema (dict):
  job_id, title, company, tier, location, url, posted_at, salary

Field mappings (confirmed against live responses):
  Greenhouse jobs?content=false : id, title, location.name, absolute_url,
                                  first_published/updated_at
  Ashby posting-api            : id, title(!leading space), location +
                                  secondaryLocations[], jobUrl, publishedAt
  Lever v0/postings?mode=json  : id, text, categories.location/allLocations,
                                  hostedUrl, createdAt(epoch ms)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("ats-watcher.fetchers")

# URL templates and per-domain concurrency cap (watcher fan-out).
ATS_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
}
DOMAIN_OF = {
    "greenhouse": "boards-api.greenhouse.io",
    "ashby": "api.ashbyhq.com",
    "lever": "api.lever.co",
}
MAX_CONCURRENCY_PER_DOMAIN = 10
REQUEST_TIMEOUT = 25


def _job(job_id, title, company, tier, location, url, posted_at="", salary="") -> dict:
    return {
        "job_id": str(job_id),
        "title": (title or "").strip(),
        "company": company,
        "tier": tier,
        "location": (location or "").strip(),
        "url": url or "",
        "posted_at": posted_at or "",
        "salary": salary or "",
    }


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #

def parse_greenhouse(data: dict, company: str, tier: str) -> list[dict]:
    out = []
    for j in (data.get("jobs") or []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_job(
            job_id=j.get("id"),
            title=j.get("title"),
            company=company, tier=tier,
            location=loc,
            url=j.get("absolute_url"),
            posted_at=j.get("first_published") or j.get("updated_at", ""),
        ))
    return out


def parse_ashby(data: dict, company: str, tier: str) -> list[dict]:
    out = []
    for j in (data.get("jobs") or []):
        # Join primary + secondary locations so a US secondary location (e.g.
        # "Remote (US)") is visible to the location filter.
        locs = [j.get("location", "")]
        for sec in (j.get("secondaryLocations") or []):
            if isinstance(sec, dict) and sec.get("location"):
                locs.append(sec["location"])
        location = " | ".join(l for l in locs if l)
        out.append(_job(
            job_id=j.get("id"),
            title=j.get("title"),
            company=company, tier=tier,
            location=location,
            url=j.get("jobUrl") or j.get("applyUrl"),
            posted_at=j.get("publishedAt") or j.get("publishedDate", ""),
            salary=_ashby_salary(j),
        ))
    return out


def _ashby_salary(j: dict) -> str:
    comp = j.get("compensation") or {}
    if isinstance(comp, dict):
        summ = comp.get("compensationTierSummary") or comp.get("summary")
        if summ:
            return str(summ)
    return ""


def parse_lever(data: list, company: str, tier: str) -> list[dict]:
    out = []
    for j in (data or []):
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        out.append(_job(
            job_id=j.get("id"),
            title=j.get("text"),
            company=company, tier=tier,
            location=" | ".join(locs),
            url=j.get("hostedUrl") or j.get("applyUrl"),
            posted_at=str(j.get("createdAt", "")),
            salary=j.get("salaryDescription", "") or "",
        ))
    return out


PARSERS = {"greenhouse": parse_greenhouse, "ashby": parse_ashby, "lever": parse_lever}


# --------------------------------------------------------------------------- #
# Async fan-out
# --------------------------------------------------------------------------- #

async def _fetch_one(session, sems, entry: dict) -> tuple[dict, Optional[list[dict]]]:
    """Fetch + parse one company. Returns (entry, jobs) or (entry, None) on failure."""
    ats = entry["ats"]
    url = ATS_URLS[ats].format(slug=entry["slug"])
    sem = sems[DOMAIN_OF[ats]]
    try:
        async with sem:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("fetch %s (%s) -> HTTP %s", entry["company"], ats, resp.status)
                    return entry, None
                data = await resp.json(content_type=None)
    except Exception as e:  # network/timeout/json — treat as a company-level failure
        logger.warning("fetch %s (%s) failed: %s", entry["company"], ats, e)
        return entry, None

    try:
        jobs = PARSERS[ats](data, entry["company"], entry.get("tier", ""))
    except Exception as e:
        logger.error("parse %s (%s) failed: %s", entry["company"], ats, e)
        return entry, None
    return entry, jobs


async def fetch_all(watchlist: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Fetch every company concurrently.

    Returns (jobs_by_company, failed_entries) where jobs_by_company maps
    company -> list[job] and failed_entries is the entries that errored.
    """
    import aiohttp  # imported here so pure parsers stay importable without aiohttp

    sems = {domain: asyncio.Semaphore(MAX_CONCURRENCY_PER_DOMAIN)
            for domain in DOMAIN_OF.values()}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {"User-Agent": "ats-watcher/1.0", "Accept": "application/json"}

    # Use certifi's CA bundle if present — the Lambda runtime has system certs,
    # but macOS dev machines often don't, which breaks aiohttp's TLS verify.
    connector = None
    try:
        import ssl
        import certifi
        connector = aiohttp.TCPConnector(
            ssl=ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        pass

    jobs_by_company: dict[str, list[dict]] = {}
    failed: list[dict] = []
    async with aiohttp.ClientSession(timeout=timeout, headers=headers,
                                     connector=connector) as session:
        tasks = [_fetch_one(session, sems, e) for e in watchlist]
        for coro in asyncio.as_completed(tasks):
            entry, jobs = await coro
            if jobs is None:
                failed.append(entry)
            else:
                jobs_by_company[entry["company"]] = jobs
    return jobs_by_company, failed
