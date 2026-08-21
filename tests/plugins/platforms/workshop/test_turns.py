from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from gateway.config import Platform, PlatformConfig
from gateway.session import build_session_key
from agent.codex_runtime import make_codex_app_server_event_bridge
from plugins.platforms.workshop.adapter import WorkshopAdapter
from plugins.platforms.workshop.auth import WorkshopAuthenticator
from plugins.platforms.workshop.http import WorkshopHTTPController
from plugins.platforms.workshop.storage import WorkshopLedger
from plugins.platforms.workshop.turns import WorkshopTurnCoordinator


KEY = "a" * 64


def _body(*, client_turn_id="client-1", workspace_id="workspace-1", chat_id="chat-1"):
    return {
        "protocol_version": 1,
        "client_turn_id": client_turn_id,
        "workspace_id": workspace_id,
        "chat_id": chat_id,
        "input": {"type": "user", "text": "Hello"},
        "tools": [],
    }


def _headers():
    return {"Authorization": f"Bearer {KEY}"}


class _SessionStore:
    def __init__(self, session_ids=None):
        self.session_ids = list(session_ids or ["session-1"])
        self.calls = []

    async def get_or_create_session(self, source):
        self.calls.append(source)
        session_id = (
            self.session_ids.pop(0)
            if len(self.session_ids) > 1
            else self.session_ids[0]
        )
        return SimpleNamespace(
            session_key=build_session_key(source),
            session_id=session_id,
        )


def _adapter(tmp_path, handler, *, session_ids=None, max_active=4):
    config = PlatformConfig(
        enabled=True,
        extra={"wake_url": "https://workshop.example.test/wake"},
    )
    adapter = WorkshopAdapter(config)
    adapter.ledger = WorkshopLedger(tmp_path / "state.db")
    adapter.turns = WorkshopTurnCoordinator(adapter.ledger)
    adapter._behavior = {
        "max_active_turns": max_active,
        "max_event_backlog_bytes": 8 * 1024 * 1024,
    }
    adapter._running = True
    adapter.set_message_handler(handler)
    store = _SessionStore(session_ids)
    runner = SimpleNamespace(
        adapters={Platform("workshop"): adapter},
        async_session_store=store,
        _session_key_for_source=lambda source: build_session_key(source),
    )
    adapter.gateway_runner = runner
    return adapter, runner, store


def _app(adapter):
    controller = WorkshopHTTPController(
        api_adapter=object(),
        authenticator=WorkshopAuthenticator(KEY),
        ledger=adapter.ledger,
    )
    app = web.Application()
    app["gateway_runner"] = adapter.gateway_runner
    for method, path, handler in controller.routes():
        app.router.add_route(method, path, handler)
    return app


def _sse_records(body: str):
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_turn_stream_uses_execution_epoch_and_persists_exact_order(tmp_path):
    captured = {}

    async def handler(event):
        captured["source"] = event.source
        captured["session_id"] = event.metadata["gateway_session_id"]
        event.metadata["_gateway_event_sink"]("text.delta", {"delta": "Hi"})
        return {
            "final_response": "Hi there",
            "input_tokens": 7,
            "output_tokens": 2,
            "last_prompt_tokens": 9,
            "model": "test-model",
        }

    adapter, _runner, store = _adapter(
        tmp_path, handler, session_ids=["admission-epoch", "execution-epoch"]
    )
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/workshop/v1/turns", json=_body(), headers=_headers()
        )
        records = _sse_records(await response.text())

    assert response.status == 200
    assert [item["event"] for item in records] == [
        "turn.started",
        "message.start",
        "text.delta",
        "text.delta",
        "usage",
        "turn.end",
    ]
    assert [item["seq"] for item in records] == list(range(1, 7))
    assert {item["session_id"] for item in records} == {"execution-epoch"}
    assert (
        records[0]["catalog_version"]
        == adapter.ledger.get_turn(records[0]["turn_id"]).catalog_version
    )
    assert records[2]["delta"] + records[3]["delta"] == "Hi there"
    assert captured["session_id"] == "execution-epoch"
    assert captured["source"].chat_id == "workspace-1"
    assert captured["source"].thread_id == "chat-1"
    assert build_session_key(captured["source"]) == (
        "agent:main:workshop:thread:workspace-1:chat-1"
    )
    assert len(store.calls) == 2


@pytest.mark.asyncio
async def test_sse_disconnect_does_not_cancel_turn(tmp_path):
    continue_turn = asyncio.Event()
    entered = asyncio.Event()

    async def handler(event):
        event.metadata["_gateway_event_sink"]("text.delta", {"delta": "first"})
        entered.set()
        await continue_turn.wait()
        event.metadata["_gateway_event_sink"]("text.delta", {"delta": "second"})
        return {"final_response": "firstsecond"}

    adapter, _runner, _store = _adapter(tmp_path, handler)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/workshop/v1/turns", json=_body(), headers=_headers()
        )
        await entered.wait()
        first_data = None
        while first_data is None:
            line = (await response.content.readline()).decode()
            if line.startswith("data: "):
                item = json.loads(line.removeprefix("data: "))
                if item["event"] == "text.delta":
                    first_data = item
        turn_id = first_data["turn_id"]
        task = adapter.turns.task_for(turn_id)
        assert task is not None
        response.close()
        continue_turn.set()
        await asyncio.wait_for(asyncio.shield(task), timeout=2)

    turn = adapter.ledger.get_turn(turn_id)
    assert turn is not None and turn.state == "completed"
    assert [event.event for event in adapter.ledger.list_events(turn_id)] == [
        "turn.started",
        "message.start",
        "text.delta",
        "text.delta",
        "usage",
        "turn.end",
    ]


