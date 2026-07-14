"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import (
    build_app_server_identity_prompt,
    build_app_server_system_prompt,
    build_system_prompt_parts,
)


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        _memory_enabled=True,
        _user_profile_enabled=True,
        _parallel_tool_call_guidance=False,
        context_compressor=None,
        _emit_status=lambda _message: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(cwd=None, skip_soul=False, context_length=None):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _init_code_repo(path):
    """A git repo that actually holds code — the coding posture requires a source
    file (or manifest), not a bare ``.git`` (a prose/notes repo stays general)."""
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "main.py").write_text("print('hi')\n")


class TestCodingContextBlock:
    def test_injected_when_active(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        stable = _stable_prompt(agent)
        assert "coding agent" in stable
        assert "Workspace" in stable

    def test_absent_when_off(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        # Drive the real path: force the resolved mode to "off" via config.
        with patch("agent.coding_context._coding_mode", return_value="off"):
            stable = _stable_prompt(agent)
        assert "coding agent" not in stable

    def test_absent_without_tools(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=[], platform="cli")
        assert "coding agent" not in _stable_prompt(agent)


class _PromptMemoryStore:
    def format_for_system_prompt(self, target):
        return {
            "memory": "# Long-term memory\nThe user prefers terse answers.",
            "user": "# User profile\nThe user's name is Samyak.",
        }[target]


def test_app_server_prompt_keeps_persona_context_and_memory_without_product_identity():
    agent = _make_agent(
        valid_tool_names={"memory", "session_search", "skill_manage"},
        _memory_store=_PromptMemoryStore(),
        pass_session_id=True,
        session_id="session-123",
    )
    with (
        patch("run_agent.load_soul_md", return_value="# Soul\nBe warm and incisive."),
        patch("run_agent.build_environment_hints", return_value=""),
        patch(
            "run_agent.build_context_files_prompt",
            return_value="# Project Context\nAGENTS.md says to verify deployments.",
        ),
        patch("agent.coding_context.coding_system_blocks", return_value=[]),
    ):
        prompt = build_app_server_system_prompt(
            agent,
            system_message="Messages can be prefixed with a sender name.",
        )
        identity = build_app_server_identity_prompt(agent)

    assert "Be warm and incisive" in prompt
    assert "AGENTS.md says to verify deployments" in prompt
    assert "The user prefers terse answers" in prompt
    assert "The user's name is Samyak" in prompt
    assert "Messages can be prefixed" in prompt
    assert "# Operator-defined persona" in identity
    assert "# Soul\nBe warm and incisive." in identity
    assert "governs the voice, register, expressiveness" in identity
    assert "terse, professional, or emotionally restrained" in identity
    assert "Session ID: session-123" in prompt
    assert "You are Hermes Agent" not in prompt
    assert "You run on Hermes Agent" not in prompt
