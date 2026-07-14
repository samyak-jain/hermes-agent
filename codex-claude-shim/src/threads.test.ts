import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { ThreadStore } from "./threads.js";

test("host sessions resume their Claude session and original prompt snapshot", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-bridge-"));
  const persistence = join(root, "threads.json");
  const firstStore = new ThreadStore(persistence);
  const first = firstStore.create({
    hostSessionId: "session-1",
    cwd: "/workspace",
    model: "claude-fable-5",
    permissionMode: "bypassPermissions",
    systemPromptAppend: "original memory snapshot",
    systemPromptIdentity: "original soul",
    tools: [],
  });
  firstStore.bindClaudeSession(first, "claude-session-1");
  firstStore.markHostContextDelivered(first);

  const secondStore = new ThreadStore(persistence);
  const resumed = secondStore.create({
    hostSessionId: "session-1",
    cwd: "/workspace",
    model: "claude-fable-5",
    permissionMode: "bypassPermissions",
    systemPromptAppend: "new snapshot",
    systemPromptIdentity: "new soul",
    tools: [],
  });
  assert.equal(resumed.threadId, first.threadId);
  assert.equal(resumed.claudeSessionId, "claude-session-1");
  assert.equal(resumed.systemPromptAppend, "original memory snapshot");
  assert.equal(resumed.systemPromptIdentity, "original soul");
  assert.equal(resumed.hostContextDelivered, true);
  assert.equal(JSON.parse(readFileSync(persistence, "utf8")).version, 2);
});

test("version 1 threads start a fresh session for first-turn host context", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-bridge-v1-"));
  const persistence = join(root, "threads.json");
  writeFileSync(
    persistence,
    JSON.stringify({
      version: 1,
      threads: [
        {
          threadId: "thr_old",
          hostSessionId: "session-old",
          claudeSessionId: "claude-old",
          systemPromptAppend: "old full context",
        },
      ],
    }),
  );

  const store = new ThreadStore(persistence);
  const migrated = store.create({
    hostSessionId: "session-old",
    systemPromptAppend: "new full context",
    systemPromptIdentity: "SOUL",
    tools: [],
  });
  assert.equal(migrated.claudeSessionId, undefined);
  assert.equal(migrated.hostContextDelivered, false);
  assert.equal(migrated.systemPromptIdentity, "SOUL");
});
