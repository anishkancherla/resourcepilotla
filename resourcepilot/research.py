"""Campus Researcher: from a school email domain to the school's official room and housing pages.

Only the domain of the student's email is ever used; the part before ``@`` is dropped before the
request is stored. The Researcher then:

1. Matches the domain to a US institution in IPEDS (Urban Institute Education Data API, keyless).
2. Finds the library's room-booking system: a short hand-checked list, then a
   ``<school>.libcal.com`` probe, then a bounded crawl of the library's own site that follows
   room-booking links and fingerprints LibCal, EMS, 25Live, Skedda, or Robin. When nothing is
   found it asks for the booking link instead of guessing, and checks the pasted link the same way.
3. Finds the housing office and its application portal, plus IPEDS housing facts.
4. For wellness, finds the school's public events calendar (Localist's keyless ``/api/2/events``
   on ``events.<domain>``, ``calendar.<domain>``, or ``<school>.localist.com``) and keeps the
   week's health and wellness events.

Every fetch is https only, limited to the school's own domain or a known booking vendor, refused
for hosts that resolve to private addresses, re-checked on every redirect, size-capped, and
counted against a small fetch and time budget. Page content is untrusted evidence: the
Researcher extracts links and fingerprints from it and never follows instructions in it. From a
structured events feed it keeps only a capped title, start time, location, and event link, which
are shown as data and never acted on.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from resourcepilot.http import Http, Page, ProviderError

IPEDS_YEAR = 2022
IPEDS_BASE = "https://educationdata.urban.org/api/v1/college-university/ipeds"
IPEDS_DIRECTORY = f"{IPEDS_BASE}/directory/{IPEDS_YEAR}/"
IPEDS_CHARACTERISTICS = f"{IPEDS_BASE}/institutional-characteristics/{IPEDS_YEAR}/"
IPEDS_SOURCE = f"IPEDS {IPEDS_YEAR} via Urban Institute Education Data API"
DIRECTORY_CACHE_KEY = f"research:ipeds-directory:{IPEDS_YEAR}:v1"
DIRECTORY_MAX_BYTES = 32 * 1024 * 1024
WEEK = 7 * 24 * 3600
DAY = 24 * 3600
CRAWL_FETCHES = 12
CRAWL_SECONDS = 20.0
MAX_LINKS_PER_PAGE = 400
MAX_FOLLOW_PER_PAGE = 3
MAX_REDIRECTS = 4

# Checked by hand. Every other school goes through the probe and crawl.
KNOWN_ROOM_SYSTEMS: dict[str, dict[str, str]] = {
    "ucla.edu": {"system": "libcal", "host": "calendar.library.ucla.edu"},
}
VENDORS: dict[str, str] = {
    "libcal.com": "libcal",
    "emscloudservice.com": "ems",
    "25live.collegenet.com": "25live",
    "skedda.com": "skedda",
    "robinpowered.com": "robin",
}
HOUSING_VENDORS = ("starrezhousing.com", "starrez.com")
EVENT_VENDORS = ("localist.com",)
# Event photos are shown by the student's browser, never fetched by ResourcePilot.
EVENT_IMAGE_HOSTS = ("localist-images.azureedge.net",)
EVENT_HOSTS = ("events.{d}", "calendar.{d}")
LOCALIST_EVENTS = "/api/2/events"
EVENT_DAYS = 7
EVENT_PAGE_SIZE = 50
MAX_EVENTS = 8
EVENTS_TTL = 30 * 60
WELLNESS_EVENTS = re.compile(
    r"\b(?:health|wellness|well-?being|counsel\w*|mindful\w*|meditat\w*|yoga|flu|vaccin\w*|"
    r"immuni[sz]\w*|clinic|sti|hiv|stress|sleep|nutrition|fitness|mental)\b",
    re.I,
)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SYSTEM_LABELS = {
    "libcal": "LibCal",
    "ems": "EMS",
    "25live": "25Live",
    "skedda": "Skedda",
    "robin": "Robin",
    "page": "Booking page",
}
LIBRARY_STARTS = (
    "https://lib.{d}/",
    "https://library.{d}/",
    "https://libraries.{d}/",
    "https://www.lib.{d}/",
    "https://www.library.{d}/",
    "https://www.{d}/library/",
    "https://www.{d}/libraries/",
)
HOUSING_STARTS = (
    "https://housing.{d}/",
    "https://reslife.{d}/",
    "https://www.housing.{d}/",
    "https://www.{d}/housing/",
    "https://{d}/housing/",
)
ROOM_WORDS = re.compile(
    r"\b(?:reserve|book|booking|reservations?)\b.{0,30}?\b(?:rooms?|spaces?|study)\b"
    r"|\bstudy\s+(?:rooms?|spaces?)\b|\bgroup\s+study\b|\broom\s+(?:reservations?|booking)\b"
    r"|\bcollaborative\s+spaces?\b",
    re.I,
)
# A booking page itself uses booking verbs; "study space" alone is usually an info page.
BOOKING_WORDS = re.compile(
    r"\b(?:reserve|book|booking|reservations?)\b.{0,30}?\b(?:rooms?|spaces?)\b"
    r"|\broom\s+(?:reservations?|booking)\b",
    re.I,
)
HOUSING_WORDS = re.compile(r"\b(?:housing|residence\s+halls?|residential\s+life|dorms?)\b", re.I)
PORTAL_TEXT = re.compile(
    r"\bapply\b|\bapplication\b|\bhousing\s+portal\b|\bmy\s?housing\b|\bhousing\s+contract\b",
    re.I,
)
PORTAL_FIRST = re.compile(r"\bapply\b|\bhousing\s+portal\b|\bmy\s?housing\b", re.I)
NOT_PORTAL = re.compile(r"maintenance|work\s+order|repair|request\s+form", re.I)
ANCHOR_RE = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
LIBCAL_ROOMS_RE = re.compile(r"resources\.push\(\{", re.S)
LIBCAL_PAGE_RE = re.compile(r"springshare|libcal", re.I)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@([a-z0-9.-]{3,253})$")
GENERIC_LABELS = frozenset({"www", "www1", "www2", "web", "home", "main"})
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

STATE_TIMEZONES: dict[str, str] = {
    **dict.fromkeys(
        "CT DE DC FL GA ME MD MA MI NH NJ NY NC OH PA RI SC VT VA WV".split(), "America/New_York"
    ),
    "IN": "America/Indiana/Indianapolis",
    "KY": "America/New_York",
    **dict.fromkeys("AL AR IL IA KS LA MN MS MO NE ND OK SD TN TX WI".split(), "America/Chicago"),
    **dict.fromkeys("CO MT NM UT WY".split(), "America/Denver"),
    "AZ": "America/Phoenix",
    "ID": "America/Boise",
    **dict.fromkeys("CA NV OR WA".split(), "America/Los_Angeles"),
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
    "PR": "America/Puerto_Rico",
    "VI": "America/St_Thomas",
    "GU": "Pacific/Guam",
}
DEFAULT_TIMEZONE = "America/Los_Angeles"
WINDOWS = {
    "morning": ("Morning (8am\u201312pm)", 8 * 60, 12 * 60),
    "afternoon": ("Afternoon (12\u20135pm)", 12 * 60, 17 * 60),
    "evening": ("Evening (5\u201310pm)", 17 * 60, 22 * 60),
    "any": ("Any time (8am\u201310pm)", 8 * 60, 22 * 60),
}


# ---------------------------------------------------------------------------
# Domains and safety
# ---------------------------------------------------------------------------


def school_domain(raw: str) -> str:
    """Return the domain of a school email (or a bare domain). Never returns the local part."""
    value = (raw or "").strip().lower()
    if "@" in value:
        match = EMAIL_RE.match(value)
        if not match:
            raise ValueError("Enter your school email, for example you@purdue.edu")
        value = match.group(1)
    else:
        value = re.sub(r"^https?://", "", value).split("/", 1)[0]
    value = value.removeprefix("www.").strip(".")
    labels = value.split(".")
    if len(labels) < 2 or not all(LABEL_RE.match(label) for label in labels):
        raise ValueError("Enter your school email, for example you@purdue.edu")
    return value


def base_domain(domain: str) -> str:
    """The institution-level domain US schools use: ``cs.purdue.edu`` becomes ``purdue.edu``."""
    return ".".join(domain.split(".")[-2:])


def on_domain(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def vendor_for(host: str) -> str | None:
    host = host.lower()
    return next((system for suffix, system in VENDORS.items() if on_domain(host, suffix)), None)


def host_allowed(host: str, domain: str, extra: tuple[str, ...] = ()) -> bool:
    return (
        on_domain(host, domain)
        or vendor_for(host) is not None
        or any(on_domain(host, suffix) for suffix in extra)
    )


def safe_url(url: str, domain: str, extra: tuple[str, ...] = ()) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.port in (None, 443)
        and not parts.username
        and host_allowed(str(parts.hostname), domain, extra)
    )


@dataclass
class Budget:
    fetches: int = CRAWL_FETCHES
    seconds: float = CRAWL_SECONDS
    started: float = field(default_factory=time.monotonic)

    def take(self) -> bool:
        if self.fetches <= 0 or time.monotonic() - self.started > self.seconds:
            return False
        self.fetches -= 1
        return True


async def safe_page(
    http: Http, url: str, domain: str, budget: Budget, extra: tuple[str, ...] = ()
) -> Page | None:
    """Fetch one https page on an allowed host, vetting every redirect hop. None on failure."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not safe_url(current, domain, extra) or not budget.take():
            return None
        if not await http.public_host(str(urlsplit(current).hostname)):
            return None
        try:
            page = await http.page(current, provider="School website")
        except ProviderError:
            return None
        if page.location and 300 <= page.status < 400:
            current = urljoin(current, page.location)
            continue
        return page if page.status == 200 else None
    return None


