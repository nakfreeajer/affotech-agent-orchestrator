# Orchestrator Rollover Transition-Authority Audit

Date: 2026-09-26
Repository: `nakfreeajer/affotech-agent-orchestrator`
Audited commit: `40adcb9d64a26313137fd6946409f57f9efd940e`
Scope: model and authority audit only; no runtime or test implementation is changed by this document.

## 1. Authority boundary and finding

The authoritative production boundary remains:

```text
state=NEXT_PROMPT_READY
taskId=000102
lastCompletedTaskId=000102
nextTaskId=000103
```

The preserved in-flight compatibility transaction is:

```text
transactionId=7fdbd798659f42295a18dd2d
taskId=000103
```

It is the only pre-upgrade legacy handover exception. It is transport/protocol evidence, not a general scheduling exception.

The architecture finding identifies two independent interpretations of the same durable state:

1. `deferred_rollover_passive_wait_required()` (lines 7281-7296) can return `WAIT` for a deferred rollover while the automatic epoch deadline is already due.
2. `ArchitectSessionRollover._automatic_recovery_epoch_eligible()` (lines 1662-1696) can return eligible once the same deadline has expired and the finite epoch budget and ownership guards pass.

`run_next_prompt_ready_once()` (lines 7402-7416) evaluates the passive predicate before calling `dispatch_next_prompt_once()`. The latter is the production path that invokes `service_deferred_rollover_once()` (lines 7216-7278). Thus the passive decision can make the recovery decision unreachable. The problem is duplicated transition authority, not merely a missing cooldown comparison.

The existing tests also need a future journey correction: the expired-cooldown passive-wait test asserts the wrong authority, and the self-rearm test advances time but bypasses the public `NEXT_PROMPT_READY` entry path.

## 2. Transition-authority map

### 2.1 Production call graph

```text
main() [7563+]
  └─ state == NEXT_PROMPT_READY
       └─ run_next_prompt_ready_once() [7402]
            ├─ deferred_rollover_passive_wait_required() [7281]
            │    └─ passive_deferred_rollover_wait() [7316]
            │         └─ sleep + reload + return
            └─ dispatch_next_prompt_once() [7216]
                 ├─ service_deferred_rollover_once() [6793]
                 │    ├─ _automatic_recovery_epoch_eligible() [1662]
                 │    ├─ terminal transaction retire/revive
                 │    ├─ pre-budget handover reconciliation
                 │    ├─ _begin_bounded_recovery() [1599]
                 │    ├─ request_if_due() [1929]
                 │    ├─ reconcile_pending_handover() [2187+]
                 │    └─ fresh Architect/recovery completion
                 └─ launch_next() only after rollover-complete gates pass
```

### 2.2 Decision and mutation sites

