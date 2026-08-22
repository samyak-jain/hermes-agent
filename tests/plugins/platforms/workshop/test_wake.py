from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from plugins.platforms.workshop.wake import (
    WorkshopWakeClient,
    WorkshopWakeRejectedError,
    WorkshopWakeRetryableError,
)


async def _server(status: int, capture: dict):
    async def handler(request):
        capture["authorization"] = request.headers.get("Authorization")
        capture["idempotency_key"] = request.headers.get("Idempotency-Key")
        capture["payload"] = await request.json()
        return web.Response(status=status)

    app = web.Application()
    app.router.add_post("/wake", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.asyncio
async def test_wake_posts_direction_specific_auth_and_idempotency():
    capture = {}
    server = await _server(202, capture)
    try:
        client = WorkshopWakeClient(
            url=str(server.make_url("/wake")),
            token="outbound-only",
            timeout_seconds=2,
        )
        await client.deliver({"turn_id": "turn-1"}, idempotency_key="wake-1")
    finally:
        await server.close()

    assert capture == {
        "authorization": "Bearer outbound-only",
        "idempotency_key": "wake-1",
        "payload": {"turn_id": "turn-1"},
    }


@pytest.mark.asyncio
async def test_wake_classifies_all_4xx_as_permanent_rejection():
    server = await _server(429, {})
    try:
        client = WorkshopWakeClient(
            url=str(server.make_url("/wake")), token="wake", timeout_seconds=2
        )
        with pytest.raises(WorkshopWakeRejectedError) as caught:
            await client.deliver({}, idempotency_key="wake-1")
    finally:
        await server.close()
    assert caught.value.status == 429


@pytest.mark.asyncio
async def test_wake_failure_never_exposes_outbound_token():
    token = "wake-secret-that-must-not-leak"
    server = await _server(401, {})
    try:
        client = WorkshopWakeClient(
            url=str(server.make_url("/wake")), token=token, timeout_seconds=2
        )
        with pytest.raises(WorkshopWakeRejectedError) as caught:
            await client.deliver({}, idempotency_key="wake-secret-test")
    finally:
        await server.close()

    assert token not in str(caught.value)


@pytest.mark.asyncio
async def test_wake_classifies_5xx_and_transport_errors_as_retryable():
    server = await _server(503, {})
    try:
        client = WorkshopWakeClient(
            url=str(server.make_url("/wake")), token="wake", timeout_seconds=2
        )
        with pytest.raises(WorkshopWakeRetryableError):
            await client.deliver({}, idempotency_key="wake-1")
    finally:
        await server.close()

    unreachable = WorkshopWakeClient(
        url="http://127.0.0.1:1/wake", token="wake", timeout_seconds=0.1
    )
    with pytest.raises(WorkshopWakeRetryableError):
        await unreachable.deliver({}, idempotency_key="wake-2")
