import assert from "node:assert/strict";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readlinkSync,
  rmSync,
  symlinkSync,
} from "node:fs";
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

test("a dangling Claude skills symlink is repaired without crashing startup", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-skills-dangling-"));
  try {
    const runtimeHome = join(root, "runtime");
    const realHome = join(root, "real-home");
    mkdirSync(join(runtimeHome, "skills"), { recursive: true });
    mkdirSync(join(realHome, ".claude"), { recursive: true });
    const destination = join(realHome, ".claude", "skills");
    symlinkSync(join(root, "missing"), destination, "dir");

    assert.doesNotThrow(() =>
      ensureRuntimeSkillsVisible({
        HERMES_HOME: runtimeHome,
        HERMES_REAL_HOME: realHome,
        HOME: join(runtimeHome, "home"),
      }),
    );
    assert.equal(existsSync(destination), true);
    assert.equal(readlinkSync(destination), join(runtimeHome, "skills"));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("an empty real-home marker falls back from the virtualized subprocess home", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-skills-empty-real-home-"));
  try {
    const runtimeHome = join(root, "runtime");
    mkdirSync(join(runtimeHome, "skills"), { recursive: true });

    ensureRuntimeSkillsVisible({
      HERMES_HOME: runtimeHome,
      HERMES_REAL_HOME: "",
      HOME: join(runtimeHome, "home"),
    });

    assert.equal(
      readlinkSync(join(runtimeHome, ".claude", "skills")),
      join(runtimeHome, "skills"),
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
