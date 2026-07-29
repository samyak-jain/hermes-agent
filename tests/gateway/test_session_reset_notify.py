"""Tests for session auto-reset notifications.

Verifies that:
- _should_reset() returns a reason string ("idle" or "daily") instead of bool
- SessionEntry captures auto_reset_reason
- SessionResetPolicy.notify controls whether notifications are sent
- notify_exclude_platforms skips notifications for excluded platforms
- resume_pending_expired auto-reset sets the correct reason and DB end_reason
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import hermes_time
import pytest

from gateway.config import (
    GatewayConfig,
    Platform,
    SessionResetPolicy,
)
from gateway.session import (
    SessionEntry,
    SessionSource,
    SessionStore,
    _daily_reset_boundary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform=Platform.TELEGRAM, chat_id="123", user_id="u1"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        user_id=user_id,
    )


def _make_store(policy=None, tmp_path=None, has_active_processes_fn=None):
    config = GatewayConfig()
    if policy:
        config.default_reset_policy = policy
    store = SessionStore(
        sessions_dir=tmp_path or "/tmp/test-sessions",
        config=config,
        has_active_processes_fn=has_active_processes_fn,
    )
    return store


@pytest.fixture
def configure_timezone(monkeypatch):
    def configure(name):
        monkeypatch.setenv("HERMES_TIMEZONE", name)
        hermes_time.reset_cache()

    yield configure
    hermes_time.reset_cache()


def _daily_reset_decision(store, entry, source, now, path):
    with patch("gateway.session._now", return_value=now):
        if path == "proactive":
            return store._is_session_expired(entry)
        return store._should_reset(entry, source)


# ---------------------------------------------------------------------------
# _should_reset returns reason string
# ---------------------------------------------------------------------------

class TestShouldResetReason:
    def test_returns_none_when_not_expired(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="both", idle_minutes=60, at_hour=4),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),  # just updated
        )
        source = _make_source()
        assert store._should_reset(entry, source) is None

    def test_returns_idle_when_idle_expired(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=30),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=1),  # 60min ago > 30min threshold
        )
        source = _make_source()
        assert store._should_reset(entry, source) == "idle"

    def test_returns_daily_when_daily_boundary_crossed(self, tmp_path):
        now = datetime.now()
        store = _make_store(
            SessionResetPolicy(mode="daily", at_hour=now.hour),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(days=1),  # last active yesterday
        )
        source = _make_source()
        assert store._should_reset(entry, source) == "daily"

    def test_daily_boundary_uses_configured_timezone(self, tmp_path, monkeypatch):
        """04:00 Asia/Kolkata must not be interpreted as 04:00 UTC."""
        monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Kolkata")
        hermes_time.reset_cache()
        try:
            # 22:45 UTC is 04:15 the following day in Asia/Kolkata. Activity
            # at 22:25 UTC (03:55 IST) is therefore across the 04:00 boundary.
            now = datetime(2026, 7, 28, 22, 45, tzinfo=timezone.utc)
            store = _make_store(
                SessionResetPolicy(mode="daily", at_hour=4),
                tmp_path,
            )
            entry = SessionEntry(
                session_key="test",
                session_id="s1",
                created_at=datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc),
                updated_at=datetime(2026, 7, 28, 22, 25, tzinfo=timezone.utc),
                platform=Platform.TELEGRAM,
            )
            source = _make_source()

            with patch("gateway.session._now", return_value=now):
                assert store._should_reset(entry, source) == "daily"
                assert store._is_session_expired(entry) is True

                monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
                hermes_time.reset_cache()
                assert store._should_reset(entry, source) is None
                assert store._is_session_expired(entry) is False
        finally:
            hermes_time.reset_cache()

    def test_fall_back_boundary_is_monotonic(
        self,
        configure_timezone,
    ):
        """The first 01:00 occurs once and remains the day's boundary."""
        configure_timezone("America/New_York")
        moments = [
            datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc),  # 00:30
            datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),   # 01:00 fold=0
            datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),  # 01:30 fold=0
            datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),  # 01:30 fold=1
            datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc),  # 02:30
        ]
        boundaries = [_daily_reset_boundary(now, 1) for now in moments]

        configured_tz = hermes_time.get_timezone()
        assert [moment.astimezone(configured_tz).fold for moment in moments[2:4]] == [
            0,
            1,
        ]
        assert boundaries == [
            datetime(2026, 10, 31, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
        ]
        assert boundaries == sorted(boundaries)

    @pytest.mark.parametrize("path", ["proactive", "routing"])
    @pytest.mark.parametrize(
        ("now", "updated_at", "expected"),
        [
            (
                datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 4, 59, tzinfo=timezone.utc),
                True,
            ),
            (
                datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 4, 59, tzinfo=timezone.utc),
                True,
            ),
            (
                datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
                False,
            ),
            (
                datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 5, 15, tzinfo=timezone.utc),
                False,
            ),
            (
                datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc),
                datetime(2026, 11, 1, 5, 15, tzinfo=timezone.utc),
                False,
            ),
        ],
        ids=[
            "fold-0-before-boundary",
            "fold-1-before-boundary",
            "exact-boundary",
            "fold-1-activity-after-boundary",
            "after-fold-activity-after-boundary",
        ],
    )
    def test_fall_back_resets_once_in_both_paths(
        self,
        tmp_path,
        configure_timezone,
        path,
        now,
        updated_at,
        expected,
    ):
        configure_timezone("America/New_York")
        store = _make_store(
            SessionResetPolicy(mode="daily", at_hour=1),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
            updated_at=updated_at,
            platform=Platform.TELEGRAM,
        )

        decision = _daily_reset_decision(
            store,
            entry,
            _make_source(),
            now,
            path,
        )
        expected_decision = (
            expected if path == "proactive" else "daily" if expected else None
        )
        assert decision == expected_decision

    @pytest.mark.parametrize("path", ["proactive", "routing"])
    def test_spring_forward_nonexistent_hour_resets_once(
        self,
        tmp_path,
        configure_timezone,
        path,
    ):
        """02:00 normalizes to 03:00 EDT, one instant for the local date."""
        configure_timezone("America/New_York")
        before = datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)
        boundary = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
        after = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)

        assert _daily_reset_boundary(before, 2) == datetime(
            2026, 3, 7, 7, 0, tzinfo=timezone.utc,
        )
        assert _daily_reset_boundary(boundary, 2) == boundary
        assert _daily_reset_boundary(after, 2) == boundary

        store = _make_store(
            SessionResetPolicy(mode="daily", at_hour=2),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime(2026, 3, 7, tzinfo=timezone.utc),
            updated_at=datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc),
            platform=Platform.TELEGRAM,
        )
        assert _daily_reset_decision(
            store, entry, _make_source(), boundary, path,
        ) == (True if path == "proactive" else "daily")

        for updated_at in (
            boundary,
            datetime(2026, 3, 8, 7, 15, tzinfo=timezone.utc),
        ):
            entry.updated_at = updated_at
            assert _daily_reset_decision(
                store, entry, _make_source(), after, path,
            ) == (False if path == "proactive" else None)

    def test_non_dst_boundary_is_stable(
        self,
        configure_timezone,
    ):
        configure_timezone("Asia/Kolkata")
        moments = [
            datetime(2026, 7, 28, 22, 29, tzinfo=timezone.utc),
            datetime(2026, 7, 28, 22, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        ]

        assert [_daily_reset_boundary(now, 4) for now in moments] == [
            datetime(2026, 7, 27, 22, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 28, 22, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 28, 22, 30, tzinfo=timezone.utc),
        ]

    def test_returns_none_when_mode_is_none(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="none"),
            tmp_path,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(days=30),
            updated_at=datetime.now() - timedelta(days=30),
        )
        source = _make_source()
        assert store._should_reset(entry, source) is None

    def test_returns_none_when_active_process_check_raises(self, tmp_path):
        def _raise(_session_key):
            raise RuntimeError("process registry unavailable")

        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=30),
            tmp_path,
            has_active_processes_fn=_raise,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=1),
        )
        source = _make_source()

        assert store._should_reset(entry, source) is None

    def test_is_session_expired_fails_closed_when_active_process_check_raises(self, tmp_path):
        def _raise(_session_key):
            raise RuntimeError("process registry unavailable")

        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=30),
            tmp_path,
            has_active_processes_fn=_raise,
        )
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            platform=Platform.TELEGRAM,
            chat_type="dm",
            created_at=datetime.now() - timedelta(hours=2),
            updated_at=datetime.now() - timedelta(hours=1),
        )

        assert store._is_session_expired(entry) is False


