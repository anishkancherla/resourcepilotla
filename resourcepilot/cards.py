"""ASI:One cards for every Orchestrator reply, plus profile status cards for every agent.

ResourcePilot offers two campus services from a menu: free study rooms at the student's own
school and campus housing facts with official links. Every reply is a card: the menu, a short
form, progress, a carousel of open rooms, a room detail, the booking-link receipt, the housing
card, and recent bookings. ResourcePilot never reserves a room or applies for housing, so no card
asks for payment.

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

from resourcepilot.intake import ROOM_DURATIONS, describe_window, upcoming_days
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
    "open_orchestrator",
)
# Bounded allowlist for planner-mediated prose: it only opens menus and views. Submitting a form,
# re-checking a room, or submitting a link needs the stable IDs that only a card selection carries.
PROSE_ACTIONS = ("open_orchestrator", "show_status", "show_results", "show_bookings", "start_over")
# Prose that only opens a service's form, never submits one.
SERVICE_WORDS = (
    ("rooms", re.compile(r"\bstudy\s*rooms?\b|\blibrary\s+rooms?\b|\bbook\s+a\s+room\b", re.I)),
    ("housing", re.compile(r"\bhousing\b|\bdorms?\b|\bresidence\s+halls?\b", re.I)),
)
SERVICE_TITLES = {"rooms": "Study rooms", "housing": "Campus housing"}
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
        "housing",
        "Campus housing",
        "Live · IPEDS",
        "success",
        "Dorm beds, typical room and board, and meal plans, plus your housing office and "
        "application portal. You apply on your school's site.",
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


def housing_form() -> tuple[str, dict[str, Any]]:
    return "form", {
        "title": "Housing at your school",
        "fields": [_email_field()],
        "submit_cta": {
            "label": "Look up housing",
            "selection": {"action": "submit_form", "service": "housing"},
        },
    }


def service_form(service: str) -> tuple[str, dict[str, Any]]:
    """The form for one service; anything but housing gets the rooms form, like the chains."""
    return housing_form() if service == "housing" else rooms_form()


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


def room_rows(line: dict[str, Any]) -> list[dict[str, str]]:
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
    return rows


def room_detail(workflow_id: str, option: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """One room before the re-check; the primary CTA asks Campus Rooms to re-check it live."""
    line = option["lines"][0]
    ids = {"workflow_id": workflow_id, "option_id": str(option["option_id"])}
    return "detail", {
        "title": str(line["name"]),
        "summary_rows": room_rows(line),
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


def room_link_receipt(option: dict[str, Any]) -> tuple[str, dict[str, Any]]:
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
    return "detail", {
        "title": "Your booking link",
        "summary_rows": rows,
        "ctas": [
            {
                "label": "Find another room",
                "primary": True,
                "selection": {"action": "open_service", "service": "rooms"},
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


def _booking_row(booking: dict[str, Any]) -> dict[str, str]:
    data = booking["data"]
    brief = dict(data.get("brief") or {})
    line = booking["option"]["lines"][0]
    school = (data.get("school") or {}).get("school_name") or line.get("school_name")
    when = line.get("slot_label") or (describe_window(brief) if brief.get("window_start") else "")
    name = f"{line['name']} · {school}" if school else str(line["name"])
    link = line.get("handoff_url")
    return {
        "label": str(when or "Study room"),
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
