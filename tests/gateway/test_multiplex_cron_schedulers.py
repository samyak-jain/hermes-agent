"""Profile ownership contracts for the multiplexed gateway cron lifecycle."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


_PASEO_READINESS_COMMAND = """\
set -e
command -v paseo-handoff >/dev/null
command -v paseo-desktop >/dev/null
test -s /run/secrets/paseo-host
paseo-desktop workspace ls >/dev/null
printf 'paseo-ready\\n'
"""


@pytest.mark.asyncio
async def test_multiplex_starts_and_stops_one_scoped_scheduler_per_profile(
    tmp_path,
    monkeypatch,
):
    import gateway.run as gateway_run
    import hermes_constants
    from cron import scheduler_provider
    from hermes_cli import profiles

    default_home = tmp_path / "hermes"
    named_home = default_home / "profiles" / "vegapunk"
    default_home.mkdir()
    named_home.mkdir(parents=True)
    default_adapters = {"discord": object()}
    named_adapters = {"discord": object()}
    barrier = threading.Barrier(2)
    providers = {}

    class FakeProvider:
        name = "fake"

        def __init__(self, profile_name):
            self.profile_name = profile_name
            self.started = threading.Event()
            self.start_home = None
            self.start_adapters = None
            self.stop_home = None
            self.stop_calls = 0

        def start(self, stop_event, *, adapters=None, loop=None, **kwargs):
            self.start_home = hermes_constants.get_hermes_home().resolve()
            self.start_adapters = adapters
            self.started.set()
            barrier.wait(timeout=3)
            stop_event.wait(3)

        def stop(self):
            self.stop_calls += 1
            self.stop_home = hermes_constants.get_hermes_home().resolve()

    def resolve_provider():
        home = hermes_constants.get_hermes_home().resolve()
        name = "vegapunk" if home == named_home.resolve() else "default"
        provider = FakeProvider(name)
        providers[name] = provider
        return provider

    monkeypatch.setattr(
        profiles,
        "profiles_to_serve",
        lambda multiplex: [
            ("default", default_home),
            ("vegapunk", named_home),
        ],
    )
    monkeypatch.setattr(
        scheduler_provider,
        "resolve_cron_scheduler",
        resolve_provider,
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        adapters=default_adapters,
        _profile_adapters={"vegapunk": named_adapters},
        _draining=False,
        _external_drain_active=False,
    )

    runtimes = gateway_run._start_profile_cron_schedulers(
        runner,
        loop=asyncio.get_running_loop(),
    )
    assert len(runtimes) == 2
    assert await asyncio.to_thread(
        lambda: all(runtime.ready.wait(3) for runtime in runtimes)
    )
    assert set(providers) == {"default", "vegapunk"}
    assert await asyncio.to_thread(
        lambda: all(provider.started.wait(3) for provider in providers.values())
    )
    assert providers["default"].start_home == default_home.resolve()
    assert providers["vegapunk"].start_home == named_home.resolve()
    assert providers["default"].start_adapters is default_adapters
    assert providers["vegapunk"].start_adapters is named_adapters

    await gateway_run._stop_profile_cron_schedulers(runtimes)

    assert all(runtime.thread and not runtime.thread.is_alive() for runtime in runtimes)
    assert providers["default"].stop_calls == 1
    assert providers["vegapunk"].stop_calls == 1
    assert providers["default"].stop_home == default_home.resolve()
    assert providers["vegapunk"].stop_home == named_home.resolve()


@pytest.mark.asyncio
async def test_single_named_profile_keeps_primary_adapter_map(
    tmp_path,
    monkeypatch,
):
    import gateway.run as gateway_run
    from cron import scheduler_provider
    from hermes_cli import profiles

    named_home = tmp_path / "profiles" / "vegapunk"
    named_home.mkdir(parents=True)
    primary_adapters = {"discord": object()}
    started = threading.Event()
    seen = {}

    class FakeProvider:
        name = "fake"

        def start(self, stop_event, *, adapters=None, loop=None, **kwargs):
            seen["adapters"] = adapters
            started.set()
            stop_event.wait(3)

        def stop(self):
            return None

    monkeypatch.setattr(
        profiles,
        "profiles_to_serve",
        lambda multiplex: [("vegapunk", named_home)],
    )
    monkeypatch.setattr(
        scheduler_provider,
        "resolve_cron_scheduler",
        lambda: FakeProvider(),
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False),
        adapters=primary_adapters,
        _profile_adapters={},
        _draining=False,
        _external_drain_active=False,
    )

    runtimes = gateway_run._start_profile_cron_schedulers(
        runner,
        loop=asyncio.get_running_loop(),
    )
    assert await asyncio.to_thread(started.wait, 3)
    assert seen["adapters"] is primary_adapters
    await gateway_run._stop_profile_cron_schedulers(runtimes)


@pytest.mark.asyncio
async def test_secondary_cron_executes_with_profile_token_and_ready_operator_terminal(
    tmp_path,
    monkeypatch,
):
    """Pin profile discovery, credentials, sandbox, relay, and continuation."""
    import gateway.run as gateway_run
    from agent.secret_scope import get_secret
    from cron import executions, jobs, scheduler, scheduler_provider
    from gateway.config import Platform
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.config import load_config
    from hermes_cli.profiles import get_active_profile_name
    from hermes_time import now as hermes_now
    from tools import terminal_tool
    from tools.cronjob_tools import cronjob
    from tools.process_registry import process_registry

    default_home = tmp_path / "hermes"
    named_home = default_home / "profiles" / "vegapunk"
    default_home.mkdir()
    named_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    (default_home / ".env").write_text(
        "BWS_ACCESS_TOKEN=default-profile-token\n",
        encoding="utf-8",
    )
    (named_home / ".env").write_text(
        "BWS_ACCESS_TOKEN=vegapunk-profile-token\n",
        encoding="utf-8",
    )
    sandbox_ip_file = tmp_path / "sandbox-ip"
    operator_ip_file = tmp_path / "operator-ip"
    sandbox_ip_file.write_text("10.0.0.2\n", encoding="utf-8")
    operator_ip_file.write_text("10.0.0.3\n", encoding="utf-8")
    config = f"""
