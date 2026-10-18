"""ASI:One cards for every Orchestrator reply, plus profile status cards for every agent.

Every payload uses only the fields documented on the Innovation Lab interactive-cards page.
ASI:One silently falls back to plain text when a payload has an unexpected shape, so
``card_message`` rejects anything else before it is sent.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from resourcepilot.clock import CAMPUS_TZ, parse, utc_now
from resourcepilot.escrow import note_for, rail_label
from resourcepilot.intake import (
    ANCHORS,
    PRIORITIES,
    ROOM_DURATIONS,
    brief_zone,
    describe_window,
    upcoming_days,
)
from resourcepilot.pricing import usd
from resourcepilot.registry import CATEGORIES
from resourcepilot.research import WINDOWS

CARD_LIMIT_BYTES = 64 * 1024
CUSTOM_CARD_MAX_DEPTH = 8
CARD_KINDS = {"carousel", "detail", "form", "review", "custom"}
ASI_ONE_URL = "https://asi1.ai/"

PREDEFINED_FIELDS = {
    "carousel": {"title", "subtitle", "items"},
    "detail": {"title", "hero_image", "summary_rows", "sub_options", "ctas"},
    "form": {"title", "fields", "submit_cta"},
    "review": {"title", "summary_rows", "approve_cta", "reject_cta"},
}
NESTED_FIELDS = {
    "items": {"id", "image", "title", "subtitle", "badges", "secondary_text", "primary_cta"},
    "badges": {"label", "variant"},
    "summary_rows": {"label", "value"},
    "ctas": {"label", "selection", "primary"},
    "fields": {"name", "kind", "label", "required", "options"},
    "options": {"value", "label"},
    "sub_options": {"name", "kind", "label", "choices"},
    "choices": {"value", "label", "secondary_text"},
}
CTA_FIELDS = {"label", "selection", "primary"}
CUSTOM_FIELDS = {
    "section": {"type", "title", "subtitle", "children"},
    "group": {"type", "direction", "gap", "children"},
    "divider": {"type"},
    "text": {"type", "value", "style"},
    "heading": {"type", "value", "level"},
    "image": {"type", "src", "alt", "aspect_ratio"},
    "badge": {"type", "label", "variant"},
    "button": {"type", "label", "primary", "action"},
    "input": {"type", "name", "kind", "label", "required", "options", "placeholder"},
    "list": {"type", "items"},
    "choice_grid": {"type", "name", "choices", "multi"},
}
LIST_ITEM_FIELDS = {"children", "action"}

# Bounded allowlist for planner-mediated prose. Consequential actions still require the
# stable workflow and bundle IDs that only a card selection carries.
KNOWN_ACTIONS = (
    "open_orchestrator",
    "show_status",
    "show_vetting",
    "show_bookings",
    "simulate_outage",
    "restore_service",
    "start_over",
)
# Prose that only opens a service's form, never submits one.
SERVICE_WORDS = (
    ("rooms", re.compile(r"\bstudy\s*rooms?\b|\blibrary\s+rooms?\b|\bbook\s+a\s+room\b", re.I)),
    ("housing", re.compile(r"\bhousing\b|\bdorms?\b|\bresidence\s+halls?\b", re.I)),
    ("bundle", re.compile(r"\bproject\s+bundle\b|\bdemo\s+bundle\b|\bdemo\s+shops?\b", re.I)),
)
SERVICE_TITLES = {"rooms": "Study rooms", "housing": "Campus housing", "bundle": "Project bundle"}


# ---------------------------------------------------------------------------
# Wire format and validation
# ---------------------------------------------------------------------------


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


def _check_custom_fields(element: dict[str, Any]) -> None:
    kind = element.get("type")
    allowed = CUSTOM_FIELDS.get(str(kind))
    if allowed is None:
        raise ValueError(f"unsupported custom element: {kind}")
    extra = set(element) - allowed
    if extra:
        raise ValueError(f"undocumented field(s) on {kind}: {sorted(extra)}")
    for child in _typed_elements(element.get("children")):
        _check_custom_fields(child)
    if kind == "list":
        for item in element.get("items") or []:
            if set(item) - LIST_ITEM_FIELDS:
                raise ValueError("undocumented field(s) on list item")
            for child in _typed_elements(item.get("children")):
                _check_custom_fields(child)


def _validate_custom_payload(payload: dict[str, Any]) -> None:
    root = payload.get("root")
    if not isinstance(root, dict) or not isinstance(root.get("type"), str):
        raise ValueError("custom card payload requires a typed root element")
    if _custom_tree_depth(root) > CUSTOM_CARD_MAX_DEPTH:
        raise ValueError(
            f"custom card tree exceeds the {CUSTOM_CARD_MAX_DEPTH}-level Agentverse limit"
        )
    _check_custom_fields(root)


def _check_cta(value: Any) -> None:
    if isinstance(value, dict) and set(value) - CTA_FIELDS:
        raise ValueError(f"undocumented CTA field(s): {sorted(set(value) - CTA_FIELDS)}")


def _check_nested(key: str, value: Any) -> None:
    if isinstance(value, list):
        allowed = NESTED_FIELDS.get(key)
        for entry in value:
            if allowed is not None and isinstance(entry, dict):
                extra = set(entry) - allowed
                if extra:
                    raise ValueError(f"undocumented field(s) in {key}: {sorted(extra)}")
                for child_key, child in entry.items():
                    if child_key == "primary_cta":
                        _check_cta(child)
                    elif child_key != "selection":
                        _check_nested(child_key, child)
    if key in {"approve_cta", "reject_cta", "submit_cta"}:
        _check_cta(value)


def _validate_predefined(kind: str, payload: dict[str, Any]) -> None:
    extra = set(payload) - PREDEFINED_FIELDS[kind]
    if extra:
        raise ValueError(f"undocumented field(s) on {kind} card: {sorted(extra)}")
    for key, value in payload.items():
        _check_nested(key, value)
    for row in payload.get("summary_rows", []):
        if not isinstance(row.get("value"), str) or not isinstance(row.get("label"), str):
            raise ValueError("summary row labels and values must be strings")
    for item in payload.get("items", []):
        for name in ("id", "title", "subtitle", "secondary_text"):
            if name in item and not isinstance(item[name], str):
                raise ValueError(f"carousel items need string {name} fields")


def card_message(
    narration: str, kind: str, payload: dict[str, Any], *, terminal: bool = False
) -> ChatMessage:
    if kind not in CARD_KINDS:
        raise ValueError(f"unsupported card kind: {kind}")
    if kind == "custom":
        _validate_custom_payload(payload)
    else:
        _validate_predefined(kind, payload)
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
    for service, pattern in SERVICE_WORDS:
        if pattern.search(lowered):
            return {"action": "open_service", "service": service}
    return {}


def _button(label: str, selection: dict[str, Any], *, primary: bool = False) -> dict[str, Any]:
    return {
        "type": "button",
        "label": label,
        "primary": primary,
        "action": {"selection": selection},
    }


def _buttons(actions: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    return {
        "type": "group",
        "direction": "row",
        "children": [
            _button(label, selection, primary=index == 0)
            for index, (label, selection) in enumerate(actions)
        ],
    }


def _zone_time(value: str, zone: ZoneInfo = CAMPUS_TZ) -> str:
    return f"{parse(value).astimezone(zone):%-I:%M %p}"


# ---------------------------------------------------------------------------
# Profile status, menu, and notices
# ---------------------------------------------------------------------------


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


SERVICES_MENU = (
    (
        "rooms",
        "Study rooms",
        "Live · any school",
        "success",
        "Real open library study rooms at your school. Free: we re-check the room, then you "
        "book it on your school's site with your own sign-in.",
    ),
    (
        "housing",
        "Campus housing",
        "Live · IPEDS",
        "success",
        "Dorm beds, typical room and board, and meal plans, plus your housing office and "
        "application portal. You apply on your school's site.",
    ),
    (
        "bundle",
        "Project bundle",
        "Demo shops",
        "warning",
        "Camera, podcast studio, and posters from five demo shops near UCLA, held all at once "
        "before you pay. Mock Stripe or test FET only. Add a free study room at your school.",
    ),
)


def welcome_card() -> tuple[str, dict[str, Any]]:
    """The campus services menu, in the spirit of Fetch's Welcome Week agents."""
    children: list[dict[str, Any]] = [
        {
            "type": "text",
            "value": (
                "Campus help for your first weeks, at any US school. Pick a service; "
                "everything runs on buttons and short forms."
            ),
            "style": "body",
        }
    ]
    for service, title, badge, variant, body in SERVICES_MENU:
        children.append(
            {
                "type": "group",
                "direction": "column",
                "children": [
                    {"type": "divider"},
                    {"type": "heading", "value": title, "level": 3},
                    {"type": "badge", "label": badge, "variant": variant},
                    {"type": "text", "value": body, "style": "muted"},
                    _button(
                        f"Open {title.lower()}",
                        {"action": "open_service", "service": service},
                        primary=service == "rooms",
                    ),
                ],
            }
        )
    children.append({"type": "divider"})
    children.append(_button("My bookings", {"action": "show_bookings"}))
    return "custom", {
        "root": {"type": "section", "title": "ResourcePilot campus services", "children": children}
    }


