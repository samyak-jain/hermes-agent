import { randomUUID } from "node:crypto";
import {
  createSdkMcpServer,
  query,
  SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
  tool,
  type Options,
  type PermissionMode,
  type SDKMessage,
  type SDKUserMessage,
} from "@anthropic-ai/claude-agent-sdk";
import { sdkEnvironment } from "./environment.js";
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
  argumentsComplete: boolean;
}

interface ToolStreamBlock {
  kind: "tool";
  callId: string;
  itemId: string;
  name: string;
  rawArguments: string;
}

interface ReasoningStreamBlock {
  kind: "reasoning";
  itemId: string;
  text: string;
}

type StreamBlock = ToolStreamBlock | ReasoningStreamBlock;

export interface PartialStreamState {
  blocks: Map<number, StreamBlock>;
  currentMessageItem?: string;
  completedReasoningBlocks: number;
}

const CLAUDE_CODE_TOOL_USE_ID_META = "claudecode/toolUseId";
const SUPPORTED_IMAGE_MIMES = new Set([
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
]);
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

export type TurnInputItem =
  | { type: "text"; text: string }
  | { type: "image"; url: string };

type ClaudeTurnContent =
  | { type: "text"; text: string }
  | {
      type: "image";
      source:
        | {
            type: "base64";
            media_type: "image/jpeg" | "image/png" | "image/gif" | "image/webp";
            data: string;
          }
        | { type: "url"; url: string };
    };

function imageUrlFromWireItem(item: any): string {
  const nested = item?.image_url;
  if (typeof nested === "string") return nested.trim();
  if (nested && typeof nested.url === "string") return nested.url.trim();
  if (typeof item?.url === "string") return item.url.trim();
  if (typeof item?.imageUrl === "string") return item.imageUrl.trim();
  const source = item?.source;
  if (source?.type === "url" && typeof source.url === "string") {
    return source.url.trim();
  }
  if (
    source?.type === "base64" &&
    typeof source.media_type === "string" &&
    typeof source.data === "string"
  ) {
    return `data:${source.media_type};base64,${source.data}`;
  }
  return "";
}

export function extractTurnInput(input: any): TurnInputItem[] {
  if (!input) return [];
  if (typeof input === "string") {
    return input.length > 0 ? [{ type: "text", text: input }] : [];
  }
  if (typeof input.message === "string") {
    return input.message.length > 0
      ? [{ type: "text", text: input.message }]
      : [];
  }
  const items = Array.isArray(input.items) ? input.items : input;
  if (!Array.isArray(items)) return [];
  const result: TurnInputItem[] = [];
  for (const item of items) {
    if (typeof item === "string") {
      if (item.length > 0) result.push({ type: "text", text: item });
      continue;
    }
    if (!item || typeof item !== "object") continue;
    if (
      (item.type === "text" || item.type === "input_text") &&
      typeof item.text === "string" &&
      item.text.length > 0
    ) {
      result.push({ type: "text", text: item.text });
      continue;
    }
    if (["image", "image_url", "input_image"].includes(item.type)) {
      const url = imageUrlFromWireItem(item);
      if (url) result.push({ type: "image", url });
    }
  }
  return result;
}

