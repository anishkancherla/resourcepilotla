from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from resourcepilot.clock import CAMPUS_TZ, parse
from resourcepilot.escrow import note_for, rail_label
from resourcepilot.intake import ANCHORS, PRIORITIES, describe_window
from resourcepilot.pricing import usd
from resourcepilot.registry import CATEGORIES

CARD_LIMIT_BYTES = 64 * 1024
CUSTOM_CARD_MAX_DEPTH = 8
CARD_KINDS = {"carousel", "detail", "form", "review", "custom"}
ASI_ONE_URL = "https://asi1.ai/"

# Bounded allowlist for planner-mediated prose. Consequential actions still require the
# stable workflow and bundle IDs that only a card selection carries.
KNOWN_ACTIONS = (
    "open_orchestrator",
    "open_form",
    "show_status",
    "show_vetting",
    "show_bookings",
    "simulate_outage",
    "restore_service",
    "start_over",
)


def _typed_elements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        element
        for element in value
        if isinstance(element, dict) and isinstance(element.get("type"), str)
    ]


def _custom_tree_depth(element: dict[str, Any]) -> int:
    """Count nested custom elements; the root element is depth one."""
    children = _typed_elements(element.get("children"))
    items = element.get("items")
    if element.get("type") == "list" and isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                children.extend(_typed_elements(item.get("children")))
    return 1 + max((_custom_tree_depth(child) for child in children), default=0)


def _validate_custom_payload(payload: dict[str, Any]) -> None:
    root = payload.get("root")
    if not isinstance(root, dict) or not isinstance(root.get("type"), str):
        raise ValueError("custom card payload requires a typed root element")
    if _custom_tree_depth(root) > CUSTOM_CARD_MAX_DEPTH:
        raise ValueError(
            f"custom card tree exceeds the {CUSTOM_CARD_MAX_DEPTH}-level Agentverse limit"
        )


def card_message(
    narration: str, kind: str, payload: dict[str, Any], *, terminal: bool = False
) -> ChatMessage:
    if kind not in CARD_KINDS:
        raise ValueError(f"unsupported card kind: {kind}")
    if kind == "custom":
        _validate_custom_payload(payload)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > CARD_LIMIT_BYTES:
        raise ValueError("card_payload exceeds the 64KB Agentverse limit")
    metadata = {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": kind,
        "card_payload": encoded,
        "preferred_drawer_width_px": "560",
    }
    if terminal:
        metadata["is_terminal"] = "true"
    return ChatMessage(
        timestamp=datetime.now(UTC),
        msg_id=uuid4(),
        content=[
            TextContent(type="text", text=narration),
            MetadataContent(type="metadata", metadata=metadata),
        ],
    )


