"""Build real seller quotes and briefs for specialist tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.intake import build_brief, parse_request_text
from resourcepilot.protocols import Quote, QuoteRequest
from resourcepilot.registry import LISTINGS
from resourcepilot.store import MemoryStore
from sellers.inventory import build_offers

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CAMPUS_TZ)
EXAMPLE = "I need a DSLR, a podcast room, and 20 color posters Friday from 3-6 PM, under $35"


def brief(text: str = EXAMPLE, **overrides: Any) -> dict[str, Any]:
    return {**build_brief(parse_request_text(text, now=NOW), now=NOW), **overrides}


async def seller_quotes(
    selected: dict[str, Any], addresses: dict[str, str], store: MemoryStore | None = None
) -> list[dict[str, Any]]:
    """Ask every listed seller for a quote exactly as Discovery would record it."""
    store = store or MemoryStore()
    request = QuoteRequest(
        round_id="round-1",
        workflow_id="wf-1",
        categories=list(selected["items"]),
        quantities=dict(selected["items"]),
        window_start=selected["window_start"],
        window_end=selected["window_end"],
    )
    quotes = []
    for listing in LISTINGS:
        if not set(listing.categories) & set(selected["items"]):
            continue
        offers = await build_offers(store, listing, request, NOW.isoformat())
        quote = Quote(
            round_id="round-1", seller_id=listing.seller_id, hold_ttl_seconds=600, offers=offers
        )
        quotes.append({**quote.dict(), "sender": addresses[listing.seller_id]})
    return quotes
