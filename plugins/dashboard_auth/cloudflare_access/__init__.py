"""Cloudflare Access request-auth provider for the Hermes dashboard.

Cloudflare Access performs login and policy evaluation at the edge.  Requests
that pass Access carry a signed application JWT in ``Cf-Access-Jwt-Assertion``;
this provider validates that assertion locally against the account JWKS and
maps its identity claims into the dashboard's ordinary ``Session`` shape.

Configuration (both values are non-secret and belong in config.yaml)::

    dashboard:
      cloudflare_access:
        team_domain: https://my-team.cloudflareaccess.com
        aud: 32eafc7626e974616deaf0dc3ce63d7bcbed58a2731e84d06bc3cdf1b53c4228

The plain ``Cf-Access-Authenticated-User-Email`` convenience header is never
trusted. Only the signed JWT establishes identity.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from typing import Any, Mapping, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    LoginStart,
    ProviderError,
    RefreshExpiredError,
    Session,
)

logger = logging.getLogger(__name__)

_JWKS_CACHE_SECONDS = 300
_CLOCK_SKEW_SECONDS = 60
_UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 30
LAST_SKIP_REASON: str = ""


def _normalise_team_domain(value: str) -> str:
    """Return a pinned HTTPS Cloudflare Access issuer without a trailing slash."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("team_domain is required")
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host.endswith(".cloudflareaccess.com")
        or host == "cloudflareaccess.com"
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError(
            "team_domain must be an HTTPS *.cloudflareaccess.com team domain"
        )
    return f"https://{host}"