| Decision or transition | Current authority | Material behavior | Future status |
|---|---|---|---|
| `WAIT` for deferred rollover | `deferred_rollover_passive_wait_required()`; `passive_deferred_rollover_wait()` | Blocks the public entry path and sleeps/reloads. It does not own automatic epoch eligibility. | Must become a thin adapter over the canonical evaluator. |
| `WAIT_COOLDOWN` for reconciliation | `reconciliation_backoff_wait_required()` and the `WAIT_BACKOFF` branch in `service_deferred_rollover_once()` | Uses `rolloverRecoveryRetryAfter`; passive helper may sleep. | One evaluator action with an explicit future deadline. |
| `WAIT_DISCUSSION` | `main()` discussion branch (7660-7676), `service_deferred_rollover_once()` pause guard (6799-6801) | Blocks workflow mutation while paused and polls. | Canonical evaluator observation, no rollover mutation. |
| `WAIT_EXECUTOR` | `dispatch_next_prompt_once()` and `request_if_due()`; service has an active PID guard (6869-6873) | Prevents handover/launch while governed Executor is live. | One ownership observation and one wait action. |
| `WAIT_ACTIVE_WRITER` | `_automatic_recovery_epoch_eligible()` (1694-1695), Executor/session recovery guards | Blocks recovery when a governed writer is active. | Canonical ownership precedence over cooldown/retry. |
| `WAIT_ARCHITECT` | `request_if_due()` (`architect_generating`), service generation-visible probe, main Architect loops | Avoids sending or reconciling while generation is active. | Explicit observation/action. |
| `RETRY` / attempt budget | `_begin_bounded_recovery()` (1599-1640) | Starts or advances a bounded recovery attempt; returns `WAIT_BACKOFF`, `ATTEMPT_ALLOWED`, or `EXHAUSTED`. | Evaluator selects `START_RECOVERY_ATTEMPT` only. |
| `RECOVER` / epoch budget | `_automatic_recovery_epoch_eligible()` and `_begin_automatic_recovery_epoch()` (1698-1730) | Checks deadline, task ownership, max epochs, PID/writer guards; resets per-epoch counters without discarding evidence. | Canonical evaluator owns eligibility. |
| `REQUEST_HANDOVER` | `ArchitectSessionRollover.request_if_due()` (1929-2160) | Creates/reuses transaction identity, sets pending flags, emits canonical protocol v1 request, records send state. | Execution adapter for evaluator action only. |
| `RECONCILE_HANDOVER` | `service_deferred_rollover_once()` pre-budget probe (6961-7000), `reconcile_pending_handover()`, `process_pending_handover_response()` | Reads and validates an existing response before another recovery/send action. | Must precede budget exhaustion when observations prove a live transaction. |
| `RETIRE_TRANSACTION` | `_retire_terminal_same_task_transaction()`; `_retire_stale_transaction_for_task()`; `request_if_due()` cleanup | Clears selected transaction evidence and records retired identity. | One canonical retirement transition with proof. |
| `REVIVE_TRANSACTION` | `_revive_terminal_delivered_transaction()` | Reopens a terminal transaction when exact delivered payload evidence is proven. | One canonical revival transition. |
| `CREATE_FRESH_ARCHITECT` | `open_fresh_with_handover()` and `_existing_fresh_candidate_page()` paths | Creates or reuses the next Architect candidate after exact handover proof. | Only after evaluator approval. |
| `COMMIT_NEW_ARCHITECT_AUTHORITY` | `complete_from_response()` and fresh readiness paths | Saves the new conversation authority before retiring old browser authority. | Ordered, observable transition. |
| `HUMAN_REQUIRED` | Budget exhaustion, safety cutout, handover/result/envelope failure paths | Converts unrecoverable conditions to explicit human authority. | Terminal evaluator action. |
| `ALLOW_EXECUTOR_LAUNCH` | `dispatch_next_prompt_once()` final gate (7267-7277) | Launches only after prompt, ownership, pause, and rollover completion checks. | Must consume evaluator result, never re-derive rollover authority. |
| `BLOCK_EXECUTOR_LAUNCH` | `dispatch_next_prompt_once()` state/pause/active PID/rollover gates | Returns without launching. | Adapter behavior for all non-launch actions. |

### 2.3 Other recovery authority

* `LocalFirstOrchestrator.__init__()` (4639-4685) derives `_operator_restart_rollover_recovery_available` from durable `DEFERRED` state and migrates old deferred records. Restart must not grant a new finite budget.
* `_record_recovery_failure()` (1642-1660), `_record_recovery_success()` (1732-1743), and `_enter_safety_cutout()` (1745-1771) write maintenance, cooldown, epoch, and terminal fields.
* `request_discussion_pause()` / `request_discussion_resume()` (4931-4958) own the pause marker and discussion epoch. Resume may restore post-discussion protocol, but must not mutate rollover authority while paused or invent a new rollover transition.
* `run_executor_state_once()`, `reconcile_executor()`, `wait_for_executor()`, `mark_executor_started()`, and `mark_executor_exit()` own Executor result/liveness transitions. Their ownership observations must be supplied to the future rollover evaluator.
* `_legacy_handover_compatibility_allowed()` (1373-1377) and `_handover_response_valid()` gate the sole legacy protocol exception. New transactions use canonical protocol v1.

## 3. Durable field ownership map

