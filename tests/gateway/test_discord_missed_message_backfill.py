"""Tests for Discord missed-message startup backfill."""

import asyncio
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    merge_pending_message_event,
)
from gateway.session import SessionSource, build_session_key


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.Object = lambda *, id: SimpleNamespace(id=id)
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import discord  # noqa: E402
from plugins.platforms.discord.adapter import (  # noqa: E402
    DiscordAdapter,
    _apply_yaml_config,
    _snowflake_at,
)


class FakeReaction:
    def __init__(self, emoji, *, me=False, users=None):
        self.emoji = emoji
        self.me = me
        self._users = list(users or [])

    async def users(self):
        for user in self._users:
            yield user


class FakeChannel:
    def __init__(self, channel_id=123, history_messages=None, parent_id=None):
        self.id = channel_id
        self.parent_id = parent_id
        self.name = "wiki-inbox"
        self.guild = SimpleNamespace(id=777, name="emo")
        self.topic = None
        self._history_messages = list(history_messages or [])

    def history(self, **kwargs):
        async def _gen():
            for message in self._history_messages:
                yield message

        return _gen()


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = DiscordAdapter(config)
    bot_user = SimpleNamespace(id=999, bot=True, display_name="Hermes", name="hermes")
    adapter._client = SimpleNamespace(user=bot_user, get_channel=lambda _id: None)
    adapter._ready_event.set()

    async def complete_mock_turn(message, **_kwargs):
        waiter = adapter._discord_recovery_waiters.get(str(message.id))
        if waiter is not None and not waiter.done():
            waiter.set_result(ProcessingOutcome.SUCCESS)
        return True

    adapter._handle_message = AsyncMock(side_effect=complete_mock_turn)
    monkeypatch.setenv("DISCORD_MISSED_MESSAGE_BACKFILL", "true")
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    return adapter


def make_message(*, message_id=1, author_id=42, content="please ingest", reactions=None, channel=None, mentions=None):
    channel = channel or FakeChannel()
    return SimpleNamespace(
        id=message_id,
        content=content,
        reactions=list(reactions or []),
        author=SimpleNamespace(id=author_id, bot=False, display_name="Emo", name="emo"),
        channel=channel,
        guild=getattr(channel, "guild", None),
        created_at=datetime.now(timezone.utc),
        attachments=[],
        mentions=list(mentions or []),
        reference=None,
        type=discord.MessageType.default,
    )


def make_bot_message(*, message_id=1, content="please ingest", channel=None, mentions=None):
    message = make_message(
        message_id=message_id,
        content=content,
        channel=channel,
        mentions=mentions,
    )
    message.author.bot = True
    return message


@pytest.mark.asyncio
async def test_backfills_message_with_only_own_success_reaction(adapter):
    message = make_message(reactions=[FakeReaction("✅", me=True)])

    assert await adapter._should_backfill_discord_message(message) is True


@pytest.mark.asyncio
async def test_configured_bot_sender_is_left_for_shared_ingress_policy(adapter, monkeypatch):
    bot_user = adapter._client.user
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "mentions")
    message = make_bot_message(
        message_id=98,
        content=f"<@{bot_user.id}> run this",
        mentions=[bot_user],
    )

    assert await adapter._should_backfill_discord_message(message) is True


@pytest.mark.asyncio
async def test_should_not_backfill_message_with_non_down_bot_response(adapter):
    bot_reply = SimpleNamespace(
        id=2,
        content="Done — captured it.",
        author=SimpleNamespace(id=999, bot=True),
        reference=SimpleNamespace(message_id=1),
        created_at=datetime.now(timezone.utc),
    )
    channel = FakeChannel(history_messages=[bot_reply])
    message = make_message(message_id=1, channel=channel)

    assert await adapter._should_backfill_discord_message(message) is False


@pytest.mark.asyncio
async def test_parent_channel_unreferenced_bot_message_does_not_suppress_backfill(adapter):
    unrelated_bot_post = SimpleNamespace(
        id=2,
        content="Done — captured a different item.",
        author=SimpleNamespace(id=999, bot=True),
        reference=None,
        created_at=datetime.now(timezone.utc),
    )
    channel = FakeChannel(history_messages=[unrelated_bot_post])
    message = make_message(message_id=1, channel=channel)

    assert await adapter._should_backfill_discord_message(message) is True


@pytest.mark.asyncio
async def test_thread_unreferenced_bot_message_does_not_mask_request(adapter):
    bot_post = SimpleNamespace(
        id=2,
        content="Done — captured a different request.",
        author=SimpleNamespace(id=999, bot=True),
        reference=None,
        created_at=datetime.now(timezone.utc),
    )
    thread = FakeChannel(channel_id=456, parent_id=123, history_messages=[bot_post])
    message = make_message(message_id=1, channel=thread)

    assert await adapter._should_backfill_discord_message(message) is True


@pytest.mark.asyncio
async def test_backfills_when_only_down_notice_exists(adapter):
    down_notice = SimpleNamespace(
        id=2,
        content="The agent is down right now.",
        author=SimpleNamespace(id=999, bot=True),
        reference=SimpleNamespace(message_id=1),
        created_at=datetime.now(timezone.utc),
    )
    channel = FakeChannel(history_messages=[down_notice])
    message = make_message(message_id=1, channel=channel)

    assert await adapter._should_backfill_discord_message(message) is True


@pytest.mark.asyncio
async def test_generic_unavailable_response_counts_as_completed(adapter):
    bot_reply = SimpleNamespace(
        id=2,
        content="That package is unavailable on this platform.",
        author=SimpleNamespace(id=999, bot=True),
        reference=SimpleNamespace(message_id=1),
        created_at=datetime.now(timezone.utc),
    )
    channel = FakeChannel(history_messages=[bot_reply])
    message = make_message(message_id=1, channel=channel)

    assert await adapter._should_backfill_discord_message(message) is False


