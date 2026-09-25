# AFFOTECH Local Orchestrator — Rollover Liveness Architecture Finding

Date: 2026-09-26

Status: CONFIRMED PRE-REPAIR FINDING

Repository baseline inspected: `de99ef63c5c60189737f29507eaf39a8a9ab5064`

This document intentionally records the diagnosis **before** any new watcher repair is made. Its purpose is to preserve the production evidence, source-level root cause, test-suite failure mode, and required repair direction so that the next change cannot quietly narrow the problem back to a single symptom.

## 1. Orchestrator objective

The Local Orchestrator exists to automate a bounded relay used to develop AFFOTECH:

`Rony -> ChatGPT Architect -> Codex Executor -> ChatGPT Architect verification -> next bounded Executor task`

Its job is transport and durable workflow coordination. It is not the AFFOTECH product and must not become alternate workflow authority.

The intended value is to remove manual copy/paste while preserving:

- Rony as final human authority;
- Architect as decision authority;
- Executor as implementation/runtime/test authority;
- independent Architect verification of Executor claims;
- one bounded task at a time;
- exactly-once delivery/dispatch behavior;
- durable crash recovery;
- persistent Executor-session continuity;
- worktree isolation;
- human pause/resume;
- safe Architect-session rollover when renderer memory reaches the configured threshold.

A correct orchestrator must satisfy both **safety** and **liveness**. Preventing duplicates is not enough if the workflow can wait forever.

## 2. Production context that exposed the finding

The relevant production boundary was preserved as:

```text
state=NEXT_PROMPT_READY
taskId=000102
lastCompletedTaskId=000102
nextTaskId=000103
rolloverDue=True
rolloverPending=True
rolloverMaintenanceState=DEFERRED
discussionPauseActive=True
rolloverTransactionId=7fdbd798659f42295a18dd2d
rolloverTransactionTaskId=000103
```

Task `000102` was complete. Task `000103` was staged but had not launched. The watcher was stopped for maintenance, then restarted from `de99ef63...`. F10 successfully cleared the human discussion pause and printed:

```text
ORCHESTRATOR RESUMED BY HUMAN state=NEXT_PROMPT_READY taskId=000102
```

No subsequent rollover recovery or Executor launch occurred.

The absence of progress was not caused by F10 failing. F10 released the pause correctly. The workflow then re-entered a deterministic passive-wait path that prevented the recovery service from being called.

## 3. Confirmed source-level defect: contradictory transition authority

Two separate parts of the watcher independently answer the same scheduling question:

> Should a deferred rollover continue waiting, or is automatic recovery eligible now?

They use overlapping but non-identical predicates.

### 3.1 Passive-wait authority

`deferred_rollover_passive_wait_required()` in `local_orchestrator_watcher.py` returns true when, among other conditions:

- workflow state is `NEXT_PROMPT_READY`;
- `rolloverDue` is true;
- `rolloverMaintenanceState == "DEFERRED"`;
- the deferred task matches `nextTaskId`;
- automatic recovery epoch count is below the configured maximum.

Critically, this predicate does **not** require `rolloverAutomaticRecoveryNextEligibleAt` to still be in the future.

Therefore it can continue to classify a rollover as passive even after its recovery cooldown has expired.

### 3.2 Recovery-eligibility authority

`ArchitectSessionRollover._automatic_recovery_epoch_eligible()` evaluates the same deferred rollover but correctly includes the recovery deadline:

```python
eligible_at = float(state.get("rolloverAutomaticRecoveryNextEligibleAt", 0.0) or 0.0)
...
if epochs >= max_epochs or time.time() < eligible_at:
    return False
```

When the epoch budget remains available and the deadline is in the past, this function correctly classifies the rollover as eligible for recovery.

### 3.3 The contradiction

For the same durable state, after the deadline has passed, both of these can be true simultaneously:

```text
deferred_rollover_passive_wait_required(...) == True
_automatic_recovery_epoch_eligible(...)      == True
```

Semantically these answers conflict:

- the passive layer says **WAIT**;
- the recovery layer says **RECOVER NOW**.

This is not merely duplicated code. It is duplicated transition authority.

## 4. Why the recovery code becomes unreachable

`run_next_prompt_ready_once()` currently executes this order:

