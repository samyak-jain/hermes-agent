"""Behavior tests for the minimal background spawn_agent lifecycle tool."""

from __future__ import annotations

import json
import threading
from pathlib import Path
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
    assert set(params["properties"]) == {
        "prompt",
        "label",
        "cancel_id",
        "result_id",
        "offset",
        "limit",
    }
    assert "required" not in params
    assert "omit prompt" in params["properties"]["cancel_id"]["description"]
    assert "next_offset" in params["properties"]["offset"]["description"]

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
    assert result["live_transcripts"] == [child._live_transcript_path]
    assert Path(result["live_transcripts"][0]).exists()
    assert "tail -f" in result["live_transcripts_hint"]
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


def test_spawn_live_transcript_streams_and_finalizes(monkeypatch, tmp_path):
    from tools import delegation_live_log as dll

    parent = _parent()
    child = SimpleNamespace(close=lambda: None, tool_progress_callback=None)
    parent._active_children.append(child)
    captured = {}

    monkeypatch.setattr(dll, "live_transcript_root", lambda: tmp_path / "live")
    monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    monkeypatch.setattr("tools.async_delegation.active_count", lambda: 0)
    monkeypatch.setattr("tools.delegate_tool._get_max_concurrent_children", lambda: 3)
    monkeypatch.setattr("tools.delegate_tool._load_config", lambda: {})
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
            "command": None,
            "args": None,
        },
    )
    monkeypatch.setattr("tools.delegate_tool._build_child_agent", lambda **kw: child)

    def fake_run(*args, **kwargs):
        child.tool_progress_callback("_thinking", "checking the code")
        child.tool_progress_callback(
            "tool.started", "terminal", "pytest -q", {"command": "pytest -q"}
        )
        child.tool_progress_callback(
            "tool.completed", "terminal", None, None,
            duration=0.2, is_error=False, result="1 passed",
        )
        return {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "duration_seconds": 0.2,
        }

    monkeypatch.setattr("tools.delegate_tool._run_single_child", fake_run)

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    monkeypatch.setattr("tools.async_delegation.dispatch_async_delegation", fake_dispatch)

    out = json.loads(spawn_agent("inspect the code", parent_agent=parent))
    path = Path(out["live_transcripts"][0])
    assert path.exists()
    assert path.parent.name == out["id"]

    result = captured["runner"]()
    text = path.read_text(encoding="utf-8")
    assert "checking the code" in text
    assert "-> terminal(pytest -q)" in text
    assert "terminal ok 0.2s: 1 passed" in text
    assert "end status=completed" in text
    assert result["live_transcript"] == str(path)
    assert result["live_transcripts"] == [str(path)]

    manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tasks"][0]["status"] == "completed"


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
    assert "exactly one of prompt, cancel_id, or result_id" in both["error"]

    monkeypatch.setattr(
        "tools.async_delegation.interrupt_delegation", lambda *a, **k: False
    )
    missing = json.loads(spawn_agent(cancel_id="sa_missing", parent_agent=parent))
    assert "No running spawn_agent subagent found" in missing["error"]


def _persist_spawn_result(
    *,
    result_id: str,
    owner: str,
    result: dict,
    completion_type: str = "spawn_result",
    parent_session_id: str = "parent-session",
):
    record = {
        "delegation_id": result_id,
        "session_key": owner,
        "origin_ui_session_id": "",
        "parent_session_id": parent_session_id,
        "completion_type": completion_type,
        "label": "long report",
        "goal": "produce a long report",
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)
    ad._persist_completion(
        {
            "type": completion_type,
            "delegation_id": result_id,
            "status": "completed",
            "completed_at": 2.0,
        },
        result,
    )


