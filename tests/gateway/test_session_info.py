"""Tests for GatewayRunner._format_session_info — session config surfacing."""

import pytest
from unittest.mock import patch

from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture()
def runner():
    """Create a bare GatewayRunner without __init__."""
    instance = GatewayRunner.__new__(GatewayRunner)
    instance.config = GatewayConfig()
    instance._session_model_overrides = {}
    return instance


def _patch_info(tmp_path, config_yaml, model, runtime):
    """Return a context-manager stack that patches _format_session_info deps."""
    cfg_path = tmp_path / "config.yaml"
    if config_yaml is not None:
        cfg_path.write_text(config_yaml)
    return (
        patch("gateway.run._hermes_home", tmp_path),
        patch("gateway.run._resolve_gateway_model", return_value=model),
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime),
    )


class TestFormatSessionInfo:

    def test_includes_model_name(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: anthropic/claude-opus-4.6\n  provider: openrouter\n",
                                  "anthropic/claude-opus-4.6",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "claude-opus-4.6" in info


    def test_config_context_length(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 32768\n",
                                  "test-model",
                                  {"provider": "custom", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "32K" in info
        assert "config" in info

    def test_default_fallback_hint(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: unknown-model-xyz\n",
                                  "unknown-model-xyz",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info

    def test_local_endpoint_shown(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "localhost:11434" in info
        assert "8K" in info

    def test_named_custom_provider_keeps_context_pin_without_model_base_url(
        self, runner, tmp_path
    ):
        """Session-reset banner must honor model.context_length for named custom providers.

        Repro: /status shows 262144 from config while the reset banner said
        ``131K tokens (detected)`` because empty model.base_url + runtime URL
        falsely cleared the pin and fell through to the Qwen family default.
        """
        model = "custom-local-agentw/Qwen-AgentWorld-35B-A3B-Q5_K_XL"
        config_yaml = (
            "model:\n"
            f"  default: {model}\n"
            "  provider: custom-local-agentw\n"
            "  context_length: 262144\n"
            "custom_providers:\n"
            "  - name: custom-local-agentw\n"
            "    base_url: http://127.0.0.1:8080/v1\n"
            "    models: {}\n"
        )
        p1, p2, p3 = _patch_info(
            tmp_path,
            config_yaml,
            model,
            {
                "provider": "custom-local-agentw",
                "base_url": "http://127.0.0.1:8080/v1",
                "api_key": "",
            },
        )
        with p1, p2, p3, patch(
            "hermes_cli.config.get_compatible_custom_providers",
            return_value=[
                {
                    "name": "custom-local-agentw",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "models": {},
                }
            ],
        ), patch(
            "agent.model_metadata.get_model_context_length",
            side_effect=lambda *args, **kwargs: (
                kwargs.get("config_context_length")
                if kwargs.get("config_context_length")
                else 131072
            ),
        ):
            info = runner._format_session_info()
        assert "262K" in info
        assert "config" in info
        assert "131K" not in info


class TestResetNoticeSessionInfo:
    """#59003: the auto-reset banner must report the serving profile's config,
    not the multiplexer's base config."""

    _RUNTIME = {"provider": "anthropic", "base_url": "", "api_key": ""}

    def _source(self):
        from gateway.config import Platform
        from gateway.session import SessionSource
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", user_id="u1",
            profile="planner",
        )

    def _homes(self, tmp_path):
        base = tmp_path / "base"
        profile = tmp_path / "profiles" / "planner"
        profile.mkdir(parents=True)
        base.mkdir()
        base.joinpath("config.yaml").write_text(
            "model:\n  default: base-model\n  provider: custom\n  context_length: 1000\n")
        profile.joinpath("config.yaml").write_text(
            "model:\n  default: profile-model\n  provider: anthropic\n  context_length: 2000\n")
        return base, profile

    def test_multiplex_uses_profile_config(self, runner, tmp_path):
        from types import SimpleNamespace
        base, profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=True)
        with patch("gateway.run._hermes_home", base), \
             patch.object(GatewayRunner, "_resolve_profile_home_for_source", return_value=profile), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source())
        assert "profile-model" in info
        assert "anthropic" in info
        assert "base-model" not in info

    def test_single_profile_uses_base_config(self, runner, tmp_path):
        from types import SimpleNamespace
        base, _profile = self._homes(tmp_path)
        runner.config = SimpleNamespace(multiplex_profiles=False)
        with patch("gateway.run._hermes_home", base), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source())
        assert "base-model" in info
        assert "profile-model" not in info


class TestResetNoticeEffectiveSessionModel:
    """Reset banners use the same model priority as the following agent turn."""

    _GLOBAL_RUNTIME = {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "global-key",
        "api_mode": "anthropic_messages",
    }
    _CHANNEL_RUNTIME = {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "channel-key",
        "api_mode": "chat_completions",
    }

    @staticmethod
    def _source():
        return SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-1",
            chat_type="channel",
            user_id="user-1",
        )

    @staticmethod
    def _write_config(tmp_path):
        tmp_path.joinpath("config.yaml").write_text(
            "model:\n"
            "  default: global/model\n"
            "  provider: anthropic\n"
            "  context_length: 1000000\n",
            encoding="utf-8",
        )

    def test_channel_override_model_and_context(self, runner, tmp_path):
        self._write_config(tmp_path)
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "channel-1": ChannelOverride(
                            model="channel/model",
                            provider="openrouter",
                        ),
                    },
                ),
            },
        )

        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._GLOBAL_RUNTIME), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs_for_provider",
                 return_value=self._CHANNEL_RUNTIME,
             ), \
             patch(
                 "agent.model_metadata.get_model_context_length",
                 return_value=372_000,
             ) as get_context:
            info = runner._reset_notice_session_info(self._source())

        assert "channel/model" in info
        assert "openrouter" in info
        assert "global/model" not in info
        assert "372K" in info
        assert "1.0M" not in info
        assert get_context.call_args.kwargs["config_context_length"] is None

    def test_global_default_without_override(self, runner, tmp_path):
        self._write_config(tmp_path)
        runner.config = GatewayConfig(
            platforms={Platform.DISCORD: PlatformConfig(enabled=True)},
        )

        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._GLOBAL_RUNTIME):
            info = runner._reset_notice_session_info(self._source())

        assert "global/model" in info
        assert "anthropic" in info
        assert "1.0M" in info

    def test_persisted_session_override_beats_channel(self, runner, tmp_path):
        self._write_config(tmp_path)
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "channel-1": ChannelOverride(
                            model="channel/model",
                            provider="openrouter",
                        ),
                    },
                ),
            },
        )

        class _PersistedOverrideStore:
            @staticmethod
            def get_model_override(session_key):
                assert session_key
                return {
                    "model": "session/model",
                    "provider": "anthropic",
                    "base_url": "https://api.anthropic.com",
                }

        runner.session_store = _PersistedOverrideStore()
        persisted_runtime = {
            **self._GLOBAL_RUNTIME,
            "api_key": "persisted-session-key",
        }

        with patch("gateway.run._hermes_home", tmp_path), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs_for_provider",
                 return_value=persisted_runtime,
             ), \
             patch(
                 "agent.model_metadata.get_model_context_length",
                 return_value=200_000,
             ):
            info = runner._reset_notice_session_info(self._source())

        assert "session/model" in info
        assert "channel/model" not in info
        assert "global/model" not in info

    def test_session_runtime_failure_reuses_precomputed_global_model(
        self,
        runner,
        tmp_path,
    ):
        self._write_config(tmp_path)
        runner.config = GatewayConfig(
            platforms={Platform.DISCORD: PlatformConfig(enabled=True)},
        )

        with patch("gateway.run._hermes_home", tmp_path), \
             patch(
                 "gateway.run._resolve_gateway_model",
                 return_value="global/model",
             ) as resolve_global, \
             patch.object(
                 runner,
                 "_resolve_session_agent_runtime",
                 side_effect=RuntimeError("credential refresh failed"),
             ), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs",
                 return_value=self._GLOBAL_RUNTIME,
             ):
            info = runner._reset_notice_session_info(self._source())

        assert "global/model" in info
        assert resolve_global.call_count == 1
