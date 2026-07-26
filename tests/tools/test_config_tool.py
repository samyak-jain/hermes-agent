from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_cli import config as config_module
from hermes_cli import managed_scope
from hermes_cli.agent_config import (
    AgentConfigError,
    apply_change,
    apply_rollback,
    history,
    inspect_config,
    prepare_change,
    prepare_rollback,
)
from tools import config_tool as config_tool_module


@pytest.fixture()
def broker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    managed = tmp_path / "managed"
    for subdir in ("cron", "sessions", "logs", "memories"):
        (home / subdir).mkdir(parents=True, exist_ok=True)
    managed.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "display": {"skin": "default", "compact": False},
                "memory": {"memory_char_limit": 2200},
                "code_execution": {"timeout": 120},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (managed / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_config": {
                    "enabled": True,
                    "ownership_mode": "unmanaged",
                },
                "model": {"default": "managed-model"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    config_module._LOAD_CONFIG_CACHE.clear()
    config_module._RAW_CONFIG_CACHE.clear()
    config_module._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_module._HERMES_HOME_ENSURED.clear()
    managed_scope.invalidate_managed_cache()
    yield home
    config_module._LOAD_CONFIG_CACHE.clear()
    config_module._RAW_CONFIG_CACHE.clear()
    config_module._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    config_module._HERMES_HOME_ENSURED.clear()
    managed_scope.invalidate_managed_cache()


def _result(**kwargs):
    return json.loads(config_tool_module.config_tool(**kwargs))


def test_inspect_reports_effective_source_without_exposing_policy(broker_home: Path):
    result = inspect_config()
    by_path = {item["path"]: item for item in result["settings"]}

    assert by_path["display.skin"] == {
        "path": "display.skin",
        "value": "default",
        "source": "user",
        "editable": True,
        "classification": "agent_owned",
        "apply": "next_session",
    }
    assert by_path["model.default"]["value"] == "managed-model"
    assert by_path["model.default"]["source"] == "managed"
    assert by_path["model.default"]["editable"] is False
    assert by_path["model.default"]["classification"] == "operator_managed"
    assert not any(item["path"].startswith("agent_config.") for item in result["settings"])


def test_unmanaged_mode_exposes_every_recognized_non_secret_leaf(
    broker_home: Path,
):
    result = inspect_config()
    by_path = {item["path"]: item for item in result["settings"]}

    # This is intentionally outside the legacy curated preference allowlist.
    inspected = inspect_config("code_execution.timeout")
    assert inspected["editable"] is True
    assert inspected["classification"] == "agent_owned"
    assert inspected["apply"] == "restart_required"
    assert by_path["display.skin"]["editable"] is True
    prepared = prepare_change(
        operation="set",
        path="code_execution.timeout",
        value=121,
        reason="operator asked",
    )
    assert prepared["classification"] == "agent_owned"


def test_unmanaged_mode_rejects_internal_metadata(broker_home: Path):
    with pytest.raises(AgentConfigError, match="Internal configuration metadata"):
        prepare_change(
            operation="set",
            path="_config_version",
            value=99,
            reason="operator asked",
        )


def test_allowlist_mode_remains_backward_compatible(
    broker_home: Path,
):
    managed = Path(os.environ["HERMES_MANAGED_DIR"])
    (managed / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "agent_config": {
                    "enabled": True,
                    "editable_paths": ["display.*"],
                    "guarded_paths": ["model.default"],
                },
                "model": {"default": "managed-model"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    managed_scope.invalidate_managed_cache()
    config_module.invalidate_config_caches()

    assert inspect_config("display.skin")["classification"] == "safe"
    with pytest.raises(AgentConfigError, match="not a recognized or operator-authorized"):
        inspect_config("code_execution.timeout")


def test_managed_and_secret_shaped_paths_fail_closed(broker_home: Path):
    with pytest.raises(AgentConfigError, match="managed"):
        prepare_change(
            operation="set",
            path="model.default",
            value="other-model",
            reason="operator asked",
        )
    with pytest.raises(AgentConfigError, match="credential-shaped"):
        prepare_change(
            operation="set",
            path="display.api_key",
            value="not-even-a-real-key",
            reason="operator asked",
        )
    with pytest.raises(AgentConfigError, match="broker policy"):
        prepare_change(
            operation="set",
            path="agent_config.enabled",
            value=False,
            reason="operator asked",
        )


def test_set_requires_approval_and_writes_atomically(
    broker_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        config_tool_module, "_approval", lambda _prepared: {"approved": True}
    )
    result = _result(
        action="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
        actor="session=test",
    )

    assert result["success"] is True
    assert result["apply"] == "next_session"
    raw = yaml.safe_load((broker_home / "config.yaml").read_text(encoding="utf-8"))
    assert raw["display"]["skin"] == "pastel"
    audit = history()
    assert audit["changes"][-1]["path"] == "display.skin"
    assert "value" not in audit["changes"][-1]
    backup = (
        broker_home
        / "state"
        / "agent-config"
        / "revisions"
        / f"{result['revision']}.yaml"
    )
    assert backup.exists()
    assert oct(backup.stat().st_mode & 0o777) == "0o600"


def test_denied_approval_changes_nothing(
    broker_home: Path, monkeypatch: pytest.MonkeyPatch
):
    before = (broker_home / "config.yaml").read_bytes()
    monkeypatch.setattr(
        config_tool_module,
        "_approval",
        lambda _prepared: {"approved": False, "message": "denied"},
    )
    result = _result(
        action="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
    )
    assert result["success"] is False
    assert (broker_home / "config.yaml").read_bytes() == before


def test_approval_race_does_not_clobber_external_change(broker_home: Path):
    prepared = prepare_change(
        operation="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
    )
    path = broker_home / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["display"]["compact"] = True
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(AgentConfigError, match="changed while approval"):
        apply_change(prepared)
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["display"]["skin"] == "default"
    assert after["display"]["compact"] is True


def test_failed_audit_restores_original_config(
    broker_home: Path, monkeypatch: pytest.MonkeyPatch
):
    prepared = prepare_change(
        operation="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
    )
    before = (broker_home / "config.yaml").read_bytes()
    monkeypatch.setattr(
        "hermes_cli.agent_config._append_audit",
        lambda _record: (_ for _ in ()).throw(OSError("audit disk unavailable")),
    )

    with pytest.raises(AgentConfigError, match="automatically rolled back"):
        apply_change(prepared)

    assert (broker_home / "config.yaml").read_bytes() == before
    assert history()["count"] == 0


def test_rollback_restores_exact_bytes_including_comments(broker_home: Path):
    from hermes_cli.config import invalidate_config_caches

    path = broker_home / "config.yaml"
    original = b"# operator comment\ndisplay:\n  skin: default  # keep this\n"
    path.write_bytes(original)
    invalidate_config_caches(path)

    prepared = prepare_change(
        operation="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
    )
    changed = apply_change(prepared)
    rollback = prepare_rollback(
        changed["revision"], reason="operator requested rollback"
    )
    apply_rollback(rollback)

    assert path.read_bytes() == original


def test_rollback_restores_originally_absent_config(broker_home: Path):
    from hermes_cli.config import invalidate_config_caches

    path = broker_home / "config.yaml"
    path.unlink()
    invalidate_config_caches(path)

    prepared = prepare_change(
        operation="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
    )
    changed = apply_change(prepared)
    assert path.exists()
    rollback = prepare_rollback(
        changed["revision"], reason="operator requested rollback"
    )
    apply_rollback(rollback)

    assert not path.exists()


def test_rollback_of_absent_file_rollback_restores_changed_file(broker_home: Path):
    from hermes_cli.config import invalidate_config_caches

    path = broker_home / "config.yaml"
    path.unlink()
    invalidate_config_caches(path)
    changed = apply_change(
        prepare_change(
            operation="set",
            path="display.skin",
            value="pastel",
            reason="operator requested pastel",
        )
    )
    rollback = apply_rollback(
        prepare_rollback(changed["revision"], reason="operator requested rollback")
    )
    assert not path.exists()

    apply_rollback(
        prepare_rollback(
            rollback["revision"], reason="operator requested undoing rollback"
        )
    )
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["display"]["skin"] == "pastel"


def test_noop_set_is_rejected(broker_home: Path):
    with pytest.raises(AgentConfigError, match="already has"):
        prepare_change(
            operation="set",
            path="display.skin",
            value="default",
            reason="operator requested default",
        )


def test_shared_config_lock_is_reentrant_for_one_profile(broker_home: Path):
    from hermes_cli.config import atomic_config_write, config_write_lock

    path = broker_home / "config.yaml"
    with config_write_lock(path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["display"]["compact"] = True
        atomic_config_write(path, raw, sort_keys=False)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["display"]["compact"] is True
    assert oct((broker_home / ".config.yaml.write.lock").stat().st_mode & 0o777) == "0o600"


def test_rollback_only_applies_to_current_revision(
    broker_home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        config_tool_module, "_approval", lambda _prepared: {"approved": True}
    )
    changed = _result(
        action="set",
        path="display.skin",
        value="pastel",
        reason="operator requested pastel",
    )
    prepared = prepare_rollback(
        changed["revision"], reason="operator requested rollback"
    )
    path = broker_home / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["display"]["compact"] = True
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(AgentConfigError, match="changed since that revision"):
        prepare_rollback(changed["revision"], reason="operator requested rollback")

    # The already-prepared rollback also has a final optimistic check.
    with pytest.raises(AgentConfigError, match="approval was pending"):
        apply_rollback(prepared)


def test_value_that_looks_like_secret_is_rejected(broker_home: Path):
    with pytest.raises(AgentConfigError, match="resembles credential"):
        prepare_change(
            operation="set",
            path="display.skin",
            value="Bearer abcdefghijklmnopqrstuvwxyz",
            reason="operator requested it",
        )


def test_config_tool_is_blocked_from_delegated_children():
    from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS

    assert "config" in DELEGATE_BLOCKED_TOOLS


def test_policy_filtered_config_schema_reaches_app_server_bridge(broker_home: Path):
    from types import SimpleNamespace

    import model_tools
    from agent.codex_runtime import _app_server_tool_schemas
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    tools = model_tools.get_tool_definitions(enabled_toolsets=["config"])
    names = {item["function"]["name"] for item in tools}
    assert names == {"config"}

    agent = SimpleNamespace(tools=tools, valid_tool_names={"config"})
    bridged = _app_server_tool_schemas(agent)
    assert [item["name"] for item in bridged] == ["config"]


def test_config_mutation_approval_cannot_be_permanent_or_bypassed_by_yolo(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = {}

    def fake_request(tool_name, reason, **kwargs):
        captured.update(tool_name=tool_name, reason=reason, **kwargs)
        return {"approved": False, "message": "test"}

    monkeypatch.setattr("tools.approval.request_tool_approval", fake_request)
    config_tool_module._approval(
        {
            "operation": "set",
            "path": "display.skin",
            "value": "pastel",
            "reason": "operator requested pastel",
            "expected_hash": "abc",
        }
    )
    assert captured["tool_name"] == "config"
    assert captured["allow_permanent"] is False
    assert captured["allow_yolo"] is False
