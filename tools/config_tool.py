"""Agent-facing validated Hermes configuration tool."""

from __future__ import annotations

import json
from typing import Any, Optional

from hermes_cli.agent_config import (
    AgentConfigError,
    approval_required,
    apply_change,
    apply_rollback,
    broker_enabled,
    history,
    inspect_config,
    prepare_change,
    prepare_rollback,
)
from tools.registry import registry, tool_error


def check_config_requirements() -> bool:
    try:
        return broker_enabled()
    except Exception:
        return False


def _approval(prepared: dict) -> dict:
    import hashlib

    from tools.approval import request_tool_approval

    operation = prepared["operation"]
    if operation == "rollback":
        target = f"rollback revision {prepared['revision']}"
    else:
        target = f"{operation} {prepared['path']}"
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "operation": operation,
                "path": prepared.get("path"),
                "value": prepared.get("value"),
                "revision": prepared.get("revision"),
                "expected_hash": prepared.get("expected_hash"),
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return request_tool_approval(
        "config",
        (
            f"Hermes is requesting an agent-owned configuration change: {target}. "
            f"Reason: {prepared['reason']}. This does not permit changes to "
            "Kumo-managed policy or secrets."
        ),
        rule_key=f"config:{fingerprint}",
        allow_permanent=False,
        allow_yolo=False,
    )


def config_tool(
    *,
    action: str,
    path: Optional[str] = None,
    value: Any = None,
    reason: Optional[str] = None,
    revision: Optional[str] = None,
    limit: int = 20,
    actor: str = "",
) -> str:
    try:
        action = str(action or "").strip().lower()
        if action == "inspect":
            return json.dumps(inspect_config(path), ensure_ascii=False)
        if action == "history":
            return json.dumps(history(limit), ensure_ascii=False)
        if action in {"set", "unset"}:
            prepared = prepare_change(
                operation=action,
                path=path or "",
                value=value,
                reason=reason or "",
            )
            if approval_required():
                approval = _approval(prepared)
                if not approval.get("approved"):
                    return json.dumps(
                        {
                            "success": False,
                            "status": approval.get("status", "blocked"),
                            "error": approval.get("message")
                            or "Configuration change was not approved.",
                        },
                        ensure_ascii=False,
                    )
            return json.dumps(apply_change(prepared, actor=actor), ensure_ascii=False)
        if action == "rollback":
            prepared = prepare_rollback(revision or "", reason=reason or "")
            if approval_required():
                approval = _approval(prepared)
                if not approval.get("approved"):
                    return json.dumps(
                        {
                            "success": False,
                            "status": approval.get("status", "blocked"),
                            "error": approval.get("message")
                            or "Configuration rollback was not approved.",
                        },
                        ensure_ascii=False,
                    )
            return json.dumps(apply_rollback(prepared, actor=actor), ensure_ascii=False)
        return tool_error(
            "Unknown action. Use inspect, set, unset, history, or rollback.",
            success=False,
        )
    except AgentConfigError as exc:
        return tool_error(str(exc), success=False)
    except Exception as exc:
        return tool_error(f"Configuration broker failed safely: {exc}", success=False)


CONFIG_SCHEMA = {
    "name": "config",
    "description": (
        "Inspect or update Hermes's agent-owned, non-secret configuration through "
        "a validated gateway broker. Use inspect before changing anything. Set, "
        "unset, and rollback follow the operator's approval policy, apply atomically, "
        "and cannot modify administrator-managed policy, credentials, or the broker policy. "
        "The result reports whether the change applies next turn, next session, "
        "or requires a drained gateway restart. "
        "Never use this tool merely because you prefer a different setting; mutate "
        "configuration only in response to an explicit operator request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "set", "unset", "history", "rollback"],
            },
            "path": {
                "type": "string",
                "description": (
                    "Dotted leaf path. Optional for inspect (omit to list the "
                    "complete agent-visible configuration surface). Required for set/unset."
                ),
            },
            "value": {
                "description": "Typed JSON value for set. Do not put secrets here.",
            },
            "reason": {
                "type": "string",
                "description": "Concise description of the operator's explicit request.",
            },
            "revision": {
                "type": "string",
                "description": "Revision ID returned by history, required for rollback.",
            },
            "limit": {
                "type": "integer",
                "description": "History entries to return (1-100).",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="config",
    toolset="config",
    schema=CONFIG_SCHEMA,
    handler=lambda args, **kw: config_tool(
        action=args.get("action", ""),
        path=args.get("path"),
        value=args.get("value"),
        reason=args.get("reason"),
        revision=args.get("revision"),
        limit=args.get("limit", 20),
        actor=(
            f"session={kw.get('session_id') or ''};"
            f"task={kw.get('task_id') or ''};"
            f"call={kw.get('tool_call_id') or ''}"
        ),
    ),
    check_fn=check_config_requirements,
    emoji="⚙️",
)
