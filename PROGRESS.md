# Workshop security-review remediation

Source: PR #68 `MERGE-AFTER-FIXES` review in the sibling review worktree. This file is the durable finding-by-finding record for the re-review.

## High-severity blockers

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| H1 slash-command escalation | Fixed | Workshop command exclusion is now structural in `MessageEvent.is_command()`: a Workshop event can never be a gateway slash command, independent of transport admission or how an alternate caller constructs the event. `transport_authorized` admits the bearer-authenticated request without operator authority, and `WorkshopAdapter.authorization_is_upstream` remains false. The real HTTP → adapter → gateway dispatch test covers `/restart`, `/yolo`, `/update`, and `/config ...` and asserts zero control-handler calls. |
| H2 handler return-contract mismatch | Fixed | Workshop now consumes the real `Optional[str]` return. `GatewayRunner` stamps a private structured outcome sidecar containing failure/interruption/usage facts; all workshop handler fakes now return `str | None`. Provider failure coverage asserts persisted `error` plus `turn.end{status:error}` with the user-facing message intact. |
| H3 route-factory startup coupling | Fixed | Route construction no longer reads credentials or opens the ledger. First workshop request lazily initializes both; failure logs `workshop_http_initialization_failed disabled=true` once and returns typed 503 while a shared `/health` remains 200. Generic route-shape/collision validation stays startup-fatal. |

## Medium findings

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| M1 external-tool platform gate | Fixed | Both event-metadata extraction and `_run_agent_inner` reject schemas/catalog/callback authority unless `source.platform == workshop`. A direct Discord runtime test proves fail-closed behavior before agent construction. |
| M2 terminal event on backlog exhaustion | Fixed | `fail_backlog_exhausted` atomically writes a bounded `error` + `turn.end{status:error}` pair even when normal quota is exhausted; ordinary writes remain strictly capped. Storage and SSE tests cover the terminal guarantee. |
| M3 hard duration deadline | Fixed | The 15-minute control deadline now has a fixed 5-second grace after interrupt, then cancels the handler task and persists `turn.end{status:aborted,stop_reason:turn_timeout}`. The test uses an uncooperative handler and a shortened grace. |
| M4 internal-turn cache churn | Fixed | A durable `workshop_chat_catalogs` row tracks the last admitted user catalog digest. Delta/wake turns reuse that digest for cached-agent identity while installing an empty schema list and no callback, so authority remains zero without alternating cache eviction. Existing wake retries pin their original turn digest. |
| M5 `after_seq` validation | Fixed | POST start and GET replay accept only bounded ASCII decimal non-negative sequences at HTTP ingress, reject values beyond SQLite's signed 64-bit range, return typed 400 for invalid/oversized values, and pass the parsed integer into the stream coordinator (no second raw parse). |

## Re-review at `39a7e85`

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| R1 stale remote-tool publication on delta turns | Fixed | The Workshop event sink now gates every `tool_call.start`, `tool_call.arguments.delta`, and `tool_call.end` against the current turn's authorized remote-name set before publishing anything to the DO or registering a pending call. Delta/wake turns retain the established catalog digest only for cache identity; their current authorized set remains empty. The real HTTP delta path drives the production Codex event bridge with a stale `writeFile` call and the production host-tool dispatcher: Hermes returns `isError`, no local dispatch occurs, no tool-call event reaches the SSE subscriber, and no call row is recorded. |
| R2 swallowed backlog exhaustion | Fixed | `WorkshopTurnCoordinator.emit_sync()` terminalizes atomically at the durable append failure site, publishes the replayable `error` + `turn.end{status:error}` pair, and wakes remote waiters before re-raising. This does not rely on propagation through `AIAgent`, whose production stream callback intentionally swallows sink exceptions. The adapter observes the already-terminal row after the handler unwinds and cannot append usage/completed. |
| R3 independently configurable command flag | Fixed | The removable `commands_enabled` flag is gone. Platform identity is the single authoritative property: `MessageEvent.is_command()` always returns false for Workshop. The test creates the event only through authenticated Workshop HTTP ingress and executes the real gateway command dispatcher. |

