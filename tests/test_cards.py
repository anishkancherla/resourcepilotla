from __future__ import annotations

import json

import pytest
from helpers import NOW, brief
from uagents_core.contrib.protocols.chat import MetadataContent, TextContent

from resourcepilot import cards
from resourcepilot.intake import upcoming_days

BUNDLE = {
    "bundle_id": "bnd_1",
    "label": "Lowest price",
    "lines": [
        {
            "category": "camera",
            "seller_id": "gear_share",
            "seller_name": "Campus Gear Share",
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
REJECTIONS = [
    {
        "seller_id": "flash_prints",
        "seller_name": "Flash Prints 4U",
        "stage": "identity",
        "item": "",
        "reason": "Identity not verified",
    }
]


def _metadata(message):
    return next(item.metadata for item in message.content if isinstance(item, MetadataContent))


FACTORIES = [
    (cards.welcome_card, "custom"),
    (lambda: cards.request_form(upcoming_days(NOW)), "form"),
    (lambda: cards.progress_card("workflow-1", "discovery"), "custom"),
    (lambda: cards.bundle_carousel("workflow-1", [BUNDLE], 3), "carousel"),
    (lambda: cards.bundle_detail("workflow-1", brief(), BUNDLE, checkout=True), "detail"),
    (lambda: cards.vetting_card("workflow-1", REJECTIONS), "detail"),
    (lambda: cards.bookings_card([]), "detail"),
    (lambda: cards.error_card("Something failed"), "custom"),
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


def test_form_selects_have_options_and_a_submit_action() -> None:
    _, form = cards.request_form(upcoming_days(NOW))
    for field in form["fields"]:
        if field["kind"] == "select":
            assert field["options"]
    assert form["submit_cta"]["selection"] == {"action": "submit_form"}


def test_direct_json_selection_is_parsed_before_prose() -> None:
    parsed = cards.parse_selection(
        '@resourcepilot {"selection":{"action":"view_bundle",'
        '"bundle_id":"bnd_1","workflow_id":"w"}}'
    )
    assert parsed["action"] == "view_bundle"
    assert parsed["bundle_id"] == "bnd_1"


def test_prose_fallback_only_allows_non_consequential_actions() -> None:
    assert cards.parse_selection("Can you show status?")["action"] == "show_status"
    assert cards.parse_selection("let me open form")["action"] == "open_form"
    assert cards.parse_selection("please request payment and place holds") == {}


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
