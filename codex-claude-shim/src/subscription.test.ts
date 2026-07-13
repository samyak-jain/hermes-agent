import assert from "node:assert/strict";
import test from "node:test";
import { sanitizeSubscriptionUsage } from "./subscription.js";

test("subscription status omits billing details and OAuth credentials", () => {
  const status = sanitizeSubscriptionUsage({
    subscription_type: "pro",
    rate_limits_available: true,
    access_token: "must-not-leak",
    rate_limits: {
      five_hour: { utilization: 12, resets_at: "five-hour-reset" },
      seven_day: { utilization: 34, resets_at: "weekly-reset" },
      seven_day_oauth_apps: null,
      model_scoped: [
        { display_name: "Fable", utilization: 9, resets_at: "fable-reset" },
      ],
      extra_usage: {
        is_enabled: false,
        utilization: 0,
        monthly_limit: 3000,
        used_credits: 0,
      },
    },
  });

  assert.deepEqual(status, {
    subscriptionType: "pro",
    rateLimitsAvailable: true,
    fiveHour: { utilization: 12, resetsAt: "five-hour-reset" },
    sevenDay: { utilization: 34, resetsAt: "weekly-reset" },
    oauthApps: null,
    modelScoped: [
      { displayName: "Fable", utilization: 9, resetsAt: "fable-reset" },
    ],
    extraUsage: { enabled: false, utilization: 0 },
  });
  assert.equal(JSON.stringify(status).includes("must-not-leak"), false);
  assert.equal(JSON.stringify(status).includes("monthly_limit"), false);
});
