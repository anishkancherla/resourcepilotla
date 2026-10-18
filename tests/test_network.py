from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from provider_fakes import SEARCH_DAY, dumps
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

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


async def open_first_room(network) -> tuple[dict, dict]:
    """Search Purdue, open the first room, and return (carousel, detail)."""
    await network.send_user(chat(rooms_form()))
    _, carousel, _ = last_card(network, "carousel")
    await network.send_user(chat(carousel["items"][0]["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    return carousel, detail


def recheck_cta(detail: dict) -> dict:
    return next(c for c in detail["ctas"] if c["selection"]["action"] == "recheck_room")


async def test_every_reply_is_a_card_and_free_text_gets_the_menu(network) -> None:
    await network.send_user(chat("hi"))
    kind, payload, _ = last_card(network)
    assert (kind, payload["root"]["title"]) == ("custom", "ResourcePilot campus services")
    menu = json.dumps(payload).lower()
    assert "bundle" not in menu and "payment" not in menu and "stripe" not in menu
    await network.send_user(chat("I need a DSLR Friday 3-6pm under $35"))
    _, menu_again, narration = last_card(network)
    assert menu_again["root"]["title"] == "ResourcePilot campus services"
    assert "buttons and forms" in narration
    assert network.store.workflows == {}  # a sentence never starts a request
    for service in ("rooms", "wellness", "housing"):
        await network.send_user(chat({"action": "open_service", "service": service}))
        assert last_card(network, "form")[1]["submit_cta"]["selection"]["service"] == service
    await network.send_user(chat({"action": "open_service", "service": "bundle"}))
    assert last_card(network)[1]["root"]["title"] == "ResourcePilot campus services"
    assert len(network.cards()) == len(network.texts())  # no plain-text reply anywhere


async def test_study_room_flow_ends_in_a_free_booking_link(network) -> None:
    carousel, detail = await open_first_room(network)
    assert completed_roles(network) == [
        "intake",
        "researcher",
        "campus_rooms",
        "verification",
        "planner",
    ]
    # Both rooms open at noon; the 2-seat room is the snuggest fit for a group of 2.
    assert [item["title"] for item in carousel["items"]] == ["WALC 3138", "HSSE 2101"]
    assert "Live · LibCal" in [badge["label"] for badge in carousel["items"][0]["badges"]]
    assert recheck_cta(detail)["label"] == "Re-check this room"
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    _, receipt, narration = last_card(network, "detail")
    assert receipt["title"] == "Your booking link"
    assert "https://calendar.lib.purdue.edu/space/102" in json.dumps(receipt)
    assert "did not reserve" in narration
    state = next(iter(network.store.workflows.values()))
    assert (state["status"], state["option"]["lines"][0]["name"]) == ("booked", "WALC 3138")
    assert network.dropped == []
    # Only the school domain ever reached a workflow, event, cache entry, or message.
    stored = dumps(
        [network.store.workflows, network.store.events, network.store.cache, network.store.claims]
    )
    assert EMAIL_USER not in stored
    assert all(EMAIL_USER not in text for text in network.texts())


async def test_my_bookings_lists_the_booked_room(network) -> None:
    _, detail = await open_first_room(network)
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    await network.send_user(chat({"action": "show_bookings"}))
    _, bookings, _ = last_card(network, "detail")
    assert "WALC 3138" in json.dumps(bookings)


async def test_room_taken_during_recheck_goes_back_to_the_other_rooms(network) -> None:
    _, detail = await open_first_room(network)
    network.use_http(booked={"102@12:00"})
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    assert any("just booked" in text for text in network.texts())
    kind, carousel, _ = last_card(network)
    assert kind == "carousel"
    assert [item["title"] for item in carousel["items"]] == ["HSSE 2101"]
    state = next(iter(network.store.workflows.values()))
    assert state["status"] == "awaiting_selection"
    assert state["data"]["exclude_items"] == ["libcal-9-7-102"]


async def test_a_double_click_rechecks_only_once(network) -> None:
    _, detail = await open_first_room(network)
    selection = recheck_cta(detail)["selection"]
    network.set_offline("campus_rooms")
    await network.send_user(chat(selection))
    await network.send_user(chat(selection))
    assert any("already being re-checked" in text for text in network.texts())


async def test_silent_recheck_returns_to_the_room_list(network) -> None:
    _, detail = await open_first_room(network)
    network.set_offline("campus_rooms")
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    await network.advance(31)
    _, _, narration = last_card(network, "carousel")
    assert "did not answer in time" in narration
    assert next(iter(network.store.workflows.values()))["status"] == "awaiting_selection"


async def test_silent_specialist_is_retried_once_then_reported(network) -> None:
    network.set_offline("planner")
    await network.send_user(chat(rooms_form()))
    await network.advance(31)
    state = next(iter(network.store.workflows.values()))
    assert (state["status"], state["retried_stages"]) == ("running", ["planner"])
    await network.advance(31)
    _, _, narration = last_card(network)
    assert "did not answer in time, even after a retry" in narration
    assert next(iter(network.store.workflows.values()))["status"] == "failed"


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
    return {**rooms_form(domain, duration="30"), "service": "wellness", "group_size": "1"}


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
        "campus_rooms",
        "verification",
        "planner",
    ]
    (view,) = wellness_view_buttons(card)
    await network.send_user(chat(view))
    _, detail, _ = last_card(network, "detail")
    assert detail["title"] == "Meditation Room"
    assert "988" in json.dumps(detail)
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    _, receipt, _ = last_card(network, "detail")
    assert receipt["title"] == "Your booking link"
    assert "https://calendar.lib.purdue.edu/space/104" in json.dumps(receipt)
    state = next(iter(network.store.workflows.values()))
    assert state["status"] == "booked"
    stored = dumps([network.store.workflows, network.store.events, network.store.cache])
    assert EMAIL_USER not in stored


async def test_wellness_room_taken_during_recheck_returns_to_the_wellness_card(network) -> None:
    await network.send_user(chat(wellness_form()))
    (view,) = wellness_view_buttons(last_card(network, "custom")[1])
    await network.send_user(chat(view))
    _, detail, _ = last_card(network, "detail")
    network.use_http(booked={"104@12:00"})
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    assert any("just booked" in text for text in network.texts())
    _, card, _ = last_card(network)
    assert card["root"]["title"].startswith("Health & wellness at")
    state = next(iter(network.store.workflows.values()))
    assert state["status"] == "awaiting_selection"


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


def housing_form(domain: str = "purdue.edu", **fields: str) -> dict:
    return {
        "action": "submit_form",
        "service": "housing",
        "email": f"{EMAIL_USER}@{domain}",
        **fields,
    }


async def test_housing_shows_each_hall_matched_to_the_criteria(network) -> None:
    await network.send_user(chat(housing_form(who="first_year", need_ac="true")))
    assert completed_roles(network) == ["intake", "researcher", "verification", "planner"]
    _, carousel, narration = last_card(network, "carousel")
    assert carousel["title"] == "On-campus housing at Purdue University-Main Campus"
    assert "matches first" in narration
    titles = [item["title"] for item in carousel["items"]]
    # Hillenbrand (upperclass) and Meredith South (graduate) are ruled out by their own pages.
    assert titles == ["Earhart", "Show all 3 halls", "Housing facts and how to apply"]
    earhart = carousel["items"][0]
    assert earhart["badges"][0] == {"label": "Matches", "variant": "success"}
    assert earhart["secondary_text"] == "Price not listed on the hall's page"

    await network.send_user(chat(earhart["primary_cta"]["selection"]))
    _, detail, _ = last_card(network, "detail")
    assert detail["title"] == "Earhart"
    rows = {row["label"]: row["value"] for row in detail["summary_rows"]}
    assert rows["Style"] == "Traditional hall"
    assert rows["Your criteria · Air conditioning"] == "Fits"
    assert rows["Apply"].startswith("https://purdue.starrezhousing.com/StarRezPortalX/")
    assert "never applies" in rows["Apply"]

    await network.send_user(chat(carousel["items"][1]["primary_cta"]["selection"]))
    _, everything, _ = last_card(network, "carousel")
    by_title = {item["title"]: item for item in everything["items"]}
    assert by_title["Hillenbrand Hall"]["badges"][0]["label"] == "Doesn't fit: Who it's for"
    assert (
        by_title["Hillenbrand Hall"]["image"] == "https://housing.purdue.edu/images/hillenbrand.jpg"
    )
    assert "From $4,512 per semester" in by_title["Hillenbrand Hall"]["secondary_text"]
    assert "image" not in by_title["Meredith South"]  # its photo was off the school's domain
    assert "attacker" not in json.dumps(network.cards())

    await network.send_user(chat(carousel["items"][-1]["primary_cta"]["selection"]))
    _, facts, _ = last_card(network, "detail")
    assert facts["title"].startswith("Housing at Purdue")
    assert next(iter(network.store.workflows.values()))["status"] == "answered"
    assert EMAIL_USER not in dumps([network.store.workflows, network.store.events])


async def test_school_without_readable_halls_gets_facts_and_links(network) -> None:
    await network.send_user(chat(housing_form("mit.edu")))
    _, card, _ = last_card(network, "detail")
    rows = {row["label"]: row["value"] for row in card["summary_rows"]}
    assert rows["Halls"].startswith("Couldn't read individual halls")
    assert completed_roles(network) == ["intake", "researcher", "verification", "planner"]


async def test_housing_flow_shows_facts_and_rechecked_links(network) -> None:
    await network.send_user(chat(housing_form()))
    _, carousel, _ = last_card(network, "carousel")
    await network.send_user(chat(carousel["items"][-1]["primary_cta"]["selection"]))
    _, card, _ = last_card(network, "detail")
    assert card["title"] == "Housing at Purdue University-Main Campus · West Lafayette, IN"
    rows = {row["label"]: row["value"] for row in card["summary_rows"]}
    assert rows["Dorm beds"] == "16,056"
    assert rows["Housing office"] == "https://housing.purdue.edu/"
    assert rows["Apply · My Housing portal"] == "https://purdue.starrezhousing.com/StarRezPortalX/"
    assert "Links re-checked live by Verification" in rows["Sources"]
    assert completed_roles(network) == ["intake", "researcher", "verification", "planner"]
    assert next(iter(network.store.workflows.values()))["status"] == "answered"


async def test_non_school_email_is_refused_with_a_card(network) -> None:
    await network.send_user(
        chat({"action": "submit_form", "service": "housing", "email": "someone@gmail.com"})
    )
    kind, _, narration = last_card(network)
    assert kind == "custom"
    assert "not a US college domain" in narration


async def test_specialists_answer_profile_chats(network) -> None:
    for key in ("intake", "researcher", "campus_rooms", "verification", "planner"):
        await network.send_user(chat("hello"), to=key)
        kind, payload, _ = last_card(network)
        assert kind == "custom"
        buttons = [c for c in payload["root"]["children"] if c["type"] == "button"]
        assert buttons[0]["action"]["selection"]["action"] == "open_orchestrator"


async def test_forged_task_results_are_ignored(network) -> None:
    from resourcepilot.models import TaskResult

    await network.send_user(chat(rooms_form()))
    state = next(iter(network.store.workflows.values()))
    forged = TaskResult(
        workflow_id=state["workflow_id"],
        request_id="forged",
        requester=state["requester"],
        session_id=state["session_id"],
        source_role="planner",
        ok=False,
        error="pwned",
    )
    before = dict(state)
    await network.send_user(forged)
    assert network.store.workflows[state["workflow_id"]]["status"] == before["status"]


async def test_room_flow_on_postgres(postgres_network) -> None:
    from network import USER

    network = postgres_network
    _, detail = await open_first_room(network)
    await network.send_user(chat(recheck_cta(detail)["selection"]))
    assert last_card(network, "detail")[1]["title"] == "Your booking link"
    bookings = await network.store.recent_bookings(USER, 5)
    assert [booking["option"]["lines"][0]["name"] for booking in bookings] == ["WALC 3138"]


def emergency_setup(network, monkeypatch, **fake_options):
    """Configure the demo call and serve ElevenLabs from a recording fake; no call is real."""
    import httpx
    from test_emergency import ENV, FakeElevenLabs

    from resourcepilot.http import Http

    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    fake = FakeElevenLabs(**fake_options)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    monkeypatch.setattr(network.by_key["emergency"], "HTTP", Http(client, network.store))
    return fake


EMERGENCY_FORM = {
    "action": "submit_form",
    "service": "emergency",
    "kind": "medical",
    "location": "Powell Library, 2nd floor",
    "people": "1",
    "details": f"Student fainted. Email {EMAIL_USER}@ucla.edu",
    "caller_safe": "yes",
    "school": "UCLA",
    # A number in the form is never dialed; only EMERGENCY_DEMO_TO_NUMBER is.
    "to_number": "+19495550199",
}


def outbound(fake) -> list:
    return [request for request in fake.requests if request.method == "POST"]


async def test_emergency_drill_reviews_then_places_exactly_one_call(network, monkeypatch) -> None:
    from test_emergency import API_KEY, DEMO_NUMBER

    fake = emergency_setup(network, monkeypatch)
    await network.send_user(chat({"action": "open_service", "service": "emergency"}))
    assert "never 911" in last_card(network, "form")[1]["title"]
    await network.send_user(chat(EMERGENCY_FORM))
    _, review, narration = last_card(network, "review")
    assert completed_roles(network) == ["emergency"]
    assert fake.requests == []  # nothing is dialed before Call now
    assert "never 911" in narration and "0142" in narration
    assert review["approve_cta"]["label"] == "Call now"
    await network.send_user(chat(review["approve_cta"]["selection"]))
    _, call, narration = last_card(network, "custom")
    assert call["root"]["title"] == "Demo call" and "speaker" in narration
    (placed,) = outbound(fake)
    assert json.loads(placed.content)["to_number"] == DEMO_NUMBER
    await network.send_user(chat(review["approve_cta"]["selection"]))  # a second press
    assert last_card(network, "custom")[1]["root"]["title"] == "Already placed"
    assert len(outbound(fake)) == 1
    check = next(
        button["action"]["selection"]
        for button in call["root"]["children"][-1]["children"]
        if button["label"] == "Check the call"
    )
    await network.send_user(chat(check))
    _, checked, _ = last_card(network, "custom")
    encoded = json.dumps(checked, ensure_ascii=False)
    assert "What is your location?" in encoded and "1:15" in encoded
    assert next(iter(network.store.workflows.values()))["status"] == "called"
    stored = dumps([network.store.workflows, network.store.events, network.store.claims])
    for secret in (API_KEY, DEMO_NUMBER, "5550142", EMAIL_USER, "+19495550199"):
        assert secret not in stored
        assert all(secret not in text for text in network.texts())


async def test_a_lost_demo_call_answer_is_never_retried(network, monkeypatch) -> None:
    fake = emergency_setup(network, monkeypatch)
    await network.send_user(chat(EMERGENCY_FORM))
    _, review, _ = last_card(network, "review")
    network.set_offline("emergency")
    await network.send_user(chat(review["approve_cta"]["selection"]))
    await network.advance(31)
    await network.advance(31)
    assert any("never retried" in text for text in network.texts())
    assert next(iter(network.store.workflows.values()))["status"] == "failed"
    network.set_offline("emergency", False)
    await network.send_user(chat(review["approve_cta"]["selection"]))
    assert last_card(network, "custom")[1]["root"]["title"] == "Already placed"
    assert outbound(fake) == []


async def test_emergency_without_setup_explains_and_calls_no_one(network, monkeypatch) -> None:
    for key in ("ELEVENLABS_API_KEY", "EMERGENCY_DEMO_TO_NUMBER"):
        monkeypatch.delenv(key, raising=False)
    await network.send_user(chat(EMERGENCY_FORM))
    assert any("not set up yet" in text for text in network.texts())
    assert next(iter(network.store.workflows.values()))["status"] == "failed"
