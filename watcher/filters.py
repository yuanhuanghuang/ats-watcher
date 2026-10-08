"""
filters.py — title/location filtering for ATS Watcher.

Encodes the operator's targeting rules (mid-level US backend/SWE). Pure functions, no
I/O, so they're trivially unit-testable against real job-title samples.

Title logic:
  * Must match an INCLUDE pattern (backend / software engineer / python / ...).
  * Dropped if it matches an EXCLUDE pattern (senior / staff / manager / intern
    / frontend / ML-research / ...), UNLESS it also matches a LEVEL_II pattern
    ("Engineer II", "SWE II", "Engineer 2") — that's her target level, so keep
    it but flag needs_review for the ambiguous collision (e.g. "Senior
    Software Engineer II").

Location logic:
  * Keep US / Remote-US.
  * Drop locations that clearly name a non-US country with no US signal.
  * Missing location -> keep + flag needs_review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Title rules
# --------------------------------------------------------------------------- #

INCLUDE_RE = re.compile(
    r"backend|software engineer|platform engineer|infrastructure engineer|"
    r"api engineer|python|distributed|full.?stack",
    re.IGNORECASE,
)

# Unambiguous seniority/management signals. These stay hard excludes at every
# tier: applying wastes an application slot no matter who is hiring.
# Note: 'co' would be too greedy; 'head of' is spaced.
HARD_LEVEL_RE = re.compile(
    r"\bstaff\b|\bprincipal\b|\bdistinguished\b|\bfellow\b|\blead\b|"
    r"\bmanager\b|\bdirector\b|\bvp\b|head of",
    re.IGNORECASE,
)

# Wrong job or wrong stage, independent of level.
ROLE_EXCLUDE_RE = re.compile(
    r"\bintern\b|new grad|university|campus|entry.level|"
    r"frontend|front.end|mobile|\bios\b|android|machine learning research|"
    r"research scientist|data scientist|security clearance|\bsales\b|"
    r"marketing|designer|recruiter|counsel|accountant",
    re.IGNORECASE,
)

# "Senior" / "Sr" / "Sr." — deliberately NOT a hard exclude. The label is
# inflated at small and mid-size employers, where it often lands on 3-4 years of
# experience, so dropping it outright loses roles that are actually in range.
# Tier C (big tech) is the exception: there the 5+ year bar is real, screening
# is more likely to reject than to level you down, and a wasted application can
# burn a per-company cooldown. See title_matches.
# \bsr\.?\b avoids matching "SRE" or a surname like "Sroka".
SENIOR_RE = re.compile(r"\bsenior\b|\bsr\.?\b", re.IGNORECASE)

# Kept as the union of the two lists above so existing callers and tests that
# import EXCLUDE_RE keep working; title_matches uses the split versions.
EXCLUDE_RE = re.compile(
    HARD_LEVEL_RE.pattern + "|" + ROLE_EXCLUDE_RE.pattern + r"|\bsenior\b",
    re.IGNORECASE,
)
SR_RE = SENIOR_RE

# Target level markers. Word-bounded so "Engineer III" does NOT match "II".
LEVEL_II_RE = re.compile(r"\bengineer\s+(?:ii|2)\b|\bswe\s+(?:ii|2)\b", re.IGNORECASE)


def title_matches(title: str, tier: str = "") -> tuple[bool, bool]:
    """Return (keep, needs_review) for a job title.

    `tier` is the watchlist tier of the hiring company, which only matters for
    the Senior rule — see SENIOR_RE.
    """
    if not title:
        return (False, False)
    t = title.strip()

    if not INCLUDE_RE.search(t):
        return (False, False)

    level_ii = bool(LEVEL_II_RE.search(t))

    # Checked before Senior so "Senior Staff Engineer" resolves as Staff.
    if HARD_LEVEL_RE.search(t) or ROLE_EXCLUDE_RE.search(t):
        # Level-II override: keep the ambiguous collision but flag for review.
        return (True, True) if level_ii else (False, False)

    if SENIOR_RE.search(t):
        # An explicit "II" is a harder level signal than the word "Senior".
        if level_ii:
            return (True, True)
        # Big tech: the 5+ year bar is real and a wasted application can burn a
        # per-company cooldown, so keep dropping these.
        if (tier or "").strip().upper() == "C":
            return (False, False)
        # Everywhere else: surface it flagged, and judge the company yourself.
        return (True, True)

    return (True, False)


# --------------------------------------------------------------------------- #
# Location rules
# --------------------------------------------------------------------------- #

# Signals that a posting is US-based (any of these => keep).
US_RE = re.compile(
    r"united states|u\.?s\.?a\b|\bu\.?s\.?\b|\busa\b|america|"
    r"remote[\s,\-]*\(?\s*us\b|\bus[\s\-]*remote|"
    # ", CA" / ", NY" style US state abbreviations (comma-prefixed to avoid
    # matching words like "in"/"or").
    r",\s*(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|"
    r"mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
    r"wa|wv|wi|wy)\b",
    re.IGNORECASE,
)

# Clearly non-US locations. Kept deliberately broad: a missed country here used
# to mean a silent pass (see location_matches), which is how a Taiwan-based role
# reached the inbox.
NON_US_RE = re.compile(
    r"\bindia\b|bangalore|bengaluru|hyderabad|pune|mumbai|delhi|gurgaon|noida|"
    r"chennai|kolkata|ahmedabad|"
    r"\bcanada\b|toronto|vancouver|montreal|ottawa|calgary|waterloo, on\b|"
    r"united kingdom|\buk\b|\bengland\b|london|manchester|scotland|edinburgh|"
    r"glasgow|\bwales\b|cardiff|belfast|\bleeds\b|"
    r"\bireland\b|dublin|\beurope\b|\bemea\b|"
    r"germany|berlin|munich|hamburg|frankfurt|france|paris|lyon|"
    r"netherlands|amsterdam|rotterdam|spain|madrid|barcelona|valencia|"
    r"portugal|lisbon|porto|poland|warsaw|krakow|romania|bucharest|"
    r"italy|\brome\b|milan|sweden|stockholm|norway|oslo|denmark|copenhagen|"
    r"finland|helsinki|iceland|reykjavik|belgium|brussels|austria|vienna|"
    r"switzerland|zurich|geneva|czech|prague|hungary|budapest|greece|athens|"
    r"bulgaria|serbia|belgrade|croatia|slovakia|slovenia|estonia|tallinn|"
    r"latvia|riga|lithuania|vilnius|ukraine|kyiv|\brussia\b|moscow|"
    r"turkey|\bt(?:ü|u)rkiye\b|istanbul|ankara|"
    r"singapore|\bapac\b|australia|sydney|melbourne|brisbane|perth|"
    r"new zealand|auckland|wellington|"
    r"japan|tokyo|osaka|\bchina\b|beijing|shanghai|shenzhen|guangzhou|"
    r"hong kong|\btaiwan\b|taipei|hsinchu|taichung|kaohsiung|"
    r"korea|seoul|philippines|manila|cebu|indonesia|jakarta|vietnam|hanoi|"
    r"ho chi minh|thailand|bangkok|malaysia|kuala lumpur|"
    r"pakistan|karachi|lahore|bangladesh|dhaka|sri lanka|colombo|nepal|"
    r"\bisrael\b|tel aviv|\buae\b|dubai|abu dhabi|saudi|riyadh|qatar|doha|"
    r"kuwait|bahrain|\bjordan\b|amman|\begypt\b|cairo|"
    r"south africa|johannesburg|cape town|nigeria|lagos|kenya|nairobi|"
    r"\bghana\b|accra|morocco|casablanca|"
    r"\bmexico\b|guadalajara|\bbrazil\b|sao paulo|s(?:ã|a)o paulo|rio de janeiro|"
    r"argentina|buenos aires|\bchile\b|santiago|\bperu\b|\blima\b|"
    r"\blatam\b|colombia|bogota|uruguay|montevideo|costa rica|san jos(?:é|e), cr\b",
    re.IGNORECASE,
)


def location_matches(location: str) -> tuple[bool, bool]:
    """Return (keep, needs_review) for a job location string."""
    if not location or not location.strip():
        return (True, True)  # missing -> keep + flag
    loc = location.strip()

    if US_RE.search(loc):
        return (True, False)
    if NON_US_RE.search(loc):
        return (False, False)
    # Unrecognized and non-empty (bare "Remote", an unlisted country, a new
    # format). Keep it — dropping outright would lose real US postings whose
    # location string we don't parse — but flag it. Passing these silently is
    # what let a Taiwan role through: the blocklist can never be complete, so
    # anything not positively identified as US has to be surfaced for review.
    return (True, True)


# --------------------------------------------------------------------------- #
# Combined
# --------------------------------------------------------------------------- #

@dataclass
class FilterResult:
    keep: bool
    needs_review: bool
    reason: str = ""


def job_matches(title: str, location: str = "", tier: str = "") -> FilterResult:
    """Combine title + location filters for a normalized job."""
    t_keep, t_review = title_matches(title, tier=tier)
    if not t_keep:
        return FilterResult(False, False, "title")

    l_keep, l_review = location_matches(location)
    if not l_keep:
        return FilterResult(False, False, "location")

    return FilterResult(True, t_review or l_review,
                        "review" if (t_review or l_review) else "")
