from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.ambient_rooms import (
    AmbientParticipationDecision,
    AmbientProvenanceStore,
    _parse_ambient_decision,
    decide_ambient_participation,
)
from gateway.run import GatewayRunner


def test_profile_decision_parses_reply_react_and_silence():
    assert _parse_ambient_decision(
        '{"action":"reply","reaction":"","confidence":0.87}'
    ) == AmbientParticipationDecision("reply", 0.87, "")
    assert _parse_ambient_decision(
        '{"action":"react","reaction":"👍","confidence":0.72}'
    ) == AmbientParticipationDecision("react", 0.72, "👍")
    assert _parse_ambient_decision(
        '{"action":"silent","reaction":"","confidence":0.91}'
    ) == AmbientParticipationDecision("silent", 0.91, "")


@pytest.mark.asyncio
async def test_app_server_profile_decision_uses_subscription_runtime(monkeypatch):
    captured = {}

    def call_app_server(**kwargs):
        captured.update(kwargs)
        return '{"action":"react","reaction":"👀","confidence":0.8}'

    monkeypatch.setattr(
        "gateway.ambient_rooms._call_app_server_ambient_decision",
        call_app_server,
    )

    result = await decide_ambient_participation(
        profile="default",
        role="personal companion",
        trigger_text="Take a look at this",
        channel_context="[bonsai] Take a look at this",
        author_is_bot=False,
        main_runtime={
            "provider": "anthropic",
            "model": "claude-fable-5",
            "api_mode": "codex_app_server",
        },
    )

    assert result == AmbientParticipationDecision("react", 0.8, "👀")
    assert captured["main_runtime"]["model"] == "claude-fable-5"
    assert "Choose exactly one action" in captured["system_prompt"]


def test_provenance_round_trips_hop_metadata(tmp_path):
    store = AmbientProvenanceStore(tmp_path / "ambient.db")
    store.record(
        message_id="reply-2",
        room_id="room",
        root_message_id="human-1",
        hop=2,
        profile="vegapunk",
    )
    assert store.lookup("reply-2") == {
        "room_id": "room",
        "root_message_id": "human-1",
        "hop": 2,
        "profile": "vegapunk",
    }


@pytest.mark.asyncio
async def test_direct_ambient_turn_is_atomic_and_gets_room_contract():
    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(profile="vegapunk", is_bot=False)
    event = SimpleNamespace(
        source=source,
        message_id="human-1",
        text="@Vegapunk take this",
        channel_context="",
        channel_prompt="existing room rule",
        metadata={
            "ambient_direct": True,
            "ambient_participants": ["default", "vegapunk"],
            "ambient_profile_role": "coding operator",
        },
    )

    assert await runner._admit_ambient_room_turn(event) is True
    assert source._ambient_room_event is True
    assert "existing room rule" in event.channel_prompt
    assert "coding operator" in event.channel_prompt


