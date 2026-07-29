"""Durable state for Discord reconnect message recovery."""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_FILENAME = "discord_message_recovery.db"
_RETENTION_DAYS = 30


class DiscordRecoveryStore:
    """Small profile-scoped SQLite ledger for completed Discord messages."""

    def __init__(self, hermes_home: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._hermes_home = Path(hermes_home or get_hermes_home())

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
                    from hermes_state import apply_sqlite_storage_policy

                    apply_sqlite_storage_policy(
                        conn, db_label="gateway/discord_message_recovery.db"
                    )
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
    ) -> sqlite3.Connection | None:
        """Hold the cross-process writer lock while an owned side effect runs."""
        conn: sqlite3.Connection | None = None
        try:
            with self._lock:
                path = self.path()
                conn = sqlite3.connect(
                    path,
                    timeout=0.1,
                    check_same_thread=False,
                )
                if not self._initialized:
                    self._initialize(conn)
                    self._initialized = True
                    conn.commit()
                    with suppress(OSError):
                        os.chmod(path, 0o600)
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
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
                    conn.rollback()
                    conn.close()
                    return None
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
                return conn
        except Exception as exc:
            if conn is not None:
                with suppress(Exception):
                    conn.rollback()
                with suppress(Exception):
                    conn.close()
            logger.warning(
                "Discord recovery claim guard unavailable: %s",
                exc,
            )
            return None

    @staticmethod
    def release_claim_guard(conn: sqlite3.Connection) -> None:
        try:
            conn.commit()
        finally:
            conn.close()

    def _initialize(self, conn: sqlite3.Connection) -> None:
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
                updated_at TEXT NOT NULL
            )
        """)
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(discord_messages)"
            ).fetchall()
        }
        if "claim_owner" not in columns:
            conn.execute(
                "ALTER TABLE discord_messages ADD COLUMN claim_owner TEXT"
            )
        if "claim_epoch" not in columns:
            conn.execute(
                "ALTER TABLE discord_messages "
                "ADD COLUMN claim_epoch INTEGER NOT NULL DEFAULT 0"
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
