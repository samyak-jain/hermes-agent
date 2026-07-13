import assert from "node:assert/strict";
import test from "node:test";
import { nativeClaudeBinary } from "./login.js";

test("native Claude login resolves the SDK platform package", () => {
  const requested: string[] = [];
  const binary = nativeClaudeBinary("linux", "arm64", (specifier) => {
    requested.push(specifier);
    return "/opt/hermes/node_modules/@anthropic-ai/claude-agent-sdk-linux-arm64/package.json";
  });

  assert.deepEqual(requested, [
    "@anthropic-ai/claude-agent-sdk-linux-arm64/package.json",
  ]);
  assert.equal(
    binary,
    "/opt/hermes/node_modules/@anthropic-ai/claude-agent-sdk-linux-arm64/claude",
  );
});
