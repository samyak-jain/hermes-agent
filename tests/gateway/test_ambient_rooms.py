import asyncio
from types import SimpleNamespace

import pytest

from gateway.ambient_rooms import AmbientProvenanceStore, AmbientRoomArbiter
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_arbiter_selects_one_highest_scoring_profile():
    arbiter = AmbientRoomArbiter()
    results = await asyncio.gather(
        arbiter.choose(
            message_id="m1",
            profile="default",
            participants=["default", "vegapunk"],
            score=0.61,
            decision_window_seconds=0.2,
        ),
        arbiter.choose(
            message_id="m1",
            profile="vegapunk",
            participants=["default", "vegapunk"],
            score=0.91,
            decision_window_seconds=0.2,
        ),
    )
    assert results == [False, True]


@pytest.mark.asyncio
async def test_arbiter_allows_every_profile_to_observe_quietly():
    arbiter = AmbientRoomArbiter()
    results = await asyncio.gather(
        arbiter.choose(
            message_id="m2",
            profile="default",
            participants=["default", "vegapunk"],
            score=0,
            decision_window_seconds=0.2,
        ),
        arbiter.choose(
            message_id="m2",
            profile="vegapunk",
            participants=["default", "vegapunk"],
            score=0,
            decision_window_seconds=0.2,
        ),
    )
    assert results == [False, False]


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
