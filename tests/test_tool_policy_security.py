import json
from types import SimpleNamespace
from unittest.mock import patch

from agent.agent_init import _apply_tool_policy
from agent.tool_policy import (
    ToolAccessPolicy,
    allowed_tool_names_for_dispatch,
    authorize_agent_tool,
    parse_tool_policy,
)
from model_tools import get_tool_definitions, handle_function_call
from tools.delegate_tool import DELEGATE_BLOCKED_TOOLS, _build_child_agent


MAIN_NAMES = {"clarify", "delegate_task", "memory", "skills_list", "skill_manage"}


def _tool(name):
    return {
        "type": "function",
        "function": {"name": name, "description": "", "parameters": {}},
    }


def test_main_exact_allowlist_has_only_five_tools():
    policy = ToolAccessPolicy(mode="allowlist", allowed_names=frozenset(MAIN_NAMES))
    definitions = get_tool_definitions(
        enabled_toolsets=["hermes-discord", "all"],
        quiet_mode=True,
        tool_policy=policy,
    )
    assert {d["function"]["name"] for d in definitions} == MAIN_NAMES
    assert "skill_view" not in {d["function"]["name"] for d in definitions}


def test_unrestricted_policy_matches_configured_available_surface():
    baseline = get_tool_definitions(enabled_toolsets=["hermes-discord"], quiet_mode=True)
    unrestricted = get_tool_definitions(
        enabled_toolsets=["hermes-discord"],
        quiet_mode=True,
        tool_policy=ToolAccessPolicy(mode="unrestricted", source="trusted-discord"),
    )
    assert {d["function"]["name"] for d in unrestricted} == {
        d["function"]["name"] for d in baseline
    }


def test_invalid_policy_fails_closed():
    policy = parse_tool_policy({"mode": "surprise"}, source="test")
    assert not policy.valid
    assert not policy.allows("terminal")

    typo = parse_tool_policy({"mode": "unrestricted", "toolz": []}, source="test")
    assert not typo.valid
    assert not typo.allows("terminal")


def test_late_injected_allowlist_tool_is_checked_only_after_all_injections():
    """Memory/context tools may satisfy an allowlist after registry discovery."""
    policy = ToolAccessPolicy(
        mode="allowlist",
        allowed_names=frozenset({"read_file", "memory_search"}),
        source="test",
    )
    agent = SimpleNamespace(
        tools=[_tool("read_file")],
        valid_tool_names={"read_file"},
        tool_policy=policy,
    )

    _apply_tool_policy(agent)
    assert agent.valid_tool_names == {"read_file"}

    agent.tools.append(_tool("memory_search"))
    _apply_tool_policy(agent, require_complete_allowlist=True)

    assert agent.valid_tool_names == {"read_file", "memory_search"}
    assert allowed_tool_names_for_dispatch(agent) == frozenset(
        {"read_file", "memory_search"}
    )


def test_final_incomplete_allowlist_denies_entire_surface():
    policy = ToolAccessPolicy(
        mode="allowlist",
        allowed_names=frozenset({"read_file", "missing_tool"}),
        source="test",
    )
    agent = SimpleNamespace(
        tools=[_tool("read_file")],
        valid_tool_names={"read_file"},
        tool_policy=policy,
        _context_engine_tool_names={"read_file"},
    )

    _apply_tool_policy(agent, require_complete_allowlist=True)

    assert agent.tools == []
    assert agent.valid_tool_names == set()
    assert agent._context_engine_tool_names == set()


def test_fabricated_disallowed_call_is_rejected_before_dispatch():
    agent = SimpleNamespace(
        tool_policy=ToolAccessPolicy(
            mode="allowlist", allowed_names=frozenset(MAIN_NAMES)
        ),
        valid_tool_names=set(MAIN_NAMES),
    )
    result = json.loads(authorize_agent_tool(agent, "skill_view"))
    assert result["code"] == "tool_not_allowed_by_policy"

    direct = json.loads(
        handle_function_call(
            "skill_view", {}, allowed_tool_names=frozenset(MAIN_NAMES)
        )
    )
    assert direct["code"] == "tool_not_allowed_by_policy"


def _parent():
    return SimpleNamespace(
        enabled_toolsets=["hermes-cli", "browser", "mcp-test-server"],
        valid_tool_names=set(MAIN_NAMES),
        _delegate_depth=0,
        _subagent_id=None,
        model="test-model",
        provider="test-provider",
        base_url="https://example.invalid/v1",
        api_key="key",
        api_mode="chat_completions",
        reasoning_config=None,
        prefill_messages=[],
        _fallback_chain=[],
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection="",
        openrouter_min_coding_score=None,
        max_tokens=None,
        request_overrides={},
        acp_command=None,
        acp_args=[],
        tool_progress_callback=None,
        _session_db=None,
        session_id="parent",
    )


def test_all_configured_child_uses_future_safe_universe_and_name_blocklist():
    child = SimpleNamespace(session_id="child")
    with patch("tools.delegate_tool._load_config", return_value={
        "child_tool_policy": {"mode": "all_configured"},
        "max_spawn_depth": 1,
    }), patch("run_agent.AIAgent", return_value=child) as ctor:
        _build_child_agent(0, "work", None, None, None, 5, 1, _parent())

    kwargs = ctor.call_args.kwargs
    assert kwargs["enabled_toolsets"] == ["all"]
    policy = kwargs["tool_policy"]
    assert policy.mode == "denylist"
    assert policy.denied_names == DELEGATE_BLOCKED_TOOLS
    assert not policy.allows("delegate_task")
    assert not policy.allows("memory")
    assert policy.allows("terminal")
    assert policy.allows("read_file")
    assert policy.allows("browser_navigate")
    assert policy.allows("future_plugin_tool")
    assert policy.allows("mcp__test__tool")


def test_orchestrator_only_regains_delegation_when_depth_allows():
    child = SimpleNamespace(session_id="child")
    with patch("tools.delegate_tool._load_config", return_value={
        "child_tool_policy": {"mode": "all_configured"},
        "max_spawn_depth": 2,
        "orchestrator_enabled": True,
    }), patch("run_agent.AIAgent", return_value=child) as ctor:
        _build_child_agent(
            0, "work", None, None, None, 5, 1, _parent(), role="orchestrator"
        )
    policy = ctor.call_args.kwargs["tool_policy"]
    assert policy.allows("delegate_task")
    for name in DELEGATE_BLOCKED_TOOLS - {"delegate_task"}:
        assert not policy.allows(name)
