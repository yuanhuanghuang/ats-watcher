"""
test_filters.py — title/location filter rules against real job-title samples.

Run: python -m pytest tests/test_filters.py -q
(or: python tests/test_filters.py  for a dependency-free run)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from watcher.filters import title_matches, location_matches, job_matches  # noqa: E402

# --------------------------------------------------------------------------- #
# Title samples (from the operator's spec)
# --------------------------------------------------------------------------- #

SHOULD_KEEP = [
    "Backend Engineer, Python",
    "Software Engineer II",
    "Software Engineer, Ambient AI",
    "Platform Engineer",
    "Full Stack Engineer",
]

SHOULD_DROP = [
    "Staff Software Engineer",
    "Principal Software Engineer",
    "Senior Staff Software Engineer",   # Staff wins over Senior
    "Lead Software Engineer",
    "Director of Software Engineering",
    "Head of Platform Engineering",
    "Backend Engineer, New Grad",
    "Engineering Manager",
    "Frontend Engineer",
    "Machine Learning Research Scientist",
]

# Keep, but flag for manual review.
SHOULD_REVIEW = [
    "Senior Software Engineer II",      # senior + II collision
    # "Senior" is no longer a hard drop outside tier C: the label is inflated at
    # small and mid-size employers, so these surface flagged for a judgement
    # call on the company rather than being discarded.
    "Senior Backend Engineer",
    "Sr. Software Engineer",
]

# Tier C (big tech) keeps the strict rule: there the 5+ year bar is real and a
# wasted application can burn a per-company cooldown.
SHOULD_DROP_AT_TIER_C = [
    "Senior Backend Engineer",
    "Sr. Software Engineer",
    "Senior Software Engineer",
]


def test_titles_should_keep():
    for t in SHOULD_KEEP:
        keep, review = title_matches(t)
        assert keep, f"expected KEEP: {t!r}"
        assert not review, f"expected no review flag: {t!r}"


def test_titles_should_drop():
    for t in SHOULD_DROP:
        keep, _ = title_matches(t)
        assert not keep, f"expected DROP: {t!r}"


def test_titles_should_keep_and_review():
    for t in SHOULD_REVIEW:
        keep, review = title_matches(t)
        assert keep, f"expected KEEP: {t!r}"
        assert review, f"expected review flag: {t!r}"


def test_senior_dropped_at_tier_c():
    for t in SHOULD_DROP_AT_TIER_C:
        keep, _ = title_matches(t, tier="C")
        assert not keep, f"expected DROP at tier C: {t!r}"
        # ...but the same title survives, flagged, at A/B.
        for tier in ("A", "B"):
            keep, review = title_matches(t, tier=tier)
            assert keep, f"expected KEEP at tier {tier}: {t!r}"
            assert review, f"expected review flag at tier {tier}: {t!r}"


def test_hard_level_terms_dropped_at_every_tier():
    for tier in ("A", "B", "C", ""):
        for t in ("Staff Software Engineer", "Principal Backend Engineer",
                  "Lead Software Engineer", "Engineering Manager, Platform"):
            keep, _ = title_matches(t, tier=tier)
            assert not keep, f"expected DROP at tier {tier!r}: {t!r}"


def test_level_iii_not_treated_as_ii():
    # "Engineer III" is a senior level and must NOT be rescued by the II
    # override. A real level-II title is a clean keep, so the distinguishing
    # assertion is that III never gets one: at tier C it is dropped outright,
    # and elsewhere it only survives flagged, via the Senior rule.
    t = "Senior Software Engineer III"
    assert title_matches(t, tier="C")[0] is False
    for tier in ("A", "B", ""):
        keep, review = title_matches(t, tier=tier)
        assert not (keep and not review), f"III must never be a clean keep: {tier!r}"
    assert title_matches("Software Engineer II", tier="A") == (True, False)


# --------------------------------------------------------------------------- #
# Location samples
# --------------------------------------------------------------------------- #

def test_locations_us_kept():
    for loc in ["San Francisco, CA", "New York, NY", "Remote (US)",
                "United States", "Austin, TX", "Remote - US"]:
        keep, _ = location_matches(loc)
        assert keep, f"expected US KEEP: {loc!r}"


def test_locations_non_us_dropped():
    for loc in ["Bangalore, India", "Toronto, Canada", "London, UK",
                "Berlin, Germany", "Singapore", "Remote (Canada)"]:
        keep, _ = location_matches(loc)
        assert not keep, f"expected non-US DROP: {loc!r}"


def test_location_missing_kept_and_flagged():
    keep, review = location_matches("")
    assert keep and review


def test_us_office_locations_without_state_abbrev_kept():
    # Regression: a trailing '|' in NON_US_RE made these match "non-US" and drop.
    for loc in ["San Francisco HQ", "New York City Office", "Seattle Office",
                "San Francisco HQ | Seattle Office"]:
        keep, _ = location_matches(loc)
        assert keep, f"expected KEEP (US office): {loc!r}"


def test_job_matches_combines():
    r = job_matches("Backend Engineer, Python", "San Francisco, CA")
    assert r.keep and not r.needs_review
    r = job_matches("Backend Engineer", "London, UK")
    assert not r.keep and r.reason == "location"
    r = job_matches("Senior Software Engineer II", "")
    assert r.keep and r.needs_review


# --------------------------------------------------------------------------- #
# Dependency-free runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
