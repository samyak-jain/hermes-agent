"""Browserbase cloud browser provider — plugin form.

Subclasses :class:`agent.browser_provider.BrowserProvider` (the plugin-facing
ABC introduced in PR #25214). The legacy in-tree module
``tools.browser_providers.browserbase`` was removed in the same PR; this file
is now the canonical implementation.

Browserbase requires direct ``BROWSERBASE_API_KEY`` and ``BROWSERBASE_PROJECT_ID``
credentials. Managed Nous gateway support has been removed — the Nous
subscription now routes through Browser Use instead (see
``plugins/browser/browser_use/``).

Config keys this provider responds to::

    browser:
      cloud_provider: "browserbase"

Auth env vars::

    BROWSERBASE_API_KEY=...       # https://browserbase.com
    BROWSERBASE_PROJECT_ID=...

Optional feature knobs::

    BROWSERBASE_BASE_URL=...      # default https://api.browserbase.com
    BROWSERBASE_PROXIES=true      # default true
    BROWSERBASE_PROXY_COUNTRY=... # ISO country code, e.g. US
    BROWSERBASE_PROXY_STATE=...   # optional state code, e.g. NY
    BROWSERBASE_PROXY_CITY=...    # optional city, e.g. NEW_YORK
    BROWSERBASE_REGION=...        # e.g. eu-central-1
    BROWSERBASE_CONTEXT_ID=...    # reuse an existing Browserbase context
    BROWSERBASE_CONTEXT_PERSIST=true  # create/reuse a project context when true
    BROWSERBASE_ADVANCED_STEALTH=false
    BROWSERBASE_KEEP_ALIVE=true   # default true
    BROWSERBASE_SESSION_TIMEOUT=... (seconds, integer, max 21600 = 6h)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any, Dict, Optional

import requests

from agent.browser_provider import BrowserProvider
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

_CONTEXT_CACHE_LOCKS_GUARD = threading.Lock()
_CONTEXT_CACHE_LOCKS: Dict[str, threading.Lock] = {}
_CONTEXT_CACHE_FILENAME = "browserbase_contexts.json"


class BrowserbaseBrowserProvider(BrowserProvider):
    """Browserbase (https://browserbase.com) cloud browser backend.

    Direct credentials only — managed-Nous-gateway support lives on the
    Browser Use provider now.
    """

    @property
    def name(self) -> str:
        return "browserbase"

    @property
    def display_name(self) -> str:
        return "Browserbase"

    def is_available(self) -> bool:
        return self._get_config_or_none() is not None

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    def _get_config_or_none(self) -> Optional[Dict[str, Any]]:
        api_key = os.environ.get("BROWSERBASE_API_KEY")
        project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
        if api_key and project_id:
            return {
                "api_key": api_key,
                "project_id": project_id,
                "base_url": os.environ.get(
                    "BROWSERBASE_BASE_URL", "https://api.browserbase.com"
                ).rstrip("/"),
            }
        return None

    def _get_config(self) -> Dict[str, Any]:
        config = self._get_config_or_none()
        if config is None:
            raise ValueError(
                "Browserbase requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID "
                "environment variables."
            )
        return config

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _context_cache_path():
        return get_hermes_home() / "state" / _CONTEXT_CACHE_FILENAME

    def _load_cached_context_id(self, project_id: str) -> Optional[str]:
        path = self._context_cache_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Could not read Browserbase context cache %s: %s", path, exc)
            return None

        if not isinstance(data, dict):
            return None
        contexts = data.get("contexts")
        if not isinstance(contexts, dict):
            return None
        context_id = contexts.get(project_id)
        return str(context_id).strip() if context_id else None

    def _cache_context_id(self, project_id: str, context_id: str) -> None:
        path = self._context_cache_path()
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            contexts = data.get("contexts")
            if not isinstance(contexts, dict):
                contexts = {}
            contexts[project_id] = context_id
            atomic_json_write(path, {"contexts": contexts}, mode=0o600, sort_keys=True)
        except OSError as exc:
            logger.warning("Could not persist Browserbase context cache %s: %s", path, exc)

    @staticmethod
    def _context_cache_lock(project_id: str) -> threading.Lock:
        """Return a per-project single-flight lock for context creation.

        The cache write is atomic, so separate processes cannot corrupt the
        JSON file, but this in-process lock cannot prevent two gateway
        processes from creating one context each before either writes. Shared
        multi-process deployments should set ``BROWSERBASE_CONTEXT_ID``
        explicitly to avoid that harmless orphan-context race.
        """
        with _CONTEXT_CACHE_LOCKS_GUARD:
            return _CONTEXT_CACHE_LOCKS.setdefault(project_id, threading.Lock())

    def _resolve_context_id(
        self,
        config: Dict[str, Any],
        headers: Dict[str, str],
        persist: bool,
        explicit_id: str,
    ) -> Optional[str]:
        if explicit_id:
            return explicit_id
        if not persist:
            return None

        project_id = str(config["project_id"])
        # Context creation must remain single-flight per project when several
        # delegated browser tasks start at the same time. Other projects no
        # longer wait behind this 30-second network boundary.
        with self._context_cache_lock(project_id):
            cached_id = self._load_cached_context_id(project_id)
            if cached_id:
                return cached_id
            try:
                response = requests.post(
                    f"{config['base_url']}/v1/contexts",
                    headers=headers,
                    json={"projectId": project_id},
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"Browserbase context API connection failed: {exc}"
                ) from exc
            if not response.ok:
                raise RuntimeError(
                    "Failed to create Browserbase context: "
                    f"{response.status_code} {response.text}"
                )
            context_id = str(response.json()["id"])
            self._cache_context_id(project_id, context_id)
            return context_id

    def create_session(self, task_id: str) -> Dict[str, object]:
        config = self._get_config()

        # Optional env-var knobs
        enable_proxies = os.environ.get("BROWSERBASE_PROXIES", "true").lower() != "false"
        enable_advanced_stealth = (
            os.environ.get("BROWSERBASE_ADVANCED_STEALTH", "false").lower() == "true"
        )
        enable_keep_alive = (
            os.environ.get("BROWSERBASE_KEEP_ALIVE", "true").lower() != "false"
        )
        custom_timeout_ms = os.environ.get("BROWSERBASE_SESSION_TIMEOUT")
        proxy_country = os.environ.get("BROWSERBASE_PROXY_COUNTRY", "").strip()
        proxy_state = os.environ.get("BROWSERBASE_PROXY_STATE", "").strip()
        proxy_city = os.environ.get("BROWSERBASE_PROXY_CITY", "").strip()
        region = os.environ.get("BROWSERBASE_REGION", "").strip()
        context_id_from_env = os.environ.get("BROWSERBASE_CONTEXT_ID", "").strip()
        context_persist_raw = os.environ.get("BROWSERBASE_CONTEXT_PERSIST")
        context_persist = (
            context_persist_raw.lower() != "false"
            if context_persist_raw is not None
            else bool(context_id_from_env)
        )
        context_requested = bool(context_id_from_env) or context_persist

        features_enabled = {
            "basic_stealth": True,
            "proxies": False,
            "advanced_stealth": False,
            "keep_alive": False,
            "custom_timeout": False,
            "persistent_context": False,
            "regional_proxy": False,
            "region": False,
        }

        session_config: Dict[str, object] = {"projectId": config["project_id"]}

        if enable_keep_alive:
            session_config["keepAlive"] = True

        if custom_timeout_ms:
            try:
                timeout_val = int(custom_timeout_ms)
                if timeout_val > 0:
                    session_config["timeout"] = timeout_val
            except ValueError:
                logger.warning(
                    "Invalid BROWSERBASE_SESSION_TIMEOUT value: %s", custom_timeout_ms
                )

        headers = {
            "Content-Type": "application/json",
            "X-BB-API-Key": config["api_key"],
        }

        if enable_proxies:
            if proxy_country:
                geolocation = {"country": proxy_country}
                if proxy_state:
                    geolocation["state"] = proxy_state
                if proxy_city:
                    geolocation["city"] = proxy_city
                session_config["proxies"] = [
                    {"type": "browserbase", "geolocation": geolocation}
                ]
            else:
                if proxy_state or proxy_city:
                    logger.warning(
                        "BROWSERBASE_PROXY_STATE/CITY require "
                        "BROWSERBASE_PROXY_COUNTRY; using the default proxy"
                    )
                session_config["proxies"] = True

        if region:
            session_config["region"] = region

        browser_settings: Dict[str, object] = {}
        if enable_advanced_stealth:
            browser_settings["advancedStealth"] = True
        if context_requested:
            context_id = self._resolve_context_id(
                config,
                headers,
                context_persist,
                context_id_from_env,
            )
            if context_id:
                browser_settings["context"] = {
                    "id": context_id,
                    "persist": context_persist,
                }
        if browser_settings:
            session_config["browserSettings"] = browser_settings

        # --- Create session via API ---

        try:
            response = requests.post(
                f"{config['base_url']}/v1/sessions",
                headers=headers,
                json=session_config,
                timeout=30,
            )

            proxies_fallback = False
            keepalive_fallback = False

            # Handle 402 — paid features unavailable
            if response.status_code == 402:
                if enable_keep_alive:
                    keepalive_fallback = True
                    logger.warning(
                        "keepAlive may require paid plan (402), retrying without it. "
                        "Sessions may timeout during long operations."
                    )
                    session_config.pop("keepAlive", None)
                    response = requests.post(
                        f"{config['base_url']}/v1/sessions",
                        headers=headers,
                        json=session_config,
                        timeout=30,
                    )

                if response.status_code == 402 and enable_proxies:
                    proxies_fallback = True
                    logger.warning(
                        "Proxies unavailable (402), retrying without proxies. "
                        "Bot detection may be less effective."
                    )
                    session_config.pop("proxies", None)
                    response = requests.post(
                        f"{config['base_url']}/v1/sessions",
                        headers=headers,
                        json=session_config,
                        timeout=30,
                    )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Browserbase API connection failed: {exc}"
            ) from exc

        if not response.ok:
            raise RuntimeError(
                f"Failed to create Browserbase session: "
                f"{response.status_code} {response.text}"
            )

        session_data = response.json()
        session_name = f"hermes_{task_id}_{uuid.uuid4().hex[:8]}"

        if enable_proxies and not proxies_fallback:
            features_enabled["proxies"] = True
        if enable_advanced_stealth:
            features_enabled["advanced_stealth"] = True
        if enable_keep_alive and not keepalive_fallback:
            features_enabled["keep_alive"] = True
        if custom_timeout_ms and "timeout" in session_config:
            features_enabled["custom_timeout"] = True
        if context_requested and "context" in browser_settings:
            features_enabled["persistent_context"] = context_persist
        if proxy_country and enable_proxies and not proxies_fallback:
            features_enabled["regional_proxy"] = True
        if region:
            features_enabled["region"] = True

        feature_str = ", ".join(k for k, v in features_enabled.items() if v)
        logger.info(
            "Created Browserbase session %s with features: %s", session_name, feature_str
        )

        return {
            "session_name": session_name,
            "bb_session_id": session_data["id"],
            "cdp_url": session_data["connectUrl"],
            "features": features_enabled,
        }

    def close_session(self, session_id: str) -> bool:
        try:
            config = self._get_config()
        except ValueError:
            logger.warning(
                "Cannot close Browserbase session %s — missing credentials", session_id
            )
            return False

        try:
            response = requests.post(
                f"{config['base_url']}/v1/sessions/{session_id}",
                headers={
                    "X-BB-API-Key": config["api_key"],
                    "Content-Type": "application/json",
                },
                json={
                    "projectId": config["project_id"],
                    "status": "REQUEST_RELEASE",
                },
                timeout=10,
            )
            if response.status_code in {200, 201, 204}:
                logger.debug("Successfully closed Browserbase session %s", session_id)
                return True
            else:
                logger.warning(
                    "Failed to close session %s: HTTP %s - %s",
                    session_id,
                    response.status_code,
                    response.text[:200],
                )
                return False
        except Exception as e:
            logger.error("Exception closing Browserbase session %s: %s", session_id, e)
            return False

    def emergency_cleanup(self, session_id: str) -> None:
        config = self._get_config_or_none()
        if config is None:
            logger.warning(
                "Cannot emergency-cleanup Browserbase session %s — missing credentials",
                session_id,
            )
            return
        try:
            requests.post(
                f"{config['base_url']}/v1/sessions/{session_id}",
                headers={
                    "X-BB-API-Key": config["api_key"],
                    "Content-Type": "application/json",
                },
                json={
                    "projectId": config["project_id"],
                    "status": "REQUEST_RELEASE",
                },
                timeout=5,
            )
        except Exception as e:
            logger.debug(
                "Emergency cleanup failed for Browserbase session %s: %s", session_id, e
            )

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Browserbase",
            "badge": "paid",
            "tag": "Cloud browser with stealth and proxies",
            "env_vars": [
                {
                    "key": "BROWSERBASE_API_KEY",
                    "prompt": "Browserbase API key",
                    "url": "https://browserbase.com",
                },
                {
                    "key": "BROWSERBASE_PROJECT_ID",
                    "prompt": "Browserbase project ID",
                },
            ],
            "post_setup": "agent_browser",
        }
