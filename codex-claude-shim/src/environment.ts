const CLAUDE_SUBSCRIPTION_OVERRIDE_KEYS = new Set([
  // Direct Anthropic, gateway and host-managed credentials.
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "ANTHROPIC_TOKEN",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
  "CLAUDE_CODE_HOST_AUTH_REFRESH_TIMEOUT_MS",
  "CLAUDE_CODE_HOST_CREDS_FILE",
  "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
  "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",

  // Custom endpoints can change both the credential and billing authority.
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_AWS_BASE_URL",
  "ANTHROPIC_BEDROCK_BASE_URL",
  "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
  "ANTHROPIC_FOUNDRY_BASE_URL",
  "ANTHROPIC_VERTEX_BASE_URL",
  "CLAUDE_CODE_ARTIFACTS_API_BASE_URL",

  // Alternate provider selectors and their authentication shortcuts.
  "CLAUDE_CODE_USE_BEDROCK",
  "CLAUDE_CODE_USE_VERTEX",
  "CLAUDE_CODE_USE_FOUNDRY",
  "CLAUDE_CODE_USE_ANTHROPIC_AWS",
  "CLAUDE_CODE_USE_GATEWAY",
  "CLAUDE_CODE_USE_MANTLE",
  "CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH",
  "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
  "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
  "CLAUDE_CODE_SKIP_MANTLE_AUTH",
  "CLAUDE_CODE_SKIP_VERTEX_AUTH",
  "ANTHROPIC_AWS_API_KEY",
  "ANTHROPIC_AWS_WORKSPACE_ID",
  "ANTHROPIC_BEDROCK_MANTLE_API_KEY",
  "ANTHROPIC_FOUNDRY_API_KEY",
  "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
  "ANTHROPIC_FOUNDRY_RESOURCE",
  "ANTHROPIC_VERTEX_PROJECT_ID",
  "AWS_BEARER_TOKEN_BEDROCK",
  "CLOUD_ML_REGION",
]);

/**
 * Build the environment for the Claude Code child.
 *
 * The adapter intentionally authenticates only from the mutable Claude Code
 * credential store selected by CLAUDE_CONFIG_DIR. Hermes can carry unrelated
 * provider credentials and routing flags for side tasks; allowing any of
 * those through can silently move the main turn to API billing, a different
 * OAuth account, or an alternate cloud backend.
 */
export function sdkEnvironment(
  source: Record<string, string | undefined> = process.env,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(source).filter(
      (entry): entry is [string, string] =>
        entry[1] !== undefined && !CLAUDE_SUBSCRIPTION_OVERRIDE_KEYS.has(entry[0]),
    ),
  );
}
