from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import NOW, brief
from uagents_core.contrib.protocols.chat import MetadataContent, TextContent

from resourcepilot import cards
from resourcepilot.clock import later
from resourcepilot.intake import upcoming_days

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = {
    "bundle_id": "bnd_1",
    "label": "Lowest price",
    "lines": [
        {
            "category": "camera",
            "seller_id": "gear_share",
            "seller_name": "Campus Gear Share",
            "item_id": "mirrorless_kit",
            "name": "Mirrorless camera kit",
            "subtotal_cents": 900,
            "policy": "No training required.",
        }
    ],
    "subtotal_cents": 900,
    "fee_cents": 57,
    "total_cents": 957,
    "walk_minutes": 4,
    "seller_count": 1,
    "deposit_cents": 4000,
    "substitutions": ["Mirrorless camera kit instead of a DSLR"],
}
ROOM_LINE = {
    "category": "study_room",
    "seller_id": "campus_rooms",
    "seller_name": "Campus Rooms",
    "item_id": "libcal-1-2-3",
    "name": "HSSE 2101",
    "subtotal_cents": 0,
    "policy": "Book on your school's site with your own sign-in.",
    "handoff": True,
    "handoff_url": "https://calendar.lib.purdue.edu/space/3",
    "slot_label": "Thu Oct 22 \u00b7 2:00 PM\u20133:00 PM",
    "capacity": 6,
    "location_label": "HSSE Library",
    "source_label": "LibCal",
    "school_name": "Purdue University",
    "live_times": True,
}
ROOM_OPTION = {
    **BUNDLE,
    "bundle_id": "bnd_room",
    "label": "Earliest opening",
    "lines": [ROOM_LINE],
    "subtotal_cents": 0,
    "fee_cents": 0,
    "total_cents": 0,
    "walk_minutes": None,
    "deposit_cents": 0,
    "substitutions": [],
}
MIXED = {**BUNDLE, "lines": [*BUNDLE["lines"], ROOM_LINE]}
ROOM_BRIEF = {
    "service": "rooms",
    "timezone": "America/Indiana/Indianapolis",
    "window_start": "2026-10-22T18:00:00+00:00",
    "window_end": "2026-10-22T19:00:00+00:00",
}
BOOKINGS = [
    {
        "hold_id": "hold_1",
        "seller_id": "gear_share",
        "item_id": "mirrorless_kit",
        "confirmation_code": "GS-ABC123",
    }
]
ROOM_BOOKING = {
    "hold_id": "hold_2",
    "seller_id": "campus_rooms",
    "item_id": "libcal-1-2-3",
    "confirmation_code": "HANDOFF-ABC123",
}
LEDGER = [
    {"kind": "payout", "party": "gear_share", "amount": "9.00", "currency": "USD",
     "status": "simulated"},
    {"kind": "fee", "party": "resourcepilot", "amount": "0.57", "currency": "USD",
     "status": "retained"},
]  # fmt: skip
REJECTIONS = [
    {
        "seller_id": "flash_prints",
        "seller_name": "Flash Prints 4U",
        "stage": "identity",
        "item": "",
        "reason": "Identity not verified",
    }
]
HOUSING = {
    "school": {"school_name": "Purdue University", "city": "West Lafayette", "state": "IN"},
    "housing": {
        "facts": [("On-campus housing", "Yes"), ("Dorm beds", "16,056")],
        "facts_source": "IPEDS 2022 via Urban Institute Education Data API",
        "office_url": "https://housing.purdue.edu/",
        "portal_url": "https://housing.purdue.edu/apply",
        "portal_label": "Apply for housing",
    },
}
PAST_BOOKINGS = [
    {"data": {"brief": brief()}, "bundle": BUNDLE},
    {"data": {"brief": ROOM_BRIEF}, "bundle": ROOM_OPTION},
]


def _metadata(message):
    return next(item.metadata for item in message.content if isinstance(item, MetadataContent))


