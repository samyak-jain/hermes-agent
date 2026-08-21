"""Durable workshop turn/event ledger stored in Hermes's shared state.db."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import random
import sqlite3
import threading
import time
from typing import Any, Callable, TypeVar
import uuid

from hermes_constants import get_hermes_home
from hermes_state import apply_sqlite_storage_policy

from .protocol import (
    COMPLETED_EVENT_RETENTION_SECONDS,
    MAX_EVENT_BACKLOG_BYTES,
    MAX_PENDING_REMOTE_CALLS,
    WorkshopEvent,
    WorkshopEventType,
    canonical_json,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")

ACTIVE_TURN_STATES = frozenset({"queued", "running", "ending"})
TERMINAL_TURN_STATES = frozenset({"completed", "error", "aborted", "interrupted"})


class WorkshopStorageError(RuntimeError):
    pass


class WorkshopNotFoundError(WorkshopStorageError):
    pass


class WorkshopConflictError(WorkshopStorageError):
    pass


class WorkshopCapacityError(WorkshopStorageError):
    pass


class WorkshopBacklogExceeded(WorkshopStorageError):
    pass


@dataclass(frozen=True)
class WorkshopTurnRecord:
    turn_id: str
    client_turn_id: str
    workspace_id: str
    chat_id: str
    session_key: str
    session_id: str
    catalog_version: str
    request_digest: str
    state: str
    next_seq: int
    event_bytes: int
    stop_reason: str | None
    created_at: float
    updated_at: float
    completed_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkshopTurnRecord":
        return cls(**{field: row[field] for field in cls.__dataclass_fields__})


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workshop_turns (
    turn_id TEXT PRIMARY KEY,
    client_turn_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    next_seq INTEGER NOT NULL DEFAULT 1,
    event_bytes INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE(workspace_id, chat_id, client_turn_id)
);

CREATE TABLE IF NOT EXISTS workshop_events (
    turn_id TEXT NOT NULL REFERENCES workshop_turns(turn_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(turn_id, seq)
);

CREATE TABLE IF NOT EXISTS workshop_tool_calls (
    turn_id TEXT NOT NULL REFERENCES workshop_turns(turn_id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    is_error INTEGER,
    created_at REAL NOT NULL,
    resolved_at REAL,
    PRIMARY KEY(turn_id, call_id)
);

CREATE TABLE IF NOT EXISTS workshop_wakes (
    producer_type TEXT NOT NULL,
    producer_id TEXT NOT NULL,
    turn_id TEXT NOT NULL REFERENCES workshop_turns(turn_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(producer_type, producer_id)
);

CREATE TABLE IF NOT EXISTS workshop_deltas (
    workspace_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    delta_id TEXT NOT NULL,
    turn_id TEXT,
    payload_digest TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(workspace_id, chat_id, delta_id)
);

CREATE INDEX IF NOT EXISTS idx_workshop_turns_session
    ON workshop_turns(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workshop_turns_state
    ON workshop_turns(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_workshop_events_turn
    ON workshop_events(turn_id, seq);
CREATE INDEX IF NOT EXISTS idx_workshop_tool_calls_pending
    ON workshop_tool_calls(turn_id, state);
CREATE INDEX IF NOT EXISTS idx_workshop_wakes_state
    ON workshop_wakes(state, updated_at);
"""