cron:
  terminal:
    backend: ssh
    ssh_host_file: {sandbox_ip_file}
    ssh_user: root
  profile_terminal:
    vegapunk:
      backend: ssh
      ssh_host_file: {operator_ip_file}
      ssh_user: root
      ssh_key: /run/secrets/operator-ssh-key
      ssh_known_hosts_file: /run/secrets/operator-known-hosts
"""
    (default_home / "config.yaml").write_text(config, encoding="utf-8")
    (named_home / "config.yaml").write_text(config, encoding="utf-8")

    context_tokens = set_session_vars(
        platform="discord",
        chat_id="vegapunk-operator",
        session_key="agent:vegapunk:discord:dm:vegapunk-operator",
        profile="vegapunk",
    )
    try:
        with gateway_run._profile_runtime_scope(named_home):
            created = cronjob(
                action="create",
                prompt="check Paseo completions",
                schedule=hermes_now().isoformat(),
                repeat=1,
                deliver="origin",
                agent_respond=True,
                name="secondary profile wake-up regression",
            )
            assert json.loads(created)["success"] is True
            stored = jobs.load_jobs()
            stored[0]["next_run_at"] = "2020-01-01T00:00:00+00:00"
            jobs.save_jobs(stored)
    finally:
        clear_session_vars(context_tokens)

    named_adapter = SimpleNamespace(
        supports_async_delivery=True,
        handle_message=lambda _event: None,
    )
    named_adapters = {Platform.DISCORD: named_adapter}
    captured = {}
    tick_finished = threading.Event()

    class ReadyOperatorEnvironment:
        cwd = "/data"

        def execute(self, command, **_kwargs):
            # This intentionally rejects the shared-/data false positive:
            # local handoff state is insufficient unless PATH, the mounted
            # credential, and a relay-touching round trip are all required.
            assert command == _PASEO_READINESS_COMMAND
            return {"output": "paseo-ready\n", "returncode": 0}

    def fake_create_environment(*, env_type, ssh_config=None, **_kwargs):
        assert env_type == "ssh"
        assert ssh_config["host"] == "10.0.0.3"
        return ReadyOperatorEnvironment()

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})

    def fake_run_one_job(job, *, adapters=None, loop=None, verbose=False):
        profile = get_active_profile_name()
        cfg = load_config()
        task_id = "cron-profile-routing-regression"
        assert scheduler._register_cron_terminal(task_id, cfg) is True
        try:
            terminal = terminal_tool.resolve_task_overrides(task_id)
            readiness = json.loads(
                terminal_tool.terminal_tool(
                    _PASEO_READINESS_COMMAND,
                    task_id=task_id,
                    force=True,
                )
            )
            assert readiness["exit_code"] == 0
            assert readiness["output"] == "paseo-ready"
            assert scheduler._inject_cron_agent_response(
                job,
                readiness["output"],
                named_adapter,
                Platform.DISCORD,
                "vegapunk-operator",
                None,
                loop,
            ) is True
            continuation = process_registry.completion_queue.get_nowait()
        finally:
            terminal_tool.clear_task_env_overrides(task_id)
            terminal_tool._active_environments.pop(task_id, None)
        captured.update(
            {
                "profile": profile,
                "token": get_secret("BWS_ACCESS_TOKEN"),
                "ssh_host": terminal.get("ssh_host"),
                "readiness": readiness["output"].strip(),
                "origin_profile": (job.get("origin") or {}).get("profile"),
                "deliver": job.get("deliver"),
                "agent_respond": job.get("agent_respond"),
                "continuation_profile": continuation.get("profile"),
                "adapters": adapters,
            }
        )
        scheduler.mark_job_run(job["id"], True)
        executions.finish_execution(job["execution_id"], success=True)
        tick_finished.set()
        return True

    monkeypatch.setattr(scheduler, "run_one_job", fake_run_one_job)

    class TickOnceProvider:
        name = "tick-once"

        def start(self, stop_event, *, adapters=None, loop=None, **kwargs):
            scheduler.tick(
                verbose=False,
                adapters=adapters,
                loop=loop,
                sync=True,
            )
            stop_event.wait(3)

        def stop(self):
            return None

    monkeypatch.setattr(
        scheduler_provider,
        "resolve_cron_scheduler",
        lambda: TickOnceProvider(),
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        adapters={"discord": object()},
        _profile_adapters={"vegapunk": named_adapters},
        _draining=False,
        _external_drain_active=False,
    )

    runtimes = gateway_run._start_profile_cron_schedulers(
        runner,
        loop=asyncio.get_running_loop(),
    )
    assert await asyncio.to_thread(tick_finished.wait, 3)
    await gateway_run._stop_profile_cron_schedulers(runtimes)

    assert captured == {
        "profile": "vegapunk",
        "token": "vegapunk-profile-token",
        "ssh_host": "10.0.0.3",
        "readiness": "paseo-ready",
        "origin_profile": "vegapunk",
        "deliver": "origin",
        "agent_respond": True,
        "continuation_profile": "vegapunk",
        "adapters": named_adapters,
    }


def test_misfire_sweep_uses_each_profile_home_and_adapter_map(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    import hermes_constants
    from cron import scheduler_provider

    default_home = tmp_path / "hermes"
    named_home = default_home / "profiles" / "vegapunk"
    default_home.mkdir()
    named_home.mkdir(parents=True)
    seen = []

    def fake_fire(provider, *, adapters, loop):
        seen.append(
            (
                provider,
                hermes_constants.get_hermes_home().resolve(),
                adapters,
                loop,
            )
        )
        return 1

    monkeypatch.setattr(scheduler_provider, "fire_overdue_jobs", fake_fire)
    default_adapters = {"discord": object()}
    named_adapters = {"discord": object()}
    runtimes = [
        SimpleNamespace(
            provider="default-provider",
            profile_home=default_home,
            adapters=default_adapters,
        ),
        SimpleNamespace(
            provider="named-provider",
            profile_home=named_home,
            adapters=named_adapters,
        ),
    ]

    assert gateway_run._fire_overdue_for_profile_cron_runtimes(
        runtimes,
        loop="loop",
    ) == 2
    assert seen == [
        ("default-provider", default_home.resolve(), default_adapters, "loop"),
        ("named-provider", named_home.resolve(), named_adapters, "loop"),
    ]
