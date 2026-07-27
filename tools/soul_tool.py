"""Agent-facing tool for reading and changing only the active profile's SOUL."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from hermes_cli.soul import (
    SoulConflict,
    SoulError,
    approval_required,
    history_soul,
    read_soul,
    rollback_soul,
    tool_enabled,
    update_soul,
)
from tools.registry import registry, tool_error


_ACTION_FIELDS = {
    "read": frozenset({"action"}),
    "update": frozenset({"action", "content", "expected_version", "reason"}),
    "history": frozenset({"action", "limit"}),
    "rollback": frozenset({"action", "revision", "expected_version", "reason"}),
}


def check_soul_requirements() -> bool:
    try:
        return tool_enabled()
    except Exception:
        return False


def _approval(
    action: str, *, content: str = "", revision: str = "", reason: str
) -> dict:
    from tools.approval import request_tool_approval

    content_hash = (
        hashlib.sha256(content.encode("utf-8")).hexdigest() if content else ""
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "action": action,
                "content_hash": content_hash,
                "revision": revision,
                "reason": reason,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    target = (
        f"restore SOUL revision {revision}"
        if action == "rollback"
        else f"replace SOUL.md with {len(content.encode('utf-8')):,} UTF-8 bytes"
    )
    return request_tool_approval(
        "soul",
        f"Hermes is requesting permission to {target}. Reason: {reason}.",
        rule_key=f"soul:{fingerprint}",
        allow_permanent=False,
        allow_yolo=False,
    )


def soul_tool(args: Mapping[str, Any], **context: Any) -> str:
    """Dispatch one strict action without accepting a profile or path."""
    if not isinstance(args, Mapping):
        return tool_error("soul arguments must be an object.", success=False)
    action = str(args.get("action") or "").strip().lower()
    allowed = _ACTION_FIELDS.get(action)
    if allowed is None:
        return tool_error(
            "Unknown action. Use read, update, history, or rollback.",
            success=False,
        )
    extras = sorted(str(key) for key in set(args) - allowed)
    if extras:
        return tool_error(
            f"Unexpected argument(s) for soul action {action!r}: {', '.join(extras)}. "
            "This tool never accepts a profile, path, or target.",
            success=False,
        )

    # Registry check_fn results are briefly cached. Re-check on every dispatch
    # so a policy disable takes effect immediately and fails closed.
    if not tool_enabled():
        return tool_error(
            "Self-SOUL access is disabled by operator policy.",
            success=False,
        )

    actor = (
        f"session={context.get('session_id') or ''};"
        f"task={context.get('task_id') or ''};"
        f"call={context.get('tool_call_id') or ''}"
    )
    try:
        if action == "read":
            result = read_soul()
        elif action == "history":
            limit = args.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool):
                raise SoulError(
                    "limit must be an integer from 1 to 100 for soul(action='history')."
                )
            result = history_soul(limit=limit)
        elif action == "update":
            content = args.get("content")
            expected_version = args.get("expected_version")
            reason = args.get("reason")
            if not isinstance(content, str):
                raise SoulError("content is required for soul(action='update').")
            if not isinstance(expected_version, str) or not expected_version.strip():
                raise SoulError(
                    "expected_version from soul(action='read') is required for update."
                )
            if not isinstance(reason, str) or not reason.strip():
                raise SoulError("reason is required for soul(action='update').")
            if approval_required():
                approval = _approval("update", content=content, reason=reason)
                if not approval.get("approved"):
                    return json.dumps(
                        {
                            "success": False,
                            "status": approval.get("status", "blocked"),
                            "error": approval.get("message")
                            or "SOUL update was not approved.",
                        },
                        ensure_ascii=False,
                    )
            result = update_soul(
                content=content,
                expected_version=expected_version,
                reason=reason,
                actor=actor,
            )
        else:
            revision = args.get("revision")
            expected_version = args.get("expected_version")
            reason = args.get("reason")
            if not isinstance(revision, str) or not revision.strip():
                raise SoulError("revision is required for soul(action='rollback').")
            if not isinstance(expected_version, str) or not expected_version.strip():
                raise SoulError(
                    "expected_version from soul(action='read') is required for rollback."
                )
            if not isinstance(reason, str) or not reason.strip():
                raise SoulError("reason is required for soul(action='rollback').")
            if approval_required():
                approval = _approval(
                    "rollback",
                    revision=revision,
                    reason=reason,
                )
                if not approval.get("approved"):
                    return json.dumps(
                        {
                            "success": False,
                            "status": approval.get("status", "blocked"),
                            "error": approval.get("message")
                            or "SOUL rollback was not approved.",
                        },
                        ensure_ascii=False,
                    )
            result = rollback_soul(
                revision=revision,
                expected_version=expected_version,
                reason=reason,
                actor=actor,
            )
        return json.dumps(result, ensure_ascii=False)
    except SoulConflict as exc:
        return tool_error(str(exc), success=False, code="version_conflict")
    except SoulError as exc:
        return tool_error(str(exc), success=False)
    except Exception as exc:
        return tool_error(f"SOUL broker failed safely: {exc}", success=False)


SOUL_SCHEMA = {
    "name": "soul",
    "description": (
        "Read or update only this running profile's SOUL.md through a validated, "
        "audited broker. The profile is derived by Hermes; this tool never accepts "
        "a profile or path. Always read first and pass its expected_version when "
        "updating or rolling back. Changes are durable immediately but the current "
        "conversation keeps its cached identity; use a new/reset session to load "
        "the changed SOUL. History is metadata-only."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "update", "history", "rollback"],
            },
            "content": {
                "type": "string",
                "description": "Complete replacement content; update only.",
            },
            "expected_version": {
                "type": "string",
                "description": (
                    "Opaque version returned by read. Required for update and rollback."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Concise reason for the operator-requested update or rollback."
                ),
            },
            "revision": {
                "type": "string",
                "description": "Revision ID returned by history; rollback only.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Metadata history entries to return.",
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="soul",
    toolset="soul",
    schema=SOUL_SCHEMA,
    handler=soul_tool,
    check_fn=check_soul_requirements,
    emoji="🪞",
    # A 65,536-byte Markdown file can approach 393k JSON characters when
    # every character requires escaping. Reads are intentionally complete.
    max_result_size_chars=410_000,
)
