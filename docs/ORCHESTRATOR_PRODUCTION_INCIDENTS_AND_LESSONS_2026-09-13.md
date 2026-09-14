# AFFOTECH Local Orchestrator — Production Incidents and Lessons

Original incident date: 2026-09-13

Stabilization closure update: 2026-09-15

## Purpose

This document preserves the production failures discovered while moving the Local Orchestrator from deterministic qualification into real AFFOTECH use, the bounded repairs that followed, and the lessons that must remain part of future Orchestrator governance.

It is historical incident evidence. Current runtime authority and current qualification status live in:

`docs/ORCHESTRATOR_RUNTIME.md`

Universal-template readiness lives in:

`docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md`

Do not use an older incident-state snapshot below to override the current runtime contract.

## Stabilization outcome

The incident chain no longer represents the current operational condition.

Accepted runtime/source checkpoint after the later recovery chain:

`57a3a914b35ed0c715aaaf0267ed0bd36a39cc78` — `fix(orchestrator): keep human decisions resident`

Deterministic qualification at that checkpoint:

- Python compile PASS;
- `244 passed / 0 failed`;
- `git diff --check` PASS;
- resident HUMAN_REQUIRED PASS;
- ordinary discussion PASS;
- same-task later EXECUTE PASS;
- repeated HUMAN_REQUIRED PASS;
- STOP-remains-resident PASS;
- temporary Architect attach/read recovery PASS;
- single-next-task launch PASS;
- existing recovery/rollover/receipt regressions PASS.

After the resident HUMAN_REQUIRED repair and the matching AFFOTECH Architect-governance synchronization, Rony reported the production watcher running smoothly and mature in normal operation.

Architect classification for the Orchestrator core is therefore now:

`PRODUCTION_MATURE_REFERENCE_IMPLEMENTATION`

This closes the broad stabilization phase. New Orchestrator engineering should now be justified by direct regression evidence, a clearly bounded maintenance need, or the universal-template extraction objective—not by reopening this incident chain.

## Incident 1 — Playwright Sync bridge crossed thread ownership

### Symptom

Production restart crashed with Playwright/greenlet errors including thread-switch and Sync-API/async-loop failures. The watcher did not reach the intended next Executor task.

### Root cause

The remote discussion-control monitor created/first used a Playwright Sync bridge on the main thread and later reused it from a worker thread. Shutdown could also close a worker-owned bridge from the main thread.

### Repair

`1ec880e7d2bede1d56078cab433f3d2793ec008b` — `fix(orchestrator): keep remote control Playwright thread-affine`

The remote worker owns bridge create/use/reattach/close for its full lifetime.

### Permanent lesson

Browser automation object ownership includes thread/event-loop ownership. Create, use and close Playwright Sync objects on the same owning thread.

## Incident 2 — Remote startup declared unavailable too quickly

### Symptom

Production printed `REMOTE_CONTROL_UNAVAILABLE` even though the worker could still be initializing.

### Root cause

A two-second readiness wait was shorter than real Playwright attachment latency and could return failure before proving worker termination.

### Repair

`54a82e81be99eb1a85831cf27ccf37a634082191` — `fix(orchestrator): harden Architect bridge startup`

Startup gained a realistic bounded budget and failure now proves/requests bounded worker shutdown rather than merely stopping the wait.

### Permanent lesson

Timeout is protocol semantics. A timeout path must prove termination when continued activity could create dual authority.

## Incident 3 — Stale composer locator / short verification timeout

### Symptom

IDLE Architect contact failed on a stale `#prompt-textarea` locator after ChatGPT rerendered the composer.

### Root cause

DOM presence was treated as actionability. The live semantic textbox could differ from the stale selector.

### Repair

Also closed in `54a82e81...`:

- prefer the current semantic textbox;
- require visible/editable actionability;
- reacquire controls after rerender;
- make proven pre-send failure nonfatal to the resident watcher.

### Permanent lesson

DOM identity is evidence, not UI authority. Dynamic interfaces require current actionable-element proof at each mutation/verification stage.

## Incident 4 — Unsent Orchestrator payload remained in the human composer

### Symptom

A full bootstrap payload remained visibly populated even though no send was proven.

### Root cause

Composer population could succeed before verification failed, and the old path had no exact owned-payload cleanup rule.

### Repair

Also closed in `54a82e81...`:

- clear only a composer whose normalized text exactly matches the known Orchestrator payload;
- never clear unrelated human text;
- never clear after an attempted/ambiguous send without reconciliation.

### Permanent lesson

Cleanup is an authority-sensitive action. The Orchestrator may clean up only text it can prove it owns.

## Incident 5 — Malformed Architect envelope was mistaken for ordinary discussion

### Symptom

A malformed `ORCHESTRATOR_RESULT` was treated as non-envelope content and IDLE attempted to bootstrap a second roadmap decision.

### Root cause

The old path inferred envelope presence only after successful strict parsing. An intended but invalid envelope therefore fell through as discussion.

### Repair

`216a57d0635bfa0e6342f276ab880b250afe4003` — `fix(orchestrator): fail closed on invalid Architect envelope`

The literal opening marker now distinguishes intended machine authority before strict parsing succeeds.

### Permanent lesson

There are three distinct input classes:

1. valid machine authority — process it;
2. intended but invalid machine authority — fail closed;
3. ordinary discussion — treat as discussion.

Malformed authority must never silently become non-authority.

## Incident 6 — Canonical envelope field-order mismatch

### Symptom

The Architect returned all required fields but in a different order from the parser contract.

### Action

A format-correction-only instruction preserved the already-decided audit and next prompt while correcting the canonical order:

