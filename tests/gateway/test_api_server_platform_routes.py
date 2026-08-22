"""Behavioral tests for plugin routes hosted by the shared API server."""

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.api_server import APIRouteConfigurationError, APIServerAdapter


async def _handler(_request):
    return None


def _entry(name: str, routes) -> PlatformEntry:
    return PlatformEntry(
        name=name,
        label=name.title(),
        adapter_factory=lambda config: object(),
        check_fn=lambda: True,
        api_route_factory=lambda _api: routes,
    )


def _api_with_platform(name: str, *, enabled: bool = True) -> APIServerAdapter:
    api = APIServerAdapter(PlatformConfig(enabled=True))
    api.gateway_runner = SimpleNamespace(
        config=GatewayConfig.from_dict(
            {"platforms": {name: {"enabled": enabled}}}
        )
    )
    return api


def test_enabled_platform_routes_are_composed_with_native_routes():
    entry = _entry("route-test", [("POST", "/api/route-test/events", _handler)])
    platform_registry.register(entry)
    try:
        api = _api_with_platform(entry.name)
        routes = {
            (method, path): handler
            for method, path, handler in api._combined_http_route_table()
        }
        assert routes[("GET", "/health")] == api._handle_health
        assert routes[("POST", "/api/route-test/events")] is _handler
    finally:
        platform_registry.unregister(entry.name)


def test_disabled_platform_route_factory_is_not_evaluated():
    evaluated = False

    def factory(_api):
        nonlocal evaluated
        evaluated = True
        return [("GET", "/api/disabled", _handler)]

    entry = _entry("disabled-route-test", [])
    entry.api_route_factory = factory
    platform_registry.register(entry)
    try:
        api = _api_with_platform(entry.name, enabled=False)
        paths = {path for _method, path, _handler in api._combined_http_route_table()}
        assert "/api/disabled" not in paths
        assert evaluated is False
    finally:
        platform_registry.unregister(entry.name)


@pytest.mark.parametrize(
    "routes",
    [
        [("TRACE", "/api/bad", _handler)],
        [("GET", "api/missing-leading-slash", _handler)],
        [("GET", "/p/{profile}/api/already-prefixed", _handler)],
        [("GET", "/api/not-callable", None)],
        [("GET", "/api/missing-handler")],
    ],
)
def test_invalid_platform_route_rows_fail_closed(routes):
    entry = _entry("invalid-route-test", routes)
    platform_registry.register(entry)
    try:
        api = _api_with_platform(entry.name)
        with pytest.raises(APIRouteConfigurationError) as exc:
            api._combined_http_route_table()
        assert exc.value.code == "api_server_invalid_route_provider"
    finally:
        platform_registry.unregister(entry.name)


def test_native_route_collision_is_fatal_and_names_both_owners():
    entry = _entry("collision-test", [("GET", "/health", _handler)])
    platform_registry.register(entry)
    try:
        api = _api_with_platform(entry.name)
        with pytest.raises(APIRouteConfigurationError) as exc:
            api._combined_http_route_table()
        assert exc.value.code == "api_server_route_collision"
        assert "api_server" in str(exc.value)
        assert entry.name in str(exc.value)
    finally:
        platform_registry.unregister(entry.name)


@pytest.mark.asyncio
async def test_connect_marks_route_collision_nonretryable(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "a" * 64)
    entry = _entry("connect-collision-test", [("GET", "/health", _handler)])
    platform_registry.register(entry)
    api = _api_with_platform(entry.name)
    try:
        assert await api.connect() is False
        assert api.has_fatal_error is True
        assert api.fatal_error_code == "api_server_route_collision"
        assert api.fatal_error_retryable is False
    finally:
        await api.disconnect()
        platform_registry.unregister(entry.name)