@pytest.mark.asyncio
async def test_run_backfill_dispatches_unaddressed_messages(adapter, monkeypatch):
    bot_user = adapter._client.user
    message = make_message(
        message_id=1,
        content=f"<@{bot_user.id}> please ingest",
        mentions=[bot_user],
    )

    async def fake_candidates(_channels):
        yield message

    monkeypatch.setenv("DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS", "123")
    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", fake_candidates)
    monkeypatch.setattr(adapter, "_should_backfill_discord_message", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_missed_message_backfill_max_dispatches", lambda: 10)
    monkeypatch.setattr(adapter, "_missed_message_backfill_channels", lambda: {"123"})

    await adapter._run_missed_message_backfill()

    adapter._handle_message.assert_awaited_once_with(
        message,
        role_authorized=False,
        recovered=True,
    )


@pytest.mark.asyncio
async def test_run_backfill_dispatch_bound_skips_oldest_and_establishes_cursor(adapter, monkeypatch):
    oldest = make_message(message_id=1)
    retained = make_message(message_id=2)
    adapter.config.extra["free_response_channels"] = "123"

    async def fake_candidates(_channels):
        yield oldest
        yield retained

    async def fake_dispatch(_message):
        return ProcessingOutcome.SUCCESS

    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", fake_candidates)
    monkeypatch.setattr(adapter, "_should_backfill_discord_message", AsyncMock(return_value=True))
    dispatch = AsyncMock(side_effect=fake_dispatch)
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", dispatch)
    monkeypatch.setattr(adapter, "_missed_message_backfill_max_dispatches", lambda: 1)
    monkeypatch.setattr(adapter, "_missed_message_backfill_channels", lambda: {"123"})

    await adapter._run_missed_message_backfill()

    dispatch.assert_awaited_once_with(retained)
    assert adapter._discord_recovery_cursor("123") == "1"


@pytest.mark.asyncio
async def test_unmentioned_chatter_does_not_consume_trigger_dispatch_bound(
    adapter, monkeypatch, caplog
):
    bot_user = adapter._client.user
    trigger = make_message(
        message_id=1,
        content=f"<@{bot_user.id}> recover this",
        mentions=[bot_user],
    )
    chatter = [
        make_message(message_id=2, content="ordinary chatter"),
        make_message(message_id=3, content="more ordinary chatter"),
    ]

    async def candidates(_channels):
        for message in (trigger, *chatter):
            yield message

    dispatch = AsyncMock(return_value=ProcessingOutcome.SUCCESS)
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", dispatch)
    monkeypatch.setattr(
        adapter,
        "_missed_message_backfill_max_dispatches",
        lambda: 1,
    )
    monkeypatch.setattr(
        adapter,
        "_missed_message_backfill_channels",
        lambda: {"123"},
    )

    with caplog.at_level("WARNING"):
        await adapter._run_missed_message_backfill()

    dispatch.assert_awaited_once_with(trigger)
    assert "recovery count bound skipped" not in caplog.text


@pytest.mark.asyncio
async def test_recovery_aborts_when_durable_ledger_is_unavailable(adapter, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", dispatch)
    monkeypatch.setattr(
        adapter,
        "_with_discord_recovery_db_async",
        AsyncMock(return_value=False),
    )

    await adapter._run_missed_message_backfill()

    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_releases_dedup_claim_when_dispatch_is_cancelled(adapter, monkeypatch):
    message = make_message(message_id=97)
    adapter.config.extra["free_response_channels"] = "123"
    started = asyncio.Event()

    async def cancelled_dispatch(_message):
        adapter._dedup.is_duplicate(str(message.id))
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "_dispatch_recovered_message", cancelled_dispatch)
    monkeypatch.setattr(adapter, "_should_backfill_discord_message", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_missed_message_backfill_channels", lambda: {"123"})

    async def candidates(_channels):
        yield message

    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", candidates)
    task = asyncio.create_task(adapter._run_missed_message_backfill())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter._dedup.contains(str(message.id)) is False


@pytest.mark.asyncio
async def test_repeated_ready_coalesces_instead_of_cancelling_active_recovery(adapter):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_recovery():
        started.set()
        await release.wait()

    first = asyncio.create_task(slow_recovery())
    adapter._missed_message_backfill_task = first
    await started.wait()

    second = adapter._ensure_missed_message_backfill_task()

    assert second is first
    assert first.cancelled() is False
    release.set()
    await first


@pytest.mark.asyncio
async def test_recovery_waits_until_gateway_startup_restore_has_finished(
    adapter, monkeypatch
):
    message = make_message(message_id=97)
    adapter.config.extra["free_response_channels"] = "123"
    candidates_started = asyncio.Event()

    async def candidates(_channels):
        candidates_started.set()
        yield message

    runner = SimpleNamespace(
        _startup_restore_in_progress=True,
        _startup_restore_tasks=[],
    )
    adapter.gateway_runner = runner
    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )

    task = adapter._ensure_missed_message_backfill_task()
    await asyncio.sleep(0.01)

    assert runner._startup_restore_tasks == []
    assert candidates_started.is_set() is False
    assert adapter._discord_recovery_cursor("123") is None

    runner._startup_restore_in_progress = False
    await task

    assert candidates_started.is_set() is True
    adapter._handle_message.assert_awaited_once()
    assert adapter._discord_recovery_cursor("123") == "97"


@pytest.mark.asyncio
async def test_recovered_mention_reuses_live_auth_and_mention_gates(adapter, monkeypatch):
    bot_user = adapter._client.user
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    denied = make_message(
        message_id=1,
        author_id=41,
        content=f"<@{bot_user.id}> denied",
        mentions=[bot_user],
    )
    allowed = make_message(
        message_id=2,
        content=f"<@{bot_user.id}> allowed",
        mentions=[bot_user],
    )

    monkeypatch.setattr(
        adapter,
        "_is_allowed_user",
        lambda user_id, *_a, **_kw: user_id == str(allowed.author.id),
    )

    assert await adapter._dispatch_recovered_message(denied) is None
    assert (
        await adapter._dispatch_recovered_message(allowed)
        == ProcessingOutcome.SUCCESS
    )
    adapter._handle_message.assert_awaited_once_with(
        allowed,
        role_authorized=False,
        recovered=True,
    )


@pytest.mark.asyncio
async def test_recovery_does_not_treat_unmentioned_message_as_dispatched(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    adapter.config.extra["free_response_channels"] = ""
    adapter._handle_message = AsyncMock(return_value=False)
    message = make_message(message_id=95, content="not addressed")

    assert await adapter._dispatch_recovered_message(message) is None
    assert adapter._discord_recovery_cursor("123") == "95"


@pytest.mark.asyncio
async def test_recovered_messages_bypass_live_text_debounce(adapter, monkeypatch):
    bot_user = adapter._client.user
    message = make_message(
        message_id=96,
        content=f"<@{bot_user.id}> recover",
        mentions=[bot_user],
    )
    adapter._text_batch_delay_seconds = 0.6
    adapter._handle_message = DiscordAdapter._handle_message.__get__(
        adapter, DiscordAdapter
    )
    adapter.handle_message = AsyncMock()
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")

    assert await adapter._handle_message(
        message,
        role_authorized=False,
        recovered=True,
    )
    adapter.handle_message.assert_awaited_once()
    assert adapter._pending_text_batches == {}


@pytest.mark.asyncio
async def test_live_messages_keep_normal_text_batching_path(adapter):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)
    adapter.config.extra["free_response_channels"] = "123"

    assert await adapter._dispatch_discord_message(message) is True

    adapter._handle_message.assert_awaited_once_with(
        message,
        role_authorized=False,
    )


@pytest.mark.asyncio
async def test_live_text_batch_sorts_racing_receipts_by_snowflake(adapter):
    first_id, second_id = _recent_snowflakes(2)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    first = MessageEvent(
        text="first",
        source=source,
        message_id=str(first_id),
        metadata={"_gateway_receipt_ids": [str(first_id)]},
    )
    second = MessageEvent(
        text="second",
        source=source,
        message_id=str(second_id),
        metadata={"_gateway_receipt_ids": [str(second_id)]},
    )
    adapter._text_batch_delay_seconds = 60

    adapter._enqueue_text_event(second)
    adapter._enqueue_text_event(first)

    pending = next(iter(adapter._pending_text_batches.values()))
    assert pending.message_id == str(first_id)
    assert pending.text == "first\nsecond"
    assert pending.metadata["_gateway_receipt_ids"] == [
        str(first_id),
        str(second_id),
    ]
    for task in adapter._pending_text_batch_tasks.values():
        task.cancel()
    await asyncio.gather(
        *adapter._pending_text_batch_tasks.values(),
        return_exceptions=True,
    )


def test_missed_message_backfill_config_bridge(monkeypatch, tmp_path):
    from gateway.config import load_gateway_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in (
        "DISCORD_MISSED_MESSAGE_BACKFILL",
        "DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS",
        "DISCORD_MISSED_MESSAGE_BACKFILL_WINDOW_SECONDS",
        "DISCORD_MISSED_MESSAGE_BACKFILL_LIMIT",
        "DISCORD_MISSED_MESSAGE_BACKFILL_MAX_DISPATCHES",
    ):
        monkeypatch.delenv(key, raising=False)

    (tmp_path / "config.yaml").write_text(
        "platforms:\n"
        "  discord:\n"
        "    enabled: true\n"
        "discord:\n"
        "  missed_message_backfill:\n"
        "    enabled: true\n"
        "    channels: ['1501971993405292796']\n"
        "    window_seconds: 3600\n"
        "    limit: 25\n"
        "    max_dispatches: 3\n"
    )

    config = load_gateway_config()
    backfill = config.platforms[Platform.DISCORD].extra[
        "missed_message_backfill"
    ]

    assert backfill == {
        "enabled": True,
        "channels": ["1501971993405292796"],
        "window_seconds": 3600,
        "limit": 25,
        "max_dispatches": 3,
    }


def test_default_config_exposes_missed_message_backfill_settings():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["discord"]["missed_message_backfill"] == {
        "enabled": True,
        "channels": "",
        "window_seconds": 21600,
        "limit": 100,
        "max_dispatches": 10,
    }


def test_missed_message_backfill_config_stays_per_adapter():
    first_extra = _apply_yaml_config(
        {},
        {
            "missed_message_backfill": {
                "enabled": True,
                "channels": ["111"],
                "window_seconds": 60,
                "limit": 5,
                "max_dispatches": 2,
            }
        },
    )
    second_extra = _apply_yaml_config(
        {},
        {
            "missed_message_backfill": {
                "enabled": False,
                "channels": ["222"],
                "window_seconds": 120,
                "limit": 6,
                "max_dispatches": 3,
            }
        },
    )

    first = DiscordAdapter(PlatformConfig(enabled=True, token="one", extra=first_extra or {}))
    second = DiscordAdapter(PlatformConfig(enabled=True, token="two", extra=second_extra or {}))

    assert first._missed_message_backfill_enabled() is True
    assert first._missed_message_backfill_channels() == {"111"}
    assert first._missed_message_backfill_window_seconds() == 60
    assert first._missed_message_backfill_limit() == 5
    assert first._missed_message_backfill_max_dispatches() == 2
    assert second._missed_message_backfill_enabled() is False
    assert second._missed_message_backfill_channels() == {"222"}
    assert second._missed_message_backfill_window_seconds() == 120
    assert second._missed_message_backfill_limit() == 6
    assert second._missed_message_backfill_max_dispatches() == 3


def test_recovery_store_pins_profile_home_at_adapter_construction(monkeypatch, tmp_path):
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="one"))
    monkeypatch.setenv("HERMES_HOME", str(second_home))

    assert adapter._discord_recovery_db_path() == (
        first_home / "gateway" / "discord_message_recovery.db"
    )


def test_default_recovery_scope_includes_allowed_and_free_response_channels(adapter, monkeypatch):
    monkeypatch.delenv("DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS", raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "100,200")
    monkeypatch.setenv("DISCORD_FREE_RESPONSE_CHANNELS", "200,300")

    assert adapter._missed_message_backfill_channels() == {"100", "200", "300"}


@pytest.mark.asyncio
async def test_persistent_responded_record_suppresses_backfill(adapter):
    message = make_message(message_id=77)
    adapter._record_discord_message_seen(message, status="responded")
    adapter._record_discord_response(
        reply_to="77",
        result=SimpleNamespace(success=True, message_id="9001"),
        content="Done — captured it.",
        final=True,
    )

    assert await adapter._should_backfill_discord_message(message) is False


def test_down_notice_response_does_not_mark_message_complete(adapter):
    adapter._record_discord_response(
        reply_to="88",
        result=SimpleNamespace(success=False, message_id="9002"),
        content="The agent is down right now.",
        final=True,
    )

    assert adapter._discord_message_is_persistently_complete("88") is False


def test_recovery_ledger_prunes_expired_rows(adapter):
    old = (datetime.now(timezone.utc) - dt.timedelta(days=31)).isoformat()

    def insert_old_rows(conn):
        conn.execute(
            "INSERT INTO discord_messages "
            "(message_id, status, updated_at) VALUES ('old-message', 'responded', ?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO discord_recovery_scans "
            "(scan_id, started_at, completed_at, status, channels, window_seconds, limit_count) "
            "VALUES ('old-scan', ?, ?, 'success', '[]', 3600, 10)",
            (old, old),
        )

    adapter._with_discord_recovery_db(insert_old_rows)
    adapter._discord_recovery_store._initialized = False
    adapter._with_discord_recovery_db(lambda _conn: None)

    def count_old(conn):
        messages = conn.execute(
            "SELECT COUNT(*) FROM discord_messages WHERE message_id='old-message'"
        ).fetchone()[0]
        scans = conn.execute(
            "SELECT COUNT(*) FROM discord_recovery_scans WHERE scan_id='old-scan'"
        ).fetchone()[0]
        return messages, scans

    assert adapter._with_discord_recovery_db(count_old) == (0, 0)


def test_recovery_ledger_migrates_claim_fencing_columns(tmp_path):
    from plugins.platforms.discord.recovery import DiscordRecoveryStore

    legacy_home = tmp_path / "legacy"
    database = legacy_home / "gateway" / "discord_message_recovery.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE discord_messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT,
                thread_id TEXT,
                parent_channel_id TEXT,
                author_id TEXT,
                created_at TEXT,
                status TEXT NOT NULL,
                replied INTEGER NOT NULL DEFAULT 0,
                emoji_ack INTEGER NOT NULL DEFAULT 0,
                outage_response INTEGER NOT NULL DEFAULT 0,
                response_message_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

    store = DiscordRecoveryStore(legacy_home)
    columns = store.call(
        lambda conn: {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(discord_messages)"
            ).fetchall()
        }
    )

    assert {"claim_owner", "claim_epoch"} <= columns


def test_empty_successful_turn_is_persistently_complete(adapter):
    message = make_message(message_id=89)
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=str(message.id),
    )
    adapter._record_discord_processing_start(event, emoji_ack=False)
    adapter._record_discord_processing_complete(event, outcome=ProcessingOutcome.SUCCESS)

    assert adapter._discord_message_is_persistently_complete("89") is True


def test_coalesced_receipts_all_receive_durable_response_evidence(adapter):
    first = make_message(message_id=90)
    second = make_message(message_id=91)
    assert adapter._claim_live_discord_message(first) == "claimed"
    assert adapter._claim_live_discord_message(second) == "claimed"

    adapter._record_discord_response(
        reply_to="90",
        receipt_ids=["90", "91"],
        result=SendResult(success=True, message_id="reply-1"),
        content="done",
        final=True,
    )

    assert adapter._discord_message_is_persistently_complete("90") is True
    assert adapter._discord_message_is_persistently_complete("91") is True


