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
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_RESULT_PAGE_CHARS = 12_000
_MAX_RESULT_PAGE_CHARS = 20_000


SPAWN_AGENT_SCHEMA = {
    "name": "spawn_agent",
    "description": (
        "Spawn, cancel, or retrieve the completed result of a background "
        "subagent. To spawn, provide a prompt; "
        "the tool returns an id and a live_transcripts path immediately, and "
        "the result arrives later as a new message — do not wait or poll. You "
        "can read or tail -f the transcript path while the child works. The "
        "subagent has no memory of this "
        "conversation, so the prompt must be self-contained (file paths, "
        "constraints, expected output). For parallel work, call this tool "
        "multiple times in one turn. To cancel one previously spawned by this "
        "conversation, provide its returned id as cancel_id and omit prompt. "
        "When a delivered result says it was truncated, provide its id as "
        "result_id and page through the full report using offset/limit; do not "
        "spawn another subagent just to read the omitted text."
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
            "result_id": {
                "type": "string",
                "description": (
                    "ID of a completed spawn_agent result owned by this "
                    "conversation. Retrieves a bounded page of its full report; "
                    "omit prompt and cancel_id."
                ),
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Zero-based character offset for result_id retrieval "
                    "(default 0). Use next_offset from the previous page."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULT_PAGE_CHARS,
                "description": (
                    "Maximum result characters to return (default 12000, "
                    "maximum 20000)."
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


def _owned_result_text(
    result_id: str,
    *,
    parent_agent,
    offset: int,
    limit: int,
) -> str:
    """Return one redacted page from an owned, durable spawn result."""
    from hermes_constants import get_hermes_dir
    from tools.async_delegation import get_owned_durable_delegation
    from tools.registry import tool_error

    session_key, origin_ui_session_id, parent_session_id = _delivery_route(
        parent_agent
    )
    record = get_owned_durable_delegation(
        result_id,
        session_key=session_key,
        origin_ui_session_id=origin_ui_session_id,
        parent_session_id=parent_session_id,
        completion_type="spawn_result",
    )
    if record is None:
        return tool_error(
            f"No spawn_agent result found with id '{result_id}' in this conversation."
        )

    state = str(record.get("state") or "unknown")
    result = record.get("result")
    if not isinstance(result, dict):
        return json.dumps(
            {
                "id": result_id,
                "status": state,
                "content": "",
                "has_more": False,
                "message": (
                    "The subagent is still running; its result will arrive "
                    "automatically. Do not poll."
                    if state in {"running", "finalizing"}
                    else "No result text was recorded."
                ),
            },
            ensure_ascii=False,
        )

    summary = str(result.get("summary") or "")
    full_path = result.get("summary_full_path")
    full_result_available = False
    unavailable_reason = ""
    text = summary

    if full_path:
        try:
            path = Path(str(full_path))
            if path.is_symlink():
                raise ValueError("result artifact is a symlink")
            resolved = path.resolve(strict=True)
            cache_root = get_hermes_dir(
                "cache/delegation",
                "delegation_cache",
            ).resolve()
            relative = resolved.relative_to(cache_root)
            parts = relative.parts
            is_legacy = (
                len(parts) == 1
                and resolved.name.startswith("subagent-summary-")
                and resolved.suffix == ".txt"
            )
            is_owned_live_artifact = (
                len(parts) == 3
                and parts[0] == "live"
                and parts[1] == result_id
                and resolved.name.startswith("subagent-summary-")
                and resolved.suffix == ".txt"
            )
            if not (is_legacy or is_owned_live_artifact) or not resolved.is_file():
                raise ValueError("result artifact is outside the owned cache path")
            text = resolved.read_text(encoding="utf-8")
            full_result_available = True
        except (OSError, UnicodeError, ValueError) as exc:
            unavailable_reason = (
                "The full result artifact is unavailable or expired; returning "
                "the stored truncated summary."
            )
            logger.warning(
                "Could not read owned spawn result %s from %s: %s",
                result_id,
                full_path,
                exc,
            )

    total_chars = len(text)
    page = text[offset:offset + limit]
    next_offset = offset + len(page)
    has_more = next_offset < total_chars
    try:
        from agent.redact import redact_sensitive_text

        page = redact_sensitive_text(page, force=True) or ""
    except Exception:
        logger.exception("Could not redact spawn result page for %s", result_id)
        return tool_error("Result retrieval failed closed because redaction was unavailable.")

    payload: Dict[str, Any] = {
        "id": result_id,
        "status": state,
        "offset": offset,
        "returned_chars": len(page),
        "total_chars": total_chars,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "full_result_available": full_result_available,
        "content": page,
    }
    if unavailable_reason:
        payload["message"] = unavailable_reason
    if result.get("error"):
        payload["error"] = str(result["error"])
    return json.dumps(payload, ensure_ascii=False)


def spawn_agent(
    prompt: Optional[str] = None,
    label: Optional[str] = None,
    cancel_id: Optional[str] = None,
    result_id: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    parent_agent=None,
) -> str:
    """Spawn, cancel, or retrieve one background child owned by this parent."""
    from tools.registry import tool_error

    if parent_agent is None:
        return tool_error("spawn_agent requires a parent agent context.")

    supplied_actions = sum(
        (
            bool(isinstance(prompt, str) and prompt.strip()),
            cancel_id is not None,
            result_id is not None,
        )
    )
    if supplied_actions != 1:
        return tool_error(
            "spawn_agent accepts exactly one of prompt, cancel_id, or result_id."
        )

    if result_id is not None:
        if not isinstance(result_id, str) or not result_id.strip():
            return tool_error("spawn_agent result_id must be a non-empty string.")
        if offset is None:
            offset = 0
        if limit is None:
            limit = _DEFAULT_RESULT_PAGE_CHARS
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return tool_error("spawn_agent result offset must be a non-negative integer.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_RESULT_PAGE_CHARS
        ):
            return tool_error(
                "spawn_agent result limit must be an integer between 1 and "
                f"{_MAX_RESULT_PAGE_CHARS}."
            )
        return _owned_result_text(
            result_id.strip(),
            parent_agent=parent_agent,
            offset=offset,
            limit=limit,
        )

    if cancel_id is not None:
        if not isinstance(cancel_id, str) or not cancel_id.strip():
            return tool_error("spawn_agent cancel_id must be a non-empty string.")

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

    # Match delegate_task's live-view contract: pre-create the append-only
    # transcript before dispatch so callers can attach `tail -f` immediately.
    # The helper is deliberately best-effort and never blocks spawning when
    # the cache directory is unavailable.
    from tools.delegation_live_log import (
        create_live_transcripts,
        update_manifest_statuses,
        wrap_progress_callback,
    )

    _live_id, live_writers, live_paths = create_live_transcripts(
        [{"goal": clean_prompt}],
        delegation_id=spawn_id,
    )
    live_writer = live_writers[0] if live_writers else None

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
        if live_writer is not None:
            child.tool_progress_callback = wrap_progress_callback(
                getattr(child, "tool_progress_callback", None),
                live_writer,
            )
            if live_paths:
                child._live_transcript_path = live_paths[0]
    finally:
        model_tools._last_resolved_tool_names = parent_tool_names

    # spawn_agent intentionally does not populate the delegate_task /agents
    # tree.  The async registry still owns and interrupts the live child.
    child._subagent_id = None
    _detach_child(parent_agent, child)

    def runner() -> Dict[str, Any]:
        from tools.delegate_tool import _apply_summary_budget

        try:
            result = _run_single_child(0, clean_prompt, child, parent_agent)
            _apply_summary_budget(
                [result],
                parent_agent,
                retrieval_id=spawn_id,
            )
            result.pop("_child_role", None)
            child_cost = result.pop("_child_cost_usd", 0.0)
            try:
                parent_cost = float(
                    getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0
                )
                parent_agent.session_estimated_cost_usd = parent_cost + float(child_cost or 0.0)
            except (TypeError, ValueError):
                pass
            if live_paths:
                result["live_transcript"] = live_paths[0]
                result["live_transcripts"] = list(live_paths)
            if live_writer is not None:
                live_writer.finalize(result)
            update_manifest_statuses(_live_id, [result])
            return result
        except Exception as exc:
            failed = {
                "task_index": 0,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            if live_writer is not None:
                live_writer.finalize(failed)
            update_manifest_statuses(_live_id, [failed])
            raise

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
        failed = {
            "task_index": 0,
            "status": "error",
            "error": dispatch.get("error") or "Failed to spawn background subagent.",
        }
        if live_writer is not None:
            live_writer.finalize(failed)
        update_manifest_statuses(_live_id, [failed])
        try:
            child.close()
        except Exception:
            logger.debug("spawn_agent child cleanup failed", exc_info=True)
        return tool_error(dispatch.get("error") or "Failed to spawn background subagent.")

    from tools.delegate_tool import _emit_parent_console

    _emit_parent_console(parent_agent, f"🔀 {spawn_id} spawned: {display_label}")
    payload: Dict[str, Any] = {"id": spawn_id, "status": "running"}
    if live_paths:
        payload["live_transcripts"] = list(live_paths)
        payload["live_transcripts_hint"] = (
            "The subagent streams a human-readable transcript to this "
            "append-only file. Read it or use `tail -f` to watch progress."
        )
    return json.dumps(payload, ensure_ascii=False)


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
        result_id=args.get("result_id"),
        offset=args.get("offset"),
        limit=args.get("limit"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
)