@pytest.mark.asyncio
async def test_admitted_ambient_turn_starts_typing_after_attention_gate():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(send_typing=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    runner._thread_metadata_for_source = lambda _source, message_id: {
        "reply_to_message_id": message_id
    }
    source = SimpleNamespace(
        profile="vegapunk",
        is_bot=False,
        chat_id="room",
    )
    event = SimpleNamespace(
        source=source,
        message_id="human-typing",
        text="@Vegapunk hello",
        channel_context="",
        channel_prompt="",
        metadata={
            "ambient_direct": True,
            "ambient_participants": ["default", "vegapunk"],
            "ambient_profile_role": "coding operator",
        },
    )

    assert await runner._admit_ambient_room_turn(event) is True
    adapter.send_typing.assert_awaited_once_with(
        "room",
        metadata={"reply_to_message_id": "human-typing"},
    )


@pytest.mark.asyncio
async def test_profile_model_independently_selects_reply(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(send_typing=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    runner._thread_metadata_for_source = lambda _source, message_id: {
        "reply_to_message_id": message_id
    }
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "gpt-5.6-sol",
        {"provider": "openai-codex", "api_mode": "codex_responses"},
    )
    source = SimpleNamespace(
        profile="vegapunk",
        is_bot=False,
        chat_id="room",
    )
    event = SimpleNamespace(
        source=source,
        raw_message=SimpleNamespace(add_reaction=AsyncMock()),
        message_id="human-model-choice",
        text="Anyone have thoughts?",
        channel_context="[bonsai] Anyone have thoughts?",
        channel_prompt="",
        metadata={
            "ambient_direct": False,
            "ambient_other_bot_mentioned": False,
            "ambient_profile_role": "coding operator",
            "ambient_room_id": "room",
        },
    )
    captured = {}

    async def decide(**kwargs):
        captured.update(kwargs)
        return AmbientParticipationDecision("reply", 0.84)

    monkeypatch.setattr(
        "gateway.ambient_rooms.decide_ambient_participation",
        decide,
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    assert await runner._admit_ambient_room_turn(event) is True
    assert captured["profile"] == "vegapunk"
    assert captured["main_runtime"] == {
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "model": "gpt-5.6-sol",
    }
    adapter.send_typing.assert_awaited_once()
    assert "selected a text reply" in event.channel_prompt


@pytest.mark.asyncio
async def test_profile_model_can_react_without_starting_agent(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(send_typing=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "claude-fable-5",
        {"provider": "anthropic", "api_mode": "codex_app_server"},
    )
    reaction = AsyncMock()
    event = SimpleNamespace(
        source=SimpleNamespace(
            profile="default",
            is_bot=False,
            chat_id="room",
        ),
        raw_message=SimpleNamespace(add_reaction=reaction),
        message_id="human-reaction",
        text="Shipped!",
        channel_context="",
        channel_prompt="",
        metadata={
            "ambient_direct": False,
            "ambient_other_bot_mentioned": False,
            "ambient_profile_role": "personal companion",
            "ambient_room_id": "room",
        },
    )

    async def decide(**_kwargs):
        return AmbientParticipationDecision("react", 0.93, "🎉")

    monkeypatch.setattr(
        "gateway.ambient_rooms.decide_ambient_participation",
        decide,
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    assert await runner._admit_ambient_room_turn(event) is False
    reaction.assert_awaited_once_with("🎉")
    adapter.send_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_model_can_stay_silent(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(send_typing=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "claude-fable-5",
        {"provider": "anthropic", "api_mode": "codex_app_server"},
    )
    event = SimpleNamespace(
        source=SimpleNamespace(
            profile="default",
            is_bot=False,
            chat_id="room",
        ),
        raw_message=SimpleNamespace(add_reaction=AsyncMock()),
        message_id="human-silence",
        text="Vegapunk, this one is really for you",
        channel_context="",
        channel_prompt="",
        metadata={
            "ambient_direct": False,
            "ambient_other_bot_mentioned": False,
            "ambient_profile_role": "personal companion",
            "ambient_room_id": "room",
        },
    )

    async def decide(**_kwargs):
        return AmbientParticipationDecision("silent", 0.95)

    monkeypatch.setattr(
        "gateway.ambient_rooms.decide_ambient_participation",
        decide,
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    assert await runner._admit_ambient_room_turn(event) is False
    event.raw_message.add_reaction.assert_not_awaited()
    adapter.send_typing.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_target_profile_stays_quiet_on_explicit_bot_mention():
    runner = object.__new__(GatewayRunner)
    event = SimpleNamespace(
        source=SimpleNamespace(profile="default", is_bot=False),
        message_id="human-2",
        text="@Vegapunk take this",
        channel_context="",
        channel_prompt="",
        metadata={
            "ambient_direct": False,
            "ambient_other_bot_mentioned": True,
            "ambient_participants": ["default", "vegapunk"],
        },
    )

    assert await runner._admit_ambient_room_turn(event) is False


@pytest.mark.asyncio
async def test_agent_cascade_stops_at_configured_hop_limit():
    runner = object.__new__(GatewayRunner)
    event = SimpleNamespace(
        source=SimpleNamespace(profile="vegapunk", is_bot=True),
        message_id="agent-3",
        text="bot follow-up",
        channel_context="",
        channel_prompt="",
        metadata={
            "ambient_direct": True,
            "ambient_other_bot_mentioned": False,
            "ambient_participants": ["default", "vegapunk"],
            "ambient_hop": 3,
            "ambient_max_hops": 3,
            "ambient_room_id": "room",
        },
    )

    assert await runner._admit_ambient_room_turn(event) is False