The current implementation stores one mutable dictionary. “Owner” means the runtime path permitted to author a field; readers include scheduling, recovery, diagnostics, and dispatch. Writes remain serialized by the watcher state lock and `save()` path.

| Field(s) | Current writers / clearers | Scheduling readers and authority risk |
|---|---|---|
| `state`, `taskId`, `lastCompletedTaskId`, `nextTaskId` | Architect acceptance, Executor start/exit/result, human-required/failure paths, prompt staging | `main()`, dispatch, Executor recovery, rollover safe-boundary checks. |
| `discussionPauseActive`, `discussionPauseEpoch` | Pause marker/control methods; epoch increments in `request_discussion_pause()` | Main pause branch, service, remote/hotkey control. Marker and JSON are one overlay. |
| `rolloverDue`, `rolloverPending`, `rolloverInProgress` | Memory trigger, `request_if_due()`, service/recovery, revive/retire, completion | Passive gate, dispatch, service, transaction predicates. Duplicated interpretation is the primary defect. |
| `rolloverMaintenanceState`, `rolloverRecoveryState` | Bounded recovery, failure/success/cutout, epoch start, handover stages | Passive wait, epoch eligibility, service. `DEFERRED` is not proof of future cooldown. |
| `rolloverRecoveryAttemptCount`, `rolloverRecoveryStartedAt`, `rolloverRecoveryLastAttemptAt`, `rolloverRecoveryRetryAfter`, `rolloverRecoveryTerminalReason` | Bounded recovery and failure/cutout/success | Backoff, terminal logic, service. Per-epoch fields must not reset global epoch budget. |
| `rolloverRecoveryEpoch`, `rolloverAutomaticRecoveryEpochCount`, `rolloverAutomaticRecoveryMaxEpochs`, `rolloverAutomaticRecoveryNextEligibleAt` | Epoch start, failure/cutout initialization, legacy migration | Epoch eligibility, deferred logging, restart behavior. |
| `rolloverDeferredForTaskId`, `rolloverAttemptedForTaskId`, `rolloverAutoAttemptCount`, `rolloverLastFailureReason` | Failure, cutout, stale/terminal retirement, epoch start/success | Task ownership and diagnostics; mismatches fail closed. |
| `rolloverTransactionId`, `rolloverTransactionTaskId`, `rolloverTransactionGeneration` | Identity helper, request, revive/retire | Handover validation, epoch eligibility, fresh bootstrap. Identity is immutable while live. |
| `handoverRequested`, `handoverReady`, `rolloverHandoverSendState` | Request, send/recovery/reconcile, revive/retire, completion | Service and dispatch. `AMBIGUOUS` requires read-only reconciliation first. |
| `rolloverHandoverRecoveryDisposition`, `rolloverHandoverRecoveryReason`, `rolloverHandoverRecoveryRetryAfter` | Handover send/reconcile failure and pending response | Reconciliation backoff; not launch permission. |
| `rolloverHandoverProtocolVersion` | New transaction creation; absent only for the exact legacy record | Protocol validation, never general scheduling. |
| `pending_handover`, `rolloverHandoverResponseIdentity` | Request/reconcile/response completion and retirement | Existing-response proof and duplicate suppression. |
| Fresh candidate fields (`rolloverFreshCandidateConversationId`, state/discovery/attempt/retry/hash/page fields) | Candidate discovery/bootstrap/readiness and retirement | Candidate reuse/create and authority commit. |
| `architectConversationId`, `architectGenerating`, response count/identity, baselines | Attach/canonicalization, response acceptance, generation observation, recovery | Architect wait, handover/fresh-session checks, post-discussion recovery. |
| `executorProcessState`, `codexPid`, `active_codex_pid`, `lastExecutorPid`, `executorSessionId` | Executor start, reconciliation, exit/result, retirement | Dispatch and recovery; PID alone is not ownership proof. |
| `executorActiveWriter`, `governedExecutorActiveWriter`, `executorSessionMode` | Executor/session validation and recovery | Epoch eligibility and dispatch. Governed writer blocks; unrelated writers do not. |
| `executorResultPath`, attempt/launch/failure fields, `humanRequiredReason` | Executor state machine and failure paths | Human-required, dispatch, and recovery decisions. |
| Post-discussion fields (`postDiscussionEnvelopeRequired`, resume epoch, task/baseline, repair fields) | F10/ORCH:RESUME and response reconciliation | Architect protocol; must not create unrelated authority after `NEXT_PROMPT_READY` is staged. |
| `rolloverTrigger`, memory ownership/session fields, `memoryThresholdBytes` | Memory sampler and trigger | Trigger only. Once due is latched, memory drop cannot cancel it. |

