"""ASI:One cards for every Orchestrator reply, plus profile status cards for every agent.

ResourcePilot offers four campus services from a menu: free study rooms at the student's own
school, health and wellness (quiet rooms, the week's wellness events, and the 988 crisis line),
campus housing facts with official links, and an emergency demo drill that phones one teammate,
never 911. Every reply is a card: the menu, a short form,
progress, a carousel of open rooms, the wellness card, a room detail, the booking-link receipt,
the housing card, and recent bookings. ResourcePilot never reserves a room or applies for
housing, so no card asks for payment.

Every payload uses only the fields documented on the Innovation Lab interactive-cards page.
ASI:One silently falls back to plain text when a payload has an unexpected shape, so
``card_message`` rejects anything else before it is sent. Selections carry stable workflow and
option IDs; the prose fallback in ``parse_selection`` only opens menus and views.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from resourcepilot.intake import (
    HOUSING_FEATURES,
    HOUSING_STYLES,
    HOUSING_WHO,
    ROOM_DURATIONS,
    WELLNESS_DURATIONS,
    describe_window,
    upcoming_days,
)
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

# Every action a card selection can carry; the Orchestrator routes exactly these.
KNOWN_ACTIONS = (
    "start_over",
    "open_service",
    "submit_form",
    "show_bookings",
    "show_status",
    "view_room",
    "recheck_room",
    "show_results",
    "submit_link",
    "view_housing",
    "show_housing_facts",
    "show_all_housing",
    "place_call",
    "call_status",
    "open_orchestrator",
)
# Bounded allowlist for planner-mediated prose: it only opens menus and views. Submitting a form,
# re-checking a room, submitting a link, or placing a demo call needs the stable IDs that only a
# card selection carries.
PROSE_ACTIONS = ("open_orchestrator", "show_status", "show_results", "show_bookings", "start_over")
# Prose that only opens a service's form, never submits one.
SERVICE_WORDS = (
    ("rooms", re.compile(r"\bstudy\s*rooms?\b|\blibrary\s+rooms?\b|\bbook\s+a\s+room\b", re.I)),
    (
        "wellness",
        re.compile(
            r"\bwellness\b|\bmeditation\b|\bmental\s+health\b|\bhealth\s+events?\b"
            r"|\bflu\s+shots?\b",
            re.I,
        ),
    ),
    ("housing", re.compile(r"\bhousing\b|\bdorms?\b|\bresidence\s+halls?\b", re.I)),
)
SERVICE_TITLES = {
    "rooms": "Study rooms",
    "wellness": "Health & wellness",
    "housing": "Campus housing",
    "emergency": "Emergency (demo)",
}
# Static on purpose: crisis help never depends on a crawl, a feed, or a school being found.
CRISIS_TEXT = (
    "In crisis? Call or text 988, the Suicide & Crisis Lifeline, any time. "
    "In an emergency, call 911."
)
# On every emergency card: the drill must never be mistaken for a real call for help.
DEMO_NOTICE = (
    "Demo drill: this calls your team's demo contact, never 911 or campus police. "
    "In a real emergency, call 911."
)
HOW_ROOMS_WORK = (
    "ResourcePilot re-checks the room live, then you book it on your school's site with your "
    "own sign-in. Nothing is reserved for you."
)
NOT_RESERVED = "Not reserved by ResourcePilot: book it with your own sign-in"


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


def parse_selection(text: str, known_actions: tuple[str, ...] = PROSE_ACTIONS) -> dict[str, Any]:
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
    """Profile chat card shared by every specialist; it points to the Orchestrator."""
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
        "wellness",
        "Health & wellness",
        "Live · any school",
        "success",
        "Meditation and quiet rooms open at your school, plus this week's wellness events and "
        "clinics. Free: you book or RSVP on your school's site. We never ask about your health.",
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
        "emergency",
        "Emergency (demo)",
        "Demo drill · not 911",
        "warning",
        "Practice reporting a campus emergency: a voice agent phones your team's demo contact "
        "and describes it. It never calls 911. In a real emergency, call 911.",
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
    children.append({"type": "text", "value": CRISIS_TEXT, "style": "muted"})
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


def _email_field() -> dict[str, Any]:
    return {"name": "email", "kind": "text", "label": "School email", "required": True}


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


def wellness_form(days: list[tuple[str, str]] | None = None) -> tuple[str, dict[str, Any]]:
    """No health questions: only the school, a day, and how long a quiet room is needed."""
    return "form", {
        "title": f"Health & wellness at your school. {CRISIS_TEXT}",
        "fields": [
            _email_field(),
            _select("day", "Day", _day_options(days or upcoming_days()), required=True),
            _select(
                "window",
                "Time of day",
                [(key, value[0]) for key, value in WINDOWS.items()],
                required=True,
            ),
            _select("duration", "Quiet room for", list(WELLNESS_DURATIONS.items()), required=True),
        ],
        "submit_cta": {
            "label": "Find rooms and events",
            "selection": {"action": "submit_form", "service": "wellness"},
        },
    }


def emergency_form() -> tuple[str, dict[str, Any]]:
    """The drill's facts. No phone number field: only the configured demo contact is called."""
    return "form", {
        "title": f"Emergency demo drill. {DEMO_NOTICE}",
        "fields": [
            _select(
                "kind",
                "What is happening",
                [
                    ("medical", "Medical"),
                    ("fire", "Fire or smoke"),
                    ("danger", "Someone in danger"),
                    ("other", "Other"),
                ],
                required=True,
            ),
            {"name": "location", "kind": "text", "label": "Where on campus", "required": True},
            _select("people", "People who need help", [(str(n), str(n)) for n in range(1, 11)]),
            {"name": "details", "kind": "text", "label": "Anything else (optional)"},
            _select("caller_safe", "Are you safe right now?", [("yes", "Yes"), ("no", "No")]),
            {"name": "school", "kind": "text", "label": "School (optional)"},
        ],
        "submit_cta": {
            "label": "Review the demo call",
            "selection": {"action": "submit_form", "service": "emergency"},
        },
    }