@pytest.mark.asyncio
async def test_image_only_delivery_records_success_and_response_evidence(
    adapter,
    tmp_path,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    assert adapter._claim_live_discord_message(message) == "claimed"
    image_path = tmp_path / "result.png"
    image_path.write_bytes(b"png")
    channel = SimpleNamespace(
        id=123,
        type=0,
        send=AsyncMock(return_value=SimpleNamespace(id=700)),
    )
    adapter._client.get_channel = lambda _id: channel

    result = await adapter.send_multiple_images(
        "123",
        [(image_path.as_uri(), "")],
        metadata={
            "notify": True,
            "reply_to_message_id": message_id,
            "_gateway_receipt_ids": [message_id],
        },
    )

    assert result.success is True
    assert result.message_id == "700"
    assert (
        adapter._discord_message_is_persistently_complete(message_id)
        is True
    )
    nonce = channel.send.await_args.kwargs["nonce"]
    assert nonce == adapter._discord_recovery_nonce(
        message_id,
        "component",
        "images",
        0,
    )
    assert len(nonce) <= 25


@pytest.mark.asyncio
async def test_forum_media_uses_fenced_bounded_nonce(
    adapter,
    tmp_path,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    assert adapter._claim_live_discord_message(message) == "claimed"
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"pdf")
    forum = SimpleNamespace(id=123, type=15)
    adapter._client.get_channel = lambda _id: forum
    forum_post = AsyncMock(
        return_value=SendResult(success=True, message_id="700")
    )
    monkeypatch.setattr(adapter, "_forum_post_file", forum_post)

    result = await adapter._send_file_attachment(
        "123",
        str(file_path),
        metadata={
            "notify": True,
            "reply_to_message_id": message_id,
            "_gateway_receipt_ids": [message_id],
        },
    )

    assert result.success
    nonce = forum_post.await_args.kwargs["nonce"]
    assert nonce == adapter._discord_recovery_nonce(
        message_id,
        "component",
        f"forum-attachment:{file_path}",
    )
    assert len(nonce) <= 25
    assert adapter._discord_message_is_persistently_complete(message_id)


@pytest.mark.asyncio
async def test_voice_delivery_is_fenced_nonce_addressed_and_evidenced(
    adapter,
    tmp_path,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    assert adapter._claim_live_discord_message(message) == "claimed"
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"ogg")
    channel = SimpleNamespace(id=123, type=0)
    http = SimpleNamespace(
        request=AsyncMock(return_value={"id": "701"}),
    )
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda _id: channel,
        fetch_channel=AsyncMock(return_value=channel),
        http=http,
    )

    result = await adapter.send_voice(
        "123",
        str(audio_path),
        metadata={
            "notify": True,
            "reply_to_message_id": message_id,
            "_gateway_receipt_ids": [message_id],
        },
    )

    assert result.success
    payload = json.loads(http.request.await_args.kwargs["form"][0]["value"])
    assert payload["enforce_nonce"] is True
    assert payload["nonce"] == adapter._discord_recovery_nonce(
        message_id,
        "component",
        f"voice:{audio_path}",
    )
    assert len(payload["nonce"]) <= 25
    assert adapter._discord_message_is_persistently_complete(message_id)


def test_fresh_processing_claim_suppresses_duplicate_recovery(adapter):
    message = make_message(message_id=99)
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=str(message.id),
    )
    adapter._record_discord_processing_start(event, emoji_ack=False)

    assert adapter._discord_message_has_active_claim("99") is True


@pytest.mark.asyncio
async def test_scan_preserves_active_claim_and_blocks_later_cursor(
    adapter, monkeypatch
):
    first_id, second_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    active = make_message(message_id=first_id, channel=channel)
    later = make_message(message_id=second_id, channel=channel)
    adapter.config.extra["free_response_channels"] = "123"
    adapter._record_discord_message_seen(active, status="processing")

    async def candidates(_channels):
        yield active
        yield later

    dispatch = AsyncMock(return_value=ProcessingOutcome.SUCCESS)

    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", dispatch)

    await adapter._run_missed_message_backfill()

    dispatch.assert_not_awaited()
    assert adapter._discord_message_has_active_claim(str(first_id)) is True
    assert adapter._discord_recovery_cursor("123") is None

    adapter._record_discord_claim_outcome(
        [str(first_id)],
        ProcessingOutcome.SUCCESS,
    )
    await adapter._set_discord_recovery_receipts(
        [str(first_id)],
        ProcessingOutcome.SUCCESS,
    )
    assert adapter._discord_recovery_cursor("123") == str(first_id)


def test_stale_processing_claim_is_recoverable(adapter):
    message = make_message(message_id=100)
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=str(message.id),
    )
    adapter._record_discord_processing_start(event, emoji_ack=False)
    stale = (datetime.now(timezone.utc) - dt.timedelta(minutes=11)).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id='100'",
            (stale,),
        )
    )

    assert adapter._discord_message_has_active_claim("100") is False


def test_stale_live_claim_is_reclaimed_by_same_adapter(adapter):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)

    assert adapter._claim_live_discord_message(message) == "claimed"
    first_epoch = adapter._discord_recovery_claim_epochs[str(message_id)]
    stale = (
        datetime.now(timezone.utc) - dt.timedelta(minutes=11)
    ).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, str(message_id)),
        )
    )

    assert adapter._claim_live_discord_message(message) == "claimed"
    assert (
        adapter._discord_recovery_claim_epochs[str(message_id)]
        == first_epoch + 1
    )


def test_record_seen_does_not_refresh_foreign_stale_claim(adapter):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)

    assert adapter._claim_live_discord_message(message) == "claimed"
    stale = (
        datetime.now(timezone.utc) - dt.timedelta(minutes=11)
    ).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, str(message_id)),
        )
    )

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    assert replacement._record_discord_message_seen(
        message,
        status="discovered",
    )
    row = replacement._with_discord_recovery_db(
        lambda conn: conn.execute(
            "SELECT status, updated_at FROM discord_messages "
            "WHERE message_id=?",
            (str(message_id),),
        ).fetchone()
    )
    assert row == ("queued", stale)
    assert replacement._claim_live_discord_message(message) == "claimed"


@pytest.mark.asyncio
async def test_processing_claim_heartbeat_renews_long_running_turn(
    adapter,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=message_id,
        metadata={"_gateway_receipt_ids": [message_id]},
    )
    adapter._register_discord_recovery_receipt("123", message_id)

    await adapter.on_processing_start(event)
    stale = (datetime.now(timezone.utc) - dt.timedelta(minutes=11)).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, message_id),
        )
    )
    assert adapter._discord_message_has_active_claim(message_id) is False

    adapter._refresh_discord_processing_claims([message_id])

    assert adapter._discord_message_has_active_claim(message_id) is True
    assert message_id in adapter._discord_recovery_claim_heartbeats

    await adapter.on_processing_complete(
        event,
        ProcessingOutcome.SUCCESS,
    )
    assert adapter._discord_recovery_claim_heartbeats == {}


@pytest.mark.asyncio
async def test_processing_hook_offloads_contended_ledger(adapter, monkeypatch):
    message = make_message(message_id=101)
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=str(message.id),
    )

    def slow_record(*_args, **_kwargs):
        import time
        time.sleep(0.1)

    monkeypatch.setattr(adapter, "_record_discord_processing_start", slow_record)
    processing = asyncio.create_task(adapter.on_processing_start(event))
    await asyncio.sleep(0.01)

    assert processing.done() is False
    await processing


@pytest.mark.asyncio
async def test_recovery_scan_offloads_ledger_writes(adapter, monkeypatch):
    def slow_scan_start(_channels):
        import time
        time.sleep(0.1)
        return "scan"

    monkeypatch.setattr(adapter, "_record_recovery_scan_start", slow_scan_start)
    monkeypatch.setattr(adapter, "_missed_message_backfill_channels", lambda: set())
    scan = asyncio.create_task(adapter._run_missed_message_backfill())
    await asyncio.sleep(0.01)

    assert scan.done() is False
    await scan


@pytest.mark.asyncio
async def test_send_offloads_final_delivery_ledger_write(adapter, monkeypatch):
    channel = FakeChannel(channel_id=123)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=9011))
    channel.fetch_message = AsyncMock()
    adapter._client.get_channel = lambda _channel_id: channel
    adapter.config.extra["free_response_channels"] = "123"

    def slow_record(**_kwargs):
        import time
        time.sleep(0.1)

    monkeypatch.setattr(adapter, "_record_discord_response", slow_record)
    sending = asyncio.create_task(
        adapter.send(
            "123",
            "done",
            reply_to="104",
            metadata={"notify": True},
        )
    )
    await asyncio.sleep(0.01)

    assert sending.done() is False
    assert (await sending).success is True


def test_final_delivery_remains_complete_after_processing_hook(adapter):
    message = make_message(message_id=91)
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=str(message.id),
    )

    adapter._record_discord_processing_start(event, emoji_ack=False)
    adapter._record_discord_response(
        reply_to="91",
        result=SimpleNamespace(success=True, message_id="9004"),
        content="Done",
        final=True,
    )
    adapter._record_discord_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert adapter._discord_message_is_persistently_complete("91") is True


def test_preview_delivery_does_not_mark_message_complete(adapter):
    adapter._record_discord_response(
        reply_to="92",
        result=SimpleNamespace(success=True, message_id="9005"),
        content="partial",
        final=False,
    )

    assert adapter._discord_message_is_persistently_complete("92") is False


def test_successful_final_delivery_clears_prior_outage_state(adapter):
    adapter._record_discord_response(
        reply_to="93",
        result=SimpleNamespace(success=False, message_id="9006"),
        content="Hermes is offline",
        final=True,
    )
    assert adapter._discord_message_is_persistently_complete("93") is False

    adapter._record_discord_response(
        reply_to="93",
        result=SimpleNamespace(success=True, message_id="9007"),
        content="Recovered successfully",
        final=True,
    )

    assert adapter._discord_message_is_persistently_complete("93") is True


@pytest.mark.asyncio
async def test_send_uses_notify_metadata_as_final_delivery_signal(adapter):
    channel = FakeChannel(channel_id=123)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=9008))
    channel.fetch_message = AsyncMock()
    adapter._client.get_channel = lambda _channel_id: channel

    preview = await adapter.send(
        "123",
        "partial",
        reply_to="94",
        metadata={"expect_edits": True},
    )
    assert preview.success is True
    assert adapter._discord_message_is_persistently_complete("94") is False

    final = await adapter.send(
        "123",
        "complete",
        reply_to="94",
        metadata={"notify": True},
    )
    assert final.success is True
    assert adapter._discord_message_is_persistently_complete("94") is True


@pytest.mark.asyncio
async def test_final_stream_edit_marks_original_request_complete(adapter):
    channel = FakeChannel(channel_id=123)
    message = SimpleNamespace(edit=AsyncMock())
    channel.fetch_message = AsyncMock(return_value=message)
    adapter._client.get_channel = lambda _channel_id: channel

    result = await adapter.edit_message(
        "123",
        "9009",
        "complete streamed response",
        finalize=True,
        metadata={"reply_to_message_id": "102"},
    )

    assert result.success is True
    assert adapter._discord_message_is_persistently_complete("102") is True


def test_disabled_recovery_does_not_create_hot_path_ledger(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_MISSED_MESSAGE_BACKFILL", "false")
    message = make_message(message_id=90)
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=str(message.id),
    )

    adapter._record_discord_processing_start(event, emoji_ack=False)
    adapter._record_discord_processing_complete(event, ProcessingOutcome.SUCCESS)
    adapter._record_discord_response(
        reply_to="90",
        result=SimpleNamespace(success=True, message_id="9003"),
        content="Done",
        final=True,
    )

    db_path = adapter._discord_recovery_db_path()
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_iter_candidates_includes_active_and_archived_threads(adapter):
    active_msg = make_message(message_id=201, channel=FakeChannel(channel_id=2010))
    archived_msg = make_message(message_id=202, channel=FakeChannel(channel_id=2020))
    active_thread = FakeChannel(channel_id=2010, history_messages=[active_msg])
    archived_thread = FakeChannel(channel_id=2020, history_messages=[archived_msg])

    class ParentChannel(FakeChannel):
        threads = [active_thread]

        def archived_threads(self, **kwargs):
            async def _gen():
                yield archived_thread
            return _gen()

    parent = ParentChannel(channel_id=123, history_messages=[])
    adapter._client.get_channel = lambda _id: parent

    got = []
    async for msg in adapter._iter_missed_message_backfill_candidates({"123"}):
        got.append(msg.id)

    assert got == [201, 202]