### Ownership rule

Only the canonical evaluator may answer “wait, recover, reconcile, retire, create, complete, or require human.” Existing helpers may serialize, observe, validate, and execute a selected action; they must not independently derive a competing action from rollover fields.

## 4. Canonical normalization of durable combinations

This is a future evaluator contract, not a schema migration. Normalization preserves evidence and never silently deletes fields.

| Durable combination | Interpretation | Required result |
|---|---|---|
| `due=False`, `pending=False`, `inProgress=False` | No active rollover | `NO_ROLLOVER`, subject to ordinary dispatch gates. |
| `due=True`, `pending=False` | Latched trigger without pending ownership | `NORMALIZE` to pending only at a safe boundary without contradictory transaction; otherwise `FAIL_CLOSED`. Never launch while due remains unresolved. |
| `due=True`, `pending=True`, `inProgress=False` | Required rollover waiting for request/recovery/reconciliation | Evaluate pause, Executor, Architect, cooldown, response, and budget; passive predicates cannot terminate evaluation. |
| `due=False`, `pending=True` | Contradictory orphaned pending evidence | `FAIL_CLOSED` / `HUMAN_REQUIRED`, unless a normal completion record proves these fields historical. |
| `inProgress=True` without transaction identity/task owner | No safe mutation identity | `HUMAN_REQUIRED`. |
| `inProgress=True` with another task owner | Cross-task authority conflict | `HUMAN_REQUIRED`. |
| `DEFERRED` without deferred/transaction/attempted task matching `nextTaskId` | Deferred state has no owner | `HUMAN_REQUIRED`; restart cannot grant recovery. |
| `DEFERRED` with future next-eligible time | Real cooldown | `WAIT_COOLDOWN` after higher-priority pause/Executor/writer guards. |
| `DEFERRED` with expired/missing deadline and budget remaining | Recovery is eligible | `START_RECOVERY_EPOCH` or `START_RECOVERY_ATTEMPT`, subject to observations. |
| `RECONCILE_PENDING` without live transaction | Cannot reconcile safely | `HUMAN_REQUIRED`; do not synthesize a transaction. |
| `AMBIGUOUS` with exact existing response proof | Evidence is consumable | `RECONCILE_HANDOVER` before exhaustion/resend. |
| `AMBIGUOUS` with future retry deadline | Transport wait | `WAIT_RECONCILIATION`. |
| `AMBIGUOUS` with expired deadline | Re-evaluate proof, then bounded recovery or human terminal action | Never indefinite `WAIT_COOLDOWN`. |
| Epoch count `>= max` while not `HUMAN_REQUIRED` | Terminal budget/state mismatch | `NORMALIZE` to explicit `HUMAN_REQUIRED`; preserve evidence. |
| `HUMAN_REQUIRED` with due/pending rollover | Human authority owns continuation | `HUMAN_REQUIRED`; no automatic epoch or launch. |
| `discussionPauseActive=True` with due rollover | Human discussion overlay | `WAIT_DISCUSSION`; no mutation or budget consumption. |
| Due rollover followed by memory below threshold | Latched maintenance decision | Keep due/pending; memory cannot cancel it. |

Precedence is: invalid identity/state; discussion pause; governed Executor/writer; Architect generation; exact existing handover; future deadlines; expired-deadline recovery; exhausted budget; request/complete. This prevents WAIT and RECOVER from both being true.

## 5. Pure decision contract

The future canonical function is:

```python
evaluate_rollover_state(
    durable_state: Mapping[str, Any],
    observations: RolloverObservations,
    now: float,
) -> RolloverDecision
```

