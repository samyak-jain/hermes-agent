import { randomUUID } from "node:crypto";
import {
  createSdkMcpServer,
  query,
  tool,
  type Options,
  type PermissionMode,
  type SDKMessage,
} from "@anthropic-ai/claude-agent-sdk";
import { jsonSchemaShape } from "./schema.js";
import type { RpcConnection } from "./rpc.js";
import type { HostToolSchema, ThreadState, ThreadStore, Usage } from "./threads.js";

interface TurnResult {
  status: "completed" | "interrupted" | "failed";
  usage?: Usage;
  contextWindow?: number;
  error?: string;
}

interface ToolItem {
  itemId: string;
  item: Record<string, unknown>;
}

function sdkEnvironment(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(process.env).filter(
      (entry): entry is [string, string] =>
        entry[1] !== undefined && entry[0] !== "ANTHROPIC_API_KEY",
    ),
  );
}

function hostToolDefinition(
  rpc: RpcConnection,
  thread: ThreadState,
  turnId: string,
  definition: HostToolSchema,
) {
  return tool(
    definition.name,
    definition.description ?? "",
    jsonSchemaShape(definition.inputSchema ?? {}),
    async (arguments_: Record<string, unknown>) => {
      const response = await rpc.request<{ content?: string; isError?: boolean }>(
        "agent/tool/call",
        {
          threadId: thread.threadId,
          turnId,
          toolCallId: `tool_${randomUUID()}`,
          name: definition.name,
          arguments: arguments_,
        },
      );
      return {
        content: [{ type: "text", text: response.content ?? "" }],
        isError: Boolean(response.isError),
      };
    },
    { alwaysLoad: true },
  );
}

function mcpServer(rpc: RpcConnection, thread: ThreadState, turnId: string) {
  return createSdkMcpServer({
    name: "agent-runtime",
    version: "1.0.0",
    instructions:
      "These are the host runtime's policy-filtered tools. Use them for all actions; " +
      "their results and approvals are authoritative.",
    tools: thread.tools.map((definition) =>
      hostToolDefinition(rpc, thread, turnId, definition),
    ),
  });
}

function parseMcpTool(name: string): { server: string; tool: string } {
  const match = /^mcp__([^_].*?)__(.+)$/.exec(name);
  return match ? { server: match[1], tool: match[2] } : { server: "mcp", tool: name };
}

function toolItem(name: string, input: unknown): Record<string, unknown> {
  const { server, tool: toolName } = parseMcpTool(name);
  return {
    type: "mcpToolCall",
    server,
    tool: toolName,
    arguments: input ?? {},
  };
}

function renderToolResult(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((entry: any) => (entry?.type === "text" ? String(entry.text ?? "") : ""))
      .join("");
  }
  return content == null ? "" : JSON.stringify(content);
}

function usageFromResult(result: any): { usage: Usage; contextWindow?: number } {
  const raw = result?.usage ?? {};
  const usage: Usage = {
    inputTokens: Number(raw.input_tokens ?? 0),
    cachedInputTokens: Number(raw.cache_read_input_tokens ?? 0),
    outputTokens: Number(raw.output_tokens ?? 0),
    reasoningOutputTokens: 0,
    totalTokens: 0,
  };
  usage.totalTokens = usage.inputTokens + usage.cachedInputTokens + usage.outputTokens;
  const modelUsage = Object.values(result?.modelUsage ?? {}) as any[];
  const contextWindow = modelUsage.find((entry) => Number(entry?.contextWindow) > 0)
    ?.contextWindow;
  return { usage, contextWindow: contextWindow ? Number(contextWindow) : undefined };
}

function addUsage(total: Usage, last: Usage): void {
  total.inputTokens += last.inputTokens;
  total.cachedInputTokens += last.cachedInputTokens;
  total.outputTokens += last.outputTokens;
  total.reasoningOutputTokens += last.reasoningOutputTokens;
  total.totalTokens += last.totalTokens;
}

