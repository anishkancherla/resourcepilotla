from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from provider_fakes import SEARCH_DAY, dumps
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

from resourcepilot.cards import example_bundle_selection

# A distinctive local part, so the safety check below can prove it was never kept.
EMAIL_USER = "zq-student-7781"


def chat(text: str | dict) -> ChatMessage:
    body = text if isinstance(text, str) else json.dumps(text)
    return ChatMessage(
        timestamp=datetime.now(UTC), msg_id=uuid4(), content=[TextContent(type="text", text=body)]
    )


def last_card(network, kind: str | None = None) -> tuple[str, dict, str]:
    found = [card for card in network.cards() if kind is None or card[0] == kind]
    assert found, f"no {kind or 'card'} received; texts: {network.texts()}"
    return found[-1]


def completed_roles(network) -> list[str]:
    events = next(iter(network.store.events.values()))
    return [e["details"].get("role") for e in events if e["event_type"] == "specialist_completed"]


def rooms_form(domain: str = "purdue.edu", **fields: str) -> dict:
    return {
        "action": "submit_form",
        "service": "rooms",
        "email": f"{EMAIL_USER}@{domain}",
        "day": SEARCH_DAY.isoformat(),
        "window": "afternoon",
        "duration": "60",
        "group_size": "2",
        **fields,
    }


async def test_every_reply_is_a_card_and_free_text_gets_the_menu(network) -> None:
    await network.send_user(chat("hi"))
    kind, payload, _ = last_card(network)
    assert (kind, payload["root"]["title"]) == ("custom", "ResourcePilot campus services")
    await network.send_user(chat("I need a DSLR Friday 3-6pm under $35"))
    _, menu, narration = last_card(network)
    assert menu["root"]["title"] == "ResourcePilot campus services"
    assert "buttons and forms" in narration
    assert network.store.workflows == {}  # a sentence never starts a request
    await network.send_user(chat("can I book a study room"))
    _, form, _ = last_card(network, "form")
    assert form["submit_cta"]["selection"]["service"] == "rooms"
    for service in ("wellness", "housing", "bundle"):
        await network.send_user(chat({"action": "open_service", "service": service}))
        assert last_card(network, "form")[1]["submit_cta"]["selection"]["service"] == service
    assert len(network.cards()) == len(network.texts())  # no plain-text reply anywhere


async def test_demo_bundle_runs_the_planning_network(network) -> None:
    await network.send_user(chat(example_bundle_selection()))
    _, payload, narration = last_card(network, "carousel")
    assert payload["items"], narration
    assert "Flash Prints 4U" in narration
    assert "Lowest price" in [item["title"] for item in payload["items"]]
    assert network.dropped == []
    assert completed_roles(network) == [
        "intake",
        "discovery",
        "verification",
        "analysis",
        "planner",
    ]
    assert network.store.stats["gear_share"]["quotes_sent"] == 1
    assert "campus_rooms" not in network.store.stats  # no study room, so it is never asked
    assert network.store.preferences  # Memory/Audit saved the buyer's preferences