It is pure and deterministic: no file/process/environment/Playwright/CDP access, logging, sleep, state write, launch, outside clock, or mutable global state. Malformed durable state yields an explicit normalization/fail-closed decision.

`RolloverObservations` must be supplied by adapters and include at least:

```text
discussionPaused, safeBoundaryState, nextTaskId
executorProcessAlive, executorOwnsCurrentBoundary, governedExecutorActiveWriter
architectGenerating
existingHandoverResponseObserved, existingHandoverResponseValid
existingHandoverTransactionMatches, existingHandoverTaskMatches
deliveryAcknowledgementProven
freshCandidateObserved, freshCandidateProofValid, freshCandidateIsCurrentTransaction
freshArchitectReady, contradictoryLiveTransaction
validResultAlreadyPresent, currentPromptAvailable
```

The result contains one action, stable reason, current task/transaction identity, and optional positive deadline/wait data. It cannot return both WAIT and RECOVER eligibility.

## 6. Minimum action set

```text
NO_ROLLOVER
WAIT_DISCUSSION
WAIT_EXECUTOR
WAIT_ACTIVE_WRITER
WAIT_ARCHITECT
WAIT_COOLDOWN
WAIT_RECONCILIATION
REQUEST_HANDOVER
RECONCILE_HANDOVER
START_RECOVERY_ATTEMPT
START_RECOVERY_EPOCH
REVIVE_TRANSACTION
RETIRE_TRANSACTION
CREATE_FRESH_ARCHITECT
COMMIT_NEW_ARCHITECT_AUTHORITY
COMPLETE_ROLLOVER
HUMAN_REQUIRED
BLOCK_EXECUTOR_LAUNCH
ALLOW_EXECUTOR_LAUNCH
```

The authority property is fixed:

```text
one durable state + one observation set + one now -> exactly one action
```

Execution adapters perform the selected action, persist its result, and re-evaluate after each durable transition.

## 7. Safety invariants

1. Never duplicate an Executor launch for a task/attempt owner.
2. Never launch while required rollover is unresolved.
3. Never blindly resend an ambiguous handover; reconcile exact existing evidence first.
4. Never consume a wrong transaction/task response.
5. Discussion pause blocks rollover mutation, fresh Architect creation, handover send, and Executor launch.
6. Do not replace Architect authority before fresh candidate proof and durable commit.
7. Do not allow a concurrent governed Executor writer; unrelated Codex sessions are not conflicts.
8. Preserve task `000102` completion and prepared task `000103`.
9. Restrict legacy compatibility exactly to `7fdbd798659f42295a18dd2d` / `000103` with absent pre-upgrade protocol version.
10. New transactions use canonical HANDOVER protocol version 1.
11. A valid `rolloverDue=True` latch cannot be canceled by a later memory drop.
12. Do not erase evidence solely to make retry/restart eligible.

## 8. Liveness invariants

1. A future cooldown may wait with a positive deadline-derived wait.
2. An expired cooldown cannot remain `WAIT_COOLDOWN` indefinitely.
3. A future reconciliation deadline may wait; after expiry the evaluator re-evaluates.
4. Recoverable deferred state with budget eventually attempts recovery through the real public entry path.
5. A valid existing handover is eventually reconciled before resend/exhaustion.
6. Completed rollover eventually clears gates and releases prepared Executor dispatch.
7. Unrecoverable state eventually becomes explicit `HUMAN_REQUIRED`.
8. Identical waiting state cannot repeat forever when no external condition remains.
9. Discussion pause may delay liveness but cannot consume attempts/epochs or busy-loop.
10. Restart continues from the durable finite budget; it cannot reset the per-task budget.

## 9. Exact legacy 000103 boundary

The `7fdbd798659f42295a18dd2d` / `000103` exception is authorized only when protocol version is absent, both identities match exactly, the visible response has exact legacy transaction evidence and existing authority checks pass, no newer conflict exists, no Executor for `000103` launched, and the response is consumed at most once.

