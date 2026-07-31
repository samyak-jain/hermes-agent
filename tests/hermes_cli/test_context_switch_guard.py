"""Tests for hermes_cli.context_switch_guard."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from hermes_cli.context_switch_guard import (
    enrich_model_switch_warnings_for_gateway,
    merge_preflight_compression_warning,
)
from hermes_cli.model_switch import ModelSwitchResult


def _result(*, model: str = "small-model") -> ModelSwitchResult:
    return ModelSwitchResult(
        success=True,
        new_model=model,
        target_provider="openrouter",
        provider_changed=False,
        api_key="k",
        base_url="https://example.com/v1",
        api_mode="chat_completions",
        provider_label="openrouter",
        model_info={"context_length": 32_000},
    )


def _compressor(monkeypatch, *, context_length: int = 200_000):
    from agent.context_compressor import ContextCompressor

    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length",
        lambda *a, **k: context_length,
    )
    return ContextCompressor(
        model="big-model",
        threshold_percent=0.5,
        protect_first_n=3,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=context_length,
    )


def test_no_warning_when_below_new_threshold(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.resolve_display_context_length",
        lambda *a, **k: 32_000,
    )
    cc = _compressor(monkeypatch)
    cc.last_prompt_tokens = 10_000
    agent = SimpleNamespace(
        context_compressor=cc,
        compression_enabled=True,
        conversation_history=[],
        base_url="",
        api_key="",
    )
    result = _result()
    merge_preflight_compression_warning(result, agent=agent)
    assert not result.warning_message


def test_warns_when_estimate_exceeds_new_threshold(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.resolve_display_context_length",
        lambda *a, **k: 32_000,
    )
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard._estimate_tokens",
        lambda *a, **k: 90_000,
    )
    cc = _compressor(monkeypatch)
    agent = SimpleNamespace(
        context_compressor=cc,
        compression_enabled=True,
        conversation_history=[],
        base_url="",
        api_key="",
    )
    result = _result()
    merge_preflight_compression_warning(result, agent=agent)
    assert result.warning_message
    assert "preflight compression" in result.warning_message
    assert "shrinks" in result.warning_message


def test_merge_appends_to_existing_warning(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard._estimate_tokens",
        lambda *a, **k: 90_000,
    )
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.resolve_display_context_length",
        lambda *a, **k: 32_000,
    )
    cc = _compressor(monkeypatch)
    agent = SimpleNamespace(
        context_compressor=cc,
        compression_enabled=True,
        base_url="",
        api_key="",
    )
    result = _result()
    result.warning_message = "expensive"
    merge_preflight_compression_warning(result, agent=agent)
    assert "expensive" in result.warning_message
    assert "preflight compression" in result.warning_message


def test_gateway_warning_awaits_async_session_store(monkeypatch):
    seen = {}

    class AsyncStore:
        async def get_or_create_session(self, source):
            seen["source"] = source
            return SimpleNamespace(session_id="session-1")

        async def load_transcript(self, session_id):
            seen["session_id"] = session_id
            return [{"role": "user", "content": "hello"}]

    agent = SimpleNamespace(context_compressor=object())
    runner = SimpleNamespace(
        _agent_cache_lock=threading.Lock(),
        _agent_cache={"key": (agent, None)},
        async_session_store=AsyncStore(),
    )
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.merge_preflight_compression_warning",
        lambda result, **kwargs: seen.update(messages=kwargs["messages"]),
    )

    asyncio.run(
        enrich_model_switch_warnings_for_gateway(
            _result(),
            runner,
            session_key="key",
            source="discord-source",
        )
    )

    assert seen == {
        "source": "discord-source",
        "session_id": "session-1",
        "messages": [{"role": "user", "content": "hello"}],
    }
