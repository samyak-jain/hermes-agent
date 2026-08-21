from __future__ import annotations

import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from plugins.platforms.workshop.auth import WorkshopAuthenticator
from plugins.platforms.workshop.http import WorkshopHTTPController
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
        "payload": {"kind": "file_changed"},
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