@pytest.mark.asyncio
async def test_wildcard_candidates_include_forum_threads(adapter):
    forum_message = make_message(
        message_id=203,
        channel=FakeChannel(channel_id=2030),
    )
    forum_thread = FakeChannel(
        channel_id=2030,
        history_messages=[forum_message],
    )
    forum = FakeChannel(channel_id=124, history_messages=[])
    forum.threads = [forum_thread]
    guild = SimpleNamespace(text_channels=[], forums=[forum])
    adapter._client.guilds = [guild]

    got = [
        message.id
        async for message in adapter._iter_missed_message_backfill_candidates(
            {"*"}
        )
    ]

    assert got == [203]


@pytest.mark.asyncio
async def test_iter_candidates_applies_scan_limit_per_channel(adapter, monkeypatch):
    first = FakeChannel(
        channel_id=123,
        history_messages=[make_message(message_id=1), make_message(message_id=2)],
    )
    second = FakeChannel(
        channel_id=456,
        history_messages=[make_message(message_id=3), make_message(message_id=4)],
    )
    adapter._client.get_channel = lambda channel_id: {123: first, 456: second}[channel_id]
    monkeypatch.setattr(adapter, "_missed_message_backfill_limit", lambda: 3)

    got = []
    async for msg in adapter._iter_missed_message_backfill_candidates({"123", "456"}):
        got.append(msg.id)

    assert len(got) == 4
    assert set(got) == {1, 2, 3, 4}


@pytest.mark.asyncio
async def test_iter_candidates_round_robins_configured_channels(adapter, monkeypatch):
    first = FakeChannel(
        channel_id=123,
        history_messages=[
            make_message(message_id=1),
            make_message(message_id=2),
            make_message(message_id=3),
        ],
    )
    second = FakeChannel(
        channel_id=456,
        history_messages=[make_message(message_id=4)],
    )
    adapter._client.get_channel = lambda channel_id: {123: first, 456: second}[channel_id]
    monkeypatch.setattr(adapter, "_missed_message_backfill_limit", lambda: 3)

    got = []
    async for message in adapter._iter_missed_message_backfill_candidates({"123", "456"}):
        got.append(message.id)

    assert 4 in got


@pytest.mark.asyncio
async def test_iter_candidates_keeps_latest_messages_when_window_exceeds_limit(
    adapter, monkeypatch, caplog
):
    class RealisticChannel(FakeChannel):
        def history(self, **kwargs):
            async def _gen():
                messages = list(self._history_messages)
                if not kwargs["oldest_first"]:
                    messages.reverse()
                for message in messages[:kwargs["limit"]]:
                    yield message

            return _gen()

    channel = RealisticChannel(
        channel_id=123,
        history_messages=[
            make_message(message_id=1),
            make_message(message_id=2),
            make_message(message_id=3),
            make_message(message_id=4),
        ],
    )
    adapter._client.get_channel = lambda _channel_id: channel
    monkeypatch.setattr(adapter, "_missed_message_backfill_limit", lambda: 3)

    with caplog.at_level("WARNING"):
        got = []
        async for msg in adapter._iter_missed_message_backfill_candidates({"123"}):
            got.append(msg.id)

    assert got == [2, 3, 4]
    assert "history count bound skipped older messages" in caplog.text


@pytest.mark.asyncio
async def test_history_count_bound_does_not_cross_active_claim(
    adapter, monkeypatch, caplog
):
    class RealisticChannel(FakeChannel):
        def history(self, **kwargs):
            async def _gen():
                messages = list(reversed(self._history_messages))
                for message in messages[: kwargs["limit"]]:
                    yield message

            return _gen()

    channel = RealisticChannel(
        channel_id=123,
        history_messages=[
            make_message(message_id=1),
            make_message(message_id=2),
            make_message(message_id=3),
            make_message(message_id=4),
        ],
    )
    for message in channel._history_messages:
        message.channel = channel
    adapter._record_discord_message_seen(
        channel._history_messages[0],
        status="processing",
    )
    adapter._client.get_channel = lambda _channel_id: channel
    monkeypatch.setattr(adapter, "_missed_message_backfill_limit", lambda: 3)

    with caplog.at_level("WARNING"):
        got = [
            message
            async for message in adapter._iter_missed_message_backfill_candidates(
                {"123"}
            )
        ]

    assert got == []
    assert adapter._discord_recovery_cursor("123") is None
    assert "history count boundary" in caplog.text
    assert "deferred behind active durable work" in caplog.text


def test_recovery_cursor_round_trip_is_channel_scoped(adapter):
    adapter._advance_discord_recovery_cursor("123", "1001")
    adapter._advance_discord_recovery_cursor("456", "2002")

    assert adapter._discord_recovery_cursor("123") == "1001"
    assert adapter._discord_recovery_cursor("456") == "2002"


@pytest.mark.asyncio
async def test_cursor_does_not_advance_past_incomplete_dispatched_message(adapter, monkeypatch):
    channel = FakeChannel(
        channel_id=123,
        history_messages=[
            make_message(message_id=1),
            make_message(message_id=2),
        ],
    )
    for message in channel._history_messages:
        message.channel = channel
    adapter._client.get_channel = lambda _channel_id: channel
    adapter.config.extra["free_response_channels"] = "123"
    monkeypatch.setattr(adapter, "_missed_message_backfill_channels", lambda: {"123"})
    monkeypatch.setattr(adapter, "_should_backfill_discord_message", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", AsyncMock(side_effect=[True, True]))
    monkeypatch.setattr(adapter, "_missed_message_backfill_max_dispatches", lambda: 10)

    await adapter._run_missed_message_backfill()

    assert adapter._discord_recovery_cursor("123") is None


def test_final_delivery_alone_does_not_advance_channel_cursor(adapter):
    message = make_message(message_id=103, channel=FakeChannel(channel_id=123))
    adapter._record_discord_message_seen(message, status="processing")

    adapter._record_discord_response(
        reply_to="103",
        result=SimpleNamespace(success=True, message_id="9010"),
        content="done",
        final=True,
    )

    assert adapter._discord_recovery_cursor("123") is None


@pytest.mark.asyncio
async def test_iter_candidates_uses_persisted_channel_cursor(adapter, monkeypatch):
    cursor_id = _snowflake_at(datetime.now(timezone.utc) - dt.timedelta(minutes=1))

    class CursorChannel(FakeChannel):
        def history(self, **kwargs):
            self.history_kwargs = kwargs

            async def _gen():
                yield make_message(message_id=cursor_id + 1, channel=self)

            return _gen()

    channel = CursorChannel(channel_id=123)
    adapter._client.get_channel = lambda _channel_id: channel
    adapter._advance_discord_recovery_cursor("123", str(cursor_id))
    monkeypatch.setattr(discord, "Object", lambda *, id: SimpleNamespace(id=id))

    got = []
    async for message in adapter._iter_missed_message_backfill_candidates({"123"}):
        got.append(message.id)

    assert got == [cursor_id + 1]
    assert getattr(channel.history_kwargs["after"], "id", None) == cursor_id


def _recent_snowflakes(count: int) -> list[int]:
    base = _snowflake_at(datetime.now(timezone.utc) - dt.timedelta(minutes=1))
    return [base + offset + 1 for offset in range(count)]


@pytest.mark.asyncio
async def test_missed_messages_replay_through_normal_inbound_path_in_order(
    adapter, monkeypatch
):
    first_id, second_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    first = make_message(message_id=first_id, channel=channel)
    second = make_message(message_id=second_id, channel=channel)
    adapter.config.extra["free_response_channels"] = "123"

    async def candidates(_channels):
        # REST/test doubles are not trusted to provide ordering.
        yield second
        yield first

    monkeypatch.setattr(
        adapter, "_known_missed_message_backfill_channels", AsyncMock(return_value={"123"})
    )
    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", candidates)
    monkeypatch.setattr(
        adapter, "_should_backfill_discord_message", AsyncMock(return_value=True)
    )

    await adapter._run_missed_message_backfill()

    assert [
        call.args[0].id for call in adapter._handle_message.await_args_list
    ] == [first_id, second_id]
    assert adapter._discord_recovery_cursor("123") == str(second_id)


@pytest.mark.asyncio
async def test_completed_cursor_dedupes_live_and_backfilled_receipt(adapter):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)
    adapter.config.extra["free_response_channels"] = "123"

    assert (
        await adapter._dispatch_recovered_message(message)
        == ProcessingOutcome.SUCCESS
    )
    assert await adapter._dispatch_recovered_message(message) is None
    assert adapter._handle_message.await_count == 1


@pytest.mark.asyncio
async def test_live_replay_after_memory_ttl_respects_durable_active_claim(
    adapter,
):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)
    adapter.config.extra["free_response_channels"] = "123"

    assert adapter._claim_live_discord_message(message) == "claimed"
    assert adapter._dedup.is_duplicate(str(message_id)) is False
    adapter._dedup._seen[str(message_id)] -= adapter._dedup._ttl + 1

    assert adapter._dedup.contains(str(message_id)) is False
    assert await adapter._dispatch_discord_message(message) is False
    adapter._handle_message.assert_not_awaited()
    assert adapter._discord_message_has_active_claim(str(message_id)) is True


def test_durable_live_claim_is_atomic_across_adapter_instances(adapter):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)
    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    adapter._with_discord_recovery_db(lambda _conn: None)
    replacement._with_discord_recovery_db(lambda _conn: None)
    start = threading.Barrier(2)
    results = []

    def claim(owner):
        start.wait(timeout=2)
        results.append(owner._claim_live_discord_message(message))

    threads = [
        threading.Thread(target=claim, args=(owner,))
        for owner in (adapter, replacement)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["active", "claimed"]


@pytest.mark.asyncio
async def test_reclaimed_lease_fences_stale_owner_and_cursor(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    first_epoch = adapter._discord_recovery_claim_epochs[message_id]
    stale = (
        datetime.now(timezone.utc) - dt.timedelta(minutes=11)
    ).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, message_id),
        )
    )

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    replacement._register_discord_recovery_receipt("123", message_id)
    assert replacement._claim_live_discord_message(message) == "claimed"
    replacement_epoch = replacement._discord_recovery_claim_epochs[
        message_id
    ]
    assert replacement_epoch > first_epoch

    def claim_row(conn):
        return conn.execute(
            """
            SELECT status, claim_owner, claim_epoch, updated_at
              FROM discord_messages
             WHERE message_id=?
            """,
            (message_id,),
        ).fetchone()

    before = replacement._with_discord_recovery_db(claim_row)
    adapter._refresh_discord_processing_claims([message_id])
    adapter._record_discord_claim_outcome(
        [message_id],
        ProcessingOutcome.SUCCESS,
    )
    adapter._record_discord_response(
        reply_to=message_id,
        result=SimpleNamespace(success=True, message_id="stale-response"),
        content="stale owner response",
        final=True,
    )
    assert replacement._with_discord_recovery_db(claim_row) == before

    await adapter._set_discord_recovery_receipts(
        [message_id],
        ProcessingOutcome.SUCCESS,
    )
    assert adapter._discord_recovery_cursor("123") is None

    replacement._record_discord_claim_outcome(
        [message_id],
        ProcessingOutcome.SUCCESS,
    )
    await replacement._set_discord_recovery_receipts(
        [message_id],
        ProcessingOutcome.SUCCESS,
    )
    assert replacement._discord_recovery_cursor("123") == message_id