FACTORIES = [
    (cards.welcome_card, "custom"),
    (lambda: cards.rooms_form(upcoming_days(NOW)), "form"),
    (cards.housing_form, "form"),
    (lambda: cards.bundle_form(upcoming_days(NOW)), "form"),
    (lambda: cards.room_link_card("workflow-1", "MIT", "rooms"), "custom"),
    (lambda: cards.room_link_card("workflow-1", "MIT", "bundle"), "custom"),
    (lambda: cards.progress_card("workflow-1", "researcher", ("intake", "researcher")), "custom"),
    (lambda: cards.bundle_carousel("workflow-1", [BUNDLE, MIXED], 3), "carousel"),
    (lambda: cards.rooms_carousel("workflow-1", [ROOM_OPTION], "Purdue"), "carousel"),
    (lambda: cards.bundle_detail("workflow-1", brief(), BUNDLE, checkout=True), "detail"),
    (lambda: cards.bundle_detail("workflow-1", ROOM_BRIEF, ROOM_OPTION, checkout=True), "detail"),
    (lambda: cards.vetting_card("workflow-1", REJECTIONS), "detail"),
    (lambda: cards.holds_review_card("workflow-1", brief(), BUNDLE, later(600)), "review"),
    (lambda: cards.holds_review_card("workflow-1", ROOM_BRIEF, ROOM_OPTION, later(60)), "review"),
    (lambda: cards.payment_options_card("workflow-1", 957, "0.000095"), "custom"),
    (
        lambda: cards.receipt_card(brief(), BUNDLE, BOOKINGS, {"currency": "USD"}, LEDGER),
        "detail",
    ),
    (lambda: cards.receipt_card(ROOM_BRIEF, ROOM_OPTION, [ROOM_BOOKING], {}, []), "detail"),
    (lambda: cards.housing_card(HOUSING), "detail"),
    (lambda: cards.recovery_card("workflow-1", "A seller failed", 957, later(600)), "review"),
    (
        lambda: cards.refund_card({"currency": "USD"}, {**LEDGER[0], "kind": "refund"}),
        "detail",
    ),
    (lambda: cards.bookings_card([]), "detail"),
    (lambda: cards.bookings_card(PAST_BOOKINGS), "detail"),
    (lambda: cards.error_card("Something failed", service="rooms"), "custom"),
    (lambda: cards.notice_card("Saved", "Your link was checked.", badge="Done"), "custom"),
    (lambda: cards.status_card("Planner", "Builds bundles", "agent1qorch"), "custom"),
]


@pytest.mark.parametrize(("factory", "kind"), FACTORIES)
def test_every_card_uses_the_current_wire_format(factory, kind) -> None:
    actual_kind, payload = factory()
    assert actual_kind == kind
    message = cards.card_message("Readable fallback", actual_kind, payload)
    metadata = _metadata(message)
    assert metadata["card_protocol_version"] == "1"
    assert metadata["requires_card_interaction"] == "true"
    assert metadata["card_kind"] == kind
    assert all(isinstance(value, str) for value in metadata.values())
    assert len(metadata["card_payload"].encode()) <= 64 * 1024
    assert json.loads(metadata["card_payload"]) == payload
    assert "is_terminal" not in metadata
    assert any(isinstance(item, TextContent) for item in message.content)


