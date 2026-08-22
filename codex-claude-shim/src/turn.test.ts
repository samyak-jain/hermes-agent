import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { SYSTEM_PROMPT_DYNAMIC_BOUNDARY } from "@anthropic-ai/claude-agent-sdk";
import { ThreadStore, type ThreadState } from "./threads.js";
import {
  createPartialStreamState,
  handleSystemMessage,
  handlePartialStreamEvent,
  mcpServer,
  promptForTurn,
  systemPromptForThread,
  toolCallIdFromMcpExtra,
  titleForThread,
} from "./turn.js";

const thread = (overrides: Partial<ThreadState> = {}): ThreadState => ({
  threadId: "thr_test",
  tools: [],
  usageTotal: {
    inputTokens: 0,
    cachedInputTokens: 0,
    outputTokens: 0,
    reasoningOutputTokens: 0,
    totalTokens: 0,
  },
  ...overrides,
});

test("SOUL and host context are separate cacheable system blocks", () => {
  const state = thread({
    systemPromptIdentity: "identity from SOUL",
    systemPromptAppend: "AGENTS.md, memories and skill guidance",
  });

  assert.deepEqual(systemPromptForThread(state), [
    "identity from SOUL",
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    "AGENTS.md, memories and skill guidance",
  ]);
  assert.deepEqual(promptForTurn(state, "hello"), {
    prompt: "hello",
    includesHostContext: false,
  });
});

test("one prompt tier does not add an unnecessary cache boundary", () => {
  assert.equal(
    systemPromptForThread(thread({ systemPromptIdentity: "SOUL" })),
    "SOUL",
  );
  assert.equal(systemPromptForThread(thread()), "");
});

test("runtime MCP tools do not add a redundant instructions message", () => {
  const server = mcpServer({} as never, thread(), "turn-1");
  assert.equal((server.instance.server as any)._instructions, undefined);
});