def housing_form() -> tuple[str, dict[str, Any]]:
    """The school plus the criteria the Planner matches each hall against."""
    must_haves = [
        {"name": f"need_{key}", "kind": "checkbox", "label": label}
        for key, label in HOUSING_FEATURES.items()
    ]
    return "form", {
        "title": "On-campus housing at your school",
        "fields": [
            _email_field(),
            _select("who", "I am a", list(HOUSING_WHO.items()), required=True),
            _select("style", "Housing style", list(HOUSING_STYLES.items())),
            *must_haves,
            {
                "name": "max_budget",
                "kind": "number",
                "label": "Max yearly budget, USD",
            },
        ],
        "submit_cta": {
            "label": "Find housing",
            "selection": {"action": "submit_form", "service": "housing"},
        },
    }


def service_form(service: str) -> tuple[str, dict[str, Any]]:
    """The form for one service; an unknown service gets the rooms form, like the chains."""
    if service == "housing":
        return housing_form()
    if service == "wellness":
        return wellness_form()
    if service == "emergency":
        return emergency_form()
    return rooms_form()


def room_link_card(workflow_id: str, school: str) -> tuple[str, dict[str, Any]]:
    """Ask for the booking link the Researcher could not find, instead of guessing one."""
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
                _buttons(
                    [
                        ("Check this link", {"action": "submit_link", "workflow_id": workflow_id}),
                        ("Start over", {"action": "start_over"}),
                    ]
                ),
            ],
        }
    }


# ---------------------------------------------------------------------------
# Progress, rooms, housing, and bookings
# ---------------------------------------------------------------------------


STAGE_LABELS = {
    "intake": "Reading your form",
    "researcher": "Researching your school",
    "campus_rooms": "Reading open rooms",
    "verification": "Verifying every result",
    "planner": "Ranking open rooms",
    "recheck": "Re-checking your room",
    "link": "Waiting for a link",
    "emergency": "Preparing the demo call",
    "call": "Placing the demo call",
    "call_status": "Checking the call",
}