def _labels(value):
    """Yield button, badge, field, and option labels; summary rows may wrap, so skip them."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "summary_rows":
                continue
            if key == "label" and isinstance(item, str):
                yield item
            yield from _labels(item)
    elif isinstance(value, list):
        for item in value:
            yield from _labels(item)


@pytest.mark.parametrize(("factory", "kind"), FACTORIES)
def test_labels_are_mobile_safe(factory, kind) -> None:
    _, payload = factory()
    assert all(len(label) <= 24 for label in _labels(payload))


@pytest.mark.parametrize(
    ("kind", "payload", "message"),
    [
        ("carousel", {"title": "x", "items": [], "style": "dense"}, "carousel card"),
        ("carousel", {"items": [{"id": "1", "title": "x", "price": "$1"}]}, "items"),
        ("detail", {"title": "x", "ctas": [{"label": "Go", "selection": {}, "icon": "x"}]}, "ctas"),
        ("review", {"title": "x", "approve_cta": {"label": "Go", "style": "red"}}, "CTA"),
        ("detail", {"title": "x", "summary_rows": [{"label": "Seats", "value": 4}]}, "strings"),
        (
            "custom",
            {"root": {"type": "button", "label": "Go", "action": {}, "color": "red"}},
            "button",
        ),
        ("custom", {"root": {"type": "table", "rows": []}}, "unsupported custom element"),
    ],
)
def test_undocumented_fields_are_rejected_before_sending(kind, payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        cards.card_message("x", kind, payload)


def test_services_menu_opens_each_service_and_bookings() -> None:
    _, menu = cards.welcome_card()
    selections = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "button":
                selections.append(node["action"]["selection"])
            for child in node.get("children", []):
                walk(child)

    walk(menu["root"])
    assert selections == [
        {"action": "open_service", "service": "rooms"},
        {"action": "open_service", "service": "housing"},
        {"action": "open_service", "service": "bundle"},
        {"action": "show_bookings"},
    ]
    assert "Demo shops" in json.dumps(menu)


@pytest.mark.parametrize("service", ["rooms", "housing", "bundle"])
def test_every_form_submits_its_own_service(service) -> None:
    _, form = cards.service_form(service)
    for field in form["fields"]:
        if field["kind"] == "select":
            assert field["options"]
    assert form["submit_cta"]["selection"] == {"action": "submit_form", "service": service}
    email = [field for field in form["fields"] if field["name"] == "email"]
    assert len(email) == 1
    assert email[0].get("required", False) is (service != "bundle")


def test_demo_bundle_is_labeled_as_demo_shops() -> None:
    _, form = cards.bundle_form()
    assert form["title"].startswith("Demo bundle")
    _, carousel = cards.bundle_carousel("workflow-1", [BUNDLE], 0)
    assert carousel["title"].startswith("Demo bundles")


def test_selections_carry_stable_workflow_and_bundle_ids() -> None:
    _, carousel = cards.bundle_carousel("workflow-1", [BUNDLE], 0)
    assert carousel["items"][0]["primary_cta"]["selection"] == {
        "action": "view_bundle",
        "workflow_id": "workflow-1",
        "bundle_id": "bnd_1",
    }
    _, detail = cards.bundle_detail("workflow-1", brief(), BUNDLE, checkout=True)
    hold = detail["ctas"][0]["selection"]
    assert hold == {"action": "place_holds", "workflow_id": "workflow-1", "bundle_id": "bnd_1"}
    _, rooms = cards.rooms_carousel("workflow-1", [ROOM_OPTION], "Purdue")
    assert rooms["items"][0]["primary_cta"]["selection"]["bundle_id"] == "bnd_room"
    assert rooms["items"][0]["badges"][-1]["label"] == "Live \u00b7 LibCal"
    _, link = cards.room_link_card("workflow-1", "MIT", "bundle")
    buttons = link["root"]["children"][-1]["children"]
    assert [button["action"]["selection"]["action"] for button in buttons] == [
        "submit_link",
        "skip_room",
        "start_over",
    ]


def test_direct_json_selection_is_parsed_before_prose() -> None:
    parsed = cards.parse_selection(
        '@resourcepilot {"selection":{"action":"view_bundle",'
        '"bundle_id":"bnd_1","workflow_id":"w"}}'
    )
    assert parsed["action"] == "view_bundle"
    assert parsed["bundle_id"] == "bnd_1"


def test_prose_fallback_only_allows_non_consequential_actions() -> None:
    assert cards.parse_selection("Can you show status?")["action"] == "show_status"
    assert cards.parse_selection("I need a study room") == {
        "action": "open_service",
        "service": "rooms",
    }
    assert cards.parse_selection("what about dorms")["service"] == "housing"
    assert cards.parse_selection("please request payment and place holds") == {}
    assert cards.parse_selection("submit form confirm free") == {}
    assert cards.parse_selection("DSLR Friday 3-6pm under $35") == {}


def test_card_rejects_oversize_payload() -> None:
    with pytest.raises(ValueError, match="64KB"):
        cards.card_message("too big", "custom", {"root": {"type": "text", "value": "x" * 70_000}})


def _nested(depth: int) -> dict:
    node = {"type": "text", "value": "leaf"}
    for _ in range(depth - 1):
        node = {"type": "group", "children": [node]}
    return {"root": node}


def test_custom_card_depth_limit() -> None:
    cards.card_message("ok", "custom", _nested(8))
    with pytest.raises(ValueError, match="8-level"):
        cards.card_message("too deep", "custom", _nested(9))
    with pytest.raises(ValueError, match="typed root"):
        cards.card_message("bad", "custom", {"root": {"children": []}})
    with pytest.raises(ValueError, match="unsupported"):
        cards.card_message("bad", "table", {})


def test_checkout_cards_carry_ids_and_honest_payment_labels() -> None:
    _, review = cards.holds_review_card("workflow-1", brief(), BUNDLE, later(600))
    assert review["approve_cta"]["selection"] == {
        "action": "choose_payment",
        "workflow_id": "workflow-1",
    }
    assert review["reject_cta"]["selection"]["action"] == "release_holds"
    assert "No money moves until you pay" in json.dumps(review)

    _, options = cards.payment_options_card("workflow-1", 957, None)
    methods = [
        child["action"]["selection"].get("payment_method")
        for child in options["root"]["children"]
        if child["type"] == "button"
    ]
    assert methods == ["stripe_mock", None]  # no FET button when FET is not configured
    assert "no money moves" in json.dumps(options)

    _, receipt = cards.receipt_card(brief(), BUNDLE, BOOKINGS, {"currency": "USD"}, LEDGER)
    text = json.dumps(receipt)
    assert "GS-ABC123" in text
    assert "Mock Stripe (no money moved)" in text


def test_free_room_cards_never_ask_for_payment() -> None:
    _, review = cards.holds_review_card("workflow-1", ROOM_BRIEF, ROOM_OPTION, later(60))
    assert review["approve_cta"]["selection"] == {
        "action": "confirm_free",
        "workflow_id": "workflow-1",
    }
    assert "payment" not in json.dumps(review).lower()
    _, receipt = cards.receipt_card(ROOM_BRIEF, ROOM_OPTION, [ROOM_BOOKING], {}, [])
    text = json.dumps(receipt)
    assert receipt["title"] == "Your booking link"
    assert ROOM_LINE["handoff_url"] in text
    assert "Not reserved by ResourcePilot" in text
    assert "HANDOFF-" not in text  # a handoff code is not a reservation, so it is not shown


def test_mixed_bundle_marks_the_free_room_outside_escrow() -> None:
    rows = cards.bundle_rows(brief(), MIXED)
    assert any(row["value"].startswith("Not covered by escrow") for row in rows)
    _, receipt = cards.receipt_card(
        brief(), MIXED, [*BOOKINGS, ROOM_BOOKING], {"currency": "USD"}, LEDGER
    )
    assert receipt["title"] == "Booked with 1 sellers"
    assert any(row["label"] == "Book it yourself" for row in receipt["summary_rows"])


def test_housing_card_shows_sources_and_official_links() -> None:
    _, card = cards.housing_card(HOUSING)
    text = json.dumps(card)
    assert card["title"] == "Housing at Purdue University \u00b7 West Lafayette, IN"
    assert "https://housing.purdue.edu/apply" in text
    assert "IPEDS 2022" in text
    assert "never applies or pays" in text


def test_every_orchestrator_reply_is_a_card() -> None:
    assert not hasattr(cards, "text_message")
    for path in ("agents/run.py", "sellers/run.py"):
        source = (ROOT / path).read_text()
        assert "TextContent(" not in source
        assert "ChatMessage(" not in source
