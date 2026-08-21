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
    profile: str | None = "default",
    message_id: str = "message-1",
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
        metadata={},
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


def test_profiles_are_distinct_but_message_is_acknowledged_once(tmp_path):
    store = DrainInbox(tmp_path / "drain.db")

    assert store.enqueue(_event(profile="default")) == (True, True)
    assert store.enqueue(_event(profile="vegapunk")) == (True, False)
    assert store.enqueue(_event(profile="vegapunk")) == (False, False)
    assert store.count() == 2


@pytest.mark.asyncio
async def test_active_session_event_is_durable_before_in_memory_followup_queue():
    adapter = _Adapter(
        PlatformConfig(enabled=True, token="test"),
        Platform.DISCORD,
    )
    adapter._message_handler = AsyncMock(return_value=None)
    captured_profiles = []

    async def drain_handler(event):
        captured_profiles.append(event.source.profile)
        return True, None

    adapter._drain_message_handler = AsyncMock(side_effect=drain_handler)
    adapter._hermes_profile_name = "vegapunk"
    event = _event(profile=None)
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(event)

    adapter._drain_message_handler.assert_awaited_once_with(event)
    adapter._message_handler.assert_not_awaited()
    assert captured_profiles == ["vegapunk"]
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


@pytest.mark.asyncio
async def test_replay_prepares_adapter_before_dispatch_and_delete(tmp_path):
    runner = object.__new__(GatewayRunner)
    store = DrainInbox(tmp_path / "drain.db")
    store.enqueue(_event(profile="vegapunk"))
    runner._drain_inbox_store = store
    runner._external_drain_active = False
    runner._draining = False
    calls = []

    class Adapter:
        config = SimpleNamespace(
            extra={
                "group_sessions_per_user": True,
                "thread_sessions_per_user": False,
            }
        )
        _session_tasks = {}
        _pending_messages = {}

        async def _prepare_external_drain_replay(self, event):
            calls.append(("prepare", event.message_id))
            return True

        async def handle_message(self, event):
            calls.append(("dispatch", event.message_id))

    runner._adapter_for_source = lambda _source: Adapter()

    assert await runner._replay_external_drain_inbox() == 1
    assert calls == [
        ("prepare", "message-1"),
        ("dispatch", "message-1"),
    ]
    assert store.count() == 0


@pytest.mark.asyncio
async def test_replay_keeps_row_when_adapter_cannot_prepare(tmp_path):
    runner = object.__new__(GatewayRunner)
    store = DrainInbox(tmp_path / "drain.db")
    store.enqueue(_event())
    runner._drain_inbox_store = store
    runner._external_drain_active = False
    runner._draining = False

    class Adapter:
        config = SimpleNamespace(extra={})
        _session_tasks = {}
        _pending_messages = {}

        async def _prepare_external_drain_replay(self, _event):
            return False

        async def handle_message(self, _event):
            raise AssertionError("unprepared replay was dispatched")

    runner._adapter_for_source = lambda _source: Adapter()

    assert await runner._replay_external_drain_inbox() == 0
    assert store.count() == 1


def test_malformed_drain_row_is_flagged_after_first_parse(tmp_path):
    store = DrainInbox(tmp_path / "drain.db")
    assert store.pending() == []
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO drain_inbox "
            "(dedup_key, message_key, payload, created_at) "
            "VALUES ('bad', 'bad', '{', 1)"
        )

    assert store.pending() == []
    assert store.pending() == []
    with store._connect() as conn:
        row = conn.execute(
            "SELECT invalid_at, invalid_error FROM drain_inbox "
            "WHERE dedup_key='bad'"
        ).fetchone()

    assert row[0] is not None
    assert row[1]