### Re-review mutation evidence

- R1: removing the current-catalog publication guard makes `test_delta_stale_remote_tool_is_denied_and_never_published` fail because stale `tool_call.*` records reach the DO-facing stream, while the independent Hermes host dispatcher still returns `isError`. This proves both defenses are covered separately.
- R2: replacing the drop-site terminalization with a plain ledger append makes the production-callback overflow test fail: the callback swallows the quota exception and the observed tail becomes `usage` + completed `turn.end` instead of `error` + error `turn.end`.
- R3: removing the Workshop platform exclusion makes all four real HTTP cases fail. `/restart`, `/yolo`, and `/update` enter their gateway control handlers; `/config ...` enters slash-command routing instead of the model path.
- The reviewer-requested raising-Codex case is retained: a `request_interrupt()` exception does not prevent Discord's legacy interrupt flag, message, or thread signal from being set. Existing cron coverage continues to assert its inline abort plus Codex-session signal.

## Post-merge fast-follow

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| N1 redacted Hermes-local activity | Fixed | Remote client tools retain executable `tool_call.*` events with complete arguments. Approved Hermes-local tools instead emit persisted `tool_activity` records constructed from exactly `{name, status}`. The runtime constructs no argument/result fields, and the Workshop adapter independently reconstructs rather than filters the payload. A real HTTP → adapter → Codex-event-bridge test sends a `memory` call with distinct secrets in its arguments and result and proves neither secret, nor `arguments`, `result`, or `call_id`, appears in SSE or ledger replay. Stale remote tools on zero-tool delta turns remain outside both the current remote catalog and approved local-name set, so R1 still suppresses them completely. |
| F1 bridge-layer redaction contract | Fixed | A bridge-only test now drives a secret-bearing local `memory` start/completion pair and asserts the exact two `tool_activity` payloads are `{name, status}`. It does not involve the Workshop adapter, so the bridge constructor is independently pinned against argument, result, call-ID, or future-field leakage. |
| N2 defensive platform identity | Fixed | `MessageEvent.is_command()` now accepts both enum-backed and plain-string platform identities without raising. Either representation of `workshop` remains structurally command-disabled; non-Workshop and source-less events retain the merged legacy behavior. |
| Oversized `after_seq` | Fixed | HTTP ingress rejects more than 19 digits and values above `2^63-1` before Python integer conversion or SQLite binding. Tests cover the 4,301-digit CPython limit and the SQLite overflow boundary on both POST and replay routes. |

N1 mutation evidence: replacing the adapter's `{name, status}` constructor with the incoming runtime payload makes `test_local_activity_reconstructs_instead_of_filtering_runtime_payload` fail on leaked `arguments`, `result`, `call_id`, and an unknown future sensitive field. The allowlist constructor was restored before the green run.

F1 mutation evidence: adding `"arguments": item.get("arguments")` directly to the bridge's completed `tool_activity` constructor makes `test_tool_activity_bridge_constructs_name_and_status_only` fail on the extra secret-bearing field. The mutation was removed; the bridge file passes all 8 tests and the expanded Workshop/Codex/gateway regression set passes **374 tests**.

Cross-repo dependency: Cloudflare OS `origin/main` at `24e87942` does not yet recognize `tool_activity` and currently rejects unknown Hermes event types. It must add a non-executable `tool_activity` projection to its existing generic activity UI before this Hermes branch is deployed. Mapping redacted activity back onto `tool_call.*` is forbidden because that path claims and executes DO tools.

Accepted v1 residuals, deliberately deferred until post-launch:

- Workshop initialization failure remains latched for the process lifetime; correcting credentials or a transient ledger failure requires a gateway restart.
- `workshop_chat_catalogs` is not pruned and can retain one row per workspace/chat indefinitely.

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

