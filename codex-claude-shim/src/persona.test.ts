import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  ensurePersonaOutputStyle,
  HERMES_PERSONA_OUTPUT_STYLE,
  HERMES_PERSONA_OUTPUT_STYLE_CONTENT,
} from "./persona.js";

test("persona output style replaces stale content with private permissions", () => {
  const root = mkdtempSync(join(tmpdir(), "claude-persona-"));
  const styles = join(root, "output-styles");
  const target = join(styles, `${HERMES_PERSONA_OUTPUT_STYLE}.md`);

  assert.equal(ensurePersonaOutputStyle(root), HERMES_PERSONA_OUTPUT_STYLE);
  assert.equal(readFileSync(target, "utf8"), HERMES_PERSONA_OUTPUT_STYLE_CONTENT);
  assert.equal(statSync(target).mode & 0o777, 0o600);
  assert.equal(statSync(styles).mode & 0o777, 0o700);

  writeFileSync(target, "stale");
  assert.equal(ensurePersonaOutputStyle(root), HERMES_PERSONA_OUTPUT_STYLE);
  assert.equal(readFileSync(target, "utf8"), HERMES_PERSONA_OUTPUT_STYLE_CONTENT);
  assert.doesNotMatch(readFileSync(target, "utf8"), /keep-coding-instructions/);
});
