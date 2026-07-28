"""Tests for per-channel model and system prompt overrides (Fixes #1955)."""

from unittest.mock import patch

import pytest

from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
)
from gateway.run import _get_channel_override, _resolve_tool_policy_for_source, GatewayRunner
from gateway.session import SessionSource


def test_profile_model_override_routes_multiplexed_agent_runtime():
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner.config = GatewayConfig()
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="roundtable",
        chat_type="group",
        user_id="operator",
        profile="vegapunk",
    )
    user_config = {
        "model": {
            "default": "claude-fable-5",
            "provider": "anthropic",
        },
        "agent": {
            "profile_models": {
                "vegapunk": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "api_mode": "codex_responses",
                }
            }
        },
    }
    with patch(
        "gateway.run._resolve_gateway_model",
        return_value="claude-fable-5",
    ), patch(
        "gateway.run._resolve_runtime_agent_kwargs",
        return_value={
            "provider": "anthropic",
            "api_key": "claude-oauth",
            "api_mode": "chat_completions",
        },
    ), patch(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        return_value={
            "provider": "openai-codex",
            "credential_pool": object(),
            "api_mode": "codex_responses",
        },
    ):
        model, runtime = runner._resolve_session_agent_runtime(
            source=source,
            user_config=user_config,
        )

    assert model == "gpt-5.6-sol"
    assert runtime["provider"] == "openai-codex"
    assert runtime["api_mode"] == "codex_responses"


class TestGetChannelOverride:


    def test_no_override_when_channel_not_in_overrides(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "999": ChannelOverride(model="openrouter/healer-alpha"),
                    },
                ),
            },
        )
        assert _get_channel_override(config, Platform.DISCORD, "123") is None

    def test_returns_override_when_channel_matches(self):
        ov = ChannelOverride(
            model="openrouter/healer-alpha",
            provider="openrouter",
            system_prompt="You are a summarizer.",
            profile_tool_policies={
                "operator": {"mode": "denylist", "tools": ["delegate_task"]}
            },
        )
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={"1234567890": ov},
                ),
            },
        )
        result = _get_channel_override(config, Platform.DISCORD, "1234567890")
        assert result is not None
        assert result.model == "openrouter/healer-alpha"
        assert result.provider == "openrouter"
        assert result.system_prompt == "You are a summarizer."
        assert result.profile_tool_policies == {
            "operator": {"mode": "denylist", "tools": ["delegate_task"]}
        }


    def test_thread_id_lookup_when_chat_id_misses(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "thread_99": ChannelOverride(model="topic-model"),
                    },
                ),
            },
        )
        result = _get_channel_override(
            config, Platform.DISCORD, "parent_chan", thread_id="thread_99"
        )
        assert result is not None
        assert result.model == "topic-model"