- Post-merge fast-follow focused suite: **331 passed**, covering every Workshop module, Codex live/external event projection, platform command parsing, and the real gateway command boundary. The canonical repository runner, repeated with its temporary directory on the unconstrained home filesystem, completed all 2,140 files with **43,766 passed**. The only six assertion failures are unchanged production-base behavior already recorded below: Bedrock default region (1), model-catalog expectations (2), loaded-host MoA timing (1), and restricted-PATH SSH fixtures (2). `test_provider_fallback.py` and `test_run_agent.py` reached the runner's 300-second per-file deadline after partial progress. All Workshop files, Codex integration, the 197-test platform-base file, MCP OAuth, Kanban, doctor, and every previously quota-contaminated file passed; no changed or adjacent surface failed. The runner retried and passed three unrelated load flakes (`test_25107_stale_base_url_api_mode.py`, `test_tui_gateway_server.py`, and `test_doctor.py`).

- Re-review R1/R2/R3 production-path regression set: **17 passed**. Canonical Workshop/gateway/Codex/interrupt adjacent set: **141 passed**; after the repository run exposed the source-less `MessageEvent` compatibility case, the expanded platform-base plus adjacent set passed **335 tests**. The three controlled production-code mutations above each make their corresponding regression test fail and were reverted before the green runs.
- Repository-wide runner at the re-review boundary: **43,705 passed across all 2,140 files**. It initially reported 21 assertions: 15 were a genuine R3 compatibility regression caused by dereferencing optional `MessageEvent.source`; the guard was corrected and the entire 194-test platform-base file plus every changed adjacent surface passed in the 335-test rerun. The six remaining assertions are unchanged production-base/environment behavior: Bedrock default region (1), model-catalog expectations (2), loaded-host MoA timing (1), and restricted-PATH SSH fixtures (2). No Workshop, Codex bridge, interrupt, or gateway command-boundary test failed. The unchanged oversized files `test_primary_runtime_restore.py`, `test_provider_fallback.py`, and `test_run_agent.py` reached the runner's 300-second per-file deadline after partial progress; `test_25107_stale_base_url_api_mode.py` did the same. `test_doctor.py` timed out once and passed all 74 tests on the runner's retry.

- Blocker-focused integration set: **63 passed** (`test_adapter`, `test_http`, `test_turns`, minimal real gateway command dispatch, shared API route composition).
- Medium-focused integration set: **70 passed** (storage, turn/SSE, HTTP ingress, platform policy/runtime).
- Low/shared-runtime Python set: **120 passed**. Claude shim: **22 passed**, including missing-provider-ID fail-closed behavior.
- Canonical adjacent regression set: **478 passed**. This includes Workshop protocol/auth/storage/HTTP/turn coordination, gateway policy/cache/session behavior, Codex runtime/shim integration, Discord and cron interrupt compatibility, and API route composition.
- Repository-wide run: **43,733 passed across 2,140 files**. The only assertion failures outside the established production-base files were the unchanged MoA wall-clock threshold (`test_references_run_in_parallel`); `git diff kumo...HEAD` is empty for both its implementation and test, and two isolated reruns reproduced the host-sensitive timing failure. The established production-base failures remain in `test_bedrock_integration.py` (1), `test_models.py` (2), and `test_ssh_environment.py` (3 in this environment).
- Two unchanged large `run_agent` files hit the parallel runner's per-file timeout after partial progress: `test_primary_runtime_restore.py` (23/36 completed) and `test_run_agent.py` (82/436 completed). The former reproduces as an environment-dependent hang in its existing Nous-provider initialization test; neither timed-out implementation/test surface is changed by the security remediation. No Workshop, gateway, interrupt, Codex shim, or policy test failed.
- Cross-repo wire reconciliation: **125 Workshop/API/command-boundary tests passed**, and the unmodified Cloudflare OS `scripts/hermes-wire-contract.test.ts` from `origin/main` passed against this worktree (**1/1**). The first local contract invocation used bare system Python and stopped during import because it lacked `aiohttp`; rerunning with the Hermes virtualenv on `PATH` exercised the real parser and passed.
