"""Tests for the SSH remote execution environment backend."""

import json
import os
import re
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.environments.ssh import SSHEnvironment
from tools.environments import ssh as ssh_env

_SSH_HOST = os.getenv("TERMINAL_SSH_HOST", "")
_SSH_USER = os.getenv("TERMINAL_SSH_USER", "")
_SSH_PORT = int(os.getenv("TERMINAL_SSH_PORT", "22"))
_SSH_KEY = os.getenv("TERMINAL_SSH_KEY", "")

_has_ssh = bool(_SSH_HOST and _SSH_USER)

requires_ssh = pytest.mark.skipif(
    not _has_ssh,
    reason="TERMINAL_SSH_HOST / TERMINAL_SSH_USER not set",
)


def _run(command, task_id="ssh_test", **kwargs):
    from tools.terminal_tool import terminal_tool
    return json.loads(terminal_tool(command, task_id=task_id, **kwargs))


def _cleanup(task_id="ssh_test"):
    from tools.terminal_tool import cleanup_vm
    cleanup_vm(task_id)


class TestBuildSSHCommand:

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    def test_base_flags(self, monkeypatch):
        # ControlMaster flags are POSIX-only (#73927): assert them only
        # where multiplexing is enabled so the test passes on Windows too.
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", True)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        for flag in ("ControlMaster=auto", "ControlPersist=300",
                      "BatchMode=yes", "StrictHostKeyChecking=accept-new"):
            assert flag in cmd

    def test_controlmaster_gated_off_on_windows(self, monkeypatch):
        """#73927: Windows OpenSSH has no Unix-domain ControlMaster, so the
        ControlPath/ControlMaster/ControlPersist options must be omitted —
        passing them fails the connection with 'getsockname failed'."""
        monkeypatch.setattr(ssh_env, "_SSH_MULTIPLEX", False)
        env = SSHEnvironment(host="h", user="u")
        cmd = " ".join(env._build_ssh_command())
        assert "ControlMaster" not in cmd
        assert "ControlPath" not in cmd
        assert "ControlPersist" not in cmd
        # Non-multiplex flags must still be present — the backend works,
        # just without connection pooling.
        assert "BatchMode=yes" in cmd
        assert "StrictHostKeyChecking=accept-new" in cmd
        assert env._build_ssh_command()[-1] == "u@h"


    def test_user_host_suffix(self):
        env = SSHEnvironment(host="h", user="u")
        assert env._build_ssh_command()[-1] == "u@h"


