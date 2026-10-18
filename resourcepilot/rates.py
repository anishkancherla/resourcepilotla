"""Housing prices from a school's own rates pages and rate PDFs, for the Campus Researcher.

Schools publish hall prices in different places. After the halls are read, prices are taken from,
best first:

1. the hall's own page (read with the hall, in ``resourcepilot.housing``);
2. a rates web table: a row with a dollar amount whose cells name the hall, such as Georgia
   Tech's "Double Traditional · $3,919/semester · ... Towers";
3. the hall's rate PDF, linked from the rates page under the hall's name, such as UCR's
   "Aberdeen-Inverness" schedule: only the yearly row ("Annual cost", "Academic year cost") and
   only its room columns, which the header names (single, double, triple, ...) before any dining
   plan, are read.

Rows that name a room type but no hall ("Doubles $3,970") are kept as school-wide rates and are
never attributed to a hall. Anything that cannot be read with confidence gets no number: the
hall keeps the official rates link instead. PDFs are untrusted documents: fetched under the same
rules as every page (https on the school's domain, public addresses, vetted redirects), capped at
3MB, required to start with ``%PDF-``, parsed for two pages at most, in a worker thread with a
time limit, and only dollar amounts are taken from them.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import io
import re
from typing import Any
from urllib.parse import urlsplit

from resourcepilot.http import Http, Page
from resourcepilot.research import (
    CONTROL_RE,
    HOUSING_VENDORS,
    TAG_RE,
    Budget,
    extract_links,
    on_domain,
    safe_page,
)

# Checked by hand: the page where a school lists its housing rates.
KNOWN_RATES: dict[str, tuple[str, ...]] = {
    "ucr.edu": ("https://housing.ucr.edu/get-housing/rates-billing-terms",),
    "gatech.edu": ("https://housing.gatech.edu/rates-fees",),
    "purdue.edu": ("https://housing.purdue.edu/my-housing/info/housing-rates.html",),
    "ncsu.edu": ("https://housing.dasa.ncsu.edu/residential-communities/costs/",),
}
PRICE_FETCHES = 16
PRICE_SECONDS = 15.0
MAX_RATE_PAGES = 2
MAX_PDFS = 10
PDF_MAX_BYTES = 3_000_000
PDF_PAGES = 2
PDF_SECONDS = 5.0
PARALLEL = 4
MAX_SCHOOL_RATES = 6
# A yearly room price outside this range is a misread: a deposit, a fee, a building total.
MIN_YEARLY_CENTS = 1_000_00
MAX_YEARLY_CENTS = 40_000_00

RATES_LINK = re.compile(r"\brates?\b|\bcosts?\b|\bfees\b|\bbilling\b|\bpricing\b", re.I)
NOT_RATES = re.compile(r"conference|guest|summer|parking|dining|meal|camp|visitor", re.I)
TABLE = re.compile(r"<table\b.*?</table\s*>", re.I | re.S)
HEADING = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]\s*>", re.I | re.S)
PANEL = re.compile(r"""(?:\bid|aria-labelledby)\s*=\s*["']([^"']+)["']""", re.I)
# A table under one of these headings or tabs is not the regular academic-year rate.
OTHER_TABLE = re.compile(
    r"summer|intersession|winter[- ]break|study[- ]abroad|transition|conference|guest|\bfees?\b",
    re.I,
)
ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.I | re.S)
CELL = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]\s*>", re.I | re.S)
AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.\d{2})?")
PERIOD = re.compile(r"academic year|annual|year|semester|term|quarter|month", re.I)
PERIODS_PER_YEAR = {
    "academic year": 1,
    "annual": 1,
    "year": 1,
    "semester": 2,
    "term": 2,
    "quarter": 3,
    "month": 9,
}
NOT_A_RATE = re.compile(
    r"\bfees?\b|deposit|resnet|activity|application|parking|late|cancel|damage|key\b|laundry"
    r"|meal|dining|board\b|summer|nightly|per night|guest",
    re.I,
)
ROOM_TYPE = re.compile(
    r"\b(?:single|double|triple|quad|studio|bedroom|suite|apartment|traditional)s?\b", re.I
)
YEARLY_ROW = re.compile(
    r"^\s*(?:annual|academic[- ]year|yearly|total(?: annual| academic year)?)\b", re.I
)
ROOM_COLUMN = re.compile(r"\b(?:single|double|triple|quad|studio)s?\b", re.I)
DINING_COLUMN = re.compile(
    r"\b(?:dining|meals?|board|plans?|swipes?|unlimited|block|bucks|dollars)\b", re.I
)


def _plain(value: str, limit: int = 400) -> str:
    text = TAG_RE.sub(" ", html_lib.unescape(value or ""))
    return re.sub(r"\s+", " ", CONTROL_RE.sub(" ", text)).strip()[:limit]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def names_hall(text: str, name: str) -> bool:
    """True when ``text`` names the hall as whole words ("Towers" in "Glenn Hopkins Towers")."""
    key = _key(name)
    if len(key) < 4:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", _key(text)) is not None


def _cents(amount: str) -> int:
    return int(amount.replace(",", "")) * 100


def _period(*texts: str) -> str:
    for text in texts:
        match = PERIOD.search(text or "")
        if match:
            return match.group(0).lower()
    return ""


def priced(cents: int, period: str) -> tuple[int, str]:
    """(yearly cents or 0 when the period is unknown, how the school put it)."""
    label = f"${cents // 100:,}" + (f" per {period}" if period else " (period not stated)")
    yearly = cents * PERIODS_PER_YEAR[period] if period else 0
    if yearly and not MIN_YEARLY_CENTS <= yearly <= MAX_YEARLY_CENTS:
        return 0, ""
    return yearly, label


def read_tables(page: Page, halls: list[dict[str, Any]]) -> tuple[dict[str, tuple], list[dict]]:
    """Prices for named halls, and school-wide room-type rates, from the page's tables.

    Returns ({option_id: (yearly_cents, label, url)}, [school rate rows]).
    """
    found: dict[str, tuple[int, str, str]] = {}
    school: list[dict[str, str]] = []
    previous = 0
    for match in TABLE.finditer(page.text):
        table = match.group(0)
        before = page.text[max(previous, match.start() - 3000) : match.start()]
        previous = match.end()
        labels = [_plain(text, 80) for text in HEADING.findall(before)[-2:]]
        labels += PANEL.findall(before)[-2:]
        if OTHER_TABLE.search(" ".join(labels)):
            continue
        # "Housing rates listed below are charged by semester": the period for the whole table.
        intro = _plain(before, 3000)[-300:]
        header: list[str] = []
        for row in ROW.findall(table):
            cells = [_plain(cell) for cell in CELL.findall(row)]
            if not cells:
                continue
            if not any(AMOUNT.search(cell) for cell in cells):
                header = header or cells
                continue
            label = cells[0]
            if NOT_A_RATE.search(label):
                continue
            # The most recent column wins when a table lists several years.
            index = max(i for i, cell in enumerate(cells) if AMOUNT.search(cell))
            amount = AMOUNT.search(cells[index])
            assert amount is not None
            column = header[index] if index < len(header) else ""
            nearest = PERIOD.findall(intro)
            period = _period(cells[index], column, " ".join(header), (nearest or [""])[-1])
            yearly, price_label = priced(_cents(amount.group(1)), period)
            if not price_label:
                continue
            text = " ".join(cells[1:])
            named = [hall for hall in halls if names_hall(text, str(hall["name"]))]
            for hall in named:
                best = found.get(hall["option_id"])
                if best is None or (yearly and (not best[0] or yearly < best[0])):
                    found[hall["option_id"]] = (yearly, price_label, page.url)
            if not named and ROOM_TYPE.search(label) and len(school) < MAX_SCHOOL_RATES:
                school.append({"label": label[:60], "price": price_label, "column": column[:40]})
    return found, school


def room_range(text: str) -> tuple[int, int] | None:
    """The yearly room price range from a rate schedule's text, or None when unsure.

    The header names the room columns (single, double, ...) before any dining-plan column; only
    those leading amounts of the yearly row are room prices.
    """
    lines = text.splitlines()
    rooms = 0
    for line in lines:
        if "$" not in line and ROOM_COLUMN.search(line):
            before = DINING_COLUMN.split(line, maxsplit=1)[0]
            rooms = len(ROOM_COLUMN.findall(before))
            break
    if not rooms:
        return None
    for line in lines:
        if not YEARLY_ROW.match(line):
            continue
        amounts = [_cents(amount) for amount in AMOUNT.findall(line)]
        if len(amounts) < rooms:
            continue
        room = [cents for cents in amounts[:rooms] if MIN_YEARLY_CENTS <= cents <= MAX_YEARLY_CENTS]
        if len(room) == rooms:
            return min(room), max(room)
    return None


def read_rate_pdf(data: bytes) -> tuple[int, int] | None:
    """Room price range from a rate PDF's first pages, or None for anything unreadable."""
    if not data.startswith(b"%PDF-"):
        return None
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:PDF_PAGES])[:40_000]
    except Exception:  # a malformed school PDF must never fail the lookup
        return None
    return room_range(text)