def extract_links(page_html: str, base_url: str) -> list[tuple[str, str]]:
    """Absolute https links with their visible text, de-duplicated, capped."""
    found: dict[str, str] = {}
    for href, inner in ANCHOR_RE.findall(page_html):
        url = urljoin(base_url, html_lib.unescape(href.strip()))
        if not url.startswith("https://") or url in found:
            continue
        text = re.sub(r"\s+", " ", html_lib.unescape(TAG_RE.sub(" ", inner))).strip()
        found[url] = text[:120]
        if len(found) >= MAX_LINKS_PER_PAGE:
            break
    return list(found.items())


def _words(url: str) -> str:
    parts = urlsplit(url)
    return re.sub(r"[-_/.?=&]+", " ", f"{parts.path} {parts.query}")


def mentions_rooms(url: str, text: str) -> bool:
    return bool(ROOM_WORDS.search(f"{text} {_words(url)}"))


def mentions_booking(url: str, text: str) -> bool:
    return bool(BOOKING_WORDS.search(f"{text} {_words(url)}"))


def fingerprint(url: str) -> str | None:
    """Name the booking system a link points at, if it is one ResourcePilot recognizes."""
    parts = urlsplit(url)
    host, path = str(parts.hostname or ""), parts.path.lower()
    vendor = vendor_for(host)
    if vendor:
        return vendor
    if "virtualems" in path:
        return "ems"
    if re.search(r"/(?:allspaces|reserve|spaces|booking)\b", path) and (
        "calendar" in host or "libcal" in host or "/reserve" in path or "allspaces" in path
    ):
        return "libcal"
    return None


