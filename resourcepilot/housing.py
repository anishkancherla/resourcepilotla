"""On-campus housing options, read from the school's own housing site by the Campus Researcher.

Rules, not a model: from the housing office page (or a hand-checked index page for a few schools)
the Researcher finds the pages that list housing ("housing options", "residence halls", "rates"),
follows the links that read like a place ("Hillenbrand Hall", "Goldin House", "The Plaza"), and
reads each hall's own page. It keeps only short display fields from the page's main content, with
menus, headers, and footers removed:

- name, from the page heading;
- photo, the page's ``og:image``, only when it is https on the school's domain;
- style (traditional, suite, apartment), who it is for, room types, and features, from plain words
  on the page such as "suite-style", "first-year students", "air conditioning";
- the lowest listed price ("$5,054 per semester") with its period, annualized for comparison;
- a short "about" line.

Page text is untrusted evidence: these fields are capped display data and never instructions.
Every fetch goes through ``safe_page`` (https, the school's domain, public addresses, vetted
redirects, a byte cap) within one fetch and time budget. Nothing is guessed: a school whose halls
cannot be read gets the housing office link, as before. ResourcePilot never applies for, reserves,
or pays for housing.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import re
from hashlib import sha256
from typing import Any
from urllib.parse import urldefrag, urlsplit

from resourcepilot.http import Http, Page
from resourcepilot.rates import price_halls
from resourcepilot.research import (
    CONTROL_RE,
    DAY,
    HOUSING_VENDORS,
    TAG_RE,
    WEEK,
    Budget,
    extract_links,
    on_domain,
    recheck_link,
    safe_page,
)

# Checked by hand: the page that lists each school's halls. Other schools are found by link text.
KNOWN_HOUSING: dict[str, tuple[str, ...]] = {
    "purdue.edu": ("https://housing.purdue.edu/my-housing/options/index.html",),
    "gatech.edu": ("https://housing.gatech.edu/rates-fees",),
    "ucr.edu": (
        "https://housing.ucr.edu/ucr-housing-options/residence-halls",
        "https://housing.ucr.edu/ucr-housing-options/campus-apartments",
    ),
}
HALL_FETCHES = 28
HALL_SECONDS = 30.0
MAX_INDEX_PAGES = 4
MAX_HALLS = 20
PARALLEL = 6
NAME_MAX = 60
ABOUT_MAX = 220
# Yearly prices outside this range are a misread (a deposit, a fee, a total for a building).
MIN_YEARLY_CENTS = 1_000_00
MAX_YEARLY_CENTS = 40_000_00

INDEX_LINK = (
    (
        3,
        re.compile(
            r"housing options|where to live|explore housing|find housing|living options"
            r"|all housing|housing communities|our communities|housing types",
            re.I,
        ),
    ),
    (
        2,
        re.compile(
            r"\bresidence halls?\b|\bapartments\b|\bcommunities\b|\bresidential colleges\b"
            r"|\bhalls\b",
            re.I,
        ),
    ),
    (1, re.compile(r"\brates\b|room types|room rates|housing rates", re.I)),
)
# A link that names a category of housing rather than one hall: read it as another index page.
CATEGORY = re.compile(
    r"^(?:on-campus |undergraduate |graduate |family |university |campus )?"
    r"(?:residence halls?|apartments|suites|halls|houses|residential colleges|housing options"
    r"|housing|communities)$",
    re.I,
)
PLACE = re.compile(
    r"\b(?:hall|house|towers?|apartments?|village|commons|court|plaza|residences?|suites"
    r"|terrace|quad|lodge|gardens?|square|place|row|street|avenue)\b",
    re.I,
)
NOISE = re.compile(
    r"association|contract|staff|club|giving|polic|about|honorary|faculty|in residence|support"
    r"|amenit|wi-?fi|move[- ]?(?:in|out)|sample|learning communit|security|dining|council"
    r"|\brates?\b|apply|application|parking|director|executive|guide|handbook|news|faq|\bform\b"
    r"|meal|maintenance|mail|package|job|employ|career|calendar|event|tour|map|contact"
    r"|conference|summer|camp|guest|visitor|policy|community standards|conduct|safety"
    r"|special term|winter break|break housing|temporary|interim|storage",
    re.I,
)
# Housing that is not for students, found under the same housing office.
NOT_STUDENT = re.compile(r"faculty|staff|conference|summer|guest|visitor|employee", re.I)
HOME_PATHS = frozenset({"", "/", "/index.html", "/index.php", "/home", "/default.aspx"})
STRIP_BLOCKS = re.compile(
    r"<(script|style|nav|header|footer|aside|noscript|form|svg|template)\b[^>]*>.*?</\1\s*>",
    re.I | re.S,
)
MAIN = re.compile(r"<(main|article)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.I | re.S)
PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p\s*>", re.I | re.S)
META = re.compile(r"<meta\b[^>]*>", re.I)
ATTR = re.compile(r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

STYLES = (
    ("apartment", re.compile(r"\bapartments?\b|\bapartment-style\b", re.I)),
    ("suite", re.compile(r"\bsuites?\b|\bsuite-style\b|\bpod-style\b", re.I)),
    (
        "traditional",
        re.compile(
            r"\btraditional\b|corridor[- ]style|community (?:bath|restroom)|hall[- ]style"
            r"|shared (?:community )?bathrooms? (?:on|down) (?:the|each) (?:hall|floor)",
            re.I,
        ),
    ),
)
WHO = (
    ("first_year", re.compile(r"first[- ]year|freshm[ae]n|incoming students|new students", re.I)),
    ("transfer", re.compile(r"\btransfer students?\b", re.I)),
    (
        "continuing",
        re.compile(
            r"upper[- ]?(?:class|division)|continuing students|returning students"
            r"|\bsophomores?\b|second[- ]year|\bjuniors?\b|\bseniors?\b",
            re.I,
        ),
    ),
    (
        "graduate",
        re.compile(r"(?<!under)graduate (?:and professional )?students?|grad students", re.I),
    ),
    ("family", re.compile(r"\bfamil(?:y|ies)\b|students with (?:children|dependents)", re.I)),
)
FEATURES = (
    ("ac", re.compile(r"air[- ]condition", re.I)),
    ("kitchen", re.compile(r"\bkitchens?\b|kitchenettes?", re.I)),
    (
        "private_bath",
        re.compile(
            r"private bath|in-?suite bath|in-?room bath|private restroom|own bathroom"
            r"|bathroom (?:in|within) (?:each|every|the) (?:room|suite|unit)",
            re.I,
        ),
    ),
    ("gender_inclusive", re.compile(r"gender[- ](?:inclusive|neutral)|all[- ]gender", re.I)),
    ("accessible", re.compile(r"\baccessib|\bADA\b", re.I)),
)
ROOMS = re.compile(r"\b(single|double|triple|quad)s?\b", re.I)
PRICE = re.compile(
    r"\$\s?(\d{1,2},\d{3}|\d{3,5})(?:\.\d{2})?\s*(?:/|per|a|each|for (?:the|each))?\s*"
    r"(academic year|year|semester|quarter|term|month)",
    re.I,
)
PERIODS_PER_YEAR = {
    "academic year": 1,
    "year": 1,
    "semester": 2,
    "term": 2,
    "quarter": 3,
    "month": 9,
}


def _plain(value: str, limit: int) -> str:
    text = TAG_RE.sub(" ", html_lib.unescape(value or ""))
    return re.sub(r"\s+", " ", CONTROL_RE.sub(" ", text)).strip()[:limit]


def _metas(page_html: str) -> dict[str, str]:
    """``og:*`` and ``description`` meta values from the page head."""
    found: dict[str, str] = {}
    for tag in META.findall(page_html[:200_000]):
        attrs = {name.lower(): a or b for name, a, b in ATTR.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in {"og:title", "og:image", "og:description", "description"} and attrs.get(
            "content"
        ):
            found.setdefault(key, attrs["content"])
    return found


def main_content(page_html: str) -> str:
    """The page's ``<main>`` or ``<article>`` if present, with menus and footers removed."""
    stripped = STRIP_BLOCKS.sub(" ", page_html)
    match = MAIN.search(stripped)
    return match.group(2) if match else stripped


