#!/usr/bin/env python3
"""Minimal background subagent lifecycle tool.

This module deliberately owns no child-agent machinery.  It adapts the
existing delegation child builder/runner to the shared async completion rail,
then returns a compact handle while the child continues in the background.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


SPAWN_AGENT_SCHEMA = {
    "name": "spawn_agent",
    "description": (
        "Spawn or cancel a background subagent. To spawn, provide a prompt; "
        "the tool returns an id immediately and the result arrives later as a "
        "new message — do not wait or poll. The subagent has no memory of this "
        "conversation, so the prompt must be self-contained (file paths, "
        "constraints, expected output). For parallel work, call this tool "
        "multiple times in one turn. To cancel one previously spawned by this "
        "conversation, provide its returned id as cancel_id and omit prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Self-contained task description. Omit when cancelling.",
            },
            "label": {
                "type": "string",
                "description": "Optional 2–4 word label for status displays.",
            },
            "cancel_id": {
                "type": "string",
                "description": (
                    "ID returned by an earlier spawn_agent call in this "
                    "conversation. Cancels that subagent; omit prompt."
                ),
            },
        },
    },
}


def _display_label(prompt: str, label: Optional[str]) -> str:
    candidate = str(label).strip() if label is not None else ""
    if not candidate:
        candidate = next(
            (line.strip() for line in prompt.splitlines() if line.strip()),
            "background task",
        )
    candidate = " ".join(candidate.split())
    return candidate if len(candidate) <= 80 else candidate[:77] + "..."


def _detach_child(parent_agent, child) -> None:
    """Move interrupt ownership from the parent turn to async_delegation."""
    active = getattr(parent_agent, "_active_children", None)
    if active is None:
        return
    lock = getattr(parent_agent, "_active_children_lock", None)
    try:
        if lock:
            with lock:
                active.remove(child)
        else:
            active.remove(child)
    except ValueError:
        pass


def _delivery_route(parent_agent) -> tuple[str, str, Optional[str]]:
    from tools.approval import get_current_session_key

    session_key = get_current_session_key(default="")
    origin_ui_session_id = ""
    try:
        from gateway.session_context import get_session_env

        source = get_session_env("HERMES_SESSION_SOURCE", "")
        origin_ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "")
        if source == "tui":
            agent_session_id = str(getattr(parent_agent, "session_id", "") or "")
            if agent_session_id:
                session_key = agent_session_id
    except Exception:
        origin_ui_session_id = ""
    return session_key, origin_ui_session_id, getattr(parent_agent, "session_id", None)


def spawn_agent(
    prompt: Optional[str] = None,
    label: Optional[str] = None,
    cancel_id: Optional[str] = None,
    parent_agent=None,
) -> str:
    """Build one leaf child or cancel one previously dispatched by this parent."""
    from tools.registry import tool_error

    if parent_agent is None:
        return tool_error("spawn_agent requires a parent agent context.")

    if cancel_id is not None:
        if not isinstance(cancel_id, str) or not cancel_id.strip():
            return tool_error("spawn_agent cancel_id must be a non-empty string.")
        if isinstance(prompt, str) and prompt.strip():
            return tool_error(
                "spawn_agent accepts either prompt or cancel_id, not both."
            )

        from tools.async_delegation import interrupt_delegation

        target_id = cancel_id.strip()
        session_key, origin_ui_session_id, parent_session_id = _delivery_route(
            parent_agent
        )
        interrupted = interrupt_delegation(
            target_id,
            session_key=session_key,
            origin_ui_session_id=origin_ui_session_id,
            parent_session_id=parent_session_id,
            completion_type="spawn_result",
        )
        if not interrupted:
            return tool_error(
                f"No running spawn_agent subagent found with id '{target_id}' "
                "in this conversation."
            )
        return json.dumps({"id": target_id, "status": "cancelling"}, ensure_ascii=False)

    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error(
            "spawn_agent requires either a non-empty prompt or cancel_id."
        )

    try:
        from gateway.session_context import async_delivery_supported

        if not async_delivery_supported():
            return tool_error(
                "spawn_agent is unavailable on this stateless endpoint because "
                "there is no channel for the background result to re-enter."
            )
    except Exception:
        pass

    from tools.async_delegation import active_count, dispatch_async_delegation
    from tools.delegate_tool import (
        DEFAULT_MAX_ITERATIONS,
        _build_child_agent,
        _get_max_concurrent_children,
        _load_config,
        _resolve_delegation_credentials,
        _run_single_child,
    )

    max_children = _get_max_concurrent_children()
    if active_count() >= max_children:
        return tool_error(
            f"Background subagent capacity reached ({max_children} running). "
            "Continue working; a result will arrive when one finishes."
        )

    cfg = _load_config()
    try:
        creds = _resolve_delegation_credentials(cfg, parent_agent)
    except ValueError as exc:
        return tool_error(str(exc))

    clean_prompt = prompt.strip()
    display_label = _display_label(clean_prompt, label)
    spawn_id = f"sa_{uuid.uuid4().hex[:6]}"

    import model_tools

    parent_tool_names = list(model_tools._last_resolved_tool_names)
    try:
        child = _build_child_agent(
            task_index=0,
            goal=clean_prompt,
            context=None,
            toolsets=None,
            model=creds["model"],
            max_iterations=cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS),
            task_count=1,
            parent_agent=parent_agent,
            override_provider=creds["provider"],
            override_base_url=creds["base_url"],
            override_api_key=creds["api_key"],
            override_api_mode=creds["api_mode"],
            override_request_overrides=creds.get("request_overrides"),
            override_max_tokens=creds.get("max_output_tokens"),
            override_acp_command=creds.get("command"),
            override_acp_args=creds.get("args"),
            role="leaf",
            relay_progress=False,
            emit_lifecycle_hooks=False,
        )
        child._delegate_saved_tool_names = parent_tool_names
    finally:
        model_tools._last_resolved_tool_names = parent_tool_names

    # spawn_agent intentionally does not populate the delegate_task /agents
    # tree.  The async registry still owns and interrupts the live child.
    child._subagent_id = None
    _detach_child(parent_agent, child)

    def runner() -> Dict[str, Any]:
        from tools.delegate_tool import _apply_summary_budget

        result = _run_single_child(0, clean_prompt, child, parent_agent)
        _apply_summary_budget([result], parent_agent)
        result.pop("_child_role", None)
        child_cost = result.pop("_child_cost_usd", 0.0)
        try:
            parent_cost = float(
                getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0
            )
            parent_agent.session_estimated_cost_usd = parent_cost + float(child_cost or 0.0)
        except (TypeError, ValueError):
            pass
        return result

    def interrupt() -> None:
        if hasattr(child, "interrupt"):
            child.interrupt("Background subagent cancelled")
        else:
            child._interrupt_requested = True

    session_key, origin_ui_session_id, parent_session_id = _delivery_route(parent_agent)
    dispatch = dispatch_async_delegation(
        goal=clean_prompt,
        context=None,
        toolsets=None,
        role="leaf",
        model=creds["model"],
        session_key=session_key,
        origin_ui_session_id=origin_ui_session_id,
        parent_session_id=parent_session_id,
        runner=runner,
        interrupt_fn=interrupt,
        max_async_children=max_children,
        delegation_id=spawn_id,
        completion_type="spawn_result",
        label=display_label,
    )
    if dispatch.get("status") != "dispatched":
        try:
            child.close()
        except Exception:
            logger.debug("spawn_agent child cleanup failed", exc_info=True)
        return tool_error(dispatch.get("error") or "Failed to spawn background subagent.")

    from tools.delegate_tool import _emit_parent_console

    _emit_parent_console(parent_agent, f"🔀 {spawn_id} spawned: {display_label}")
    return json.dumps({"id": spawn_id, "status": "running"}, ensure_ascii=False)


from tools.delegate_tool import check_delegate_requirements
from tools.registry import registry

registry.register(
    name="spawn_agent",
    toolset="spawn",
    schema=SPAWN_AGENT_SCHEMA,
    handler=lambda args, **kw: spawn_agent(
        prompt=args.get("prompt"),
        label=args.get("label"),
        cancel_id=args.get("cancel_id"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
)