# ---------------------------------------------------------------------------
# IPEDS: institution and housing facts
# ---------------------------------------------------------------------------


def _row_domain(url_school: str) -> tuple[str, bool]:
    value = re.sub(r"^https?://", "", (url_school or "").strip().lower())
    host, _, path = value.partition("/")
    host = host.removeprefix("www.").strip(".")
    return host, bool(path.strip("/"))


def _common_words(names: list[str]) -> str:
    """The shared leading words, so two campuses never yield a name cut mid-word."""
    token_lists = [re.findall(r"[^\s-]+|[\s-]+", name) for name in names]
    shared: list[str] = []
    for tokens in zip(*token_lists, strict=False):
        if len(set(tokens)) != 1:
            break
        shared.append(tokens[0])
    return "".join(shared).rstrip(" -,")


def build_directory_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map each school web domain to its main institution (largest, then without a URL path)."""
    index: dict[str, dict[str, Any]] = {}
    ranks: dict[str, tuple[int, int, int, int]] = {}
    sharing: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        host, has_path = _row_domain(str(row.get("url_school") or ""))
        if "." not in host or not row.get("unitid"):
            continue
        entry: dict[str, Any] = {
            "unitid": int(row["unitid"]),
            "name": str(row.get("inst_name") or host),
            "city": str(row.get("city") or ""),
            "state": str(row.get("state_abbr") or ""),
        }
        # MIT lists web.mit.edu, so index the institution-level domain too, below exact hosts.
        root = base_domain(host)
        branch = entry
        if root != host and host.split(".")[0] not in GENERIC_LABELS:
            # tacoma.uw.edu is a branch campus: uw.edu names the system, not that campus.
            branch = {**entry, "unitid": 0, "name": str(entry["name"]).split("-")[0].strip()}
        if root != host:
            sharing.setdefault(root, {})[entry["unitid"]] = branch
        for key, exact in ((host, 1), (root, 0)):
            rank = (
                exact,
                int(row.get("currently_active_ipeds") == 1),
                int(row.get("inst_size") or 0),
                int(not has_path),
            )
            if key in index and ranks[key] >= rank:
                continue
            ranks[key] = rank
            index[key] = entry if exact else branch
    for root, entries in sharing.items():
        if ranks[root][0] == 0 and len(entries) > 1:
            # tacoma.uw.edu and bothell.uw.edu share uw.edu: name the system, claim no campus.
            names = [str(item["name"]) for item in entries.values()]
            common = _common_words(names) or root
            states = {str(item["state"]) for item in entries.values()}
            index[root] = {
                "unitid": 0,
                "name": common,
                "city": "",
                "state": states.pop() if len(states) == 1 else "",
            }
    return index


async def directory_index(http: Http) -> dict[str, dict[str, Any]]:
    cache = http.cache
    if cache is not None:
        cached = await cache.cache_get(DIRECTORY_CACHE_KEY)
        if isinstance(cached, dict) and cached:
            return cached
    data = await http.get_json(
        IPEDS_DIRECTORY,
        provider="IPEDS",
        max_bytes=DIRECTORY_MAX_BYTES,
        timeout_seconds=30.0,
    )
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("IPEDS returned the directory in an unexpected format")
    index = build_directory_index(rows)
    if cache is not None and index:
        await cache.cache_put(DIRECTORY_CACHE_KEY, index, WEEK)
    return index


def match_institution(index: dict[str, dict[str, Any]], domain: str) -> dict[str, Any] | None:
    labels = domain.split(".")
    for start in range(len(labels) - 1):
        candidate = ".".join(labels[start:])
        if candidate in index:
            return {**index[candidate], "domain": candidate}
    return None


async def lookup_school(http: Http, domain: str) -> dict[str, str]:
    """Return the school record every later step uses. Raises ValueError for non-schools.

    If IPEDS cannot be read the request fails with a retryable error rather than guessing: room
    times depend on the school's timezone.
    """
    try:
        index = await directory_index(http)
    except ProviderError:
        index = await directory_index(http)  # one retry; the directory is then cached for a week
    institution = match_institution(index, domain)
    if institution is None and not domain.endswith(".edu"):
        raise ValueError(
            f"{domain} is not a US college domain in IPEDS. Use your school email address"
        )
    if institution is None:
        root = base_domain(domain)
        return {
            "domain": root,
            "school_name": root,
            "unitid": "",
            "city": "",
            "state": "",
            "timezone": DEFAULT_TIMEZONE,
            "timezone_note": "Timezone not known; times shown in Pacific time",
        }
    state = str(institution["state"])
    return {
        "domain": str(institution["domain"]),
        "school_name": str(institution["name"]),
        "unitid": str(institution["unitid"] or ""),
        "city": str(institution["city"]),
        "state": state,
        "timezone": STATE_TIMEZONES.get(state, DEFAULT_TIMEZONE),
        "timezone_note": f"Times use {state} local time" if state else "",
    }


def _amount(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def housing_facts(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Readable IPEDS housing facts; negative IPEDS codes mean not reported."""

    def money(value: Any) -> str:
        amount = _amount(value)
        return f"${amount:,} per year (typical)" if amount else "Not reported"

    housing = _amount(row.get("oncampus_housing"))
    beds = _amount(row.get("dormitory_capacity"))
    meals = _amount(row.get("meals_per_week"))
    meal_plan = _amount(row.get("meal_plan"))
    required = _amount(row.get("oncampus_required"))
    facts = [
        ("On-campus housing", {1: "Yes", 0: "No"}.get(housing or 0, "Not reported")),
        ("Dorm beds", f"{beds:,}" if beds else "Not reported"),
        ("Room", money(row.get("typical_room_charge"))),
        ("Board (meals)", money(row.get("typical_board_charge"))),
    ]
    if meal_plan == 1 and meals:
        facts.append(("Meal plan", f"Offered, up to {meals} meals a week"))
    elif meal_plan in (1, 2):
        facts.append(("Meal plan", "Offered"))
    elif meal_plan == 3:
        facts.append(("Meal plan", "Not offered"))
    if required == 1:
        facts.append(("Live on campus", "Required for some students"))
    return facts