function parseDataImageUrl(url: string): {
  mime: "image/jpeg" | "image/png" | "image/gif" | "image/webp";
  data: string;
} {
  const match = /^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$/.exec(url);
  if (!match) throw new Error("image data URL must contain valid base64");
  const mime = match[1].toLowerCase();
  if (!SUPPORTED_IMAGE_MIMES.has(mime)) {
    throw new Error(`unsupported image MIME type: ${mime}`);
  }
  const data = match[2];
  const decoded = Buffer.from(data, "base64");
  if (decoded.length === 0) throw new Error("image data URL is empty");
  if (decoded.length > MAX_IMAGE_BYTES) {
    throw new Error("image exceeds Claude's 5 MB per-image limit");
  }
  const canonicalInput = data.replace(/=+$/, "");
  const canonicalDecoded = decoded.toString("base64").replace(/=+$/, "");
  if (canonicalInput !== canonicalDecoded) {
    throw new Error("image data URL contains malformed base64");
  }
  const detected = decoded.subarray(0, 12);
  const mimeMatches =
    (mime === "image/png" && detected.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))) ||
    (mime === "image/jpeg" && detected.subarray(0, 3).equals(Buffer.from("ffd8ff", "hex"))) ||
    (mime === "image/gif" && ["GIF87a", "GIF89a"].includes(detected.subarray(0, 6).toString("ascii"))) ||
    (mime === "image/webp" &&
      detected.subarray(0, 4).toString("ascii") === "RIFF" &&
      detected.subarray(8, 12).toString("ascii") === "WEBP");
  if (!mimeMatches) throw new Error(`image bytes do not match declared MIME type: ${mime}`);
  const structurallyComplete =
    (mime === "image/png" &&
      decoded.length >= 20 &&
      decoded.subarray(-12).equals(Buffer.from("0000000049454e44ae426082", "hex"))) ||
    (mime === "image/jpeg" &&
      decoded.length >= 4 &&
      decoded.subarray(-2).equals(Buffer.from("ffd9", "hex"))) ||
    (mime === "image/gif" && decoded.length >= 14 && decoded.at(-1) === 0x3b) ||
    (mime === "image/webp" &&
      decoded.length >= 12 &&
      decoded.readUInt32LE(4) + 8 === decoded.length);
  if (!structurallyComplete) {
    throw new Error(`image data is truncated or malformed: ${mime}`);
  }
  return { mime: mime as any, data };
}

export function claudeContentForTurn(items: TurnInputItem[]): ClaudeTurnContent[] {
  return items.map((item) => {
    if (item.type === "text") return { type: "text", text: item.text };
    if (item.url.startsWith("data:")) {
      const parsed = parseDataImageUrl(item.url);
      return {
        type: "image",
        source: {
          type: "base64",
          media_type: parsed.mime,
          data: parsed.data,
        },
      };
    }
    let parsed: URL;
    try {
      parsed = new URL(item.url);
    } catch {
      throw new Error("image URL must use http(s) or a supported data URL");
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new Error("image URL must use http(s) or a supported data URL");
    }
    return { type: "image", source: { type: "url", url: item.url } };
  });
}

async function* richPrompt(
  content: ClaudeTurnContent[],
): AsyncGenerator<SDKUserMessage> {
  yield {
    type: "user",
    message: { role: "user", content },
    parent_tool_use_id: null,
  };
}

export function toolCallIdFromMcpExtra(extra: unknown): string | undefined {
  if (typeof extra !== "object" || extra === null) return undefined;
  const meta = (extra as { _meta?: unknown })._meta;
  if (typeof meta !== "object" || meta === null) return undefined;
  const toolUseId = (meta as Record<string, unknown>)[
    CLAUDE_CODE_TOOL_USE_ID_META
  ];
  return typeof toolUseId === "string" && toolUseId.length > 0
    ? toolUseId
    : undefined;
}

export function titleForThread(thread: ThreadState): string | undefined {
  // Hermes owns its user-visible titles. A fixed local Claude-session title
  // prevents the SDK from spending a separate model request summarizing the
  // first user message.
  return thread.claudeSessionId ? undefined : "Hermes Agent";
}

export function systemPromptForThread(thread: ThreadState): string | string[] {
  const stable = thread.systemPromptIdentity?.trim();
  const dynamic = thread.systemPromptAppend?.trim();
  if (stable && dynamic) {
    return [stable, SYSTEM_PROMPT_DYNAMIC_BOUNDARY, dynamic];
  }
  return stable || dynamic || "";
}

export function handleSystemMessage(
  rpc: Pick<RpcConnection, "notify">,
  threads: ThreadStore,
  thread: ThreadState,
  turnId: string,
  message: { subtype: string; session_id?: string },
): boolean {
  if (message.subtype === "init" && message.session_id) {
    threads.bindClaudeSession(thread, message.session_id);
  }
  if (message.subtype === "thinking_tokens") {
    // The SDK's token estimates are only a liveness signal. Keep counts and
    // all reasoning-derived data inside the shim; the bridge needs only the
    // turn identity to advance the existing thinking indicator.
    rpc.notify("item/reasoning/progress", {
      threadId: thread.threadId,
      turnId,
    });
    return true;
  }
  return false;
}

