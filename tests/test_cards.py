from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from uagents_core.contrib.protocols.chat import MetadataContent, TextContent

from resourcepilot import cards
from resourcepilot.intake import describe_window, upcoming_days

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
LINE = {
    "item_id": "libcal-1-2-3",
    "name": "HSSE 2101",
    "capacity": 6,
    "slot_start": "2026-10-22T18:00:00+00:00",
    "slot_end": "2026-10-22T19:00:00+00:00",
    "slot_label": "Thu Oct 22 · 2:00 PM\u20133:00 PM",
    "location_label": "HSSE Library",
    "handoff_url": "https://calendar.lib.purdue.edu/space/3",
    "live_times": True,
    "source_label": "LibCal",
    "school_name": "Purdue University",
    "available": True,
    "unavailable_reason": "",
    "policy": "Free. ResourcePilot re-checks this room but cannot reserve it.",
}
PAGE_LINE = {
    **LINE,
    "item_id": "booking-page",
    "name": "Book on Purdue University's LibCal",
    "capacity": 0,
    "slot_label": "",
    "location_label": "Live times unavailable right now",
    "handoff_url": "https://calendar.lib.purdue.edu/reserve",
    "live_times": False,
}
OPTION = {"option_id": "opt_0123456789", "label": "Earliest opening", "lines": [LINE]}
PAGE_OPTION = {"option_id": "opt_abcdef0123", "label": "Booking page", "lines": [PAGE_LINE]}
ROOM_BRIEF = {
    "service": "rooms",
    "school_domain": "purdue.edu",
    "day": "2026-10-22",
    "window": "afternoon",
    "duration_minutes": 60,
    "hours": 1.0,
    "group_size": 2,
    "timezone": "America/Indiana/Indianapolis",
    "window_start": "2026-10-22T16:00:00+00:00",
    "window_end": "2026-10-22T21:00:00+00:00",
}
SCHOOL = {"school_name": "Purdue University", "city": "West Lafayette", "state": "IN"}
HOUSING = {
    "school": SCHOOL,
    "housing": {
        "facts": [("On-campus housing", "Yes"), ("Dorm beds", "16,056")],
        "facts_source": "IPEDS 2022 via Urban Institute Education Data API",
        "office_url": "https://housing.purdue.edu/",
        "portal_url": "https://housing.purdue.edu/apply",
        "portal_label": "Apply for housing",
    },
}
BOOKED = {
    "workflow_id": "workflow-1",
    "status": "booked",
    "option": OPTION,
    "data": {"brief": ROOM_BRIEF, "school": SCHOOL},
}
PAYMENT_WORDS = re.compile(
    r"\b(?:bundles?|pay(?:ment|s)?|paid|escrow|stripe|fet|demo\s+shops?|deposits?|holds?)\b", re.I
)


def _metadata(message):
    return next(item.metadata for item in message.content if isinstance(item, MetadataContent))


