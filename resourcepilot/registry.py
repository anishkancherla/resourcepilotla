"""ResourcePilot's curated seller registry.

This is the listing ResourcePilot maintains about sellers it may source from. It is separate from
each seller's own catalog: a seller controls its prices and inventory, while ResourcePilot
controls whether it trusts that seller.

``identity_verified`` is a demo listing attribute. In production it would be backed by evidence
such as Agentverse domain verification or a Fetch Business brand claim. This repository does not
query either service and does not claim to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

from resourcepilot.config import SELLER_BY_KEY


@dataclass(frozen=True)
class SellerListing:
    seller_id: str
    display_name: str
    categories: tuple[str, ...]
    location_label: str
    lat: float
    lon: float
    open_hour: int
    close_hour: int
    identity_verified: bool
    verification_basis: str

    @property
    def address(self) -> str:
        return os.getenv(SELLER_BY_KEY[self.seller_id].address_env, "").strip()

    @property
    def wallet(self) -> str:
        return os.getenv(SELLER_BY_KEY[self.seller_id].wallet_env, "").strip()


LISTINGS: Final[tuple[SellerListing, ...]] = (
    SellerListing(
        "westwood_print",
        "Westwood Print Lab",
        ("posters",),
        "Westwood Blvd",
        34.0617,
        -118.4469,
        8,
        20,
        True,
        "Demo listing: identity marked verified during onboarding",
    ),
    SellerListing(
        "gayley_copy",
        "Gayley Copy Center",
        ("posters",),
        "Gayley Ave",
        34.0633,
        -118.4495,
        7,
        22,
        True,
        "Demo listing: identity marked verified during onboarding",
    ),
    SellerListing(
        "flash_prints",
        "Flash Prints 4U",
        ("posters",),
        "Location not disclosed",
        34.0590,
        -118.4430,
        0,
        24,
        False,
        "Demo listing: no verified domain or business identity on file",
    ),
    SellerListing(
        "gear_share",
        "Campus Gear Share",
        ("camera", "microphone", "tripod"),
        "Media commons, ground floor",
        34.0700,
        -118.4430,
        9,
        21,
        True,
        "Demo listing: identity marked verified during onboarding",
    ),
    SellerListing(
        "studio_rooms",
        "Hilltop Studio Rooms",
        ("podcast_room", "study_room"),
        "Hilltop arts building",
        34.0725,
        -118.4420,
        8,
        23,
        True,
        "Demo listing: identity marked verified during onboarding",
    ),
)

LISTING_BY_ID: Final = {listing.seller_id: listing for listing in LISTINGS}
CATEGORIES: Final = {
    "camera": "Camera",
    "microphone": "Microphone",
    "tripod": "Tripod",
    "podcast_room": "Podcast studio",
    "study_room": "Study room",
    "posters": "Color posters",
}
# Categories priced per item rather than per hour.
PER_ITEM_CATEGORIES: Final = frozenset({"posters"})


def listings_for(categories: list[str]) -> list[SellerListing]:
    wanted = set(categories)
    return [listing for listing in LISTINGS if wanted.intersection(listing.categories)]


def listing_for_address(address: str) -> SellerListing | None:
    return next((listing for listing in LISTINGS if listing.address == address), None)