```python
if passive_deferred_rollover_wait(watcher, logger, run_id):
    return None

process = dispatch_next_prompt_once(...)
```

`passive_deferred_rollover_wait()` calls `deferred_rollover_passive_wait_required()` first. If that returns true, the function sleeps, reloads state and returns true.

The caller then returns from the `NEXT_PROMPT_READY` iteration before `dispatch_next_prompt_once()` runs.

But `dispatch_next_prompt_once()` is the path that calls:

`service_deferred_rollover_once()`

and `service_deferred_rollover_once()` is the path that consults the real automatic-recovery eligibility logic.

The resulting control flow is:

```text
NEXT_PROMPT_READY
    -> passive_deferred_rollover_wait()
        -> deferred_rollover_passive_wait_required() == True
        -> positive sleep
        -> reload
        -> return True
    -> run_next_prompt_ready_once() returns
    -> next main-loop iteration
    -> same predicate again
```

Once the cooldown has already expired, the passage of additional time does not change the passive predicate because the predicate does not consult the deadline.

The recovery service therefore remains unreachable while the same deferred state and epoch budget remain present.

This is a **liveness failure / permanent passive-wait livelock**.

It is safer than the earlier zero-delay busy loop because it sleeps positively, but it still makes no workflow progress.

## 5. Why restart and F10 did not fix it

The production restart correctly restored `NEXT_PROMPT_READY`. F10 correctly cleared the durable discussion pause.

Neither action changes the predicate that keeps `deferred_rollover_passive_wait_required()` true.

The recovery epoch fields are durable. `_record_recovery_failure()` persists:

- `rolloverRecoveryState = DEFERRED`;
- `rolloverMaintenanceState = DEFERRED`;
- `rolloverDeferredForTaskId`;
- `rolloverAutomaticRecoveryEpochCount`;
- `rolloverAutomaticRecoveryMaxEpochs`;
- `rolloverAutomaticRecoveryNextEligibleAt`.

Therefore a normal watcher restart reconstructs the same deferred condition. After F10, the workflow simply re-enters the same passive gate.

This explains why the terminal showed `ORCHESTRATOR RESUMED BY HUMAN` but no task launch or useful rollover progress followed.

## 6. Test-suite finding: a regression test encoded the defect as expected behavior

The strongest qualification failure is in:

`test_deferred_wait_never_uses_zero_delay_and_does_not_inflate_budget`

in `test_local_first_orchestrator.py`.

The test constructs:

```python
"rolloverMaintenanceState": "DEFERRED",
"rolloverAutomaticRecoveryEpochCount": 1,
"rolloverAutomaticRecoveryMaxEpochs": 3,
"rolloverAutomaticRecoveryNextEligibleAt": 0,
```

`NextEligibleAt=0` means there is no future cooldown remaining.

The test then asserts:

```python
assert watcher_module.passive_deferred_rollover_wait(watcher, poll_interval=0) is True
assert waits and waits[0] > 0
```

The test was intended to prove the earlier zero-delay busy-spin repair:

- never sleep with zero delay;
- do not inflate recovery budget while passively waiting.

But it also inadvertently certifies a stronger and incorrect behavior:

> remain passive even when automatic recovery is already eligible.

The suite therefore converted the production livelock into a passing regression assertion.

This is a key reason a large green suite did not protect production liveness.

## 7. Second test finding: the self-rearm test bypasses the real production path

`test_deferred_rollover_self_rearms_after_cooldown_without_restart` sets an initial fake time before the deadline and correctly proves:

```text
deferred passive wait = true
automatic recovery eligible = false
```

It then advances fake time beyond the deadline and proves:

```text
automatic recovery eligible = true
```

However, after advancing time it does **not** re-enter the real `NEXT_PROMPT_READY` production path.

Instead, it directly calls internal recovery methods, including `_begin_automatic_recovery_epoch()`.

That bypasses the upstream passive gate that production must pass through first.

Therefore the test proves the recovery mechanism can work **if called**, but not that the real watcher can ever reach it after the cooldown expires.

The test name claims end-to-end self-rearming behavior that the test does not actually exercise.

## 8. Safety versus liveness

The orchestrator has accumulated strong safety mechanisms, including:

- exact task identity;
- exact result-delivery proof;
- no blind resend after ambiguous delivery;
- bounded handover reconciliation;
- active-Executor ownership checks;
- worktree isolation;
- strict machine envelopes;
- one-fresh-Architect rules;
- F9 discussion pause;
- bounded recovery budgets;
- durable crash recovery.