def text_message(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(UTC),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


def parse_selection(text: str, known_actions: tuple[str, ...] = KNOWN_ACTIONS) -> dict[str, Any]:
    """Parse direct JSON selections first, then a bounded allowlist of prose actions."""
    raw = re.sub(r"^(?:@\S+\s+)+", "", (text or "").strip())
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        selection = parsed.get("selection")
        return {**parsed, **selection} if isinstance(selection, dict) else parsed
    lowered = raw.lower()
    for action in known_actions:
        if action.replace("_", " ") in lowered or action in lowered:
            return {"action": action}
    return {}


def _button(label: str, selection: dict[str, Any], *, primary: bool = False) -> dict[str, Any]:
    return {
        "type": "button",
        "label": label,
        "primary": primary,
        "action": {"selection": selection},
    }


def status_card(
    title: str,
    responsibility: str,
    orchestrator_address: str,
    *,
    badges: list[tuple[str, str]] | None = None,
    facts: list[str] | None = None,
    extra_buttons: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Profile chat card shared by specialists and sellers; it points to the Orchestrator."""
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": label, "variant": variant}
        for label, variant in (badges or [("Online", "success")])
    ]
    children.append({"type": "text", "value": responsibility, "style": "body"})
    children.extend({"type": "text", "value": fact, "style": "muted"} for fact in facts or [])
    children.append(
        {
            "type": "button",
            "label": "Use ResourcePilot",
            "primary": True,
            "action": {
                "selection": {
                    "action": "open_orchestrator",
                    "agent_address": orchestrator_address,
                },
                "redirect": ASI_ONE_URL,
            },
        }
    )
    children.extend(extra_buttons or [])
    return "custom", {"root": {"type": "section", "title": title, "children": children}}


def demo_control_buttons(fault_mode: str) -> list[dict[str, Any]]:
    if fault_mode == "none":
        return [_button("Simulate outage", {"action": "simulate_outage"})]
    return [_button("Restore service", {"action": "restore_service"})]


EXAMPLE_REQUEST = (
    "I need a DSLR, a podcast room, and 20 color posters Friday from 3-6 PM, under $35"
)


def _hour_label(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour - 1) % 12 + 1} {suffix}"


def welcome_card() -> tuple[str, dict[str, Any]]:
    return "custom", {
        "root": {
            "type": "section",
            "title": "ResourcePilot",
            "children": [
                {
                    "type": "text",
                    "value": (
                        "Tell me what you need in one sentence. I get live quotes from "
                        "independent seller agents, vet them, and hold everything before "
                        "you pay. Sellers are paid only after every booking commits."
                    ),
                    "style": "body",
                },
                _button(
                    "Try the example",
                    {"action": "submit_text", "text": EXAMPLE_REQUEST},
                    primary=True,
                ),
                _button("Use the form", {"action": "open_form"}),
                _button("My bookings", {"action": "show_bookings"}),
            ],
        }
    }


def request_form(days: list[tuple[str, str]], note: str = "") -> tuple[str, dict[str, Any]]:
    def select(
        name: str, label: str, options: list[tuple[str, str]], **extra: Any
    ) -> dict[str, Any]:
        return {
            "name": name,
            "kind": "select",
            "label": label,
            "options": [{"value": value, "label": text} for value, text in options],
            **extra,
        }

    return "form", {
        "title": "What do you need?",
        "subtitle": note or "Pick resources, a time, and a budget",
        "fields": [
            select(
                "camera",
                "Camera",
                [
                    ("none", "No camera"),
                    ("any", "Any camera"),
                    ("dslr", "DSLR"),
                    ("mirrorless", "Mirrorless"),
                ],
            ),
            select(
                "room",
                "Room",
                [
                    ("none", "No room"),
                    ("podcast_room", "Podcast studio"),
                    ("study_room", "Study room"),
                ],
            ),
            {"name": "posters", "kind": "number", "label": "Color posters"},
            {"name": "microphone", "kind": "checkbox", "label": "USB microphone"},
            {"name": "tripod", "kind": "checkbox", "label": "Tripod"},
            select("day", "Day", days, required=True),
            select(
                "start_hour",
                "Start time",
                [(str(hour), _hour_label(hour)) for hour in range(8, 22)],
                required=True,
            ),
            select(
                "hours",
                "Duration",
                [(str(h), f"{h} hour{'s' if h > 1 else ''}") for h in range(1, 7)],
                required=True,
            ),
            {"name": "budget", "kind": "number", "label": "Budget (USD)", "required": True},
            select("priority", "Priority", list(PRIORITIES.items())),
            select("anchor", "Starting point", [(k, v[0]) for k, v in ANCHORS.items()]),
            {"name": "has_training", "kind": "checkbox", "label": "I have gear training"},
        ],
        "submit_cta": {"label": "Find sellers", "selection": {"action": "submit_form"}},
    }


STAGE_LABELS = {
    "intake": "Reading your request",
    "discovery": "Collecting seller quotes",
    "verification": "Vetting sellers",
    "analysis": "Checking logistics",
    "planner": "Building bundles",
    "holds": "Placing holds",
    "payment": "Waiting for payment",
    "booking": "Committing bookings",
}


def progress_card(workflow_id: str, stage: str) -> tuple[str, dict[str, Any]]:
    return "custom", {
        "root": {
            "type": "section",
            "title": "Agents at work",
            "children": [
                {
                    "type": "badge",
                    "label": STAGE_LABELS.get(stage, stage.title()),
                    "variant": "info",
                },
                {"type": "text", "value": f"Workflow {workflow_id[:8]}", "style": "muted"},
                _button("Refresh status", {"action": "show_status", "workflow_id": workflow_id}),
            ],
        }
    }


def _sellers(bundle: dict[str, Any]) -> str:
    return ", ".join(dict.fromkeys(line["seller_name"] for line in bundle["lines"]))


def bundle_carousel(
    workflow_id: str, bundles: list[dict[str, Any]], rejected: int
) -> tuple[str, dict[str, Any]]:
    items = []
    for bundle in bundles:
        badges = [{"label": f"{bundle['seller_count']} vetted sellers", "variant": "success"}]
        if bundle["substitutions"]:
            badges.append({"label": "Substitution", "variant": "warning"})
        items.append(
            {
                "id": bundle["bundle_id"],
                "title": bundle["label"],
                "subtitle": _sellers(bundle),
                "secondary_text": (
                    f"{usd(bundle['total_cents'])} · {bundle['walk_minutes']} min walk"
                ),
                "badges": badges,
                "primary_cta": {
                    "label": "View bundle",
                    "selection": {
                        "action": "view_bundle",
                        "workflow_id": workflow_id,
                        "bundle_id": bundle["bundle_id"],
                    },
                },
            }
        )
    return "carousel", {
        "title": "Bundles from vetted sellers",
        "subtitle": f"{rejected} offers were filtered out by vetting and logistics",
        "style": "slide",
        "items": items,
    }


def bundle_rows(brief: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, str]]:
    rows = [
        {
            "label": f"{CATEGORIES[line['category']]} · {line['seller_name']}",
            "value": f"{line['name']}: {usd(int(line['subtotal_cents']))}",
        }
        for line in bundle["lines"]
    ]
    rows.extend(
        [
            {"label": "When", "value": describe_window(brief)},
            {"label": "Booking fee", "value": usd(bundle["fee_cents"])},
            {"label": "Total", "value": usd(bundle["total_cents"])},
            {"label": "Walk", "value": f"About {bundle['walk_minutes']} minutes in total"},
        ]
    )
    if bundle["deposit_cents"]:
        rows.append(
            {
                "label": "Deposits",
                "value": f"{usd(bundle['deposit_cents'])} refundable, paid to sellers at pickup",
            }
        )
    for note in bundle["substitutions"]:
        rows.append({"label": "Substitution", "value": note})
    return rows


def bundle_detail(
    workflow_id: str, brief: dict[str, Any], bundle: dict[str, Any], *, checkout: bool
) -> tuple[str, dict[str, Any]]:
    ids = {"workflow_id": workflow_id, "bundle_id": bundle["bundle_id"]}
    ctas = []
    if checkout:
        ctas.append(
            {
                "label": "Hold everything",
                "primary": True,
                "selection": {"action": "place_holds", **ids},
            }
        )
    ctas.extend(
        [
            {"label": "Why not others?", "selection": {"action": "show_vetting", **ids}},
            {"label": "Start over", "selection": {"action": "start_over"}},
        ]
    )
    rows = bundle_rows(brief, bundle)
    rows.extend(
        {"label": f"Policy · {line['seller_name']}", "value": str(line["policy"])}
        for line in bundle["lines"]
    )
    return "detail", {"title": bundle["label"], "summary_rows": rows, "ctas": ctas}


def _local_time(value: str) -> str:
    return f"{parse(value).astimezone(CAMPUS_TZ):%-I:%M %p}"


def holds_review_card(
    workflow_id: str, brief: dict[str, Any], bundle: dict[str, Any], holds_expire_at: str
) -> tuple[str, dict[str, Any]]:
    rows = bundle_rows(brief, bundle)
    rows.extend(
        [
            {
                "label": "Holds",
                "value": (
                    f"All {bundle['seller_count']} sellers are holding your items until "
                    f"{_local_time(holds_expire_at)}"
                ),
            },
            {
                "label": "Payment",
                "value": (
                    "No money moves until you pay. Your payment sits in escrow, and sellers "
                    "are paid only after every booking commits."
                ),
            },
        ]
    )
    return "review", {
        "title": "Everything is on hold",
        "summary_rows": rows,
        "approve_cta": {
            "label": "Choose payment",
            "primary": True,
            "selection": {"action": "choose_payment", "workflow_id": workflow_id},
        },
        "reject_cta": {
            "label": "Release holds",
            "selection": {"action": "release_holds", "workflow_id": workflow_id},
        },
    }


def payment_options_card(
    workflow_id: str, total_cents: int, fet_amount: str | None
) -> tuple[str, dict[str, Any]]:
    children: list[dict[str, Any]] = [
        {"type": "heading", "value": f"Total {usd(total_cents)}", "level": 2},
        {
            "type": "text",
            "value": "Mock Stripe is a simulation: no Stripe API call and no money moves.",
            "style": "body",
        },
        _button(
            "Mock Stripe",
            {"action": "pay", "workflow_id": workflow_id, "payment_method": "stripe_mock"},
            primary=True,
        ),
    ]
    if fet_amount:
        children.extend(
            [
                {
                    "type": "text",
                    "value": (
                        f"Or pay {fet_amount} faucet-issued test FET on Fetch stable-testnet. "
                        "The Orchestrator verifies the transaction on chain."
                    ),
                    "style": "body",
                },
                _button(
                    "Pay test FET",
                    {"action": "pay", "workflow_id": workflow_id, "payment_method": "fet_direct"},
                ),
            ]
        )
    children.append(
        _button("Release holds", {"action": "release_holds", "workflow_id": workflow_id})
    )
    return "custom", {
        "root": {"type": "section", "title": "Pay ResourcePilot escrow", "children": children}
    }


def receipt_card(
    brief: dict[str, Any],
    bundle: dict[str, Any],
    bookings: list[dict[str, Any]],
    payment: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    names = {line["seller_id"]: line["seller_name"] for line in bundle["lines"]}
    lines = {line["seller_id"] + ":" + line["item_id"]: line for line in bundle["lines"]}
    rows = []
    for booking in bookings:
        line = lines.get(f"{booking['seller_id']}:{booking['item_id']}", {})
        rows.append(
            {
                "label": f"{CATEGORIES.get(line.get('category', ''), 'Item')} · "
                f"{names.get(booking['seller_id'], booking['seller_id'])}",
                "value": f"{line.get('name', booking['item_id'])}: {booking['confirmation_code']}",
            }
        )
    rows.extend(
        [
            {"label": "When", "value": describe_window(brief)},
            {
                "label": "Paid",
                "value": f"{usd(bundle['total_cents'])} via {rail_label(payment['currency'])}",
            },
        ]
    )
    for entry in ledger:
        label = (
            "Booking fee · ResourcePilot"
            if entry["kind"] == "fee"
            else f"{entry['kind'].title()} · {names.get(entry['party'], 'Buyer')}"
        )
        rows.append(
            {
                "label": label,
                "value": f"{entry['amount']} {entry['currency']}: {note_for(entry)}",
            }
        )
    if bundle["deposit_cents"]:
        rows.append(
            {"label": "Deposits", "value": "Refundable deposits are paid to sellers at pickup"}
        )
    return "detail", {
        "title": f"Booked with {bundle['seller_count']} sellers",
        "summary_rows": rows,
        "ctas": [
            {"label": "My bookings", "primary": True, "selection": {"action": "show_bookings"}},
            {"label": "New request", "selection": {"action": "start_over"}},
        ],
    }


def recovery_card(
    workflow_id: str, error: str, total_cents: int, refund_due_at: str
) -> tuple[str, dict[str, Any]]:
    return "review", {
        "title": "Booking did not complete",
        "summary_rows": [
            {"label": "What happened", "value": error},
            {"label": "Bookings", "value": "Every seller's booking was cancelled"},
            {
                "label": "Your payment",
                "value": f"{usd(total_cents)} is safe in escrow. No seller was paid.",
            },
            {
                "label": "Auto refund",
                "value": f"Refunded automatically at {_local_time(refund_due_at)} if you wait",
            },
            {"label": "Retry", "value": "Retrying uses the same payment; you never pay twice"},
        ],
        "approve_cta": {
            "label": "Retry booking",
            "primary": True,
            "selection": {"action": "retry_booking", "workflow_id": workflow_id},
        },
        "reject_cta": {
            "label": "Refund me",
            "selection": {"action": "refund", "workflow_id": workflow_id},
        },
    }


def refund_card(payment: dict[str, Any], entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "detail", {
        "title": "Refunded",
        "summary_rows": [
            {"label": "Amount", "value": f"{entry['amount']} {entry['currency']}"},
            {"label": "Method", "value": rail_label(str(payment["currency"]))},
            {"label": "Status", "value": note_for(entry)},
            {"label": "Sellers", "value": "No seller was paid for this request"},
        ],
        "ctas": [{"label": "New request", "primary": True, "selection": {"action": "start_over"}}],
    }


VETTING_STAGES = {
    "identity": "Identity",
    "protocol": "Protocol",
    "track_record": "Track record",
    "policy": "Policy",
    "logistics": "Logistics",
    "availability": "No answer",
}


def vetting_card(workflow_id: str, rejections: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    rows = [
        {
            "label": f"{VETTING_STAGES.get(item['stage'], item['stage'])} · {item['seller_name']}",
            "value": f"{item['item'] + ': ' if item.get('item') else ''}{item['reason']}",
        }
        for item in rejections[:20]
    ] or [{"label": "Vetting", "value": "Every quote passed vetting"}]
    return "detail", {
        "title": "What vetting filtered out",
        "summary_rows": rows,
        "ctas": [
            {
                "label": "Back to bundles",
                "primary": True,
                "selection": {"action": "show_status", "workflow_id": workflow_id},
            }
        ],
    }


def bookings_card(bookings: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    rows = [
        {
            "label": describe_window(booking["data"]["brief"]),
            "value": f"{_sellers(booking['bundle'])} · {usd(booking['bundle']['total_cents'])}",
        }
        for booking in bookings
        if booking.get("bundle") and booking.get("data", {}).get("brief")
    ] or [{"label": "Bookings", "value": "No bookings yet"}]
    return "detail", {
        "title": "Your recent bookings",
        "summary_rows": rows,
        "ctas": [{"label": "New request", "primary": True, "selection": {"action": "start_over"}}],
    }


def error_card(message: str, *, show_form: bool = True) -> tuple[str, dict[str, Any]]:
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": "Needs attention", "variant": "warning"},
        {"type": "text", "value": message, "style": "body"},
    ]
    if show_form:
        children.append(_button("Use the form", {"action": "open_form"}, primary=True))
    children.append(_button("Start over", {"action": "start_over"}))
    return "custom", {"root": {"type": "section", "title": "ResourcePilot", "children": children}}
