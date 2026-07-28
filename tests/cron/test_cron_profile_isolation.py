"""Regression tests for #4707 — cron must be per-profile.

Design intent (Teknium, June 2026): a profile's cron jobs both LIVE in that
profile's HERMES_HOME and EXECUTE under it.

- Storage: a job created under profile ``coder`` writes to
  ``~/.hermes/profiles/coder/cron/jobs.json`` — NOT the shared default root.
- Execution: the profile-scoped gateway's in-process ticker resolves the
  active HERMES_HOME (profile home) at call time, so jobs run with that
  profile's ``.env`` / ``config.yaml`` / scripts / skills.

This is the opposite direction from the (reverted) #50112/#32091 "anchor at the
shared root" approach. Anchoring at the root funnels every profile's jobs into
one store and runs them under whatever HERMES_HOME the ticker happens to have —
leaking config/credentials/skills across profiles, the security boundary #4707
was filed for. These tests pin per-profile isolation so a stale-branch merge or
a re-anchor "fix" can't silently flip it back.
"""
import importlib
import json
from pathlib import Path


def _set_profile_env(monkeypatch, root: Path, profile_home: Path) -> None:
    """Pretend the platform default root is ``root`` and the active
    HERMES_HOME is a profile under it (``<root>/profiles/<name>``)."""
    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: root
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))


def test_cron_storage_anchors_at_profile_home(tmp_path, monkeypatch):
    """Under a profile HERMES_HOME (<root>/profiles/<name>), the cron store
    resolves to <profile>/cron, NOT the shared <root>/cron."""
    root = tmp_path / "hermes_home"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    _set_profile_env(monkeypatch, root, profile_home)

    import hermes_constants

    # Sanity: the override is wired the way the gateway sees it.
    assert hermes_constants.get_hermes_home().resolve() == profile_home.resolve()
    assert hermes_constants.get_default_hermes_root().resolve() == root.resolve()

    # cron/jobs.py computes HERMES_DIR from get_hermes_home() at import, so a
    # fresh import under this env anchors the store at <profile>/cron.
    import cron.jobs as jobs

    importlib.reload(jobs)
    try:
        assert jobs.HERMES_DIR.resolve() == profile_home.resolve()
        assert (
            jobs.JOBS_FILE.resolve()
            == (profile_home / "cron" / "jobs.json").resolve()
        )
        # The shared-root path must NOT be the store — that would re-break
        # per-profile isolation (#4707).
        assert (
            jobs.JOBS_FILE.resolve() != (root / "cron" / "jobs.json").resolve()
        )
    finally:
        monkeypatch.undo()
        importlib.reload(jobs)


def test_cron_lock_path_anchors_at_profile_home(tmp_path, monkeypatch):
    """The tick lock is also profile-scoped, so two profile gateways tick
    independently instead of contending on one shared lock."""
    root = tmp_path / "hermes_home"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    _set_profile_env(monkeypatch, root, profile_home)

    import cron.scheduler as scheduler

    lock_dir, lock_file = scheduler._get_lock_paths()
    assert lock_dir.resolve() == (profile_home / "cron").resolve()
    assert lock_file.resolve() == (profile_home / "cron" / ".tick.lock").resolve()
    assert lock_dir.resolve() != (root / "cron").resolve()


def test_cron_execution_home_follows_active_profile(tmp_path, monkeypatch):
    """Execution-time home resolution (.env / config.yaml / scripts) follows
    the active profile, not the shared root — so a profile gateway runs its
    jobs with that profile's runtime config."""
    root = tmp_path / "hermes_home"
    profile_home = root / "profiles" / "coder"
    profile_home.mkdir(parents=True)

    _set_profile_env(monkeypatch, root, profile_home)

    import cron.scheduler as scheduler

    # The module-level test override must be clear so the dynamic path runs.
    monkeypatch.setattr(scheduler, "_hermes_home", None, raising=False)
    assert scheduler._get_hermes_home().resolve() == profile_home.resolve()
    assert scheduler._get_hermes_home().resolve() != root.resolve()


def test_cron_storage_unaffected_when_no_profile(tmp_path, monkeypatch):
    """With no profile (HERMES_HOME == root), the store is the root's cron dir
    — unchanged behavior for single-profile installs."""
    root = tmp_path / "hermes_home"
    root.mkdir(parents=True)

    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "_get_platform_default_hermes_home", lambda: root
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    import cron.jobs as jobs

    importlib.reload(jobs)
    try:
        assert jobs.HERMES_DIR.resolve() == root.resolve()
        assert jobs.JOBS_FILE.resolve() == (root / "cron" / "jobs.json").resolve()
    finally:
        monkeypatch.undo()
        importlib.reload(jobs)