def notice_card(
    title: str,
    body: str,
    *,
    badge: str | None = None,
    variant: str = "info",
    buttons: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[str, dict[str, Any]]:
    children: list[dict[str, Any]] = []
    if badge:
        children.append({"type": "badge", "label": badge, "variant": variant})
    children.append({"type": "text", "value": body, "style": "body"})
    children.append(_buttons(buttons or [("Start over", {"action": "start_over"})]))
    return "custom", {"root": {"type": "section", "title": title, "children": children}}


def error_card(message: str, *, service: str | None = None) -> tuple[str, dict[str, Any]]:
    actions: list[tuple[str, dict[str, Any]]] = []
    if service in SERVICE_TITLES:
        actions.append(("Open the form", {"action": "open_service", "service": service}))
    actions.append(("Start over", {"action": "start_over"}))
    return notice_card(
        "ResourcePilot", message, badge="Needs attention", variant="warning", buttons=actions
    )


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


def _select(
    name: str, label: str, options: list[tuple[str, str]], *, required: bool = False
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "name": name,
        "kind": "select",
        "label": label,
        "options": [{"value": value, "label": text} for value, text in options],
    }
    if required:
        field["required"] = True
    return field


def _hour_label(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour - 1) % 12 + 1} {suffix}"


def _email_field(label: str = "School email", *, required: bool = True) -> dict[str, Any]:
    field: dict[str, Any] = {"name": "email", "kind": "text", "label": label}
    if required:
        field["required"] = True
    return field


