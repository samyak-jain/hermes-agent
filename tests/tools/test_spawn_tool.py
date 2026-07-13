"""Behavior tests for the minimal background spawn_agent lifecycle tool."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import async_delegation as ad
from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS
from tools.process_registry import ProcessRegistry, format_process_notification, process_registry
from tools.spawn_tool import SPAWN_AGENT_SCHEMA, spawn_agent


@pytest.fixture(autouse=True)
def _clean_async_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _parent():
    lines = []
    parent = SimpleNamespace(
        session_id="parent-session",
        model="parent-model",
        base_url="https://example.invalid/v1",
        provider="custom",
        api_mode="chat_completions",
        api_key="test-key",
        enabled_toolsets=["spawn", "terminal", "file"],
        valid_tool_names={"spawn_agent", "terminal", "read_file"},
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _current_turn_id="turn-1",
        _memory_manager=None,
        _safe_print=lines.append,
    )
    parent.lines = lines
    return parent


def test_schema_supports_spawn_and_cancel_without_duplicating_tools():
    assert SPAWN_AGENT_SCHEMA["name"] == "spawn_agent"
    params = SPAWN_AGENT_SCHEMA["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"prompt", "label", "cancel_id"}
    assert "required" not in params
    assert "omit prompt" in params["properties"]["cancel_id"]["description"]

    from tools.registry import registry

    entry = registry.get_entry("spawn_agent")
    assert entry is not None
    assert entry.toolset == "spawn"
    assert entry.dynamic_schema_overrides is None


def test_children_cannot_spawn_recursively():
    assert "spawn_agent" in DELEGATE_BLOCKED_TOOLS


def test_spawn_toolset_can_replace_delegation_toolset():
    from model_tools import get_tool_definitions

    definitions = get_tool_definitions(
        enabled_toolsets=["spawn"],
        disabled_toolsets=["delegation"],
        quiet_mode=True,
    )
    assert {item["function"]["name"] for item in definitions} == {"spawn_agent"}


def test_spawn_returns_compact_handle_and_dispatches_leaf(monkeypatch):
    parent = _parent()
    interrupt_reasons = []
    child = SimpleNamespace(
        close=lambda: None,
        interrupt=lambda reason=None: interrupt_reasons.append(reason),
    )
    parent._active_children.append(child)
    captured = {}

    def fake_build(**kwargs):
        captured["build"] = kwargs
        return child

    def fake_dispatch(**kwargs):
        captured["dispatch"] = kwargs
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr("tools.async_delegation.active_count", lambda: 0)
    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation", fake_dispatch)
    monkeypatch.setattr("tools.delegate_tool._get_max_concurrent_children", lambda: 3)
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: {"max_iterations": 17})
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_delegation_credentials",
        lambda cfg, agent: {
            "model": "child-model",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "request_overrides": None,
            "max_output_tokens": None,
        },
    )
    monkeypatch.setattr("tools.delegate_tool._build_child_agent", fake_build)
    monkeypatch.setattr(
        "tools.approval.get_current_session_key", lambda default="": "route-key"
    )

    result = json.loads(
        spawn_agent(
            "Audit /workspace/auth.py and report concrete findings.",
            label="audit auth flow",
            parent_agent=parent,
        )
    )

    assert result["status"] == "running"
    assert result["id"].startswith("sa_")
    assert len(result["id"]) == 9
    assert captured["build"]["role"] == "leaf"
    assert captured["build"]["toolsets"] is None
    assert captured["build"]["relay_progress"] is False
    assert captured["build"]["emit_lifecycle_hooks"] is False
    assert captured["dispatch"]["completion_type"] == "spawn_result"
    assert captured["dispatch"]["label"] == "audit auth flow"
    assert captured["dispatch"]["session_key"] == "route-key"
    assert child not in parent._active_children
    assert child._subagent_id is None
    assert parent.lines == [f"🔀 {result['id']} spawned: audit auth flow"]

    captured["dispatch"]["interrupt_fn"]()
    assert interrupt_reasons == ["Background subagent cancelled"]


def test_cancel_requests_owned_spawn_by_returned_id(monkeypatch):
    parent = _parent()
    captured = {}

    def fake_interrupt(delegation_id, **kwargs):
        captured["delegation_id"] = delegation_id
        captured.update(kwargs)
        return True

    monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="": "route-key")
    monkeypatch.setattr("tools.async_delegation.interrupt_delegation", fake_interrupt)

    result = json.loads(spawn_agent(cancel_id=" sa_12ab34 ", parent_agent=parent))

    assert result == {"id": "sa_12ab34", "status": "cancelling"}
    assert captured == {
        "delegation_id": "sa_12ab34",
        "session_key": "route-key",
        "origin_ui_session_id": "",
        "parent_session_id": "parent-session",
        "completion_type": "spawn_result",
    }


def test_cancel_rejects_ambiguous_or_unowned_requests(monkeypatch):
    parent = _parent()

    both = json.loads(
        spawn_agent(prompt="do work", cancel_id="sa_12ab34", parent_agent=parent)
    )
    assert "either prompt or cancel_id" in both["error"]

    monkeypatch.setattr(
        "tools.async_delegation.interrupt_delegation", lambda *a, **k: False
    )
    missing = json.loads(spawn_agent(cancel_id="sa_missing", parent_agent=parent))
    assert "No running spawn_agent subagent found" in missing["error"]


def test_cancel_interrupts_live_spawn_and_delivers_interrupted_result(monkeypatch):
    parent = _parent()
    gate = threading.Event()

    def runner():
        gate.wait(timeout=5)
        return {"status": "interrupted", "error": "cancelled", "summary": None}

    dispatched = ad.dispatch_async_delegation(
        goal="inspect auth",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="route-key",
        parent_session_id="parent-session",
        runner=runner,
        interrupt_fn=gate.set,
        delegation_id="sa_12ab34",
        completion_type="spawn_result",
        max_async_children=1,
    )
    assert dispatched["status"] == "dispatched"
    monkeypatch.setattr(
        "tools.approval.get_current_session_key", lambda default="": "route-key"
    )

    cancelled = json.loads(spawn_agent(cancel_id="sa_12ab34", parent_agent=parent))

    assert cancelled == {"id": "sa_12ab34", "status": "cancelling"}
    event = process_registry.completion_queue.get(timeout=2)
    assert event["delegation_id"] == "sa_12ab34"
    assert event["status"] == "interrupted"


def test_spawn_result_success_and_failure_are_compact():
    success = format_process_notification(
        {
            "type": "spawn_result",
            "delegation_id": "sa_1a2b3c",
            "label": "audit auth flow",
            "status": "completed",
            "summary": "Found two missing authorization checks.",
            "duration_seconds": 192,
        }
    )
    assert success == (
        '[Subagent sa_1a2b3c ("audit auth flow") finished — 3m12s]\n'
        "Found two missing authorization checks."
    )
    assert "Original goal" not in success
    assert "Model:" not in success

    failure = format_process_notification(
        {
            "type": "spawn_result",
            "delegation_id": "sa_bad123",
            "goal": "fallback label\nmore details",
            "status": "error",
            "error": "provider unavailable",
            "summary": "Checked the first module.",
            "duration_seconds": 45,
        }
    )
    assert failure == (
        '[Subagent sa_bad123 ("fallback label") FAILED — 45s]\n'
        "provider unavailable\nPartial output:\nChecked the first module."
    )

    cancelled = format_process_notification(
        {
            "type": "spawn_result",
            "delegation_id": "sa_stop123",
            "label": "slow audit",
            "status": "interrupted",
            "error": "Background subagent cancelled",
            "duration_seconds": 2,
        }
    )
    assert cancelled == '[Subagent sa_stop123 ("slow audit") cancelled — 2s]'


def test_async_dispatch_emits_spawn_result_event():
    result = ad.dispatch_async_delegation(
        goal="inspect code",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {
            "status": "completed",
            "summary": "done",
            "duration_seconds": 1,
        },
        delegation_id="sa_abcdef",
        completion_type="spawn_result",
        label="inspect code",
        max_async_children=1,
    )
    assert result == {"status": "dispatched", "delegation_id": "sa_abcdef"}
    evt = process_registry.completion_queue.get(timeout=2)
    assert evt["type"] == "spawn_result"
    assert evt["label"] == "inspect code"
    assert evt["session_key"] == "owner"


def test_spawn_result_drain_is_session_scoped():
    registry = ProcessRegistry()
    registry.completion_queue.put(
        {
            "type": "spawn_result",
            "delegation_id": "sa_foreign",
            "session_key": "other",
            "status": "completed",
            "summary": "foreign",
        }
    )
    registry.completion_queue.put(
        {
            "type": "spawn_result",
            "delegation_id": "sa_owned",
            "session_key": "mine",
            "status": "completed",
            "summary": "owned",
        }
    )

    drained = registry.drain_notifications(session_key="mine")
    assert [evt["delegation_id"] for evt, _ in drained] == ["sa_owned"]
    assert registry.completion_queue.get_nowait()["delegation_id"] == "sa_foreign"


def test_agent_dispatch_forwards_parent_context():
    import run_agent

    agent = object.__new__(run_agent.AIAgent)
    with patch("tools.spawn_tool.spawn_agent", return_value='{"ok":true}') as call:
        result = run_agent.AIAgent._dispatch_spawn_agent(
            agent, {"prompt": "do work", "label": "work"}
        )
        assert result == '{"ok":true}'
        call.assert_called_once_with(
            prompt="do work",
            label="work",
            cancel_id=None,
            parent_agent=agent,
        )

        call.reset_mock()
        result = run_agent.AIAgent._dispatch_spawn_agent(
            agent, {"cancel_id": "sa_123abc"}
        )
    assert result == '{"ok":true}'
    call.assert_called_once_with(
        prompt=None,
        label=None,
        cancel_id="sa_123abc",
        parent_agent=agent,
    )
