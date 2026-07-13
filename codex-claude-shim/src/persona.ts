import {
  chmodSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const HERMES_PERSONA_OUTPUT_STYLE = "hermes-persona";

export const HERMES_PERSONA_OUTPUT_STYLE_CONTENT = `---
description: Follow the operator-defined Hermes persona
---

The host appends an operator-defined persona to this system prompt. For every
user-facing response, follow that persona's voice, register, expressiveness,
emotional presence, nicknames, stage directions, and emoji. Do not flatten it
into a default terse, professional, or emotionally restrained assistant voice.

Safety, honesty, task requirements, tool-use discipline, and code correctness
remain in force.
`;

function claudeConfigDir(override?: string): string {
  return override ?? process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude");
}

/**
 * Install the host-owned output style selected for persona-bearing threads.
 *
 * `settingSources: []` keeps filesystem settings isolated, but Claude Code
 * still discovers output styles from CLAUDE_CONFIG_DIR. The explicit SDK
 * `settings` option selects this file without loading settings.json.
 */
export function ensurePersonaOutputStyle(configDir?: string): string {
  const directory = join(claudeConfigDir(configDir), "output-styles");
  const target = join(directory, `${HERMES_PERSONA_OUTPUT_STYLE}.md`);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);

  try {
    if (readFileSync(target, "utf8") === HERMES_PERSONA_OUTPUT_STYLE_CONTENT) {
      chmodSync(target, 0o600);
      return HERMES_PERSONA_OUTPUT_STYLE;
    }
  } catch {
    // Missing or unreadable files are replaced atomically below.
  }

  const temporary = `${target}.tmp-${process.pid}`;
  writeFileSync(temporary, HERMES_PERSONA_OUTPUT_STYLE_CONTENT, {
    encoding: "utf8",
    mode: 0o600,
  });
  chmodSync(temporary, 0o600);
  renameSync(temporary, target);
  return HERMES_PERSONA_OUTPUT_STYLE;
}