class TestChannelToolPolicy:
    def _config(self):
        return {
            "agent": {
                "tool_policy": {
                    "mode": "allowlist",
                    "tools": ["clarify", "delegate_task", "memory", "skills_list", "skill_manage"],
                    "gateway_override_authority": "managed_only",
                }
            },
            "platforms": {
                "discord": {
                    "channel_overrides": {
                        "trusted": {"tool_policy": {"mode": "unrestricted"}}
                    }
                }
            },
        }

    def test_dm_and_unrelated_channel_are_restricted(self):
        cfg = self._config()
        for source in (
            SessionSource(platform=Platform.DISCORD, chat_id="dm", chat_type="dm"),
            SessionSource(platform=Platform.DISCORD, chat_id="other", chat_type="group"),
            SessionSource(
                platform=Platform.DISCORD,
                chat_id="other-thread",
                thread_id="other-thread",
                parent_chat_id="other",
                chat_type="thread",
            ),
        ):
            policy = _resolve_tool_policy_for_source(cfg, source)
            assert policy.mode == "allowlist"
            assert not policy.allows("terminal")

    def test_managed_trusted_channel_is_unrestricted(self, monkeypatch):
        cfg = self._config()
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: path == "platforms.discord.channel_overrides.trusted.tool_policy.mode",
        )
        source = SessionSource(platform=Platform.DISCORD, chat_id="trusted", chat_type="group")
        assert _resolve_tool_policy_for_source(cfg, source).mode == "unrestricted"

    def test_trusted_parent_is_inherited_by_thread(self, monkeypatch):
        cfg = self._config()
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: "channel_overrides.trusted.tool_policy.mode" in path,
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread-1",
            thread_id="thread-1",
            parent_chat_id="trusted",
            chat_type="thread",
        )
        assert _resolve_tool_policy_for_source(cfg, source).mode == "unrestricted"

    def test_unmanaged_override_cannot_elevate(self, monkeypatch):
        cfg = self._config()
        monkeypatch.setattr("hermes_cli.managed_scope.is_key_managed", lambda _path: False)
        source = SessionSource(platform=Platform.DISCORD, chat_id="trusted")
        assert _resolve_tool_policy_for_source(cfg, source).mode == "allowlist"

    def test_managed_profile_policy_applies_without_channel_override(
        self, monkeypatch
    ):
        cfg = self._config()
        cfg["agent"]["profile_tool_policies"] = {
            "vegapunk": {
                "mode": "denylist",
                "tools": ["delegate_task"],
            }
        }
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: "profile_tool_policies.vegapunk" in path,
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="shared-room",
            profile="vegapunk",
        )

        policy = _resolve_tool_policy_for_source(cfg, source)

        assert policy.mode == "denylist"
        assert policy.allows("terminal")
        assert not policy.allows("delegate_task")

    def test_managed_channel_profile_policy_preserves_profile_authority(
        self, monkeypatch
    ):
        cfg = self._config()
        cfg["agent"]["profile_tool_policies"] = {
            "vegapunk": {
                "mode": "denylist",
                "tools": ["delegate_task"],
            }
        }
        trusted = cfg["platforms"]["discord"]["channel_overrides"]["trusted"]
        trusted["tool_policy"] = {
            "mode": "allowlist",
            "tools": ["memory", "cronjob"],
        }
        trusted["profile_tool_policies"] = {
            "vegapunk": {
                "mode": "denylist",
                "tools": ["delegate_task"],
            }
        }
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: (
                "profile_tool_policies.vegapunk" in path
                or "channel_overrides.trusted.tool_policy" in path
            ),
        )

        lena_trusted = SessionSource(
            platform=Platform.DISCORD,
            chat_id="trusted",
            chat_type="group",
            profile="default",
        )
        lena_other = SessionSource(
            platform=Platform.DISCORD,
            chat_id="other",
            chat_type="group",
            profile="default",
        )
        vegapunk_trusted = SessionSource(
            platform=Platform.DISCORD,
            chat_id="trusted",
            chat_type="group",
            profile="vegapunk",
        )
        vegapunk_other = SessionSource(
            platform=Platform.DISCORD,
            chat_id="other",
            chat_type="group",
            profile="vegapunk",
        )

        lena_trusted_policy = _resolve_tool_policy_for_source(cfg, lena_trusted)
        lena_other_policy = _resolve_tool_policy_for_source(cfg, lena_other)
        vegapunk_trusted_policy = _resolve_tool_policy_for_source(
            cfg, vegapunk_trusted
        )
        vegapunk_other_policy = _resolve_tool_policy_for_source(
            cfg, vegapunk_other
        )

        assert lena_trusted_policy.allows("cronjob")
        assert not lena_trusted_policy.allows("terminal")
        assert not lena_other_policy.allows("cronjob")
        assert vegapunk_trusted_policy.allows("cronjob")
        assert vegapunk_trusted_policy.allows("terminal")
        assert not vegapunk_trusted_policy.allows("delegate_task")
        assert vegapunk_other_policy.allows("cronjob")
        assert vegapunk_other_policy.allows("terminal")
        assert not vegapunk_other_policy.allows("delegate_task")

    def test_unmanaged_channel_profile_policy_cannot_elevate(
        self, monkeypatch
    ):
        cfg = self._config()
        cfg["platforms"]["discord"]["channel_overrides"]["trusted"][
            "profile_tool_policies"
        ] = {
            "vegapunk": {
                "mode": "denylist",
                "tools": ["delegate_task"],
            }
        }
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: "channel_overrides.trusted.tool_policy" in path,
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="trusted",
            profile="vegapunk",
        )

        policy = _resolve_tool_policy_for_source(cfg, source)

        assert policy.mode == "allowlist"
        assert not policy.allows("terminal")

    def test_invalid_channel_policy_falls_back_to_restricted(self, monkeypatch):
        cfg = self._config()
        cfg["platforms"]["discord"]["channel_overrides"]["trusted"]["tool_policy"] = {
            "mode": "unrestricted",
            "tools": ["terminal"],
        }
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: "channel_overrides.trusted.tool_policy" in path,
        )
        source = SessionSource(platform=Platform.DISCORD, chat_id="trusted")
        policy = _resolve_tool_policy_for_source(cfg, source)
        assert policy.mode == "allowlist"
        assert not policy.allows("terminal")

    def test_typed_channel_override_field_drives_resolution(self, monkeypatch):
        cfg = {
            "agent": {
                "tool_policy": {
                    "mode": "allowlist",
                    "tools": ["memory"],
                    "gateway_override_authority": "managed_only",
                }
            }
        }
        gateway_config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "trusted": ChannelOverride(
                            tool_policy={"mode": "unrestricted"},
                        ),
                    },
                ),
            },
        )
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda path: "channel_overrides.trusted.tool_policy" in path,
        )

        source = SessionSource(platform=Platform.DISCORD, chat_id="trusted")
        policy = _resolve_tool_policy_for_source(cfg, source, gateway_config)

        assert policy.mode == "unrestricted"

    def test_non_mapping_global_policy_defaults_to_managed_only(self, monkeypatch):
        cfg = {"agent": {"tool_policy": "allowlist"}}
        gateway_config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "trusted": ChannelOverride(
                            tool_policy={"mode": "unrestricted"},
                        ),
                    },
                ),
            },
        )
        monkeypatch.setattr(
            "hermes_cli.managed_scope.is_key_managed",
            lambda _path: False,
        )

        source = SessionSource(platform=Platform.DISCORD, chat_id="trusted")
        policy = _resolve_tool_policy_for_source(cfg, source, gateway_config)

        assert not policy.valid
        assert not policy.allows("terminal")

    def test_dm_model_switch_or_same_gpt_model_never_changes_policy(self):
        cfg = self._config()
        source = SessionSource(platform=Platform.DISCORD, chat_id="dm", chat_type="dm")
        cfg["model"] = {"default": "gpt-model"}
        # /model is stored independently by the runner and never enters this
        # source-authority resolver; model identity cannot elevate a DM.
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._session_model_overrides = {"discord:dm": {"model": "gpt-model"}}
        assert _resolve_tool_policy_for_source(cfg, source).mode == "allowlist"


