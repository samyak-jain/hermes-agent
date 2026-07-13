import { existsSync, lstatSync, mkdirSync, symlinkSync } from "node:fs";
import { dirname, join } from "node:path";

type RuntimeEnvironment = Record<string, string | undefined>;

export function ensureRuntimeSkillsVisible(
  environment: RuntimeEnvironment = process.env,
): void {
  const runtimeHome = environment.HERMES_HOME ?? environment.HOME;
  // Hermes deliberately virtualizes HOME for coding subprocesses. Claude's
  // durable config still lives under HERMES_REAL_HOME, so use the same root
  // for its native skills directory instead of the isolated subprocess HOME.
  const claudeHome = environment.HERMES_REAL_HOME ?? environment.HOME;
  if (!runtimeHome || !claudeHome) return;
  const source = join(runtimeHome, "skills");
  const destination = join(claudeHome, ".claude", "skills");
  if (!existsSync(source)) return;
  mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
  if (existsSync(destination)) {
    if (lstatSync(destination).isSymbolicLink()) return;
    return;
  }
  symlinkSync(source, destination, "dir");
}