export function promptForTurn(
  thread: ThreadState,
  userInput: TurnInputItem[] | string,
): {
  prompt: string | AsyncIterable<SDKUserMessage>;
  includesHostContext: boolean;
} {
  const normalized =
    typeof userInput === "string"
      ? ([{ type: "text", text: userInput }] as TurnInputItem[])
      : userInput;
  const content = claudeContentForTurn(normalized);
  if (content.every((item) => item.type === "text")) {
    return {
      prompt: content.map((item: any) => item.text).join("\n"),
      includesHostContext: false,
    };
  }
  return { prompt: richPrompt(content), includesHostContext: false };
}

export function mcpContentFromHost(content: unknown): any[] {
  const values = Array.isArray(content) ? content : [content];
  const result: any[] = [];
  for (const entry of values) {
    if (typeof entry === "string") {
      result.push({ type: "text", text: entry });
      continue;
    }
    if (!entry || typeof entry !== "object") {
      result.push({ type: "text", text: JSON.stringify(entry ?? null) });
      continue;
    }
    const item: any = entry;
    if (item.type === "text") {
      result.push({ type: "text", text: String(item.text ?? "") });
      continue;
    }
    if (item.type === "image" && typeof item.data === "string") {
      result.push({
        type: "image",
        data: item.data,
        mimeType: String(item.mimeType ?? item.mime_type ?? "image/png"),
      });
      continue;
    }
    if (["image", "image_url", "input_image"].includes(item.type)) {
      const url = imageUrlFromWireItem(item);
      if (!url.startsWith("data:")) {
        throw new Error("host tool image content must be an embedded data URL");
      }
      const parsed = parseDataImageUrl(url);
      result.push({ type: "image", data: parsed.data, mimeType: parsed.mime });
      continue;
    }
    result.push({ type: "text", text: JSON.stringify(item) });
  }
  return result;
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
    async (arguments_: Record<string, unknown>, extra: unknown) => {
      // Claude Code attaches the provider's tool_use block ID to the MCP
      // request metadata. Preserve it across the host callback so streamed
      // tool-use events and execution/results share one unambiguous identity.
      const toolCallId = toolCallIdFromMcpExtra(extra);
      if (!toolCallId) {
        throw new Error(
          `Host tool ${definition.name} is missing the provider tool_use ID`,
        );
      }
      const response = await rpc.request<{ content?: unknown; isError?: boolean }>(
        "agent/tool/call",
        {
          threadId: thread.threadId,
          turnId,
          toolCallId,
          name: definition.name,
          arguments: arguments_,
        },
      );
      return {
        content: mcpContentFromHost(response.content ?? ""),
        isError: Boolean(response.isError),
      };
    },
    { alwaysLoad: true },
  );
}

export function mcpServer(rpc: RpcConnection, thread: ThreadState, turnId: string) {
  return createSdkMcpServer({
    name: "agent-runtime",
    version: "1.0.0",
    tools: thread.tools.map((definition) =>
      hostToolDefinition(rpc, thread, turnId, definition),
    ),
  });
}

function parseMcpTool(name: string): { server: string; tool: string } {
  const match = /^mcp__([^_].*?)__(.+)$/.exec(name);
  return match ? { server: match[1], tool: match[2] } : { server: "mcp", tool: name };
}

function toolItem(
  name: string,
  input: unknown,
  providerCallId?: string,
): Record<string, unknown> {
  const { server, tool: toolName } = parseMcpTool(name);
  return {
    type: "mcpToolCall",
    server,
    tool: toolName,
    arguments: input ?? {},
    ...(providerCallId ? { providerCallId } : {}),
  };
}

export function createPartialStreamState(): PartialStreamState {
  return { blocks: new Map(), completedReasoningBlocks: 0 };
}

