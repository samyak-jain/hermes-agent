"""Cloudflare Access provider and real dashboard-gate integration tests."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import plugins.dashboard_auth.cloudflare_access as cf_plugin
from hermes_cli import web_server
from hermes_cli.dashboard_auth import (
    ProviderError,
    assert_protocol_compliance,
    clear_providers,
    list_request_auth_providers,
    list_session_providers,
    register_provider,
)
from hermes_cli.dashboard_auth.client_ip import client_ip

ISSUER = "https://hermes-test.cloudflareaccess.com"
AUD = "test-access-application-audience"


@pytest.fixture(scope="module")
def rsa_keypair() -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {
        "private_pem": private_pem,
        "public_key": key.public_key(),
        "kid": "cf-test",
    }


def _token(keys, **overrides) -> str:
    now = int(time.time())
    claims = {
        "type": "app",
        "aud": [AUD],
        "exp": now + 900,
        "iat": now,
        "nbf": now,
        "iss": ISSUER,
        "sub": "user-123",
        "email": "user@example.com",
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        keys["private_pem"],
        algorithm="RS256",
        headers={"kid": keys["kid"]},
    )


def _provider(keys) -> cf_plugin.CloudflareAccessProvider:
    provider = cf_plugin.CloudflareAccessProvider(team_domain=ISSUER, aud=AUD)
    signing_key = MagicMock()
    signing_key.key = keys["public_key"]
    provider._jwks_client = MagicMock()
    provider._jwks_client.get_signing_key.return_value = signing_key
    return provider


def test_protocol_and_capabilities():
    assert assert_protocol_compliance(cf_plugin.CloudflareAccessProvider) is None
    provider = cf_plugin.CloudflareAccessProvider(
        team_domain="hermes-test.cloudflareaccess.com/", aud=AUD
    )
    assert provider.supports_request_auth is True
    assert provider.supports_session is False
    assert provider._jwks_url == f"{ISSUER}/cdn-cgi/access/certs"
    assert provider._get_jwks_client().timeout == 5


@pytest.mark.parametrize(
    "domain",
    [
        "http://team.cloudflareaccess.com",
        "https://evil.example.com",
        "https://team.cloudflareaccess.com/path",
        "https://team.cloudflareaccess.com:443",
    ],
)
def test_rejects_unpinned_team_domains(domain):
    with pytest.raises(ValueError, match="team_domain"):
        cf_plugin.CloudflareAccessProvider(team_domain=domain, aud=AUD)


def test_valid_human_assertion_maps_session(rsa_keypair):
    session = _provider(rsa_keypair).verify_request(
        headers={"cf-access-jwt-assertion": _token(rsa_keypair)}
    )
    assert session is not None
    assert session.user_id == "user-123"
    assert session.email == "user@example.com"
    assert session.provider == "cloudflare-access"
    assert session.refresh_token == ""


def test_service_token_uses_common_name(rsa_keypair):
    token = _token(rsa_keypair, sub="", email="", common_name="service-id.access")
    session = _provider(rsa_keypair).verify_request(
        headers={"cf-access-jwt-assertion": token}
    )
    assert session is not None
    assert session.user_id == "service-id.access"
    assert session.display_name == "Service token"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": ["another-app"]},
        {"iss": "https://other.cloudflareaccess.com"},
        {"exp": 1},
        {"type": "org"},
        {"sub": "", "email": ""},
    ],
)
def test_invalid_assertions_are_rejected(rsa_keypair, overrides):
    assert (
        _provider(rsa_keypair).verify_request(
            headers={"cf-access-jwt-assertion": _token(rsa_keypair, **overrides)}
        )
        is None
    )


def test_unsigned_email_header_is_never_trusted(rsa_keypair):
    assert (
        _provider(rsa_keypair).verify_request(
            headers={"cf-access-authenticated-user-email": "attacker@example.com"}
        )
        is None
    )


def test_jwks_outage_is_provider_error(rsa_keypair):
    provider = _provider(rsa_keypair)
    provider._jwks_client.get_signing_key.side_effect = OSError("offline")
    with pytest.raises(ProviderError, match="JWKS"):
        provider.verify_request(
            headers={"cf-access-jwt-assertion": _token(rsa_keypair)}
        )


def test_unknown_kid_refresh_has_cooldown(rsa_keypair):
    provider = _provider(rsa_keypair)
    provider._jwks_client.get_signing_key.side_effect = jwt.PyJWKClientError(
        'Unable to find a signing key that matches: "unknown"'
    )
    token = _token(rsa_keypair)

    assert provider.verify_request(headers={"cf-access-jwt-assertion": token}) is None
    assert provider.verify_request(headers={"cf-access-jwt-assertion": token}) is None
    provider._jwks_client.get_signing_key.assert_called_once()


def test_cf_connecting_ip_is_trusted_only_after_verified_assertion(rsa_keypair):
    request = MagicMock()
    request.state = SimpleNamespace()
    request.headers = {
        "cf-connecting-ip": "10.0.0.1",
        "x-forwarded-for": "203.0.113.9, 192.0.2.1",
    }
    request.client.host = "192.0.2.20"

    clear_providers()
    assert client_ip(request) == "203.0.113.9"
    try:
        provider = _provider(rsa_keypair)
        register_provider(provider)
        # Registration alone is insufficient: failed assertions must use the
        # ordinary peer/proxy chain so a direct origin request cannot spoof CF.
        assert client_ip(request) == "203.0.113.9"
        request.state.session = SimpleNamespace(provider=provider.name)
        assert client_ip(request) == "10.0.0.1"
    finally:
        clear_providers()


def test_registration_uses_config_only():
    ctx = MagicMock()
    with patch.object(
        cf_plugin,
        "_load_settings",
        return_value={"team_domain": ISSUER, "aud": AUD},
    ):
        cf_plugin.register(ctx)
    provider = ctx.register_dashboard_auth_provider.call_args.args[0]
    assert isinstance(provider, cf_plugin.CloudflareAccessProvider)
    assert cf_plugin.LAST_SKIP_REASON == ""


def test_registration_skip_reason_names_both_settings():
    ctx = MagicMock()
    with patch.object(cf_plugin, "_load_settings", return_value={}):
        cf_plugin.register(ctx)
    ctx.register_dashboard_auth_provider.assert_not_called()
    assert "team_domain" in cf_plugin.LAST_SKIP_REASON
    assert ".aud" in cf_plugin.LAST_SKIP_REASON


@pytest.fixture
def gated_client(rsa_keypair):
    clear_providers()
    provider = _provider(rsa_keypair)
    register_provider(provider)
    previous = {
        "host": getattr(web_server.app.state, "bound_host", None),
        "port": getattr(web_server.app.state, "bound_port", None),
        "required": getattr(web_server.app.state, "auth_required", None),
    }
    web_server.app.state.bound_host = "dashboard.example.com"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    yield TestClient(web_server.app, base_url="https://dashboard.example.com")
    clear_providers()
    web_server.app.state.bound_host = previous["host"]
    web_server.app.state.bound_port = previous["port"]
    web_server.app.state.auth_required = previous["required"]


def test_request_provider_registry_is_noninteractive(rsa_keypair):
    clear_providers()
    try:
        register_provider(_provider(rsa_keypair))
        assert [p.name for p in list_request_auth_providers()] == ["cloudflare-access"]
        assert list_session_providers() == []
    finally:
        clear_providers()


def test_real_gate_authenticates_header_and_exposes_logout(gated_client, rsa_keypair):
    response = gated_client.get(
        "/api/auth/me",
        headers={
            "Cf-Access-Jwt-Assertion": _token(rsa_keypair),
            "Cf-Access-Authenticated-User-Email": "spoofed@example.com",
        },
    )
    assert response.status_code == 200
    assert response.json()["email"] == "user@example.com"
    assert response.json()["logout_url"] == "/cdn-cgi/access/logout"


def test_login_redirects_authenticated_edge_user(gated_client, rsa_keypair):
    response = gated_client.get(
        "/login",
        headers={"Cf-Access-Jwt-Assertion": _token(rsa_keypair)},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_login_fails_closed_without_edge_assertion(gated_client):
    response = gated_client.get("/login", follow_redirects=False)
    assert response.status_code == 401
    assert "no auth providers" not in response.text.lower()


def test_real_gate_fails_closed_without_assertion_for_api_and_html(gated_client):
    api_response = gated_client.get("/api/auth/me")
    assert api_response.status_code == 401
    assert api_response.json()["reason"] == "invalid_request_assertion"
    html_response = gated_client.get("/", follow_redirects=False)
    assert html_response.status_code == 401
    assert "location" not in html_response.headers
