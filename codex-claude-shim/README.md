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

The adapter replaces Claude Code's preset with Hermes' cache-aware custom
system prompt. SOUL is sent once as the globally cacheable prefix. Gateway
context, AGENTS.md (or the selected project
context file), memory, user profile, conversation date, model/provider
identity, and session metadata come from Hermes' canonical prompt builder and
follow the SDK's dynamic system-prompt boundary. Gateway/session context is
system-level; user turns never carry a duplicate SOUL or project-context
block.

The native Hermes stable tier also contains long generic tool-loop, skills,
environment, provider, and platform guidance. The app-server does not repeat
that boilerplate: Claude receives the live policy-filtered schemas and MCP
contract directly, while the generic text can push a custom subscription
request onto the extra-usage route. This keeps the operator-owned persona and
conversation context authoritative while removing redundant harness prose.

Hermes still owns per-message composition before the SDK handoff. Discord
sender prefixes, optional message timestamps, triggering-message IDs, gateway
turn notes, plugin context, and external-memory recall therefore follow the
same user-message rules as the native harness. The clean transcript remains
free of those API-only additions. A fixed SDK-local title prevents Claude from
making a separate model call merely to title a session; Hermes keeps owning its
actual user-visible session title.

Hermes skills remain available through the host's policy-filtered skill tools;
the adapter does not load Claude Code's separate filesystem skills, auto-memory,
or auto-dream stores. App-server thread IDs, Claude session IDs, and the initial
prompt snapshot are persisted under
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

The SDK child deliberately removes Anthropic API-key, OAuth-token, base-URL,
and alternate-cloud environment overrides. Authentication therefore comes
only from the Claude Code credential store selected by `CLAUDE_CONFIG_DIR`;
Claude filesystem settings are disabled so an `env` or `apiKeyHelper` entry
cannot override that credential source. Hermes side-task credentials cannot
silently select API billing or a different account. Authenticate the container
user with Claude Code and keep its credential file at
`$CLAUDE_CONFIG_DIR/.credentials.json` with mode 0600.

Using a custom `systemPrompt` does not change the authentication or billing
route. The SDK still launches its bundled Claude executable with the persisted
subscription credential; the prompt contents are independent of that
credential selection.

The SDK still contributes three small protocol-owned blocks that its public
options do not expose for removal: the Claude Agent SDK identity line, the
subscription billing marker, and a first-turn account/date reminder. They are
kept because the billing marker is part of subscription routing and patching
the bundled executable would be brittle. The large Claude Code coding prompt,
filesystem context, output style, native skills, and model-generated session
title are not used.

`codex --subscription-status` reads Claude's structured usage status without
making a model request. It reports only non-secret subscription type,
five-hour/weekly utilization, model-scoped limits, and whether extra usage is
enabled.

Use `codex --subscription-login` interactively on the machine that will run
the adapter. It launches the Claude Code binary bundled with the Agent SDK and
creates an independent refreshable login in `CLAUDE_CONFIG_DIR`. Do not copy a
live `.credentials.json` between machines: Claude OAuth refreshes rotate the
session credentials, so two copies will eventually invalidate one another.

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