class TestControlSocketPath:
    """Regression tests for issue #11840.

    macOS caps Unix domain socket paths at 104 bytes (sun_path). SSH
    appends a 16-byte random suffix to the control socket path when
    operating in ControlMaster mode. An IPv6 host embedded in the
    filename plus the deeply-nested macOS $TMPDIR easily blows past
    the limit, causing every tool call to fail immediately.
    """

    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr(ssh_env, "_control_dir", None)
        monkeypatch.setattr(ssh_env, "_control_dir_identity", None)
        monkeypatch.setattr("tools.environments.ssh.subprocess.run",
                            lambda *a, **k: subprocess.CompletedProcess([], 0))
        monkeypatch.setattr("tools.environments.ssh.subprocess.Popen",
                            lambda *a, **k: MagicMock(stdout=iter([]),
                                                      stderr=iter([]),
                                                      stdin=MagicMock()))
        monkeypatch.setattr("tools.environments.base.time.sleep", lambda _: None)

    # SSH appends ``.XXXXXXXXXXXXXXXX`` (17 bytes) to the ControlPath in
    # ControlMaster mode; the macOS sun_path field is 104 bytes including
    # the NUL terminator, so the usable path length is 103 bytes.
    _SSH_CONTROLMASTER_SUFFIX = 17
    _MAX_SUN_PATH = 103

    def test_fits_under_macos_socket_limit_with_ipv6_host(self, monkeypatch):
        """A realistic macOS $TMPDIR + IPv6 host must still produce a
        control socket path that fits once SSH appends its ControlMaster
        suffix (see issue #11840)."""
        # Simulate the macOS $TMPDIR shape from the issue traceback —
        # 48 bytes, the typical length of ``/var/folders/XX/YYYYYYYYY/T``.
        fake_tmp = "/var/folders/2t/wbkw5yb158jc3zhswgl7tz9c0000gn/T"
        monkeypatch.setattr(
            "tools.environments.ssh.tempfile.mkdtemp",
            lambda prefix: f"{fake_tmp}/{prefix}12345678",
        )
        # The simulated path doesn't exist on the test host — skip the
        # real chmod so __init__ can proceed.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "chmod", lambda *a, **k: None)

        env = SSHEnvironment(
            host="9373:9b91:4480:558d:708e:e601:24e8:d8d0",
            user="hermes",
            port=22,
        )

        total_len = len(str(env.control_socket)) + self._SSH_CONTROLMASTER_SUFFIX
        assert total_len <= self._MAX_SUN_PATH, (
            f"control socket path would exceed the {self._MAX_SUN_PATH}-byte "
            f"Unix domain socket limit once SSH appends its 16-byte suffix: "
            f"{env.control_socket} (+{self._SSH_CONTROLMASTER_SUFFIX} = {total_len})"
        )

    def test_path_is_owned_by_each_environment(self):
        """Same-target environments must not share teardown authority."""
        first = SSHEnvironment(host="example.com", user="alice", port=2222)
        second = SSHEnvironment(host="example.com", user="alice", port=2222)
        assert first.control_socket != second.control_socket

    @pytest.mark.parametrize(
        ("cleaned_task", "active_task"),
        [
            ("cron", "interactive"),
            ("interactive", "cron"),
        ],
    )
    def test_cleanup_cannot_close_another_same_target_environment(
        self,
        monkeypatch,
        cleaned_task,
        active_task,
    ):
        """Same-target cleanup must not disconnect another active task."""
        commands = []

        def fake_run(command, *args, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(ssh_env.subprocess, "run", fake_run)
        cleaned = SSHEnvironment(
            host="operator", user="root", task_id=cleaned_task
        )
        active = SSHEnvironment(host="operator", user="root", task_id=active_task)
        cleaned.control_socket.touch()
        active.control_socket.touch()

        cleaned.cleanup()

        exit_commands = [
            command for command in commands if "-O" in command and "exit" in command
        ]
        assert exit_commands == [
            [
                "ssh",
                "-o",
                f"ControlPath={cleaned.control_socket}",
                "-O",
                "exit",
                "root@operator",
            ]
        ]
        assert not cleaned.control_socket.exists()
        assert active.control_socket.exists()

    def test_path_differs_for_different_targets(self):
        """Different (user, host, port) triples must produce different paths."""
        base = SSHEnvironment(host="h", user="u", port=22).control_socket
        assert SSHEnvironment(host="h", user="u", port=23).control_socket != base
        assert SSHEnvironment(host="h", user="v", port=22).control_socket != base
        assert SSHEnvironment(host="g", user="u", port=22).control_socket != base

    def test_control_directory_is_private_and_isolated_by_effective_uid(
        self, monkeypatch, tmp_path,
    ):
        """A root diagnostic process cannot poison the gateway socket path."""
        created = []

        def fake_mkdtemp(prefix):
            path = tmp_path / f"{prefix}{len(created):08d}"
            path.mkdir(mode=0o700)
            created.append(path)
            return str(path)

        effective_uid = [0]
        monkeypatch.setattr(ssh_env.tempfile, "mkdtemp", fake_mkdtemp)
        monkeypatch.setattr(ssh_env.os, "geteuid", lambda: effective_uid[0])

        root_env = SSHEnvironment(host="h", user="u")
        effective_uid[0] = 10000
        gateway_env = SSHEnvironment(host="h", user="u")

        assert root_env.control_dir != gateway_env.control_dir
        assert root_env.control_socket != gateway_env.control_socket
        assert root_env.control_dir.stat().st_mode & 0o777 == 0o700
        assert gateway_env.control_dir.stat().st_mode & 0o777 == 0o700


class TestTerminalToolConfig:
    def test_ssh_persistent_default_true(self, monkeypatch):
        """SSH persistent defaults to True (via TERMINAL_PERSISTENT_SHELL)."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.delenv("TERMINAL_PERSISTENT_SHELL", raising=False)
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is True


    def test_ssh_persistent_respects_config(self, monkeypatch):
        """TERMINAL_PERSISTENT_SHELL=false disables SSH persistent by default."""
        monkeypatch.delenv("TERMINAL_SSH_PERSISTENT", raising=False)
        monkeypatch.setenv("TERMINAL_PERSISTENT_SHELL", "false")
        from tools.terminal_tool import _get_env_config
        assert _get_env_config()["ssh_persistent"] is False


class TestSSHPreflight:
    def test_ensure_ssh_available_raises_clear_error_when_missing(self, monkeypatch):
        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: None)

        with pytest.raises(RuntimeError, match="SSH is not installed or not in PATH"):
            ssh_env._ensure_ssh_available()


    def test_ssh_environment_connects_when_ssh_exists(self, monkeypatch):
        called = {"count": 0}

        monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")

        def _fake_establish(self):
            called["count"] += 1

        monkeypatch.setattr(ssh_env.SSHEnvironment, "_establish_connection", _fake_establish)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/home/alice")
        monkeypatch.setattr(ssh_env.SSHEnvironment, "_ensure_remote_dirs", lambda self: None)
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)
        monkeypatch.setattr(ssh_env, "FileSyncManager", lambda **kw: type("M", (), {"sync": lambda self, **k: None})())

        env = ssh_env.SSHEnvironment(host="example.com", user="alice")

        assert called["count"] == 1
        assert env.host == "example.com"
        assert env.user == "alice"


class TestSSHSystemdSupervision:
    @pytest.fixture(autouse=True)
    def _mock_connection(self, monkeypatch):
        monkeypatch.setattr(ssh_env, "_ensure_ssh_available", lambda: None)
        monkeypatch.setattr(
            ssh_env.SSHEnvironment, "_establish_connection", lambda self: None
        )
        monkeypatch.setattr(
            ssh_env.SSHEnvironment, "_detect_remote_home", lambda self: "/root"
        )
        monkeypatch.setattr(ssh_env.SSHEnvironment, "init_session", lambda self: None)

    def test_foreground_command_has_remote_deadline_and_memory_cgroup(
        self, monkeypatch,
    ):
        captured = {}
        proc = MagicMock()

        def fake_popen(cmd, stdin_data=None):
            captured["cmd"] = cmd
            return proc

        monkeypatch.setattr(ssh_env, "_popen_bash", fake_popen)
        env = ssh_env.SSHEnvironment(
            host="sandbox",
            user="root",
            sync_files=False,
            task_id="subagent-123",
            systemd_run=True,
            systemd_slice="hermes-work.slice",
            command_memory_max_mb=1024,
        )

        result = env._run_bash("nix search nixpkgs wacli --json", timeout=180)
        remote = captured["cmd"][-1]

        assert result is proc
        assert "systemd-run" in remote
        assert "--wait" in remote
        assert "RuntimeMaxSec=185s" in remote
        assert "MemoryHigh=819M" in remote
        assert "MemoryMax=1024M" in remote
        assert "--slice=hermes-work.slice" in remote
        assert "nix search nixpkgs wacli --json" in remote
        assert getattr(proc, "_hermes_remote_systemd_unit").startswith(
            "hermes-cmd-"
        )

    def test_kill_stops_remote_unit_before_local_client(self, monkeypatch):
        stopped = []
        proc = MagicMock()
        proc._hermes_remote_systemd_unit = "hermes-cmd-deadbeef"
        env = ssh_env.SSHEnvironment(
            host="sandbox",
            user="root",
            sync_files=False,
            systemd_run=True,
        )
        monkeypatch.setattr(env, "_stop_remote_units", lambda units: stopped.extend(units))

        env._kill_process(proc)

        assert stopped == ["hermes-cmd-deadbeef"]
        proc.kill.assert_called_once()

    def test_background_command_has_hard_lease(self):
        env = ssh_env.SSHEnvironment(
            host="sandbox",
            user="root",
            sync_files=False,
            task_id="subagent-123",
            systemd_run=True,
            systemd_slice="hermes-work.slice",
            command_memory_max_mb=1024,
            background_ttl_seconds=21600,
        )
        env._remote_executable_cache = {
            "bash": "/run/current-system/sw/bin/bash",
            "systemd-run": "/run/current-system/sw/bin/systemd-run",
            "systemctl": "/run/current-system/sw/bin/systemctl",
            "sleep": "/run/current-system/sw/bin/sleep",
            "tail": "/run/current-system/sw/bin/tail",
        }

        built = env.build_background_command(
            command="python /data/worker.py",
            log_path="/tmp/worker.log",
            pid_path="/tmp/worker.pid",
            exit_path="/tmp/worker.exit",
        )

        assert built is not None
        command, unit = built
        assert unit.startswith("hermes-bg-")
        assert "RuntimeMaxSec=21600s" in command
        assert "MemoryMax=1024M" in command
        assert "--expand-environment=no" in command
        assert "/run/current-system/sw/bin/bash" in command
        assert "printf" in command
        assert "/tmp/worker.pid" in command

    @staticmethod
    def _fake_systemd_tools(tmp_path):
        bash = shutil.which("bash")
        sleep = shutil.which("sleep")
        tail = shutil.which("tail")
        assert bash and sleep and tail

        systemd_run = tmp_path / "systemd-run"
        systemd_run.write_text(
            f"""#!{bash}
set -eu
while (( $# )); do
    if [[ "$1" == "--" ]]; then
        shift
        break
    fi
    shift
done
"$@" </dev/null >/dev/null 2>&1 &
"""
        )
        systemd_run.chmod(0o755)

        systemctl = tmp_path / "systemctl"
        systemctl.write_text(
            f"""#!{bash}
case "${{1:-}}" in
    is-active|stop|reset-failed) exit 0 ;;
    show)
        printf '%s\\n' 'ActiveState=failed' 'SubState=failed' \\
            'Result=exit-code' 'ExecMainStatus=127'
        exit 0
        ;;
esac
exit 0
"""
        )
        systemctl.chmod(0o755)
        return {
            "bash": bash,
            "systemd-run": str(systemd_run),
            "systemctl": str(systemctl),
            "sleep": sleep,
            "tail": tail,
        }

    @pytest.mark.parametrize(
        ("payload", "expected_launcher_rc", "expected_unit_rc"),
        [
            ("printf 'benign command ran\\n'", 0, 0),
            ("command_that_does_not_exist_anywhere", 127, 127),
        ],
    )
    def test_background_launcher_uses_numeric_pid_under_restricted_path(
        self,
        tmp_path,
        payload,
        expected_launcher_rc,
        expected_unit_rc,
    ):
        env = ssh_env.SSHEnvironment(
            host="sandbox",
            user="root",
            sync_files=False,
            task_id="restricted-path-test",
            systemd_run=True,
        )
        env._remote_executable_cache = self._fake_systemd_tools(tmp_path)
        log_path = tmp_path / "worker.log"
        pid_path = tmp_path / "worker.pid"
        exit_path = tmp_path / "worker.exit"

        launch, _unit = env.build_background_command(
            command=payload,
            log_path=str(log_path),
            pid_path=str(pid_path),
            exit_path=str(exit_path),
        )
        result = subprocess.run(
            [env._remote_executable_cache["bash"], "-c", launch],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/nix/store/restricted-path-with-no-programs"},
        )

        assert result.returncode == expected_launcher_rc
        assert pid_path.read_text().strip().isdigit()
        assert int(pid_path.read_text().strip()) > 0
        assert exit_path.read_text().strip() == str(expected_unit_rc)
        if expected_launcher_rc == 0:
            match = re.search(r"^HERMES_BG_PID=(\d+)$", result.stdout, re.MULTILINE)
            assert match is not None
            assert match.group(1) == pid_path.read_text().strip()
            assert log_path.read_text() == "benign command ran\n"
        else:
            assert "HERMES_BG_PID=" not in result.stdout
            assert "not found" in result.stderr


def _setup_ssh_env(monkeypatch, persistent: bool):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", _SSH_HOST)
    monkeypatch.setenv("TERMINAL_SSH_USER", _SSH_USER)
    monkeypatch.setenv("TERMINAL_SSH_PERSISTENT", "true" if persistent else "false")
    if _SSH_PORT != 22:
        monkeypatch.setenv("TERMINAL_SSH_PORT", str(_SSH_PORT))
    if _SSH_KEY:
        monkeypatch.setenv("TERMINAL_SSH_KEY", _SSH_KEY)


@requires_ssh
class TestOneShotSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=False)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello")
        assert r["exit_code"] == 0
        assert "hello" in r["output"]


    def test_state_does_not_persist(self):
        _run("export HERMES_ONESHOT_TEST=yes")
        r = _run("echo $HERMES_ONESHOT_TEST")
        assert r["output"].strip() == ""


@requires_ssh
class TestPersistentSSH:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        _setup_ssh_env(monkeypatch, persistent=True)
        yield
        _cleanup()

    def test_echo(self):
        r = _run("echo hello-persistent")
        assert r["exit_code"] == 0
        assert "hello-persistent" in r["output"]

    def test_env_var_persists(self):
        _run("export HERMES_PERSIST_TEST=works")
        r = _run("echo $HERMES_PERSIST_TEST")
        assert r["output"].strip() == "works"


    def test_large_output(self):
        r = _run("seq 1 1000")
        assert r["exit_code"] == 0
        lines = r["output"].strip().splitlines()
        assert len(lines) == 1000
        assert lines[0] == "1"
        assert lines[-1] == "1000"