class WorkshopLedger:
    """Small transactional ledger for replay, idempotency, and recovery.

    Each operation opens a short-lived connection to the profile-aware shared
    ``state.db``.  That mirrors async-delegation durability and lets the host's
    existing Litestream policy cover workshop state without another database.
    """

    _MAX_WRITE_ATTEMPTS = 8

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        max_event_backlog_bytes: int = MAX_EVENT_BACKLOG_BYTES,
        completed_retention_seconds: int = COMPLETED_EVENT_RETENTION_SECONDS,
        max_pending_calls: int = MAX_PENDING_REMOTE_CALLS,
    ):
        self.db_path = (
            Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
        )
        self.max_event_backlog_bytes = int(max_event_backlog_bytes)
        self.completed_retention_seconds = int(completed_retention_seconds)
        self.max_pending_calls = int(max_pending_calls)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        apply_sqlite_storage_policy(conn, db_label="state.db (workshop)")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def _read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock, self._connect() as conn:
            return fn(conn)

    def _write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(self._MAX_WRITE_ATTEMPTS):
            try:
                with self._lock, self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        value = fn(conn)
                        conn.commit()
                        return value
                    except BaseException:
                        conn.rollback()
                        raise
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
                if attempt + 1 < self._MAX_WRITE_ATTEMPTS:
                    time.sleep(random.uniform(0.02, 0.15))
        raise last_error or sqlite3.OperationalError("workshop state.db write failed")

    def create_turn(
        self,
        *,
        client_turn_id: str,
        workspace_id: str,
        chat_id: str,
        session_key: str,
        session_id: str,
        catalog_version: str,
        request_digest: str,
        turn_id: str | None = None,
        state: str = "queued",
        max_active_turns: int | None = None,
    ) -> tuple[WorkshopTurnRecord, bool]:
        if state not in ACTIVE_TURN_STATES:
            raise ValueError(f"invalid initial workshop turn state: {state}")
        now = time.time()
        candidate_id = turn_id or f"wturn_{uuid.uuid4().hex}"

        def write(conn: sqlite3.Connection) -> tuple[WorkshopTurnRecord, bool]:
            existing = conn.execute(
                """SELECT * FROM workshop_turns
                   WHERE workspace_id=? AND chat_id=? AND client_turn_id=?""",
                (workspace_id, chat_id, client_turn_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise WorkshopConflictError(
                        "client_turn_id was already used with a different request"
                    )
                return WorkshopTurnRecord.from_row(existing), False
            if max_active_turns is not None:
                placeholders = ",".join("?" for _ in ACTIVE_TURN_STATES)
                active = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM workshop_turns "
                        f"WHERE state IN ({placeholders})",
                        tuple(ACTIVE_TURN_STATES),
                    ).fetchone()[0]
                )
                if active >= max_active_turns:
                    raise WorkshopCapacityError(
                        f"workshop already has {max_active_turns} active turns"
                    )
            conn.execute(
                """INSERT INTO workshop_turns
                   (turn_id, client_turn_id, workspace_id, chat_id,
                    session_key, session_id, catalog_version, request_digest,
                    state, next_seq, event_bytes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)""",
                (
                    candidate_id,
                    client_turn_id,
                    workspace_id,
                    chat_id,
                    session_key,
                    session_id,
                    catalog_version,
                    request_digest,
                    state,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM workshop_turns WHERE turn_id=?", (candidate_id,)
            ).fetchone()
            assert row is not None
            return WorkshopTurnRecord.from_row(row), True

        return self._write(write)

    def bind_queued_turn_session(
        self,
        *,
        turn_id: str,
        session_key: str,
        session_id: str,
    ) -> WorkshopTurnRecord:
        """Pin a queued turn to the session epoch it will actually execute.

        A preceding serialized turn may rotate the chat's active session while
        compressing.  Rebinding is therefore allowed only before the first
        event has been allocated; once ``turn.started`` exists the public epoch
        is immutable.
        """

        now = time.time()

        def write(conn: sqlite3.Connection) -> WorkshopTurnRecord:
            row = conn.execute(
                "SELECT state, next_seq FROM workshop_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise WorkshopNotFoundError(f"unknown workshop turn: {turn_id}")
            if row["state"] != "queued" or int(row["next_seq"]) != 1:
                raise WorkshopConflictError(
                    "workshop turn session can only be bound before turn.started"
                )
            conn.execute(
                """UPDATE workshop_turns
                   SET session_key=?, session_id=?, updated_at=?
                   WHERE turn_id=?""",
                (session_key, session_id, now, turn_id),
            )
            updated = conn.execute(
                "SELECT * FROM workshop_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            assert updated is not None
            return WorkshopTurnRecord.from_row(updated)

        return self._write(write)

    def get_turn(self, turn_id: str) -> WorkshopTurnRecord | None:
        def read(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT * FROM workshop_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            return WorkshopTurnRecord.from_row(row) if row is not None else None

        return self._read(read)

    def get_turn_for_client(
        self, workspace_id: str, chat_id: str, client_turn_id: str
    ) -> WorkshopTurnRecord | None:
        def read(conn: sqlite3.Connection):
            row = conn.execute(
                """SELECT * FROM workshop_turns
                   WHERE workspace_id=? AND chat_id=? AND client_turn_id=?""",
                (workspace_id, chat_id, client_turn_id),
            ).fetchone()
            return WorkshopTurnRecord.from_row(row) if row is not None else None

        return self._read(read)

    def count_active_turns(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_TURN_STATES)
        return self._read(
            lambda conn: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM workshop_turns WHERE state IN ({placeholders})",
                    tuple(ACTIVE_TURN_STATES),
                ).fetchone()[0]
            )
        )

    def recover_active_turns(self) -> int:
        """Close turns whose in-process SDK continuation died with the host.

        Recovery is intentionally semantic rather than generator resumption:
        callers can replay the durable terminal event and retry under their
        own idempotency key.
        """
        placeholders = ",".join("?" for _ in ACTIVE_TURN_STATES)
        turn_ids = self._read(
            lambda conn: [
                row["turn_id"]
                for row in conn.execute(
                    f"SELECT turn_id FROM workshop_turns WHERE state IN ({placeholders})",
                    tuple(ACTIVE_TURN_STATES),
                ).fetchall()
            ]
        )
        for turn_id in turn_ids:
            self.finish_turn(
                turn_id=turn_id,
                state="interrupted",
                stop_reason="gateway_restart",
            )
        return len(turn_ids)

    def set_turn_state(self, turn_id: str, state: str) -> WorkshopTurnRecord:
        if state not in ACTIVE_TURN_STATES | TERMINAL_TURN_STATES:
            raise ValueError(f"invalid workshop turn state: {state}")
        now = time.time()

        def write(conn: sqlite3.Connection):
            row = conn.execute(
                "SELECT state FROM workshop_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if row is None:
                raise WorkshopNotFoundError(f"unknown workshop turn: {turn_id}")
            if row["state"] in TERMINAL_TURN_STATES and row["state"] != state:
                raise WorkshopConflictError(
                    "terminal workshop turn cannot change state"
                )
            completed_at = now if state in TERMINAL_TURN_STATES else None
            conn.execute(
                """UPDATE workshop_turns
                   SET state=?, updated_at=?, completed_at=COALESCE(completed_at, ?)
                   WHERE turn_id=?""",
                (state, now, completed_at, turn_id),
            )
            updated = conn.execute(
                "SELECT * FROM workshop_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            assert updated is not None
            return WorkshopTurnRecord.from_row(updated)

        return self._write(write)

    def append_event(
        self,
        *,
        turn_id: str,
        event: str | WorkshopEventType,
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> WorkshopEvent:
        event_name = event.value if isinstance(event, WorkshopEventType) else str(event)
        now = time.time() if timestamp is None else float(timestamp)

        def write(conn: sqlite3.Connection) -> WorkshopEvent:
            row = conn.execute(
                "SELECT session_id, state, next_seq, event_bytes FROM workshop_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise WorkshopNotFoundError(f"unknown workshop turn: {turn_id}")
            if row["state"] in TERMINAL_TURN_STATES:
                raise WorkshopConflictError("cannot append to a terminal workshop turn")
            item = WorkshopEvent.create(
                turn_id=turn_id,
                session_id=row["session_id"],
                seq=int(row["next_seq"]),
                event=event_name,
                payload=payload,
                timestamp=now,
            )
            added_bytes = 0
            if item.persistent:
                serialized = canonical_json(item.to_wire())
                added_bytes = len(serialized.encode("utf-8"))
                if int(row["event_bytes"]) + added_bytes > self.max_event_backlog_bytes:
                    raise WorkshopBacklogExceeded(
                        f"workshop event backlog exceeds {self.max_event_backlog_bytes} bytes"
                    )
                conn.execute(
                    """INSERT INTO workshop_events
                       (turn_id, seq, event_type, event_json, event_bytes, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (turn_id, item.seq, item.event, serialized, added_bytes, now),
                )
            conn.execute(
                """UPDATE workshop_turns
                   SET next_seq=?, event_bytes=event_bytes+?, updated_at=?
                   WHERE turn_id=?""",
                (item.seq + 1, added_bytes, now, turn_id),
            )
            return item

        return self._write(write)

    def finish_turn(
        self,
        *,
        turn_id: str,
        state: str,
        stop_reason: str,
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
    ) -> WorkshopEvent:
        if state not in TERMINAL_TURN_STATES:
            raise ValueError(f"invalid terminal workshop turn state: {state}")
        now = time.time() if timestamp is None else float(timestamp)
        terminal_payload = {
            "status": state,
            "stop_reason": stop_reason,
            **(payload or {}),
        }

        def write(conn: sqlite3.Connection) -> WorkshopEvent:
            row = conn.execute(
                "SELECT session_id, state, next_seq, event_bytes FROM workshop_turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise WorkshopNotFoundError(f"unknown workshop turn: {turn_id}")
            if row["state"] in TERMINAL_TURN_STATES:
                events = conn.execute(
                    """SELECT event_json FROM workshop_events
                       WHERE turn_id=? AND event_type=? ORDER BY seq DESC LIMIT 1""",
                    (turn_id, WorkshopEventType.TURN_END.value),
                ).fetchone()
                if events is not None and row["state"] == state:
                    recorded = self._event_from_json(events["event_json"])
                    if recorded.payload == terminal_payload:
                        return recorded
                raise WorkshopConflictError(
                    "workshop turn already finished differently"
                )
            item = WorkshopEvent.create(
                turn_id=turn_id,
                session_id=row["session_id"],
                seq=int(row["next_seq"]),
                event=WorkshopEventType.TURN_END,
                payload=terminal_payload,
                timestamp=now,
            )
            serialized = canonical_json(item.to_wire())
            added_bytes = len(serialized.encode("utf-8"))
            if int(row["event_bytes"]) + added_bytes > self.max_event_backlog_bytes:
                raise WorkshopBacklogExceeded(
                    f"workshop event backlog exceeds {self.max_event_backlog_bytes} bytes"
                )
            conn.execute(
                """INSERT INTO workshop_events
                   (turn_id, seq, event_type, event_json, event_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (turn_id, item.seq, item.event, serialized, added_bytes, now),
            )
            conn.execute(
                """UPDATE workshop_turns
                   SET state=?, stop_reason=?, next_seq=?,
                       event_bytes=event_bytes+?, updated_at=?, completed_at=?
                   WHERE turn_id=?""",
                (state, stop_reason, item.seq + 1, added_bytes, now, now, turn_id),
            )
            return item

        return self._write(write)

    @staticmethod
    def _event_from_json(raw: str) -> WorkshopEvent:
        value = json.loads(raw)
        base_fields = {
            "protocol_version",
            "turn_id",
            "session_id",
            "seq",
            "event",
            "timestamp",
        }
        return WorkshopEvent.create(
            turn_id=value["turn_id"],
            session_id=value["session_id"],
            seq=int(value["seq"]),
            event=value["event"],
            timestamp=float(value["timestamp"]),
            payload={
                key: item for key, item in value.items() if key not in base_fields
            },
        )

    def list_events(self, turn_id: str, *, after_seq: int = 0) -> list[WorkshopEvent]:
        if not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")

        def read(conn: sqlite3.Connection):
            exists = conn.execute(
                "SELECT 1 FROM workshop_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if exists is None:
                raise WorkshopNotFoundError(f"unknown workshop turn: {turn_id}")
            rows = conn.execute(
                """SELECT event_json FROM workshop_events
                   WHERE turn_id=? AND seq>? ORDER BY seq""",
                (turn_id, after_seq),
            ).fetchall()
            return [self._event_from_json(row["event_json"]) for row in rows]

        return self._read(read)

    def register_tool_call(
        self,
        *,
        turn_id: str,
        call_id: str,
        name: str,
        arguments: Any,
    ) -> bool:
        arguments_json = canonical_json(arguments)
        now = time.time()

        def write(conn: sqlite3.Connection) -> bool:
            turn = conn.execute(
                "SELECT state FROM workshop_turns WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if turn is None:
                raise WorkshopNotFoundError(f"unknown workshop turn: {turn_id}")
            if turn["state"] in TERMINAL_TURN_STATES:
                raise WorkshopConflictError(
                    "cannot register a tool call on a terminal turn"
                )
            existing = conn.execute(
                "SELECT name, arguments_json FROM workshop_tool_calls WHERE turn_id=? AND call_id=?",
                (turn_id, call_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["name"] != name
                    or existing["arguments_json"] != arguments_json
                ):
                    raise WorkshopConflictError(
                        "call_id was reused with different arguments"
                    )
                return False
            pending = int(
                conn.execute(
                    """SELECT COUNT(*) FROM workshop_tool_calls
                       WHERE turn_id=? AND state='pending'""",
                    (turn_id,),
                ).fetchone()[0]
            )
            if pending >= self.max_pending_calls:
                raise WorkshopConflictError(
                    f"turn already has {self.max_pending_calls} pending remote calls"
                )
            conn.execute(
                """INSERT INTO workshop_tool_calls
                   (turn_id, call_id, name, state, arguments_json, created_at)
                   VALUES (?, ?, ?, 'pending', ?, ?)""",
                (turn_id, call_id, name, arguments_json, now),
            )
            return True

        return self._write(write)

    def resolve_tool_call(
        self,
        *,
        turn_id: str,
        call_id: str,
        result: Any,
        is_error: bool,
    ) -> bool:
        result_json = canonical_json(result)
        now = time.time()

        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """SELECT state, result_json, is_error FROM workshop_tool_calls
                   WHERE turn_id=? AND call_id=?""",
                (turn_id, call_id),
            ).fetchone()
            if row is None:
                raise WorkshopNotFoundError(f"unknown workshop tool call: {call_id}")
            if row["state"] == "resolved":
                if (
                    row["result_json"] == result_json
                    and bool(row["is_error"]) is is_error
                ):
                    return False
                raise WorkshopConflictError(
                    "tool result conflicts with the recorded result"
                )
            if row["state"] != "pending":
                raise WorkshopConflictError(f"tool call is not pending: {row['state']}")
            conn.execute(
                """UPDATE workshop_tool_calls
                   SET state='resolved', result_json=?, is_error=?, resolved_at=?
                   WHERE turn_id=? AND call_id=?""",
                (result_json, 1 if is_error else 0, now, turn_id, call_id),
            )
            return True

        return self._write(write)

    def record_wake(
        self,
        *,
        producer_type: str,
        producer_id: str,
        turn_id: str,
        state: str = "pending",
    ) -> bool:
        now = time.time()

        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """SELECT turn_id FROM workshop_wakes
                   WHERE producer_type=? AND producer_id=?""",
                (producer_type, producer_id),
            ).fetchone()
            if row is not None:
                if row["turn_id"] != turn_id:
                    raise WorkshopConflictError("wake producer identity was reused")
                return False
            conn.execute(
                """INSERT INTO workshop_wakes
                   (producer_type, producer_id, turn_id, state, attempts, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?)""",
                (producer_type, producer_id, turn_id, state, now),
            )
            return True

        return self._write(write)

    def mark_wake_dead_letter(
        self, *, producer_type: str, producer_id: str, error: str
    ) -> None:
        def write(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """UPDATE workshop_wakes
                   SET state='dead_letter', attempts=attempts+1,
                       last_error=?, updated_at=?
                   WHERE producer_type=? AND producer_id=?""",
                (error[:2048], time.time(), producer_type, producer_id),
            )
            if cursor.rowcount != 1:
                raise WorkshopNotFoundError("unknown workshop wake")

        self._write(write)

    def record_delta(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        delta_id: str,
        payload_digest: str,
        turn_id: str | None = None,
    ) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """SELECT payload_digest, turn_id FROM workshop_deltas
                   WHERE workspace_id=? AND chat_id=? AND delta_id=?""",
                (workspace_id, chat_id, delta_id),
            ).fetchone()
            if row is not None:
                if row["payload_digest"] != payload_digest or row["turn_id"] != turn_id:
                    raise WorkshopConflictError(
                        "delta_id was reused with different content"
                    )
                return False
            conn.execute(
                """INSERT INTO workshop_deltas
                   (workspace_id, chat_id, delta_id, turn_id, payload_digest, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (workspace_id, chat_id, delta_id, turn_id, payload_digest, time.time()),
            )
            return True

        return self._write(write)

    def prune_completed(self, *, now: float | None = None) -> int:
        cutoff = (
            time.time() if now is None else now
        ) - self.completed_retention_seconds

        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """DELETE FROM workshop_turns
                   WHERE state IN ('completed', 'error', 'aborted', 'interrupted')
                     AND completed_at IS NOT NULL AND completed_at < ?""",
                (cutoff,),
            )
            return cursor.rowcount

        return self._write(write)

    def dead_letter_wake_count(self) -> int:
        return self._read(
            lambda conn: int(
                conn.execute(
                    "SELECT COUNT(*) FROM workshop_wakes WHERE state='dead_letter'"
                ).fetchone()[0]
            )
        )
