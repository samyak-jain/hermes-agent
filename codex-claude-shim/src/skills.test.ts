import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { ensureRuntimeSkillsVisible } from "./skills.js";

test("skills use the durable real home when Hermes virtualizes subprocess HOME", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-skills-"));
  try {
    const runtimeHome = join(root, "runtime");
    const realHome = join(root, "real-home");
    mkdirSync(join(runtimeHome, "skills"), { recursive: true });

    ensureRuntimeSkillsVisible({
      HERMES_HOME: runtimeHome,
      HERMES_REAL_HOME: realHome,
      HOME: join(runtimeHome, "home"),
    });

    assert.equal(
      readlinkSync(join(realHome, ".claude", "skills")),
      join(runtimeHome, "skills"),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