Those mechanisms are valuable and must not be discarded casually.

The current defect demonstrates that safety alone is insufficient.

A workflow can satisfy all of these properties:

```text
no duplicate launch
no duplicate handover
no mutation while paused
no stale authority accepted
no unsafe retry
```

while also satisfying:

```text
no useful work ever happens again
```

The missing class of invariant is **liveness**.

Examples of required liveness properties:

- when F10 releases the final human pause, the workflow eventually re-evaluates its next legal machine action;
- when a recovery cooldown expires and budget remains, recovery is eventually attempted;
- when a valid existing handover is available, it is eventually consumed;
- when rollover completes, the prepared Executor task eventually launches exactly once;
- if progress is impossible, the workflow eventually reaches an explicit terminal `HUMAN_REQUIRED` state instead of silently waiting forever.

## 9. Architectural root cause

The confirmed defect is a concrete instance of a broader architectural problem:

**transition authority is distributed across several functions that independently interpret the same mutable durable state.**

Rollover behavior is represented by many overlapping fields, including:

- `rolloverDue`;
- `rolloverPending`;
- `rolloverInProgress`;
- `rolloverMaintenanceState`;
- `rolloverRecoveryState`;
- `rolloverRecoveryAttemptCount`;
- `rolloverRecoveryEpoch`;
- `rolloverAutomaticRecoveryEpochCount`;
- `rolloverAutomaticRecoveryNextEligibleAt`;
- `rolloverHandoverSendState`;
- `rolloverHandoverRecoveryDisposition`;
- `rolloverTransactionId`;
- `rolloverTransactionTaskId`;
- `rolloverDeferredForTaskId`;
- `rolloverAttemptedForTaskId`;
- fresh-Architect candidate fields;
- handover fields;
- human discussion overlay fields.

The problem is not simply the number of fields. The problem is that different functions are allowed to derive scheduling authority from different subsets of them.

Current decision-making is distributed across, among other areas:

- the main loop;
- passive deferred waiting;
- reconciliation backoff waiting;
- Executor dispatch;
- rollover service;
- automatic recovery epoch eligibility;
- handover reconciliation;
- transaction retirement/revival;
- crash/restart recovery;
- F9/F10 discussion control.

When multiple layers answer the same scheduling question independently, local fixes can make one layer safer while making another layer unreachable.

That is the recurring mechanism behind the recent pattern:

```text
production incident
-> narrow safety repair
-> local regression test
-> green suite
-> another interaction defect appears in production
```

## 10. Independent second opinion

After the source-level contradiction above was identified, Rony requested an independent second opinion from Claude against the public repository.

The independent review reached the same material conclusion from source:

- the current deferred-rollover condition is a permanent liveness failure rather than a flaky browser race;
- transition authority for rollover wait/recovery is duplicated;
- the passive gate runs before the recovery eligibility logic and can keep it unreachable;
- the zero-delay regression test currently encodes the invalid permanent-wait behavior as expected;
- the self-rearm test bypasses the actual production entry path;
- the core relay should not be discarded;
- the rollover/recovery decision surface requires state-machine consolidation and liveness-oriented journey testing.

The independent review is corroborating evidence, not runtime authority. The repository source and production state remain authoritative.

## 11. Architecture assessment

Current conclusion:

**Do not rewrite the whole orchestrator. Do not continue with symptom-only rollover patches. Perform a bounded consolidation of the rollover/recovery decision surface.**

The following areas should be preserved unless separate evidence proves a defect:

- core Architect -> Executor -> Architect relay;
- exactly-once delivery/receipt mechanisms;
- worktree isolation;
- persistent Executor-session model;
- strict machine-envelope parsing;
- F9/F10 human discussion overlay;
- browser one-fresh-session and exact-bootstrap proof mechanisms;
- Windows-safe logging behavior;
- crash recovery principles.

The unstable area is primarily the **rollover/recovery scheduling authority and its tests**.

## 12. Required direction for the next repair

The next repair must address the bug class, not only add one missing deadline check.

A narrow check such as:

```text
if cooldown expired:
    stop passive waiting
```

would unblock the observed state but would leave the duplicated-authority architecture intact.