```text
<ORCHESTRATOR_RESULT>
classification=ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT
action=EXECUTE|HUMAN_REQUIRED|STOP
taskId=<completed task id>
documentation=NOT_REQUIRED|REQUIRED|COMPLETE
promptBegin
<complete next bounded prompt only when action=EXECUTE>
promptEnd
</ORCHESTRATOR_RESULT>
```

### Permanent lesson

Producer and parser contracts are literal. Current field order is protocol-significant until a separately governed protocol revision changes it.

## Incident 7 — One-time HUMAN_REQUIRED release encountered UTF-8 BOM

### Symptom

A guarded state-release read failed on a UTF-8 BOM before mutation.

### Action

The bounded recovery reader tolerated `utf-8-sig`, preserved the completed task identity and performed only the explicitly authorized state release.

### Permanent lesson

Recovery tooling must tolerate the durable encoding actually present and must fail before mutation when parsing is uncertain. A later technical crash does not recreate one-time human authority.

## Incident 8 — Stored Codex PID was mistaken for possible active work

### Observation

Durable state retained an old numeric `codexPid` after the child was already dead.

### Conclusion

Recorded PID is historical metadata until paired with a current liveness check.

### Permanent lesson

Never kill broad Codex process groups based solely on a stored PID. Process identity requires current liveness and ownership evidence.

## Later stabilization failures and repairs

The 2026-09-13 incidents were followed by additional real-production defects during result delivery and rollover interaction. Those repairs are part of the same stabilization story even though they occurred after the original incident note.

Key accepted additions:

- `fce038e29698f18e13c7f9624bf6a80337b2cdc6` — exact result-delivery proof / recovery foundation;
- `87a214fe98a190827dd06f0456e0d34a687410d5` — same-task false-reconciliation correction;
- `9bc1d9cc030d0e0279bc06b953f354c01533df30` — durable SHA-256 delivery receipt;
- `ad29cca8b4eac1bf039c3e3b5a017a358fd57528` — rollover changed from workflow preemption to non-preemptive maintenance;
- `67d1a8d911f5bc839441eadeb69e2f20c16c4651` — confirmed-result recovery after rollover failure;
- `26f6644af1703abd2068198708ae7dbe54544f6a` — fact-based completed-confirmed workflow recovery;
- `57a3a914b35ed0c715aaaf0267ed0bd36a39cc78` — resident human-decision continuation.

The important architectural correction across these repairs was to stop letting stale error labels or session-maintenance state outrank stronger durable workflow facts.

## Why these defects escaped earlier tests

Earlier qualification was strong at the component level: parser behavior, IDLE continuation, transport, pause controls, remote monitoring, rollover helpers and recovery all had deterministic tests.

The missing proof was repeated composition through the real ChatGPT DOM, real process lifetime, real durable state and real project work.

Permanent lesson:

**Regression qualification is necessary, but browser-mediated orchestration also needs governed real production use.**

At the same time, a real regression should reopen only the proven failing boundary—not trigger a full redesign of accepted foundations.

## Permanent governance lessons

1. Preserve completed work before transport or browser maintenance.
2. Browser object ownership includes thread ownership.
3. Startup failure must prove safe termination where continued activity could create dual authority.
4. DOM presence is not actionability.
5. Proven pre-send and ambiguous post-send failures are different authority classes.
6. Clear only text the Orchestrator can prove it owns.
7. Malformed machine authority fails closed.
8. Machine protocols are literal contracts.
9. One-time human authority is not recreated by later technical failure.
10. Stored process metadata is not liveness.
11. Production proof matters at browser/runtime boundaries.
12. Repair the proven boundary; do not reopen unrelated accepted foundations.
13. Recovery should prefer current durable facts over stale error labels.
14. Rollover is maintenance only and must never own project workflow authority.
15. HUMAN_REQUIRED may pause project execution while supervision remains resident.
16. Ordinary human/Architect discussion is not machine execution authority.
17. The Project Architect decides when human authority is sufficient; the Orchestrator does not infer business decisions.
18. Keep the Orchestrator boring.

Canonical design target:

`recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action`

## Stabilization closure and universal-template implication

The original future-template condition has now been met sufficiently to begin universalization work: the Orchestrator has continued through real AFFOTECH milestones, survived production transport/recovery incidents, gained resident human-decision handling, and is reported by the final human authority as running smoothly and mature.

Therefore the next Orchestrator engineering phase may move from **stabilization** to **universal template extraction**.

This does **not** mean the current source is already a drop-in universal template.

The current implementation still carries AFFOTECH-specific repository paths, remote identity, Executor session, Architect conversation identity, bootstrap naming and related configuration assumptions.

Universalization must extract those values into a project profile while preserving the proven workflow semantics.

See:

`docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md`

The future template qualification suite must preserve at minimum:

- thread-aware browser ownership;
- realistic startup/failure shutdown behavior;
- current actionable composer resolution;
- exact owned-payload cleanup;
- malformed-envelope fail-closed behavior;
- canonical producer/parser compatibility;
- resident HUMAN_REQUIRED continuation;
- exactly-once result delivery;
- discussion pause during live Executor/result-ready boundaries;
- fact-based recovery;
- one writer/project/task;
- project-profile isolation;
- one real browser-mediated smoke path before declaring a new project's production readiness.

## Remaining maintenance qualification gap

Deferred Architect rollover at the intended live-Executor boundary remains separately not fully end-to-end qualified.

This is not a reason to reopen the mature core loop and is not a blocker for universal-template extraction.

The protected invariant remains:

**rollover may be due, but maintenance must never halt or own project workflow authority.**
