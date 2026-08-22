from types import SimpleNamespace

import pytest

from agent.codex_runtime import (
    _app_server_tool_schemas,
    _dispatch_app_server_host_tool,
)
from agent.transports.codex_app_server_session import HostToolResult


def _local_tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Local {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _external_tool(name: str) -> dict:
    return {
        "name": name,
        "description": f"Remote {name}",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "origin": "workshop",
    }


def test_external_catalog_merges_after_policy_filtered_local_tools():
    agent = SimpleNamespace(
        tools=[_local_tool("memory"), _local_tool("terminal")],
        valid_tool_names={"memory"},
        _external_tool_schemas=[_external_tool("writeFile")],
    )

    schemas = _app_server_tool_schemas(agent)

    assert [schema["name"] for schema in schemas] == ["memory", "writeFile"]
    assert schemas[1]["inputSchema"]["required"] == ["path"]
    assert "origin" not in schemas[1]


def test_external_name_cannot_shadow_even_a_policy_denied_local_tool():
    agent = SimpleNamespace(
        tools=[_local_tool("terminal")],
        valid_tool_names=set(),
        _external_tool_schemas=[_external_tool("terminal")],
    )

    with pytest.raises(PermissionError, match="Hermes-local"):
        _app_server_tool_schemas(agent)


def test_duplicate_external_names_fail_closed():
    agent = SimpleNamespace(
        tools=[],
        valid_tool_names=set(),
        _external_tool_schemas=[
            _external_tool("writeFile"),
            _external_tool("writeFile"),
        ],
    )

    with pytest.raises(ValueError, match="Duplicate external"):
        _app_server_tool_schemas(agent)


def test_external_dispatch_never_enters_local_tool_handler():
    local_calls = []
    remote_calls = []
    agent = SimpleNamespace(
        valid_tool_names={"memory"},
        _external_tool_names=frozenset({"writeFile"}),
        _external_tool_callback=lambda *args: (
            remote_calls.append(args)
            or SimpleNamespace(content={"ok": True}, is_error=False)
        ),
        _invoke_tool=lambda *args, **kwargs: local_calls.append((args, kwargs)),
    )

    result = _dispatch_app_server_host_tool(
        agent,
        "writeFile",
        {"path": "README.md"},
        "toolu_exact_remote",
    )

    assert result == HostToolResult(content={"ok": True}, is_error=False)
    assert remote_calls == [
        ("writeFile", {"path": "README.md"}, "toolu_exact_remote")
    ]
    assert local_calls == []


def test_local_dispatch_still_requires_exact_policy():
    agent = SimpleNamespace(
        valid_tool_names=set(),
        _external_tool_names=frozenset(),
        _invoke_tool=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
    )

    with pytest.raises(PermissionError, match="not enabled"):
        _dispatch_app_server_host_tool(agent, "terminal", {}, "toolu_denied")