class CloudflareAccessProvider(DashboardAuthProvider):
    """Validate Cloudflare Access application assertions on every request."""

    name = "cloudflare-access"
    display_name = "Cloudflare Access"
    supports_session = False
    supports_request_auth = True
    trusted_client_ip_header = "cf-connecting-ip"
    # Intercepted by Cloudflare on the protected application domain. Using the
    # application URL clears that cookie immediately and is preferable to the
    # team-domain logout for dashboard UX.
    logout_url = "/cdn-cgi/access/logout"

    def __init__(self, *, team_domain: str, aud: str) -> None:
        self._issuer = _normalise_team_domain(team_domain)
        self._aud = str(aud or "").strip()
        if not self._aud:
            raise ValueError("aud is required")
        self._jwks_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._jwks_client: Any = None
        self._jwks_lock = threading.Lock()
        self._last_unknown_kid_refresh = 0.0
        self._recent_signing_keys: dict[str, tuple[Any, float]] = {}

    def _get_jwks_client(self):
        if self._jwks_client is None:
            from jwt import PyJWKClient

            self._jwks_client = PyJWKClient(
                self._jwks_url,
                cache_keys=False,
                lifespan=_JWKS_CACHE_SECONDS,
                timeout=5,
            )
        return self._jwks_client

    def verify_request(self, *, headers: Mapping[str, str]) -> Optional[Session]:
        assertion = str(headers.get("cf-access-jwt-assertion", "") or "").strip()
        if not assertion:
            return None

        import jwt

        try:
            token_header = jwt.get_unverified_header(assertion)
            if token_header.get("alg") != "RS256":
                return None
            kid = str(token_header.get("kid", "") or "").strip()
            if not kid:
                return None
            signing_key = self._get_signing_key(kid)
            if signing_key is None:
                return None
        except jwt.InvalidTokenError:
            return None
        except jwt.PyJWKClientError as exc:
            # PyJWKClient refreshes the JWKS once when a kid is unknown. A
            # remaining "unable to find" result is an invalid token; fetch /
            # transport failures mean the origin cannot currently decide.
            if "unable to find a signing key" in str(exc).lower():
                return None
            raise ProviderError(f"Cloudflare Access JWKS lookup failed: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive network path
            raise ProviderError(
                f"Cloudflare Access JWKS lookup failed: {exc!r}"
            ) from exc

        try:
            claims = jwt.decode(
                assertion,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._aud,
                issuer=self._issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={
                    "require": ["aud", "exp", "iat", "iss", "sub", "type"],
                },
            )
        except jwt.InvalidTokenError:
            return None

        if claims.get("type") != "app":
            return None

        sub = str(claims.get("sub", "") or "").strip()
        email = str(claims.get("email", "") or "").strip()
        common_name = str(claims.get("common_name", "") or "").strip()
        # Human tokens carry sub + email. Service-token assertions explicitly
        # carry an empty sub and identify the caller by common_name.
        user_id = sub or common_name
        if not user_id or (not sub and not common_name):
            return None

        return Session(
            user_id=user_id,
            email=email,
            display_name=(email or ("Service token" if common_name else "")),
            org_id="",
            provider=self.name,
            expires_at=int(claims["exp"]),
            access_token=assertion,
            refresh_token="",
        )

    def _get_signing_key(self, kid: str):
        """Resolve ``kid`` with one refresh, rate-limited for unknown keys.

        PyJWKClient already refreshes once when a key is absent. The small
        global cooldown prevents a directly-reachable origin from being used
        as a JWKS-fetch amplifier by sending a stream of random key IDs. A
        legitimate rotation can be delayed by at most 30 seconds after such a
        miss; recently verified signing keys remain usable without network I/O.
        """
        import jwt

        client = self._get_jwks_client()
        with self._jwks_lock:
            now = time.monotonic()
            if (
                self._last_unknown_kid_refresh
                and now - self._last_unknown_kid_refresh
                < _UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS
            ):
                cached = self._recent_signing_keys.get(kid)
                if cached and now - cached[1] < _JWKS_CACHE_SECONDS:
                    return cached[0]
                return None
            try:
                signing_key = client.get_signing_key(kid)
                self._recent_signing_keys[kid] = (signing_key, now)
                return signing_key
            except jwt.PyJWKClientError as exc:
                if "unable to find a signing key" in str(exc).lower():
                    self._last_unknown_kid_refresh = now
                raise

    # Cloudflare owns login, refresh, and revocation. These methods satisfy the
    # common provider protocol but are excluded from all interactive loops by
    # supports_session=False.
    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError("Cloudflare Access performs login at the edge")

    def complete_login(self, *, code, state, code_verifier, redirect_uri) -> Session:
        raise NotImplementedError("Cloudflare Access performs login at the edge")

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise RefreshExpiredError("Cloudflare Access refreshes sessions at the edge")

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


def _load_settings() -> dict[str, str]:
    # Deliberately config-only: team_domain and aud are non-secret behavioural
    # settings. Hermes reserves ~/.hermes/.env and new HERMES_* variables for
    # credentials, so unlike secret-bearing providers there is no env override.
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        section = cfg_get(cfg, "dashboard", "cloudflare_access", default={})
    except Exception as exc:  # noqa: BLE001 - plugin registration must be safe
        logger.debug("dashboard-auth-cloudflare-access: config load failed: %s", exc)
        return {}
    if not isinstance(section, dict):
        return {}
    return {
        "team_domain": str(section.get("team_domain", "") or "").strip(),
        "aud": str(section.get("aud", "") or "").strip(),
    }


def register(ctx) -> None:
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""
    settings = _load_settings()
    team_domain = settings.get("team_domain", "")
    aud = settings.get("aud", "")
    if not team_domain or not aud:
        LAST_SKIP_REASON = (
            "Cloudflare Access dashboard auth is not configured. Set both "
            "dashboard.cloudflare_access.team_domain and "
            "dashboard.cloudflare_access.aud in config.yaml. "
            f"(team_domain set: {bool(team_domain)}; aud set: {bool(aud)})"
        )
        logger.debug("dashboard-auth-cloudflare-access: %s", LAST_SKIP_REASON)
        return

    try:
        provider = CloudflareAccessProvider(team_domain=team_domain, aud=aud)
    except ValueError as exc:
        LAST_SKIP_REASON = f"CloudflareAccessProvider construction failed: {exc}"
        logger.warning("dashboard-auth-cloudflare-access: %s", LAST_SKIP_REASON)
        return

    ctx.register_dashboard_auth_provider(provider)
    logger.info(
        "dashboard-auth-cloudflare-access: registered (issuer=%s, aud=%s…)",
        provider._issuer,
        aud[:8],
    )
