from __future__ import annotations

import json

import tools.composio_tool as module


def test_model_schema_has_no_connection_mutations():
    operations = module.COMPOSIO_SCHEMA["parameters"]["properties"]["operation"]["enum"]
    assert operations == ["list_toolkits", "list_connections", "execute"]
    assert "callback_url" not in module.COMPOSIO_SCHEMA["parameters"]["properties"]


def test_hidden_connection_mutation_cannot_be_called(monkeypatch):
    class Client:
        def __init__(self):
            pass

    monkeypatch.setattr(module, "ComposioClient", Client)
    result = json.loads(module.composio_tool({"operation": "delete_connection", "connection_id": "ca_1"}))
    assert "Unknown Composio operation" in result["error"]
