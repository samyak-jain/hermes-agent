from __future__ import annotations

import json

import tools.composio_tool as module


def test_model_schema_has_no_connection_mutations():
    operations = module.COMPOSIO_SCHEMA["parameters"]["properties"]["operation"]["enum"]
    assert operations == ["list_toolkits", "list_connections", "search", "get_schemas", "execute"]
    assert "callback_url" not in module.COMPOSIO_SCHEMA["parameters"]["properties"]


def test_hidden_connection_mutation_cannot_be_called(monkeypatch):
    class Client:
        def __init__(self):
            pass

    monkeypatch.setattr(module, "ComposioClient", Client)
    result = json.loads(module.composio_tool({"operation": "delete_connection", "connection_id": "ca_1"}))
    assert "Unknown Composio operation" in result["error"]


def test_approval_action_is_denied_before_execution(monkeypatch):
    calls = []

    class Client:
        def __init__(self):
            pass

        def require_app(self, app):
            return "gmail"

        def require_action(self, app, action):
            return "GMAIL_SEND_EMAIL"

        def action_requires_approval(self, app, action):
            return True

        def execute(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(module, "ComposioClient", Client)
    monkeypatch.setattr(module, "_approval_request", lambda *args: {
        "approved": False,
        "message": "denied",
    })
    result = json.loads(module.composio_tool({
        "operation": "execute",
        "app": "gmail",
        "action": "GMAIL_SEND_EMAIL",
        "params": {"to": "person@example.com"},
    }))
    assert result["error"] == "denied"
    assert calls == []


def test_approved_action_executes_and_read_bypasses_approval(monkeypatch):
    executed = []
    approvals = []

    class Client:
        def __init__(self):
            pass

        def require_app(self, app):
            return "gmail"

        def require_action(self, app, action):
            return str(action).upper()

        def action_requires_approval(self, app, action):
            return action == "GMAIL_SEND_EMAIL"

        def execute(self, app, action, params, connected_account_id=None):
            executed.append((app, action, params, connected_account_id))
            return {"ok": True}

    monkeypatch.setattr(module, "ComposioClient", Client)
    monkeypatch.setattr(module, "_approval_request", lambda *args: approvals.append(args) or {"approved": True})

    sent = json.loads(module.composio_tool({
        "operation": "execute", "app": "gmail", "action": "gmail_send_email",
        "params": {"to": "person@example.com"}, "connection_id": "ca_1",
    }))
    read = json.loads(module.composio_tool({
        "operation": "execute", "app": "gmail", "action": "gmail_get_profile", "params": {},
    }))
    assert sent["success"] is True and read["success"] is True
    assert len(approvals) == 1
    assert [item[1] for item in executed] == ["GMAIL_SEND_EMAIL", "GMAIL_GET_PROFILE"]


def test_approval_fingerprint_is_bound_to_exact_payload(monkeypatch):
    captured = []
    monkeypatch.setattr("tools.approval.request_tool_approval", lambda *args, **kwargs: captured.append(kwargs) or {"approved": True})
    module._approval_request("gmail", "GMAIL_SEND_EMAIL", {"to": "a@example.com"}, "ca_1")
    module._approval_request("gmail", "GMAIL_SEND_EMAIL", {"to": "b@example.com"}, "ca_1")
    assert captured[0]["rule_key"] != captured[1]["rule_key"]
    assert captured[0]["allow_permanent"] is False
    assert captured[0]["allow_yolo"] is False


def test_consumer_mcp_write_relies_on_provider_elicitation_without_duplicate_approval(monkeypatch):
    executed = []

    class Client:
        consumer_mcp = True

        def __init__(self):
            pass

        def require_app(self, app):
            return "gmail"

        def require_action(self, app, action):
            return "GMAIL_SEND_EMAIL"

        def action_requires_approval(self, app, action):
            raise AssertionError("local approval must not run for consumer MCP")

        def execute(self, *args, **kwargs):
            executed.append((args, kwargs))
            return {"ok": True}

    monkeypatch.setattr(module, "ComposioClient", Client)
    monkeypatch.setattr(
        module,
        "_approval_request",
        lambda *args: (_ for _ in ()).throw(AssertionError("duplicate approval")),
    )
    result = json.loads(module.composio_tool({
        "operation": "execute",
        "app": "gmail",
        "action": "GMAIL_SEND_EMAIL",
        "params": {"to": "person@example.com"},
        "account": "personal",
    }, task_id="turn-1"))
    assert result["success"] is True
    assert executed[0][1]["connected_account_id"] == "personal"
    assert executed[0][1]["context"] == {"task_id": "turn-1"}


def test_account_and_connection_id_must_not_disagree(monkeypatch):
    class Client:
        consumer_mcp = True

        def __init__(self):
            pass

        def require_app(self, app):
            return "gmail"

        def require_action(self, app, action):
            return "GMAIL_GET_PROFILE"

    monkeypatch.setattr(module, "ComposioClient", Client)
    result = json.loads(module.composio_tool({
        "operation": "execute",
        "app": "gmail",
        "action": "GMAIL_GET_PROFILE",
        "params": {},
        "account": "personal",
        "connection_id": "loopedin",
    }))
    assert "disagree" in result["error"]
