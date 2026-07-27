"""Durable inbound-message queue for planned gateway drain windows.

The external restart broker first asks the gateway to stop admitting new
turns, then waits for active work to finish.  Discord and other adapters stay
connected during that interval, so rejecting messages forces users to notice
and resend them.  This module persists those already-received normalized
events until the drain is cancelled or the replacement gateway is ready.

Only normalized gateway data is stored.  Platform client objects and raw
message objects are deliberately excluded.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_state import apply_sqlite_storage_policy


@dataclass(frozen=True)
class QueuedDrainEvent:
    row_id: int
    event: MessageEvent


def _event_payload(event: MessageEvent) -> dict[str, Any]:
    source = event.source
    return {
        "version": 1,
        "text": event.text,
        "message_type": event.message_type.value,
        "source": source.to_dict(),
        "source_local": {
            "is_bot": bool(source.is_bot),
            "role_authorized": bool(source.role_authorized),
            "ambient_authorized_bot": bool(source.ambient_authorized_bot),
        },
        "message_id": event.message_id,
        "platform_update_id": event.platform_update_id,
        "media_urls": list(event.media_urls or []),
        "media_types": list(event.media_types or []),
        "reply_to_message_id": event.reply_to_message_id,
        "reply_to_text": event.reply_to_text,
        "reply_to_author_id": event.reply_to_author_id,
        "reply_to_author_name": event.reply_to_author_name,
        "reply_to_is_own_message": bool(event.reply_to_is_own_message),
        "auto_skill": event.auto_skill,
        "channel_prompt": event.channel_prompt,
        "channel_context": event.channel_context,
        "metadata": dict(event.metadata or {}),
        "timestamp": event.timestamp.isoformat(),
    }


def _event_from_payload(payload: dict[str, Any]) -> MessageEvent:
    source = SessionSource.from_dict(dict(payload["source"]))
    source_local = payload.get("source_local") or {}
    source.is_bot = bool(source_local.get("is_bot"))
    source.role_authorized = bool(source_local.get("role_authorized"))
    source.ambient_authorized_bot = bool(
        source_local.get("ambient_authorized_bot")
    )
    raw_timestamp = payload.get("timestamp")
    try:
        timestamp = datetime.fromisoformat(str(raw_timestamp))
    except (TypeError, ValueError):
        timestamp = datetime.now()
    return MessageEvent(
        text=str(payload.get("text") or ""),
        message_type=MessageType(
            str(payload.get("message_type") or MessageType.TEXT.value)
        ),
        source=source,
        raw_message=None,
        message_id=payload.get("message_id"),
        platform_update_id=payload.get("platform_update_id"),
        media_urls=list(payload.get("media_urls") or []),
        media_types=list(payload.get("media_types") or []),
        reply_to_message_id=payload.get("reply_to_message_id"),
        reply_to_text=payload.get("reply_to_text"),
        reply_to_author_id=payload.get("reply_to_author_id"),
        reply_to_author_name=payload.get("reply_to_author_name"),
        reply_to_is_own_message=bool(
            payload.get("reply_to_is_own_message", False)
        ),
        auto_skill=payload.get("auto_skill"),
        channel_prompt=payload.get("channel_prompt"),
        channel_context=payload.get("channel_context"),
        internal=False,
        metadata=dict(payload.get("metadata") or {}),
        timestamp=timestamp,
    )


class DrainInbox:
    """SQLite-backed FIFO of normalized events received during a drain."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        apply_sqlite_storage_policy(conn, db_label=self.path.name)
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS drain_inbox (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            dedup_key TEXT NOT NULL UNIQUE,
                            message_key TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            acknowledged INTEGER NOT NULL DEFAULT 0,
                            created_at REAL NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS drain_inbox_created "
                        "ON drain_inbox(created_at, id)"
                    )
                    conn.commit()
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
                    self._initialized = True
        return conn

    @staticmethod
    def _keys(event: MessageEvent) -> tuple[str, str]:
        source = event.source
        platform = source.platform.value
        profile = str(source.profile or "default")
        message_id = str(event.message_id or uuid.uuid4().hex)
        message_key = f"{platform}:{source.chat_id}:{message_id}"
        return f"{message_key}:{profile}", message_key

    def enqueue(self, event: MessageEvent) -> tuple[bool, bool]:
        """Persist an event.

        Returns ``(inserted, should_acknowledge)``.  Multiple profile adapters
        legitimately enqueue the same shared-room message; only the first row
        should produce a user-visible acknowledgement.
        """

        dedup_key, message_key = self._keys(event)
        payload = json.dumps(
            _event_payload(event),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM drain_inbox WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone():
                conn.rollback()
                return False, False
            should_ack = conn.execute(
                "SELECT 1 FROM drain_inbox "
                "WHERE message_key = ? AND acknowledged = 1 LIMIT 1",
                (message_key,),
            ).fetchone() is None
            conn.execute(
                """
                INSERT INTO drain_inbox
                    (dedup_key, message_key, payload, acknowledged, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    dedup_key,
                    message_key,
                    payload,
                    1 if should_ack else 0,
                    time.time(),
                ),
            )
            conn.commit()
        return True, should_ack

    def pending(self) -> list[QueuedDrainEvent]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM drain_inbox ORDER BY created_at, id"
            ).fetchall()
        result: list[QueuedDrainEvent] = []
        for row in rows:
            try:
                result.append(
                    QueuedDrainEvent(
                        row_id=int(row["id"]),
                        event=_event_from_payload(json.loads(row["payload"])),
                    )
                )
            except Exception:
                # Keep malformed rows for operator inspection instead of
                # silently deleting user input.
                continue
        return result

    def delete(self, row_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM drain_inbox WHERE id = ?", (int(row_id),))
            conn.commit()

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM drain_inbox").fetchone()
        return int(row[0] if row else 0)
