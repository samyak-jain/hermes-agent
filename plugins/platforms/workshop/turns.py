"""Live workshop turn ownership and replayable SSE delivery."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from .protocol import WorkshopEvent, WorkshopEventType
from .storage import TERMINAL_TURN_STATES, WorkshopLedger


logger = logging.getLogger(__name__)

_SSE_KEEPALIVE_SECONDS = 15.0
_MAX_RECENT_EVENT_BYTES = 512 * 1024


@dataclass(frozen=True)
class WorkshopRemoteResult:
    content: Any
    is_error: bool
    end_turn: bool = False


class WorkshopRemoteCallTimeout(TimeoutError):
    pass


class WorkshopRemoteCallCancelled(RuntimeError):
    pass


@dataclass
class _LiveTurn:
    loop: asyncio.AbstractEventLoop
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    recent_events: deque[tuple[WorkshopEvent, int]] = field(default_factory=deque)
    recent_bytes: int = 0
    subscribers: int = 0
    task: asyncio.Task | None = None
    text_parts: list[str] = field(default_factory=list)
    interrupt_signatures: set[tuple[str, str, str]] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, event: WorkshopEvent) -> None:
        if event.event == WorkshopEventType.TEXT_DELTA.value:
            delta = event.payload.get("delta")
            if isinstance(delta, str):
                with self.lock:
                    self.text_parts.append(delta)
        # Keep a bounded recent window of every event, not just live-only
        # previews. A subscriber reads SQLite and this window independently;
        # retaining persistent events here closes the race where a higher live
        # sequence is observed before the preceding SQLite row and advances the
        # cursor past it. Persistent events remain recoverable after eviction;
        # thinking and raw argument fragments remain live-only.
        size = len(
            json.dumps(
                event.to_wire(), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        with self.lock:
            self.recent_events.append((event, size))
            self.recent_bytes += size
            while self.recent_events and self.recent_bytes > _MAX_RECENT_EVENT_BYTES:
                _old, old_size = self.recent_events.popleft()
                self.recent_bytes -= old_size
        self.loop.call_soon_threadsafe(self.changed.set)

    def recent_after(self, seq: int) -> list[WorkshopEvent]:
        with self.lock:
            return [event for event, _size in self.recent_events if event.seq > seq]

    def emitted_text(self) -> str:
        with self.lock:
            return "".join(self.text_parts)

    def claim_interrupt(self, signature: tuple[str, str, str]) -> bool:
        with self.lock:
            if signature in self.interrupt_signatures:
                return False
            self.interrupt_signatures.add(signature)
            return True

    def release_interrupt(self, signature: tuple[str, str, str]) -> None:
        with self.lock:
            self.interrupt_signatures.discard(signature)


@dataclass
class _LaneState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class WorkshopTurnCoordinator:
    """Own running tasks independently from any particular SSE connection."""

    def __init__(self, ledger: WorkshopLedger):
        self.ledger = ledger
        self._live: dict[str, _LiveTurn] = {}
        self._lane_locks: dict[str, _LaneState] = {}
        self._remote_waiters: dict[tuple[str, str], threading.Event] = {}
        self._remote_waiters_lock = threading.Lock()

    @asynccontextmanager
    async def lane(self, session_key: str):
        state = self._lane_locks.get(session_key)
        if state is None:
            state = _LaneState()
            self._lane_locks[session_key] = state
        state.users += 1
        try:
            async with state.lock:
                yield
        finally:
            state.users -= 1
            if (
                state.users == 0
                and not state.lock.locked()
                and self._lane_locks.get(session_key) is state
            ):
                self._lane_locks.pop(session_key, None)

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
        if (
            state is not None
            and state.subscribers == 0
            and (state.task is None or state.task.done())
        ):
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
        self._wake_remote_waiters(turn_id)
        return item

    async def finish_backlog_exhausted(
        self, turn_id: str, *, message: str
    ) -> tuple[WorkshopEvent, WorkshopEvent]:
        items = await asyncio.to_thread(
            self.ledger.fail_backlog_exhausted,
            turn_id=turn_id,
            message=message,
        )
        live = self._live.get(turn_id)
        if live is not None:
            for item in items:
                live.publish(item)
        self._wake_remote_waiters(turn_id)
        return items

    def _waiter_for(self, turn_id: str, call_id: str) -> threading.Event:
        key = (turn_id, call_id)
        with self._remote_waiters_lock:
            return self._remote_waiters.setdefault(key, threading.Event())

    def _wake_remote_waiters(self, turn_id: str, call_id: str | None = None) -> None:
        with self._remote_waiters_lock:
            waiters = [
                waiter
                for (candidate_turn, candidate_call), waiter in self._remote_waiters.items()
                if candidate_turn == turn_id
                and (call_id is None or candidate_call == call_id)
            ]
        for waiter in waiters:
            waiter.set()

    def wait_for_remote_result(
        self,
        *,
        turn_id: str,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> WorkshopRemoteResult:
        """Register a remote call durably and block its SDK worker for a result."""

        self.ledger.register_tool_call(
            turn_id=turn_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
        )
        key = (turn_id, call_id)
        waiter = self._waiter_for(turn_id, call_id)
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                # Clear before reading: a resolver either commits before this
                # read (and is observed below) or sets the event afterward.
                waiter.clear()
                record = self.ledger.get_tool_call(turn_id, call_id)
                if record is None:
                    raise WorkshopRemoteCallCancelled(
                        f"remote tool call disappeared: {call_id}"
                    )
                if record.state == "resolved":
                    turn = self.ledger.get_turn(turn_id)
                    end_turn = bool(
                        turn is not None
                        and turn.control_signal is not None
                        and turn.control_mode == "after_current_call"
                        and self.ledger.count_pending_tool_calls(turn_id) == 0
                    )
                    return WorkshopRemoteResult(
                        content=record.result,
                        is_error=bool(record.is_error),
                        end_turn=end_turn,
                    )
                if record.state == "cancelled":
                    return WorkshopRemoteResult(
                        content=record.result,
                        is_error=True,
                        end_turn=True,
                    )
                if record.state != "pending":
                    raise WorkshopRemoteCallCancelled(
                        f"remote tool call ended with state {record.state}: {call_id}"
                    )
                turn = self.ledger.get_turn(turn_id)
                if turn is None or turn.state in TERMINAL_TURN_STATES:
                    raise WorkshopRemoteCallCancelled(
                        f"workshop turn ended while waiting for remote tool: {call_id}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self.ledger.expire_tool_call(
                        turn_id=turn_id, call_id=call_id
                    ):
                        raise WorkshopRemoteCallTimeout(
                            f"remote tool result timed out after {timeout_seconds:g}s"
                        )
                    # A result committed at the deadline; observe it.
                    continue
                waiter.wait(remaining)
        finally:
            with self._remote_waiters_lock:
                if self._remote_waiters.get(key) is waiter:
                    self._remote_waiters.pop(key, None)

    def resolve_remote_result(
        self,
        *,
        turn_id: str,
        call_id: str,
        result: Any,
        is_error: bool,
    ) -> bool:
        created = self.ledger.resolve_tool_call(
            turn_id=turn_id,
            call_id=call_id,
            result=result,
            is_error=is_error,
        )
        self._wake_remote_waiters(turn_id, call_id)
        return created

    def request_control(
        self,
        *,
        turn_id: str,
        signal: str,
        mode: str,
        reason: str,
        replace: bool = False,
    ):
        record, created, affected_calls = self.ledger.request_turn_control(
            turn_id=turn_id,
            signal=signal,
            mode=mode,
            reason=reason,
            replace=replace,
        )
        if mode == "immediate":
            self._wake_remote_waiters(turn_id)
        live = self._live.get(turn_id)
        if live is not None:
            live.loop.call_soon_threadsafe(live.changed.set)
        return record, created, affected_calls

    def emitted_text(self, turn_id: str) -> str:
        state = self._live.get(turn_id)
        return state.emitted_text() if state is not None else ""

    def claim_interrupt(
        self, turn_id: str, signature: tuple[str, str, str]
    ) -> bool:
        state = self._live.get(turn_id)
        return state.claim_interrupt(signature) if state is not None else False

    def release_interrupt(
        self, turn_id: str, signature: tuple[str, str, str]
    ) -> None:
        state = self._live.get(turn_id)
        if state is not None:
            state.release_interrupt(signature)

    def task_for(self, turn_id: str) -> asyncio.Task | None:
        state = self._live.get(turn_id)
        return state.task if state is not None else None

    async def stream_response(
        self, request: Any, turn_id: str, *, after_seq: int
    ):
        """Replay durable events, then tail the live task until turn.end.

        The turn task is never awaited or cancelled by this method.  An HTTP
        disconnect therefore tears down only this observer.
        """

        from aiohttp import web

        turn = await asyncio.to_thread(self.ledger.get_turn, turn_id)
        if turn is None:
            raise KeyError(turn_id)
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
                previews = live.recent_after(cursor) if live is not None else []
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
                if turn is None:
                    break
                if turn.state in TERMINAL_TURN_STATES:
                    # turn.end and the terminal state commit atomically, but
                    # they may commit between the event query above and this
                    # state query. Merge one final tail after observing the
                    # terminal state so the response never closes one event
                    # early.
                    durable_tail = await asyncio.to_thread(
                        self.ledger.list_events, turn_id, after_seq=cursor
                    )
                    recent_tail = (
                        live.recent_after(cursor) if live is not None else []
                    )
                    terminal_tail = {item.seq: item for item in durable_tail}
                    terminal_tail.update(
                        {item.seq: item for item in recent_tail}
                    )
                    for seq in sorted(terminal_tail):
                        item = terminal_tail[seq]
                        payload = json.dumps(
                            item.to_wire(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        await response.write(
                            (
                                f"id: {item.seq}\nevent: {item.event}\n"
                                f"data: {payload}\n\n"
                            ).encode("utf-8")
                        )
                        cursor = item.seq
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
                ) or live.recent_after(cursor):
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