The future evaluator receives structured transport evidence such as `existingHandoverValid`; it does not special-case `000103` in scheduling logic. New transactions persist protocol v1 before send and cannot use legacy markers as authority.

## 10. Structural drift-prevention design

Add a future CI test (not in this phase) that parses the watcher with `ast`, identifies the canonical evaluator and its guarded-field read set, and rejects new runtime helpers that branch on two or more rollover decision fields and return/record scheduling actions (`WAIT`, `RETRY`, `RECOVER`, `HUMAN_REQUIRED`, `LAUNCH`, or equivalents) outside the evaluator/thin-adapter allowlist.

The guarded set includes the due/pending/progress, maintenance/recovery/epoch/deadline, handover/disposition, transaction identity, discussion pause, Executor ownership, and Architect generation fields. The test permits serialization, diagnostics, logging, protocol validators, fixture construction, and adapters executing a selected action. It also rejects process/browser API calls from the pure evaluator and requires public-entry journey tests rather than private recovery calls only.

## 11. Deterministic journey-test specification

Use isolated state directories, fake clock/sleep, controlled observations, and durable reload. Enter through the real public path, especially `run_next_prompt_ready_once()` / `dispatch_next_prompt_once()`.

* **A — normal rollover:** threshold latch, canonical handover, exact response, fresh readiness/authority commit, clear rollover, one prepared-task launch.
* **B — F9 → F10:** F9 changes no workflow counters; F10 re-enters evaluation once; repeated cycles do not inflate epochs.
* **C — cooldown expiry:** future deadline waits positively; fake time advances; the same public path changes to recovery eligibility; no attempt/epoch inflation while waiting.
* **D — ambiguous handover:** first pass reads without resend; later exact response is consumed; unresolved evidence remains bounded.
* **E — crash/restart:** reload after request, acknowledge, ambiguity, response, fresh creation, readiness, and authority commit; no duplicate send/launch.
* **F — Architect generation:** generation visible waits; after it stops, request/reconcile is reachable without replay.
* **G — legacy transaction:** exact `7fdb...` / `000103` response recovers once without resend; changed identity fails closed.
* **H — fresh Architect:** exact handover, readiness, durable authority before old retirement, then `000103` exactly once.
* **I — unrecoverable:** bounded attempts/epochs lead to explicit HUMAN_REQUIRED and zero further actions.
* **J — out-of-band durable corruption:** isolated fixtures prove documented normalization/fail-closed behavior; this is not an operator procedure for production editing.

Every journey asserts safety (no duplicate launch/send, no mutation during pause, no stale identity, no launch before rollover) and liveness (expired waits change action, recoverable states progress, successful rollover releases dispatch, impossible states terminate explicitly).

## 12. Bounded implementation cutover

* **Commit B — pure evaluator and tests, unused by production:** immutable observations, normalization, action enum, evaluator, invalid-state and legacy-boundary tests.
* **Commit C — public-path cutover:** have `run_next_prompt_ready_once()` and `dispatch_next_prompt_once()` obtain observations and execute the evaluator result; correct the expired-cooldown expectation and use the real entry path for self-rearm.
* **Commit D — reconciliation/backoff consolidation:** subordinate passive and backoff predicates and `_automatic_recovery_epoch_eligible()` to evaluator decisions; retain positive waits/logging as adapters.
* **Commit E — transaction lifecycle consolidation:** route retire/revive, handover reconciliation, fresh candidate creation, and authority commit through explicit selected transitions while retaining evidence and crash durability.
* **Commit F — remove/subordinate duplicate predicates and add structural guard:** complete AST drift prevention and production-shaped `000102 → 000103` journey coverage.

Each cutover commit must pass safety and liveness journeys before enabling the next authority. The initial evaluator should normalize existing records without a broad durable-schema migration.

## 13. Audit conclusion

The rollover subsystem has strong safety controls but more than one runtime authority for the same wait/recovery decision. The repair must therefore be a bounded state-machine consolidation, not another local cooldown predicate. The evaluator owns decisions; browser, process, persistence, and logging code supply observations or execute selected actions. Until that cutover and the journey criteria pass, the `000102 → 000103` rollover remains not production-qualified.
