from __future__ import annotations

import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from plugins.platforms.workshop.auth import WorkshopAuthenticator
from plugins.platforms.workshop.http import WorkshopHTTPController, create_api_routes
from plugins.platforms.workshop.storage import WorkshopLedger


WORKSHOP_KEY = "a" * 64
API_SERVER_KEY = "b" * 64


def _turn_body():
    return {
        "protocol_version": 1,
        "client_turn_id": "client-1",
        "workspace_id": "workspace-1",
        "chat_id": "chat-1",
        "input": {"type": "user", "text": "Build it"},
        "tools": [
            {
                "name": "writeFile",
                "description": "Write a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    }


def _headers(key: str = WORKSHOP_KEY):
    return {"Authorization": f"Bearer {key}"}


def _app(tmp_path, *, adapters=None):
    ledger = WorkshopLedger(tmp_path / "state.db")
    controller = WorkshopHTTPController(
        api_adapter=object(),
        authenticator=WorkshopAuthenticator(WORKSHOP_KEY),
        ledger=ledger,
    )
    app = web.Application()
    app["gateway_runner"] = SimpleNamespace(adapters=adapters or {})
    for method, path, handler in controller.routes():
        app.router.add_route(method, path, handler)
    return app, ledger


@pytest.mark.asyncio
async def test_workshop_routes_require_the_separate_workshop_bearer(tmp_path):
    app, _ledger = _app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        missing = await client.post("/api/workshop/v1/turns", json=_turn_body())
        api_key = await client.post(
            "/api/workshop/v1/turns",
            json=_turn_body(),
            headers=_headers(API_SERVER_KEY),
        )

    assert missing.status == 401
    assert api_key.status == 401


@pytest.mark.asyncio
async def test_missing_workshop_key_disables_only_workshop_routes(
    monkeypatch, caplog
):
    monkeypatch.delenv("WORKSHOP_API_KEY", raising=False)
    controller = WorkshopHTTPController(api_adapter=object())
    app = web.Application()
    app["gateway_runner"] = SimpleNamespace(adapters={})

    async def shared_health(_request):
        return web.json_response({"status": "ok"})

    app.router.add_get("/health", shared_health)
    for method, path, handler in controller.routes():
        app.router.add_route(method, path, handler)

    async with TestClient(TestServer(app)) as client:
        workshop = await client.post(
            "/api/workshop/v1/turns",
            json=_turn_body(),
            headers=_headers(),
        )
        workshop_body = await workshop.json()
        shared = await client.get("/health")
        shared_body = await shared.json()

    assert workshop.status == 503
    assert workshop_body["error"]["code"] == "workshop_unavailable"
    assert shared.status == 200
    assert shared_body == {"status": "ok"}
    assert "workshop_http_initialization_failed disabled=true" in caplog.text


def test_workshop_route_factory_never_eagerly_reads_credentials(monkeypatch):
    monkeypatch.delenv("WORKSHOP_API_KEY", raising=False)
    routes = create_api_routes(object())
    assert ("GET", "/api/workshop/v1/health") in {
        (method, path) for method, path, _handler in routes
    }


@pytest.mark.asyncio
async def test_health_surfaces_durable_wake_dead_letters(tmp_path):
    app, ledger = _app(tmp_path)
    turn, _created = ledger.create_turn(
        client_turn_id="wake-1",
        workspace_id="workspace-1",
        chat_id="chat-1",
        session_key="agent:main:workshop:thread:workspace-1:chat-1",
        session_id="session-1",
        catalog_version="catalog-1",
        request_digest="request-1",
    )
    ledger.record_wake(
        producer_type="spawn_result",
        producer_id="delegation-1",
        turn_id=turn.turn_id,
    )
    ledger.mark_wake_dead_letter(
        producer_type="spawn_result",
        producer_id="delegation-1",
        error="HTTP 403",
    )
    ledger.finish_turn(
        turn_id=turn.turn_id,
        state="error",
        stop_reason="wake_rejected",
    )

    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.get("/api/workshop/v1/health")
        response = await client.get(
            "/api/workshop/v1/health", headers=_headers()
        )
        body = await response.json()

    assert unauthorized.status == 401
    assert response.status == 200
    assert body == {
        "protocol_version": 1,
        "status": "degraded",
        "connected": False,
        "active_turns": 0,
        "dead_letter_wakes": 1,
        "limits": {},
        "client_tool_authority": "per_turn_remote_callback",
    }


@pytest.mark.asyncio
async def test_health_reports_effective_limits_without_secrets(tmp_path):
    behavior = {
        "wake_url": "https://workshop.example.test/wake",
        "max_active_turns": 4,
        "max_pending_remote_calls": 8,
        "max_client_tools": 32,
        "max_tool_schema_bytes": 256 * 1024,
        "max_event_backlog_bytes": 8 * 1024 * 1024,
        "completed_event_retention_seconds": 24 * 60 * 60,
        "turn_timeout_seconds": 15 * 60,
        "remote_tool_timeout_seconds": 5 * 60,
        "wake_timeout_seconds": 10,
    }
    adapter = SimpleNamespace(
        _running=True,
        _behavior=behavior,
        ledger=WorkshopLedger(tmp_path / "adapter-state.db"),
    )
    app, _ledger = _app(tmp_path, adapters={"workshop": adapter})

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/api/workshop/v1/health", headers=_headers()
        )
        body = await response.json()

    assert response.status == 200
    assert body["status"] == "ok"
    assert body["connected"] is True
    assert body["limits"] == {
        key: value for key, value in behavior.items() if key != "wake_url"
    }
    assert "wake_url" not in json.dumps(body)


@pytest.mark.asyncio
async def test_authentication_precedes_resource_identifier_validation(tmp_path):
    app, _ledger = _app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/workshop/v1/turns/bad:id/tool-results/bad:id",
            json={"not": "parsed"},
        )

    assert response.status == 401


@pytest.mark.asyncio
async def test_valid_turn_is_strictly_parsed_before_adapter_availability(tmp_path):
    app, _ledger = _app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        invalid = _turn_body()
        invalid["tools"][0]["parameters"]["properties"]["path"]["pattern"] = ".+"
        rejected = await client.post(
            "/api/workshop/v1/turns", json=invalid, headers=_headers()
        )
        rejected_body = await rejected.json()
        unavailable = await client.post(
            "/api/workshop/v1/turns", json=_turn_body(), headers=_headers()
        )

    assert rejected.status == 400
    assert rejected_body["error"]["code"] == "unsupported_schema_keyword"
    assert unavailable.status == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_after",
    [
        "-1",
        "abc",
        "1.5",
        "+1",
        "",
        pytest.param("9" * 4301, id="over-python-int-limit"),
        pytest.param(str(1 << 63), id="over-sqlite-int-limit"),
    ],
)
async def test_after_seq_is_validated_at_http_ingress_for_post_and_get(
    tmp_path, raw_after
):
    app, _ledger = _app(tmp_path)
    suffix = f"?after_seq={raw_after}"
    async with TestClient(TestServer(app)) as client:
        started = await client.post(
            f"/api/workshop/v1/turns{suffix}",
            json=_turn_body(),
            headers=_headers(),
        )
        replay = await client.get(
            f"/api/workshop/v1/turns/wturn_valid/events{suffix}",
            headers=_headers(),
        )
        started_body = await started.json()
        replay_body = await replay.json()

    assert started.status == 400
    assert started_body["error"]["code"] == "invalid_event_sequence"
    assert replay.status == 400
    assert replay_body["error"]["code"] == "invalid_event_sequence"


