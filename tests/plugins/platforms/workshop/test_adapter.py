from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from plugins.platforms.workshop.adapter import (
    WorkshopAdapter,
    _apply_yaml_config,
    register,
    validate_config,
)


def _config(**extra):
    return PlatformConfig(
        enabled=True,
        extra={"wake_url": "https://workshop.example/api/hermes/wake", **extra},
    )


def test_dynamic_platform_identity_and_registration_contract():
    ctx = MagicMock()
    register(ctx)
    kwargs = ctx.register_platform.call_args.kwargs

    assert Platform("workshop").value == "workshop"
    assert kwargs["name"] == "workshop"
    assert kwargs["required_env"] == ["WORKSHOP_API_KEY", "WORKSHOP_WAKE_TOKEN"]
    assert callable(kwargs["api_route_factory"])
    assert isinstance(kwargs["adapter_factory"](_config()), WorkshopAdapter)


def test_yaml_bridge_keeps_only_owned_behavior_settings():
    result = _apply_yaml_config(
        {},
        {
            "wake_url": "https://workshop.example/wake",
            "max_active_turns": 4,
            "enabled": True,
            "tool_policy": {"mode": "allowlist"},
            "unrelated": "drop-me",
        },
    )

    assert result == {
        "wake_url": "https://workshop.example/wake",
        "max_active_turns": 4,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"wake_url": "http://not-tls.example/wake"},
        {"max_active_turns": 5},
        {"max_pending_remote_calls": 9},
        {"max_event_backlog_bytes": 8 * 1024 * 1024 + 1},
        {"turn_timeout_seconds": True},
    ],
)
def test_security_limits_cannot_be_configured_above_approved_caps(extra):
    config = _config(**extra)
    assert validate_config(config) is False


@pytest.mark.asyncio
async def test_connect_fails_closed_for_invalid_or_shared_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_API_KEY", "a" * 64)
    monkeypatch.setenv("WORKSHOP_WAKE_TOKEN", "a" * 64)
    adapter = WorkshopAdapter(_config())

    assert await adapter.connect() is False
    assert adapter.fatal_error_code == "workshop_invalid_credentials"
    assert adapter.fatal_error_retryable is False


@pytest.mark.asyncio
async def test_connect_initializes_shared_ledger_and_recovers_stale_turns(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_API_KEY", "a" * 64)
    monkeypatch.setenv("WORKSHOP_WAKE_TOKEN", "wake-secret")
    first = WorkshopAdapter(_config())
    assert await first.connect() is True
    assert first.ledger is not None
    turn, _created = first.ledger.create_turn(
        client_turn_id="client-1",
        workspace_id="workspace-1",
        chat_id="chat-1",
        session_key="agent:main:workshop:thread:workspace-1:chat-1",
        session_id="session-1",
        catalog_version="catalog-1",
        request_digest="request-1",
    )
    await first.disconnect()

    second = WorkshopAdapter(_config())
    assert await second.connect() is True
    assert second.ledger is not None
    recovered = second.ledger.get_turn(turn.turn_id)
    assert recovered is not None
    assert recovered.state == "interrupted"
    await second.disconnect()


@pytest.mark.asyncio
async def test_restart_keeps_pending_wake_and_exposes_interrupted_turn(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_API_KEY", "a" * 64)
    monkeypatch.setenv("WORKSHOP_WAKE_TOKEN", "wake-secret")
    first = WorkshopAdapter(_config())
    assert await first.connect() is True
    turn, _created = first.ledger.create_turn(
        client_turn_id="wake.pending",
        workspace_id="workspace-1",
        chat_id="chat-1",
        session_key="agent:main:workshop:thread:workspace-1:chat-1",
        session_id="session-1",
        catalog_version="catalog-empty",
        request_digest="request-wake",
    )
    first.ledger.record_wake(
        producer_type="spawn_result",
        producer_id="delegation-restart",
        turn_id=turn.turn_id,
    )
    await first.disconnect()

    second = WorkshopAdapter(_config())
    assert await second.connect() is True
    recovered = second.ledger.get_turn(turn.turn_id)
    wake = second.ledger.get_wake("spawn_result", "delegation-restart")

    assert recovered is not None and recovered.state == "interrupted"
    assert wake is not None and wake.state == "pending"
    assert second.ledger.list_events(turn.turn_id)[-1].event == "turn.end"
    await second.disconnect()


@pytest.mark.asyncio
async def test_in_process_reconnect_does_not_interrupt_live_turn(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WORKSHOP_API_KEY", "a" * 64)
    monkeypatch.setenv("WORKSHOP_WAKE_TOKEN", "wake-secret")
    adapter = WorkshopAdapter(_config())
    assert await adapter.connect() is True
    assert adapter.ledger is not None
    turn, _created = adapter.ledger.create_turn(
        client_turn_id="client-live",
        workspace_id="workspace-1",
        chat_id="chat-1",
        session_key="agent:main:workshop:thread:workspace-1:chat-1",
        session_id="session-1",
        catalog_version="catalog-1",
        request_digest="request-live",
    )

    assert await adapter.connect(is_reconnect=True) is True
    assert adapter.ledger is not None
    still_live = adapter.ledger.get_turn(turn.turn_id)
    assert still_live is not None
    assert still_live.state == "queued"
    await adapter.disconnect()
