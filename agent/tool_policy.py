"""Exact-name tool authorization shared by schema and execution paths.

Toolsets describe configured capability.  ``ToolAccessPolicy`` is the final,
per-agent authorization boundary applied after that capability is resolved.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, Iterable, Literal, Mapping

logger = logging.getLogger(__name__)

ToolPolicyMode = Literal["legacy", "allowlist", "unrestricted", "denylist"]


@dataclass(frozen=True)
class ToolAccessPolicy:
    mode: ToolPolicyMode = "legacy"
    allowed_names: frozenset[str] = frozenset()
    denied_names: frozenset[str] = frozenset()
    source: str = "legacy"
    valid: bool = True
    error: str = ""

    def allows(self, name: str) -> bool:
        if not self.valid or not isinstance(name, str) or not name:
            return False
        if self.mode == "allowlist":
            return name in self.allowed_names
        if self.mode == "denylist":
            return name not in self.denied_names
        return self.mode in {"legacy", "unrestricted"}

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(
            {
                "mode": self.mode,
                "allow": sorted(self.allowed_names),
                "deny": sorted(self.denied_names),
                "source": self.source,
                "valid": self.valid,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


LEGACY_TOOL_POLICY = ToolAccessPolicy()
DENY_ALL_TOOL_POLICY = ToolAccessPolicy(mode="allowlist", source="deny-all")


def _name_set(value: Any, *, field: str) -> tuple[frozenset[str], str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(), f"{field} must be a list of individual tool names"
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            return frozenset(), f"{field} contains an empty or non-string tool name"
        name = raw.strip()
        if name in {"*", "all"}:
            return frozenset(), f"{field} must contain individual names, not {name!r}"
        names.add(name)
    return frozenset(names), ""


def parse_tool_policy(
    raw: Any,
    *,
    source: str = "config",
    fallback: ToolAccessPolicy = LEGACY_TOOL_POLICY,
) -> ToolAccessPolicy:
    """Parse a policy. Invalid explicit input is deny-all, never unrestricted."""
    if raw is None:
        return fallback
    if isinstance(raw, ToolAccessPolicy):
        return raw
    if isinstance(raw, str):
        raw = {"mode": raw}
    if not isinstance(raw, Mapping):
        msg = f"tool policy at {source} must be a mapping"
        logger.error(msg)
        return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=msg)
    unknown = set(raw) - {"mode", "tools", "gateway_override_authority"}
    if unknown:
        msg = f"unknown tool policy keys at {source}: {', '.join(sorted(map(str, unknown)))}"
        logger.error(msg)
        return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=msg)
    authority = raw.get("gateway_override_authority")
    if authority is not None and str(authority).strip().lower() not in {"any", "managed_only"}:
        msg = (
            f"invalid gateway_override_authority {authority!r} at {source}; "
            "expected 'managed_only' or 'any'"
        )
        logger.error(msg)
        return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=msg)
    mode = str(raw.get("mode", "") or "").strip().lower()
    if mode not in {"legacy", "allowlist", "unrestricted", "denylist"}:
        msg = f"invalid tool policy mode {mode!r} at {source}"
        logger.error(msg)
        return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=msg)
    if mode == "allowlist":
        names, err = _name_set(raw.get("tools"), field=f"{source}.tools")
        if err:
            logger.error(err)
            return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=err)
        return ToolAccessPolicy(mode=mode, allowed_names=names, source=source)
    if mode == "denylist":
        names, err = _name_set(raw.get("tools", []), field=f"{source}.tools")
        if err:
            logger.error(err)
            return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=err)
        return ToolAccessPolicy(mode=mode, denied_names=names, source=source)
    if "tools" in raw:
        msg = f"{source}.tools is only valid for allowlist/denylist policies"
        logger.error(msg)
        return ToolAccessPolicy(mode="allowlist", source=source, valid=False, error=msg)
    return ToolAccessPolicy(mode=mode, source=source)


def policy_from_config(config: Mapping[str, Any] | None) -> ToolAccessPolicy:
    agent = (config or {}).get("agent") or {}
    raw = agent.get("tool_policy") if isinstance(agent, Mapping) else None
    return parse_tool_policy(raw, source="agent.tool_policy")


def filter_tool_definitions(definitions: Iterable[dict], policy: ToolAccessPolicy) -> list[dict]:
    return [
        definition
        for definition in definitions
        if policy.allows(str((definition.get("function") or {}).get("name") or ""))
    ]


def denied_tool_result(name: str, *, unavailable: bool = False) -> str:
    code = "tool_not_available_in_session" if unavailable else "tool_not_allowed_by_policy"
    message = (
        f"Tool {name!r} is not available in this session."
        if unavailable
        else f"Tool {name!r} is not allowed by this session's tool policy."
    )
    return json.dumps({"error": message, "code": code}, ensure_ascii=False)


def authorize_agent_tool(agent: Any, name: str) -> str | None:
    policy = getattr(agent, "tool_policy", LEGACY_TOOL_POLICY)
    if not policy.allows(name):
        return denied_tool_result(name)
    # Preserve historical direct-library semantics when no new policy was
    # configured. Exact/unrestricted policies are strict session scopes.
    if policy.mode == "legacy":
        return None
    names = getattr(agent, "valid_tool_names", set())
    if name not in names:
        return denied_tool_result(name, unavailable=True)
    return None
