import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { SYSTEM_PROMPT_DYNAMIC_BOUNDARY } from "@anthropic-ai/claude-agent-sdk";
import { ThreadStore, type ThreadState } from "./threads.js";
import {
  handleSystemMessage,
  mcpServer,
  promptForTurn,
  runTurn,
  systemPromptForThread,
  titleForThread,
  tokenUsageForTurn,
  usageFromSdkMessage,
} from "./turn.js";

const thread = (overrides: Partial<ThreadState> = {}): ThreadState => ({
  threadId: "thr_test",
  tools: [],
  usageTotal: {
    inputTokens: 0,
    cachedInputTokens: 0,
    cacheCreationInputTokens: 0,
    outputTokens: 0,
    reasoningOutputTokens: 0,
    totalTokens: 0,
  },
  ...overrides,
});

test("runTurn publishes final assistant usage as last and aggregate usage as turn", async () => {
  const root = mkdtempSync(join(tmpdir(), "claude-usage-"));
  const store = new ThreadStore(join(root, "threads.json"));
  const state = store.create({ tools: [] });
  const notifications: Array<{ method: string; params: any }> = [];
  const rpc = {
    notify: (method: string, params: any) => notifications.push({ method, params }),
    rejectPendingOutboundForTurn: () => undefined,
  };
  const messages = [
    {
      type: "assistant",
      message: {
        usage: {
          input_tokens: 30,
          cache_read_input_tokens: 20,
          cache_creation_input_tokens: 10,
          output_tokens: 5,
        },
        content: [{ type: "text", text: "done" }],
      },
    },
    {
      type: "result",
      is_error: false,
      usage: {
        input_tokens: 300,
        cache_read_input_tokens: 200,
        cache_creation_input_tokens: 100,
        output_tokens: 50,
      },
      modelUsage: { claude: { contextWindow: 200_000 } },
    },
  ];
  const queryFn = (() =>
    (async function* () {
      yield* messages;
    })()) as unknown as typeof import("@anthropic-ai/claude-agent-sdk").query;

  await runTurn({
    rpc: rpc as never,
    threads: store,
    thread: state,
    turnId: "turn-usage",
    userText: "hello",
    queryFn,
  });

  const usage = notifications.find(
    (entry) => entry.method === "thread/tokenUsage/updated",
  )?.params.tokenUsage;
  assert.equal(usage.last.totalTokens, 65);
  assert.equal(usage.turn.totalTokens, 650);
  assert.equal(usage.total.totalTokens, 650);
});

test("compaction fails without an observed compact boundary", async () => {
  const root = mkdtempSync(join(tmpdir(), "claude-compact-unconfirmed-"));
  const store = new ThreadStore(join(root, "threads.json"));
  const state = store.create({ tools: [] });
  const notifications: Array<{ method: string; params: any }> = [];
  const rpc = {
    notify: (method: string, params: any) => notifications.push({ method, params }),
    rejectPendingOutboundForTurn: () => undefined,
  };
  const queryFn = (() =>
    (async function* () {
      yield { type: "result", is_error: false, usage: {} };
    })()) as unknown as typeof import("@anthropic-ai/claude-agent-sdk").query;

  const result = await runTurn({
    rpc: rpc as never,
    threads: store,
    thread: state,
    turnId: "turn-compact",
    userText: "/compact",
    compaction: true,
    queryFn,
  });

  assert.equal(result.status, "failed");
  assert.match(result.error ?? "", /without a compact boundary/);
  assert.equal(
    notifications.some((entry) => entry.method === "thread/compacted"),
    false,
  );
});

test("SDK usage includes cache creation tokens in context occupancy", () => {
  assert.deepEqual(
    usageFromSdkMessage({
      input_tokens: 11,
      cache_read_input_tokens: 13,
      cache_creation_input_tokens: 17,
      output_tokens: 19,
    }),
    {
      inputTokens: 11,
      cachedInputTokens: 13,
      cacheCreationInputTokens: 17,
      outputTokens: 19,
      reasoningOutputTokens: 0,
      totalTokens: 60,
    },
  );
});

test("token usage keeps aggregate billing separate from final request occupancy", () => {
  const aggregate = usageFromSdkMessage({
    input_tokens: 300,
    cache_read_input_tokens: 200,
    cache_creation_input_tokens: 100,
    output_tokens: 50,
  });
  const last = usageFromSdkMessage({
    input_tokens: 30,
    cache_read_input_tokens: 20,
    cache_creation_input_tokens: 10,
    output_tokens: 5,
  });
  const total = { ...aggregate };

  assert.deepEqual(tokenUsageForTurn(aggregate, last, total, 200_000), {
    last,
    turn: aggregate,
    total,
    modelContextWindow: 200_000,
  });
  assert.equal(last.totalTokens, 65);
  assert.equal(aggregate.totalTokens, 650);
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
    true,
  );
  assert.deepEqual(promptForTurn(state, "after compact"), {
    prompt: "after compact",
    includesHostContext: false,
  });
});
