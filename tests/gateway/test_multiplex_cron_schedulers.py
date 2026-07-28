"""Profile ownership contracts for the multiplexed gateway cron lifecycle."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


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
