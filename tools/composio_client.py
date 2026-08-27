"""Small, optional-dependency wrapper around the current Composio SDK."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping


class ComposioError(RuntimeError):
    """An operator-actionable Composio integration error."""


MAX_COMPOSIO_RESULT_CHARS = 50_000


def _as_dict(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _as_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_dict(v) for v in value]
    for method in ("model_dump", "to_dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            return _as_dict(fn())
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {k: _as_dict(v) for k, v in data.items() if not k.startswith("_")}
    return str(value)


def _items(value: Any) -> list[Any]:
    items = getattr(value, "items", None)
    if items is None and isinstance(value, dict):
        items = value.get("items")
    return list(items or [])


def _nested(data: Any, *paths: str) -> Any:
    obj = _as_dict(data)
    for path in paths:
        cur = obj
        for part in path.split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if cur not in (None, ""):
            return cur
    return None


@dataclass(frozen=True)
class ComposioSettings:
    enabled: bool
    apps: tuple[str, ...]
    user_id: str
    api_key: str | None
    allowed_actions: Mapping[str, frozenset[str]] | None = None
    scopes: Mapping[str, tuple[str, ...]] | None = None

    @classmethod
    def load(cls) -> "ComposioSettings":
        from hermes_cli.config import load_config_readonly

        block = load_config_readonly().get("composio") or {}
        if not isinstance(block, dict):
            block = {}
        apps = block.get("apps") or []
        if isinstance(apps, str):
            apps = [part.strip() for part in apps.split(",")]
        normalized = tuple(dict.fromkeys(str(app).strip().lower() for app in apps if str(app).strip()))
        raw_actions = block.get("allowed_actions") or {}
        if not isinstance(raw_actions, dict):
            raw_actions = {}
        allowed_actions: dict[str, frozenset[str]] = {}
        for raw_app, raw_values in raw_actions.items():
            app = str(raw_app).strip().lower()
            if not app or app not in normalized or not isinstance(raw_values, (list, tuple, set, frozenset)):
                continue
            allowed_actions[app] = frozenset(
                str(value).strip().upper()
                for value in raw_values
                if str(value).strip()
            )
        raw_scopes = block.get("scopes") or {}
        if not isinstance(raw_scopes, dict):
            raw_scopes = {}
        scopes: dict[str, tuple[str, ...]] = {}
        for raw_app, raw_values in raw_scopes.items():
            app = str(raw_app).strip().lower()
            if not app or app not in normalized or not isinstance(raw_values, (list, tuple)):
                continue
            scopes[app] = tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in raw_values
                    if str(value).strip()
                )
            )
        # ``entity_id`` is accepted as a compatibility spelling, but the current
        # SDK calls this stable external identity ``user_id``.
        user_id = str(block.get("user_id") or block.get("entity_id") or "default").strip()
        api_key = os.environ.get("COMPOSIO_API_KEY") or block.get("api_key")
        return cls(
            block.get("enabled") is True,
            normalized,
            user_id,
            str(api_key).strip() if api_key else None,
            allowed_actions,
            scopes,
        )


class ComposioClient:
    def __init__(self, settings: ComposioSettings | None = None, *, sdk: Any = None):
        self.settings = settings or ComposioSettings.load()
        if not self.settings.enabled:
            raise ComposioError("Composio is disabled; set composio.enabled: true in config.yaml.")
        if not self.settings.api_key:
            raise ComposioError("Composio API key is missing (set COMPOSIO_API_KEY or composio.api_key).")
        if sdk is None:
            try:
                from tools.lazy_deps import ensure
                ensure("tool.composio")
                from composio import Composio
            except Exception as exc:
                raise ComposioError(f"Composio SDK is unavailable: {exc}") from exc
            sdk = Composio(api_key=self.settings.api_key)
        self.sdk = sdk

    def require_enabled(self) -> None:
        if not self.settings.enabled:
            raise ComposioError("Composio is disabled; set composio.enabled: true in config.yaml.")

    def require_app(self, app: str) -> str:
        slug = str(app or "").strip().lower()
        if not slug or slug not in self.settings.apps:
            allowed = ", ".join(self.settings.apps) or "none"
            raise ComposioError(f"Composio app '{slug or app}' is not allowed (allowed: {allowed}).")
        return slug

    def require_action(self, app: str, action: str) -> str:
        action_slug = str(action or "").strip().upper()
        if not action_slug:
            raise ComposioError("action is required.")
        allowed = (self.settings.allowed_actions or {}).get(app, frozenset())
        if action_slug not in allowed:
            raise ComposioError(
                f"Composio action '{action_slug}' is not in the operator allowlist for '{app}'."
            )
        return action_slug

    def list_toolkits(self) -> list[dict[str, Any]]:
        self.require_enabled()
        if not self.settings.apps:
            return []
        response = self.sdk.toolkits.list(limit=1000, managed_by="all", sort_by="alphabetically")
        result = []
        for item in _items(response):
            data = _as_dict(item)
            slug = str(_nested(data, "slug") or "").lower()
            if slug in self.settings.apps:
                result.append(data)
        return result

    def _auth_config_id(self, app: str) -> str:
        response = self.sdk.auth_configs.list(toolkit_slug=app, is_composio_managed=True, limit=1000)
        config_name = f"Hermes {app} scoped"
        configs = [
            config for config in _items(response)
            if str(_nested(config, "name") or "") == config_name
        ]
        if not configs:
            options: dict[str, Any] = {
                "type": "use_composio_managed_auth",
                "name": config_name,
            }
            configured_scopes = (self.settings.scopes or {}).get(app, ())
            if configured_scopes:
                options["credentials"] = {"scopes": ",".join(configured_scopes)}
            created = self.sdk.auth_configs.create(app, options)
            config_id = _nested(created, "id", "nanoid", "auth_config.id")
        else:
            config_id = _nested(configs[0], "id", "nanoid", "auth_config.id")
        if not config_id:
            raise ComposioError(f"Composio returned no auth-config id for '{app}'.")
        return str(config_id)

    def initiate_connection(self, app: str, *, callback_url: str | None = None) -> dict[str, Any]:
        self.require_enabled()
        slug = self.require_app(app)
        auth_config_id = self._auth_config_id(slug)
        request = self.sdk.connected_accounts.link(
            self.settings.user_id,
            auth_config_id,
            callback_url=callback_url,
        )
        return {
            "id": str(getattr(request, "id", "")),
            "status": str(getattr(request, "status", "INITIATED")),
            "redirect_url": getattr(request, "redirect_url", None),
            "app": slug,
        }

    def list_connections(self) -> list[dict[str, Any]]:
        self.require_enabled()
        if not self.settings.apps:
            return []
        response = self.sdk.connected_accounts.list(
            user_ids=[self.settings.user_id], toolkit_slugs=list(self.settings.apps), limit=1000
        )
        result = []
        for item in _items(response):
            data = _as_dict(item)
            toolkit = str(_nested(data, "toolkit.slug", "toolkit_slug", "auth_config.toolkit.slug") or "").lower()
            user_id = str(_nested(data, "user_id", "user.id") or "")
            if toolkit in self.settings.apps and user_id in ("", self.settings.user_id):
                result.append(data)
        return result

    def _require_account(self, connection_id: str, *, app: str | None = None) -> Any:
        target = str(connection_id or "").strip()
        if not target:
            raise ComposioError("connection_id is required.")
        account = self.sdk.connected_accounts.get(target)
        toolkit = str(_nested(account, "toolkit.slug", "toolkit_slug", "auth_config.toolkit.slug") or "").lower()
        if not toolkit:
            raise ComposioError("Could not determine the connection's toolkit; refusing the operation.")
        self.require_app(toolkit)
        if app is not None and toolkit != app:
            raise ComposioError(f"Connection '{target}' belongs to '{toolkit}', not allowed app '{app}'.")
        user_id = str(_nested(account, "user_id", "user.id") or "")
        if user_id and user_id != self.settings.user_id:
            raise ComposioError("Connection belongs to a different Composio user; refusing the operation.")
        return account

    def delete_connection(self, connection_id: str) -> dict[str, Any]:
        self.require_enabled()
        target = str(connection_id or "").strip()
        # Fetch first and enforce toolkit + user ownership against the
        # authoritative account before the destructive request.
        self._require_account(target)
        response = self.sdk.connected_accounts.delete(target)
        return {"deleted": True, "connection_id": target, "result": _as_dict(response)}

    def execute(self, app: str, action: str, params: dict[str, Any], *, connected_account_id: str | None = None) -> dict[str, Any]:
        self.require_enabled()
        slug = self.require_app(app)
        action_slug = self.require_action(slug, action)
        raw_tool = self.sdk.tools.get_raw_composio_tool_by_slug(action_slug)
        actual_app = str(_nested(raw_tool, "toolkit.slug", "toolkit_slug", "app_name") or "").lower()
        if actual_app != slug:
            raise ComposioError(
                f"Action '{action_slug}' belongs to '{actual_app or 'unknown'}', not allowed app '{slug}'."
            )
        if connected_account_id:
            self._require_account(connected_account_id, app=slug)
        result = self.sdk.tools.execute(
            action_slug,
            arguments=params,
            user_id=self.settings.user_id,
            connected_account_id=connected_account_id,
            dangerously_skip_version_check=True,
        )
        return _as_dict(result)


def json_result(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, default=str)
    if len(serialized) <= MAX_COMPOSIO_RESULT_CHARS:
        return serialized
    # The preview is itself JSON-escaped. One serialized character can expand
    # to two here (quotes/backslashes), so leave ample room for valid framing.
    preview_limit = MAX_COMPOSIO_RESULT_CHARS // 3
    return json.dumps({
        "truncated": True,
        "original_chars": len(serialized),
        "preview": serialized[:preview_limit],
    }, ensure_ascii=False)