def _rate_links(page: Page, domain: str) -> list[str]:
    links = []
    for url, text in extract_links(page.text, page.url):
        host = str(urlsplit(url).hostname or "")
        if on_domain(host, domain) and RATES_LINK.search(text) and not NOT_RATES.search(text):
            links.append(url.split("#")[0])
    return links


def _pdf_links(page: Page, domain: str) -> list[tuple[str, str]]:
    return [
        (url, text)
        for url, text in extract_links(page.text, page.url)
        if urlsplit(url).path.lower().endswith(".pdf")
        and on_domain(str(urlsplit(url).hostname or ""), domain)
    ]


async def price_halls(
    http: Http,
    domain: str,
    halls: list[dict[str, Any]],
    pages: list[Page],
    *,
    budget: Budget | None = None,
) -> dict[str, Any]:
    """Fill in each hall's price from the school's rates pages and PDFs, in place.

    Returns the school-wide rates page link and room-type rates for the cards.
    """
    budget = budget or Budget(fetches=PRICE_FETCHES, seconds=PRICE_SECONDS)
    by_url = {page.url: page for page in pages}
    wanted = [*KNOWN_RATES.get(domain, ())]
    for page in pages:
        wanted.extend(_rate_links(page, domain))
    rate_pages: list[Page] = []
    for url in dict.fromkeys(wanted):
        if len(rate_pages) >= MAX_RATE_PAGES:
            break
        rate_page = by_url.get(url) or await safe_page(http, url, domain, budget, HOUSING_VENDORS)
        if rate_page is not None and rate_page not in rate_pages:
            rate_pages.append(rate_page)
    school_rates: list[dict[str, str]] = []
    for page in [*pages, *(page for page in rate_pages if page not in pages)]:
        found, school = read_tables(page, halls)
        for hall in halls:
            if hall["option_id"] in found and not hall.get("price_label"):
                yearly, label, url = found[hall["option_id"]]
                hall.update(
                    yearly_cents=yearly,
                    price_label=label,
                    price_source="rates table",
                    rates_url=url,
                )
        for rate in school:
            if rate not in school_rates and len(school_rates) < MAX_SCHOOL_RATES:
                school_rates.append(rate)

    pdfs: dict[str, list[str]] = {}
    for page in [*rate_pages, *pages]:
        for url, text in _pdf_links(page, domain):
            for hall in halls:
                if not hall.get("price_label") and names_hall(text, str(hall["name"])):
                    pdfs.setdefault(hall["option_id"], [])
                    # The first schedule listed under the hall's name is its regular rate.
                    if not pdfs[hall["option_id"]]:
                        pdfs[hall["option_id"]].append(url)
    gate = asyncio.Semaphore(PARALLEL)

    async def read(url: str) -> tuple[int, int] | None:
        async with gate:
            page = await safe_page(
                http, url, domain, budget, HOUSING_VENDORS, max_bytes=PDF_MAX_BYTES
            )
        if page is None or not page.data.startswith(b"%PDF-"):
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(read_rate_pdf, page.data), timeout=PDF_SECONDS
            )
        except TimeoutError:
            return None

    urls = list(dict.fromkeys(url for links in pdfs.values() for url in links))[:MAX_PDFS]
    ranges = dict(zip(urls, await asyncio.gather(*(read(url) for url in urls)), strict=True))
    for hall in halls:
        links = pdfs.get(hall["option_id"]) or []
        if not links:
            continue
        hall.setdefault("rates_url", links[0])
        read_ranges = [span for url in links if (span := ranges.get(url))]
        if not read_ranges:
            continue
        low = min(low for low, _ in read_ranges)
        high = max(high for _, high in read_ranges)
        label = (
            f"Room ${low // 100:,} a year"
            if low == high
            else f"Room ${low // 100:,}\u2013${high // 100:,} a year"
        )
        hall.update(
            yearly_cents=low,
            price_high_cents=high,
            price_label=label,
            price_source="rates PDF",
            rates_url=links[0],
        )
    return {
        "rates_url": rate_pages[0].url if rate_pages else "",
        "school_rates": school_rates,
    }