@pytest.mark.parametrize("forum", [False, True])
@pytest.mark.asyncio
async def test_reclaimed_lease_fences_actual_outbound_send(adapter, forum):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    stale = (
        datetime.now(timezone.utc) - dt.timedelta(minutes=11)
    ).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, message_id),
        )
    )

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    replacement._register_discord_recovery_receipt("123", message_id)
    assert replacement._claim_live_discord_message(message) == "claimed"

    channel = FakeChannel(channel_id=123)
    channel.type = 15 if forum else 0
    channel.send = AsyncMock(return_value=SimpleNamespace(id=9012))
    for owner in (adapter, replacement):
        owner._client = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_channel=lambda _channel_id: channel,
        )
        owner._reply_to_mode = "off"

    stale_forum_send = AsyncMock(
        return_value=SendResult(success=True, message_id="9013")
    )
    replacement_forum_send = AsyncMock(
        return_value=SendResult(success=True, message_id="9014")
    )
    adapter._send_to_forum = stale_forum_send
    replacement._send_to_forum = replacement_forum_send
    original_record_response = replacement._record_discord_response
    guarded_records = []

    def record_response_under_guard(**kwargs):
        guard = kwargs.get("claim_guard")
        guarded_records.append(guard is not None)
        return original_record_response(**kwargs)

    replacement._record_discord_response = record_response_under_guard

    stale_result = await adapter.send(
        "123",
        "stale response",
        reply_to=message_id,
        metadata={"notify": True},
    )
    assert stale_result.success is False
    assert stale_result.error == "Discord recovery claim no longer owned"
    channel.send.assert_not_awaited()
    stale_forum_send.assert_not_awaited()

    current_result = await replacement.send(
        "123",
        "current response",
        reply_to=message_id,
        metadata={"notify": True},
    )
    assert current_result.success is True
    assert guarded_records == [True]
    duplicate_result = await replacement.send(
        "123",
        "duplicate response",
        reply_to=message_id,
        metadata={"notify": True},
    )
    assert duplicate_result.success is False
    assert duplicate_result.error == "Discord recovery claim no longer owned"
    if forum:
        replacement_forum_send.assert_awaited_once()
        channel.send.assert_not_awaited()
    else:
        channel.send.assert_awaited_once()
        replacement_forum_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_multichunk_retry_reuses_enforced_nonces(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    adapter._reply_to_mode = "off"
    adapter.MAX_MESSAGE_LENGTH = 20

    visible_by_nonce = {}
    calls = []
    fail_second_chunk_once = True

    async def send_with_enforced_nonce(*, content, reference, nonce):
        nonlocal fail_second_chunk_once
        calls.append((content, nonce))
        if nonce in visible_by_nonce:
            return visible_by_nonce[nonce]
        if len(visible_by_nonce) == 1 and fail_second_chunk_once:
            fail_second_chunk_once = False
            raise RuntimeError("transient chunk failure")
        sent = SimpleNamespace(id=9020 + len(visible_by_nonce))
        visible_by_nonce[nonce] = sent
        return sent

    channel = FakeChannel(channel_id=123)
    channel.send = AsyncMock(side_effect=send_with_enforced_nonce)
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda _channel_id: channel,
    )
    content = "A" * 50

    first = await adapter.send(
        "123",
        content,
        metadata={
            "notify": True,
            "reply_to_message_id": message_id,
        },
    )
    assert first.success is False
    assert adapter._discord_message_is_persistently_complete(message_id) is False

    second = await adapter.send(
        "123",
        content,
        metadata={
            "notify": True,
            "reply_to_message_id": message_id,
        },
    )
    assert second.success is True
    nonces = [nonce for _content, nonce in calls]
    first_nonce = adapter._discord_recovery_nonce(
        message_id,
        "text",
        1,
        0,
    )
    assert nonces.count(first_nonce) == 2
    assert len(visible_by_nonce) == len(
        adapter.truncate_message(content, adapter.MAX_MESSAGE_LENGTH)
    )
    assert all(len(nonce) <= 25 for nonce in nonces)
    assert adapter._discord_message_is_persistently_complete(message_id) is True


@pytest.mark.asyncio
async def test_forum_recovery_delivery_uses_enforced_chunk_nonces(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    thread_channel = SimpleNamespace(
        id=555,
        send=AsyncMock(return_value=SimpleNamespace(id=9021)),
    )
    thread = SimpleNamespace(
        id=555,
        thread=thread_channel,
        message=SimpleNamespace(id=9020),
    )
    original_create_forum_thread = adapter._create_forum_thread
    create_forum_thread = AsyncMock(return_value=thread)
    adapter._create_forum_thread = create_forum_thread
    adapter.MAX_MESSAGE_LENGTH = 20

    result = await adapter._send_to_forum(
        SimpleNamespace(id=123),
        "A" * 50,
        nonce_base=(message_id, "text", 1),
    )

    assert result.success is True
    assert create_forum_thread.await_args.kwargs["nonce"] == (
        adapter._discord_recovery_nonce(message_id, "text", 1, 0)
    )
    assert [
        call.kwargs["nonce"]
        for call in thread_channel.send.await_args_list
    ] == [
        adapter._discord_recovery_nonce(
            message_id,
            "text",
            1,
            index,
        )
        for index in range(1, thread_channel.send.await_count + 1)
    ]
    thread_channel.send.reset_mock(
        side_effect=True,
        return_value=True,
    )
    thread_channel.send.side_effect = RuntimeError("follow-up failed")
    partial = await adapter._send_to_forum(
        SimpleNamespace(id=123),
        "A" * 50,
        nonce_base=(message_id, "text", 1),
    )
    assert partial.success is False
    assert "follow-up failed" in (partial.error or "")
    adapter._create_forum_thread = original_create_forum_thread

    params_context = MagicMock()
    params_context.__enter__.return_value = "params"
    params_context.__exit__.return_value = False
    handle_parameters = MagicMock(return_value=params_context)
    http_module = MagicMock(handle_message_parameters=handle_parameters)
    monkeypatch.setitem(sys.modules, "discord.http", http_module)
    http = SimpleNamespace(
        start_thread_in_forum=AsyncMock(
            return_value={"id": "555", "message": {"id": "9020"}}
        )
    )
    state = SimpleNamespace(http=http, allowed_mentions=None)
    forum = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=777),
        _state=state,
        default_auto_archive_duration=1440,
        create_thread=AsyncMock(),
    )
    built_thread = SimpleNamespace(id=555)
    built_message = SimpleNamespace(id=9020)
    monkeypatch.setattr(discord, "Thread", MagicMock(return_value=built_thread))
    import plugins.platforms.discord.adapter as adapter_module

    monkeypatch.setattr(
        adapter_module,
        "DiscordMessage",
        MagicMock(return_value=built_message),
    )

    created = await adapter._create_forum_thread(
        forum,
        name="Recovery",
        content="Recovered response",
        nonce=f"{message_id}-1-0",
    )

    assert created.thread is built_thread
    assert created.message is built_message
    assert (
        handle_parameters.call_args.kwargs["nonce"]
        == f"{message_id}-1-0"
    )
    http.start_thread_in_forum.assert_awaited_once_with(
        123,
        params="params",
        reason=None,
    )
    forum.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_reclaimed_lease_fences_streamed_edit(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    stale = (
        datetime.now(timezone.utc) - dt.timedelta(minutes=11)
    ).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, message_id),
        )
    )

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    replacement._register_discord_recovery_receipt("123", message_id)
    assert replacement._claim_live_discord_message(message) == "claimed"

    response = SimpleNamespace(edit=AsyncMock())
    channel = FakeChannel(channel_id=123)
    channel.fetch_message = AsyncMock(return_value=response)
    for owner in (adapter, replacement):
        owner._client = SimpleNamespace(
            user=SimpleNamespace(id=999),
            get_channel=lambda _channel_id: channel,
        )

    stale_result = await adapter.edit_message(
        "123",
        "9015",
        "stale streamed response",
        finalize=True,
        metadata={"reply_to_message_id": message_id},
    )
    assert stale_result.success is False
    assert stale_result.error == "Discord recovery claim no longer owned"
    response.edit.assert_not_awaited()

    current_result = await replacement.edit_message(
        "123",
        "9015",
        "current streamed response",
        finalize=True,
        metadata={"reply_to_message_id": message_id},
    )
    assert current_result.success is True
    response.edit.assert_awaited_once_with(content="current streamed response")


@pytest.mark.asyncio
async def test_final_overflow_edit_records_recovery_completion(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"

    response = SimpleNamespace(edit=AsyncMock())
    channel = FakeChannel(channel_id=123)
    channel.fetch_message = AsyncMock(return_value=response)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=9016))
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda _channel_id: channel,
    )

    result = await adapter.edit_message(
        "123",
        "9015",
        "x" * (adapter.MAX_MESSAGE_LENGTH + 1),
        finalize=True,
        metadata={"reply_to_message_id": message_id},
    )

    assert result.success is True
    assert adapter._discord_message_is_persistently_complete(message_id) is True


@pytest.mark.asyncio
async def test_reclaimed_lease_fences_processing_reactions(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=message_id,
    )
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    stale = (
        datetime.now(timezone.utc) - dt.timedelta(minutes=11)
    ).isoformat()
    adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "UPDATE discord_messages SET updated_at=? WHERE message_id=?",
            (stale, message_id),
        )
    )

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    replacement._client = SimpleNamespace(user=SimpleNamespace(id=999))
    replacement._register_discord_recovery_receipt("123", message_id)
    assert replacement._claim_live_discord_message(message) == "claimed"

    await adapter.on_processing_complete(
        event,
        ProcessingOutcome.SUCCESS,
    )
    message.add_reaction.assert_not_awaited()
    message.remove_reaction.assert_not_awaited()
    assert adapter._discord_recovery_cursor("123") is None

    await replacement.on_processing_complete(
        event,
        ProcessingOutcome.SUCCESS,
    )
    message.remove_reaction.assert_awaited_once_with(
        "👀",
        replacement._client.user,
    )
    message.add_reaction.assert_awaited_once_with("✅")
    assert replacement._discord_recovery_cursor("123") == message_id


@pytest.mark.asyncio
async def test_current_owner_final_reaction_allows_responded_claim(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    message.add_reaction = AsyncMock()
    message.remove_reaction = AsyncMock()
    event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        raw_message=message,
        message_id=message_id,
    )
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    adapter._record_discord_response(
        reply_to=message_id,
        result=SendResult(success=True, message_id="9022"),
        content="done",
        final=True,
    )

    await adapter.on_processing_complete(
        event,
        ProcessingOutcome.SUCCESS,
    )

    message.remove_reaction.assert_awaited_once_with(
        "👀",
        adapter._client.user,
    )
    message.add_reaction.assert_awaited_once_with("✅")
    assert adapter._discord_recovery_cursor("123") == message_id


@pytest.mark.asyncio
async def test_later_adapter_cannot_advance_past_other_active_claim(adapter):
    first_id, second_id = [
        str(value) for value in _recent_snowflakes(2)
    ]
    first = make_message(message_id=int(first_id))
    second = make_message(message_id=int(second_id))
    adapter._register_discord_recovery_receipt("123", first_id)
    assert adapter._claim_live_discord_message(first) == "claimed"

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    replacement._register_discord_recovery_receipt("123", second_id)
    assert replacement._claim_live_discord_message(second) == "claimed"

    replacement._record_discord_claim_outcome(
        [second_id],
        ProcessingOutcome.SUCCESS,
    )
    await replacement._set_discord_recovery_receipts(
        [second_id],
        ProcessingOutcome.SUCCESS,
    )

    assert replacement._discord_recovery_cursor("123") is None