def _photo(url: str, domain: str) -> str:
    parts = urlsplit(url)
    host = str(parts.hostname or "")
    if (
        len(url) > 400
        or parts.scheme != "https"
        or parts.port
        or parts.username
        or not on_domain(host, domain)
    ):
        return ""
    return url


def _price(text: str) -> tuple[int, str]:
    """The lowest listed price, annualized, and how the page put it. (0, "") when none."""
    best: tuple[int, str] = (0, "")
    for amount, period in PRICE.findall(text):
        cents = int(amount.replace(",", "")) * 100
        yearly = cents * PERIODS_PER_YEAR[period.lower()]
        if not MIN_YEARLY_CENTS <= yearly <= MAX_YEARLY_CENTS:
            continue
        if not best[0] or yearly < best[0]:
            best = (yearly, f"${cents // 100:,} per {period.lower()}")
    return best


def _style(name: str, text: str) -> str:
    if re.search(r"apartments?\b", name, re.I):
        return "apartment"
    counts = {style: len(pattern.findall(text)) for style, pattern in STYLES}
    style, count = max(counts.items(), key=lambda item: item[1])
    return style if count else ""


def _who(text: str) -> list[str]:
    """Who a hall is for. A page naming nearly every group is general text, not a rule."""
    groups = [key for key, pattern in WHO if pattern.search(text)]
    return groups if len(groups) <= 3 else []


