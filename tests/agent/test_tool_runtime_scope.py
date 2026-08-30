"""Tool availability is resolved against the agent being built/refreshed."""

from __future__ import annotations

import types


def _clear_tool_caches():
    import model_tools
    from tools.registry import invalidate_check_fn_cache

    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()


def test_delegated_codex_child_constructs_with_native_vision(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cached_image = tmp_path / "cache" / "images" / "pixel.png"
    cached_image.parent.mkdir(parents=True)
    cached_image.write_bytes(b"test")

    from agent.auxiliary_client import _read_main_provider
    from run_agent import AIAgent
    from tools import delegate_tool, vision_tools

    # This makes the availability verdict an exact scope probe: it succeeds
    # only while the child runtime is bound, and cannot be rescued by auxiliary
    # credentials or by the Anthropic parent.
    monkeypatch.setattr(
        vision_tools,
        "_should_use_native_vision_fast_path",
        lambda: _read_main_provider() == "openai-codex",
    )
    monkeypatch.setattr(
        delegate_tool,
        "_load_config",
        lambda: {
            "child_tool_policy": {"mode": "all_configured"},
            "max_spawn_depth": 1,
            "child_terminal": {
                "backend": "local",
                "agent_visible_cache_base": "/opt/hermes-cache",
            },
        },
    )
    _clear_tool_caches()

    parent = AIAgent(
        base_url="https://api.example.invalid",
        api_key="parent-test",
        provider="anthropic",
        api_mode="anthropic_messages",
        model="claude-fable-5",
        enabled_toolsets=[],
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    child = None
    try:
        child = delegate_tool._build_child_agent(
            0,
            f"inspect the attachment at {cached_image}",
            None,
            None,
            "gpt-5.6-sol",
            5,
            1,
            parent,
            override_provider="openai-codex",
            override_base_url="https://chatgpt.com/backend-api/codex",
            override_api_key="child-test",
            override_api_mode="codex_responses",
        )
        assert "vision_analyze" in child.valid_tool_names
        assert child._subagent_goal == (
            "inspect the attachment at /opt/hermes-cache/images/pixel.png"
        )
    finally:
        if child is not None:
            child.close()
        parent.close()
        _clear_tool_caches()


def test_tool_refresh_binds_the_refreshed_agents_runtime(monkeypatch):
    import model_tools
    from agent.auxiliary_client import _read_main_provider, scoped_runtime_main
    from agent.tool_policy import LEGACY_TOOL_POLICY
    from tools import mcp_tool, vision_tools

    seen = []

    def native_for_child():
        seen.append(_read_main_provider())
        return _read_main_provider() == "openai-codex"

    monkeypatch.setattr(
        vision_tools, "_should_use_native_vision_fast_path", native_for_child
    )
    agent = types.SimpleNamespace(
        provider="openai-codex",
        model="gpt-5.6-sol",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="child-test",
        api_mode="codex_responses",
        auth_mode="oauth",
        enabled_toolsets=["vision"],
        disabled_toolsets=None,
        tool_policy=LEGACY_TOOL_POLICY,
        tools=[],
        valid_tool_names=set(),
        _tool_snapshot_generation=-1,
    )
    _clear_tool_caches()
    try:
        with scoped_runtime_main({"provider": "anthropic", "model": "claude-fable-5"}):
            added = mcp_tool.refresh_agent_mcp_tools(agent)
            assert _read_main_provider() == "anthropic"
        assert added == {"vision_analyze"}
        assert seen == ["openai-codex"]
        assert "vision_analyze" in agent.valid_tool_names
    finally:
        model_tools._clear_tool_defs_cache()
        _clear_tool_caches()
