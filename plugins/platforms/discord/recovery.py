"""Durable state for Discord reconnect message recovery."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import sqlite3
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_FILENAME = "discord_message_recovery.db"
_RETENTION_DAYS = 30


class DiscordClaimGuard:
    """Per-message cross-process lock held while a Discord side effect runs."""

    def __init__(
        self,
        handle: Any,
        local_lock: threading.Lock,
    ) -> None:
        self.handle = handle
        self.local_lock = local_lock


class DiscordRecoveryStore:
    """Small profile-scoped SQLite ledger for completed Discord messages."""

    def __init__(self, hermes_home: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._hermes_home = Path(hermes_home or get_hermes_home())
        self._message_locks: dict[str, threading.Lock] = {}

    def path(self) -> Path:
        directory = self._hermes_home / "gateway"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / _DB_FILENAME

    def call(self, fn: Callable[[sqlite3.Connection], Any], default: Any = None) -> Any:
        try:
            with self._lock:
                path = self.path()
                conn = sqlite3.connect(path, timeout=0.1)
                try:
                    if not self._initialized:
                        self._initialize(conn)
                        self._initialized = True
                        with suppress(OSError):
                            os.chmod(path, 0o600)
                    result = fn(conn)
                    conn.commit()
                    return result
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("Discord recovery ledger unavailable: %s", exc)
            return default

    def acquire_claim_guard(
        self,
        message_id: str,
        claim_owner: str,
        claim_epoch: int,
        *,
        allow_responded: bool = False,
    ) -> DiscordClaimGuard | None:
        """Fence one owned side effect without locking the whole SQLite DB."""
        guard = self.acquire_message_lock(message_id)
        if guard is None:
            return None

        def _owned(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """
                    SELECT 1
                      FROM discord_messages
                     WHERE message_id=?
                       AND claim_owner=?
                       AND claim_epoch=?
                       AND (
                           status IN ('queued', 'processing')
                           OR (? AND status='responded')
                       )
                    """,
                (
                    message_id,
                    claim_owner,
                    claim_epoch,
                    1 if allow_responded else 0,
                ),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                UPDATE discord_messages
                   SET updated_at=?
                 WHERE message_id=?
                   AND claim_owner=?
                   AND claim_epoch=?
                """,
                (
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    message_id,
                    claim_owner,
                    claim_epoch,
                ),
            )
            return True

        if self.call(_owned, False):
            return guard
        self.release_claim_guard(guard)
        return None

    def acquire_message_lock(
        self,
        message_id: str,
        *,
        timeout: float = 5.0,
    ) -> DiscordClaimGuard | None:
        """Acquire a lock scoped to one Discord snowflake.

        SQLite has database-wide writer locks, so holding ``BEGIN IMMEDIATE``
        across a network await stalls unrelated channel ingress. A small
        advisory lock file gives the same crash-released, cross-process fence
        while allowing other message IDs to keep claiming and completing.
        """
        key = hashlib.sha256(str(message_id).encode()).hexdigest()
        with self._lock:
            local_lock = self._message_locks.setdefault(
                key,
                threading.Lock(),
            )
        if not local_lock.acquire(timeout=max(0.0, timeout)):
            return None

        lock_dir = self._hermes_home / "gateway" / "discord_recovery_locks"
        handle = None
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
            handle = (lock_dir / f"{key}.lock").open("a+b")
            with suppress(OSError):
                os.chmod(handle.name, 0o600)
            try:
                import fcntl
            except ImportError:
                # Windows has no fcntl; the process-local lock still prevents
                # duplicate callbacks within the only supported local daemon.
                return DiscordClaimGuard(handle, local_lock)

            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    return DiscordClaimGuard(handle, local_lock)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        handle.close()
                        local_lock.release()
                        return None
                    time.sleep(0.01)
        except Exception as exc:
            if handle is not None:
                with suppress(Exception):
                    handle.close()
            local_lock.release()
            logger.warning(
                "Discord recovery message lock unavailable: %s",
                exc,
            )
            return None

    def acquire_channel_lock(
        self,
        channel_id: str,
        *,
        timeout: float = 5.0,
    ) -> DiscordClaimGuard | None:
        """Fence receipt insertion and cursor commits for one Discord lane."""
        return self.acquire_message_lock(
            f"channel:{channel_id}",
            timeout=timeout,
        )

    @staticmethod
    def release_claim_guard(guard: DiscordClaimGuard) -> None:
        try:
            try:
                import fcntl
            except ImportError:
                pass
            else:
                with suppress(OSError):
                    fcntl.flock(guard.handle.fileno(), fcntl.LOCK_UN)
            guard.handle.close()
        finally:
            guard.local_lock.release()

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_messages (
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
                claim_owner TEXT,
                claim_epoch INTEGER NOT NULL DEFAULT 0,
                routing_thread_id TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(discord_messages)").fetchall()
        }
        if "claim_owner" not in columns:
            conn.execute("ALTER TABLE discord_messages ADD COLUMN claim_owner TEXT")
        if "claim_epoch" not in columns:
            conn.execute(
                "ALTER TABLE discord_messages "
                "ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0"
            )
        if "routing_thread_id" not in columns:
            conn.execute(
                "ALTER TABLE discord_messages ADD COLUMN routing_thread_id TEXT"
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_recovery_scans (
                scan_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                channels TEXT NOT NULL,
                window_seconds REAL NOT NULL,
                limit_count INTEGER NOT NULL,
                scanned INTEGER NOT NULL DEFAULT 0,
                missed INTEGER NOT NULL DEFAULT 0,
                dispatched INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discord_recovery_cursors (
                channel_id TEXT PRIMARY KEY,
                last_message_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_RETENTION_DAYS)
        ).isoformat()
        conn.execute("DELETE FROM discord_messages WHERE updated_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM discord_recovery_scans "
            "WHERE COALESCE(completed_at, started_at) < ?",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM discord_recovery_cursors WHERE updated_at < ?",
            (cutoff,),
        )
