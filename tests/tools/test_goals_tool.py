from __future__ import annotations

import json
from pathlib import Path

import model_tools
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import goals_tool as goals_tool_module
from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS
from tools.registry import invalidate_check_fn_cache


def _result(args: dict, **context) -> dict:
    return json.loads(goals_tool_module.goals_tool(args, **context))


def test_schema_has_no_profile_or_path_and_rejects_unknown_arguments():
    parameters = goals_tool_module.GOALS_SCHEMA["parameters"]
    assert parameters["additionalProperties"] is False
    assert {"profile", "path", "target"}.isdisjoint(parameters["properties"])

    result = _result({"action": "read", "profile": "default"})
    assert result["success"] is False
    assert "never accepts a profile, path, or target" in result["error"]

    description = goals_tool_module.GOALS_SCHEMA["description"]
    assert "operator-owned" in description
    assert "Never invent" in description
    assert "reprioritize, or delete" in description
    assert "faithful reason" in description


def test_update_uses_server_scoped_broker_without_target_arguments(monkeypatch):
    captured = {}
    monkeypatch.setattr(goals_tool_module, "tool_enabled", lambda: True)
    monkeypatch.setattr(goals_tool_module, "approval_required", lambda: False)

    def fake_update(**kwargs):
        captured.update(kwargs)
        return {"success": True, "version": "sha256:new"}

    monkeypatch.setattr(goals_tool_module, "update_goals", fake_update)
    result = _result(
        {
            "action": "update",
            "content": "# Safe",
            "expected_version": "sha256:old",
            "reason": "operator requested it",
        },
        session_id="session-1",
        task_id="task-1",
        tool_call_id="call-1",
    )

    assert result["success"] is True
    assert set(captured) == {"content", "expected_version", "reason", "actor"}
    assert "session=session-1" in captured["actor"]


def test_read_uses_active_server_profile(tmp_path: Path, monkeypatch):
    default = tmp_path / "home"
    named = default / "profiles" / "vegapunk"
    named.mkdir(parents=True)
    (default / "GOALS.md").write_text("default goals", encoding="utf-8")
    (named / "GOALS.md").write_text("named goals", encoding="utf-8")
    monkeypatch.setattr(goals_tool_module, "tool_enabled", lambda: True)

    token = set_hermes_home_override(named)
    try:
        result = _result({"action": "read"})
    finally:
        reset_hermes_home_override(token)

    assert result["profile"] == "vegapunk"
    assert result["content"] == "named goals"
    assert (default / "GOALS.md").read_text(encoding="utf-8") == "default goals"


def test_disabled_policy_fails_closed(monkeypatch):
    monkeypatch.setattr(goals_tool_module, "tool_enabled", lambda: False)
    result = _result({"action": "read"})
    assert result == {
        "success": False,
        "error": "Goals access is disabled by operator policy.",
    }


def test_managed_no_approval_policy_skips_approval(monkeypatch):
    monkeypatch.setattr(goals_tool_module, "tool_enabled", lambda: True)
    monkeypatch.setattr(goals_tool_module, "approval_required", lambda: False)
    monkeypatch.setattr(
        goals_tool_module,
        "_approval",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("approval must not be requested")
        ),
    )
    monkeypatch.setattr(
        goals_tool_module,
        "rollback_goals",
        lambda **_kwargs: {"success": True, "version": "sha256:restored"},
    )

    result = _result({
        "action": "rollback",
        "revision": "20260728T120000Z-1234abcd",
        "expected_version": "sha256:current",
        "reason": "operator requested rollback",
    })
    assert result["success"] is True


def test_tool_is_service_gated_and_not_in_core(monkeypatch):
    monkeypatch.setattr(goals_tool_module, "tool_enabled", lambda: False)
    invalidate_check_fn_cache()
    disabled = model_tools.get_tool_definitions(enabled_toolsets=["goals"])
    assert "goals" not in {item["function"]["name"] for item in disabled}

    monkeypatch.setattr(goals_tool_module, "tool_enabled", lambda: True)
    invalidate_check_fn_cache()
    enabled = model_tools.get_tool_definitions(
        enabled_toolsets=["goals"],
        skip_tool_search_assembly=True,
    )
    assert "goals" in {item["function"]["name"] for item in enabled}

    core = model_tools.get_tool_definitions(enabled_toolsets=["hermes-core"])
    assert "goals" not in {item["function"]["name"] for item in core}


def test_delegated_children_cannot_receive_goals():
    assert "goals" in DELEGATE_BLOCKED_TOOLS
