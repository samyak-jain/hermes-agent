"""Workshop platform adapter and HTTP-owned turn execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

from .auth import (
    WorkshopAuthConfigurationError,
    load_workshop_credentials,
)
from .http import create_api_routes
from .protocol import (
    COMPLETED_EVENT_RETENTION_SECONDS,
    MAX_ACTIVE_TURNS,
    MAX_CLIENT_TOOLS,
    MAX_EVENT_BACKLOG_BYTES,
    MAX_PENDING_REMOTE_CALLS,
    MAX_SCHEMA_BYTES,
    MAX_TURN_SECONDS,
    WorkshopEventType,
    WorkshopTurnRequest,
)
from .storage import (
    WorkshopBacklogExceeded,
    WorkshopCapacityError,
    WorkshopConflictError,
    WorkshopLedger,
)
from .turns import WorkshopTurnCoordinator


logger = logging.getLogger(__name__)


_BEHAVIOR_CONFIG_FIELDS = frozenset({
    "wake_url",
    "remote_tool_timeout_seconds",
    "wake_timeout_seconds",
    "max_client_tools",
    "max_tool_schema_bytes",
    "max_event_backlog_bytes",
    "completed_event_retention_seconds",
    "max_active_turns",
    "max_pending_remote_calls",
    "turn_timeout_seconds",
})


def _wake_url(config: PlatformConfig) -> str:
    return str((config.extra or {}).get("wake_url") or "").strip()


_INTEGER_LIMITS = {
    "remote_tool_timeout_seconds": (1, MAX_TURN_SECONDS, 300),
    "wake_timeout_seconds": (1, 60, 10),
    "max_client_tools": (1, MAX_CLIENT_TOOLS, MAX_CLIENT_TOOLS),
    "max_tool_schema_bytes": (1, MAX_SCHEMA_BYTES, MAX_SCHEMA_BYTES),
    "max_event_backlog_bytes": (
        64 * 1024,
        MAX_EVENT_BACKLOG_BYTES,
        MAX_EVENT_BACKLOG_BYTES,
    ),
    "completed_event_retention_seconds": (
        1,
        COMPLETED_EVENT_RETENTION_SECONDS,
        COMPLETED_EVENT_RETENTION_SECONDS,
    ),
    "max_active_turns": (1, MAX_ACTIVE_TURNS, MAX_ACTIVE_TURNS),
    "max_pending_remote_calls": (
        1,
        MAX_PENDING_REMOTE_CALLS,
        MAX_PENDING_REMOTE_CALLS,
    ),
    "turn_timeout_seconds": (1, MAX_TURN_SECONDS, MAX_TURN_SECONDS),
}


def behavior_config(config: PlatformConfig) -> dict[str, int | str]:
    extra = config.extra or {}
    values: dict[str, int | str] = {"wake_url": _wake_url(config)}
    parsed = urlparse(str(values["wake_url"]))
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("platforms.workshop.wake_url must be a public HTTPS URL")
    for name, (minimum, maximum, default) in _INTEGER_LIMITS.items():
        raw = extra.get(name, default)
        if isinstance(raw, bool):
            raise ValueError(f"platforms.workshop.{name} must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"platforms.workshop.{name} must be an integer") from exc
        if value < minimum or value > maximum:
            raise ValueError(
                f"platforms.workshop.{name} must be between {minimum} and {maximum}"
            )
        values[name] = value
    return values


def validate_config(config: PlatformConfig) -> bool:
    try:
        behavior_config(config)
    except ValueError:
        return False
    return True


def is_connected(config: PlatformConfig) -> bool:
    if not config.enabled or not validate_config(config):
        return False
    try:
        load_workshop_credentials()
    except WorkshopAuthConfigurationError:
        return False
    return True


def _apply_yaml_config(_full_config: dict, platform_config: dict) -> dict:
    return {
        key: platform_config[key]
        for key in _BEHAVIOR_CONFIG_FIELDS
        if key in platform_config
    }


class WorkshopAdapter(BasePlatformAdapter):
    supports_async_delivery = True

    @property
    def authorization_is_upstream(self) -> bool:
        # Inbound requests are admitted only after the workshop-specific
        # bearer check on the shared API listener.  There is no end-user
        # platform account for the generic gateway allowlist to evaluate.
        return True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("workshop"))
        self.ledger: WorkshopLedger | None = None
        self.turns: WorkshopTurnCoordinator | None = None
        self._behavior: dict[str, int | str] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            behavior = behavior_config(self.config)
        except ValueError as exc:
            self._set_fatal_error(
                "workshop_invalid_config",
                str(exc),
                retryable=False,
            )
            return False
        try:
            load_workshop_credentials()
        except WorkshopAuthConfigurationError as exc:
            self._set_fatal_error(
                "workshop_invalid_credentials", str(exc), retryable=False
            )
            return False
        self._behavior = behavior
        if self.ledger is None:
            self.ledger = WorkshopLedger(
                max_event_backlog_bytes=int(behavior["max_event_backlog_bytes"]),
                completed_retention_seconds=int(
                    behavior["completed_event_retention_seconds"]
                ),
                max_pending_calls=int(behavior["max_pending_remote_calls"]),
            )
        if self.turns is None:
            self.turns = WorkshopTurnCoordinator(self.ledger)
        # A fresh process cannot resume SDK generators, so stale active rows
        # become replayable interrupted turns.  An in-process adapter reconnect
        # must not interrupt live GatewayRunner tasks that still own them.
        recovered = 0 if is_reconnect else self.ledger.recover_active_turns()
        if recovered:
            logger.warning("workshop_recovered_interrupted_turns count=%d", recovered)
        self._running = True
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del chat_id, content, reply_to, metadata
        return SendResult(
            success=False,
            error="Workshop replies are delivered only on their owning turn stream",
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": str(chat_id), "type": "thread", "platform": "workshop"}

    @staticmethod
    def _source(turn: WorkshopTurnRequest) -> SessionSource:
        # Deliberately model a workspace as the parent chat and the Cloudflare
        # chat as its shared thread. This yields the stable routing key:
        # agent:main:workshop:thread:<workspace_id>:<chat_id>.
        return SessionSource(
            platform=Platform("workshop"),
            chat_type="thread",
            chat_id=turn.workspace_id,
            thread_id=turn.chat_id,
            chat_name=f"{turn.workspace_id}/{turn.chat_id}",
        )

    def _runner(self):
        runner = getattr(self, "gateway_runner", None)
        if runner is None:
            raise RuntimeError("Workshop adapter is not attached to GatewayRunner")
        return runner

    async def _resolve_session(self, source: SessionSource):
        runner = self._runner()
        entry = await runner.async_session_store.get_or_create_session(source)
        return entry.session_key, entry.session_id

    @staticmethod
    def _json_error(code: str, message: str, *, status: int, headers=None):
        from aiohttp import web

        return web.json_response(
            {"error": {"code": code, "message": message}},
            status=status,
            headers=headers,
        )

    async def start_workshop_turn(
        self,
        turn: WorkshopTurnRequest,
        *,
        request,
        controller,
    ):
        del controller
        if self.ledger is None or self.turns is None or not self._running:
            return self._json_error(
                "workshop_unavailable", "Workshop platform is not connected", status=503
            )

        existing = await asyncio.to_thread(
            self.ledger.get_turn_for_client,
            turn.workspace_id,
            turn.chat_id,
            turn.client_turn_id,
        )
        if existing is not None:
            if existing.request_digest != turn.request_digest:
                return self._json_error(
                    "turn_idempotency_conflict",
                    "client_turn_id was already used with a different request",
                    status=409,
                )
            return await self.turns.stream_response(request, existing.turn_id)

        source = self._source(turn)
        session_key, session_id = await self._resolve_session(source)
        try:
            record, created = await asyncio.to_thread(
                self.ledger.create_turn,
                client_turn_id=turn.client_turn_id,
                workspace_id=turn.workspace_id,
                chat_id=turn.chat_id,
                session_key=session_key,
                session_id=session_id,
                catalog_version=turn.catalog_version,
                request_digest=turn.request_digest,
                max_active_turns=int(self._behavior["max_active_turns"]),
            )
        except WorkshopCapacityError:
            return self._json_error(
                "workshop_capacity_exceeded",
                "Workshop has reached its active turn limit",
                status=429,
                headers={"Retry-After": "1"},
            )
        except WorkshopConflictError as exc:
            return self._json_error("turn_idempotency_conflict", str(exc), status=409)

        if created:
            self.turns.ensure_live(record.turn_id)
            self.turns.launch(
                record.turn_id,
                lambda: self._execute_turn(record.turn_id, turn, source),
            )
        return await self.turns.stream_response(request, record.turn_id)

    async def stream_workshop_events(
        self,
        turn_id: str,
        *,
        after_seq: int,
        request,
        controller,
    ):
        del after_seq, controller
        if self.ledger is None or self.turns is None:
            return self._json_error(
                "workshop_unavailable", "Workshop platform is not connected", status=503
            )
        if await asyncio.to_thread(self.ledger.get_turn, turn_id) is None:
            return self._json_error(
                "turn_not_found", "Workshop turn not found", status=404
            )
        return await self.turns.stream_response(request, turn_id)

    async def _execute_turn(
        self,
        turn_id: str,
        turn: WorkshopTurnRequest,
        source: SessionSource,
    ) -> None:
        assert self.ledger is not None and self.turns is not None
        runner = self._runner()
        initial_key = runner._session_key_for_source(source)
        lane = self.turns.lane_lock(initial_key)
        async with lane:
            try:
                # Re-resolve after waiting: an earlier turn in this lane may
                # have rotated to a compressed child session.
                session_key, session_id = await self._resolve_session(source)
                await asyncio.to_thread(
                    self.ledger.bind_queued_turn_session,
                    turn_id=turn_id,
                    session_key=session_key,
                    session_id=session_id,
                )
                await asyncio.to_thread(self.ledger.set_turn_state, turn_id, "running")
                await self.turns.emit(
                    turn_id,
                    WorkshopEventType.TURN_STARTED,
                    {"catalog_version": turn.catalog_version},
                )
                await self.turns.emit(
                    turn_id,
                    WorkshopEventType.MESSAGE_START,
                    {"role": "assistant"},
                )

                def event_sink(event: str, payload: dict[str, Any]) -> None:
                    self.turns.emit_sync(turn_id, event, payload)

                event = MessageEvent(
                    text=turn.text,
                    message_type=MessageType.TEXT,
                    source=source,
                    message_id=turn.client_turn_id,
                    metadata={
                        "gateway_session_id": session_id,
                        "_gateway_event_sink": event_sink,
                        "workshop_turn_id": turn_id,
                        "workshop_catalog_version": turn.catalog_version,
                        "workshop_tools": [
                            tool.to_bridge_schema() for tool in turn.tools
                        ],
                        "workshop_client_metadata": dict(turn.metadata),
                    },
                )
                handler = getattr(self, "_message_handler", None)
                if not callable(handler):
                    raise RuntimeError("Workshop message handler is unavailable")
                result = await handler(event)
                result = result if isinstance(result, dict) else {}
                final_response = str(result.get("final_response") or "")
                streamed = self.turns.emitted_text(turn_id)
                if final_response and not streamed:
                    await self.turns.emit(
                        turn_id,
                        WorkshopEventType.TEXT_DELTA,
                        {"delta": final_response},
                    )
                elif final_response.startswith(streamed) and len(final_response) > len(
                    streamed
                ):
                    await self.turns.emit(
                        turn_id,
                        WorkshopEventType.TEXT_DELTA,
                        {"delta": final_response[len(streamed) :]},
                    )

                await self.turns.emit(
                    turn_id,
                    WorkshopEventType.USAGE,
                    {
                        "input_tokens": int(result.get("input_tokens") or 0),
                        "output_tokens": int(result.get("output_tokens") or 0),
                        "last_prompt_tokens": int(
                            result.get("last_prompt_tokens") or 0
                        ),
                        "model": result.get("model"),
                    },
                )
                if result.get("interrupted"):
                    stop_reason = str(result.get("interrupt_message") or "interrupted")[
                        :1024
                    ]
                    await self.turns.finish(
                        turn_id,
                        state="interrupted",
                        stop_reason=stop_reason,
                    )
                elif result.get("failed") and result.get("error"):
                    message = str(result.get("error"))[:2048]
                    await self.turns.emit(
                        turn_id,
                        WorkshopEventType.ERROR,
                        {"code": "agent_error", "message": message, "retryable": False},
                    )
                    await self.turns.finish(
                        turn_id, state="error", stop_reason="agent_error"
                    )
                else:
                    await self.turns.finish(
                        turn_id, state="completed", stop_reason="complete"
                    )
            except asyncio.CancelledError:
                # Gateway shutdown owns cancellation. Persist a replayable
                # terminal boundary; an SSE observer never cancels this task.
                await self._finish_execution_error(
                    turn_id, code="gateway_shutdown", message="Gateway shut down"
                )
                raise
            except WorkshopBacklogExceeded:
                logger.error(
                    "workshop_event_backlog_exceeded turn_id=%s limit=%s",
                    turn_id,
                    self._behavior.get("max_event_backlog_bytes"),
                )
                try:
                    await asyncio.to_thread(
                        self.ledger.set_turn_state, turn_id, "error"
                    )
                except Exception:
                    logger.exception(
                        "workshop_backlog_terminal_state_failed turn_id=%s", turn_id
                    )
            except Exception as exc:
                logger.exception("workshop_turn_failed turn_id=%s", turn_id)
                await self._finish_execution_error(
                    turn_id,
                    code="turn_execution_failed",
                    message=str(exc)[:2048] or type(exc).__name__,
                )

    async def _finish_execution_error(
        self, turn_id: str, *, code: str, message: str
    ) -> None:
        assert self.turns is not None
        try:
            await self.turns.emit(
                turn_id,
                WorkshopEventType.ERROR,
                {"code": code, "message": message, "retryable": False},
            )
            await self.turns.finish(turn_id, state="error", stop_reason=code)
        except WorkshopConflictError:
            return
        except Exception:
            logger.exception(
                "workshop_turn_error_boundary_failed turn_id=%s code=%s",
                turn_id,
                code,
            )


def register(ctx) -> None:
    ctx.register_platform(
        name="workshop",
        label="Workshop (Cloudflare OS)",
        adapter_factory=lambda config: WorkshopAdapter(config),
        check_fn=lambda: True,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["WORKSHOP_API_KEY", "WORKSHOP_WAKE_TOKEN"],
        apply_yaml_config_fn=_apply_yaml_config,
        api_route_factory=create_api_routes,
        emoji="🛠️",
        allow_update_command=False,
        platform_hint=(
            "You are driving a Cloudflare OS workshop. Workshop tool calls "
            "execute remotely in the workshop Durable Object."
        ),
    )
