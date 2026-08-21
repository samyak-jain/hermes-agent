"""apply_managed_overlay() — the shared helper used by every standalone loader."""
import textwrap

import pytest


@pytest.fixture
def managed(tmp_path, monkeypatch):
    md = tmp_path / "managed"
    md.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(md))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return md


def _write(md, body):
    (md / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()


def test_overlay_noop_without_scope(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "nope"))
    managed_scope.invalidate_managed_cache()
    src = {"display": {"skin": "user"}}
    assert managed_scope.apply_managed_overlay(src) == {"display": {"skin": "user"}}


def test_overlay_preserves_user_siblings(managed):
    from hermes_cli import managed_scope

    _write(managed, "display:\n  skin: charizard\n")
    out = managed_scope.apply_managed_overlay(
        {"display": {"skin": "user", "show_reasoning": True}}
    )
    assert out["display"]["skin"] == "charizard"
    assert out["display"]["show_reasoning"] is True


def test_managed_exact_tool_list_replaces_user_list(managed):
    from hermes_cli import managed_scope

    _write(managed, """
    agent:
      tool_policy:
        mode: allowlist
        tools: [clarify, delegate_task, memory, skills_list, skill_manage]
        gateway_override_authority: managed_only
    """)
    out = managed_scope.apply_managed_overlay({
        "agent": {"tool_policy": {"mode": "unrestricted", "tools": ["terminal"]}}
    })
    assert out["agent"]["tool_policy"]["mode"] == "allowlist"
    assert out["agent"]["tool_policy"]["tools"] == [
        "clarify", "delegate_task", "memory", "skills_list", "skill_manage"
    ]
    assert out["agent"]["tool_policy"]["gateway_override_authority"] == "managed_only"