# An "about" line is shown to the student, so text that reads like a message to a reader or a
# machine (an address, a link, "instructions") is skipped rather than displayed.
NOT_ABOUT = re.compile(r"@|https?://|www\.|instruction|\bprompt\b|\bassistant\b|\bAI\b", re.I)


def _about(metas: dict[str, str], body: str) -> str:
    about = _plain(metas.get("og:description") or metas.get("description") or "", ABOUT_MAX)
    if len(about) >= 40 and not NOT_ABOUT.search(about):
        return about
    for paragraph in PARAGRAPH.findall(body):
        text = _plain(paragraph, ABOUT_MAX)
        if len(text) >= 60 and not NOT_ABOUT.search(text):
            return text
    return ""


def _name(page_html: str, body: str, metas: dict[str, str], link_text: str) -> str:
    """The listing's own link text when it names a place; else the page heading or title."""
    text = _plain(link_text, NAME_MAX)
    headings = [_plain(h, NAME_MAX) for h in (*H1.findall(body), *H1.findall(page_html))]
    for heading in headings:
        # Listing cards often append a tagline: "Falkirk Roomy apartments" for "Falkirk".
        if heading and len(heading) < len(text) and text.lower().startswith(heading.lower()):
            return heading
    if text and not CATEGORY.match(text) and not NOISE.search(text):
        return text.title() if text.isupper() else text
    for candidate in headings:
        name = candidate
        if name and not CATEGORY.match(name) and not NOISE.search(name):
            return name
    title = re.split(r"\s+[|\u2013\u2014-]\s+", _plain(metas.get("og:title", ""), 120))[0]
    title = title[:NAME_MAX]
    if title and not CATEGORY.match(title) and not NOISE.search(title):
        return title
    return link_text[:NAME_MAX]


def read_hall(page: Page, domain: str, link_text: str) -> dict[str, Any] | None:
    """One housing option from its own page, or None when the page is not about a place to live."""
    metas = _metas(page.text)
    body = main_content(page.text)
    text = _plain(body, 60_000)
    if not re.search(r"\b(?:room|residen|student|live|living|floor|bed)", text, re.I):
        return None
    name = _name(page.text, body, metas, link_text)
    if not name:
        return None
    yearly, price_label = _price(text)
    url = normalize(page.url)
    priced = {"price_source": "hall page", "rates_url": url} if price_label else {}
    return {
        "option_id": f"hall_{sha256(url.encode()).hexdigest()[:10]}",
        "name": name,
        "url": url,
        "photo": _photo(metas.get("og:image", ""), domain),
        "style": _style(name, text),
        "who": _who(text),
        "rooms": sorted(
            {room.lower() for room in ROOMS.findall(text)},
            key=["single", "double", "triple", "quad"].index,
        ),
        "features": [key for key, pattern in FEATURES if pattern.search(text)],
        "yearly_cents": yearly,
        "price_label": price_label,
        **priced,
        "about": _about(metas, body),
    }


def _index_links(page: Page, domain: str) -> list[str]:
    """The office's own links to pages that list housing, best first."""
    scored: dict[str, int] = {}
    for url, text in extract_links(page.text, page.url):
        host = str(urlsplit(url).hostname or "")
        if not on_domain(host, domain) or (NOISE.search(text) and "rates" not in text.lower()):
            continue
        score = next((value for value, pattern in INDEX_LINK if pattern.search(text)), 0)
        if score:
            clean, _ = urldefrag(url)
            scored[clean] = max(scored.get(clean, 0), score)
    return sorted(scored, key=lambda link: -scored[link])[:2]


def normalize(url: str) -> str:
    """One spelling per page: no fragment, no ``www.``, no trailing slash."""
    clean, _ = urldefrag(url)
    parts = urlsplit(clean)
    host = str(parts.hostname or "").removeprefix("www.")
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{parts.path.rstrip('/') or '/'}{query}"