# ---------------------------------------------------------------------------
# SessionEntry captures reason
# ---------------------------------------------------------------------------

class TestSessionEntryReason:
    def test_auto_reset_reason_stored(self, tmp_path):
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        # Create initial session
        entry1 = store.get_or_create_session(source)
        assert not entry1.was_auto_reset

        # Age it past the idle threshold
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        # Next call should create a new session with reason
        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.auto_reset_reason == "idle"
        assert entry2.session_id != entry1.session_id

    def test_reset_had_activity_false_when_no_tokens(self, tmp_path):
        """Expired session with no tokens → reset_had_activity=False."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry1 = store.get_or_create_session(source)
        # No tokens used — session was idle with no conversation
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is False

    def test_reset_had_activity_true_when_tokens_used(self, tmp_path):
        """Expired session with tokens → reset_had_activity=True."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry1 = store.get_or_create_session(source)
        # Simulate some conversation happened (last_prompt_tokens is the field
        # written on every turn; total_tokens is never persisted).
        entry1.last_prompt_tokens = 5000
        entry1.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.reset_had_activity is True


# ---------------------------------------------------------------------------
# SessionResetPolicy notify config
# ---------------------------------------------------------------------------

class TestResetPolicyNotify:
    def test_notify_defaults_true(self):
        policy = SessionResetPolicy()
        assert policy.notify is True

    def test_notify_exclude_defaults(self):
        policy = SessionResetPolicy()
        assert "api_server" in policy.notify_exclude_platforms
        assert "webhook" in policy.notify_exclude_platforms

    def test_from_dict_with_notify_false(self):
        policy = SessionResetPolicy.from_dict({"notify": False})
        assert policy.notify is False

    def test_from_dict_with_custom_excludes(self):
        policy = SessionResetPolicy.from_dict({
            "notify_exclude_platforms": ["api_server", "webhook", "homeassistant"],
        })
        assert "homeassistant" in policy.notify_exclude_platforms

    def test_from_dict_preserves_defaults_on_missing_keys(self):
        policy = SessionResetPolicy.from_dict({})
        assert policy.notify is True
        assert "api_server" in policy.notify_exclude_platforms

    def test_to_dict_roundtrip(self):
        original = SessionResetPolicy(
            mode="idle",
            notify=False,
            notify_exclude_platforms=("api_server",),
        )
        restored = SessionResetPolicy.from_dict(original.to_dict())
        assert restored.notify == original.notify
        assert restored.notify_exclude_platforms == original.notify_exclude_platforms
        assert restored.mode == original.mode


