"""Authentication helpers for workshop ingress and outbound wake calls."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
import re
from typing import Mapping


_INBOUND_KEY_RE = re.compile(r"^[0-9a-fA-F]{64,}$")


class WorkshopAuthConfigurationError(RuntimeError):
    pass


def load_workshop_api_key(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    key = str(source.get("WORKSHOP_API_KEY") or "").strip()
    if not _INBOUND_KEY_RE.fullmatch(key):
        raise WorkshopAuthConfigurationError(
            "WORKSHOP_API_KEY must contain at least 64 hexadecimal characters"
        )
    return key


def load_workshop_wake_token(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    token = str(source.get("WORKSHOP_WAKE_TOKEN") or "").strip()
    if not token:
        raise WorkshopAuthConfigurationError("WORKSHOP_WAKE_TOKEN is required")
    return token


def load_workshop_credentials(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Load the direction-specific credentials and enforce separation."""
    api_key = load_workshop_api_key(env)
    wake_token = load_workshop_wake_token(env)
    if hmac.compare_digest(api_key, wake_token):
        raise WorkshopAuthConfigurationError(
            "WORKSHOP_API_KEY and WORKSHOP_WAKE_TOKEN must be distinct"
        )
    return api_key, wake_token


def bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@dataclass(frozen=True)
class WorkshopAuthenticator:
    """Constant-time verifier scoped only to workshop HTTP handlers."""

    api_key: str

    @classmethod
    def from_environment(cls) -> "WorkshopAuthenticator":
        return cls(load_workshop_api_key())

    def authorized(self, authorization: str | None) -> bool:
        supplied = bearer_token(authorization)
        if supplied is None:
            # Compare a fixed dummy of the same length to keep the missing/bad
            # header path from becoming an obvious oracle.
            supplied = "0" * len(self.api_key)
        return hmac.compare_digest(supplied, self.api_key)
