from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

from resourcepilot.cards import EXAMPLE_REQUEST


def chat(text: str | dict) -> ChatMessage:
    body = text if isinstance(text, str) else json.dumps(text)
    return ChatMessage(
        timestamp=datetime.now(UTC), msg_id=uuid4(), content=[TextContent(type="text", text=body)]
    )


def last_card(network, kind: str | None = None) -> tuple[str, dict, str]:
    found = [card for card in network.cards() if kind is None or card[0] == kind]
    assert found, f"no {kind or 'card'} received; texts: {network.texts()}"
    return found[-1]


async def test_welcome_and_form_cards(network) -> None:
    await network.send_user(chat("hi"))
    kind, payload, _ = last_card(network)
    assert kind == "custom"
    assert payload["root"]["title"] == "ResourcePilot"
    await network.send_user(chat({"action": "open_form"}))
    assert last_card(network)[0] == "form"


async def test_sentence_runs_the_whole_planning_network(network) -> None:
    await network.send_user(chat(EXAMPLE_REQUEST))
    _, payload, narration = last_card(network, "carousel")
    assert payload["items"], narration
    assert "Flash Prints 4U" in narration
    assert "Lowest price" in [item["title"] for item in payload["items"]]
    assert network.dropped == []

    events = next(iter(network.store.events.values()))
    roles = [e["details"].get("role") for e in events if e["event_type"] == "specialist_completed"]
    assert roles == ["intake", "discovery", "verification", "analysis", "planner"]
    assert network.store.stats["gear_share"]["quotes_sent"] == 1
    assert network.store.preferences  # Memory/Audit saved the buyer's preferences


async def test_bundle_detail_and_vetting_cards(network) -> None:
    await network.send_user(chat(EXAMPLE_REQUEST))
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
    await network.send_user(chat(EXAMPLE_REQUEST))
    assert not [card for card in network.cards() if card[0] == "carousel"]
    await network.advance(6)
    narration = last_card(network, "carousel")[2]
    assert "podcast_room" in narration
    assert network.store.stats["studio_rooms"]["no_responses"] == 1


async def test_incomplete_sentence_asks_with_a_form(network) -> None:
    await network.send_user(chat("I need some posters and a camera"))
    kind, _, narration = last_card(network)
    assert kind == "form"
    assert "Missing" in narration


async def test_specialists_and_sellers_answer_profile_chats(network) -> None:
    for key in ("verification", "gear_share", "flash_prints"):
        await network.send_user(chat("hello"), to=key)
        kind, payload, _ = last_card(network)
        assert kind == "custom"
        buttons = [c for c in payload["root"]["children"] if c["type"] == "button"]
        assert buttons[0]["action"]["selection"]["action"] == "open_orchestrator"
    _, flash, _ = last_card(network)
    assert any(c.get("label") == "Unverified" for c in flash["root"]["children"])


async def test_forged_task_results_are_ignored(network) -> None:
    from resourcepilot.models import TaskResult

    await network.send_user(chat(EXAMPLE_REQUEST))
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
