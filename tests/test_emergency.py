from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from resourcepilot.emergency import (
    ELEVENLABS_API,
    OUTBOUND_CALL_PATH,
    DemoCallConfig,
    blocked_reason,
    call_preview,
    call_status,
    dynamic_variables,
    emergency_brief,
    emergency_handler,
    first_message,
    load_config,
    mask_number,
    normalize_number,
    place_demo_call,
)
from resourcepilot.http import Http, ProviderError
from resourcepilot.store import MemoryStore

DEMO_NUMBER = "+13105550142"
API_KEY = "test-elevenlabs-key"
ENV = {
    "ELEVENLABS_API_KEY": API_KEY,
    "ELEVENLABS_AGENT_ID": "agent_demo",
    "ELEVENLABS_PHONE_NUMBER_ID": "phnum_demo",
    "EMERGENCY_DEMO_TO_NUMBER": "(310) 555-0142",
}
FORM = {
    "kind": "medical",
    "location": "Powell Library, 2nd floor",
    "people": "1",
    "details": "Student fainted and is breathing. Email me at pat.lee@ucla.edu",
    "caller_safe": "yes",
}


def config() -> DemoCallConfig:
    return load_config(ENV)


class FakeElevenLabs:
    def __init__(self, *, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.body is not None:
            return httpx.Response(self.status, json=self.body)
        if request.url.path == OUTBOUND_CALL_PATH:
            return httpx.Response(
                self.status,
                json={
                    "success": True,
                    "message": "ok",
                    "conversation_id": "conv_123",
                    "callSid": "CA123",
                },
            )
        return httpx.Response(
            self.status,
            json={
                "status": "done",
                "metadata": {"call_duration_secs": 75},
                "analysis": {"transcript_summary": "Reported a fainting at Powell Library."},
                "transcript": [
                    {"role": "agent", "message": "This is a ResourcePilot demo drill."},
                    {"role": "user", "message": "What is your location?"},
                    {"role": "user", "message": ""},
                ],
            },
        )

    def http(self) -> Http:
        return Http(httpx.AsyncClient(transport=httpx.MockTransport(self)))


@pytest.mark.parametrize(
    ("raw", "number"),
    [
        ("(310) 555-0142", "+13105550142"),
        ("1-310-555-0142", "+13105550142"),
        ("+44 20 7946 0958", "+442079460958"),
    ],
)
def test_normalize_number_returns_e164(raw: str, number: str) -> None:
    assert normalize_number(raw) == number


@pytest.mark.parametrize("raw", ["", "call me", "555-0142", "+1 310 555 0142 999 999"])
def test_normalize_number_rejects_junk(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_number(raw)


@pytest.mark.parametrize(
    "number",
    [
        "+911",
        "+1911",
        "+112",
        "+999",
        "+988",
        "+19115550142",
        "+12125110142",
        "+18002738255",
        "+18002221222",
        "+13101115555",
        "+11105550142",
    ],
)
def test_emergency_and_crisis_numbers_are_never_dialable(number: str) -> None:
    assert blocked_reason(number)


def test_ordinary_numbers_are_dialable() -> None:
    assert blocked_reason("+13105550142") is None
    assert blocked_reason("+442079460958") is None


@pytest.mark.parametrize("number", ["911", "1 911", "988", "+18002738255", "(212) 911-0000"])
def test_config_refuses_emergency_destinations(number: str) -> None:
    with pytest.raises(ValueError):
        load_config({**ENV, "EMERGENCY_DEMO_TO_NUMBER": number})


def test_config_names_missing_variables_without_values() -> None:
    with pytest.raises(ValueError) as error:
        load_config({"ELEVENLABS_API_KEY": API_KEY})
    message = str(error.value)
    assert "ELEVENLABS_AGENT_ID" in message and "EMERGENCY_DEMO_TO_NUMBER" in message
    assert API_KEY not in message


def test_config_only_talks_to_elevenlabs() -> None:
    with pytest.raises(ValueError):
        load_config({**ENV, "ELEVENLABS_API_BASE": "https://evil.example.com"})
    assert load_config({**ENV, "ELEVENLABS_API_BASE": "https://api.us.elevenlabs.io"}).api_base


def test_config_repr_hides_key_and_number() -> None:
    text = repr(config()) + str(config())
    assert API_KEY not in text and DEMO_NUMBER not in text
    assert mask_number(DEMO_NUMBER).endswith("0142")


def test_brief_scrubs_emails_and_caps_text() -> None:
    brief = emergency_brief({**FORM, "location": "x" * 500}, school="UCLA")
    assert "pat.lee" not in json.dumps(brief) and "[email removed]" in brief["details"]
    assert len(brief["location"]) <= 120
    assert brief["school"] == "UCLA" and brief["people"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "prank"},
        {"location": "   "},
        {"people": "zero"},
        {"people": "0"},
        {"people": "500"},
        {"caller_safe": "maybe"},
    ],
)
def test_brief_rejects_bad_forms(change: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        emergency_brief({**FORM, **change})


def test_voice_always_opens_with_the_demo_notice() -> None:
    for kind in ("medical", "fire", "danger", "other"):
        opening = first_message(emergency_brief({**FORM, "kind": kind}))
        assert opening.startswith(
            "Hello, this is a ResourcePilot demo drill, not a real emergency."
        )
    assert all(
        isinstance(value, str) for value in dynamic_variables(emergency_brief(FORM)).values()
    )


def test_preview_says_it_is_not_911_and_masks_the_number() -> None:
    preview = call_preview(emergency_brief(FORM), config())
    calling = dict(preview["rows"])["Calling"]
    assert "not 911" in calling and DEMO_NUMBER not in json.dumps(preview)


async def test_place_call_dials_only_the_configured_number() -> None:
    fake = FakeElevenLabs()
    call = await place_demo_call(fake.http(), config(), emergency_brief(FORM))
    assert call["conversation_id"] == "conv_123" and call["status"] == "calling"
    (request,) = fake.requests
    body = json.loads(request.content)
    assert str(request.url) == ELEVENLABS_API + OUTBOUND_CALL_PATH
    assert request.headers["xi-api-key"] == API_KEY
    assert body["to_number"] == DEMO_NUMBER
    client_data = body["conversation_initiation_client_data"]
    assert client_data["dynamic_variables"]["location"] == "Powell Library, 2nd floor"
    assert "demo drill" in client_data["conversation_config_override"]["agent"]["first_message"]
    assert DEMO_NUMBER not in json.dumps(call)


@pytest.mark.parametrize(
    ("status", "body", "fragment"),
    [
        (401, {"detail": "bad key test-elevenlabs-key"}, "API key"),
        (422, {"detail": []}, "call settings"),
        (429, {}, "rate limiting"),
        (500, {}, "error (500)"),
        (200, {"success": False, "message": "no"}, "could not start"),
    ],
)
async def test_place_call_errors_are_card_safe(status: int, body: Any, fragment: str) -> None:
    fake = FakeElevenLabs(status=status, body=body)
    with pytest.raises(ProviderError) as error:
        await place_demo_call(fake.http(), config(), emergency_brief(FORM))
    assert fragment in str(error.value) and API_KEY not in str(error.value)
    assert len(fake.requests) == 1


async def test_call_status_reads_transcript_and_summary() -> None:
    status = await call_status(FakeElevenLabs().http(), config(), "conv_123")
    assert status["finished"] and status["duration"] == "1:15"
    assert status["summary"].startswith("Reported a fainting")
    assert [line["speaker"] for line in status["transcript"]] == [
        "ResourcePilot voice",
        "Dispatcher",
    ]


@pytest.mark.parametrize("conversation_id", ["", "../voices", "conv 1", "a/b"])
async def test_call_status_rejects_unsafe_ids(conversation_id: str) -> None:
    fake = FakeElevenLabs()
    with pytest.raises(ValueError):
        await call_status(fake.http(), config(), conversation_id)
    assert not fake.requests


async def test_handler_ignores_numbers_in_the_payload() -> None:
    fake = FakeElevenLabs()
    payload = {
        "phase": "call",
        "workflow_id": "wf_1",
        "brief": FORM,
        "to_number": "+19115550000",
        "call": {"to_number": "911"},
    }
    result = await emergency_handler(payload, fake.http(), MemoryStore(), config=config())
    assert result["call"]["conversation_id"] == "conv_123"
    assert json.loads(fake.requests[0].content)["to_number"] == DEMO_NUMBER


async def test_handler_places_one_call_per_workflow() -> None:
    fake = FakeElevenLabs()
    store = MemoryStore()
    payload = {"phase": "call", "workflow_id": "wf_1", "brief": FORM}
    first = await emergency_handler(payload, fake.http(), store, config=config())
    second = await emergency_handler(payload, fake.http(), store, config=config())
    assert second["duplicate"] and second["call"] == first["call"]
    assert len(fake.requests) == 1


async def test_handler_never_retries_a_failed_call() -> None:
    fake = FakeElevenLabs(status=500, body={})
    store = MemoryStore()
    payload = {"phase": "call", "workflow_id": "wf_1", "brief": FORM}
    with pytest.raises(ProviderError):
        await emergency_handler(payload, fake.http(), store, config=config())
    with pytest.raises(ValueError):
        await emergency_handler(payload, fake.http(), store, config=config())
    assert len(fake.requests) == 1


async def test_handler_preview_and_status_place_no_call() -> None:
    fake = FakeElevenLabs()
    store = MemoryStore()
    preview = await emergency_handler(
        {"phase": "preview", "brief": FORM, "school_name": "UCLA"},
        fake.http(),
        store,
        config=config(),
    )
    assert preview["preview"]["opening"].startswith("Hello, this is a ResourcePilot demo drill")
    assert not fake.requests
    status = await emergency_handler(
        {"phase": "status", "call": {"conversation_id": "conv_123"}},
        fake.http(),
        store,
        config=config(),
    )
    assert status["call_status"]["status"] == "done"
    assert all(request.method == "GET" for request in fake.requests)


async def test_handler_requires_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError):
        await emergency_handler(
            {"phase": "preview", "brief": FORM},
            FakeElevenLabs().http(),
            MemoryStore(),
            config=None,
        )
