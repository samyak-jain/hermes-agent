import hermes_cli.main as main_mod
import hermes_cli.auth as auth_mod
import hermes_cli.config as config_mod


def test_has_any_provider_configured_detects_oauth_provider_status(monkeypatch):
    monkeypatch.setattr(config_mod, "load_config", lambda: {"model": ""})
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG", {"model": ""}, raising=False)
    monkeypatch.setattr(main_mod.os, "getenv", lambda _k, _d="": "")

    class DummyPath:
        def exists(self):
            return False

    monkeypatch.setattr(config_mod, "get_env_path", lambda: DummyPath())
    monkeypatch.setattr(config_mod, "get_hermes_home", lambda: DummyPath())

    class DummyProvider:
        def __init__(self, auth_type):
            self.auth_type = auth_type
            self.api_key_env_vars = []

    monkeypatch.setattr(
        auth_mod,
        "PROVIDER_REGISTRY",
        {
            "openai-codex": DummyProvider("oauth_device_code"),
            "openrouter": DummyProvider("api_key"),
        },
    )

    def fake_get_auth_status(provider_id=None):
        return {"logged_in": provider_id == "openai-codex"}

    monkeypatch.setattr(auth_mod, "get_auth_status", fake_get_auth_status)

    assert main_mod._has_any_provider_configured() is True
