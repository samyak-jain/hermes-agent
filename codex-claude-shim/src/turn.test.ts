import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { HERMES_PERSONA_OUTPUT_STYLE } from "./persona.js";
import { ThreadStore, type ThreadState } from "./threads.js";
import {
  handleSystemMessage,
  outputStyleForThread,
  promptForTurn,
  systemPromptForThread,
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

test("SOUL remains in the preset append while full host context is delivered once", () => {
  const state = thread({
    systemPromptIdentity: "identity from SOUL",
    systemPromptAppend: "SOUL, AGENTS.md, memories and skill guidance",
  });

  assert.match(systemPromptForThread(state), /identity from SOUL/);
  assert.equal(
    outputStyleForThread(state, mkdtempSync(join(tmpdir(), "claude-style-"))),
    HERMES_PERSONA_OUTPUT_STYLE,
  );
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

test("threads without a SOUL keep the preset's default output style", () => {
  assert.equal(outputStyleForThread(thread()), undefined);
});

test("native compaction makes host context eligible for re-delivery", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-compact-"));
  const persistence = join(root, "threads.json");
  const store = new ThreadStore(persistence);
  const state = store.create({
    hostSessionId: "session-1",
    systemPromptAppend: "persistent host context",
    systemPromptIdentity: "SOUL",
    tools: [],
  });
  store.markHostContextDelivered(state);

  assert.equal(
    handleSystemMessage(store, state, {
      subtype: "compact_boundary",
      session_id: "claude-session-1",
    }),
    true,
  );
  assert.equal(state.hostContextDelivered, false);
  assert.equal(
    JSON.parse(readFileSync(persistence, "utf8")).threads[0].hostContextDelivered,
    false,
  );
  assert.equal(promptForTurn(state, "after compact").includesHostContext, true);
});