def progress_card(
    workflow_id: str, stage: str, chain: tuple[str, ...] = ()
) -> tuple[str, dict[str, Any]]:
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": STAGE_LABELS.get(stage, stage.title()), "variant": "info"}
    ]
    if stage in chain:
        done = chain[: chain.index(stage)]
        steps = [f"✓ {STAGE_LABELS.get(role, role.title())}" for role in done]
        steps.append(f"• {STAGE_LABELS.get(stage, stage.title())}")
        children.append({"type": "text", "value": "\n".join(steps), "style": "body"})
    children.append({"type": "text", "value": f"Workflow {workflow_id[:8]}", "style": "muted"})
    children.append(
        _button("Refresh status", {"action": "show_status", "workflow_id": workflow_id})
    )
    return "custom", {"root": {"type": "section", "title": "Agents at work", "children": children}}


def rooms_carousel(
    workflow_id: str, options: list[dict[str, Any]], school: str
) -> tuple[str, dict[str, Any]]:
    """One item per planned option; the item ID and the selection carry its stable option_id."""
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
                "id": str(option["option_id"]),
                "title": str(line["name"]),
                "subtitle": str(line.get("slot_label") or line.get("location_label") or ""),
                "secondary_text": f"Free · {seats} · {line.get('location_label') or ''}",
                "badges": badges,
                "primary_cta": {
                    "label": "View room",
                    "selection": {
                        "action": "view_room",
                        "workflow_id": workflow_id,
                        "option_id": str(option["option_id"]),
                    },
                },
            }
        )
    return "carousel", {
        "title": f"Open study rooms at {school}",
        "subtitle": "Re-checked live before you get the booking link. Always free.",
        "items": items,
    }


def room_rows(line: dict[str, Any], *, wellness: bool = False) -> list[dict[str, str]]:
    """Summary rows for one room line; the booking link is a visible row value."""
    rows = [{"label": "Room", "value": str(line["name"])}]
    if line.get("slot_label"):
        rows.append({"label": "When", "value": str(line["slot_label"])})
    if line.get("capacity"):
        rows.append({"label": "Seats", "value": str(line["capacity"])})
    if line.get("location_label"):
        rows.append({"label": "Where", "value": str(line["location_label"])})
    if line.get("school_name"):
        rows.append({"label": "School", "value": str(line["school_name"])})
    rows.extend(
        [
            {
                "label": "Times",
                "value": f"Live from {line.get('source_label') or 'LibCal'}"
                if line.get("live_times")
                else "Not shown live: check times on the booking page",
            },
            {"label": "Price", "value": "Free"},
            {"label": "Book here", "value": str(line.get("handoff_url") or "")},
            {"label": "How it works", "value": str(line.get("policy") or HOW_ROOMS_WORK)},
        ]
    )
    if wellness:
        rows.append({"label": "Need help now?", "value": CRISIS_TEXT})
    return rows


def room_detail(
    workflow_id: str, option: dict[str, Any], *, wellness: bool = False
) -> tuple[str, dict[str, Any]]:
    """One room before the re-check; the primary CTA asks Campus Rooms to re-check it live."""
    line = option["lines"][0]
    ids = {"workflow_id": workflow_id, "option_id": str(option["option_id"])}
    return "detail", {
        "title": str(line["name"]),
        "summary_rows": room_rows(line, wellness=wellness),
        "ctas": [
            {
                "label": "Re-check this room",
                "primary": True,
                "selection": {"action": "recheck_room", **ids},
            },
            {
                "label": "Other rooms",
                "selection": {"action": "show_results", "workflow_id": workflow_id},
            },
        ],
    }


