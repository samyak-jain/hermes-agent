import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
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
    tools: [],
  });
  firstStore.bindClaudeSession(first, "claude-session-1");

  const secondStore = new ThreadStore(persistence);
  const resumed = secondStore.create({
    hostSessionId: "session-1",
    cwd: "/workspace",
    model: "claude-fable-5",
    permissionMode: "bypassPermissions",
    systemPromptAppend: "new snapshot",
    tools: [],
  });
  assert.equal(resumed.threadId, first.threadId);
  assert.equal(resumed.claudeSessionId, "claude-session-1");
  assert.equal(resumed.systemPromptAppend, "original memory snapshot");
  assert.equal(JSON.parse(readFileSync(persistence, "utf8")).version, 1);
});
