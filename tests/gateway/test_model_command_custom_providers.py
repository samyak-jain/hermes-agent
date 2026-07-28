"""Regression tests for gateway /model support of config.yaml custom_providers."""

import yaml
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    return runner


def _make_event(text="/model"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )


@pytest.mark.asyncio
async def test_direct_model_switch_offloads_to_thread(tmp_path, monkeypatch):
    """A direct `/model <name>` switch must route switch_model() through
    asyncio.to_thread so the blocking models.dev HTTP fetch can't freeze the
    gateway event loop (#20525)."""
    import asyncio

    from hermes_cli.model_switch import ModelSwitchResult

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"model": {"default": "gpt-5.4", "provider": "openrouter"}}
        ),
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

    # Fail the switch so the handler returns before _finish_switch (which needs
    # full runner state) — we only care that the offload happened.
    def _fake_switch(**kwargs):
        return ModelSwitchResult(success=False, error_message="nope")

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", _fake_switch)

    offloaded = []
    real_to_thread = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)

    result = await _make_runner()._handle_model_command(_make_event("/model gpt-5.4"))

    # switch_model was offloaded to a worker thread, not run on the event loop.
    assert "_fake_switch" in offloaded
    assert result is not None and "nope" in result


@pytest.mark.asyncio
async def test_model_reports_same_effective_managed_profile_route_as_turn_client(
    tmp_path,
    monkeypatch,
):
    """The /model display and AIAgent construction route share one resolver."""
    import gateway.run as gateway_run

    profile_home = tmp_path / "profiles" / "vegapunk"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "agent-owned-model",
                    "provider": "openrouter",
                }
            }
        ),
        encoding="utf-8",
    )
    effective_config = {
        "model": {
            "default": "managed-global-model",
            "provider": "anthropic",
        },
        "agent": {
            "profile_models": {
                "vegapunk": {
                    "model": "managed-profile-model",
                    "provider": "openai-codex",
                    "api_mode": "codex_responses",
                }
            }
        },
        "providers": {},
    }
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: effective_config)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_gateway_model",
        lambda _config=None: "managed-global-model",
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "anthropic",
            "api_mode": "chat_completions",
            "api_key": "test-only",
        },
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {
            "provider": provider,
            "api_mode": "codex_responses",
            "api_key": "test-only",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [],
    )

    runner = _make_runner()
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._last_resolved_model = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: (
        "agent:vegapunk:discord:channel:operator-room"
    )
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    runner._adapter_for_source = lambda _source: None
    event = MessageEvent(
        text="/model",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="operator-room",
            chat_type="group",
            user_id="operator",
            profile="vegapunk",
        ),
    )

    output = await runner._handle_model_command(event)
    model, runtime = runner._resolve_session_agent_runtime(
        source=event.source,
        session_key="agent:vegapunk:discord:channel:operator-room",
        user_config=effective_config,
    )
    turn_client_route = runner._resolve_turn_agent_config("", model, runtime)

    assert turn_client_route["model"] == "managed-profile-model"
    assert turn_client_route["runtime"]["provider"] == "openai-codex"
    assert (
        "**Effective route (profile/channel winner):** "
        "`managed-profile-model` via `openai-codex`"
    ) in output
    assert (
        "**Agent-owned model config (overridden):** "
        "`agent-owned-model` via `openrouter`"
    ) in output
