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
  systemPromptForThread,
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
