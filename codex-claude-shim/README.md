# Claude Agent SDK app-server adapter

This package presents the Codex app-server JSONL protocol to the Python agent
runtime and executes turns with the Claude Agent SDK. It is intentionally
embedded in the container image as the `codex` binary; it is not a general
Codex replacement.

The adapter preserves host behavior by disabling Claude Code's built-in action
tools and registering one in-process MCP server from the current agent's
policy-filtered tool schemas. Each MCP call is sent back over the same
bidirectional JSON-RPC connection as `agent/tool/call`, then dispatched by the
live Python `AIAgent`. Memory, session search, approvals, file safety,
middleware, `spawn_agent`, and channel-specific tool policy therefore use the
same implementations as the default runtime.

The appended prompt contains the user-owned behavior layers only: SOUL.md,
gateway context, AGENTS.md or the selected project context file, built-in and
external memories, platform hints, and memory/skill guidance. The normal
product identity and provider-specific tool-loop instructions are omitted;
Claude Code retains its own preset prompt.

Hermes skills are made visible through `~/.claude/skills`, while Claude Code's
separate auto-memory and auto-dream stores are disabled. App-server thread IDs,
Claude session IDs, and the initial prompt snapshot are persisted under
`$HERMES_HOME/.claude-agent-bridge/` so a gateway restart resumes the same
conversation without replacing its original memory snapshot.

## Configuration

```yaml
model:
  default: claude-fable-5
  provider: anthropic
  agent_runtime: codex_app_server
  codex_app_server:
    adapter: claude_agent_sdk
    binary: codex
    model: claude-fable-5
    permission_mode: bypassPermissions
    post_tool_quiet_timeout: 300
```

`bypassPermissions` applies only inside Claude Code. Claude has no native
action tools in this adapter, and every bridged call still passes through the
host agent's policy and approval middleware.

The SDK child deliberately removes `ANTHROPIC_API_KEY` so a configured API key
cannot silently take precedence over subscription auth. Authenticate the
container user with Claude Code and keep its credential file at
`$HOME/.claude/.credentials.json` with mode 0600.

Configure OpenAI children and background self-improvement independently:

```yaml
delegation:
  provider: openai-codex
  model: gpt-5.6-sol

auxiliary:
  background_review:
    provider: openai-codex
    model: gpt-5.6-sol
```

The projected `mcpToolCall` events retain the original host tool names, so tool
iteration counting and automatic skill review see the same transcript shape as
the default runtime.