test("runtime MCP tools preserve Claude's provider tool_use ID", async () => {
  let resolveToolRequest!: (params: Record<string, unknown>) => void;
  const toolRequest = new Promise<Record<string, unknown>>((resolve) => {
    resolveToolRequest = resolve;
  });
  const rpc = {
    request: async (_method: string, params: Record<string, unknown>) => {
      resolveToolRequest(params);
      return { content: "ok" };
    },
  };
  const server = mcpServer(
    rpc as never,
    thread({
      tools: [
        {
          name: "probe",
          description: "identity probe",
          inputSchema: {
            type: "object",
            properties: { value: { type: "string" } },
            required: ["value"],
          },
        },
      ],
    }),
    "turn-1",
  );
  const sent: unknown[] = [];
  const transport: any = {
    async start() {},
    async send(message: unknown) {
      sent.push(message);
    },
    async close() {},
  };
  await server.instance.connect(transport);
  transport.onmessage({
    jsonrpc: "2.0",
    id: 7,
    method: "tools/call",
    params: {
      name: "probe",
      arguments: { value: "x" },
      _meta: {
        "claudecode/toolUseId": "toolu_provider_identity_123",
        progressToken: 2,
      },
    },
  });

  assert.deepEqual(await toolRequest, {
    threadId: "thr_test",
    turnId: "turn-1",
    toolCallId: "toolu_provider_identity_123",
    name: "probe",
    arguments: { value: "x" },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal((sent.at(-1) as any)?.id, 7);
  await server.instance.close();
});

test("runtime MCP tools fail closed without a provider tool_use ID", async () => {
  const rpc = {
    request: async () => {
      assert.fail("host callback must not run without provider identity");
    },
  };
  const server = mcpServer(
    rpc as never,
    thread({
      tools: [
        {
          name: "probe",
          description: "identity probe",
          inputSchema: { type: "object", properties: {} },
        },
      ],
    }),
    "turn-1",
  );
  const sent: unknown[] = [];
  const transport: any = {
    async start() {},
    async send(message: unknown) {
      sent.push(message);
    },
    async close() {},
  };
  await server.instance.connect(transport);
  transport.onmessage({
    jsonrpc: "2.0",
    id: 8,
    method: "tools/call",
    params: { name: "probe", arguments: {} },
  });
  await new Promise((resolve) => setImmediate(resolve));

  const response = sent.find((message: any) => message?.id === 8) as any;
  assert.equal(response?.result?.isError, true);
  assert.match(
    String(response?.result?.content?.[0]?.text),
    /provider tool_use ID/,
  );
  await server.instance.close();
});

test("provider tool_use ID extraction fails closed on malformed metadata", () => {
  assert.equal(toolCallIdFromMcpExtra(undefined), undefined);
  assert.equal(toolCallIdFromMcpExtra({ _meta: {} }), undefined);
  assert.equal(
    toolCallIdFromMcpExtra({ _meta: { "claudecode/toolUseId": 7 } }),
    undefined,
  );
  assert.equal(
    toolCallIdFromMcpExtra({
      _meta: { "claudecode/toolUseId": "toolu_provider_identity_123" },
    }),
    "toolu_provider_identity_123",
  );
});

test("partial SDK events preserve thinking and provider tool-call identity", () => {
  const notifications: Array<{
    method: string;
    params: Record<string, any>;
  }> = [];
  const rpc = {
    notify(method: string, params: Record<string, any>) {
      notifications.push({ method, params });
    },
  };
  const state = createPartialStreamState();
  const toolItems = new Map();
  const emit = (event: Record<string, unknown>) =>
    handlePartialStreamEvent({
      rpc: rpc as never,
      threadId: "thr_test",
      turnId: "turn-1",
      event,
      state,
      toolItems,
    });

  emit({
    type: "content_block_start",
    index: 0,
    content_block: { type: "thinking", thinking: "" },
  });
  emit({
    type: "content_block_delta",
    index: 0,
    delta: { type: "thinking_delta", thinking: "reason" },
  });
  emit({ type: "content_block_stop", index: 0 });
  emit({
    type: "content_block_start",
    index: 1,
    content_block: {
      type: "tool_use",
      id: "toolu_provider_123",
      name: "workshop_write",
      input: {},
    },
  });
  emit({
    type: "content_block_delta",
    index: 1,
    delta: { type: "input_json_delta", partial_json: '{"path":' },
  });
  emit({
    type: "content_block_delta",
    index: 1,
    delta: { type: "input_json_delta", partial_json: '"README.md"}' },
  });
  emit({ type: "content_block_stop", index: 1 });

  assert.deepEqual(
    notifications.map(({ method }) => method),
    [
      "item/started",
      "item/reasoning/delta",
      "item/completed",
      "item/started",
      "item/toolCall/argumentsDelta",
      "item/toolCall/argumentsDelta",
      "item/toolCall/argumentsCompleted",
    ],
  );
  assert.equal(notifications[1].params.delta, "reason");
  assert.equal(
    notifications[3].params.item.providerCallId,
    "toolu_provider_123",
  );
  assert.deepEqual(notifications[6].params, {
    threadId: "thr_test",
    turnId: "turn-1",
    itemId: "toolu_provider_123",
    callId: "toolu_provider_123",
    name: "workshop_write",
    arguments: { path: "README.md" },
    argumentsJson: '{"path":"README.md"}',
  });
});

test("new SDK sessions skip automatic model-generated titles", () => {
  assert.equal(titleForThread(thread()), "Hermes Agent");
  assert.equal(
    titleForThread(thread({ claudeSessionId: "claude-session-1" })),
    undefined,
  );
});

test("native compaction never moves system context into a user message", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-compact-"));
  const store = new ThreadStore(join(root, "threads.json"));
  const state = store.create({
    hostSessionId: "session-1",
    systemPromptAppend: "persistent system context",
    systemPromptIdentity: "SOUL",
    tools: [],
  });

  assert.equal(
    handleSystemMessage(store, state, {
      subtype: "compact_boundary",
      session_id: "claude-session-1",
    }),
    false,
  );
  assert.deepEqual(promptForTurn(state, "after compact"), {
    prompt: "after compact",
    includesHostContext: false,
  });
});