function notifyToolArgumentsCompleted(
  rpc: RpcConnection,
  threadId: string,
  turnId: string,
  mapped: ToolItem,
  callId: string,
  name: string,
  arguments_: unknown,
  rawArguments: string,
): void {
  mapped.item = toolItem(name, arguments_, callId);
  mapped.argumentsComplete = true;
  rpc.notify("item/toolCall/argumentsCompleted", {
    threadId,
    turnId,
    itemId: mapped.itemId,
    callId,
    name,
    arguments: arguments_,
    argumentsJson: rawArguments,
  });
}

export function handlePartialStreamEvent(options: {
  rpc: RpcConnection;
  threadId: string;
  turnId: string;
  event: any;
  state: PartialStreamState;
  toolItems: Map<string, ToolItem>;
}): boolean {
  const { rpc, threadId, turnId, event, state, toolItems } = options;
  const index = Number(event?.index);

  if (event?.type === "content_block_start" && Number.isInteger(index)) {
    const block = event.content_block;
    if (block?.type === "tool_use") {
      const callId = typeof block.id === "string" ? block.id : "";
      const name = typeof block.name === "string" ? block.name : "";
      if (!callId || !name) return true;
      const itemId = callId;
      const item = toolItem(name, block.input ?? {}, callId);
      toolItems.set(callId, { itemId, item, argumentsComplete: false });
      state.blocks.set(index, {
        kind: "tool",
        callId,
        itemId,
        name,
        rawArguments: "",
      });
      rpc.notify("item/started", {
        threadId,
        turnId,
        item: { id: itemId, status: "inProgress", ...item },
      });
      return true;
    }
    if (block?.type === "thinking") {
      const itemId = `item_${randomUUID()}`;
      state.blocks.set(index, { kind: "reasoning", itemId, text: "" });
      rpc.notify("item/started", {
        threadId,
        turnId,
        item: { id: itemId, type: "reasoning", summary: [], content: [] },
      });
      return true;
    }
  }

  if (event?.type === "content_block_delta" && Number.isInteger(index)) {
    if (event.delta?.type === "text_delta") {
      if (!state.currentMessageItem) {
        state.currentMessageItem = `item_${randomUUID()}`;
        rpc.notify("item/started", {
          threadId,
          turnId,
          item: { id: state.currentMessageItem, type: "agentMessage", text: "" },
        });
      }
      rpc.notify("item/agentMessage/delta", {
        threadId,
        turnId,
        itemId: state.currentMessageItem,
        delta: event.delta.text,
      });
      return true;
    }

    const streamed = state.blocks.get(index);
    if (event.delta?.type === "thinking_delta") {
      if (!streamed || streamed.kind !== "reasoning") return true;
      const delta = String(event.delta.thinking ?? "");
      streamed.text += delta;
      rpc.notify("item/reasoning/delta", {
        threadId,
        turnId,
        itemId: streamed.itemId,
        delta,
      });
      return true;
    }
    if (event.delta?.type === "input_json_delta") {
      if (!streamed || streamed.kind !== "tool") return true;
      const delta = String(event.delta.partial_json ?? "");
      streamed.rawArguments += delta;
      rpc.notify("item/toolCall/argumentsDelta", {
        threadId,
        turnId,
        itemId: streamed.itemId,
        callId: streamed.callId,
        name: streamed.name,
        delta,
      });
      return true;
    }
  }

  if (event?.type === "content_block_stop" && Number.isInteger(index)) {
    const streamed = state.blocks.get(index);
    if (!streamed) return false;
    state.blocks.delete(index);
    if (streamed.kind === "reasoning") {
      rpc.notify("item/completed", {
        threadId,
        turnId,
        item: {
          id: streamed.itemId,
          type: "reasoning",
          summary: [streamed.text],
          content: [],
        },
      });
      state.completedReasoningBlocks += 1;
      return true;
    }

    const mapped = toolItems.get(streamed.callId);
    if (!mapped) return true;
    try {
      const parsed = JSON.parse(streamed.rawArguments || "{}");
      notifyToolArgumentsCompleted(
        rpc,
        threadId,
        turnId,
        mapped,
        streamed.callId,
        streamed.name,
        parsed,
        streamed.rawArguments || "{}",
      );
    } catch (error: any) {
      rpc.notify("item/toolCall/argumentsCompleted", {
        threadId,
        turnId,
        itemId: streamed.itemId,
        callId: streamed.callId,
        name: streamed.name,
        arguments: null,
        argumentsJson: streamed.rawArguments,
        error: { message: error?.message ?? "invalid tool arguments" },
      });
    }
    return true;
  }

  return false;
}