def _below(url: str, listing: str) -> bool:
    """A page nested under the listing page, like ``/residence-halls/aberdeen`` under
    ``/residence-halls``: a hall even when its name has no "Hall" in it."""
    link, base = urlsplit(url), urlsplit(listing)
    if link.hostname != base.hostname:
        return False
    root = base.path.rsplit("/", 1)[0] if base.path.endswith((".html", ".php")) else base.path
    root = root.rstrip("/")
    return bool(root) and link.path.startswith(root + "/") and link.path.rstrip("/") != root


def hall_links(page: Page, domain: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(hall links with their text, category pages to read next) on one listing page."""
    halls: list[tuple[str, str]] = []
    categories: list[str] = []
    listing = normalize(page.url)
    for url, text in extract_links(page.text, page.url):
        host = str(urlsplit(url).hostname or "")
        clean = normalize(url)
        path = urlsplit(clean).path
        if (
            not on_domain(host, domain)
            or not 2 < len(text) <= 45
            or path.lower() in HOME_PATHS
            or clean == listing
            or NOT_STUDENT.search(path)
        ):
            continue
        if CATEGORY.match(text):
            categories.append(clean)
        elif not NOISE.search(text) and (PLACE.search(text) or _below(clean, listing)):
            halls.append((clean, text))
    return halls, categories


async def find_housing_options(
    http: Http, domain: str, office_url: str, *, budget: Budget | None = None
) -> dict[str, Any]:
    """Every hall the school lists, read from its own pages; ``status`` says how it went."""
    cache_key = f"research:housing-options:{domain}:v2"
    if http.cache is not None:
        cached = await http.cache.cache_get(cache_key)
        if isinstance(cached, dict) and cached.get("status"):
            return cached
    budget = budget or Budget(fetches=HALL_FETCHES, seconds=HALL_SECONDS)
    queue = list(KNOWN_HOUSING.get(domain, ()))
    fetched: dict[str, Page] = {}
    if not queue and office_url:
        office = await safe_page(http, office_url, domain, budget, HOUSING_VENDORS)
        if office is not None:
            fetched[office.url] = office
            queue = [office.url, *_index_links(office, domain)]
    candidates: dict[str, str] = {}
    seen: set[str] = set()
    listing = ""
    read_pages: list[Page] = list(fetched.values())
    while queue and len(seen) < MAX_INDEX_PAGES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        page = fetched.get(url) or await safe_page(http, url, domain, budget, HOUSING_VENDORS)
        if page is None:
            continue
        if page not in read_pages:
            read_pages.append(page)
        halls, categories = hall_links(page, domain)
        if halls and not listing:
            listing = page.url
        for link, text in halls:
            candidates.setdefault(link, text)
        queue.extend(link for link in categories if link not in seen)
    links = [(url, text) for url, text in candidates.items() if url not in seen][:MAX_HALLS]
    gate = asyncio.Semaphore(PARALLEL)

    async def read(url: str, text: str) -> dict[str, Any] | None:
        async with gate:
            page = await safe_page(http, url, domain, budget, HOUSING_VENDORS)
        return read_hall(page, domain, text) if page is not None else None

    options: list[dict[str, Any]] = []
    names: set[str] = set()
    for option in await asyncio.gather(*(read(url, text) for url, text in links)):
        key = option and (option["url"], option["name"].lower())
        if option and key and key[0] not in names and key[1] not in names:
            names.update(key)
            options.append(option)
    pricing = (
        await price_halls(http, domain, options, read_pages)
        if options
        else {"rates_url": "", "school_rates": []}
    )
    result: dict[str, Any] = {
        "status": "found" if options else "not_found",
        "listing_url": listing,
        "options": options,
        "rates_url": pricing["rates_url"],
        "school_rates": pricing["school_rates"],
        "evidence": (
            f"{len(options)} halls read from {urlsplit(listing).hostname}"
            if options
            else f"No individual halls could be read from {domain}'s housing site"
        ),
    }
    if http.cache is not None:
        await http.cache.cache_put(cache_key, result, WEEK if options else DAY)
    return result


async def recheck_halls(
    http: Http, options: list[dict[str, Any]], domain: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Verification's own re-open of every hall page: (halls that still open, names dropped)."""
    gate = asyncio.Semaphore(PARALLEL)

    async def opens(option: dict[str, Any]) -> bool:
        async with gate:
            return await recheck_link(http, str(option.get("url") or ""), domain)

    results = await asyncio.gather(*(opens(option) for option in options))
    kept = [option for option, ok in zip(options, results, strict=True) if ok]
    dropped = [
        str(option.get("name")) for option, ok in zip(options, results, strict=True) if not ok
    ]
    return kept, dropped
