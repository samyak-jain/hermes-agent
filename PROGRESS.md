# Workshop security-review remediation

Source: PR #68 `MERGE-AFTER-FIXES` review in the sibling review worktree. This file is the durable finding-by-finding record for the re-review.

## High-severity blockers

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| H1 slash-command escalation | Fixed | `MessageEvent.commands_enabled=False` makes every leading-slash workshop input ordinary model content; `transport_authorized` admits the bearer-authenticated request without operator authority, and `WorkshopAdapter.authorization_is_upstream` is now false. The real gateway dispatch test covers `/restart`, `/yolo`, `/update`, and `/config ...` and asserts zero control-handler calls. |
| H2 handler return-contract mismatch | Fixed | Workshop now consumes the real `Optional[str]` return. `GatewayRunner` stamps a private structured outcome sidecar containing failure/interruption/usage facts; all workshop handler fakes now return `str | None`. Provider failure coverage asserts persisted `error` plus `turn.end{status:error}` with the user-facing message intact. |
| H3 route-factory startup coupling | Fixed | Route construction no longer reads credentials or opens the ledger. First workshop request lazily initializes both; failure logs `workshop_http_initialization_failed disabled=true` once and returns typed 503 while a shared `/health` remains 200. Generic route-shape/collision validation stays startup-fatal. |

## Medium findings

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| M1 external-tool platform gate | Fixed | Both event-metadata extraction and `_run_agent_inner` reject schemas/catalog/callback authority unless `source.platform == workshop`. A direct Discord runtime test proves fail-closed behavior before agent construction. |
| M2 terminal event on backlog exhaustion | Fixed | `fail_backlog_exhausted` atomically writes a bounded `error` + `turn.end{status:error}` pair even when normal quota is exhausted; ordinary writes remain strictly capped. Storage and SSE tests cover the terminal guarantee. |
| M3 hard duration deadline | Fixed | The 15-minute control deadline now has a fixed 5-second grace after interrupt, then cancels the handler task and persists `turn.end{status:aborted,stop_reason:turn_timeout}`. The test uses an uncooperative handler and a shortened grace. |
| M4 internal-turn cache churn | Fixed | A durable `workshop_chat_catalogs` row tracks the last admitted user catalog digest. Delta/wake turns reuse that digest for cached-agent identity while installing an empty schema list and no callback, so authority remains zero without alternating cache eviction. Existing wake retries pin their original turn digest. |
| M5 `after_seq` validation | Fixed | POST start and GET replay accept only ASCII decimal non-negative sequences at HTTP ingress, return typed 400 for invalid values, and pass the parsed integer into the stream coordinator (no second raw parse). |

## Low findings

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| L1 non-ASCII bearer 500 | Fixed | Authenticator compares UTF-8 bytes, so arbitrary Unicode bearer input returns false/401 without `compare_digest` raising. |
| L2 collision snapshot fails open | Fixed | Registry enumeration failure now aborts app-server tool-surface construction; a test forces the snapshot exception while an external `terminal` schema is present. |
| L3 root config policy over-match | Fixed | Legacy root scanning is restricted to enum, bundled-plugin, and runtime-registered platform names. A malformed global `agent.tool_policy` now produces exactly its one authoritative error. |
| L4 lane-lock growth | Fixed | Lane state reference-counts holders/waiters and evicts itself after the final release. Tests preserve same-lane serialization and drain 100 unique caller-controlled lanes to zero entries. |
| L5 silent provider-ID fallback | Fixed | The shim no longer invents a random host-tool ID. Missing/malformed SDK metadata becomes an MCP error result before any Hermes/workshop callback, while exact provider ID coverage remains. |
| L6 caller metadata / cross-repo wire contract | Reconciled | Cloudflare OS `origin/main` (`eb919686`) always sends `metadata: {}` from `makeHermesTurnRequest`, matching the approved B1 display-metadata contract. Hermes accepts only a JSON object bounded to 16 KiB, depth 4, 128 nodes, 64 items/fields per collection, 4 KiB strings, and 128-byte keys. It participates in the idempotency digest but is never forwarded into `MessageEvent`, prompt context, tool policy, or instructions. |
| L7 asynchronous retired-shim close | Fixed | Signature-miss eviction is separated from cross-process stale eviction. On the off-loop agent worker, the signature-retired client is synchronously soft-released before replacement construction; an integration test records `release` before `init`. Cross-process cleanup remains asynchronous to preserve heartbeat safety. |
| L8 invisible-Unicode delta rejection | Documented | Approved fail-closed behavior is retained. `WORKSHOP_PLATFORM_PLAN.md` now requires the DO to treat rejection as permanent for that `delta_id`, sanitize/reframe content, and allocate a new identity. |

## Shared runtime / fork contract

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| Codex app-server interrupt propagation | Covered; pre-deploy smoke pending | Explicit tests prove Discord retains the legacy flags/message while signaling Codex, and cron retains `_active_request_abort("interrupt_abort")` while also signaling Codex. This is documented in `WORKSHOP_PLATFORM_PLAN.md` as a deliberate Kumo fork-contract effectiveness change. The orchestrator must mirror that note into external `hermes-fork.md` and run one live Discord interrupt plus one cron turn before rollout. |

## Verification log

- Blocker-focused integration set: **63 passed** (`test_adapter`, `test_http`, `test_turns`, minimal real gateway command dispatch, shared API route composition).
- Medium-focused integration set: **70 passed** (storage, turn/SSE, HTTP ingress, platform policy/runtime).
- Low/shared-runtime Python set: **120 passed**. Claude shim: **22 passed**, including missing-provider-ID fail-closed behavior.
- Canonical adjacent regression set: **478 passed**. This includes Workshop protocol/auth/storage/HTTP/turn coordination, gateway policy/cache/session behavior, Codex runtime/shim integration, Discord and cron interrupt compatibility, and API route composition.
- Repository-wide run: **43,733 passed across 2,140 files**. The only assertion failures outside the established production-base files were the unchanged MoA wall-clock threshold (`test_references_run_in_parallel`); `git diff kumo...HEAD` is empty for both its implementation and test, and two isolated reruns reproduced the host-sensitive timing failure. The established production-base failures remain in `test_bedrock_integration.py` (1), `test_models.py` (2), and `test_ssh_environment.py` (3 in this environment).
- Two unchanged large `run_agent` files hit the parallel runner's per-file timeout after partial progress: `test_primary_runtime_restore.py` (23/36 completed) and `test_run_agent.py` (82/436 completed). The former reproduces as an environment-dependent hang in its existing Nous-provider initialization test; neither timed-out implementation/test surface is changed by the security remediation. No Workshop, gateway, interrupt, Codex shim, or policy test failed.
- Cross-repo wire reconciliation: **125 Workshop/API/command-boundary tests passed**, and the unmodified Cloudflare OS `scripts/hermes-wire-contract.test.ts` from `origin/main` passed against this worktree (**1/1**). The first local contract invocation used bare system Python and stopped during import because it lacked `aiohttp`; rerunning with the Hermes virtualenv on `PATH` exercised the real parser and passed.
