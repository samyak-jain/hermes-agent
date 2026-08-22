from __future__ import annotations

import pytest

from plugins.platforms.workshop.auth import (
    WorkshopAuthConfigurationError,
    WorkshopAuthenticator,
    load_workshop_credentials,
    load_workshop_api_key,
    load_workshop_wake_token,
)


def test_inbound_key_requires_at_least_64_hex_characters():
    key = "ab" * 32
    assert load_workshop_api_key({"WORKSHOP_API_KEY": key}) == key

    for invalid in ("", "a" * 63, "z" * 64, "sk-" + "a" * 64):
        with pytest.raises(WorkshopAuthConfigurationError):
            load_workshop_api_key({"WORKSHOP_API_KEY": invalid})


def test_wake_token_is_distinct_required_secret():
    with pytest.raises(WorkshopAuthConfigurationError):
        load_workshop_wake_token({})
    assert load_workshop_wake_token({"WORKSHOP_WAKE_TOKEN": "wake-secret"}) == "wake-secret"

    same = "ab" * 32
    with pytest.raises(WorkshopAuthConfigurationError):
        load_workshop_credentials(
            {"WORKSHOP_API_KEY": same, "WORKSHOP_WAKE_TOKEN": same}
        )


def test_authenticator_accepts_only_its_bearer():
    key = "c" * 64
    auth = WorkshopAuthenticator(key)

    assert auth.authorized(f"Bearer {key}") is True
    assert auth.authorized(f"bearer {key}") is True
    assert auth.authorized("Bearer " + "d" * 64) is False
    assert auth.authorized(None) is False


def test_non_ascii_bearer_is_a_normal_authentication_failure():
    auth = WorkshopAuthenticator("a" * 64)
    assert auth.authorized("Bearer " + "é" * 64) is False