async def housing_characteristics(http: Http, unitid: str) -> list[tuple[str, str]]:
    if not unitid:
        return []
    data = await http.get_json(
        IPEDS_CHARACTERISTICS,
        provider="IPEDS",
        params={"unitid": unitid},
        cache_ttl=WEEK,
    )
    rows = data.get("results") if isinstance(data, dict) else None
    return housing_facts(rows[0]) if isinstance(rows, list) and rows else []


# ---------------------------------------------------------------------------
# Room-booking system
# ---------------------------------------------------------------------------


async def confirm_libcal(
    http: Http, host: str, domain: str, budget: Budget, *, must_mention: bool
) -> int:
    """Count the rooms on a LibCal ``/allspaces`` page; 0 means it is not a usable room system."""
    page = await safe_page(http, f"https://{host}/allspaces", domain, budget)
    if page is None:
        return 0
    if must_mention and domain not in page.text.lower():
        # A <slug>.libcal.com site that never links back to this school belongs to someone else.
        return 0
    return len(LIBCAL_ROOMS_RE.findall(page.text))


def _found(
    system: str, booking_url: str, found_by: str, evidence: str, **extra: str
) -> dict[str, Any]:
    return {
        "status": "found",
        "system": system,
        "system_label": SYSTEM_LABELS[system],
        "booking_url": booking_url,
        "found_by": found_by,
        "evidence": evidence,
        **extra,
    }