function renderToolResult(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((entry: any) => {
        if (entry?.type === "text") return String(entry.text ?? "");
        if (entry?.type === "image") {
          return `[image result: ${String(entry.source?.media_type ?? entry.mimeType ?? "image")}]`;
        }
        return "";
      })
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
  userInput: TurnInputItem[];
  compaction?: boolean;
}): Promise<TurnResult> {
  const { rpc, threads, thread, turnId, userInput, compaction = false } = options;
  const threadId = thread.threadId;
  const turnPrompt = promptForTurn(thread, userInput);
  const abort = new AbortController();
  const toolItems = new Map<string, ToolItem>();
  const partialStream = createPartialStreamState();

  const sdkOptions: Options = {
    cwd: thread.cwd,
    resume: thread.claudeSessionId,
    model: thread.model,
    permissionMode: (thread.permissionMode ?? "bypassPermissions") as PermissionMode,
    allowDangerouslySkipPermissions:
      (thread.permissionMode ?? "bypassPermissions") === "bypassPermissions",
    includePartialMessages: true,
    abortController: abort,
    systemPrompt: systemPromptForThread(thread),
    ...(titleForThread(thread) ? { title: titleForThread(thread) } : {}),
    // Hermes supplies its own prompt, context files and skills. Loading Claude
    // filesystem settings here could also load an env/apiKeyHelper override
    // and silently move the main turn away from subscription authentication.
    settingSources: [],
    managedSettings: {
      autoMemoryEnabled: false,
      autoDreamEnabled: false,
    },
    tools: [],
    strictMcpConfig: true,
    mcpServers: { "agent-runtime": mcpServer(rpc, thread, turnId) },
    env: sdkEnvironment(),
    stderr: (data) => process.stderr.write(data),
  };

  const runningQuery = query({ prompt: turnPrompt.prompt, options: sdkOptions });
  thread.activeTurn = { turnId, query: runningQuery, abort };
  rpc.notify("turn/started", { threadId, turn: { id: turnId } });

  let result: TurnResult = { status: "failed", error: "no result received" };
  try {
    for await (const message of runningQuery as AsyncIterable<SDKMessage>) {
      switch (message.type) {
        case "system":
          handleSystemMessage(rpc, threads, thread, turnId, message);
          break;
        case "stream_event": {
          const event: any = (message as any).event;
          handlePartialStreamEvent({
            rpc,
            threadId,
            turnId,
            event,
            state: partialStream,
            toolItems,
          });
          break;
        }
        case "assistant":
          for (const block of (message as any).message?.content ?? []) {
            if (block.type === "text") {
              const itemId = partialStream.currentMessageItem ?? `item_${randomUUID()}`;
              if (!partialStream.currentMessageItem) {
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
              partialStream.currentMessageItem = undefined;
            } else if (block.type === "thinking") {
              if (partialStream.completedReasoningBlocks > 0) {
                partialStream.completedReasoningBlocks -= 1;
                continue;
              }
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
              const prior = toolItems.get(block.id);
              if (prior) {
                if (!prior.argumentsComplete) {
                  notifyToolArgumentsCompleted(
                    rpc,
                    threadId,
                    turnId,
                    prior,
                    block.id,
                    block.name,
                    block.input,
                    JSON.stringify(block.input ?? {}),
                  );
                } else {
                  prior.item = toolItem(block.name, block.input, block.id);
                }
                continue;
              }
              const itemId = block.id;
              const item = toolItem(block.name, block.input, block.id);
              const mapped = { itemId, item, argumentsComplete: false };
              toolItems.set(block.id, mapped);
              rpc.notify("item/started", {
                threadId,
                turnId,
                item: { id: itemId, status: "inProgress", ...item },
              });
              notifyToolArgumentsCompleted(
                rpc,
                threadId,
                turnId,
                mapped,
                block.id,
                block.name,
                block.input,
                JSON.stringify(block.input ?? {}),
              );
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