def test_heartbeat_and_execution_ledger_follow_runtime_profile(tmp_path):
    """Import-time defaults must not funnel multiplex threads into one store."""
    from cron import executions, jobs
    from gateway.run import _profile_runtime_scope

    default_home = tmp_path / "hermes"
    named_home = default_home / "profiles" / "vegapunk"
    default_home.mkdir()
    named_home.mkdir(parents=True)

    with _profile_runtime_scope(default_home):
        jobs.record_ticker_heartbeat(success=True)
        default_execution = executions.create_execution(
            "same-job-id",
            source="builtin",
        )
    with _profile_runtime_scope(named_home):
        jobs.record_ticker_heartbeat(success=True)
        named_execution = executions.create_execution(
            "same-job-id",
            source="builtin",
        )

    assert (default_home / "cron" / "ticker_heartbeat").is_file()
    assert (named_home / "cron" / "ticker_heartbeat").is_file()
    assert (default_home / "cron" / "ticker_last_success").is_file()
    assert (named_home / "cron" / "ticker_last_success").is_file()
    assert (default_home / "cron" / "executions.db").is_file()
    assert (named_home / "cron" / "executions.db").is_file()
    assert default_execution["id"] != named_execution["id"]


def test_scoped_tool_created_overdue_oneshots_run_with_profile_origin_and_adapters(
    tmp_path,
    monkeypatch,
):
    """Exercise create-tool → persisted store → due scan → execution per profile."""
    from cron import executions, jobs, scheduler
    from gateway.run import _profile_runtime_scope
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_time import now as hermes_now
    from tools.cronjob_tools import cronjob

    default_home = tmp_path / "hermes"
    named_home = default_home / "profiles" / "vegapunk"
    default_home.mkdir()
    named_home.mkdir(parents=True)
    adapter_maps = {
        "default": {"discord": object()},
        "vegapunk": {"discord": object()},
    }
    ran = []

    def fake_run_one_job(job, *, adapters=None, loop=None, verbose=False):
        from hermes_constants import get_hermes_home

        home = get_hermes_home().resolve()
        profile = "vegapunk" if home == named_home.resolve() else "default"
        ran.append(
            {
                "profile": profile,
                "home": home,
                "adapters": adapters,
                "origin": job.get("origin"),
                "agent_respond": job.get("agent_respond"),
            }
        )
        scheduler.mark_job_run(job["id"], True)
        executions.finish_execution(job["execution_id"], success=True)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", fake_run_one_job)

    for profile, home in (
        ("default", default_home),
        ("vegapunk", named_home),
    ):
        tokens = set_session_vars(
            platform="discord",
            chat_id=f"{profile}-channel",
            session_key=f"agent:{profile}:discord:dm:{profile}-channel",
            profile=profile,
        )
        try:
            with _profile_runtime_scope(home):
                created = json.loads(
                    cronjob(
                        action="create",
                        prompt=f"report for {profile}",
                        schedule=hermes_now().isoformat(),
                        repeat=1,
                        deliver="origin",
                        agent_respond=True,
                        name=f"{profile} due job",
                    )
                )
                assert created["success"] is True
                stored = jobs.load_jobs()
                assert len(stored) == 1
                # Simulate the gateway being down for hours. Existing
                # next_run_at remains eligible for exactly one catch-up fire.
                stored[0]["next_run_at"] = "2020-01-01T00:00:00+00:00"
                jobs.save_jobs(stored)
        finally:
            clear_session_vars(tokens)

    for profile, home in (
        ("default", default_home),
        ("vegapunk", named_home),
    ):
        with _profile_runtime_scope(home):
            assert scheduler.tick(
                verbose=False,
                adapters=adapter_maps[profile],
                sync=True,
            ) == 1
            assert jobs.load_jobs() == []

    assert {item["profile"] for item in ran} == {"default", "vegapunk"}
    for item in ran:
        profile = item["profile"]
        assert item["adapters"] is adapter_maps[profile]
        assert item["origin"]["profile"] == profile
        assert item["origin"]["chat_id"] == f"{profile}-channel"
        assert item["agent_respond"] is True