def _day_options(days: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(value, "Today" if index == 0 else text) for index, (value, text) in enumerate(days)]


def rooms_form(days: list[tuple[str, str]] | None = None) -> tuple[str, dict[str, Any]]:
    return "form", {
        "title": "Find a study room at your school",
        "fields": [
            _email_field(),
            _select("day", "Day", _day_options(days or upcoming_days()), required=True),
            _select(
                "window",
                "Time of day",
                [(key, value[0]) for key, value in WINDOWS.items()],
                required=True,
            ),
            _select("duration", "How long", list(ROOM_DURATIONS.items()), required=True),
            _select("group_size", "People", [(str(n), str(n)) for n in range(1, 13)]),
        ],
        "submit_cta": {
            "label": "Find open rooms",
            "selection": {"action": "submit_form", "service": "rooms"},
        },
    }


def housing_form() -> tuple[str, dict[str, Any]]:
    return "form", {
        "title": "Housing at your school",
        "fields": [_email_field()],
        "submit_cta": {
            "label": "Look up housing",
            "selection": {"action": "submit_form", "service": "housing"},
        },
    }


def bundle_form(days: list[tuple[str, str]] | None = None) -> tuple[str, dict[str, Any]]:
    return "form", {
        "title": "Demo bundle · UCLA-area demo shops",
        "fields": [
            _select(
                "camera",
                "Camera",
                [
                    ("none", "No camera"),
                    ("any", "Any camera"),
                    ("dslr", "DSLR"),
                    ("mirrorless", "Mirrorless"),
                ],
            ),
            _select(
                "room",
                "Room",
                [
                    ("none", "No room"),
                    ("podcast_room", "Podcast studio"),
                    ("study_room", "Free room at my school"),
                ],
            ),
            {"name": "posters", "kind": "number", "label": "Color posters"},
            {"name": "microphone", "kind": "checkbox", "label": "USB microphone"},
            {"name": "tripod", "kind": "checkbox", "label": "Tripod"},
            _select("day", "Day", _day_options(days or upcoming_days()), required=True),
            _select(
                "start_hour",
                "Start time",
                [(str(hour), _hour_label(hour)) for hour in range(8, 22)],
                required=True,
            ),
            _select(
                "hours",
                "Duration",
                [(str(h), f"{h} hour{'s' if h > 1 else ''}") for h in range(1, 7)],
                required=True,
            ),
            {"name": "budget", "kind": "number", "label": "Budget (USD)", "required": True},
            _select("priority", "Priority", list(PRIORITIES.items())),
            _select("anchor", "Starting point", [(k, v[0]) for k, v in ANCHORS.items()]),
            {"name": "has_training", "kind": "checkbox", "label": "I have gear training"},
            _email_field("School email for a room", required=False),
        ],
        "submit_cta": {
            "label": "Find sellers",
            "selection": {"action": "submit_form", "service": "bundle"},
        },
    }