@pytest.mark.asyncio
async def test_claim_transfer_cannot_interleave_with_cursor_commit(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(message) == "claimed"
    adapter._record_discord_claim_outcome(
        [message_id],
        ProcessingOutcome.SUCCESS,
    )

    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    replacement._with_discord_recovery_db(lambda _conn: None)
    commit_started = threading.Event()
    release_commit = threading.Event()
    original_advance = adapter._advance_discord_recovery_cursor

    def paused_advance(*args, **kwargs):
        commit_started.set()
        release_commit.wait(timeout=5)
        return original_advance(*args, **kwargs)

    monkeypatch.setattr(
        adapter,
        "_advance_discord_recovery_cursor",
        paused_advance,
    )
    commit_task = asyncio.create_task(
        adapter._set_discord_recovery_receipts(
            [message_id],
            ProcessingOutcome.SUCCESS,
        )
    )
    assert await asyncio.to_thread(commit_started.wait, 5)

    claim_task = asyncio.create_task(
        asyncio.to_thread(
            replacement._claim_live_discord_message,
            message,
        )
    )
    await asyncio.sleep(0.05)
    assert claim_task.done() is False

    release_commit.set()
    await asyncio.wait_for(commit_task, timeout=5)
    assert await asyncio.wait_for(claim_task, timeout=5) == "complete"
    assert adapter._discord_recovery_cursor("123") == message_id


@pytest.mark.asyncio
async def test_durable_later_completion_rebuilds_contiguous_cursor(
    adapter,
    monkeypatch,
):
    first_id, second_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    first = make_message(message_id=first_id, channel=channel)
    second = make_message(message_id=second_id, channel=channel)
    adapter.config.extra["free_response_channels"] = "123"
    adapter._record_discord_message_seen(second, status="processing")
    adapter._record_discord_response(
        reply_to=str(second_id),
        result=SimpleNamespace(success=True, message_id="response-2"),
        content="done",
        final=True,
    )

    async def candidates(_channels):
        yield first
        yield second

    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )

    await adapter._run_missed_message_backfill()

    assert [
        call.args[0].id for call in adapter._handle_message.await_args_list
    ] == [first_id]
    assert adapter._discord_recovery_cursor("123") == str(second_id)


@pytest.mark.asyncio
async def test_live_receipt_waits_behind_backfill_and_cannot_race_cursor(
    adapter, monkeypatch
):
    missed_id, live_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    missed = make_message(message_id=missed_id, channel=channel)
    bot_user = adapter._client.user
    live = make_message(
        message_id=live_id,
        channel=channel,
        content=f"<@{bot_user.id}> live",
        mentions=[bot_user],
    )
    adapter.config.extra["free_response_channels"] = "123"

    async def candidates(_channels):
        yield missed

    monkeypatch.setattr(
        adapter, "_known_missed_message_backfill_channels", AsyncMock(return_value={"123"})
    )
    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", candidates)
    monkeypatch.setattr(
        adapter, "_should_backfill_discord_message", AsyncMock(return_value=True)
    )
    adapter._discord_recovery_barrier.clear()

    original_handle = adapter._handle_message
    dispatched = []

    async def barrier_aware_handle(message, **kwargs):
        if message is live:
            await adapter._discord_recovery_barrier.wait()
        dispatched.append(message.id)
        return await original_handle(message, **kwargs)

    adapter._handle_message = AsyncMock(side_effect=barrier_aware_handle)
    live_task = asyncio.create_task(adapter._dispatch_discord_message(live))
    await asyncio.sleep(0)
    assert dispatched == []

    await adapter._run_missed_message_backfill()
    assert await live_task is True
    assert dispatched == [missed_id, live_id]
    assert adapter._discord_recovery_cursor("123") == str(missed_id)
    adapter._record_discord_claim_outcome(
        [str(live_id)],
        ProcessingOutcome.SUCCESS,
    )
    await adapter._set_discord_recovery_receipts(
        [str(live_id)],
        ProcessingOutcome.SUCCESS,
    )
    assert adapter._discord_recovery_cursor("123") == str(live_id)


@pytest.mark.asyncio
async def test_concurrent_live_callbacks_register_before_cursor_io(
    adapter,
    monkeypatch,
):
    first_id, second_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    first = make_message(message_id=first_id, channel=channel)
    second = make_message(message_id=second_id, channel=channel)
    adapter.config.extra["free_response_channels"] = "123"
    first_lookup_started = threading.Event()
    release_first_lookup = threading.Event()
    original_cursor = adapter._discord_recovery_cursor
    first_lookup_pending = True

    def delayed_cursor(channel_id):
        nonlocal first_lookup_pending
        if first_lookup_pending:
            first_lookup_pending = False
            first_lookup_started.set()
            release_first_lookup.wait(timeout=2)
        return original_cursor(channel_id)

    async def complete(message, **_kwargs):
        adapter._record_discord_claim_outcome(
            [str(message.id)],
            ProcessingOutcome.SUCCESS,
        )
        await adapter._set_discord_recovery_receipts(
            [str(message.id)],
            ProcessingOutcome.SUCCESS,
        )
        return True

    monkeypatch.setattr(
        adapter,
        "_discord_recovery_cursor",
        delayed_cursor,
    )
    adapter._handle_message = AsyncMock(side_effect=complete)

    first_task = asyncio.create_task(
        adapter._dispatch_discord_message(first)
    )
    while not first_lookup_started.is_set():
        await asyncio.sleep(0)
    assert adapter._discord_message_has_active_claim(str(first_id)) is True
    replacement = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake-token")
    )
    assert replacement._discord_message_has_active_claim(str(first_id)) is True
    second_task = asyncio.create_task(
        adapter._dispatch_discord_message(second)
    )

    assert await asyncio.wait_for(second_task, timeout=5) is True
    assert original_cursor("123") is None

    release_first_lookup.set()
    assert await asyncio.wait_for(first_task, timeout=5) is True
    assert [
        call.args[0].id for call in adapter._handle_message.await_args_list
    ] == [second_id, first_id]
    assert original_cursor("123") == str(second_id)


@pytest.mark.asyncio
async def test_processing_failure_stops_channel_and_does_not_advance_cursor(
    adapter, monkeypatch
):
    first_id, second_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    first = make_message(message_id=first_id, channel=channel)
    second = make_message(message_id=second_id, channel=channel)
    adapter.config.extra["free_response_channels"] = "123"

    async def candidates(_channels):
        yield first
        yield second

    dispatch = AsyncMock(return_value=ProcessingOutcome.FAILURE)
    monkeypatch.setattr(
        adapter, "_known_missed_message_backfill_channels", AsyncMock(return_value={"123"})
    )
    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", candidates)
    monkeypatch.setattr(
        adapter, "_should_backfill_discord_message", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", dispatch)

    await adapter._run_missed_message_backfill()

    dispatch.assert_awaited_once_with(first)
    assert adapter._discord_recovery_cursor("123") is None
    retry_task = adapter._missed_message_backfill_retry_task
    assert retry_task is not None and not retry_task.done()
    assert await adapter._dispatch_discord_message(second) is True
    assert adapter._discord_recovery_cursor("123") is None
    retry_task.cancel()
    await asyncio.gather(retry_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_silent_ambient_decision_advances_only_after_routing_finishes(
    adapter,
):
    message_id = _recent_snowflakes(1)[0]
    message = make_message(message_id=message_id)
    routing_started = asyncio.Event()
    finish_routing = asyncio.Event()

    async def silent_route(_message, **_kwargs):
        routing_started.set()
        await finish_routing.wait()
        return False

    adapter._handle_message = AsyncMock(side_effect=silent_route)
    task = asyncio.create_task(
        adapter._dispatch_serialized_discord_message(
            message,
            role_authorized=False,
        )
    )
    await routing_started.wait()

    assert adapter._discord_recovery_cursor("123") is None

    finish_routing.set()
    assert await task is None
    assert adapter._discord_recovery_cursor("123") == str(message_id)


@pytest.mark.asyncio
async def test_slow_send_guard_does_not_block_unrelated_live_claim(adapter):
    first_id, second_id = [str(value) for value in _recent_snowflakes(2)]
    first = make_message(message_id=int(first_id))
    second = make_message(message_id=int(second_id))
    assert adapter._claim_live_discord_message(first) == "claimed"

    send_started = asyncio.Event()
    finish_send = asyncio.Event()
    channel = FakeChannel(channel_id=123)

    async def slow_send(**_kwargs):
        send_started.set()
        await finish_send.wait()
        return SimpleNamespace(id=9012)

    channel.send = AsyncMock(side_effect=slow_send)
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda _channel_id: channel,
    )
    adapter._reply_to_mode = "off"

    send_task = asyncio.create_task(
        adapter.send(
            "123",
            "first response",
            reply_to=first_id,
            metadata={"notify": True},
        )
    )
    await send_started.wait()

    assert adapter._claim_live_discord_message(second) == "claimed"

    finish_send.set()
    assert (await send_task).success is True


@pytest.mark.asyncio
async def test_auto_thread_starter_is_rejected_before_durable_claim(adapter):
    starter_id = str(_recent_snowflakes(1)[0])
    adapter._auto_thread_starters.is_duplicate(starter_id)
    starter = make_message(message_id=int(starter_id))

    assert await adapter._dispatch_discord_message(starter) is False
    assert starter_id not in adapter._discord_recovery_claim_heartbeats
    assert starter_id not in adapter._discord_recovery_message_channels
    assert adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "SELECT 1 FROM discord_messages WHERE message_id=?",
            (starter_id,),
        ).fetchone()
    ) is None


@pytest.mark.asyncio
async def test_active_foreign_lease_processes_older_work_and_schedules_rescan(
    adapter,
    monkeypatch,
):
    older_id, active_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    older = make_message(message_id=older_id, channel=channel)
    active = make_message(message_id=active_id, channel=channel)
    adapter.config.extra["free_response_channels"] = "123"

    owner = DiscordAdapter(PlatformConfig(enabled=True, token="owner"))
    owner._client = adapter._client
    assert owner._claim_live_discord_message(active) == "claimed"

    async def candidates(_channels):
        yield older
        yield active

    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )

    await adapter._run_missed_message_backfill()

    adapter._handle_message.assert_awaited_once_with(
        older,
        role_authorized=False,
        recovered=True,
    )
    assert adapter._discord_recovery_cursor("123") == str(older_id)
    retry_task = adapter._missed_message_backfill_retry_task
    assert retry_task is not None and not retry_task.done()
    retry_task.cancel()
    await asyncio.gather(retry_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unauthorized_triggers_do_not_consume_dispatch_bound(
    adapter,
    monkeypatch,
):
    ids = _recent_snowflakes(3)
    bot_user = adapter._client.user
    allowed = make_message(
        message_id=ids[0],
        author_id=42,
        content=f"<@{bot_user.id}> allowed",
        mentions=[bot_user],
    )
    denied = [
        make_message(
            message_id=message_id,
            author_id=100 + index,
            content=f"<@{bot_user.id}> denied",
            mentions=[bot_user],
        )
        for index, message_id in enumerate(ids[1:])
    ]
    monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setattr(
        adapter,
        "_is_allowed_user",
        lambda user_id, *_args, **_kwargs: user_id == "42",
    )

    async def candidates(_channels):
        for message in [allowed, *denied]:
            yield message

    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_missed_message_backfill_max_dispatches",
        lambda: 1,
    )

    await adapter._run_missed_message_backfill()

    adapter._handle_message.assert_awaited_once_with(
        allowed,
        role_authorized=False,
        recovered=True,
    )