The preferred direction is one authoritative, side-effect-free rollover decision function, conceptually:

```text
evaluate_rollover_state(state, observations, now) -> RolloverAction
```

Possible actions may include:

```text
WAIT_DISCUSSION
WAIT_COOLDOWN
WAIT_RECONCILIATION
WAIT_ARCHITECT
REQUEST_HANDOVER
RECONCILE_HANDOVER
CREATE_FRESH_ARCHITECT
COMPLETE_ROLLOVER
HUMAN_REQUIRED
```

Exact names are not yet governed. The important invariant is that **one component owns the decision** and execution helpers obey it rather than independently re-deriving eligibility.

Decision and I/O should be separated:

- decision layer: pure, deterministic, no browser mutation, no sleep, no state writes;
- execution layer: performs the one selected action and records the resulting durable transition.

The first consolidation should preserve the existing durable schema where practical. Large state-schema reduction should be a later, separately governed step after the new decision model is proven against existing production records.

## 13. Required verification before production resumes

The next rollover repair is not qualified by unit-test count alone.

Qualification must include multi-iteration journey tests through the real production entry path, especially `run_next_prompt_ready_once()`.

At minimum, deterministic simulation must cover:

1. future automatic-recovery cooldown -> positive bounded wait;
2. automatic-recovery deadline expires -> decision changes from wait to recovery;
3. F9 while deadline expires -> no workflow mutation;
4. F10 after deadline expired -> recovery path becomes reachable;
5. ambiguous handover -> read-only reconciliation, no resend;
6. valid existing handover appears later -> eventual consumption;
7. crash/restart during each major rollover phase -> convergence without duplicate send/launch;
8. Architect still generating -> wait, then eventual continuation after generation stops;
9. rollover success -> prepared next task launches exactly once;
10. unrecoverable/budget-exhausted state -> explicit `HUMAN_REQUIRED`, not silent indefinite wait.

The suite must assert both:

### Safety

- no duplicate Executor launch;
- no duplicate ambiguous handover resend;
- no mutation during discussion pause;
- no stale/wrong transaction acceptance;
- no Executor launch before rollover authority is complete.

### Liveness

- waiting conditions that expire must stop producing `WAIT`;
- recoverable states must eventually attempt recovery;
- successful recovery must eventually leave rollover state;
- prepared work must eventually dispatch exactly once;
- unrecoverable states must eventually become explicit terminal human authority.

A useful universal property is:

> If no unresolved external condition remains and recovery budget exists, the same durable waiting state must not repeat forever.

A bounded simulation should prove that every reachable rollover state either progresses after its external condition resolves or terminates explicitly as `HUMAN_REQUIRED`.

## 14. Governance preserved during repair

Until this finding is closed:

- do not manually edit production `state.json` to force progress;
- do not manually resume the persistent AFFOTECH Executor session while the watcher owns it;
- do not rerun completed task `000102`;
- do not launch prepared task `000103` before rollover authority is safely resolved;
- do not delete/reset the preserved worktrees or transaction evidence;
- keep the production watcher stopped during source maintenance unless Rony explicitly starts it for a controlled qualification;
- keep documentation of the failed behavior even after repair; do not rewrite history as if the defect never existed.

The independent reviewer suggested manual state manipulation as a possible immediate data-level unblock. That suggestion is **not adopted** because existing AFFOTECH orchestrator governance explicitly prohibits manual production-state editing as a normal recovery mechanism.

## 15. Closure criteria for this finding

This finding remains OPEN until all of the following are true:

1. rollover wait/recovery scheduling has one authoritative decision surface;
2. the contradictory expired-cooldown state cannot produce both WAIT and RECOVER eligibility;
3. the incorrect zero-delay regression expectation is corrected;
4. the self-rearm test reaches recovery through the real production entry path;
5. journey-level liveness tests pass;
6. the preserved `000102 -> 000103` production rollover recovers without handover resend or duplicate task launch;
7. a fresh Architect becomes authoritative with exact proof;
8. task `000103` launches exactly once;
9. subsequent clean rollover/endurance qualification completes without recurrence;
10. canonical runtime/incident documentation is updated with the accepted repair and production qualification evidence.

Until those criteria are met, Architect rollover/recovery remains **not production-qualified**, even if the rest of the orchestrator core relay remains usable and accepted.
