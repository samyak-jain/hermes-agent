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
| L1 non-ASCII bearer 500 | Pending | Compare encoded bytes and return 401 without a traceback. |
| L2 collision snapshot fails open | Pending | Make registry snapshot failure fatal to external-schema construction. |
| L3 root config policy over-match | Pending | Restrict legacy root-level platform-policy scanning to registered/known platform names. |
| L4 lane-lock growth | Pending | Reference-count or evict idle lane locks without weakening same-chat serialization. |
| L5 silent provider-ID fallback | Pending | Fail loudly when a workshop-origin callback lacks the provider tool-use ID; preserve fallback only for non-workshop host tools if required. |
| L6 dead caller metadata | Pending | Remove unsupported caller metadata from protocol ingress rather than carrying an unused authority/data surface. |
| L7 asynchronous retired-shim close | Pending | Make replacement wait for the retired app-server close on the signature-miss path, or document a safe bounded alternative if the gateway event-loop contract prevents it. |
| L8 invisible-Unicode delta rejection | Pending | No code change planned: approved fail-closed behavior. Add an operational note for the DO integration contract and typed-error handling. |

## Shared runtime / fork contract

| Finding | State | Evidence / disposition |
| --- | --- | --- |
| Codex app-server interrupt propagation | Pending | Add explicit Discord and cron-path tests proving the shared `AIAgent.interrupt()` propagation changes only runtime effectiveness, not routing, authorization, or turn semantics. If the behavior itself differs from the documented fork contract, record it as deliberate and flag it for the orchestrator. |

## Verification log

- Blocker-focused integration set: **63 passed** (`test_adapter`, `test_http`, `test_turns`, minimal real gateway command dispatch, shared API route composition).
- Medium-focused integration set: **70 passed** (storage, turn/SSE, HTTP ingress, platform policy/runtime).
- Low remediation and full-suite rerun remain pending.