@pytest.mark.asyncio
async def test_completed_turn_replay_is_available_without_live_adapter(tmp_path):
    app, ledger = _app(tmp_path)
    turn, _created = ledger.create_turn(
        client_turn_id="client-1",
        workspace_id="workspace-1",
        chat_id="chat-1",
        session_key="agent:main:workshop:thread:workspace-1:chat-1",
        session_id="session-1",
        catalog_version="catalog-1",
        request_digest="request-1",
    )
    ledger.append_event(
        turn_id=turn.turn_id,
        event="text.delta",
        payload={"delta": "hello"},
    )
    ledger.append_event(
        turn_id=turn.turn_id,
        event="thinking.delta",
        payload={"delta": "live only"},
    )
    ledger.finish_turn(
        turn_id=turn.turn_id,
        state="completed",
        stop_reason="complete",
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            f"/api/workshop/v1/turns/{turn.turn_id}/events",
            headers=_headers(),
        )
        body = await response.text()

    assert response.status == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    records = [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert [record["event"] for record in records] == ["text.delta", "turn.end"]
    assert records[-1]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_active_turn_replay_fails_typed_when_adapter_is_down(tmp_path):
    app, ledger = _app(tmp_path)
    turn, _created = ledger.create_turn(
        client_turn_id="client-1",
        workspace_id="workspace-1",
        chat_id="chat-1",
        session_key="agent:main:workshop:thread:workspace-1:chat-1",
        session_id="session-1",
        catalog_version="catalog-1",
        request_digest="request-1",
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            f"/api/workshop/v1/turns/{turn.turn_id}/events",
            headers=_headers(),
        )
        response_body = await response.json()

    assert response.status == 503
    assert response_body["error"]["code"] == "workshop_unavailable"


@pytest.mark.asyncio
async def test_delta_path_and_body_identity_must_match(tmp_path):
    adapter = SimpleNamespace()

    async def ingest(_parsed, **_kwargs):
        return web.json_response({"ok": True})

    adapter.ingest_workshop_delta = ingest
    app, _ledger = _app(tmp_path, adapters={"workshop": adapter})
    body = {
        "protocol_version": 1,
        "delta_id": "delta-1",
        "workspace_id": "workspace-other",
        "chat_id": "chat-1",
        "payload": {
            "type": "file_changed",
            "version": 1,
            "timestamp": "2026-08-22T12:00:00Z",
            "data": {},
        },
    }
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/workshop/v1/sessions/workspace-1/chat-1/deltas",
            json=body,
            headers=_headers(),
        )
        response_body = await response.json()

    assert response.status == 409
    assert response_body["error"]["code"] == "route_identity_mismatch"
