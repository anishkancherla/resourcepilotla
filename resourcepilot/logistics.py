"""Logistics analysis: can each trusted offer actually be ready, and how far is the walk?

Walking time is computed only for the demo shops, which have campus coordinates. A live room at
the student's own school has none, so it is labeled on campus and left out of the route.
"""

from __future__ import annotations

from datetime import timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any

from resourcepilot.clock import parse, utc_now
from resourcepilot.intake import ANCHORS

EARTH_RADIUS_M = 6_371_000
# Straight-line distance understates a real walking route; a fixed factor keeps the estimate
# honest without calling a routing service.
WALK_DETOUR_FACTOR = 1.3
WALK_METERS_PER_MINUTE = 80


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


def walk_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    meters = distance_m(lat1, lon1, lat2, lon2) * WALK_DETOUR_FACTOR
    return max(1, round(meters / WALK_METERS_PER_MINUTE))


def analysis_handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    brief = dict(payload["brief"])
    now = parse(str(context["now"])) if context.get("now") else utc_now()
    window_start = parse(str(brief["window_start"]))
    _, anchor_lat, anchor_lon = ANCHORS.get(str(brief.get("anchor")), ANCHORS["student_union"])
    rejections = list(payload.get("rejections", []))
    analyzed = []
    for raw in payload.get("offers", []):
        offer = dict(raw)
        lead = timedelta(hours=float(offer.get("ready_in_hours", 0)))
        if now + lead > window_start:
            rejections.append(
                {
                    "seller_id": offer["seller_id"],
                    "seller_name": offer["seller_name"],
                    "stage": "logistics",
                    "item": offer["name"],
                    "reason": f"Not ready in time (needs {offer['ready_in_hours']}h lead time)",
                }
            )
            continue
        if offer.get("lat") is None or offer.get("lon") is None:
            offer["walk_minutes"] = None
        else:
            offer["walk_minutes"] = walk_minutes(
                anchor_lat, anchor_lon, float(offer["lat"]), float(offer["lon"])
            )
        analyzed.append(offer)
    covered = {offer["category"] for offer in analyzed}
    if not covered and brief.get("service") != "wellness":
        raise ValueError("No trusted offer can be ready before the booking starts")
    return {
        **payload,
        "offers": analyzed,
        "rejections": rejections,
        "uncovered": sorted(set(brief["items"]) - covered),
    }
