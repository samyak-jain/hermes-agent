import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { sdkEnvironment } from "./environment.js";

type Resolver = (specifier: string) => string;

/** Resolve the native Claude Code binary bundled for this SDK platform. */
export function nativeClaudeBinary(
  platform = process.platform,
  architecture = process.arch,
  resolve: Resolver = createRequire(import.meta.url).resolve,
): string {
  const packageName = `@anthropic-ai/claude-agent-sdk-${platform}-${architecture}`;
  const packageJson = resolve(`${packageName}/package.json`);
  return join(dirname(packageJson), platform === "win32" ? "claude.exe" : "claude");
}

/** Create a dedicated, refreshable Claude Code login in CLAUDE_CONFIG_DIR. */
export async function subscriptionLogin(): Promise<number> {
  const child = spawn(nativeClaudeBinary(), ["auth", "login"], {
    cwd: process.cwd(),
    env: sdkEnvironment(),
    stdio: "inherit",
  });
  return await new Promise<number>((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (signal) reject(new Error(`Claude login terminated by ${signal}`));
      else resolve(code ?? 1);
    });
  });
}
