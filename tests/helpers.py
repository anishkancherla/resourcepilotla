"""Build real seller quotes and briefs for specialist tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from resourcepilot.cards import example_bundle_selection
from resourcepilot.clock import CAMPUS_TZ
from resourcepilot.intake import bundle_brief
from resourcepilot.protocols import Quote, QuoteRequest
from resourcepilot.registry import LISTINGS
from resourcepilot.store import MemoryStore
from sellers.inventory import build_offers

NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CAMPUS_TZ)
# The demo-bundle form as a student submits it: DSLR, podcast room, 20 posters, Fri 3-6 PM, $35.
EXAMPLE = {
    key: value
    for key, value in example_bundle_selection(NOW).items()
    if key not in {"action", "service"}
}


def brief(fields: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    return {**bundle_brief({**EXAMPLE, **(fields or {})}, now=NOW), **overrides}


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