@pytest.mark.asyncio
async def test_after_seq_replays_only_later_semantic_events(tmp_path):
    async def handler(event):
        event.metadata["_gateway_event_sink"]("text.delta", {"delta": "answer"})
        return {"final_response": "answer"}

    adapter, _runner, _store = _adapter(tmp_path, handler)
    async with TestClient(TestServer(_app(adapter))) as client:
        initial = await client.post(
            "/api/workshop/v1/turns", json=_body(), headers=_headers()
        )
        records = _sse_records(await initial.text())
        replay = await client.get(
            f"/api/workshop/v1/turns/{records[0]['turn_id']}/events?after_seq=3",
            headers=_headers(),
        )
        replay_records = _sse_records(await replay.text())

    assert [item["event"] for item in replay_records] == ["usage", "turn.end"]
    assert [item["seq"] for item in replay_records] == [4, 5]


@pytest.mark.asyncio
async def test_runtime_raw_events_stream_live_but_only_semantic_events_persist(tmp_path):
    async def handler(event):
        bridge = make_codex_app_server_event_bridge(
            SimpleNamespace(
                _external_event_sink=event.metadata["_gateway_event_sink"],
                _fire_reasoning_delta=None,
                tool_progress_callback=None,
                tool_start_callback=None,
            )
        )
        bridge({
            "method": "item/reasoning/delta",
            "params": {"delta": "private thought"},
        })
        bridge({
            "method": "item/started",
            "params": {
                "item": {
                    "type": "mcpToolCall",
                    "id": "toolu_exact_123",
                    "providerCallId": "toolu_exact_123",
                    "server": "agent-runtime",
                    "tool": "workshop_write",
                    "arguments": {},
                }
            },
        })
        bridge({
            "method": "item/toolCall/argumentsDelta",
            "params": {
                "callId": "toolu_exact_123",
                "name": "workshop_write",
                "delta": '{"path":',
            },
        })
        bridge({
            "method": "item/toolCall/argumentsCompleted",
            "params": {
                "callId": "toolu_exact_123",
                "name": "workshop_write",
                "arguments": {"path": "README.md"},
            },
        })
        return {"final_response": "done"}

    adapter, _runner, _store = _adapter(tmp_path, handler)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/api/workshop/v1/turns", json=_body(), headers=_headers()
        )
        records = _sse_records(await response.text())

    turn_id = records[0]["turn_id"]
    assert [record["event"] for record in records] == [
        "turn.started",
        "message.start",
        "thinking.delta",
        "tool_call.start",
        "tool_call.arguments.delta",
        "tool_call.end",
        "text.delta",
        "usage",
        "turn.end",
    ]
    assert records[3]["call_id"] == "toolu_exact_123"
    assert records[5]["arguments"] == {"path": "README.md"}
    assert [item.event for item in adapter.ledger.list_events(turn_id)] == [
        "turn.started",
        "message.start",
        "tool_call.start",
        "tool_call.end",
        "text.delta",
        "usage",
        "turn.end",
    ]


@pytest.mark.asyncio
async def test_same_chat_turns_serialize_while_different_chats_overlap(tmp_path):
    active_by_lane = {}
    max_by_lane = {}
    total_active = 0
    max_total = 0

    async def handler(event):
        nonlocal total_active, max_total
        lane = (event.source.chat_id, event.source.thread_id)
        active_by_lane[lane] = active_by_lane.get(lane, 0) + 1
        max_by_lane[lane] = max(max_by_lane.get(lane, 0), active_by_lane[lane])
        total_active += 1
        max_total = max(max_total, total_active)
        await asyncio.sleep(0.05)
        total_active -= 1
        active_by_lane[lane] -= 1
        return {"final_response": f"done-{event.message_id}"}

    adapter, _runner, _store = _adapter(tmp_path, handler)
    async with TestClient(TestServer(_app(adapter))) as client:
        requests = [
            client.post(
                "/api/workshop/v1/turns",
                json=_body(client_turn_id="a1", chat_id="chat-a"),
                headers=_headers(),
            ),
            client.post(
                "/api/workshop/v1/turns",
                json=_body(client_turn_id="a2", chat_id="chat-a"),
                headers=_headers(),
            ),
            client.post(
                "/api/workshop/v1/turns",
                json=_body(client_turn_id="b1", chat_id="chat-b"),
                headers=_headers(),
            ),
        ]
        responses = await asyncio.gather(*requests)
        await asyncio.gather(*(response.read() for response in responses))

    assert max(max_by_lane.values()) == 1
    assert max_total >= 2


@pytest.mark.asyncio
async def test_active_turn_limit_is_atomic_and_retryable(tmp_path):
    release = asyncio.Event()

    async def handler(_event):
        await release.wait()
        return {"final_response": "done"}

    adapter, _runner, _store = _adapter(tmp_path, handler, max_active=1)
    async with TestClient(TestServer(_app(adapter))) as client:
        first = await client.post(
            "/api/workshop/v1/turns",
            json=_body(client_turn_id="first", chat_id="one"),
            headers=_headers(),
        )
        rejected = await client.post(
            "/api/workshop/v1/turns",
            json=_body(client_turn_id="second", chat_id="two"),
            headers=_headers(),
        )
        rejected_body = await rejected.json()
        assert rejected.status == 429
        assert rejected.headers["Retry-After"] == "1"
        assert rejected_body["error"]["code"] == "workshop_capacity_exceeded"
        release.set()
        await first.read()


def test_external_text_sink_composes_with_native_gateway_stream():
    from gateway.run import _compose_external_text_event_sink

    native = []
    external = []
    callback = _compose_external_text_event_sink(
        native.append,
        lambda event, payload: external.append((event, payload)),
    )

    callback("hello")

    assert native == ["hello"]
    assert external == [("text.delta", {"delta": "hello"})]
