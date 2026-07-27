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
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from hermes_state import apply_sqlite_storage_policy

logger = logging.getLogger(__name__)


_BOT_IDS: set[str] = set()
_BOT_IDS_LOCK = threading.RLock()


@dataclass(frozen=True)
class AmbientParticipationDecision:
    """One profile model's private shared-room participation choice."""

    action: str
    confidence: float = 0.0
    reaction: str = ""


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


def _parse_ambient_decision(text: str) -> AmbientParticipationDecision:
    match = re.search(r"\{.*?\}", text or "", flags=re.DOTALL)
    payload = json.loads(match.group(0) if match else text)
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"reply", "react", "silent"}:
        raise ValueError(f"unsupported ambient action: {action or '(empty)'}")
    reaction = str(payload.get("reaction") or "").strip()
    if action == "react":
        if not reaction or "\n" in reaction or len(reaction) > 64:
            raise ValueError("ambient react action requires one short reaction")
    else:
        reaction = ""
    try:
        confidence = max(0.0, min(float(payload.get("confidence", 0.5)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.5
    return AmbientParticipationDecision(
        action=action,
        confidence=confidence,
        reaction=reaction,
    )


def _call_app_server_ambient_decision(
    *,
    system_prompt: str,
    user_prompt: str,
    main_runtime: dict,
) -> str:
    """Run a private decision through the configured app-server runtime.

    Claude subscription authentication lives behind the Claude Agent SDK
    bridge and cannot be reproduced by a native Anthropic auxiliary client.
    A short-lived, tool-free app-server session therefore preserves the exact
    configured model and subscription route without touching the profile's
    cached conversational thread.
    """
    from agent.codex_runtime import _codex_app_server_config
    from agent.transports.codex_app_server_session import CodexAppServerSession

    app_cfg = _codex_app_server_config()
    with CodexAppServerSession(
        cwd=os.getcwd(),
        codex_bin=str(app_cfg.get("binary") or "codex"),
        model=str(app_cfg.get("model") or main_runtime.get("model") or "") or None,
        permission_mode=str(
            app_cfg.get("permission_mode") or "bypassPermissions"
        ),
        system_prompt_identity=system_prompt,
        tool_schemas=[],
    ) as session:
        result = session.run_turn(
            user_prompt,
            turn_timeout=30,
            post_tool_quiet_timeout=15,
        )
    if result.error:
        raise RuntimeError(result.error)
    return result.final_text


async def decide_ambient_participation(
    *,
    profile: str,
    role: str,
    trigger_text: str,
    channel_context: str,
    author_is_bot: bool,
    main_runtime: dict,
) -> AmbientParticipationDecision:
    """Ask this profile's configured model how it wants to participate.

    The decision is private and tool-free. It never mutates the cached main
    conversation, while still using that profile's exact configured model and
    authentication route.
    """
    from agent.auxiliary_client import (
        async_call_llm,
        extract_content_or_reasoning,
    )

    system_prompt = (
        f"You are {profile}, participating as an independent person in a "
        "shared Discord group chat. Make your own private participation "
        "decision according to your role and voice. Choose exactly one action: "
        "\"reply\" when you have a useful natural contribution, \"react\" when "
        "one emoji reaction is the most human response, or \"silent\" when the "
        "room is better without an interruption. Be selective. Do not reply "
        "merely to agree, repeat another participant, narrate silence, or keep "
        "bot chatter alive without new value. Return JSON only: "
        "{\"action\":\"reply|react|silent\",\"reaction\":\"emoji or empty\","
        "\"confidence\":0..1}."
    )
    user_prompt = (
        f"Agent profile: {profile}\n"
        f"Agent role: {role or 'general participant'}\n"
        f"Newest author is a bot: {str(author_is_bot).lower()}\n"
        f"Recent room context:\n{channel_context or '(none)'}\n\n"
        f"Newest message:\n{trigger_text}"
    )
    if str(main_runtime.get("api_mode") or "") == "codex_app_server":
        text = await asyncio.to_thread(
            _call_app_server_ambient_decision,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            main_runtime=main_runtime,
        )
    else:
        response = await async_call_llm(
            task="ambient_attention",
            main_runtime=main_runtime,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=100,
            timeout=30,
        )
        text = extract_content_or_reasoning(response).strip()
    return _parse_ambient_decision(text)
