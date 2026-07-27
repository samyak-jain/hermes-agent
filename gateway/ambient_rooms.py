"""Shared-room participation and loop provenance for multiplexed gateways.

Discord is the room transcript.  Profiles keep independent Hermes sessions,
but every bot adapter sees the same inbound room event.  This module provides
the small amount of shared coordination needed to make that feel like a human
group chat:

* each participant independently decides whether to speak;
* explicit mentions bypass arbitration and always reach the named bot;
* bot-authored replies carry a durable hop count so agent cascades are bounded
  across gateway restarts.

No conversation content is stored here.  The provenance database contains
Discord message IDs and routing metadata only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from hermes_constants import get_hermes_home
from hermes_state import apply_sqlite_storage_policy

logger = logging.getLogger(__name__)


@dataclass
class _Decision:
    created_at: float
    participants: tuple[str, ...]
    candidates: dict[str, float] = field(default_factory=dict)
    selected: frozenset[str] = field(default_factory=frozenset)
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class AmbientRoomArbiter:
    """Coordinate independent participation decisions for one room message.

    Waiting briefly for every configured participant keeps decisions based on
    the same room snapshot and gives us one observable decision record.  It
    must not turn a human-style group chat into a single-assistant router:
    every candidate whose own attention score is positive is admitted.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._decisions: dict[str, _Decision] = {}

    async def choose(
        self,
        *,
        message_id: str,
        profile: str,
        participants: Iterable[str],
        score: float,
        decision_window_seconds: float,
    ) -> bool:
        participant_tuple = tuple(dict.fromkeys(str(p) for p in participants if p))
        now = time.monotonic()
        async with self._lock:
            self._prune(now)
            decision = self._decisions.get(message_id)
            if decision is None:
                decision = _Decision(
                    created_at=now,
                    participants=participant_tuple,
                )
                self._decisions[message_id] = decision
            decision.candidates[profile] = max(0.0, min(float(score), 1.0))
            if set(decision.participants).issubset(decision.candidates):
                self._finalize(decision)

        if not decision.ready.is_set():
            remaining = max(
                0.0,
                decision.created_at + max(decision_window_seconds, 0.05)
                - time.monotonic(),
            )
            try:
                await asyncio.wait_for(decision.ready.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                async with self._lock:
                    self._finalize(decision)

        return profile in decision.selected

    @staticmethod
    def _finalize(decision: _Decision) -> None:
        if decision.ready.is_set():
            return
        decision.selected = frozenset(
            profile
            for profile in decision.participants
            if decision.candidates.get(profile, 0.0) > 0.0
        )
        logger.info(
            "Ambient participation decision: candidates=%s selected=%s",
            {
                profile: round(decision.candidates.get(profile, 0.0), 3)
                for profile in decision.participants
            },
            sorted(decision.selected),
        )
        decision.ready.set()

    def _prune(self, now: float) -> None:
        for key, decision in list(self._decisions.items()):
            if now - decision.created_at > 300:
                self._decisions.pop(key, None)


_ARBITER = AmbientRoomArbiter()
_BOT_IDS: set[str] = set()
_BOT_IDS_LOCK = threading.RLock()


def ambient_arbiter() -> AmbientRoomArbiter:
    return _ARBITER


def register_ambient_bot_id(bot_id: object) -> None:
    """Trust a bot identity owned by an adapter in this gateway process."""
    normalized = str(bot_id or "").strip()
    if normalized:
        with _BOT_IDS_LOCK:
            _BOT_IDS.add(normalized)


def is_registered_ambient_bot(bot_id: object) -> bool:
    normalized = str(bot_id or "").strip()
    if not normalized:
        return False
    with _BOT_IDS_LOCK:
        return normalized in _BOT_IDS


class AmbientProvenanceStore:
    """Durable Discord reply-hop metadata, safe for concurrent adapters."""

    def __init__(self, path: Optional[Path] = None) -> None:
        process_home = Path(
            os.environ.get("HERMES_HOME") or str(get_hermes_home())
        )
        self.path = Path(path or (process_home / "ambient_rooms.db"))
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
                        CREATE TABLE IF NOT EXISTS ambient_messages (
                            message_id TEXT PRIMARY KEY,
                            room_id TEXT NOT NULL,
                            root_message_id TEXT NOT NULL,
                            hop INTEGER NOT NULL,
                            profile TEXT NOT NULL,
                            created_at REAL NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS ambient_messages_created "
                        "ON ambient_messages(created_at)"
                    )
                    conn.commit()
                    self._initialized = True
        return conn

    def record(
        self,
        *,
        message_id: str,
        room_id: str,
        root_message_id: str,
        hop: int,
        profile: str,
    ) -> None:
        if not message_id:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ambient_messages
                    (message_id, room_id, root_message_id, hop, profile, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(message_id),
                    str(room_id),
                    str(root_message_id or message_id),
                    max(0, int(hop)),
                    str(profile or "default"),
                    time.time(),
                ),
            )
            conn.execute(
                "DELETE FROM ambient_messages WHERE created_at < ?",
                (time.time() - 30 * 86400,),
            )

    def lookup(self, message_id: str) -> Optional[dict]:
        if not message_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT room_id, root_message_id, hop, profile "
                "FROM ambient_messages WHERE message_id = ?",
                (str(message_id),),
            ).fetchone()
        return dict(row) if row is not None else None


_PROVENANCE = AmbientProvenanceStore()


def ambient_provenance() -> AmbientProvenanceStore:
    return _PROVENANCE


async def score_ambient_participation(
    *,
    profile: str,
    role: str,
    trigger_text: str,
    channel_context: str,
    author_is_bot: bool,
    main_runtime: dict,
) -> float:
    """Return a 0..1 likelihood that this profile should speak now.

    This is deliberately a tiny auxiliary call rather than a full agent turn.
    A failure is quiet: ambient participation is optional and direct mentions
    still work independently.
    """
    from agent.auxiliary_client import (
        async_call_llm,
        extract_content_or_reasoning,
    )

    prompt = (
        "You are an attention gate for one participant in a shared group chat. "
        "Decide whether this agent should visibly reply to the newest message. "
        "Reply with JSON only: {\"speak\":true|false,\"confidence\":0..1}. "
        "Be selective. Speak when the message materially benefits from this "
        "agent's role, addresses its work, corrects a consequential mistake, "
        "or naturally invites its contribution. Do not speak merely to agree, "
        "repeat another participant, narrate silence, or continue bot chatter "
        "without new value.\n\n"
        f"Agent profile: {profile}\n"
        f"Agent role: {role or 'general participant'}\n"
        f"Newest author is a bot: {str(author_is_bot).lower()}\n"
        f"Recent room context:\n{channel_context or '(none)'}\n\n"
        f"Newest message:\n{trigger_text}"
    )
    response = await async_call_llm(
        task="ambient_attention",
        main_runtime=main_runtime,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=80,
        timeout=30,
    )
    text = extract_content_or_reasoning(response).strip()
    import json
    import re

    match = re.search(r"\{.*?\}", text, flags=re.DOTALL)
    payload = json.loads(match.group(0) if match else text)
    if not payload.get("speak"):
        return 0.0
    return max(0.01, min(float(payload.get("confidence", 0.5)), 1.0))