@pytest.mark.asyncio
async def test_recovered_auto_thread_reuses_durable_routing_thread(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    assert adapter._claim_live_discord_message(message) == "claimed"
    adapter._record_discord_routing_thread(message_id, "777")
    existing_thread = SimpleNamespace(id=777)
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda channel_id: (
            existing_thread if channel_id == 777 else None
        ),
    )
    adapter.handle_message = AsyncMock()
    create_thread = AsyncMock()
    monkeypatch.setattr(adapter, "_auto_create_thread", create_thread)
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")

    admitted = await DiscordAdapter._handle_message(
        adapter,
        message,
        recovered=True,
    )

    assert admitted is True
    create_thread.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.source.thread_id == "777"


@pytest.mark.asyncio
async def test_auto_thread_failure_keeps_live_receipt_retryable(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    channel = FakeChannel(channel_id=123)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=9000))
    message = make_message(message_id=int(message_id), channel=channel)
    adapter._handle_message = DiscordAdapter._handle_message.__get__(
        adapter,
        DiscordAdapter,
    )
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(
        adapter,
        "_auto_create_thread",
        AsyncMock(return_value=None),
    )
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")

    with pytest.raises(
        RuntimeError,
        match="Discord auto-thread routing failed",
    ):
        await adapter._dispatch_discord_message(message)

    assert adapter._discord_recovery_cursor("123") is None
    assert adapter._discord_message_has_active_claim(message_id) is False


@pytest.mark.asyncio
async def test_failed_inline_clarification_does_not_advance_cursor(adapter):
    from tools import clarify_gateway

    message_id = str(_recent_snowflakes(1)[0])
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    event = MessageEvent(
        text="The second option",
        source=source,
        message_id=message_id,
        metadata={"_gateway_receipt_ids": [message_id]},
    )
    raw_message = make_message(message_id=int(message_id))
    event.raw_message = raw_message
    session_key = build_session_key(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._register_discord_recovery_receipt("123", message_id)
    assert adapter._claim_live_discord_message(raw_message) == "claimed"
    adapter._start_discord_processing_claim_heartbeats([message_id])
    adapter._message_handler = AsyncMock(
        side_effect=RuntimeError("clarification resolver failed")
    )
    clarify_gateway.register(
        "clarify-failure-recovery-test",
        session_key,
        "Which option?",
        ["The first option", "The second option"],
    )
    try:
        await adapter._dispatch_discord_event(event, recovered=False)
    finally:
        clarify_gateway.clear_session(session_key)
        adapter._active_sessions.pop(session_key, None)

    assert adapter._discord_recovery_cursor("123") is None
    assert adapter._discord_message_has_active_claim(message_id) is False


@pytest.mark.asyncio
async def test_live_silent_decision_cannot_skip_unregistered_backfill(
    adapter,
    monkeypatch,
):
    missed_id, live_id = _recent_snowflakes(2)
    channel = FakeChannel(channel_id=123)
    bot_user = adapter._client.user
    missed = make_message(
        message_id=missed_id,
        channel=channel,
        content=f"<@{bot_user.id}> recover this",
        mentions=[bot_user],
    )
    live = make_message(
        message_id=live_id,
        channel=channel,
        content="unmentioned ambient chatter",
    )
    routed_live = asyncio.Event()

    async def candidates(_channels):
        yield missed

    async def handle(message, **_kwargs):
        if message is live:
            routed_live.set()
            return False
        waiter = adapter._discord_recovery_waiters.get(str(message.id))
        if waiter is not None and not waiter.done():
            waiter.set_result(ProcessingOutcome.SUCCESS)
        return True

    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )
    adapter._handle_message = AsyncMock(side_effect=handle)
    adapter._discord_recovery_barrier.clear()

    live_task = asyncio.create_task(adapter._dispatch_discord_message(live))
    await routed_live.wait()

    assert live_task.done() is False
    assert adapter._discord_recovery_cursor("123") is None

    await adapter._run_missed_message_backfill()
    assert await live_task is False
    assert [
        call.args[0].id for call in adapter._handle_message.await_args_list
    ] == [live_id, missed_id]
    assert adapter._discord_recovery_cursor("123") == str(live_id)
    assert adapter._discord_message_has_active_claim(str(live_id)) is False


def test_completed_cursor_survives_state_store_restart(adapter):
    message_id = str(_recent_snowflakes(1)[0])
    adapter._advance_discord_recovery_cursor("123", message_id)

    restarted = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))

    assert restarted._discord_recovery_cursor("123") == message_id


@pytest.mark.asyncio
async def test_later_success_waits_behind_failed_receipt(adapter):
    first_id, second_id = [str(value) for value in _recent_snowflakes(2)]
    adapter._register_discord_recovery_receipt("123", first_id)
    adapter._register_discord_recovery_receipt("123", second_id)

    await adapter._set_discord_recovery_receipts(
        [second_id],
        ProcessingOutcome.SUCCESS,
    )
    await adapter._set_discord_recovery_receipts(
        [first_id],
        ProcessingOutcome.FAILURE,
    )

    assert adapter._discord_recovery_cursor("123") is None

    await adapter._set_discord_recovery_receipts(
        [first_id],
        ProcessingOutcome.SUCCESS,
    )

    assert adapter._discord_recovery_cursor("123") == second_id


