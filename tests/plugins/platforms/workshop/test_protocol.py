from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from plugins.platforms.workshop.protocol import (
    LIVE_ONLY_EVENT_TYPES,
    PROTOCOL_VERSION,
    WorkshopControlRequest,
    WorkshopDeltaRequest,
    WorkshopEvent,
    WorkshopProtocolError,
    WorkshopTurnRequest,
    parse_tool_catalog,
)


def _tool(name: str = "writeFile", schema=None):
    return {
        "name": name,
        "description": "Write a workshop file",
        "parameters": schema
        or {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }


def _turn(tools=None):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "client_turn_id": "client-1",
        "workspace_id": "workspace-1",
        "chat_id": "chat-1",
        "input": {"type": "user", "text": "Build it"},
        "tools": [_tool()] if tools is None else tools,
    }


def test_tool_catalog_digest_is_canonical_across_input_order():
    first, first_digest = parse_tool_catalog([_tool("writeFile"), _tool("readFile")])
    second, second_digest = parse_tool_catalog([_tool("readFile"), _tool("writeFile")])

    assert [tool.name for tool in first] == ["readFile", "writeFile"]
    assert first == second
    assert first_digest == second_digest


def test_representative_cloudflare_catalog_is_accepted_without_schema_weakening():
    fixture_dir = Path(__file__).parents[4] / "fixtures" / "workshop-tool-schemas"
    manifest = json.loads((fixture_dir / "index.json").read_text())
    raw_tools = [
        json.loads((fixture_dir / item["file"]).read_text())
        for item in manifest["tools"]
    ]

    tools, digest = parse_tool_catalog(list(reversed(raw_tools)))

    assert [tool.name for tool in tools] == sorted(item["name"] for item in raw_tools)
    assert digest == manifest["digest"]
    canonical = json.dumps(
        sorted(raw_tools, key=lambda item: item["name"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == digest


@pytest.mark.parametrize("name", ["spawn_agent", "memory", "mcp__workshop__writeFile"])
def test_remote_tool_cannot_shadow_local_or_reserved_names(name):
    with pytest.raises(WorkshopProtocolError) as exc:
        parse_tool_catalog([_tool(name)])
    assert exc.value.code == "reserved_tool_name"


@pytest.mark.parametrize("keyword", ["$ref", "allOf", "pattern", "minimum", "format"])
def test_unsupported_json_schema_keywords_are_rejected(keyword):
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string", keyword: "unsupported"}},
    }
    with pytest.raises(WorkshopProtocolError) as exc:
        parse_tool_catalog([_tool(schema=schema)])
    assert exc.value.code == "unsupported_schema_keyword"
    assert keyword in str(exc.value)


def test_required_must_reference_declared_properties():
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["missing"],
    }
    with pytest.raises(WorkshopProtocolError) as exc:
        parse_tool_catalog([_tool(schema=schema)])
    assert exc.value.code == "invalid_tool_schema"


def test_turn_request_returns_catalog_version_and_stable_request_digest():
    request = WorkshopTurnRequest.from_dict(_turn())
    repeated = WorkshopTurnRequest.from_dict(_turn())

    assert len(request.catalog_version) == 64
    assert request.request_digest == repeated.request_digest
    assert request.tools[0].to_bridge_schema()["origin"] == "workshop"


def test_turn_request_rejects_colons_in_routing_ids():
    raw = _turn()
    raw["workspace_id"] = "tenant:escape"
    with pytest.raises(WorkshopProtocolError) as exc:
        WorkshopTurnRequest.from_dict(raw)
    assert exc.value.code == "invalid_identifier"


def test_turn_request_rejects_unsupported_caller_metadata():
    raw = _turn()
    raw["metadata"] = {"title": "unused"}
    with pytest.raises(WorkshopProtocolError) as exc:
        WorkshopTurnRequest.from_dict(raw)
    assert exc.value.code == "unknown_field"


def test_control_defaults_are_signal_specific():
    end_turn = WorkshopControlRequest.from_dict(
        {"protocol_version": 1, "signal": "end_turn", "reason": "approval"}
    )
    abort = WorkshopControlRequest.from_dict(
        {"protocol_version": 1, "signal": "abort", "reason": "user stopped"}
    )

    assert end_turn.mode == "after_current_call"
    assert abort.mode == "immediate"


def test_abort_cannot_request_after_current_call():
    with pytest.raises(WorkshopProtocolError) as exc:
        WorkshopControlRequest.from_dict(
            {
                "protocol_version": 1,
                "signal": "abort",
                "mode": "after_current_call",
                "reason": "stop",
            }
        )
    assert exc.value.code == "invalid_control"


def test_delta_requires_body_and_route_safe_ids():
    delta = WorkshopDeltaRequest.from_dict(
        {
            "protocol_version": 1,
            "delta_id": "delta-1",
            "workspace_id": "workspace-1",
            "chat_id": "chat-1",
            "payload": {
                "type": "file_changed",
                "version": 1,
                "timestamp": "2026-08-22T12:00:00Z",
                "data": {"path": "README.md"},
            },
        }
    )
    assert delta.payload["type"] == "file_changed"
    assert delta.canonical_payload == (
        '{"data":{"path":"README.md"},"timestamp":"2026-08-22T12:00:00Z",'
        '"type":"file_changed","version":1}'
    )


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            {
                "type": "file_changed",
                "version": 2,
                "timestamp": "2026-08-22T12:00:00Z",
                "data": {},
            },
            "unsupported_delta_version",
        ),
        (
            {
                "type": "file_changed",
                "version": 1,
                "timestamp": "2026-08-22T12:00:00",
                "data": {},
            },
            "invalid_delta_timestamp",
        ),
        (
            {
                "type": "file_changed",
                "version": 1,
                "timestamp": "2026-08-22T12:00:00Z",
                "data": {},
                "control": {"signal": "abort"},
            },
            "unknown_field",
        ),
    ],
)
def test_delta_envelope_is_strict(payload, code):
    with pytest.raises(WorkshopProtocolError) as exc:
        WorkshopDeltaRequest.from_dict(
            {
                "protocol_version": 1,
                "delta_id": "delta-1",
                "workspace_id": "workspace-1",
                "chat_id": "chat-1",
                "payload": payload,
            }
        )
    assert exc.value.code == code


def test_delta_data_has_bounded_collection_size():
    with pytest.raises(WorkshopProtocolError) as exc:
        WorkshopDeltaRequest.from_dict(
            {
                "protocol_version": 1,
                "delta_id": "delta-1",
                "workspace_id": "workspace-1",
                "chat_id": "chat-1",
                "payload": {
                    "type": "many_changes",
                    "version": 1,
                    "timestamp": "2026-08-22T12:00:00+00:00",
                    "data": list(range(257)),
                },
            }
        )
    assert exc.value.code == "delta_too_complex"


def test_live_only_events_are_never_persistent():
    for event_type in LIVE_ONLY_EVENT_TYPES:
        event = WorkshopEvent.create(
            turn_id="turn-1",
            session_id="session-1",
            seq=1,
            event=event_type,
            payload={"delta": "fragment"},
        )
        assert event.persistent is False

    text = WorkshopEvent.create(
        turn_id="turn-1",
        session_id="session-1",
        seq=1,
        event="text.delta",
        payload={"delta": "hello"},
    )
    assert text.persistent is True