async def _from_links(http: Http, page: Page, domain: str, budget: Budget) -> dict[str, Any] | None:
    if LIBCAL_PAGE_RE.search(page.text[:200_000]):
        host = str(urlsplit(page.url).hostname)
        rooms = await confirm_libcal(http, host, domain, budget, must_mention=False)
        if rooms:
            return _found(
                "libcal",
                f"https://{host}/allspaces",
                "crawl",
                f"{page.url} is a LibCal page listing {rooms} spaces",
                host=host,
                rooms_listed=str(rooms),
            )
    checked: set[str] = set()
    links = extract_links(page.text, page.url)
    # Try room-booking links first, so the evidence names the right link.
    links.sort(key=lambda link: not mentions_rooms(*link))
    for url, text in links:
        system = fingerprint(url)
        if system is None:
            continue
        host = str(urlsplit(url).hostname)
        if system == "libcal":
            if host in checked:
                continue
            checked.add(host)
            rooms = await confirm_libcal(http, host, domain, budget, must_mention=False)
            if rooms:
                return _found(
                    "libcal",
                    f"https://{host}/allspaces",
                    "crawl",
                    f"LibCal site linked from {page.url}; {rooms} spaces listed",
                    host=host,
                    rooms_listed=str(rooms),
                )
        elif mentions_rooms(url, text):
            return _found(
                system, url, "crawl", f"Linked from {page.url} as \u201c{text or url}\u201d"
            )
    return None


async def crawl_library(http: Http, domain: str, budget: Budget) -> dict[str, Any] | None:
    """Breadth-first over the library site, at most two hops, following room-booking links."""
    queue: list[tuple[Page, int]] = []
    for start in LIBRARY_STARTS:
        page = await safe_page(http, start.format(d=domain), domain, budget)
        if page is not None:
            queue.append((page, 0))
            break
    candidate: dict[str, Any] | None = None
    visited: set[str] = set()
    while queue:
        page, depth = queue.pop(0)
        visited.add(page.url)
        found = await _from_links(http, page, domain, budget)
        if found:
            return found
        if depth >= 2:
            continue
        followed = 0
        for url, text in extract_links(page.text, page.url):
            host = str(urlsplit(url).hostname)
            if url in visited or not on_domain(host, domain) or not mentions_rooms(url, text):
                continue
            visited.add(url)
            if candidate is None and mentions_booking(url, text):
                candidate = _found(
                    "page",
                    url,
                    "crawl",
                    f"Room-booking link on {page.url}: \u201c{text or url}\u201d",
                )
            next_page = await safe_page(http, url, domain, budget)
            if next_page is not None:
                queue.append((next_page, depth + 1))
            followed += 1
            if followed >= MAX_FOLLOW_PER_PAGE:
                break
    return candidate


async def verify_booking_link(http: Http, domain: str, link: str) -> dict[str, Any]:
    """Check a link the student pasted: https, on the school or a booking vendor, and live."""
    url = link.strip()
    if not safe_url(url, domain):
        raise ValueError(f"Paste an https link on {domain} or a booking system such as libcal.com")
    budget = Budget(fetches=4)
    page = await safe_page(http, url, domain, budget)
    if page is None:
        raise ValueError("That link did not open. Copy it from your library's room-booking page")
    found = await _from_links(http, page, domain, budget)
    if found:
        return {**found, "found_by": "student link"}
    system = fingerprint(page.url) or "page"
    host = str(urlsplit(page.url).hostname)
    if system == "libcal":
        rooms = await confirm_libcal(http, host, domain, budget, must_mention=False)
        if rooms:
            return _found(
                "libcal",
                f"https://{host}/allspaces",
                "student link",
                f"The link you pasted is LibCal with {rooms} spaces",
                host=host,
                rooms_listed=str(rooms),
            )
        system = "page"
    return _found(system, page.url, "student link", "The link you pasted opened")


async def find_room_system(http: Http, domain: str, *, budget: Budget | None = None) -> dict:
    """Registry, then ``<slug>.libcal.com``, then the library crawl; ``needs_link`` otherwise."""
    cache_key = f"research:rooms:{domain}:v1"
    if http.cache is not None:
        cached = await http.cache.cache_get(cache_key)
        if isinstance(cached, dict) and cached.get("status"):
            return cached
    budget = budget or Budget()
    result: dict[str, Any] | None = None
    known = KNOWN_ROOM_SYSTEMS.get(domain)
    if known:
        host = known["host"]
        result = _found(
            "libcal",
            f"https://{host}/allspaces",
            "checked list",
            f"Hand-checked LibCal site for {domain}",
            host=host,
        )
    if result is None:
        slug = domain.split(".")[0]
        host = f"{slug}.libcal.com"
        rooms = await confirm_libcal(http, host, domain, budget, must_mention=True)
        if rooms:
            result = _found(
                "libcal",
                f"https://{host}/allspaces",
                "libcal probe",
                f"{host} lists {rooms} spaces and links to {domain}",
                host=host,
                rooms_listed=str(rooms),
            )
    if result is None:
        result = await crawl_library(http, domain, budget)
    if result is None:
        result = {
            "status": "needs_link",
            "system": "",
            "system_label": "",
            "booking_url": "",
            "found_by": "",
            "evidence": f"No room-booking system found on {domain}'s library site",
        }
    if http.cache is not None:
        await http.cache.cache_put(cache_key, result, WEEK if result["status"] == "found" else DAY)
    return result


