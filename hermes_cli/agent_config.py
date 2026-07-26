"""Validated agent-facing configuration broker.

This module is the narrow write boundary between a sandboxed conversational
agent and the gateway-owned ``config.yaml``.  It deliberately does *not*
expose the file or the managed overlay.  Instead it:

* exposes either an operator allowlist or every schema-known/existing,
  non-secret setting that is not pinned by the managed overlay;
* reports effective values together with their source and apply semantics;
* rejects managed, credential-shaped, container-valued, internal, or absent
  unknown leaves;
* applies optimistic concurrency plus a cross-process advisory lock;
* snapshots the prior file, writes atomically, and appends a metadata-only
  audit record that can be used for a fail-closed rollback.

Interactive approval policy belongs to ``tools/config_tool.py`` so this module
remains usable from CLI tests and future non-model clients without smuggling
UI state into the persistence layer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from hermes_constants import get_hermes_home

try:  # POSIX production path; Windows keeps process-local atomicity.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


SAFE_BUILTIN_PATTERNS = frozenset(
    {
        "display.*",
        "streaming.enabled",
        "agent.reasoning_effort",
        "agent.verify_on_stop",
        "agent.max_turns",
        "memory.memory_char_limit",
        "memory.user_char_limit",
        "memory.memory_enabled",
        "memory.user_profile_enabled",
        "memory.write_approval",
        "session_reset.*",
        "skills.creation_nudge_interval",
        "skills.disabled",
        "compression.abort_on_summary_failure",
        "compression.codex_app_server_auto",
        "compression.codex_gpt55_autoraise",
        "compression.codex_gpt55_autoraise_notice",
        "compression.enabled",
        "compression.hygiene_hard_message_limit",
        "compression.in_place",
        "compression.protect_first_n",
        "compression.protect_last_n",
        "compression.target_ratio",
        "compression.threshold",
        "prompt_caching.cache_ttl",
    }
)

GUARDED_BUILTIN_PATTERNS = frozenset(
    {
        "approvals.*",
        "browser.allow_unsafe_evaluate",
        "code_execution.*",
        "command_allowlist",
        "credential_pool_strategies.*",
        "delegation.*",
        "discord.*",
        "mcp_servers.*",
        "model.default",
        "model.provider",
        "platform_toolsets.*",
        "platforms.*",
        "plugins.*",
        "terminal.*",
        "updates.*",
    }
)

# Configuration may describe where a credential comes from, but the broker
# must never read or write credential material.  Match whole dotted segments
# so harmless names such as ``token_usage`` are not rejected accidentally.
_SECRET_SEGMENT_RE = re.compile(
    r"(?:^|_)(?:api_?key|secret|password|passwd|token|credential|cookie|"
    r"private_?key|client_?secret|authorization)(?:$|_)",
    re.IGNORECASE,
)
_OBVIOUS_SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"\b(?:sk|pk)[-_][A-Za-z0-9._-]{12,}|"
    r"\b(?:ghp|github_pat|xox[aboprs]|bb)_[A-Za-z0-9._-]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")",
    re.IGNORECASE,
)
_MCP_ENV_PATH_RE = re.compile(r"^mcp_servers\.[^.]+\.env$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

_APPLY_MODE_EXACT = {
    "agent.reasoning_effort": "next_turn",
    "display.tool_progress": "next_turn",
    "display.show_reasoning": "next_turn",
    "session_reset.enabled": "next_turn",
}
_NEXT_SESSION_PREFIXES = (
    "agent.",
    "compression.",
    "display.",
    "memory.",
    "prompt_caching.",
    "session_reset.",
    "skills.",
    "streaming.",
)


class AgentConfigError(RuntimeError):
    """A validated agent configuration operation was rejected."""


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return path.startswith(prefix + ".") and len(path) > len(prefix) + 1
    return path == pattern


def _matches_any(path: str, patterns: set[str] | frozenset[str] | list[str]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)


def _configured_patterns(config: dict, key: str) -> set[str]:
    block = config.get("agent_config") if isinstance(config, dict) else None
    raw = block.get(key) if isinstance(block, dict) else None
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def broker_enabled(config: Optional[dict] = None) -> bool:
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    block = config.get("agent_config") if isinstance(config, dict) else None
    return bool(isinstance(block, dict) and block.get("enabled") is True)


def approval_required(config: Optional[dict] = None) -> bool:
    """Return the operator-owned mutation approval policy.

    Missing or malformed policy fails closed to the historical behavior.
    ``agent_config`` cannot edit itself, so an agent cannot weaken this gate.
    """
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    block = config.get("agent_config") if isinstance(config, dict) else None
    if not isinstance(block, dict):
        return True
    return block.get("require_approval", True) is not False


def _ownership_mode(config: dict) -> str:
    block = config.get("agent_config") if isinstance(config, dict) else None
    mode = (
        str(block.get("ownership_mode") or "allowlist").strip().lower()
        if isinstance(block, dict)
        else "allowlist"
    )
    return mode if mode in {"allowlist", "unmanaged"} else "allowlist"


def _path_class(path: str, config: dict) -> Optional[str]:
    if _ownership_mode(config) == "unmanaged":
        from hermes_cli import managed_scope
        from hermes_cli.config import _validate_config_key

        known, _suggestion = _validate_config_key(path)
        marker = object()
        if not known and _nested(config, path, marker) is marker:
            return None
        return "operator_managed" if managed_scope.is_key_managed(path) else "agent_owned"

    safe_policy = _configured_patterns(config, "editable_paths")
    guarded_policy = _configured_patterns(config, "guarded_paths")
    if _matches_any(path, SAFE_BUILTIN_PATTERNS) and _matches_any(path, safe_policy):
        return "safe"
    if _matches_any(path, GUARDED_BUILTIN_PATTERNS) and _matches_any(path, guarded_policy):
        return "guarded"
    return None


def _secret_shaped_path(path: str) -> bool:
    return any(_SECRET_SEGMENT_RE.search(segment) for segment in path.split("."))


def _value_looks_secret(value: Any) -> bool:
    if isinstance(value, str):
        if _OBVIOUS_SECRET_VALUE_RE.search(value):
            return True
        try:
            from agent.redact import redact_sensitive_text

            return (
                redact_sensitive_text(
                    value,
                    force=True,
                    file_read=True,
                    redact_url_credentials=True,
                )
                != value
            )
        except Exception:
            # The explicit patterns above remain the fail-safe minimum when
            # the general redactor is unavailable during early startup.
            return False
    if isinstance(value, list):
        return any(_value_looks_secret(item) for item in value)
    if isinstance(value, dict):
        return any(
            _SECRET_SEGMENT_RE.search(str(key)) or _value_looks_secret(item)
            for key, item in value.items()
        )
    return False


def _is_mcp_env_reference_map(path: str, value: Any) -> bool:
    """Accept only name-preserving environment references, never plaintext.

    MCP environment configuration is structurally a mapping, but its values
    should point at credentials already present in the gateway environment.
    Requiring ``KEY: ${KEY}`` keeps secret material out of config.yaml and
    prevents this narrow structured exception from becoming a generic mapping
    write escape hatch.
    """
    if not _MCP_ENV_PATH_RE.fullmatch(path) or not isinstance(value, dict):
        return False
    if not value or len(value) > 128:
        return False
    for key, reference in value.items():
        if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
            return False
        if not isinstance(reference, str):
            return False
        match = _ENV_REFERENCE_RE.fullmatch(reference)
        if match is None or match.group(1) != key:
            return False
    return True


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(child, dict):
                out.update(_flatten(child, path))
            else:
                out[path] = child
    elif prefix:
        out[prefix] = value
    return out


def _nested(config: dict, path: str, missing: Any) -> Any:
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return missing
        node = node[part]
    return node


def _source_for(path: str, raw: dict, managed: dict) -> str:
    marker = object()
    if _nested(managed, path, marker) is not marker:
        return "managed"
    if _nested(raw, path, marker) is not marker:
        return "user"
    return "default"


def _apply_mode(path: str) -> str:
    if path in _APPLY_MODE_EXACT:
        return _APPLY_MODE_EXACT[path]
    if path.startswith(_NEXT_SESSION_PREFIXES):
        return "next_session"
    # Settings outside the explicitly understood live/session surfaces may be
    # consumed by gateway adapters, provider clients, MCP discovery, or other
    # startup-only components. Persist them, but report the real conservative
    # boundary instead of falsely promising that a new chat is sufficient.
    return "restart_required"


def _assert_enabled(config: dict) -> None:
    if not broker_enabled(config):
        raise AgentConfigError(
            "Agent-managed configuration is disabled by the operator."
        )


def _assert_path_allowed(path: str, config: dict, *, for_write: bool) -> str:
    path = str(path or "").strip()
    if not path or path.startswith(".") or path.endswith(".") or ".." in path:
        raise AgentConfigError("A valid dotted configuration leaf is required.")
    if path.split(".", 1)[0].startswith("_"):
        raise AgentConfigError("Internal configuration metadata is not agent-editable.")
    if path == "agent_config" or path.startswith("agent_config."):
        raise AgentConfigError("The configuration broker policy cannot edit itself.")
    if _secret_shaped_path(path):
        raise AgentConfigError(
            f"'{path}' is credential-shaped. Secrets must use the authentication "
            "or secret-management workflow, never config.yaml."
        )
    classification = _path_class(path, config)
    if classification is None:
        raise AgentConfigError(
            f"'{path}' is not a recognized or operator-authorized agent "
            "configuration leaf."
        )
    if for_write:
        from hermes_cli import managed_scope

        if classification == "operator_managed" or managed_scope.is_key_managed(path):
            raise AgentConfigError(
                f"'{path}' is managed by Kumo/your administrator and cannot be "
                "changed through the agent-owned configuration."
            )
    return classification


def inspect_config(path: Optional[str] = None) -> dict:
    """Return the non-secret agent-visible effective configuration view."""
    from hermes_cli import managed_scope
    from hermes_cli.config import load_config, read_raw_config

    effective = load_config()
    _assert_enabled(effective)
    raw = read_raw_config()
    managed = managed_scope.load_managed_config()
    effective_flat = _flatten(effective)

    if path:
        classification = _assert_path_allowed(path, effective, for_write=False)
        marker = object()
        value = _nested(effective, path, marker)
        if value is marker:
            raise AgentConfigError(f"Configuration leaf '{path}' is not set.")
        if isinstance(value, dict) or _value_looks_secret(value):
            raise AgentConfigError(
                f"'{path}' cannot be returned through the non-secret configuration view."
            )
        source = _source_for(path, raw, managed)
        return {
            "success": True,
            "path": path,
            "value": value,
            "source": source,
            "editable": source != "managed",
            "classification": classification,
            "apply": _apply_mode(path),
        }

    settings = []
    for dotted in sorted(effective_flat):
        top = dotted.split(".", 1)[0]
        if top.startswith("_") or top == "agent_config":
            continue
        classification = _path_class(dotted, effective)
        if classification is None or _secret_shaped_path(dotted):
            continue
        value = effective_flat[dotted]
        if isinstance(value, dict) or _value_looks_secret(value):
            continue
        source = _source_for(dotted, raw, managed)
        settings.append(
            {
                "path": dotted,
                "value": value,
                "source": source,
                "editable": source != "managed",
                "classification": classification,
                "apply": _apply_mode(dotted),
            }
        )
    return {"success": True, "settings": settings, "count": len(settings)}


def _state_dir() -> Path:
    return get_hermes_home() / "state" / "agent-config"


def _file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_raw_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _load_yaml_bytes(data: bytes) -> dict:
    if not data:
        return {}
    parsed = yaml.safe_load(data.decode("utf-8")) or {}
    if not isinstance(parsed, dict):
        raise AgentConfigError("config.yaml must contain a YAML mapping.")
    return parsed


def _validate_value(path: str, value: Any) -> None:
    from hermes_cli.config import (
        DEFAULT_CONFIG,
        _default_value_for_key,
        _validate_config_key,
        load_config,
    )

    known, _suggestion = _validate_config_key(path)
    if not known:
        marker = object()
        current = _nested(load_config(), path, marker)
        if current is marker:
            raise AgentConfigError(f"'{path}' is not recognized by this Hermes version.")
    if isinstance(value, dict):
        if not _is_mcp_env_reference_map(path, value):
            raise AgentConfigError(
                "Mappings are not agent-editable except mcp_servers.<name>.env, "
                "whose values must use exact KEY: ${KEY} environment references."
            )
        return
    if isinstance(value, str):
        if len(value) > 4096 or "\x00" in value:
            raise AgentConfigError("String configuration values must be under 4096 characters.")
    elif isinstance(value, list):
        if len(value) > 256 or any(isinstance(item, (dict, list)) for item in value):
            raise AgentConfigError("Lists are limited to 256 scalar values.")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise AgentConfigError(f"Unsupported configuration value type: {type(value).__name__}.")

    if path == "agent.reasoning_effort":
        if value not in {
            "",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
            "ultra",
        }:
            raise AgentConfigError("agent.reasoning_effort has an unsupported value.")
        return
    if path == "agent.verify_on_stop":
        if value not in {"auto", True, False}:
            raise AgentConfigError("agent.verify_on_stop must be 'auto', true, or false.")
        return

    default = _default_value_for_key(path)
    if default is None and not known:
        default = current
    if default is None:
        # A known dynamically-shaped leaf has no concrete type information.
        return
    expected = type(default)
    valid = (
        (expected is bool and isinstance(value, bool))
        or (expected is int and isinstance(value, int) and not isinstance(value, bool))
        or (
            expected is float
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        or (expected is str and isinstance(value, str))
        or (expected is list and isinstance(value, list))
        or isinstance(value, expected)
    )
    if not valid:
        raise AgentConfigError(
            f"'{path}' expects {expected.__name__}, got {type(value).__name__}."
        )

    if path == "agent.max_turns" and not 1 <= value <= 1000:
        raise AgentConfigError("agent.max_turns must be between 1 and 1000.")
    if path in {"memory.memory_char_limit", "memory.user_char_limit"} and not 1 <= value <= 100000:
        raise AgentConfigError(f"{path} must be between 1 and 100000 characters.")
    if path in {"compression.threshold", "compression.target_ratio"} and not 0 < float(value) <= 1:
        raise AgentConfigError(f"{path} must be greater than 0 and at most 1.")
    if path == "prompt_caching.cache_ttl" and value not in {"5m", "1h"}:
        raise AgentConfigError("prompt_caching.cache_ttl must be '5m' or '1h'.")

    # Keep the import above intentionally used: if DEFAULT_CONFIG stops being a
    # mapping, this broker must fail during validation rather than weakening.
    if not isinstance(DEFAULT_CONFIG, dict):  # pragma: no cover - invariant
        raise AgentConfigError("Hermes configuration schema is unavailable.")


def _validate_candidate(raw: dict) -> None:
    from hermes_cli import managed_scope
    from hermes_cli.config import DEFAULT_CONFIG, _deep_merge, validate_config_structure

    effective = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), copy.deepcopy(raw))
    effective = managed_scope.apply_managed_overlay(effective)
    errors = [
        issue.message
        for issue in validate_config_structure(effective)
        if getattr(issue, "severity", "") == "error"
    ]
    if errors:
        raise AgentConfigError("Configuration validation failed: " + "; ".join(errors))


def prepare_change(
    *,
    operation: str,
    path: str,
    value: Any = None,
    reason: str,
) -> dict:
    """Build an optimistic set/unset change preview."""
    from hermes_cli.config import get_config_path, load_config

    effective = load_config()
    _assert_enabled(effective)
    classification = _assert_path_allowed(path, effective, for_write=True)
    reason = str(reason or "").strip()
    if not reason:
        raise AgentConfigError("A concise operator-request reason is required.")
    if len(reason) > 500:
        raise AgentConfigError("The change reason must be at most 500 characters.")
    if _value_looks_secret(reason):
        raise AgentConfigError("The change reason appears to contain credential material.")
    if operation == "set":
        _validate_value(path, value)
        if _value_looks_secret(value) and not _is_mcp_env_reference_map(path, value):
            raise AgentConfigError(
                "The proposed value resembles credential material. Use the "
                "secret-management or authentication workflow instead."
            )
    elif operation != "unset":
        raise AgentConfigError("Operation must be 'set' or 'unset'.")

    config_path = get_config_path()
    before = _read_raw_bytes(config_path)
    raw = _load_yaml_bytes(before)
    marker = object()
    old_value = _nested(raw, path, marker)
    if operation == "unset" and old_value is marker:
        raise AgentConfigError(f"'{path}' is not explicitly set in the user config.")
    if operation == "set" and old_value is not marker and old_value == value:
        raise AgentConfigError(f"'{path}' already has the requested user value.")
    return {
        "operation": operation,
        "path": path,
        "value": value,
        "old_present": old_value is not marker,
        "old_value": None if old_value is marker else old_value,
        "reason": reason,
        "classification": classification,
        "apply": _apply_mode(path),
        "expected_hash": _file_hash(before),
        "before_exists": config_path.exists(),
    }


def _unset_nested(config: dict, path: str) -> bool:
    parts = path.split(".")
    node: Any = config
    parents: list[tuple[dict, str]] = []
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        parents.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break
    return True


def _write_backup(revision: str, before: bytes) -> Path:
    revisions = _state_dir() / "revisions"
    revisions.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(revisions, 0o700)
    except OSError:
        pass
    path = revisions / f"{revision}.yaml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(before)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _append_audit(record: dict) -> None:
    path = _state_dir() / "audit.jsonl"
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _new_revision() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)


def _restore_snapshot(config_path: Path, before: bytes, *, existed: bool) -> None:
    """Restore an exact validated snapshot, including prior non-existence."""
    from hermes_cli.config import atomic_config_bytes_write, invalidate_config_caches

    if existed:
        atomic_config_bytes_write(config_path, before)
    else:
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass
        invalidate_config_caches(config_path)


def _restore_bytes_after_failed_audit(
    config_path: Path, before: bytes, *, existed: bool
) -> None:
    """Compensate a committed write when its mandatory audit append fails."""
    _restore_snapshot(config_path, before, existed=existed)


def apply_change(prepared: dict, *, actor: str = "") -> dict:
    """Apply a prepared preview if config.yaml has not changed meanwhile."""
    from hermes_cli.config import (
        _set_nested,
        atomic_config_write,
        config_write_lock,
        get_config_path,
    )

    config_path = get_config_path()
    with config_write_lock(config_path):
        before = _read_raw_bytes(config_path)
        actual_hash = _file_hash(before)
        if (
            actual_hash != prepared.get("expected_hash")
            or config_path.exists() != bool(prepared.get("before_exists"))
        ):
            raise AgentConfigError(
                "config.yaml changed after this change was prepared. Nothing was "
                "written; inspect the current value and retry."
            )
        raw = _load_yaml_bytes(before)
        if prepared["operation"] == "set":
            _set_nested(raw, prepared["path"], prepared.get("value"))
        else:
            if not _unset_nested(raw, prepared["path"]):
                raise AgentConfigError(
                    f"'{prepared['path']}' changed before the prepared unset was applied."
                )
        _validate_candidate(raw)
        revision = _new_revision()
        _write_backup(revision, before)
        atomic_config_write(config_path, raw, sort_keys=False)
        after = _read_raw_bytes(config_path)
        record = {
            "revision": revision,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "operation": prepared["operation"],
            "path": prepared["path"],
            "classification": prepared["classification"],
            "apply": prepared["apply"],
            "reason": prepared["reason"],
            "actor": str(actor or "")[:200],
            "before_hash": actual_hash,
            "after_hash": _file_hash(after),
            "before_exists": bool(prepared.get("before_exists")),
            "after_exists": config_path.exists(),
        }
        try:
            _append_audit(record)
        except Exception as audit_exc:
            try:
                _restore_bytes_after_failed_audit(
                    config_path,
                    before,
                    existed=bool(prepared.get("before_exists")),
                )
            except Exception as restore_exc:
                raise AgentConfigError(
                    "Configuration was written but its audit record failed, and "
                    "automatic restoration also failed. Stop and ask the operator "
                    f"to reconcile config.yaml. Audit error: {audit_exc}; "
                    f"restore error: {restore_exc}"
                ) from restore_exc
            raise AgentConfigError(
                "Configuration audit persistence failed, so the prepared change "
                "was automatically rolled back."
            ) from audit_exc
        from hermes_cli.config import invalidate_config_caches

        invalidate_config_caches(config_path)
    return {
        "success": True,
        "revision": revision,
        "operation": prepared["operation"],
        "path": prepared["path"],
        "classification": prepared["classification"],
        "apply": prepared["apply"],
        "message": (
            "Configuration updated atomically. "
            + (
                "The current conversation keeps its cached prompt; this applies "
                "from the next session."
                if prepared["apply"] == "next_session"
                else (
                    "The setting is persisted but requires a drained gateway "
                    "restart before every runtime component is guaranteed to "
                    "observe it."
                    if prepared["apply"] == "restart_required"
                    else "No gateway restart is required."
                )
            )
        ),
    }


def history(limit: int = 20) -> dict:
    """Return metadata-only change history; values and backups stay private."""
    limit = max(1, min(int(limit or 20), 100))
    path = _state_dir() / "audit.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    records = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        records.append(
            {
                key: item.get(key)
                for key in (
                    "revision",
                    "timestamp",
                    "operation",
                    "path",
                    "classification",
                    "apply",
                    "reason",
                    "actor",
                )
            }
        )
    return {"success": True, "changes": records, "count": len(records)}


def prepare_rollback(revision: str, *, reason: str) -> dict:
    """Build a rollback preview, allowed only while that revision is current."""
    revision = str(revision or "").strip()
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", revision):
        raise AgentConfigError("A valid revision ID is required.")
    reason = str(reason or "").strip()
    if not reason:
        raise AgentConfigError("A concise rollback reason is required.")
    if len(reason) > 500 or _value_looks_secret(reason):
        raise AgentConfigError(
            "The rollback reason must be under 500 characters and contain no credentials."
        )
    path = _state_dir() / "audit.jsonl"
    target = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if item.get("revision") == revision:
            target = item
            break
    if target is None:
        raise AgentConfigError(f"Unknown configuration revision '{revision}'.")
    backup = _state_dir() / "revisions" / f"{revision}.yaml"
    before = backup.read_bytes()
    _validate_candidate(_load_yaml_bytes(before))
    from hermes_cli.config import get_config_path, load_config

    effective = load_config()
    _assert_enabled(effective)
    current = _read_raw_bytes(get_config_path())
    current_exists = get_config_path().exists()
    if (
        _file_hash(current) != target.get("after_hash")
        or current_exists != bool(target.get("after_exists", True))
    ):
        raise AgentConfigError(
            "Rollback refused because config.yaml has changed since that revision. "
            "This prevents an old rollback from erasing later operator changes."
        )
    return {
        "operation": "rollback",
        "revision": revision,
        "path": target.get("path"),
        "reason": reason[:500],
        "expected_hash": _file_hash(current),
        "expected_exists": current_exists,
        "restore_bytes": before,
        "restore_exists": bool(target.get("before_exists", True)),
        "apply": target.get("apply") or "next_session",
    }


def apply_rollback(prepared: dict, *, actor: str = "") -> dict:
    from hermes_cli.config import config_write_lock, get_config_path

    config_path = get_config_path()
    with config_write_lock(config_path):
        current = _read_raw_bytes(config_path)
        current_exists = config_path.exists()
        if (
            _file_hash(current) != prepared.get("expected_hash")
            or current_exists != bool(prepared.get("expected_exists", True))
        ):
            raise AgentConfigError(
                "config.yaml changed after the rollback was prepared. Nothing was written."
            )
        restored = _load_yaml_bytes(prepared["restore_bytes"])
        _validate_candidate(restored)
        rollback_revision = _new_revision()
        _write_backup(rollback_revision, current)
        _restore_snapshot(
            config_path,
            prepared["restore_bytes"],
            existed=bool(prepared.get("restore_exists", True)),
        )
        after = _read_raw_bytes(config_path)
        try:
            _append_audit(
                {
                    "revision": rollback_revision,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "operation": "rollback",
                    "path": prepared.get("path"),
                    "classification": "rollback",
                    "apply": prepared.get("apply") or "next_session",
                    "reason": prepared["reason"],
                    "actor": str(actor or "")[:200],
                    "rolled_back_revision": prepared["revision"],
                    "before_hash": prepared["expected_hash"],
                    "after_hash": _file_hash(after),
                    "before_exists": current_exists,
                    "after_exists": config_path.exists(),
                }
            )
        except Exception as audit_exc:
            try:
                _restore_bytes_after_failed_audit(
                    config_path, current, existed=current_exists
                )
            except Exception as restore_exc:
                raise AgentConfigError(
                    "Rollback was written but its audit record failed, and "
                    "automatic restoration also failed. Stop and ask the operator "
                    f"to reconcile config.yaml. Audit error: {audit_exc}; "
                    f"restore error: {restore_exc}"
                ) from restore_exc
            raise AgentConfigError(
                "Rollback audit persistence failed, so the rollback itself was undone."
            ) from audit_exc
        from hermes_cli.config import invalidate_config_caches

        invalidate_config_caches(config_path)
    return {
        "success": True,
        "revision": rollback_revision,
        "rolled_back_revision": prepared["revision"],
        "apply": prepared.get("apply") or "next_session",
        "message": "Configuration rollback applied atomically.",
    }
