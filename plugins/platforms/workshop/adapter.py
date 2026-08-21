"""Workshop platform adapter.

Phase 1 establishes registration, configuration, authentication readiness,
and the durable ledger.  Stateful turn dispatch and event streaming are added
in phase 2 on this same adapter surface.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

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
)
from .storage import WorkshopLedger


logger = logging.getLogger(__name__)


_BEHAVIOR_CONFIG_FIELDS = frozenset(
    {
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
    }
)


def _wake_url(config: PlatformConfig) -> str:
    return str((config.extra or {}).get("wake_url") or "").strip()


_INTEGER_LIMITS = {
    "remote_tool_timeout_seconds": (1, MAX_TURN_SECONDS, 300),
    "wake_timeout_seconds": (1, 60, 10),
    "max_client_tools": (1, MAX_CLIENT_TOOLS, MAX_CLIENT_TOOLS),
    "max_tool_schema_bytes": (1, MAX_SCHEMA_BYTES, MAX_SCHEMA_BYTES),
    "max_event_backlog_bytes": (64 * 1024, MAX_EVENT_BACKLOG_BYTES, MAX_EVENT_BACKLOG_BYTES),
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
            raise ValueError(
                f"platforms.workshop.{name} must be an integer"
            ) from exc
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
        self.ledger = WorkshopLedger(
            max_event_backlog_bytes=int(behavior["max_event_backlog_bytes"]),
            completed_retention_seconds=int(
                behavior["completed_event_retention_seconds"]
            ),
            max_pending_calls=int(behavior["max_pending_remote_calls"]),
        )
        # A fresh process cannot resume SDK generators, so stale active rows
        # become replayable interrupted turns.  An in-process adapter reconnect
        # must not interrupt live GatewayRunner tasks that still own them.
        recovered = 0 if is_reconnect else self.ledger.recover_active_turns()
        if recovered:
            logger.warning(
                "workshop_recovered_interrupted_turns count=%d", recovered
            )
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
            error="Workshop turn delivery is not active until phase 2",
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": str(chat_id), "type": "thread", "platform": "workshop"}


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