# ---------------------------------------------------------------------------
# Housing office and portal
# ---------------------------------------------------------------------------


async def find_housing_pages(
    http: Http, domain: str, *, budget: Budget | None = None
) -> dict[str, str]:
    cache_key = f"research:housing:{domain}:v1"
    if http.cache is not None:
        cached = await http.cache.cache_get(cache_key)
        if isinstance(cached, dict):
            return cached
    budget = budget or Budget(fetches=8)
    office: Page | None = None
    for start in HOUSING_STARTS:
        page = await safe_page(http, start.format(d=domain), domain, budget, HOUSING_VENDORS)
        if page is not None and HOUSING_WORDS.search(TAG_RE.sub(" ", page.text[:60_000])):
            office = page
            break
    result = {"office_url": "", "portal_url": "", "portal_label": ""}
    if office is not None:
        result["office_url"] = office.url
        ranked: list[tuple[int, str, str]] = []
        for url, text in extract_links(office.text, office.url):
            vendor = any(on_domain(str(urlsplit(url).hostname), v) for v in HOUSING_VENDORS)
            # Only the housing office's own pages or its portal vendor: an "Apply" link elsewhere
            # on the school's site is usually admissions, not housing.
            if not (vendor or _within(url, office.url)):
                continue
            if NOT_PORTAL.search(f"{text} {url}") or url.rstrip("/") == office.url.rstrip("/"):
                continue
            if not (PORTAL_TEXT.search(text) or vendor):
                continue
            score = 0 if vendor else 1 if PORTAL_FIRST.search(text) else 2
            ranked.append((score, url, text))
        if ranked:
            _, url, text = min(ranked, key=lambda item: item[0])
            result["portal_url"], result["portal_label"] = url, text[:60]
    if http.cache is not None:
        await http.cache.cache_put(cache_key, result, WEEK if result["office_url"] else DAY)
    return result


def _within(url: str, office_url: str) -> bool:
    """On the office's host (or a subdomain of it) and under the office's path."""
    link, office = urlsplit(url), urlsplit(office_url)
    host, office_host = str(link.hostname), str(office.hostname)
    if host != office_host:
        return on_domain(host, office_host)
    return link.path.startswith(office.path.rstrip("/") + "/") or office.path in ("", "/")


async def recheck_link(http: Http, url: str, domain: str) -> bool:
    """Verification's independent re-check that an official link still opens on the school."""
    if not url:
        return False
    page = await safe_page(http, url, domain, Budget(fetches=MAX_REDIRECTS + 1), HOUSING_VENDORS)
    return page is not None


# ---------------------------------------------------------------------------
# Wellness events
# ---------------------------------------------------------------------------


def _plain(value: Any, limit: int) -> str:
    """Display text from a feed field: no tags, no control characters, one line, capped."""
    text = TAG_RE.sub(" ", html_lib.unescape(str(value or "")))
    return re.sub(r"\s+", " ", CONTROL_RE.sub(" ", text)).strip()[:limit]


