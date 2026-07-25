"""Child-scoped terminal backends keep parent execution local."""

from pathlib import Path

import pytest

from tools import terminal_tool
from tools.delegate_tool import _get_child_terminal_overrides, _seed_child_session_cwd
from tools.environments import ssh as ssh_env
from tools.file_tools import _terminal_env_type_for_task


def test_child_ssh_config_resolves_runtime_host_file(tmp_path: Path):
    host_file = tmp_path / "sandbox-ip"
    host_file.write_text("10.233.1.2\n", encoding="utf-8")

    overrides = _get_child_terminal_overrides(
        {
            "child_terminal": {
                "backend": "ssh",
                "cwd": "/data",
                "ssh_host_file": str(host_file),
                "ssh_user": "root",
                "ssh_key": "/run/secrets/sandbox-key",
                "ssh_known_hosts_file": "/run/secrets/sandbox-known-hosts",
                "ssh_sync_files": False,
            }
        }
    )

    assert overrides == {
        "env_type": "ssh",
        "cwd": "/data",
        "ssh_host": "10.233.1.2",
        "ssh_user": "root",
        "ssh_port": 22,
        "ssh_key": "/run/secrets/sandbox-key",
        "ssh_known_hosts_file": "/run/secrets/sandbox-known-hosts",
        "ssh_sync_files": False,
        "ssh_systemd_run": False,
        "ssh_command_memory_max_mb": 0,
        "ssh_background_ttl_seconds": 86400,
    }


def test_child_ssh_config_fails_closed_without_runtime_host(tmp_path: Path):
    with pytest.raises(ValueError, match="requires ssh_host"):
        _get_child_terminal_overrides(
            {
                "child_terminal": {
                    "backend": "ssh",
                    "ssh_host_file": str(tmp_path / "missing"),
                    "ssh_user": "root",
                }
            }
        )


def test_child_ssh_file_sync_is_opt_in():
    overrides = _get_child_terminal_overrides(
        {
            "child_terminal": {
                "backend": "ssh",
                "ssh_host": "10.233.1.2",
                "ssh_user": "root",
            }
        }
    )

    assert overrides["ssh_sync_files"] is False


def test_child_ssh_supervisor_policy_is_propagated():
    overrides = _get_child_terminal_overrides(
        {
            "child_terminal": {
                "backend": "ssh",
                "ssh_host": "10.233.1.2",
                "ssh_user": "root",
                "ssh_systemd_run": True,
                "ssh_systemd_slice": "hermes-work.slice",
                "ssh_command_memory_max_mb": 1024,
                "ssh_background_ttl_seconds": 21600,
            }
        }
    )

    assert overrides["ssh_systemd_run"] is True
    assert overrides["ssh_systemd_slice"] == "hermes-work.slice"
    assert overrides["ssh_command_memory_max_mb"] == 1024
    assert overrides["ssh_background_ttl_seconds"] == 21600


def test_task_override_changes_child_not_parent(monkeypatch):
    task_id = "child-isolated"
    monkeypatch.setenv("TERMINAL_ENV", "local")
    terminal_tool.register_task_env_overrides(
        task_id,
        {
            "env_type": "ssh",
            "cwd": "/data",
            "ssh_host": "10.233.1.2",
            "ssh_user": "root",
            "ssh_sync_files": False,
        },
    )
    try:
        child = terminal_tool.get_task_env_config(task_id)
        parent = terminal_tool.get_task_env_config(None)

        assert child["env_type"] == "ssh"
        assert child["ssh_sync_files"] is False
        assert terminal_tool.resolve_task_overrides(task_id)["cwd"] == "/data"
        assert terminal_tool.get_session_cwd(task_id) == "/data"
        assert parent["env_type"] == "local"
        assert terminal_tool._resolve_container_task_id(task_id) == task_id
        assert _terminal_env_type_for_task(task_id) == "ssh"
    finally:
        terminal_tool.clear_task_env_overrides(task_id)


def test_parent_cwd_seed_does_not_overwrite_explicit_child_cwd():
    parent_id = "parent-session"
    child_id = "child-session"
    terminal_tool.record_session_cwd(parent_id, "/parent")
    terminal_tool.register_task_env_overrides(child_id, {"cwd": "/child"})
    try:
        _seed_child_session_cwd(child_id, parent_id)
        assert terminal_tool.get_session_cwd(child_id) == "/child"
    finally:
        terminal_tool.clear_session_cwd(parent_id)
        terminal_tool.clear_task_env_overrides(child_id)


def test_ssh_no_sync_uses_pinned_known_hosts(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ssh_env, "_ensure_ssh_available", lambda: None)
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", lambda self: None)
    monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/root")
    monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(
        ssh_env.SSHEnvironment,
        "_ensure_remote_dirs",
        lambda self: pytest.fail("file sync must remain disabled"),
    )

    known_hosts = tmp_path / "known_hosts"
    env = ssh_env.SSHEnvironment(
        host="10.233.1.2",
        user="root",
        key_path="/run/secrets/sandbox-key",
        known_hosts_path=str(known_hosts),
        sync_files=False,
    )

    command = env._build_ssh_command()
    assert env._sync_manager is None
    assert f"UserKnownHostsFile={known_hosts}" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "StrictHostKeyChecking=accept-new" not in command