@pytest.mark.asyncio
async def test_clarify_reply_bypasses_recovery_barrier(adapter):
    from tools import clarify_gateway

    message_id = str(_recent_snowflakes(1)[0])
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    event = MessageEvent(
        text="The second option",
        source=source,
        message_id=message_id,
        metadata={"_gateway_receipt_ids": [message_id]},
    )
    session_key = build_session_key(
        source,
        group_sessions_per_user=adapter.config.extra.get(
            "group_sessions_per_user",
            True,
        ),
        thread_sessions_per_user=adapter.config.extra.get(
            "thread_sessions_per_user",
            False,
        ),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._register_discord_recovery_receipt("123", message_id)
    adapter._discord_recovery_barrier.clear()
    adapter.handle_message = AsyncMock()
    clarify_gateway.register(
        "clarify-recovery-test",
        session_key,
        "Which option?",
        ["The first option", "The second option"],
    )
    try:
        await asyncio.wait_for(
            adapter._dispatch_discord_event(event, recovered=False),
            timeout=5,
        )
    finally:
        clarify_gateway.clear_session(session_key)
        adapter._active_sessions.pop(session_key, None)

    adapter.handle_message.assert_awaited_once_with(event)
    assert adapter._discord_recovery_cursor("123") == message_id


@pytest.mark.asyncio
async def test_session_finishing_during_barrier_does_not_complete_new_turn(
    adapter,
):
    message_id = str(_recent_snowflakes(1)[0])
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    event = MessageEvent(
        text="start the next task",
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
        metadata={"_gateway_receipt_ids": [message_id]},
    )
    session_key = build_session_key(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._register_discord_recovery_receipt("123", message_id)
    adapter._discord_recovery_barrier.clear()

    async def start_new_turn(_event):
        adapter._active_sessions[session_key] = asyncio.Event()

    adapter.handle_message = AsyncMock(side_effect=start_new_turn)
    dispatch = asyncio.create_task(
        adapter._dispatch_discord_event(event, recovered=False)
    )
    await asyncio.sleep(0)
    assert dispatch.done() is False

    adapter._active_sessions.pop(session_key, None)
    adapter._discord_recovery_barrier.set()
    await dispatch

    adapter.handle_message.assert_awaited_once_with(event)
    assert adapter._discord_recovery_cursor("123") is None
    assert (
        adapter._discord_recovery_receipts["123"][message_id]
        == "pending"
    )
    adapter._active_sessions.pop(session_key, None)


@pytest.mark.asyncio
async def test_recovered_turn_waiting_for_clarification_does_not_deadlock(
    adapter,
):
    from tools import clarify_gateway

    original_id, reply_id = [
        str(value) for value in _recent_snowflakes(2)
    ]
    message = make_message(message_id=int(original_id))
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    session_key = build_session_key(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    original_event = MessageEvent(
        text=message.content,
        message_type=MessageType.TEXT,
        source=source,
        raw_message=message,
        message_id=original_id,
        metadata={"_gateway_receipt_ids": [original_id]},
    )
    reply_event = MessageEvent(
        text="The second option",
        message_type=MessageType.TEXT,
        source=source,
        message_id=reply_id,
        metadata={"_gateway_receipt_ids": [reply_id]},
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._handle_message = AsyncMock(return_value=True)
    adapter._register_discord_recovery_receipt("123", reply_id)
    adapter._discord_recovery_barrier.clear()
    clarify_gateway.register(
        "clarify-recovered-turn-test",
        session_key,
        "Which option?",
        ["The first option", "The second option"],
    )

    recovered = asyncio.create_task(
        adapter._dispatch_serialized_discord_message(
            message,
            role_authorized=False,
        )
    )
    while original_id not in adapter._discord_recovery_waiters:
        await asyncio.sleep(0)
    await adapter.on_processing_start(original_event)

    async def resolve_original(_event):
        await adapter.on_processing_complete(
            original_event,
            ProcessingOutcome.SUCCESS,
        )

    adapter.handle_message = AsyncMock(side_effect=resolve_original)
    try:
        assert adapter._discord_recovery_cursor("123") is None
        await asyncio.wait_for(
            adapter._dispatch_discord_event(
                reply_event,
                recovered=False,
            ),
            timeout=5,
        )
        assert await asyncio.wait_for(recovered, timeout=5) == (
            ProcessingOutcome.SUCCESS
        )
    finally:
        clarify_gateway.clear_session(session_key)
        adapter._active_sessions.pop(session_key, None)

    adapter.handle_message.assert_awaited_once_with(reply_event)
    assert adapter._discord_recovery_cursor("123") == reply_id


@pytest.mark.asyncio
async def test_backfill_dispatches_historical_clarification_answer(
    adapter,
    monkeypatch,
):
    from tools import clarify_gateway

    original_id, answer_id = [
        str(value) for value in _recent_snowflakes(2)
    ]
    parent_channel = FakeChannel(channel_id=123)
    thread_channel = FakeChannel(channel_id=456, parent_id=123)
    original = make_message(
        message_id=int(original_id),
        channel=parent_channel,
    )
    answer = make_message(
        message_id=int(answer_id),
        content="The second option",
        channel=thread_channel,
    )
    session_key = "discord:recovered-control-lane"
    release_original = asyncio.Event()
    dispatched: list[str] = []

    async def candidates(_channels):
        yield original
        yield answer

    async def dispatch(message):
        message_id = str(message.id)
        dispatched.append(message_id)
        started = adapter._discord_recovery_started_events.get(message_id)
        if message_id == original_id:
            adapter._discord_recovery_session_keys[message_id] = session_key
            adapter._active_sessions[session_key] = asyncio.Event()
            clarify_gateway.register(
                "historical-clarification-answer",
                session_key,
                "Which option?",
                ["The first option", "The second option"],
            )
            if started is not None:
                started.set()
            await release_original.wait()
            return ProcessingOutcome.SUCCESS
        assert (
            clarify_gateway.get_pending_for_session(
                session_key,
                include_choice_prompts=True,
            )
            is not None
        )
        if started is not None:
            started.set()
        release_original.set()
        return ProcessingOutcome.SUCCESS

    adapter.config.extra["free_response_channels"] = "123"
    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123", "456"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(adapter, "_dispatch_recovered_message", dispatch)
    try:
        await asyncio.wait_for(
            adapter._run_missed_message_backfill(),
            timeout=5,
        )
    finally:
        clarify_gateway.clear_session(session_key)
        adapter._active_sessions.pop(session_key, None)

    assert dispatched == [original_id, answer_id]


@pytest.mark.asyncio
async def test_backfill_parks_unanswered_historical_clarification(
    adapter,
    monkeypatch,
):
    from tools import clarify_gateway

    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    session_key = "discord:parked-recovered-control"
    release = asyncio.Event()

    async def candidates(_channels):
        yield message

    async def dispatch(recovered_message):
        adapter._discord_recovery_session_keys[message_id] = session_key
        adapter._active_sessions[session_key] = asyncio.Event()
        clarify_gateway.register(
            "parked-historical-clarification",
            session_key,
            "Which option?",
            ["one", "two"],
        )
        started = adapter._discord_recovery_started_events.get(message_id)
        if started is not None:
            started.set()
        await release.wait()
        return ProcessingOutcome.SUCCESS

    adapter.config.extra["free_response_channels"] = "123"
    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        adapter,
        "_dispatch_recovered_message",
        dispatch,
    )
    try:
        await asyncio.wait_for(
            adapter._run_missed_message_backfill(),
            timeout=5,
        )
        parked = [
            task
            for task in adapter._background_tasks
            if not task.done()
        ]
        assert len(parked) == 1
        assert adapter._discord_recovery_cursor("123") is None
        release.set()
        await asyncio.wait_for(parked[0], timeout=5)
    finally:
        release.set()
        clarify_gateway.clear_session(session_key)
        adapter._active_sessions.pop(session_key, None)


@pytest.mark.asyncio
async def test_plain_approval_reply_bypasses_recovery_barrier(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    event = MessageEvent(
        text="yes",
        source=source,
        message_id=message_id,
        metadata={"_gateway_receipt_ids": [message_id]},
    )
    session_key = build_session_key(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    adapter._register_discord_recovery_receipt("123", message_id)
    adapter._discord_recovery_barrier.clear()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(
        "tools.approval.has_blocking_approval",
        lambda key: key == session_key,
    )
    try:
        await asyncio.wait_for(
            adapter._dispatch_discord_event(event, recovered=False),
            timeout=5,
        )
    finally:
        adapter._active_sessions.pop(session_key, None)

    adapter.handle_message.assert_awaited_once_with(event)
    assert adapter._discord_recovery_cursor("123") == message_id


@pytest.mark.asyncio
async def test_parent_cursor_does_not_bound_child_thread(adapter):
    class TrackingChannel(FakeChannel):
        def __init__(self, channel_id):
            super().__init__(channel_id=channel_id)
            self.threads = []
            self.history_calls = []

        def history(self, **kwargs):
            self.history_calls.append(kwargs)

            async def _gen():
                if False:
                    yield None

            return _gen()

    parent = TrackingChannel(123)
    child = TrackingChannel(456)
    parent.threads.append(child)
    parent_cursor = str(_recent_snowflakes(1)[0])
    adapter._advance_discord_recovery_cursor("123", parent_cursor)
    time_cutoff = datetime.now(timezone.utc) - dt.timedelta(minutes=5)

    messages = [
        message
        async for message in adapter._iter_channel_and_thread_messages(
            parent,
            limit=10,
            after=time_cutoff,
            seen_channels=set(),
        )
    ]

    assert messages == []
    assert child.history_calls[0]["after"] is time_cutoff


def test_busy_turn_merge_preserves_all_discord_receipts():
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    first = MessageEvent(
        text="first",
        source=source,
        message_id="101",
        metadata={"_gateway_receipt_ids": ["101"]},
    )
    second = MessageEvent(
        text="second",
        source=source,
        message_id="102",
        metadata={"_gateway_receipt_ids": ["102"]},
    )
    pending = {"session": first}

    merge_pending_message_event(
        pending,
        "session",
        second,
        merge_text=True,
    )

    assert pending["session"].text == "first\nsecond"
    assert pending["session"].metadata["_gateway_receipt_ids"] == [
        "101",
        "102",
    ]


@pytest.mark.asyncio
async def test_nonmergeable_pending_replacement_transfers_discord_receipts(
    adapter,
):
    first_id, second_id = [
        str(value) for value in _recent_snowflakes(2)
    ]
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="channel",
        user_id="42",
    )
    first = MessageEvent(
        text="/status",
        message_type=MessageType.COMMAND,
        source=source,
        message_id=first_id,
        metadata={"_gateway_receipt_ids": [first_id]},
    )
    second = MessageEvent(
        text="/help",
        message_type=MessageType.COMMAND,
        source=source,
        message_id=second_id,
        metadata={"_gateway_receipt_ids": [second_id]},
    )
    pending = {}
    adapter._register_discord_recovery_receipt("123", first_id)
    adapter._register_discord_recovery_receipt("123", second_id)

    merge_pending_message_event(pending, "session", first)
    merge_pending_message_event(pending, "session", second)

    replacement = pending["session"]
    assert replacement is second
    assert replacement.metadata["_gateway_receipt_ids"] == [
        first_id,
        second_id,
    ]

    await adapter._set_discord_recovery_receipts(
        replacement.metadata["_gateway_receipt_ids"],
        ProcessingOutcome.SUCCESS,
    )
    assert adapter._discord_recovery_cursor("123") == second_id


@pytest.mark.asyncio
async def test_empty_backfill_is_noop(adapter, monkeypatch):
    async def candidates(_channels):
        if False:
            yield None

    monkeypatch.setattr(
        adapter, "_known_missed_message_backfill_channels", AsyncMock(return_value={"123"})
    )
    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", candidates)

    await adapter._run_missed_message_backfill()

    adapter._handle_message.assert_not_awaited()
    assert adapter._discord_recovery_cursor("123") is None


@pytest.mark.asyncio
async def test_ignored_only_backfill_advances_cursor_after_policy_decision(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))

    async def candidates(_channels):
        yield message

    monkeypatch.setattr(
        adapter,
        "_known_missed_message_backfill_channels",
        AsyncMock(return_value={"123"}),
    )
    monkeypatch.setattr(
        adapter,
        "_iter_missed_message_backfill_candidates",
        candidates,
    )
    monkeypatch.setattr(
        adapter,
        "_should_backfill_discord_message",
        AsyncMock(return_value=True),
    )

    await adapter._run_missed_message_backfill()

    adapter._handle_message.assert_not_awaited()
    assert adapter._discord_recovery_cursor("123") == message_id
    assert adapter._discord_message_is_persistently_complete(message_id)


def test_discovered_receipt_blocks_newer_cross_process_cursor(adapter):
    older_id, newer_id = [
        str(value) for value in _recent_snowflakes(2)
    ]
    message = make_message(message_id=int(older_id))

    assert adapter._record_discord_message_seen(
        message,
        status="discovered",
    )
    assert (
        adapter._advance_discord_recovery_cursor_if_unblocked(
            "123",
            newer_id,
        )
        is False
    )
    assert adapter._discord_recovery_cursor("123") is None


@pytest.mark.asyncio
async def test_incomplete_auto_thread_receipt_restores_parent_scan_lane(
    adapter,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    assert adapter._claim_live_discord_message(message) == "claimed"
    adapter._record_discord_routing_thread(message_id, "456")
    adapter._record_discord_claim_outcome(
        [message_id],
        ProcessingOutcome.FAILURE,
    )

    channels = await adapter._known_missed_message_backfill_channels()

    assert "123" in channels
    assert "456" in channels


@pytest.mark.asyncio
async def test_live_claim_contention_keeps_durable_pending_and_schedules_scan(
    adapter,
    monkeypatch,
):
    message_id = str(_recent_snowflakes(1)[0])
    message = make_message(message_id=int(message_id))
    scheduled = MagicMock()
    monkeypatch.setattr(
        adapter,
        "_claim_live_discord_message",
        lambda _message: "active",
    )
    monkeypatch.setattr(
        adapter,
        "_discord_message_has_active_claim",
        lambda _message_id: False,
    )
    monkeypatch.setattr(
        adapter,
        "_schedule_discord_recovery_retry",
        scheduled,
    )

    assert await adapter._dispatch_discord_message(message) is False

    status = adapter._with_discord_recovery_db(
        lambda conn: conn.execute(
            "SELECT status FROM discord_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()[0]
    )
    assert status == "discovered"
    scheduled.assert_called_once_with(0.1)


@pytest.mark.asyncio
async def test_backfill_never_dispatches_bot_own_webhook_or_system_messages(
    adapter, monkeypatch
):
    ids = _recent_snowflakes(4)
    channel = FakeChannel(channel_id=123)
    bot = make_bot_message(message_id=ids[0], channel=channel)
    own = make_message(message_id=ids[1], channel=channel)
    own.author = adapter._client.user
    webhook = make_message(message_id=ids[2], channel=channel)
    webhook.webhook_id = "hook"
    system = make_message(message_id=ids[3], channel=channel)
    system.type = SimpleNamespace(name="system")

    async def candidates(_channels):
        for message in (bot, own, webhook, system):
            yield message

    should_backfill = AsyncMock(return_value=True)
    monkeypatch.setattr(
        adapter, "_known_missed_message_backfill_channels", AsyncMock(return_value={"123"})
    )
    monkeypatch.setattr(adapter, "_iter_missed_message_backfill_candidates", candidates)
    monkeypatch.setattr(adapter, "_should_backfill_discord_message", should_backfill)

    await adapter._run_missed_message_backfill()

    should_backfill.assert_not_awaited()
    adapter._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_age_bound_logs_and_establishes_safe_cursor(adapter, monkeypatch, caplog):
    old_id = _snowflake_at(datetime.now(timezone.utc) - dt.timedelta(hours=8))
    adapter._advance_discord_recovery_cursor("123", str(old_id))
    channel = FakeChannel(channel_id=123, history_messages=[])
    adapter._client.get_channel = lambda _channel_id: channel

    with caplog.at_level("WARNING"):
        got = [
            message
            async for message in adapter._iter_missed_message_backfill_candidates(
                {"123"}
            )
        ]

    assert got == []
    assert int(adapter._discord_recovery_cursor("123")) > old_id
    assert "age bound skipped history" in caplog.text


@pytest.mark.asyncio
async def test_age_bound_does_not_cross_active_claim(adapter, caplog):
    old_cursor = _snowflake_at(
        datetime.now(timezone.utc) - dt.timedelta(hours=8)
    )
    active_id = _snowflake_at(
        datetime.now(timezone.utc) - dt.timedelta(hours=7)
    )
    adapter._advance_discord_recovery_cursor("123", str(old_cursor))
    channel = FakeChannel(channel_id=123)
    active = make_message(message_id=active_id, channel=channel)
    adapter._record_discord_message_seen(active, status="processing")
    adapter._client.get_channel = lambda _channel_id: channel

    with caplog.at_level("WARNING"):
        got = [
            message
            async for message in adapter._iter_missed_message_backfill_candidates(
                {"123"}
            )
        ]

    assert got == []
    assert adapter._discord_recovery_cursor("123") == str(old_cursor)
    assert "age boundary" in caplog.text
    assert "deferred behind active durable work" in caplog.text
