import { query } from "@anthropic-ai/claude-agent-sdk";
import { sdkEnvironment } from "./environment.js";

interface UsageWindow {
  utilization?: number | null;
  resets_at?: string | null;
}

function usageWindow(raw: UsageWindow | null | undefined) {
  if (!raw) return null;
  return {
    utilization: raw.utilization ?? null,
    resetsAt: raw.resets_at ?? null,
  };
}

/** Select the non-secret fields that operators need from Claude's usage API. */
export function sanitizeSubscriptionUsage(usage: any) {
  const limits = usage?.rate_limits ?? {};
  return {
    subscriptionType: usage?.subscription_type ?? null,
    rateLimitsAvailable: Boolean(usage?.rate_limits_available),
    fiveHour: usageWindow(limits.five_hour),
    sevenDay: usageWindow(limits.seven_day),
    oauthApps: usageWindow(limits.seven_day_oauth_apps),
    modelScoped: Array.isArray(limits.model_scoped)
      ? limits.model_scoped.map((entry: any) => ({
          displayName: String(entry?.display_name ?? ""),
          utilization: entry?.utilization ?? null,
          resetsAt: entry?.resets_at ?? null,
        }))
      : [],
    extraUsage: limits.extra_usage
      ? {
          enabled: Boolean(limits.extra_usage.is_enabled),
          utilization: limits.extra_usage.utilization ?? null,
        }
      : null,
  };
}

/** Return non-secret Claude subscription state without making a model call. */
export async function subscriptionStatus() {
  const runningQuery = query({
    prompt: "",
    options: {
      cwd: process.cwd(),
      tools: [],
      // Credentials are loaded independently of settings. Filesystem settings
      // may contain env/apiKeyHelper overrides, so isolate this diagnostic from
      // every settings tier just like the main subscription-only turn.
      settingSources: [],
      env: sdkEnvironment(),
    },
  });

  try {
    const usage: any =
      await runningQuery.usage_EXPERIMENTAL_MAY_CHANGE_DO_NOT_RELY_ON_THIS_API_YET();
    return sanitizeSubscriptionUsage(usage);
  } finally {
    runningQuery.close();
  }
}