# ---------------------------------------------------------------------------
# SessionEntry to_dict / from_dict roundtrip for auto-reset fields
# ---------------------------------------------------------------------------

class TestSessionEntryAutoResetRoundtrip:
    def test_was_auto_reset_persists_across_roundtrip(self, tmp_path):
        """was_auto_reset=True survives to_dict() → from_dict() (gateway restart)."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        entry.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.was_auto_reset is True
        assert entry2.auto_reset_reason == "idle"
        assert entry2.session_id != entry.session_id

        # Simulate gateway restart: reload from disk
        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry2.session_key)
        assert reloaded is not None
        assert reloaded.was_auto_reset is True
        assert reloaded.auto_reset_reason == "idle"

    def test_reset_had_activity_persists_across_roundtrip(self, tmp_path):
        """reset_had_activity survives to_dict() → from_dict() (gateway restart)."""
        store = _make_store(
            SessionResetPolicy(mode="idle", idle_minutes=1),
            tmp_path,
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        entry.last_prompt_tokens = 1000
        entry.updated_at = datetime.now() - timedelta(minutes=5)
        store._save()

        entry2 = store.get_or_create_session(source)
        assert entry2.reset_had_activity is True

        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry2.session_key)
        assert reloaded is not None
        assert reloaded.reset_had_activity is True

    def test_auto_reset_reason_none_roundtrip(self, tmp_path):
        """auto_reset_reason=None (no reset) survives roundtrip cleanly."""
        store = _make_store(tmp_path=tmp_path)
        source = _make_source()

        entry = store.get_or_create_session(source)
        assert entry.was_auto_reset is False

        store._loaded = False
        store._entries.clear()
        store._ensure_loaded()

        reloaded = store._entries.get(entry.session_key)
        assert reloaded is not None
        assert reloaded.was_auto_reset is False
        assert reloaded.auto_reset_reason is None
        assert reloaded.reset_had_activity is False


# ---------------------------------------------------------------------------
# resume_pending_expired: auto_reset_reason and DB end_reason (#58933)
# ---------------------------------------------------------------------------

def _make_db_mock() -> MagicMock:
    """Return a SessionDB mock with safe defaults for all lookup methods."""
    db = MagicMock()
    db.get_session.return_value = None
    db.get_compression_tip.return_value = None  # avoids MagicMock leaking into session_id
    db.find_latest_gateway_session_for_peer.return_value = None
    db.reopen_session.return_value = None
    db.create_session.return_value = None
    return db


def _make_store_with_db(tmp_path, db_mock=None, policy=None) -> SessionStore:
    """Build a SessionStore with a mock SessionDB, bypassing disk load."""
    cfg_policy = policy or SessionResetPolicy(mode="none")
    config = GatewayConfig(default_reset_policy=cfg_policy)
    with patch("gateway.session.SessionStore._ensure_loaded"):
        store = SessionStore(sessions_dir=tmp_path, config=config)
    store._db = db_mock if db_mock is not None else _make_db_mock()
    store._loaded = True
    return store


class TestResumePendingExpiredAutoReset:
    """resume_pending sessions past the freshness window should fire
    was_auto_reset=True with auto_reset_reason='resume_pending_expired' and
    persist that reason to state.db (#58933)."""

    def _seed_stale_resume_pending(self, store, source, freshness_seconds=3600):
        """Create a session, mark it resume_pending, then backdate the mark
        past the freshness window so get_or_create_session treats it as a
        zombie."""
        entry = store.get_or_create_session(source)
        store.mark_resume_pending(entry.session_key)
        with store._lock:
            entry = store._entries[entry.session_key]
            entry.last_resume_marked_at = (
                datetime.now() - timedelta(seconds=freshness_seconds + 60)
            )
            entry.updated_at = datetime.now()  # keep updated_at fresh
            store._save()
        return entry

    def test_stale_resume_pending_sets_auto_reset_reason(
        self, tmp_path, monkeypatch
    ):
        """Stale resume_pending triggers was_auto_reset=True with reason
        'resume_pending_expired', NOT 'idle'."""
        monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
        # The freshness gate requires an opted-in reset policy — mode "none"
        # disables it entirely (#61052). Use a huge idle window so only the
        # freshness gate (not the idle policy) can fire.
        store = _make_store_with_db(
            tmp_path,
            policy=SessionResetPolicy(mode="idle", idle_minutes=999999),
        )
        source = _make_source()

        old = self._seed_stale_resume_pending(store, source)

        new = store.get_or_create_session(source)

        assert new.session_id != old.session_id, "should have created a new session"
        assert new.was_auto_reset is True
        assert new.auto_reset_reason == "resume_pending_expired"

    def test_stale_resume_pending_had_activity_flag(
        self, tmp_path, monkeypatch
    ):
        """reset_had_activity reflects whether the old session was used."""
        monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
        store = _make_store_with_db(
            tmp_path,
            policy=SessionResetPolicy(mode="idle", idle_minutes=999999),
        )
        source = _make_source()

        old = self._seed_stale_resume_pending(store, source)
        # Simulate some conversation on the old session.
        with store._lock:
            old.last_prompt_tokens = 50_000
            store._save()

        new = store.get_or_create_session(source)
        assert new.reset_had_activity is True

    def test_stale_resume_pending_db_end_reason_is_specific(
        self, tmp_path, monkeypatch
    ):
        """state.db must record end_reason='resume_pending_expired', NOT the
        generic 'session_reset', so the event is auditable (#58933 fix)."""
        monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "3600")
        db = _make_db_mock()
        store = _make_store_with_db(
            tmp_path, db,
            policy=SessionResetPolicy(mode="idle", idle_minutes=999999),
        )
        source = _make_source()

        old = self._seed_stale_resume_pending(store, source)
        store.get_or_create_session(source)

        # Auto-reset now writes through promote_to_session_reset so an
        # accidental agent_close end can't shadow the reset boundary.
        db.promote_to_session_reset.assert_called_once()
        ended_id, ended_reason = db.promote_to_session_reset.call_args.args
        assert ended_id == old.session_id
        assert ended_reason == "resume_pending_expired", (
            f"expected 'resume_pending_expired', got {ended_reason!r} — "
            "the DB end_reason must not be the generic 'session_reset'"
        )

    def test_idle_reset_db_end_reason_reflects_idle(
        self, tmp_path
    ):
        """Regular idle auto-reset persists 'idle' as end_reason so that all
        auto-reset paths are auditable (#58933 should not regress the common
        idle/daily path)."""
        db = _make_db_mock()
        store = _make_store_with_db(
            tmp_path, db, policy=SessionResetPolicy(mode="idle", idle_minutes=1)
        )
        source = _make_source()

        entry = store.get_or_create_session(source)
        # Age past idle threshold.
        with store._lock:
            entry.updated_at = datetime.now() - timedelta(minutes=5)
            store._save()

        store.get_or_create_session(source)

        db.promote_to_session_reset.assert_called_once()
        _, ended_reason = db.promote_to_session_reset.call_args.args
        assert ended_reason == "idle"

    def test_freshness_disabled_skips_resume_pending_expired(
        self, tmp_path, monkeypatch
    ):
        """When gateway_auto_continue_freshness=0, resume_pending is never
        expired — the same session is returned regardless of age."""
        monkeypatch.setenv("HERMES_AUTO_CONTINUE_FRESHNESS", "0")
        db = _make_db_mock()
        store = _make_store_with_db(tmp_path, db)
        source = _make_source()

        old = self._seed_stale_resume_pending(store, source, freshness_seconds=999_999)

        refreshed = store.get_or_create_session(source)
        # Freshness disabled → same session, no DB end_session call.
        assert refreshed.session_id == old.session_id
        db.end_session.assert_not_called()
        db.promote_to_session_reset.assert_not_called()