def test_result_retrieval_pages_owned_full_report_without_file_tool(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.approval.get_current_session_key",
        lambda default="": "route-key",
    )
    result_id = "sa_page12"
    report = "0123456789abcdefghijklmnopqrstuvwxyz"
    report_dir = tmp_path / "cache" / "delegation" / "live" / result_id
    report_dir.mkdir(parents=True)
    report_path = report_dir / "subagent-summary-0-test.txt"
    report_path.write_text(report, encoding="utf-8")
    _persist_spawn_result(
        result_id=result_id,
        owner="route-key",
        result={
            "status": "completed",
            "summary": "truncated",
            "summary_truncated": True,
            "summary_full_path": str(report_path),
        },
    )

    first = json.loads(
        spawn_agent(
            result_id=result_id,
            offset=10,
            limit=8,
            parent_agent=_parent(),
        )
    )
    assert first == {
        "id": result_id,
        "status": "completed",
        "offset": 10,
        "returned_chars": 8,
        "total_chars": len(report),
        "next_offset": 18,
        "has_more": True,
        "full_result_available": True,
        "content": "abcdefgh",
    }

    final = json.loads(
        spawn_agent(
            result_id=result_id,
            offset=first["next_offset"],
            limit=20,
            parent_agent=_parent(),
        )
    )
    assert final["content"] == "ijklmnopqrstuvwxyz"
    assert final["next_offset"] is None
    assert final["has_more"] is False


def test_result_retrieval_fails_closed_for_foreign_or_wrong_tool_results(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.approval.get_current_session_key",
        lambda default="": "route-key",
    )
    _persist_spawn_result(
        result_id="sa_foreign",
        owner="someone-else",
        parent_session_id="someone-else-parent",
        result={"status": "completed", "summary": "private"},
    )
    _persist_spawn_result(
        result_id="deleg_sync",
        owner="route-key",
        completion_type="async_delegation",
        result={"status": "completed", "summary": "not a spawn result"},
    )

    foreign = json.loads(
        spawn_agent(result_id="sa_foreign", parent_agent=_parent())
    )
    wrong_type = json.loads(
        spawn_agent(result_id="deleg_sync", parent_agent=_parent())
    )
    assert foreign["error"] == (
        "No spawn_agent result found with id 'sa_foreign' in this conversation."
    )
    assert wrong_type["error"] == (
        "No spawn_agent result found with id 'deleg_sync' in this conversation."
    )


def test_result_retrieval_never_reads_untrusted_recorded_path(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "tools.approval.get_current_session_key",
        lambda default="": "route-key",
    )
    outside = tmp_path / "secret.txt"
    outside.write_text("must not be returned", encoding="utf-8")
    _persist_spawn_result(
        result_id="sa_badpath",
        owner="route-key",
        result={
            "status": "completed",
            "summary": "safe truncated copy",
            "summary_truncated": True,
            "summary_full_path": str(outside),
        },
    )

    retrieved = json.loads(
        spawn_agent(result_id="sa_badpath", parent_agent=_parent())
    )
    assert retrieved["content"] == "safe truncated copy"
    assert retrieved["full_result_available"] is False
    assert "unavailable or expired" in retrieved["message"]
    assert "must not be returned" not in json.dumps(retrieved)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"result_id": "sa_x", "offset": -1}, "non-negative integer"),
        ({"result_id": "sa_x", "offset": True}, "non-negative integer"),
        ({"result_id": "sa_x", "limit": 0}, "between 1 and 20000"),
        ({"result_id": "sa_x", "limit": 20_001}, "between 1 and 20000"),
    ],
)
def test_result_retrieval_validates_page_bounds(kwargs, message):
    result = json.loads(spawn_agent(parent_agent=_parent(), **kwargs))
    assert message in result["error"]


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
            "live_transcript": "/tmp/live/sa_abcdef/task-0.log",
            "live_transcripts": ["/tmp/live/sa_abcdef/task-0.log"],
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
    assert evt["live_transcript"] == "/tmp/live/sa_abcdef/task-0.log"
    assert evt["live_transcripts"] == ["/tmp/live/sa_abcdef/task-0.log"]


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
            result_id=None,
            offset=None,
            limit=None,
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
            result_id=None,
            offset=None,
            limit=None,
            parent_agent=agent,
        )

        call.reset_mock()
        result = run_agent.AIAgent._dispatch_spawn_agent(
            agent,
            {"result_id": "sa_123abc", "offset": 12, "limit": 34},
        )
        assert result == '{"ok":true}'
        call.assert_called_once_with(
            prompt=None,
            label=None,
            cancel_id=None,
            result_id="sa_123abc",
            offset=12,
            limit=34,
            parent_agent=agent,
        )
