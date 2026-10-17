"""Bundle planning: combine one trusted offer per requested resource into budget-safe plans."""

from __future__ import annotations

from functools import partial
from hashlib import sha256
from itertools import product
from typing import Any

from resourcepilot.intake import ANCHORS, PRIORITIES
from resourcepilot.logistics import walk_minutes
from resourcepilot.pricing import totals, usd
from resourcepilot.registry import CATEGORIES

OPTIONS_PER_CATEGORY = 3
MAX_BUNDLES = 3


def _route_minutes(anchor: str, lines: list[dict[str, Any]]) -> int:
    """Nearest-neighbor walk from the starting point through each distinct seller."""
    _, lat, lon = ANCHORS[anchor]
    stops = {line["seller_id"]: (float(line["lat"]), float(line["lon"])) for line in lines}
    total = 0
    while stops:
        seller_id, (next_lat, next_lon) = min(
            stops.items(), key=lambda item: walk_minutes(lat, lon, *item[1])
        )
        total += walk_minutes(lat, lon, next_lat, next_lon)
        lat, lon = next_lat, next_lon
        del stops[seller_id]
    return total


def _bundle_id(lines: list[dict[str, Any]]) -> str:
    key = "|".join(sorted(f"{line['seller_id']}:{line['item_id']}" for line in lines))
    return f"bnd_{sha256(key.encode()).hexdigest()[:10]}"


def _line(offer: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "category",
        "seller_id",
        "seller_name",
        "item_id",
        "name",
        "quantity",
        "hours",
        "unit",
        "unit_price_cents",
        "subtotal_cents",
        "deposit_cents",
        "requires_training",
        "policy",
        "rating",
        "walk_minutes",
        "location_label",
        "lat",
        "lon",
        "exact_match",
        "new_seller",
    )
    return {field: offer.get(field) for field in fields}


def _build(brief: dict[str, Any], combo: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    lines = [_line(offer) for offer in combo]
    subtotal = sum(int(line["subtotal_cents"]) for line in lines)
    substitutions = [
        f"{line['name']} instead of a {brief['camera_type'].upper()}"
        for line in lines
        if not line["exact_match"]
    ]
    return {
        "bundle_id": _bundle_id(lines),
        "lines": lines,
        **totals(subtotal),
        "walk_minutes": _route_minutes(str(brief["anchor"]), lines),
        "avg_rating": round(sum(float(line["rating"]) for line in lines) / len(lines), 2),
        "seller_count": len({line["seller_id"] for line in lines}),
        "deposit_cents": sum(int(line["deposit_cents"] or 0) for line in lines),
        "substitutions": substitutions,
    }


def _sort_key(priority: str, bundle: dict[str, Any]) -> tuple[float, ...]:
    inexact = float(len(bundle["substitutions"]))
    if priority == "closest":
        return (inexact, bundle["walk_minutes"], bundle["total_cents"])
    if priority == "best_rated":
        return (inexact, -bundle["avg_rating"], bundle["total_cents"])
    return (inexact, bundle["total_cents"], bundle["walk_minutes"])


def _options(offers: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    matching = [offer for offer in offers if offer["category"] == category]
    by_price = sorted(matching, key=lambda o: (not o["exact_match"], o["subtotal_cents"]))
    by_walk = sorted(matching, key=lambda o: (not o["exact_match"], o["walk_minutes"]))
    by_rating = sorted(matching, key=lambda o: (not o["exact_match"], -float(o["rating"])))
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for ranking in (by_price, by_walk, by_rating):
        for offer in ranking[:OPTIONS_PER_CATEGORY]:
            chosen.setdefault((offer["seller_id"], offer["item_id"]), offer)
    return list(chosen.values())


def plan_bundles(
    brief: dict[str, Any], offers: list[dict[str, Any]], exclude_sellers: set[str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (up to three distinct within-budget bundles, uncovered categories)."""
    usable = [offer for offer in offers if offer["seller_id"] not in (exclude_sellers or set())]
    categories = [category for category in brief["items"] if category in CATEGORIES]
    options = {category: _options(usable, category) for category in categories}
    uncovered = [category for category in categories if not options[category]]
    covered = [category for category in categories if options[category]]
    if not covered:
        raise ValueError("No remaining trusted seller can cover this request")
    candidates = [_build(brief, combo) for combo in product(*(options[c] for c in covered))]
    affordable = [bundle for bundle in candidates if bundle["total_cents"] <= brief["budget_cents"]]
    if not affordable:
        cheapest = min(candidates, key=lambda bundle: bundle["total_cents"])
        raise ValueError(
            f"The cheapest trusted bundle costs {usd(cheapest['total_cents'])} with fees, "
            f"over your {usd(int(brief['budget_cents']))} budget"
        )
    priorities = [str(brief["priority"])] + [p for p in PRIORITIES if p != brief["priority"]]
    bundles: list[dict[str, Any]] = []
    for priority in priorities:
        best = min(affordable, key=partial(_sort_key, priority))
        if all(best["bundle_id"] != existing["bundle_id"] for existing in bundles):
            bundles.append({**best, "label": PRIORITIES[priority], "priority": priority})
        if len(bundles) == MAX_BUNDLES:
            break
    return bundles, uncovered


def planner_handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    brief = dict(payload["brief"])
    excluded = {str(seller) for seller in payload.get("exclude_sellers", [])}
    bundles, uncovered = plan_bundles(brief, list(payload.get("offers", [])), excluded)
    return {
        **payload,
        "bundles": bundles,
        "recommended_id": bundles[0]["bundle_id"],
        "uncovered": sorted(set(payload.get("uncovered", [])) | set(uncovered)),
        "exclude_sellers": sorted(excluded),
    }