class TestResolveModelForChannel:
    def test_uses_channel_override_when_present(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(model="anthropic/claude-opus-4.6"),
                    },
                ),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        model = runner._resolve_model_for_channel(Platform.DISCORD, "chan_1")
        assert model == "anthropic/claude-opus-4.6"


class TestGetSystemPromptForChannel:
    def test_uses_channel_override_when_present(self):
        config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(system_prompt="You are a coding assistant."),
                    },
                ),
            },
        )
        runner = object.__new__(GatewayRunner)
        runner.config = config
        runner._ephemeral_system_prompt = "Global prompt"
        prompt = runner._get_system_prompt_for_channel(Platform.DISCORD, "chan_1")
        assert prompt == "You are a coding assistant."


class TestResolveSessionAgentRuntimePriority:
    """Model/runtime priority: session /model → channel_overrides → global."""

    def test_channel_override_beats_global(self):
        runner = object.__new__(GatewayRunner)
        runner._session_model_overrides = {}
        runner.config = GatewayConfig(
            platforms={
                Platform.DISCORD: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "chan_1": ChannelOverride(
                            model="channel/model",
                            provider="openrouter",
                        ),
                    },
                ),
            },
        )
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="chan_1",
            user_id="u1",
        )
        with patch("gateway.run._resolve_gateway_model", return_value="global/model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value={
                 "provider": "anthropic",
                 "api_key": "k",
                 "base_url": "https://api.anthropic.com",
                 "api_mode": "chat_completions",
             }), \
             patch(
                 "gateway.run._resolve_runtime_agent_kwargs_for_provider",
                 return_value={
                     "provider": "openrouter",
                     "api_key": "k2",
                     "base_url": "https://openrouter.ai/api/v1",
                     "api_mode": "chat_completions",
                 },
             ):
            model, runtime = runner._resolve_session_agent_runtime(
                source=source,
                user_config={"model": {"default": "global/model"}},
            )
        assert model == "channel/model"
        assert runtime["provider"] == "openrouter"