export async function runTurn(options: {
  rpc: RpcConnection;
  threads: ThreadStore;
  thread: ThreadState;
  turnId: string;
  userText: string;
  compaction?: boolean;
}): Promise<TurnResult> {
  const { rpc, threads, thread, turnId, userText, compaction = false } = options;
  const threadId = thread.threadId;
  const abort = new AbortController();
  const toolItems = new Map<string, ToolItem>();
  let currentMessageItem: string | undefined;

  const sdkOptions: Options = {
    cwd: thread.cwd,
    resume: thread.claudeSessionId,
    model: thread.model,
    permissionMode: (thread.permissionMode ?? "bypassPermissions") as PermissionMode,
    allowDangerouslySkipPermissions:
      (thread.permissionMode ?? "bypassPermissions") === "bypassPermissions",
    includePartialMessages: true,
    abortController: abort,
    systemPrompt: {
      type: "preset",
      preset: "claude_code",
      append: thread.systemPromptAppend,
    },
    settingSources: ["user", "project", "local"],
    managedSettings: {
      autoMemoryEnabled: false,
      autoDreamEnabled: false,
    },
    skills: "all",
    tools: [],
    strictMcpConfig: true,
    mcpServers: { "agent-runtime": mcpServer(rpc, thread, turnId) },
    env: sdkEnvironment(),
    stderr: (data) => process.stderr.write(data),
  };

  const runningQuery = query({ prompt: userText, options: sdkOptions });
  thread.activeTurn = { turnId, query: runningQuery, abort };
  rpc.notify("turn/started", { threadId, turn: { id: turnId } });

  let result: TurnResult = { status: "failed", error: "no result received" };
  try {
    for await (const message of runningQuery as AsyncIterable<SDKMessage>) {
      switch (message.type) {
        case "system":
          if (message.subtype === "init") {
            threads.bindClaudeSession(thread, message.session_id);
          }
          break;
        case "stream_event": {
          const event: any = (message as any).event;
          if (
            event?.type === "content_block_delta" &&
            event.delta?.type === "text_delta"
          ) {
            if (!currentMessageItem) {
              currentMessageItem = `item_${randomUUID()}`;
              rpc.notify("item/started", {
                threadId,
                turnId,
                item: { id: currentMessageItem, type: "agentMessage", text: "" },
              });
            }
            rpc.notify("item/agentMessage/delta", {
              threadId,
              turnId,
              itemId: currentMessageItem,
              delta: event.delta.text,
            });
          }
          break;
        }
        case "assistant":
          for (const block of (message as any).message?.content ?? []) {
            if (block.type === "text") {
              const itemId = currentMessageItem ?? `item_${randomUUID()}`;
              if (!currentMessageItem) {
                rpc.notify("item/started", {
                  threadId,
                  turnId,
                  item: { id: itemId, type: "agentMessage", text: "" },
                });
              }
              rpc.notify("item/completed", {
                threadId,
                turnId,
                item: { id: itemId, type: "agentMessage", text: block.text },
              });
              currentMessageItem = undefined;
            } else if (block.type === "thinking") {
              rpc.notify("item/completed", {
                threadId,
                turnId,
                item: {
                  id: `item_${randomUUID()}`,
                  type: "reasoning",
                  summary: [String(block.thinking ?? "")],
                  content: [],
                },
              });
            } else if (block.type === "tool_use") {
              const itemId = `item_${randomUUID()}`;
              const item = toolItem(block.name, block.input);
              toolItems.set(block.id, { itemId, item });
              rpc.notify("item/started", {
                threadId,
                turnId,
                item: { id: itemId, status: "inProgress", ...item },
              });
            }
          }
          break;
        case "user":
          for (const block of (message as any).message?.content ?? []) {
            if (block?.type !== "tool_result") continue;
            const mapped = toolItems.get(block.tool_use_id);
            if (!mapped) continue;
            const output = renderToolResult(block.content);
            rpc.notify("item/completed", {
              threadId,
              turnId,
              item: {
                id: mapped.itemId,
                status: block.is_error ? "failed" : "completed",
                ...mapped.item,
                ...(block.is_error
                  ? { error: { message: output } }
                  : { result: output }),
              },
            });
            toolItems.delete(block.tool_use_id);
          }
          break;
        case "result": {
          const sdkResult: any = message;
          const measured = usageFromResult(sdkResult);
          result = sdkResult.is_error
            ? {
                status: "failed",
                error: String(sdkResult.result ?? sdkResult.subtype ?? "SDK error"),
                ...measured,
              }
            : { status: "completed", ...measured };
          break;
        }
      }
    }
  } catch (error: any) {
    result = abort.signal.aborted
      ? { status: "interrupted" }
      : { status: "failed", error: error?.message ?? String(error) };
  } finally {
    thread.activeTurn = undefined;
  }

  if (result.usage) {
    addUsage(thread.usageTotal, result.usage);
    rpc.notify("thread/tokenUsage/updated", {
      threadId,
      turnId,
      tokenUsage: {
        last: result.usage,
        total: thread.usageTotal,
        modelContextWindow: result.contextWindow,
      },
    });
  }
  if (compaction && result.status === "completed") {
    const item = { id: `item_${randomUUID()}`, type: "contextCompaction" };
    rpc.notify("item/started", { threadId, turnId, item });
    rpc.notify("item/completed", { threadId, turnId, item });
    rpc.notify("thread/compacted", { threadId, turnId });
  }
  rpc.notify("turn/completed", {
    threadId,
    turn: {
      id: turnId,
      status: result.status,
      ...(result.error ? { error: { message: result.error } } : {}),
    },
  });
  return result;
}
