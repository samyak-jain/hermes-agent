"""Shared profile-aware main-model selection.

The multiplex gateway and each profile-local cron scheduler must resolve the
same ``agent.profile_models`` entry.  Keep the config-only portion here so the
two execution paths cannot drift while retaining their separate credential and
runtime construction.
"""

from __future__ import annotations

from typing import Any


def resolve_profile_model_config(
    config: Any,
    profile_name: str | None,
) -> dict[str, Any]:
    """Return the normalized model override for ``profile_name``.

    An absent or malformed entry is represented as an empty dict.  Only fields
    consumed by the gateway/cron runtime boundary are returned.
    """
    if not isinstance(config, dict):
        return {}
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return {}
    profiles = agent.get("profile_models")
    if not isinstance(profiles, dict):
        return {}
    raw = profiles.get(str(profile_name or "default"))
    if not isinstance(raw, dict):
        return {}

    resolved: dict[str, Any] = {}
    model = raw.get("model") or raw.get("default")
    if isinstance(model, str) and model.strip():
        resolved["model"] = model.strip()
    provider = raw.get("provider")
    if isinstance(provider, str) and provider.strip():
        resolved["provider"] = provider.strip()
    for key in ("api_mode", "base_url", "max_tokens"):
        if raw.get(key) not in (None, ""):
            resolved[key] = raw[key]
    return resolved