def service_form(service: str) -> tuple[str, dict[str, Any]]:
    if service == "rooms":
        return rooms_form()
    if service == "housing":
        return housing_form()
    return bundle_form()


def room_link_card(workflow_id: str, school: str, service: str) -> tuple[str, dict[str, Any]]:
    """Ask for the booking link the Researcher could not find, instead of guessing one."""
    actions: list[tuple[str, dict[str, Any]]] = [
        ("Check this link", {"action": "submit_link", "workflow_id": workflow_id})
    ]
    if service == "bundle":
        actions.append(("Skip the room", {"action": "skip_room", "workflow_id": workflow_id}))
    actions.append(("Start over", {"action": "start_over"}))
    return "custom", {
        "root": {
            "type": "section",
            "title": f"Where does {school} book study rooms?",
            "children": [
                {"type": "badge", "label": "Link needed", "variant": "warning"},
                {
                    "type": "text",
                    "value": (
                        "The Campus Researcher checked your library's site and did not find a "
                        "room-booking system it recognizes. Paste the link you use to book a "
                        "room and it will check that link instead of guessing."
                    ),
                    "style": "body",
                },
                {
                    "type": "input",
                    "name": "booking_link",
                    "kind": "text",
                    "label": "Room booking link",
                    "required": True,
                    "placeholder": "https://",
                },
                _buttons(actions),
            ],
        }
    }


EXAMPLE_FIELDS = {
    "camera": "dslr",
    "room": "podcast_room",
    "posters": "20",
    "start_hour": "15",
    "hours": "3",
    "budget": "35",
    "priority": "cheapest",
    "anchor": "student_union",
}


def example_bundle_selection(now: datetime | None = None) -> dict[str, Any]:
    """A complete demo-bundle form submission for next Friday, used by demos and tests."""
    today = (now or utc_now()).astimezone(CAMPUS_TZ).date()
    friday = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    return {
        "action": "submit_form",
        "service": "bundle",
        **EXAMPLE_FIELDS,
        "day": friday.isoformat(),
    }


# ---------------------------------------------------------------------------
# Progress, options, and details
# ---------------------------------------------------------------------------


STAGE_LABELS = {
    "intake": "Reading your form",
    "researcher": "Researching your school",
    "link": "Waiting for a link",
    "discovery": "Collecting quotes",
    "verification": "Vetting and re-checking",
    "analysis": "Checking logistics",
    "planner": "Ranking options",
    "holds": "Re-checking and holding",
    "payment": "Waiting for payment",
    "booking": "Confirming",
}


