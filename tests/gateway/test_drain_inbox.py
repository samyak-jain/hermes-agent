import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.drain_inbox import DrainInbox
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _event(
    *,
    profile: str = "default",
    message_id: str = "message-1",
    ambient: bool = False,
) -> MessageEvent:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="roundtable",
        chat_type="group",
        user_id="operator",
        user_name="bonsai",
        profile=profile,
        message_id=message_id,
    )
    return MessageEvent(
        text="please continue after maintenance",
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
        reply_to_message_id="previous",
        reply_to_text="context",
        channel_context="recent room context",
        metadata={"ambient_room_id": "roundtable"} if ambient else {},
        timestamp=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
    )


class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, *args, **kwargs):
        return SendResult(success=True, message_id="ack")


def test_drain_inbox_round_trips_normalized_event_across_instances(tmp_path):
    path = tmp_path / "drain.db"
    first = DrainInbox(path)
    inserted, acknowledged = first.enqueue(_event(profile="vegapunk"))
    assert (inserted, acknowledged) == (True, True)

    restored = DrainInbox(path).pending()
    assert len(restored) == 1
    event = restored[0].event
    assert event.text == "please continue after maintenance"
    assert event.source.profile == "vegapunk"
    assert event.source.user_id == "operator"
    assert event.reply_to_message_id == "previous"
    assert event.channel_context == "recent room context"
    assert event.timestamp == datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def test_shared_room_profiles_are_distinct_but_acknowledged_once(tmp_path):
    store = DrainInbox(tmp_path / "drain.db")

    assert store.enqueue(_event(profile="default", ambient=True)) == (True, True)
    assert store.enqueue(_event(profile="vegapunk", ambient=True)) == (True, False)
    assert store.enqueue(_event(profile="vegapunk", ambient=True)) == (False, False)
    assert store.count() == 2


@pytest.mark.asyncio
async def test_ambient_queue_acknowledges_with_one_reaction_not_bot_text(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner._drain_inbox_store = DrainInbox(tmp_path / "drain.db")
    event = _event(ambient=True)
    event.raw_message = SimpleNamespace(add_reaction=AsyncMock())

    result = await runner._queue_external_drain_event(event)

    assert result is None
    event.raw_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_active_session_event_is_durable_before_in_memory_followup_queue():
    adapter = _Adapter(
        PlatformConfig(enabled=True, token="test"),
        Platform.DISCORD,
    )
    adapter._message_handler = AsyncMock(return_value=None)
    adapter._drain_message_handler = AsyncMock(return_value=(True, None))
    event = _event()
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(event)

    adapter._drain_message_handler.assert_awaited_once_with(event)
    adapter._message_handler.assert_not_awaited()
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_replay_waits_for_turn_then_deletes_durable_row(tmp_path):
    runner = object.__new__(GatewayRunner)
    store = DrainInbox(tmp_path / "drain.db")
    store.enqueue(_event(profile="vegapunk"))
    runner._drain_inbox_store = store
    runner._external_drain_active = False
    runner._draining = False
    received = []

    class Adapter:
        def __init__(self):
            self.config = SimpleNamespace(
                extra={
                    "group_sessions_per_user": True,
                    "thread_sessions_per_user": False,
                }
            )
            self._session_tasks = {}
            self._pending_messages = {}

        async def handle_message(self, event):
            key = build_session_key(
                event.source,
                group_sessions_per_user=True,
                thread_sessions_per_user=False,
            )

            async def run():
                await asyncio.sleep(0.01)
                received.append(event)

            task = asyncio.create_task(run())
            self._session_tasks[key] = task

            def cleanup(done):
                if self._session_tasks.get(key) is done:
                    self._session_tasks.pop(key, None)

            task.add_done_callback(cleanup)

    adapter = Adapter()
    runner._adapter_for_source = lambda _source: adapter

    assert await runner._replay_external_drain_inbox() == 1
    assert len(received) == 1
    assert received[0].source.profile == "vegapunk"
    assert store.count() == 0
