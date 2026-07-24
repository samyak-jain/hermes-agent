"""Behavior tests for Browserbase session feature configuration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider


def _response(payload: dict, status_code: int = 201):
    return SimpleNamespace(
        ok=200 <= status_code < 300,
        status_code=status_code,
        text=json.dumps(payload),
        json=lambda: payload,
    )


def _configure(monkeypatch, tmp_path, **overrides: str) -> None:
    values = {
        "HERMES_HOME": str(tmp_path),
        "BROWSERBASE_API_KEY": "test-key",
        "BROWSERBASE_PROJECT_ID": "project-1",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_existing_context_merges_with_stealth_proxy_location_and_region(
    monkeypatch, tmp_path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        BROWSERBASE_CONTEXT_ID="context-existing",
        BROWSERBASE_ADVANCED_STEALTH="true",
        BROWSERBASE_PROXY_COUNTRY="US",
        BROWSERBASE_PROXY_STATE="NY",
        BROWSERBASE_PROXY_CITY="NEW_YORK",
        BROWSERBASE_REGION="us-east-1",
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _response({"id": "session-1", "connectUrl": "wss://session"})

    monkeypatch.setattr(
        "plugins.browser.browserbase.provider.requests.post", fake_post
    )

    result = BrowserbaseBrowserProvider().create_session("task")

    assert len(calls) == 1
    session_config = calls[0][1]["json"]
    assert session_config["browserSettings"] == {
        "advancedStealth": True,
        "context": {"id": "context-existing", "persist": True},
    }
    assert session_config["proxies"] == [
        {
            "type": "browserbase",
            "geolocation": {
                "country": "US",
                "state": "NY",
                "city": "NEW_YORK",
            },
        }
    ]
    assert session_config["region"] == "us-east-1"
    assert result["features"]["persistent_context"] is True
    assert result["features"]["regional_proxy"] is True
    assert result["features"]["region"] is True


def test_context_resolver_uses_already_resolved_explicit_id(
    monkeypatch,
    tmp_path,
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        BROWSERBASE_CONTEXT_ID="environment-value",
    )
    provider = BrowserbaseBrowserProvider()
    config = provider._get_config()

    assert provider._resolve_context_id(
        config,
        {"X-BB-API-Key": "test-key"},
        persist=True,
        explicit_id="resolved-once",
    ) == "resolved-once"


def test_persistent_context_is_created_once_and_reused_from_profile_cache(
    monkeypatch, tmp_path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        BROWSERBASE_CONTEXT_PERSIST="true",
        BROWSERBASE_PROXIES="false",
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/v1/contexts"):
            return _response({"id": "context-created"})
        number = sum(call_url.endswith("/v1/sessions") for call_url, _ in calls)
        return _response(
            {"id": f"session-{number}", "connectUrl": f"wss://session-{number}"}
        )

    monkeypatch.setattr(
        "plugins.browser.browserbase.provider.requests.post", fake_post
    )
    provider = BrowserbaseBrowserProvider()

    first = provider.create_session("first")
    provider.close_session(first["bb_session_id"])
    second = provider.create_session("second")
    provider.close_session(second["bb_session_id"])

    context_calls = [call for call in calls if call[0].endswith("/v1/contexts")]
    session_calls = [call for call in calls if call[0].endswith("/v1/sessions")]
    assert len(context_calls) == 1
    assert context_calls[0][1]["json"] == {"projectId": "project-1"}
    assert len(session_calls) == 2
    for _, kwargs in session_calls:
        assert kwargs["json"]["browserSettings"]["context"] == {
            "id": "context-created",
            "persist": True,
        }

    cache = json.loads(
        (tmp_path / "state" / "browserbase_contexts.json").read_text()
    )
    assert cache == {"contexts": {"project-1": "context-created"}}


def test_paid_proxy_fallback_removes_entire_regional_proxy_config(
    monkeypatch, tmp_path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        BROWSERBASE_KEEP_ALIVE="false",
        BROWSERBASE_PROXY_COUNTRY="GB",
    )
    session_payloads = []

    def fake_post(url, **kwargs):
        session_payloads.append(dict(kwargs["json"]))
        if len(session_payloads) == 1:
            return _response({"error": "payment required"}, status_code=402)
        return _response({"id": "session-1", "connectUrl": "wss://session"})

    monkeypatch.setattr(
        "plugins.browser.browserbase.provider.requests.post", fake_post
    )

    result = BrowserbaseBrowserProvider().create_session("task")

    assert "proxies" in session_payloads[0]
    assert "proxies" not in session_payloads[1]
    assert result["features"]["proxies"] is False
    assert result["features"]["regional_proxy"] is False


def test_required_paid_features_fail_loudly_on_402(
    monkeypatch, tmp_path
) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        BrowserbaseBrowserProvider, "_require_paid_features", lambda self: True
    )
    monkeypatch.setattr(
        "plugins.browser.browserbase.provider.requests.post",
        lambda *args, **kwargs: _response(
            {"error": "payment required"}, status_code=402
        ),
    )

    with pytest.raises(RuntimeError, match="expected paid Session feature"):
        BrowserbaseBrowserProvider().create_session("task")


def test_corrupt_context_cache_fails_without_creating_new_identity(
    monkeypatch, tmp_path
) -> None:
    _configure(
        monkeypatch,
        tmp_path,
        BROWSERBASE_CONTEXT_PERSIST="true",
        BROWSERBASE_PROXIES="false",
    )
    cache_path = tmp_path / "state" / "browserbase_contexts.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{broken", encoding="utf-8")
    post_calls = []
    monkeypatch.setattr(
        "plugins.browser.browserbase.provider.requests.post",
        lambda *args, **kwargs: post_calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="context cache is unreadable"):
        BrowserbaseBrowserProvider().create_session("task")

    assert post_calls == []