async def _localist(
    http: Http, url: str, domain: str, budget: Budget
) -> tuple[dict[str, Any], str] | None:
    """A Localist events response and the host that served it, or None."""
    page = await safe_page(http, url, domain, budget, EVENT_VENDORS)
    if page is None:
        return None
    try:
        data = json.loads(page.text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    return data, str(urlsplit(page.url).hostname)


async def find_events_feed(http: Http, domain: str, *, budget: Budget | None = None) -> dict:
    """The school's public Localist calendar, or ``not_found``. Never guessed, never asked for."""
    cache_key = f"research:events:{domain}:v1"
    if http.cache is not None:
        cached = await http.cache.cache_get(cache_key)
        if isinstance(cached, dict) and cached.get("status"):
            return cached
    budget = budget or Budget(fetches=6)
    result: dict[str, Any] = {
        "status": "not_found",
        "feed_host": "",
        "evidence": f"No public events calendar found on {domain}",
    }
    for pattern in EVENT_HOSTS:
        found = await _localist(
            http, f"https://{pattern.format(d=domain)}{LOCALIST_EVENTS}?pp=1", domain, budget
        )
        if found:
            result = {
                "status": "found",
                "feed_host": found[1],
                "evidence": f"Public Localist events feed on {found[1]}",
            }
            break
    if result["status"] != "found":
        host = f"{domain.split('.')[0]}.localist.com"
        home = await safe_page(http, f"https://{host}/", domain, budget, EVENT_VENDORS)
        # A <slug>.localist.com calendar that never links back to this school is someone else's.
        if home is not None and domain in home.text.lower():
            found = await _localist(http, f"https://{host}{LOCALIST_EVENTS}?pp=1", domain, budget)
            if found:
                result = {
                    "status": "found",
                    "feed_host": found[1],
                    "evidence": f"{host} is a Localist calendar that links to {domain}",
                }
    if http.cache is not None:
        await http.cache.cache_put(cache_key, result, WEEK if result["status"] == "found" else DAY)
    return result


def _filter_names(filters: Any) -> list[str]:
    names: list[str] = []
    if isinstance(filters, dict):
        for items in filters.values():
            if isinstance(items, list):
                names.extend(
                    str(item.get("name") or "") for item in items if isinstance(item, dict)
                )
    return names


def event_photo(value: Any, domain: str) -> str:
    """An event's photo URL if it is https on the school or Localist's image host, else ""."""
    url = str(value or "")
    parts = urlsplit(url)
    host = str(parts.hostname or "")
    allowed = on_domain(host, domain) or any(on_domain(host, h) for h in EVENT_IMAGE_HOSTS)
    if len(url) > 400 or parts.scheme != "https" or parts.port or parts.username or not allowed:
        return ""
    return url


def wellness_event(raw: Any, domain: str, zone: ZoneInfo, first_day: date) -> dict[str, str] | None:
    """One health or wellness event from a Localist feed item, or None."""
    event = raw.get("event") if isinstance(raw, dict) else None
    if not isinstance(event, dict):
        return None
    title = _plain(event.get("title"), 80)
    url = str(event.get("localist_url") or "")
    if not title or not safe_url(url, domain, EVENT_VENDORS):
        return None
    labels = [title, *map(str, event.get("keywords") or []), *map(str, event.get("tags") or [])]
    labels += _filter_names(event.get("filters"))
    if not WELLNESS_EVENTS.search(" ".join(labels)):
        return None
    starts: list[tuple[datetime, bool]] = []
    for item in event.get("event_instances") or []:
        instance = item.get("event_instance") if isinstance(item, dict) else None
        if not isinstance(instance, dict):
            continue
        try:
            start = datetime.fromisoformat(str(instance.get("start"))).astimezone(zone)
        except ValueError:
            continue
        if start.date() >= first_day:
            starts.append((start, bool(instance.get("all_day"))))
    if not starts:
        return None
    start, all_day = min(starts)
    when = f"{start:%a %b} {start.day}"
    if not all_day:
        when += f" \u00b7 {start:%-I:%M %p}".replace(":00", "")
    return {
        "event_id": _plain(event.get("id"), 20),
        "photo": event_photo(event.get("photo_url"), domain),
        "title": title,
        "starts_at": start.isoformat(),
        "when": when,
        "location": _plain(event.get("location_name"), 60),
        "url": url,
    }


async def wellness_events(
    http: Http,
    feed_host: str,
    domain: str,
    day: str,
    timezone: str,
    *,
    budget: Budget | None = None,
) -> list[dict[str, str]]:
    """The week's health and wellness events, soonest first. Raises ProviderError on failure."""
    url = f"https://{feed_host}{LOCALIST_EVENTS}?start={day}&days={EVENT_DAYS}&pp={EVENT_PAGE_SIZE}"
    found = await _localist(http, url, domain, budget or Budget(fetches=MAX_REDIRECTS + 1))
    if found is None:
        raise ProviderError("The events calendar did not answer")
    zone = ZoneInfo(timezone)
    first_day = date.fromisoformat(day)
    events = [
        event
        for raw in found[0]["events"]
        if (event := wellness_event(raw, domain, zone, first_day)) is not None
    ]
    unique = {event["url"]: event for event in sorted(events, key=lambda e: e["starts_at"])}
    return list(unique.values())[:MAX_EVENTS]


async def wellness_research(http: Http, school: dict[str, str], day: str) -> dict[str, Any]:
    """Events for the wellness card. A miss or an outage is reported, never a failure."""
    feed = await find_events_feed(http, school["domain"])
    out: dict[str, Any] = {**feed, "day": day, "events": []}
    if feed["status"] != "found":
        return out
    cache_key = f"research:events-list:{feed['feed_host']}:{day}:v2"
    if http.cache is not None:
        cached = await http.cache.cache_get(cache_key)
        if isinstance(cached, list):
            return {**out, "events": cached}
    try:
        events = await wellness_events(
            http, feed["feed_host"], school["domain"], day, school["timezone"]
        )
    except ProviderError as exc:
        return {**out, "status": "unavailable", "evidence": str(exc)}
    if http.cache is not None:
        await http.cache.cache_put(cache_key, events, EVENTS_TTL)
    return {**out, "events": events}


async def recheck_events(
    http: Http, wellness: dict[str, Any], school: dict[str, str]
) -> dict[str, Any]:
    """Verification's independent re-fetch of the feed: keep only events it still lists."""
    events = list(wellness.get("events") or [])
    if not events or not wellness.get("feed_host"):
        return {**wellness, "unverified": []}
    try:
        live = await wellness_events(
            http,
            str(wellness["feed_host"]),
            school["domain"],
            str(wellness["day"]),
            school["timezone"],
        )
    except ProviderError:
        live = []
    still = {event["url"] for event in live}
    kept = [event for event in events if event.get("url") in still]
    dropped = [str(event.get("title", "")) for event in events if event.get("url") not in still]
    return {**wellness, "events": kept, "unverified": dropped}


# ---------------------------------------------------------------------------
# Windows and the handler
# ---------------------------------------------------------------------------


def room_window(day: str, window: str, timezone: str, *, now: datetime) -> tuple[str, str]:
    """The search window in the school's own timezone, starting no earlier than now."""
    zone = ZoneInfo(timezone)
    _, low, high = WINDOWS[window]
    selected = date.fromisoformat(day)
    start = datetime.combine(selected, datetime.min.time(), zone) + timedelta(minutes=low)
    end = datetime.combine(selected, datetime.min.time(), zone) + timedelta(minutes=high)
    local_now = now.astimezone(zone)
    if start < local_now:
        # Round up to the next half hour, the granularity LibCal books in.
        rounded = local_now.replace(second=0, microsecond=0)
        extra = (30 - rounded.minute % 30) % 30 or (30 if rounded < local_now else 0)
        start = rounded + timedelta(minutes=extra)
    return start.isoformat(), end.isoformat()


def campus_context(school: dict[str, str], rooms: dict[str, Any], brief: dict[str, Any]) -> dict:
    """String-only context for Campus Rooms, carried in ResourceHold 1.1.0 requests."""
    context = {
        "domain": school["domain"],
        "school_name": school["school_name"],
        "timezone": school["timezone"],
        "system": str(rooms.get("system", "")),
        "system_label": str(rooms.get("system_label", "")),
        "booking_url": str(rooms.get("booking_url", "")),
        "libcal_host": str(rooms.get("host", "")) if rooms.get("system") == "libcal" else "",
        "group_size": str(brief.get("group_size", 1)),
    }
    if brief.get("duration_minutes"):
        context["duration_minutes"] = str(brief["duration_minutes"])
    return context


async def research(payload: dict[str, Any], http: Http, *, now: datetime) -> dict[str, Any]:
    brief = dict(payload["brief"])
    service = str(brief["service"])
    school = await lookup_school(http, str(brief["school_domain"]))
    out: dict[str, Any] = {**payload, "school": school}
    if service == "housing":
        pages = await find_housing_pages(http, school["domain"])
        try:
            facts = await housing_characteristics(http, school["unitid"])
        except ProviderError:
            facts = []
        out["housing"] = {**pages, "facts": facts, "facts_source": IPEDS_SOURCE if facts else ""}
        if not facts and not pages["office_url"]:
            raise ValueError(
                f"No housing facts or housing office found for {school['school_name']}"
            )
        return out

    if service == "wellness":
        # Rooms are optional here: a school without a public room system still gets events and
        # the crisis line, so a miss never pauses to ask for a link.
        rooms = await find_room_system(http, school["domain"])
        out["rooms"] = rooms
        start, end = room_window(
            str(brief["day"]), str(brief["window"]), school["timezone"], now=now
        )
        if start >= end:
            raise ValueError(f"That time has already passed at {school['school_name']}")
        brief.update(window_start=start, window_end=end, timezone=school["timezone"])
        out["brief"] = brief
        if rooms["status"] == "found":
            out["campus"] = campus_context(school, rooms, brief)
        out["wellness"] = await wellness_research(http, school, str(brief["day"]))
        return out

    if brief.get("booking_link"):
        link = str(brief.pop("booking_link"))
        out["brief"] = brief
        try:
            rooms = await verify_booking_link(http, school["domain"], link)
        except ValueError as exc:
            # Ask again instead of failing the request: the student may have pasted the wrong page.
            out["rooms"] = {"status": "needs_link", "evidence": str(exc)}
            return {**out, "needs_link": True, "link_error": str(exc)}
    else:
        rooms = await find_room_system(http, school["domain"])
    out["rooms"] = rooms
    if rooms["status"] != "found":
        out["needs_link"] = True
        return out
    if service == "rooms":
        start, end = room_window(
            str(brief["day"]), str(brief["window"]), school["timezone"], now=now
        )
        if start >= end:
            raise ValueError(f"That time has already passed at {school['school_name']}")
        brief.update(window_start=start, window_end=end, timezone=school["timezone"])
        out["brief"] = brief
    out["campus"] = campus_context(school, rooms, brief)
    return out