def progress_card(
    workflow_id: str, stage: str, chain: tuple[str, ...] = ()
) -> tuple[str, dict[str, Any]]:
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": STAGE_LABELS.get(stage, stage.title()), "variant": "info"}
    ]
    if stage in chain:
        done = chain[: chain.index(stage)]
        steps = [f"✓ {STAGE_LABELS[role]}" for role in done]
        steps.append(f"• {STAGE_LABELS[stage]}")
        children.append({"type": "text", "value": "\n".join(steps), "style": "body"})
    children.append({"type": "text", "value": f"Workflow {workflow_id[:8]}", "style": "muted"})
    children.append(
        _button("Refresh status", {"action": "show_status", "workflow_id": workflow_id})
    )
    return "custom", {"root": {"type": "section", "title": "Agents at work", "children": children}}


def _sellers(bundle: dict[str, Any]) -> str:
    return ", ".join(dict.fromkeys(line["seller_name"] for line in bundle["lines"]))


def _walk_text(bundle: dict[str, Any]) -> str:
    walk = bundle.get("walk_minutes")
    return f"{walk} min walk" if walk else "no walking route"


def bundle_carousel(
    workflow_id: str, bundles: list[dict[str, Any]], rejected: int
) -> tuple[str, dict[str, Any]]:
    items = []
    for bundle in bundles:
        priced = sum(1 for line in bundle["lines"] if not line.get("handoff"))
        badges = [{"label": f"{bundle['seller_count']} vetted sellers", "variant": "success"}]
        if priced < len(bundle["lines"]):
            badges.append({"label": "+ free study room", "variant": "info"})
        if bundle["substitutions"]:
            badges.append({"label": "Substitution", "variant": "warning"})
        items.append(
            {
                "id": bundle["bundle_id"],
                "title": bundle["label"],
                "subtitle": _sellers(bundle),
                "secondary_text": f"{usd(bundle['total_cents'])} · {_walk_text(bundle)}",
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
        "title": "Demo bundles from vetted sellers",
        "subtitle": f"{rejected} offers were filtered out by vetting and logistics",
        "items": items,
    }


def rooms_carousel(
    workflow_id: str, options: list[dict[str, Any]], school: str
) -> tuple[str, dict[str, Any]]:
    items = []
    for index, option in enumerate(options):
        line = option["lines"][0]
        seats = f"seats {line['capacity']}" if line.get("capacity") else "seats not listed"
        live = bool(line.get("live_times"))
        badges = [
            {
                "label": f"Live · {line.get('source_label') or 'LibCal'}" if live else "Link only",
                "variant": "success" if live else "warning",
            }
        ]
        if index == 0:
            badges.insert(0, {"label": "Earliest", "variant": "info"})
        items.append(
            {
                "id": option["bundle_id"],
                "title": str(line["name"]),
                "subtitle": str(line.get("slot_label") or line.get("location_label") or ""),
                "secondary_text": f"Free · {seats} · {line.get('location_label') or ''}",
                "badges": badges,
                "primary_cta": {
                    "label": "View room",
                    "selection": {
                        "action": "view_bundle",
                        "workflow_id": workflow_id,
                        "bundle_id": option["bundle_id"],
                    },
                },
            }
        )
    return "carousel", {
        "title": f"Open study rooms at {school}",
        "subtitle": "Re-checked live before you get the booking link. Always free.",
        "items": items,
    }


def _line_row(line: dict[str, Any]) -> dict[str, str]:
    if line.get("handoff"):
        when = f" · {line['slot_label']}" if line.get("slot_label") else ""
        return {
            "label": f"Study room · {line.get('school_name') or 'your school'}",
            "value": f"{line['name']}{when}: free, you book it",
        }
    return {
        "label": f"{CATEGORIES[line['category']]} · {line['seller_name']}",
        "value": f"{line['name']}: {usd(int(line['subtotal_cents']))}",
    }


def bundle_rows(brief: dict[str, Any], bundle: dict[str, Any]) -> list[dict[str, str]]:
    if brief.get("service") == "rooms":
        return room_rows(bundle["lines"][0])
    rows = [_line_row(line) for line in bundle["lines"]]
    rows.extend(
        [
            {"label": "When", "value": describe_window(brief)},
            {"label": "Booking fee", "value": usd(bundle["fee_cents"])},
            {"label": "Total", "value": usd(bundle["total_cents"])},
        ]
    )
    if bundle.get("walk_minutes"):
        rows.append(
            {"label": "Walk", "value": f"About {bundle['walk_minutes']} minutes between shops"}
        )
    if any(line.get("handoff") for line in bundle["lines"]):
        rows.append(
            {
                "label": "Free room",
                "value": "Not covered by escrow: ResourcePilot re-checks it, you book it",
            }
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


def room_rows(line: dict[str, Any]) -> list[dict[str, str]]:
    rows = [{"label": "Room", "value": str(line["name"])}]
    if line.get("slot_label"):
        rows.append({"label": "When", "value": str(line["slot_label"])})
    if line.get("capacity"):
        rows.append({"label": "Seats", "value": str(line["capacity"])})
    if line.get("location_label"):
        rows.append({"label": "Where", "value": str(line["location_label"])})
    rows.extend(
        [
            {"label": "Price", "value": "Free"},
            {"label": "Book here", "value": str(line.get("handoff_url") or "")},
            {
                "label": "How it works",
                "value": (
                    "ResourcePilot re-checks the room live, then you book it on your "
                    "school's site with your own sign-in. Nothing is reserved for you."
                ),
            },
        ]
    )
    return rows


def bundle_detail(
    workflow_id: str, brief: dict[str, Any], bundle: dict[str, Any], *, checkout: bool
) -> tuple[str, dict[str, Any]]:
    ids = {"workflow_id": workflow_id, "bundle_id": bundle["bundle_id"]}
    rooms = brief.get("service") == "rooms"
    ctas = []
    if checkout:
        ctas.append(
            {
                "label": "Re-check this room" if rooms else "Hold everything",
                "primary": True,
                "selection": {"action": "place_holds", **ids},
            }
        )
    ctas.extend(
        [
            {
                "label": "Other rooms" if rooms else "Why not others?",
                "selection": {"action": "show_status" if rooms else "show_vetting", **ids},
            },
            {"label": "Start over", "selection": {"action": "start_over"}},
        ]
    )
    rows = bundle_rows(brief, bundle)
    if not rooms:
        rows.extend(
            {"label": f"Policy · {line['seller_name']}", "value": str(line["policy"])}
            for line in bundle["lines"]
        )
    title = str(bundle["lines"][0]["name"]) if rooms else str(bundle["label"])
    return "detail", {"title": title, "summary_rows": rows, "ctas": ctas}


def holds_review_card(
    workflow_id: str, brief: dict[str, Any], bundle: dict[str, Any], holds_expire_at: str
) -> tuple[str, dict[str, Any]]:
    rows = bundle_rows(brief, bundle)
    if int(bundle["total_cents"]) == 0:
        rows.append(
            {
                "label": "Re-checked",
                "value": f"Still open as of {_zone_time(utc_now().isoformat(), brief_zone(brief))}",
            }
        )
        return "review", {
            "title": "Still open",
            "summary_rows": rows,
            "approve_cta": {
                "label": "Get booking link",
                "primary": True,
                "selection": {"action": "confirm_free", "workflow_id": workflow_id},
            },
            "reject_cta": {
                "label": "Pick another",
                "selection": {"action": "release_holds", "workflow_id": workflow_id},
            },
        }
    rows.extend(
        [
            {
                "label": "Holds",
                "value": (
                    f"All {bundle['seller_count']} sellers are holding your items until "
                    f"{_zone_time(holds_expire_at)}"
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
    free = int(bundle["total_cents"]) == 0
    rows = []
    for booking in bookings:
        line = lines.get(f"{booking['seller_id']}:{booking['item_id']}", {})
        if line.get("handoff"):
            when = f" · {line['slot_label']}" if line.get("slot_label") else ""
            rows.append(
                {
                    "label": "Book it yourself",
                    "value": f"{line.get('name', 'Study room')}{when}: {line.get('handoff_url')}",
                }
            )
            continue
        rows.append(
            {
                "label": f"{CATEGORIES.get(line.get('category', ''), 'Item')} · "
                f"{names.get(booking['seller_id'], booking['seller_id'])}",
                "value": f"{line.get('name', booking['item_id'])}: {booking['confirmation_code']}",
            }
        )
    if any(line.get("handoff") for line in bundle["lines"]):
        rows.append(
            {
                "label": "Study room",
                "value": (
                    "Not reserved by ResourcePilot. Open the link and book with your own "
                    "sign-in; it was open when we re-checked it."
                ),
            }
        )
    if free:
        rows.append({"label": "Paid", "value": "Nothing: study rooms are free"})
    else:
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
    priced = len({line["seller_id"] for line in bundle["lines"] if not line.get("handoff")})
    return "detail", {
        "title": "Your booking link" if free else f"Booked with {priced} sellers",
        "summary_rows": rows,
        "ctas": [
            {"label": "My bookings", "primary": True, "selection": {"action": "show_bookings"}},
            {"label": "New request", "selection": {"action": "start_over"}},
        ],
    }


def housing_card(state_data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    school = dict(state_data.get("school") or {})
    housing = dict(state_data.get("housing") or {})
    rows = [{"label": str(label), "value": str(value)} for label, value in housing.get("facts", [])]
    if housing.get("office_url"):
        rows.append({"label": "Housing office", "value": str(housing["office_url"])})
    else:
        rows.append({"label": "Housing office", "value": "Not found on the school's site"})
    if housing.get("portal_url"):
        label = str(housing.get("portal_label") or "Application")
        rows.append({"label": f"Apply · {label}"[:40], "value": str(housing["portal_url"])})
    rows.append(
        {
            "label": "Next step",
            "value": "Apply on your school's site with your own sign-in. ResourcePilot never "
            "applies or pays for housing.",
        }
    )
    sources = [housing.get("facts_source") or "", "Links re-checked live by Verification"]
    if housing.get("unverified"):
        sources.append(f"Dropped links that did not open: {', '.join(housing['unverified'])}")
    rows.append({"label": "Sources", "value": "; ".join(source for source in sources if source)})
    place = ", ".join(part for part in (school.get("city"), school.get("state")) if part)
    title = f"Housing at {school.get('school_name') or 'your school'}"
    if place:
        title += f" · {place}"
    return "detail", {
        "title": title,
        "summary_rows": rows,
        "ctas": [
            {
                "label": "Study rooms",
                "primary": True,
                "selection": {"action": "open_service", "service": "rooms"},
            },
            {"label": "Start over", "selection": {"action": "start_over"}},
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
                "value": f"Refunded automatically at {_zone_time(refund_due_at)} if you wait",
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
                "label": "Back to options",
                "primary": True,
                "selection": {"action": "show_status", "workflow_id": workflow_id},
            }
        ],
    }


def _booking_row(booking: dict[str, Any]) -> dict[str, str]:
    brief, bundle = booking["data"]["brief"], booking["bundle"]
    if brief.get("service") == "rooms":
        line = bundle["lines"][0]
        return {
            "label": str(line.get("slot_label") or describe_window(brief)),
            "value": f"{line['name']} · {line.get('school_name') or ''} · free link",
        }
    return {
        "label": describe_window(brief),
        "value": f"{_sellers(bundle)} · {usd(bundle['total_cents'])}",
    }


def bookings_card(bookings: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    rows = [
        _booking_row(booking)
        for booking in bookings
        if booking.get("bundle") and booking.get("data", {}).get("brief")
    ] or [{"label": "Bookings", "value": "No bookings yet"}]
    return "detail", {
        "title": "Your recent bookings",
        "summary_rows": rows,
        "ctas": [{"label": "New request", "primary": True, "selection": {"action": "start_over"}}],
    }