def room_link_receipt(
    option: dict[str, Any], *, service: str = "rooms"
) -> tuple[str, dict[str, Any]]:
    """The booking link after a successful re-check. Safe as a terminal card: only CTAs remain."""
    line = option["lines"][0]
    rows = [{"label": "Room", "value": str(line["name"])}]
    if line.get("slot_label"):
        rows.append({"label": "When", "value": str(line["slot_label"])})
    if line.get("location_label"):
        rows.append({"label": "Where", "value": str(line["location_label"])})
    rows.extend(
        [
            {"label": "Book here", "value": str(line.get("handoff_url") or "")},
            {"label": "Next step", "value": NOT_RESERVED},
            {
                "label": "Re-checked",
                "value": "Open when ResourcePilot re-checked it live just now"
                if line.get("live_times")
                else "Not checked live: confirm the time on the booking page",
            },
            {"label": "Price", "value": "Free"},
        ]
    )
    if service == "wellness":
        rows.append({"label": "Need help now?", "value": CRISIS_TEXT})
    return "detail", {
        "title": "Your booking link",
        "summary_rows": rows,
        "ctas": [
            {
                "label": "Find another room",
                "primary": True,
                "selection": {
                    "action": "open_service",
                    "service": "wellness" if service == "wellness" else "rooms",
                },
            },
            {"label": "Menu", "selection": {"action": "start_over"}},
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
    if housing.get("options_status") == "not_found":
        school_name = school.get("school_name") or "your school"
        rows.append(
            {
                "label": "Halls",
                "value": f"Couldn't read individual halls from {school_name}'s site. "
                "The housing office lists them.",
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


CHECK_LABELS = {
    "who": "Who it's for",
    "style": "Style",
    "budget": "Budget",
    **HOUSING_FEATURES,
}
STYLE_NAMES = {"traditional": "Traditional hall", "suite": "Suite", "apartment": "Apartment"}
WHO_NAMES = {
    "first_year": "First-year",
    "transfer": "Transfer",
    "continuing": "Continuing",
    "graduate": "Graduate",
    "family": "Families",
}
FEATURE_SHORT = {
    "ac": "A/C",
    "kitchen": "Kitchen",
    "private_bath": "Private bath",
    "gender_inclusive": "Gender-inclusive",
    "accessible": "Accessible",
}


def _usd(cents: int) -> str:
    return f"${cents // 100:,}"


def hall_price(hall: dict[str, Any]) -> str:
    """How the hall's own page states its price, with a yearly figure to compare halls."""
    label, yearly = str(hall.get("price_label") or ""), int(hall.get("yearly_cents") or 0)
    if not label or not yearly:
        return "Price not listed on the hall's page"
    if label.endswith(("year", "academic year")):
        return f"From {label}"
    return f"From {label} (about {_usd(yearly)} a year)"


def _hall_subtitle(hall: dict[str, Any]) -> str:
    style = STYLE_NAMES.get(str(hall.get("style") or ""), "Style not listed")
    who = ", ".join(WHO_NAMES[key] for key in hall.get("who") or [] if key in WHO_NAMES)
    return f"{style} · {who}" if who else style


def _hall_badges(hall: dict[str, Any]) -> list[dict[str, str]]:
    if hall.get("ruled_out"):
        names = ", ".join(CHECK_LABELS.get(key, key) for key in hall["ruled_out"])
        badges = [{"label": f"Doesn't fit: {names}"[:40], "variant": "warning"}]
    elif hall.get("unknown"):
        names = ", ".join(
            FEATURE_SHORT.get(key, CHECK_LABELS.get(key, key)) for key in hall["unknown"]
        )
        badges = [{"label": f"Not listed: {names}"[:40], "variant": "warning"}]
    else:
        badges = [{"label": "Matches", "variant": "success"}]
    badges.extend(
        {"label": FEATURE_SHORT[key], "variant": "info"}
        for key in hall.get("features") or []
        if key in FEATURE_SHORT
    )
    return badges[:4]


def housing_carousel(
    workflow_id: str, data: dict[str, Any], *, show_all: bool = False
) -> tuple[str, dict[str, Any]]:
    """Every hall the Planner kept (or all of them), plus two items for facts and the full list."""
    school = str(data.get("school", {}).get("school_name") or "your school")
    halls = list(data.get("all_halls" if show_all else "halls") or [])
    items: list[dict[str, Any]] = []
    for hall in halls:
        item: dict[str, Any] = {
            "id": str(hall["option_id"]),
            "title": str(hall["name"]),
            "subtitle": _hall_subtitle(hall),
            "secondary_text": hall_price(hall),
            "badges": _hall_badges(hall),
            "primary_cta": {
                "label": "View hall",
                "selection": {
                    "action": "view_housing",
                    "workflow_id": workflow_id,
                    "option_id": str(hall["option_id"]),
                },
            },
        }
        if hall.get("photo"):
            item["image"] = str(hall["photo"])
        items.append(item)
    hidden = int(data.get("hidden_count") or 0)
    if hidden and not show_all:
        items.append(
            {
                "id": "show_all",
                "title": f"Show all {len(data.get('all_halls') or [])} halls",
                "subtitle": f"{hidden} hidden because their page rules out your criteria",
                "primary_cta": {
                    "label": "Show all",
                    "selection": {"action": "show_all_housing", "workflow_id": workflow_id},
                },
            }
        )
    items.append(
        {
            "id": "housing_facts",
            "title": "Housing facts and how to apply",
            "subtitle": "Dorm beds, typical costs, the housing office, and its application portal",
            "primary_cta": {
                "label": "Housing facts",
                "selection": {"action": "show_housing_facts", "workflow_id": workflow_id},
            },
        }
    )
    housing = dict(data.get("housing") or {})
    facts = dict(housing.get("facts") or [])
    beds = (
        f"{facts['Dorm beds']} dorm beds · "
        if facts.get("Dorm beds", "").replace(",", "").isdigit()
        else ""
    )
    return "carousel", {
        "title": f"On-campus housing at {school}",
        "subtitle": (
            f"{beds}{len(halls)} option{'s' if len(halls) != 1 else ''} from the school's own "
            "pages, matches first. You apply on the school's site."
        ),
        "items": items,
    }


def housing_detail(
    workflow_id: str, hall: dict[str, Any], data: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """One hall as its own page describes it, how it fits the criteria, and where to apply."""
    housing = dict(data.get("housing") or {})
    rows = [
        {"label": "Style", "value": STYLE_NAMES.get(str(hall.get("style") or ""), "Not listed")},
        {
            "label": "Who it's for",
            "value": ", ".join(WHO_NAMES[key] for key in hall.get("who") or [] if key in WHO_NAMES)
            or "Not listed",
        },
        {
            "label": "Rooms",
            "value": ", ".join(room.title() for room in hall.get("rooms") or []) or "Not listed",
        },
        {
            "label": "Features",
            "value": ", ".join(HOUSING_FEATURES[key] for key in hall.get("features") or [])
            or "None listed",
        },
        {"label": "Price", "value": hall_price(hall)},
    ]
    if hall.get("about"):
        rows.append({"label": "About", "value": str(hall["about"])})
    for key, value in (hall.get("checks") or {}).items():
        verdict = {
            "match": "Fits",
            "unknown": "Not listed on the hall's page",
            "out": "Doesn't fit",
        }
        rows.append(
            {"label": f"Your criteria · {CHECK_LABELS.get(key, key)}", "value": verdict[value]}
        )
    rows.append({"label": "Official page", "value": str(hall["url"])})
    apply_url = str(housing.get("portal_url") or housing.get("office_url") or "")
    rows.append(
        {
            "label": "Apply",
            "value": (
                f"{apply_url}: apply on your school's site with your own sign-in. "
                if apply_url
                else "Apply through your school's housing office. "
            )
            + "ResourcePilot never applies for, reserves, or pays for housing.",
        }
    )
    rows.append(
        {"label": "Source", "value": "Read from the hall's page and re-opened by Verification"}
    )
    payload: dict[str, Any] = {
        "title": str(hall["name"]),
        "summary_rows": rows,
        "ctas": [
            {
                "label": "Back to halls",
                "primary": True,
                "selection": {"action": "show_status", "workflow_id": workflow_id},
            },
            {
                "label": "Housing facts",
                "selection": {"action": "show_housing_facts", "workflow_id": workflow_id},
            },
            {"label": "Start over", "selection": {"action": "start_over"}},
        ],
    }
    if hall.get("photo"):
        payload = {"title": payload["title"], "hero_image": str(hall["photo"]), **payload}
    return "detail", payload


def no_housing_match_card(workflow_id: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    total = len(data.get("all_halls") or [])
    school = str(data.get("school", {}).get("school_name") or "your school")
    return notice_card(
        f"No hall at {school} fits",
        f"ResourcePilot read {total} halls from {school}'s site, and each one's page rules out at "
        "least one of your criteria. See them all, or change your criteria.",
        badge="No match",
        variant="warning",
        buttons=[
            ("Show all halls", {"action": "show_all_housing", "workflow_id": workflow_id}),
            ("Change criteria", {"action": "open_service", "service": "housing"}),
            ("Housing facts", {"action": "show_housing_facts", "workflow_id": workflow_id}),
        ],
    )


def wellness_card(workflow_id: str, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Crisis line first, then open wellness rooms, then the week's events. Never empty."""
    school = dict(data.get("school") or {})
    name = str(school.get("school_name") or "your school")
    wellness = dict(data.get("wellness") or {})
    options = list(data.get("options") or [])
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": "Crisis help · 24/7", "variant": "warning"},
        {"type": "text", "value": CRISIS_TEXT, "style": "body"},
        {"type": "divider"},
        {"type": "heading", "value": "Wellness rooms open", "level": 3},
    ]
    if options:
        for option in options:
            line = option["lines"][0]
            detail = " · ".join(
                part
                for part in (
                    str(line.get("slot_label") or ""),
                    str(line.get("location_label") or ""),
                )
                # LibCal often names a one-room group after the room itself.
                if part and part != str(line["name"])
            )
            children.append(
                {
                    "type": "group",
                    "direction": "row",
                    "children": [
                        {
                            "type": "text",
                            "value": f"{line['name']} · {detail}" if detail else str(line["name"]),
                            "style": "body",
                        },
                        _button(
                            "View room",
                            {
                                "action": "view_room",
                                "workflow_id": workflow_id,
                                "option_id": str(option["option_id"]),
                            },
                        ),
                    ],
                }
            )
        children.append(
            {
                "type": "text",
                "value": "Free. Re-checked live before you get the booking link.",
                "style": "muted",
            }
        )
    else:
        rooms = dict(data.get("rooms") or {})
        if data.get("room_note"):
            note = str(data["room_note"])
        elif rooms.get("status") != "found":
            note = f"No public room-booking system found for {name}"
        else:
            note = f"No wellness room at {name} is open in that window"
        children.append({"type": "text", "value": note, "style": "muted"})
    children.extend(
        [{"type": "divider"}, {"type": "heading", "value": "Wellness events this week", "level": 3}]
    )
    events = list(wellness.get("events") or [])
    if events:
        children.append(
            {
                "type": "list",
                "items": [
                    {
                        "children": [
                            {"type": "text", "value": str(event["title"]), "style": "body"},
                            {
                                "type": "text",
                                "value": " · ".join(
                                    part
                                    for part in (
                                        str(event["when"]),
                                        str(event.get("location") or ""),
                                    )
                                    if part
                                ),
                                "style": "muted",
                            },
                            {"type": "text", "value": f"RSVP: {event['url']}", "style": "muted"},
                        ]
                    }
                    for event in events
                ],
            }
        )
    elif wellness.get("status") == "found":
        children.append(
            {"type": "text", "value": "No wellness events listed this week", "style": "muted"}
        )
    elif wellness.get("status") == "unavailable":
        children.append(
            {"type": "text", "value": "The events calendar did not answer", "style": "muted"}
        )
    else:
        children.append(
            {
                "type": "text",
                "value": f"No public events calendar found for {name}",
                "style": "muted",
            }
        )
    sources = []
    if wellness.get("feed_host"):
        sources.append(f"Events: {wellness['feed_host']} (Localist), re-checked by Verification.")
    if wellness.get("unverified"):
        sources.append(f"Dropped events no longer listed: {len(wellness['unverified'])}.")
    sources.append("ResourcePilot never asks about your health or books appointments.")
    children.extend(
        [
            {"type": "divider"},
            {"type": "text", "value": " ".join(sources), "style": "muted"},
            _buttons(
                [
                    ("Study rooms", {"action": "open_service", "service": "rooms"}),
                    ("Start over", {"action": "start_over"}),
                ]
            ),
        ]
    )
    return "custom", {
        "root": {"type": "section", "title": f"Health & wellness at {name}", "children": children}
    }


def emergency_review_card(workflow_id: str, preview: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """What will be said and to whom, before anything is dialed. Only "Call now" places it."""
    rows = [{"label": str(label), "value": str(value)} for label, value in preview.get("rows", [])]
    rows.append({"label": "Opening line", "value": str(preview.get("opening") or "")})
    rows.append({"label": "Not 911", "value": DEMO_NOTICE})
    return "review", {
        "title": "Review the demo call",
        "summary_rows": rows,
        "approve_cta": {
            "label": "Call now",
            "primary": True,
            "selection": {"action": "place_call", "workflow_id": workflow_id},
        },
        "reject_cta": {"label": "Cancel", "selection": {"action": "start_over"}},
    }


def emergency_call_card(
    workflow_id: str, call: dict[str, Any], status: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """The placed demo call, and once checked, its status, summary, and transcript."""
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": "Demo drill · not 911", "variant": "warning"},
        {"type": "text", "value": DEMO_NOTICE, "style": "body"},
        {
            "type": "text",
            "value": f"Calling your demo contact ({call.get('to') or 'not shown'}).",
            "style": "body",
        },
    ]
    if status:
        children.append(
            {
                "type": "text",
                "value": f"{status.get('status_label') or 'Checking the call'} · "
                f"{status.get('duration') or '0:00'}",
                "style": "muted",
            }
        )
        if status.get("summary"):
            children.extend(
                [
                    {"type": "heading", "value": "Summary", "level": 3},
                    {"type": "text", "value": str(status["summary"]), "style": "body"},
                ]
            )
        lines = list(status.get("transcript") or [])
        if lines:
            children.append({"type": "heading", "value": "Transcript", "level": 3})
            children.append(
                {
                    "type": "list",
                    "items": [
                        {
                            "children": [
                                {"type": "text", "value": str(line["speaker"]), "style": "muted"},
                                {"type": "text", "value": str(line["message"]), "style": "body"},
                            ]
                        }
                        for line in lines
                    ],
                }
            )
    else:
        children.append(
            {
                "type": "text",
                "value": "Put the demo phone on speaker. Check the call for its transcript.",
                "style": "muted",
            }
        )
    children.append(
        _buttons(
            [
                ("Check the call", {"action": "call_status", "workflow_id": workflow_id}),
                ("Menu", {"action": "start_over"}),
            ]
        )
    )
    return "custom", {"root": {"type": "section", "title": "Demo call", "children": children}}


def _booking_row(booking: dict[str, Any]) -> dict[str, str]:
    data = booking["data"]
    brief = dict(data.get("brief") or {})
    line = booking["option"]["lines"][0]
    school = (data.get("school") or {}).get("school_name") or line.get("school_name")
    when = line.get("slot_label") or (describe_window(brief) if brief.get("window_start") else "")
    name = f"{line['name']} · {school}" if school else str(line["name"])
    link = line.get("handoff_url")
    kind = "Wellness room" if brief.get("service") == "wellness" else "Study room"
    return {
        "label": str(when or kind),
        "value": f"{name}: {link}" if link else name,
    }


def bookings_card(bookings: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Recent booked workflow states: each keeps its chosen option and its brief and school."""
    rows = [
        _booking_row(booking)
        for booking in bookings
        if (booking.get("option") or {}).get("lines") and (booking.get("data") or {}).get("brief")
    ][:10] or [{"label": "Bookings", "value": "No bookings yet"}]
    return "detail", {
        "title": "Your recent booking links",
        "summary_rows": rows,
        "ctas": [{"label": "New request", "primary": True, "selection": {"action": "start_over"}}],
    }
