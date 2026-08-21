"""Live workshop turn ownership and replayable SSE delivery."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import logging
import threading
from typing import Any, Awaitable, Callable

from .protocol import LIVE_ONLY_EVENT_TYPES, WorkshopEvent, WorkshopEventType
from .storage import TERMINAL_TURN_STATES, WorkshopLedger


logger = logging.getLogger(__name__)

_SSE_KEEPALIVE_SECONDS = 15.0
_MAX_LIVE_PREVIEW_BYTES = 512 * 1024


@dataclass
class _LiveTurn:
    loop: asyncio.AbstractEventLoop
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    preview_events: deque[tuple[WorkshopEvent, int]] = field(default_factory=deque)
    preview_bytes: int = 0
    subscribers: int = 0
    task: asyncio.Task | None = None
    text_parts: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, event: WorkshopEvent) -> None:
        if event.event == WorkshopEventType.TEXT_DELTA.value:
            delta = event.payload.get("delta")
            if isinstance(delta, str):
                with self.lock:
                    self.text_parts.append(delta)
        if event.event in LIVE_ONLY_EVENT_TYPES:
            size = len(
                json.dumps(
                    event.to_wire(), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            with self.lock:
                self.preview_events.append((event, size))
                self.preview_bytes += size
                while (
                    self.preview_events and self.preview_bytes > _MAX_LIVE_PREVIEW_BYTES
                ):
                    _old, old_size = self.preview_events.popleft()
                    self.preview_bytes -= old_size
        self.loop.call_soon_threadsafe(self.changed.set)

    def previews_after(self, seq: int) -> list[WorkshopEvent]:
        with self.lock:
            return [event for event, _size in self.preview_events if event.seq > seq]

    def emitted_text(self) -> str:
        with self.lock:
            return "".join(self.text_parts)


class WorkshopTurnCoordinator:
    """Own running tasks independently from any particular SSE connection."""

    def __init__(self, ledger: WorkshopLedger):
        self.ledger = ledger
        self._live: dict[str, _LiveTurn] = {}
        self._lane_locks: dict[str, asyncio.Lock] = {}

    def lane_lock(self, session_key: str) -> asyncio.Lock:
        return self._lane_locks.setdefault(session_key, asyncio.Lock())

    def ensure_live(self, turn_id: str) -> _LiveTurn:
        state = self._live.get(turn_id)
        if state is None:
            state = _LiveTurn(loop=asyncio.get_running_loop())
            self._live[turn_id] = state
        return state

    def launch(
        self,
        turn_id: str,
        run: Callable[[], Awaitable[None]],
    ) -> asyncio.Task:
        state = self.ensure_live(turn_id)
        if state.task is not None:
            return state.task
        state.task = asyncio.create_task(run(), name=f"workshop-turn:{turn_id}")
        state.task.add_done_callback(lambda _task: self._discard_if_idle(turn_id))
        return state.task

    def _discard_if_idle(self, turn_id: str) -> None:
        state = self._live.get(turn_id)
        if state is not None and state.subscribers == 0:
            self._live.pop(turn_id, None)

    def emit_sync(
        self,
        turn_id: str,
        event: str | WorkshopEventType,
        payload: dict[str, Any] | None = None,
    ) -> WorkshopEvent:
        """Persist and publish an event from an agent worker thread.

        The SQLite allocation is synchronous on purpose: returning to the SDK
        callback only after a sequence number is durable gives text deltas
        deterministic ordering and applies backlog pressure at the producer.
        """

        item = self.ledger.append_event(turn_id=turn_id, event=event, payload=payload)
        state = self._live.get(turn_id)
        if state is not None:
            state.publish(item)
        return item

    async def emit(
        self,
        turn_id: str,
        event: str | WorkshopEventType,
        payload: dict[str, Any] | None = None,
    ) -> WorkshopEvent:
        return await asyncio.to_thread(self.emit_sync, turn_id, event, payload)

    async def finish(
        self,
        turn_id: str,
        *,
        state: str,
        stop_reason: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkshopEvent:
        item = await asyncio.to_thread(
            self.ledger.finish_turn,
            turn_id=turn_id,
            state=state,
            stop_reason=stop_reason,
            payload=payload,
        )
        live = self._live.get(turn_id)
        if live is not None:
            live.publish(item)
        return item

    def emitted_text(self, turn_id: str) -> str:
        state = self._live.get(turn_id)
        return state.emitted_text() if state is not None else ""

    def task_for(self, turn_id: str) -> asyncio.Task | None:
        state = self._live.get(turn_id)
        return state.task if state is not None else None

    async def stream_response(self, request: Any, turn_id: str):
        """Replay durable events, then tail the live task until turn.end.

        The turn task is never awaited or cancelled by this method.  An HTTP
        disconnect therefore tears down only this observer.
        """

        from aiohttp import web

        turn = await asyncio.to_thread(self.ledger.get_turn, turn_id)
        if turn is None:
            raise KeyError(turn_id)
        raw_after = request.query.get("after_seq", "0")
        after_seq = int(raw_after)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        live = self._live.get(turn_id)
        if live is not None:
            live.subscribers += 1
        cursor = after_seq
        try:
            while True:
                durable = await asyncio.to_thread(
                    self.ledger.list_events, turn_id, after_seq=cursor
                )
                previews = live.previews_after(cursor) if live is not None else []
                pending = {item.seq: item for item in durable}
                pending.update({item.seq: item for item in previews})
                for seq in sorted(pending):
                    item = pending[seq]
                    payload = json.dumps(
                        item.to_wire(), ensure_ascii=False, separators=(",", ":")
                    )
                    await response.write(
                        f"id: {item.seq}\nevent: {item.event}\ndata: {payload}\n\n".encode(
                            "utf-8"
                        )
                    )
                    cursor = item.seq

                turn = await asyncio.to_thread(self.ledger.get_turn, turn_id)
                if turn is None or turn.state in TERMINAL_TURN_STATES:
                    break
                if live is None:
                    # A fresh process closes orphaned active turns during
                    # adapter connect. Reaching this branch means the adapter
                    # cannot own the continuation and must not hang forever.
                    break

                live.changed.clear()
                # Close the clear-before-wait race by checking once more after
                # arming the event. Persistent events are visible in SQLite;
                # live-only previews remain in the bounded in-memory deque.
                if await asyncio.to_thread(
                    self.ledger.list_events, turn_id, after_seq=cursor
                ) or live.previews_after(cursor):
                    continue
                try:
                    await asyncio.wait_for(
                        live.changed.wait(), timeout=_SSE_KEEPALIVE_SECONDS
                    )
                except TimeoutError:
                    await response.write(b": keepalive\n\n")
        except (ConnectionResetError, BrokenPipeError):
            logger.debug("Workshop SSE disconnected: turn_id=%s", turn_id)
        finally:
            if live is not None:
                live.subscribers = max(0, live.subscribers - 1)
                self._discard_if_idle(turn_id)

        try:
            await response.write_eof()
        except (ConnectionResetError, BrokenPipeError):
            pass
        return response
