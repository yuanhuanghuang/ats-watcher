"""
test_fetchers.py — parser tests against real ATS response shapes (trimmed).

Fixtures are minimized real payloads captured from the live APIs, so the field
mappings stay honest. Run: python tests/test_fetchers.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from watcher.fetchers import parse_greenhouse, parse_ashby, parse_lever  # noqa: E402

GREENHOUSE = {
    "jobs": [
        {"id": 8023928, "title": "Backend Engineer",
         "location": {"name": "San Francisco, CA"},
         "absolute_url": "https://boards.greenhouse.io/x/jobs/8023928",
         "first_published": "2026-07-30T06:59:38-04:00",
         "updated_at": "2026-08-04T07:02:31-04:00"},
    ]
}

ASHBY = {
    "apiVersion": "1",
    "jobs": [
        {"id": "34413f8d-26bf", "title": " Security Engineer, Cloud",
         "location": "New York, NY (HQ)",
         "secondaryLocations": [
             {"location": "Remote (US)"},
             {"location": "Miami, FL"}],
         "publishedAt": "2026-04-07T17:12:35.753+00:00",
         "jobUrl": "https://jobs.ashbyhq.com/ramp/34413f8d",
         "applyUrl": "https://jobs.ashbyhq.com/ramp/34413f8d/apply"},
    ]
}

LEVER = [
    {"id": "abc-123", "text": "Software Engineer, Backend",
     "categories": {"location": "Arlington, TX",
                    "allLocations": ["Arlington, TX", "Remote - US"]},
     "hostedUrl": "https://jobs.lever.co/x/abc-123",
     "createdAt": 1712500000000},
]


def test_greenhouse_parse():
    jobs = parse_greenhouse(GREENHOUSE, "Stripe", "A")
    j = jobs[0]
    assert j["job_id"] == "8023928"
    assert j["title"] == "Backend Engineer"
    assert j["company"] == "Stripe" and j["tier"] == "A"
    assert j["location"] == "San Francisco, CA"
    assert j["url"].endswith("/8023928")
    assert j["posted_at"] == "2026-07-30T06:59:38-04:00"


def test_ashby_parse():
    jobs = parse_ashby(ASHBY, "Ramp", "A")
    j = jobs[0]
    assert j["job_id"] == "34413f8d-26bf"
    assert j["title"] == "Security Engineer, Cloud"   # leading space stripped
    # primary + secondary locations joined so US secondary is visible to filter
    assert "New York, NY (HQ)" in j["location"]
    assert "Remote (US)" in j["location"]
    assert j["url"].startswith("https://jobs.ashbyhq.com/ramp/")


def test_lever_parse():
    jobs = parse_lever(LEVER, "Plaid", "A")
    j = jobs[0]
    assert j["job_id"] == "abc-123"
    assert j["title"] == "Software Engineer, Backend"
    assert "Arlington, TX" in j["location"] and "Remote - US" in j["location"]
    assert j["url"].endswith("/abc-123")
    assert j["posted_at"] == "1712500000000"


def test_empty_payloads():
    assert parse_greenhouse({"jobs": []}, "X", "A") == []
    assert parse_ashby({"jobs": []}, "X", "A") == []
    assert parse_lever([], "X", "A") == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1; print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
