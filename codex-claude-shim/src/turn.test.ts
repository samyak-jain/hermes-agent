import assert from "node:assert/strict";
import test from "node:test";
import { promptForTurn, systemPromptForThread } from "./turn.js";
import type { ThreadState } from "./threads.js";

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

test("SOUL remains in the preset append while full host context is delivered once", () => {
  const state = thread({
    systemPromptIdentity: "identity from SOUL",
    systemPromptAppend: "SOUL, AGENTS.md, memories and skill guidance",
  });

  assert.match(systemPromptForThread(state), /identity from SOUL/);
  const first = promptForTurn(state, "hello");
  assert.equal(first.includesHostContext, true);
  assert.match(first.prompt, /<host_context>/);
  assert.match(first.prompt, /AGENTS\.md, memories/);

  state.hostContextDelivered = true;
  assert.deepEqual(promptForTurn(state, "next"), {
    prompt: "next",
    includesHostContext: false,
  });
});