FACTORIES = [
    (cards.welcome_card, "custom"),
    (lambda: cards.rooms_form(upcoming_days(NOW)), "form"),
    (cards.housing_form, "form"),
    (lambda: cards.service_form("rooms"), "form"),
    (lambda: cards.service_form("housing"), "form"),
    (lambda: cards.room_link_card("workflow-1", "MIT"), "custom"),
    (lambda: cards.progress_card("workflow-1", "researcher", ("intake", "researcher")), "custom"),
    (lambda: cards.progress_card("workflow-1", "recheck"), "custom"),
    (lambda: cards.rooms_carousel("workflow-1", [OPTION, PAGE_OPTION], "Purdue"), "carousel"),
    (lambda: cards.room_detail("workflow-1", OPTION), "detail"),
    (lambda: cards.room_detail("workflow-1", PAGE_OPTION), "detail"),
    (lambda: cards.room_link_receipt(OPTION), "detail"),
    (lambda: cards.room_link_receipt(PAGE_OPTION), "detail"),
    (lambda: cards.housing_card(HOUSING), "detail"),
    (lambda: cards.housing_card({}), "detail"),
    (lambda: cards.bookings_card([]), "detail"),
    (lambda: cards.bookings_card([BOOKED]), "detail"),
    (lambda: cards.error_card("Something failed", service="rooms"), "custom"),
    (lambda: cards.error_card("Something failed"), "custom"),
    (lambda: cards.notice_card("Saved", "Your link was checked.", badge="Done"), "custom"),
    (lambda: cards.status_card("Planner", "Ranks open rooms", "agent1qorch"), "custom"),
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
    assert len(metadata["card_payload"].encode()) <= cards.CARD_LIMIT_BYTES
    assert json.loads(metadata["card_payload"]) == payload
    assert "is_terminal" not in metadata
    assert any(isinstance(item, TextContent) for item in message.content)
    if kind == "custom":
        assert cards._custom_tree_depth(payload["root"]) <= cards.CUSTOM_CARD_MAX_DEPTH


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


def _selections(value):
    """Yield every selection a card can send back, from CTAs, buttons, and carousel items."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "selection" and isinstance(item, dict):
                yield item
            else:
                yield from _selections(item)
    elif isinstance(value, list):
        for item in value:
            yield from _selections(item)


@pytest.mark.parametrize(("factory", "kind"), FACTORIES)
def test_labels_are_mobile_safe(factory, kind) -> None:
    _, payload = factory()
    assert all(len(label) <= 24 for label in _labels(payload))


@pytest.mark.parametrize(("factory", "kind"), FACTORIES)
def test_every_selection_uses_a_known_action(factory, kind) -> None:
    _, payload = factory()
    actions = [selection["action"] for selection in _selections(payload)]
    assert actions
    assert set(actions) <= set(cards.KNOWN_ACTIONS)


def test_terminal_flag_is_a_string() -> None:
    message = cards.card_message("Done", *cards.room_link_receipt(OPTION), terminal=True)
    assert _metadata(message)["is_terminal"] == "true"


@pytest.mark.parametrize(
    ("kind", "payload", "message"),
    [
        ("carousel", {"title": "x", "items": [], "style": "dense"}, "carousel card"),
        ("carousel", {"items": [{"id": "1", "title": "x", "price": "$1"}]}, "items"),
        ("carousel", {"items": [{"id": 1, "title": "x"}]}, "string id"),
        ("detail", {"title": "x", "ctas": [{"label": "Go", "selection": {}, "icon": "x"}]}, "ctas"),
        ("review", {"title": "x", "approve_cta": {"label": "Go", "style": "red"}}, "CTA"),
        ("detail", {"title": "x", "summary_rows": [{"label": "Seats", "value": 4}]}, "strings"),
        ("form", {"title": "x", "fields": [{"name": "a", "kind": "text", "hint": "x"}]}, "fields"),
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


def test_services_menu_offers_three_services_and_bookings() -> None:
    _, menu = cards.welcome_card()
    assert list(_selections(menu)) == [
        {"action": "open_service", "service": "rooms"},
        {"action": "open_service", "service": "wellness"},
        {"action": "open_service", "service": "housing"},
        {"action": "show_bookings"},
    ]
    text = json.dumps(menu).lower()
    assert "study rooms" in text
    assert "campus housing" in text
    assert "my bookings" in text
    assert not PAYMENT_WORDS.findall(text)
    assert cards.CRISIS_TEXT in json.dumps(menu, ensure_ascii=False)


@pytest.mark.parametrize("service", ["rooms", "wellness", "housing"])
def test_every_form_submits_its_own_service(service) -> None:
    _, form = cards.service_form(service)
    for field in form["fields"]:
        if field["kind"] == "select":
            assert field["options"]
    assert form["submit_cta"]["selection"] == {"action": "submit_form", "service": service}
    email = [field for field in form["fields"] if field["name"] == "email"]
    assert len(email) == 1
    assert email[0]["required"] is True


def test_rooms_form_offers_days_windows_durations_and_group_size() -> None:
    _, form = cards.rooms_form(upcoming_days(NOW))
    fields = {field["name"]: field for field in form["fields"]}
    assert list(fields) == ["email", "day", "window", "duration", "group_size"]
    assert fields["day"]["options"][0] == {"value": "2026-10-21", "label": "Today"}
    assert [option["value"] for option in fields["window"]["options"]] == [
        "morning",
        "afternoon",
        "evening",
        "any",
    ]
    assert [option["value"] for option in fields["duration"]["options"]] == [
        "30",
        "60",
        "90",
        "120",
    ]
    assert len(fields["group_size"]["options"]) == 12


def test_room_link_card_checks_a_pasted_link_without_skipping() -> None:
    _, link = cards.room_link_card("workflow-1", "MIT")
    root = link["root"]
    assert root["title"] == "Where does MIT book study rooms?"
    inputs = [child for child in root["children"] if child["type"] == "input"]
    assert [field["name"] for field in inputs] == ["booking_link"]
    assert list(_selections(link)) == [
        {"action": "submit_link", "workflow_id": "workflow-1"},
        {"action": "start_over"},
    ]
    assert "skip" not in json.dumps(link).lower()


def test_rooms_carousel_items_carry_stable_option_ids() -> None:
    _, carousel = cards.rooms_carousel("workflow-1", [OPTION, PAGE_OPTION], "Purdue")
    assert carousel["title"] == "Open study rooms at Purdue"
    first, second = carousel["items"]
    assert first["id"] == "opt_0123456789"
    assert first["primary_cta"]["selection"] == {
        "action": "view_room",
        "workflow_id": "workflow-1",
        "option_id": "opt_0123456789",
    }
    assert [badge["label"] for badge in first["badges"]] == ["Earliest", "Live · LibCal"]
    assert "seats 6" in first["secondary_text"]
    assert second["id"] == "opt_abcdef0123"
    assert second["primary_cta"]["selection"]["option_id"] == "opt_abcdef0123"
    assert [badge["label"] for badge in second["badges"]] == ["Link only"]
    assert "seats not listed" in second["secondary_text"]


def test_room_detail_rechecks_by_option_and_shows_the_link() -> None:
    _, detail = cards.room_detail("workflow-1", OPTION)
    assert detail["title"] == "HSSE 2101"
    assert [cta["selection"] for cta in detail["ctas"]] == [
        {"action": "recheck_room", "workflow_id": "workflow-1", "option_id": "opt_0123456789"},
        {"action": "show_results", "workflow_id": "workflow-1"},
    ]
    assert detail["ctas"][0]["primary"] is True
    rows = {row["label"]: row["value"] for row in detail["summary_rows"]}
    assert rows["Book here"] == LINE["handoff_url"]
    assert rows["When"] == LINE["slot_label"]
    assert rows["Seats"] == "6"
    assert rows["Price"] == "Free"
    assert rows["Times"] == "Live from LibCal"
    _, page = cards.room_detail("workflow-1", PAGE_OPTION)
    page_rows = {row["label"]: row["value"] for row in page["summary_rows"]}
    assert "When" not in page_rows
    assert "Seats" not in page_rows
    assert page_rows["Times"].startswith("Not shown live")


def test_room_link_receipt_is_a_link_not_a_reservation() -> None:
    _, receipt = cards.room_link_receipt(OPTION)
    assert receipt["title"] == "Your booking link"
    rows = {row["label"]: row["value"] for row in receipt["summary_rows"]}
    assert rows["Room"] == "HSSE 2101"
    assert rows["When"] == LINE["slot_label"]
    assert rows["Book here"] == LINE["handoff_url"]
    assert rows["Next step"] == "Not reserved by ResourcePilot: book it with your own sign-in"
    assert rows["Re-checked"].startswith("Open when ResourcePilot re-checked it live")
    assert [cta["selection"] for cta in receipt["ctas"]] == [
        {"action": "open_service", "service": "rooms"},
        {"action": "start_over"},
    ]
    assert [cta["label"] for cta in receipt["ctas"]] == ["Find another room", "Menu"]
    assert "sub_options" not in receipt  # terminal-safe: no input beyond its CTA buttons
    assert not PAYMENT_WORDS.findall(json.dumps(receipt))
    _, page = cards.room_link_receipt(PAGE_OPTION)
    page_rows = {row["label"]: row["value"] for row in page["summary_rows"]}
    assert page_rows["Re-checked"].startswith("Not checked live")


def test_housing_card_shows_sources_and_official_links() -> None:
    _, card = cards.housing_card(HOUSING)
    text = json.dumps(card)
    assert card["title"] == "Housing at Purdue University · West Lafayette, IN"
    assert "https://housing.purdue.edu/apply" in text
    assert "IPEDS 2022" in text
    assert "never applies or pays" in text
    assert [cta["selection"] for cta in card["ctas"]] == [
        {"action": "open_service", "service": "rooms"},
        {"action": "start_over"},
    ]
    _, empty = cards.housing_card({})
    assert empty["title"] == "Housing at your school"
    assert {"label": "Housing office", "value": "Not found on the school's site"} in (
        empty["summary_rows"]
    )


def test_bookings_card_lists_booked_rooms_with_their_links() -> None:
    _, empty = cards.bookings_card([])
    assert empty["summary_rows"] == [{"label": "Bookings", "value": "No bookings yet"}]
    _, card = cards.bookings_card([BOOKED, {"status": "booked", "data": {}}])
    assert card["summary_rows"] == [
        {
            "label": LINE["slot_label"],
            "value": f"HSSE 2101 · Purdue University: {LINE['handoff_url']}",
        }
    ]
    assert card["ctas"][0]["selection"] == {"action": "start_over"}
    _, page = cards.bookings_card([{**BOOKED, "option": PAGE_OPTION}])
    assert page["summary_rows"][0]["label"] == describe_window(ROOM_BRIEF)


def test_progress_card_marks_finished_and_current_stages() -> None:
    chain = ("intake", "researcher", "campus_rooms", "verification", "planner")
    assert set(chain) <= set(cards.STAGE_LABELS)
    assert {"recheck", "link"} <= set(cards.STAGE_LABELS)
    assert all(len(label) <= 24 for label in cards.STAGE_LABELS.values())
    _, card = cards.progress_card("workflow-123456789", "verification", chain)
    children = card["root"]["children"]
    assert children[0] == {"type": "badge", "label": "Verifying every result", "variant": "info"}
    steps = children[1]["value"].splitlines()
    assert steps == [
        "✓ Reading your form",
        "✓ Researching your school",
        "✓ Reading open rooms",
        "• Verifying every result",
    ]
    assert children[2]["value"] == "Workflow workflow"
    assert list(_selections(card)) == [
        {"action": "show_status", "workflow_id": "workflow-123456789"}
    ]
    _, recheck = cards.progress_card("workflow-1", "recheck")
    assert recheck["root"]["children"][0]["label"] == "Re-checking your room"
    assert len(recheck["root"]["children"]) == 3  # no step list outside a chain


def test_known_actions_cover_the_room_flow_only() -> None:
    assert set(cards.KNOWN_ACTIONS) == {
        "start_over",
        "open_service",
        "submit_form",
        "show_bookings",
        "show_status",
        "view_room",
        "recheck_room",
        "show_results",
        "submit_link",
        "open_orchestrator",
    }
    assert set(cards.PROSE_ACTIONS) <= set(cards.KNOWN_ACTIONS)
    assert not {"submit_form", "recheck_room", "submit_link"} & set(cards.PROSE_ACTIONS)


def test_direct_json_selection_is_parsed_before_prose() -> None:
    parsed = cards.parse_selection(
        '@resourcepilot {"selection":{"action":"recheck_room",'
        '"option_id":"opt_0123456789","workflow_id":"w"}}'
    )
    assert parsed["action"] == "recheck_room"
    assert parsed["option_id"] == "opt_0123456789"
    assert parsed["workflow_id"] == "w"
    flat = cards.parse_selection(
        '{"action":"submit_link","workflow_id":"w","booking_link":"https://x.edu"}'
    )
    assert flat == {"action": "submit_link", "workflow_id": "w", "booking_link": "https://x.edu"}
    view = cards.parse_selection('{"action":"view_room","workflow_id":"w","option_id":"opt_1"}')
    assert view["option_id"] == "opt_1"


def test_prose_fallback_only_allows_non_consequential_actions() -> None:
    assert cards.parse_selection("Can you show status?")["action"] == "show_status"
    assert cards.parse_selection("show results again") == {"action": "show_results"}
    assert cards.parse_selection("please show bookings") == {"action": "show_bookings"}
    assert cards.parse_selection("start over") == {"action": "start_over"}
    assert cards.parse_selection("I need a study room") == {
        "action": "open_service",
        "service": "rooms",
    }
    assert cards.parse_selection("what about dorms")["service"] == "housing"
    assert cards.parse_selection("recheck room and submit link") == {}
    assert cards.parse_selection("submit form https://purdue.edu") == {}
    assert cards.parse_selection("view room opt_1") == {}
    assert cards.parse_selection("project bundle with a DSLR") == {}
    assert cards.parse_selection("") == {}


def test_cards_module_has_no_bundle_or_payment_surface() -> None:
    removed = (
        "bundle_form",
        "bundle_carousel",
        "bundle_detail",
        "bundle_rows",
        "holds_review_card",
        "payment_options_card",
        "receipt_card",
        "recovery_card",
        "refund_card",
        "vetting_card",
        "example_bundle_selection",
        "demo_control_buttons",
    )
    assert not [name for name in removed if hasattr(cards, name)]
    assert set(cards.SERVICE_TITLES) == {"rooms", "wellness", "housing"}
    source = (ROOT / "resourcepilot" / "cards.py").read_text()
    for module in ("pricing", "registry", "bundles", "logistics", "escrow", "payment"):
        assert f"resourcepilot.{module}" not in source
        assert f"from resourcepilot import {module}" not in source


def test_every_orchestrator_reply_is_a_card() -> None:
    assert not hasattr(cards, "text_message")
    source = (ROOT / "agents" / "run.py").read_text()
    assert "TextContent(" not in source
    assert "ChatMessage(" not in source


def test_wellness_card_always_carries_the_crisis_line() -> None:
    kind, payload = cards.wellness_card("wf-1", {"school": {"school_name": "Test U"}})
    message = cards.card_message("x", kind, payload)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert message.content[1].metadata["card_kind"] == "custom"
    assert cards.CRISIS_TEXT in encoded
    assert "No public room-booking system found for Test U" in encoded
    assert "No public events calendar found for Test U" in encoded
    _, form = cards.wellness_form()
    assert cards.CRISIS_TEXT in form["title"]
    assert {field["name"] for field in form["fields"]} == {"email", "day", "window", "duration"}


def test_wellness_card_rooms_use_stable_option_ids() -> None:
    data = {"school": {"school_name": "Test U"}, "options": [OPTION]}
    kind, payload = cards.wellness_card("wf-1", data)
    cards.card_message("x", kind, payload)
    assert {"action": "view_room", "workflow_id": "wf-1", "option_id": OPTION["option_id"]} in list(
        _selections(payload)
    )
    _, detail = cards.room_detail("wf-1", OPTION, wellness=True)
    assert {"label": "Need help now?", "value": cards.CRISIS_TEXT} in detail["summary_rows"]
    _, receipt = cards.room_link_receipt(OPTION, service="wellness")
    assert receipt["ctas"][0]["selection"] == {"action": "open_service", "service": "wellness"}


def test_wellness_prose_only_opens_the_form() -> None:
    for text in ("any wellness events?", "I need a meditation space", "flu shot near me"):
        assert cards.parse_selection(text) == {"action": "open_service", "service": "wellness"}
