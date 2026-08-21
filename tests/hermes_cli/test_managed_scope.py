"""Unit tests for hermes_cli.managed_scope (resolver + loaders + key helpers)."""
import logging
import textwrap

import pytest


# ── Directory resolver ───────────────────────────────────────────────────────


def test_missing_managed_dir_override_logs_policy_failure(
    tmp_path,
    monkeypatch,
    caplog,
):
    from hermes_cli import managed_scope

    missing = tmp_path / "missing-managed"
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(missing))

    with caplog.at_level(logging.ERROR, logger=managed_scope.__name__):
        assert managed_scope.get_managed_dir() is None

    assert str(missing) in caplog.text


def test_pytest_detection_does_not_trust_environment(monkeypatch):
    import sys

    from hermes_cli import managed_scope

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "spoofed")
    monkeypatch.delitem(sys.modules, "pytest")

    assert managed_scope._under_pytest() is False






# ── Loaders + key helpers ────────────────────────────────────────────────────


def _write_managed(tmp_path, monkeypatch, *, config=None, env=None):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir(exist_ok=True)
    if config is not None:
        (managed / "config.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    if env is not None:
        (managed / ".env").write_text(textwrap.dedent(env), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    return managed








def test_load_managed_env_and_is_env_managed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _write_managed(
        tmp_path, monkeypatch, env="OPENAI_API_BASE=https://org.example/v1\n"
    )
    assert managed_scope.load_managed_env() == {
        "OPENAI_API_BASE": "https://org.example/v1"
    }
    assert managed_scope.is_env_managed("OPENAI_API_BASE") is True
    assert managed_scope.is_env_managed("OTHER") is False


def test_load_managed_env_uses_canonical_dotenv_parsing(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _write_managed(
        tmp_path,
        monkeypatch,
        env=(
            'export QUOTED="has # inside and \\"quotes\\"" # trailing\n'
            "UNQUOTED=value # comment\n"
            "HASH=foo#bar\n"
            "SINGLE='literal # value' # trailing\n"
        ),
    )

    assert managed_scope.load_managed_env() == {
        "QUOTED": 'has # inside and "quotes"',
        "UNQUOTED": "value",
        "HASH": "foo#bar",
        "SINGLE": "literal # value",
    }




def test_managed_dir_env_scrubbed_by_default():
    """conftest must scrub HERMES_MANAGED_DIR so a dev-shell value can't leak in."""
    import os

    assert "HERMES_MANAGED_DIR" not in os.environ


def test_load_managed_config_malformed_fails_closed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    _write_managed(tmp_path, monkeypatch, config="model: : : not yaml :")
    with pytest.raises(managed_scope.ManagedConfigError):
        managed_scope.load_managed_config()


def test_malformed_edit_retains_last_known_good(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = _write_managed(
        tmp_path,
        monkeypatch,
        config="agent:\n  tool_policy:\n    mode: allowlist\n    tools: [memory]\n",
    )
    good = managed_scope.load_managed_config()
    (managed / "config.yaml").write_text("agent: : broken", encoding="utf-8")
    assert managed_scope.load_managed_config() == good
