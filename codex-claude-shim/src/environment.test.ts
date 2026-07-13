import assert from "node:assert/strict";
import test from "node:test";
import { sdkEnvironment } from "./environment.js";

test("SDK environment uses only the Claude credential store for Anthropic auth", () => {
  const environment = sdkEnvironment({
    CLAUDE_CONFIG_DIR: "/opt/data/.claude",
    HERMES_HOME: "/opt/data",
    ANTHROPIC_API_KEY: "api-key",
    ANTHROPIC_AUTH_TOKEN: "gateway-auth-token",
    ANTHROPIC_TOKEN: "other-oauth-account",
    CLAUDE_CODE_OAUTH_TOKEN: "setup-token",
    ANTHROPIC_BASE_URL: "https://metered.example.test",
    CLAUDE_CODE_USE_BEDROCK: "1",
    CLAUDE_CODE_USE_VERTEX: "1",
    CLAUDE_CODE_USE_FOUNDRY: "1",
    CLAUDE_CODE_USE_ANTHROPIC_AWS: "1",
    CLAUDE_CODE_USE_GATEWAY: "1",
    CLAUDE_CODE_USE_MANTLE: "1",
    CLAUDE_CODE_HOST_CREDS_FILE: "/run/provider-creds",
    ANTHROPIC_BEDROCK_BASE_URL: "https://bedrock.example.test",
    ANTHROPIC_FOUNDRY_AUTH_TOKEN: "foundry-token",
    AWS_BEARER_TOKEN_BEDROCK: "bedrock-token",
  });

  assert.deepEqual(environment, {
    CLAUDE_CONFIG_DIR: "/opt/data/.claude",
    HERMES_HOME: "/opt/data",
  });
});
