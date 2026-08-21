"""Tests for config.yaml structure validation (validate_config_structure)."""


import argparse
import json

import pytest
import yaml

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _EXTRA_KNOWN_ROOT_KEYS,
    _KNOWN_ROOT_KEYS,
    validate_config_structure,
    ConfigIssue,
)


class TestCustomProvidersValidation:
    """custom_providers must be a YAML list, not a dict."""

    def test_dict_instead_of_list(self):
        """The exact Discord user scenario — custom_providers as flat dict."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "Generativelanguage.googleapis.com",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "xxx",
                "model": "models/gemini-2.5-flash",
                "rate_limit_delay": 2.0,
                "fallback_model": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3.6-plus:free",
                },
            },
            "fallback_providers": [],
        })
        errors = [i for i in issues if i.severity == "error"]
        assert any("dict" in i.message and "list" in i.message for i in errors), (
            "Should detect custom_providers as dict instead of list"
        )

    def test_dict_detects_misplaced_fields(self):
        """When custom_providers is a dict, detect fields that look misplaced."""
        issues = validate_config_structure({
            "custom_providers": {
                "name": "test",
                "base_url": "https://example.com",
                "api_key": "xxx",
            },
        })
        warnings = [i for i in issues if i.severity == "warning"]
        # Should flag base_url, api_key as looking like custom_providers entry fields
        misplaced = [i for i in warnings if "custom_providers entry fields" in i.message]
        assert len(misplaced) == 1


    def test_list_entry_not_dict(self):
        """Non-dict list entries should warn."""
        issues = validate_config_structure({
            "custom_providers": ["not-a-dict"],
            "model": {"provider": "custom"},
        })
        assert any("not a dict" in i.message for i in issues)




class TestMissingModelSection:
    """Warn when custom_providers exists but model section is missing."""


    def test_custom_providers_with_model(self):
        issues = validate_config_structure({
            "custom_providers": [
                {"name": "test", "base_url": "https://example.com/v1"},
            ],
            "model": {"provider": "custom", "default": "test-model"},
        })
        # Should not warn about missing model section
        assert not any("no 'model' section" in i.message for i in issues)


class TestExactToolPolicyValidation:
    def test_valid_global_and_channel_policies(self):
        issues = validate_config_structure({
            "agent": {"tool_policy": {
                "mode": "allowlist",
                "tools": ["memory"],
                "gateway_override_authority": "managed_only",
            }},
            "platforms": {"discord": {"channel_overrides": {
                "123": {"tool_policy": {"mode": "unrestricted"}},
            }}},
        })
        assert not [issue for issue in issues if "tool policy" in issue.message]

    def test_valid_cron_policy(self):
        issues = validate_config_structure({
            "cron": {"tool_policy": {
                "mode": "allowlist",
                "tools": ["delegate_task", "memory"],
            }},
        })

        assert not [issue for issue in issues if "cron.tool_policy" in issue.message]

    def test_malformed_cron_policy_is_an_error(self):
        issues = validate_config_structure({
            "cron": {"tool_policy": {
                "mode": "unrestricted",
                "tools": ["delegate_task"],
            }},
        })

        assert any(
            issue.severity == "error"
            and "cron.tool_policy" in issue.message
            for issue in issues
        )

    def test_malformed_channel_policy_is_an_error(self):
        issues = validate_config_structure({
            "platforms": {"discord": {"channel_overrides": {
                "123": {"tool_policy": {"mode": "unrestricted", "tools": ["terminal"]}},
            }}},
        })
        assert any(
            issue.severity == "error" and "only valid" in issue.message
            for issue in issues
        )

    def test_legacy_discord_layout_uses_same_channel_policy_validation(self):
        issues = validate_config_structure({
            "discord": {"channel_overrides": {
                "123": {
                    "tool_policy": {
                        "mode": "unrestricted",
                        "tools": ["terminal"],
                    }
                },
            }},
        })

        assert any(
            issue.severity == "error"
            and "discord.channel_overrides.123.tool_policy" in issue.message
            for issue in issues
        )

    def test_profile_policy_in_channel_override_is_retained_typed_config(self):
        """The deployed profile-scoped channel policy is a supported field."""
        from gateway.config import ChannelOverride

        raw_override = {
            "profile_tool_policies": {
                "vegapunk": {
                    "mode": "denylist",
                    "tools": ["delegate_task"],
                }
            }
        }
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {
                        "channel_overrides": {
                            "operator-room": raw_override,
                        }
                    }
                }
            },
            source="managed:/etc/hermes/config.yaml",
            unknown_severity="error",
        )

        assert not [
            issue
            for issue in issues
            if "profile_tool_policies.vegapunk" in issue.path
        ]
        assert ChannelOverride.from_dict(raw_override).profile_tool_policies == {
            "vegapunk": {
                "mode": "denylist",
                "tools": ["delegate_task"],
            }
        }

    def test_unknown_profile_policy_key_surfaces_source_and_dotted_path(self):
        issues = validate_config_structure(
            {
                "platforms": {
                    "discord": {
                        "channel_overrides": {
                            "operator-room": {
                                "profile_tool_policies": {
                                    "vegapunk": {
                                        "mode": "denylist",
                                        "tools": ["delegate_task"],
                                        "toolsets": ["terminal"],
                                    }
                                }
                            }
                        }
                    }
                }
            },
            source="managed:/nix/store/kumo-config.yaml",
            unknown_severity="error",
        )

        issue = next(issue for issue in issues if issue.path.endswith(".toolsets"))
        assert issue.severity == "error"
        assert issue.source == "managed:/nix/store/kumo-config.yaml"
        assert issue.path == (
            "platforms.discord.channel_overrides.operator-room."
            "profile_tool_policies.vegapunk.toolsets"
        )
        assert issue.source in issue.message
        assert issue.path in issue.message


class TestConfigIssueDataclass:
    """ConfigIssue should be a proper dataclass."""

    def test_fields(self):
        issue = ConfigIssue(severity="error", message="test msg", hint="test hint")
        assert issue.severity == "error"
        assert issue.message == "test msg"
        assert issue.hint == "test hint"

    def test_equality(self):
        a = ConfigIssue("error", "msg", "hint")
        b = ConfigIssue("error", "msg", "hint")
        assert a == b


class TestVoiceSubmitModeValidation:
    def test_default_is_direct(self):
        assert DEFAULT_CONFIG["voice"]["submit_mode"] == "direct"

    def test_direct_and_draft_are_valid(self):
        for mode in ("direct", "draft"):
            issues = validate_config_structure({"voice": {"submit_mode": mode}})
            assert not any("voice.submit_mode" in issue.message for issue in issues)

    def test_invalid_mode_is_reported(self):
        issues = validate_config_structure({"voice": {"submit_mode": "refine"}})

        assert any(
            issue.severity == "error"
            and "voice.submit_mode" in issue.message
            and "direct" in issue.hint
            and "draft" in issue.hint
            for issue in issues
        )


def test_managed_config_validate_command_is_ci_strict_and_value_free(
    tmp_path,
    capsys,
):
    from hermes_cli.config import config_command

    managed = tmp_path / "managed-config.yaml"
    managed.write_text(
        yaml.safe_dump(
            {
                "platforms": {
                    "discord": {
                        "channel_overrides": {
                            "operator-room": {
                                "profile_tool_policies": {
                                    "vegapunk": {
                                        "mode": "denylist",
                                        "tools": ["delegate_task"],
                                        "ignored_typo": "private-value-must-not-print",
                                    }
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        config_command(
            argparse.Namespace(
                config_command="validate",
                managed=str(managed),
                json=True,
            )
        )

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["success"] is False
    assert payload["issues"][0]["severity"] == "error"
    assert payload["issues"][0]["path"].endswith(
        "profile_tool_policies.vegapunk.ignored_typo"
    )
    assert "private-value-must-not-print" not in output


def test_cache_invalidation_preserves_last_known_good_policy(tmp_path, monkeypatch):
    from hermes_cli import config as config_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "approvals:\n  deny:\n    - 'curl*evil.example*'\n",
        encoding="utf-8",
    )
    config_module.invalidate_config_caches(config_path)
    config_module._LAST_EXPANDED_CONFIG_BY_PATH.pop(str(config_path), None)

    assert config_module.load_config()["approvals"]["deny"] == [
        "curl*evil.example*"
    ]

    config_path.write_text("approvals:\n  deny: [unclosed\n", encoding="utf-8")
    config_module.invalidate_config_caches(config_path)

    assert config_module.load_config()["approvals"]["deny"] == [
        "curl*evil.example*"
    ]

class TestUnknownTopLevelKeys:
    """Arbitrary top-level keys must NOT warn — they are bridged to os.environ.

    Top-level scalars in config.yaml are forwarded into the environment
    (gateway/run.py, hermes send) so users can feed skills and external apps
    env-style keys like DISCORD_HOME_CHANNEL or MY_APP_TOKEN. A closed-world
    allowlist can never enumerate those, so no "Unknown top-level config key"
    warning may exist.
    """


    def test_known_root_keys_derived_from_default_config(self):
        """_KNOWN_ROOT_KEYS must be DEFAULT_CONFIG.keys() plus extras — single source of truth."""
        assert set(DEFAULT_CONFIG.keys()).issubset(_KNOWN_ROOT_KEYS)
        assert _EXTRA_KNOWN_ROOT_KEYS.issubset(_KNOWN_ROOT_KEYS)
        assert _KNOWN_ROOT_KEYS == frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS

    def test_provider_like_unknown_root_keeps_misplaced_message(self):
        """Preserve existing base_url/api_key root-level guidance."""
        issues = validate_config_structure({
            "base_url": "https://example.com/v1",
            "api_key": "secret",
        })
        misplaced = [
            i for i in issues
            if i.severity == "warning" and "looks misplaced" in i.message
        ]
        assert any("base_url" in i.message for i in misplaced)
        assert any("api_key" in i.message for i in misplaced)