async def test_bundle_detail_and_vetting_cards(network) -> None:
    await network.send_user(chat(example_bundle_selection()))
    _, carousel, _ = last_card(network, "carousel")
    await network.send_user(chat(carousel["items"][0]["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    labels = [row["label"] for row in detail["summary_rows"]]
    assert "Booking fee" in labels and "Total" in labels
    why = next(cta for cta in detail["ctas"] if cta["selection"]["action"] == "show_vetting")
    await network.send_user(chat(why["selection"]))
    _, vetting, _ = last_card(network, "detail")
    reasons = " ".join(row["value"] for row in vetting["summary_rows"])
    assert "Identity not verified" in reasons
    assert "Requires gear-share training" in reasons


async def test_silent_seller_is_reported_after_the_quote_window(network) -> None:
    await network.store.set_fault_mode("studio_rooms", "ghost_quotes")
    await network.send_user(chat(example_bundle_selection()))
    assert not [card for card in network.cards() if card[0] == "carousel"]
    await network.advance(6)
    narration = last_card(network, "carousel")[2]
    assert "podcast_room" in narration
    assert network.store.stats["studio_rooms"]["no_responses"] == 1


async def test_incomplete_form_asks_again_with_the_same_form(network) -> None:
    await network.send_user(chat({"action": "submit_form", "service": "bundle", "posters": "5"}))
    kind, form, narration = last_card(network)
    assert kind == "form"
    assert form["submit_cta"]["selection"]["service"] == "bundle"
    assert "Missing" in narration


async def test_study_room_flow_ends_in_a_free_booking_link(network) -> None:
    await network.send_user(chat(rooms_form()))
    _, carousel, narration = last_card(network, "carousel")
    assert "Purdue" in narration
    assert completed_roles(network) == [
        "intake",
        "researcher",
        "discovery",
        "verification",
        "analysis",
        "planner",
    ]
    first = carousel["items"][0]
    # Both rooms open at noon; the 2-seat room is the snuggest fit for a group of 2.
    assert [item["title"] for item in carousel["items"]] == ["WALC 3138", "HSSE 2101"]
    assert "Live · LibCal" in [badge["label"] for badge in first["badges"]]
    await network.send_user(chat(first["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    assert detail["ctas"][0]["label"] == "Re-check this room"
    await network.send_user(chat(detail["ctas"][0]["selection"]))
    _, review, _ = last_card(network, "review")
    assert review["approve_cta"]["selection"]["action"] == "confirm_free"
    await network.send_user(chat(review["approve_cta"]["selection"]))
    _, receipt, _ = last_card(network, "detail")
    assert receipt["title"] == "Your booking link"
    assert "https://calendar.lib.purdue.edu/space/102" in json.dumps(receipt)
    state = next(iter(network.store.workflows.values()))
    assert state["status"] == "booked"
    assert state["payment_reference"] == f"free:{state['workflow_id']}"
    assert network.store.payments == {}  # free rooms never create a payment or escrow
    assert network.store.escrow == {}
    # Only the school domain ever reached a workflow, event, cache entry, or message.
    stored = dumps(
        [network.store.workflows, network.store.events, network.store.cache, network.store.claims]
    )
    assert EMAIL_USER not in stored
    assert all(EMAIL_USER not in text for text in network.texts())


async def test_room_taken_during_recheck_goes_back_to_the_rooms(network) -> None:
    await network.send_user(chat(rooms_form()))
    _, carousel, _ = last_card(network, "carousel")
    await network.send_user(chat(carousel["items"][0]["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    network.use_http(booked={"102@12:00"})
    await network.send_user(chat(detail["ctas"][0]["selection"]))
    assert not [card for card in network.cards() if card[0] == "review"]
    assert any("just booked" in text for text in network.texts())
    assert last_card(network)[0] == "carousel"
    assert network.store.payments == {}


async def test_unknown_room_system_asks_for_the_link_then_checks_it(network) -> None:
    await network.send_user(chat(rooms_form("mit.edu")))
    _, ask, _ = last_card(network, "custom")
    assert ask["root"]["title"].startswith("Where does Massachusetts Institute")
    submit = ask["root"]["children"][-1]["children"][0]["action"]["selection"]
    assert submit["action"] == "submit_link"
    await network.send_user(chat({**submit, "booking_link": "https://evil.example.com/rooms"}))
    assert "https link" in last_card(network)[2]
    await network.send_user(
        chat({**submit, "booking_link": "https://libraries.mit.edu/study/reserve-a-room"})
    )
    _, carousel, _ = last_card(network, "carousel")
    assert carousel["items"][0]["badges"][-1]["label"] == "Link only"


def wellness_form(domain: str = "purdue.edu") -> dict:
    return {
        **rooms_form(domain, duration="30"),
        "service": "wellness",
        "group_size": "1",
    }


def wellness_view_buttons(card: dict) -> list[dict]:
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "button" and node["label"] == "View room":
                found.append(node["action"]["selection"])
            for child in node.get("children", []):
                walk(child)

    walk(card["root"])
    return found


async def test_wellness_flow_shows_crisis_rooms_and_events_then_a_link(network) -> None:
    await network.send_user(chat(wellness_form()))
    _, card, narration = last_card(network, "custom")
    assert card["root"]["title"] == "Health & wellness at Purdue University-Main Campus"
    assert "988" in narration
    encoded = json.dumps(card, ensure_ascii=False)
    assert encoded.index("988") < encoded.index("Wellness rooms open")
    assert "Free Flu Shot Clinic" in encoded and "Football" not in encoded
    assert "evil.example.com" not in encoded
    assert completed_roles(network) == [
        "intake",
        "researcher",
        "discovery",
        "verification",
        "analysis",
        "planner",
    ]
    (view,) = wellness_view_buttons(card)
    await network.send_user(chat(view))
    _, detail, _ = last_card(network, "detail")
    assert detail["title"] == "Meditation Room"
    assert detail["ctas"][0]["label"] == "Re-check this room"
    await network.send_user(chat(detail["ctas"][0]["selection"]))
    _, review, _ = last_card(network, "review")
    await network.send_user(chat(review["approve_cta"]["selection"]))
    _, receipt, _ = last_card(network, "detail")
    assert "https://calendar.lib.purdue.edu/space/104" in json.dumps(receipt)
    assert network.store.payments == {} and network.store.escrow == {}
    stored = dumps([network.store.workflows, network.store.events, network.store.cache])
    assert EMAIL_USER not in stored


async def test_wellness_without_rooms_or_events_still_answers_with_the_crisis_line(
    network,
) -> None:
    await network.send_user(chat(wellness_form("mit.edu")))
    _, card, _ = last_card(network, "custom")
    encoded = json.dumps(card, ensure_ascii=False)
    assert "988" in encoded
    assert "No public room-booking system found" in encoded
    assert "No public events calendar found" in encoded
    assert completed_roles(network) == ["intake", "researcher", "verification"]
    assert next(iter(network.store.workflows.values()))["status"] == "answered"


async def test_wellness_explains_a_libcal_without_wellness_rooms(network) -> None:
    await network.send_user(chat(wellness_form("ucla.edu")))
    _, card, _ = last_card(network, "custom")
    assert "No wellness rooms are listed" in json.dumps(card)
    assert wellness_view_buttons(card) == []
    assert next(iter(network.store.workflows.values()))["status"] == "awaiting_selection"


async def test_housing_flow_shows_facts_and_rechecked_links(network) -> None:
    await network.send_user(
        chat({"action": "submit_form", "service": "housing", "email": f"{EMAIL_USER}@purdue.edu"})
    )
    _, card, _ = last_card(network, "detail")
    assert card["title"] == "Housing at Purdue University-Main Campus · West Lafayette, IN"
    rows = {row["label"]: row["value"] for row in card["summary_rows"]}
    assert rows["Dorm beds"] == "16,056"
    assert rows["Housing office"] == "https://housing.purdue.edu/"
    assert (
        rows["Apply \u00b7 My Housing portal"]
        == "https://purdue.starrezhousing.com/StarRezPortalX/"
    )
    assert "Links re-checked live by Verification" in rows["Sources"]
    assert completed_roles(network) == ["intake", "researcher", "verification"]
    assert next(iter(network.store.workflows.values()))["status"] == "answered"


async def test_non_school_email_is_refused_with_a_card(network) -> None:
    await network.send_user(
        chat({"action": "submit_form", "service": "housing", "email": "someone@gmail.com"})
    )
    kind, _, narration = last_card(network)
    assert kind == "custom"
    assert "not a US college domain" in narration


async def test_specialists_and_sellers_answer_profile_chats(network) -> None:
    for key in ("verification", "researcher", "gear_share", "campus_rooms", "flash_prints"):
        await network.send_user(chat("hello"), to=key)
        kind, payload, _ = last_card(network)
        assert kind == "custom"
        buttons = [c for c in payload["root"]["children"] if c["type"] == "button"]
        assert buttons[0]["action"]["selection"]["action"] == "open_orchestrator"
    _, flash, _ = last_card(network)
    assert any(c.get("label") == "Unverified" for c in flash["root"]["children"])


async def test_forged_task_results_are_ignored(network) -> None:
    from resourcepilot.models import TaskResult

    await network.send_user(chat(example_bundle_selection()))
    state = next(iter(network.store.workflows.values()))
    forged = TaskResult(
        workflow_id=state["workflow_id"],
        request_id="forged",
        requester=state["requester"],
        session_id="s",
        source_role="planner",
        ok=False,
        error="pwned",
    )
    before = dict(state)
    await network.send_user(forged)
    assert network.store.workflows[state["workflow_id"]]["status"] == before["status"]
