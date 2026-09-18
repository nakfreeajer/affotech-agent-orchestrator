# AFFOTECH Local Orchestrator — Production Incidents and Lessons

Original incident date: 2026-09-13

Stabilization closure update: 2026-09-18

## Purpose

This document preserves the production failures discovered while moving the Local Orchestrator from deterministic qualification into real AFFOTECH use, the bounded repairs that followed, and the lessons that must remain part of future Orchestrator governance.

It is historical incident evidence. Current runtime authority and current qualification status live in:

`docs/ORCHESTRATOR_RUNTIME.md`

Universal-template readiness lives in:

`docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md`

Do not use an older incident-state snapshot below to override the current runtime contract.

## Stabilization outcome

The original 2026-09-13 incident chain no longer represents the current operational condition, but later production use on 2026-09-18 exposed additional restart/rollover recovery defects that required bounded repair.

Accepted runtime/source checkpoint after the latest recovery chain:

`2cebe036eb89535ddd90cd52a679b6451ff112d6` — `fix(orchestrator): recover stale fresh candidate identity`

At that checkpoint the specific September 18 deadlock was closed by deterministic qualification against the exact observed production state:

- completed task `000052` remained completed and was not rerun;
- staged next task `000053` remained preserved;
- stale completed Executor PID ownership was retired without killing any process;
- stale persisted fresh-Architect candidate identity was corrected by exact bootstrap + `ARCHITECT_SESSION_READY` content proof;
- the already-open fresh Architect session was reused;
- no additional fresh Architect tab was created;
- no duplicate handover was sent;
- old Architect closure occurred only after durable authority commit;
- rollover due/pending state cleared;
- the next Executor launch became eligible;
- first-suite, focused rollover/recovery tests and Node tests passed apart from one unrelated order-sensitive Windows Git relay fixture already known outside this repair boundary.

After production restart on the accepted checkpoint, Rony reported that the watcher was running again. This is evidence that the proven deadlock boundary was recovered; it is not permission to reopen or redesign unrelated accepted workflow foundations.

Architect classification for the Orchestrator core remains:

`PRODUCTION_MATURE_REFERENCE_IMPLEMENTATION`

New Orchestrator engineering must be justified by direct regression evidence, a clearly bounded maintenance need, or the universal-template extraction objective—not by speculative hardening.

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

Recorded PID is historical metadata until paired with current liveness and ownership evidence.

### Permanent lesson

Never kill broad Codex process groups based solely on a stored PID. Process identity requires current liveness and ownership evidence.

## Later stabilization failures and repairs

The 2026-09-13 incidents were followed by additional real-production defects during result delivery and rollover interaction. Those repairs are part of the same stabilization story even though they occurred after the original incident note.

Key accepted additions before the September 18 restart/rollover chain:

- `fce038e29698f18e13c7f9624bf6a80337b2cdc6` — exact result-delivery proof / recovery foundation;
- `87a214fe98a190827dd06f0456e0d34a687410d5` — same-task false-reconciliation correction;
- `9bc1d9cc030d0e0279bc06b953f354c01533df30` — durable SHA-256 delivery receipt;
- `ad29cca8b4eac1bf039c3e3b5a017a358fd57528` — rollover changed from workflow preemption to non-preemptive maintenance;
- `67d1a8d911f5bc839441eadeb69e2f20c16c4651` — confirmed-result recovery after rollover failure;
- `26f6644af1703abd2068198708ae7dbe54544f6a` — fact-based completed-confirmed workflow recovery;
- `57a3a914b35ed0c715aaaf0267ed0bd36a39cc78` — resident human-decision continuation.

The important architectural correction across these repairs was to stop letting stale error labels or session-maintenance state outrank stronger durable workflow facts.

## 2026-09-18 restart and rollover regression chain

### Production symptom

After task `000052` had already completed and the Architect had already staged task `000053`, the watcher repeatedly restarted into:

```text
state=NEXT_PROMPT_READY
taskId=000052
lastCompletedTaskId=000052
nextTaskId=000053
rolloverDue=True
```

Architect memory exceeded the rollover threshold, an already-open fresh Architect session visibly replied `ARCHITECT_SESSION_READY`, but:

- the old Architect session did not close;
- the fresh Architect session did not become authority;
- task `000053` did not launch;
- browser memory continued to increase;
- task `000052` must not be rerun because its result had already been delivered and reviewed.

The final diagnosis required reading the exact durable state and the live CDP target list instead of relying only on synthetic fixtures.

### Failure A — post-confirmation cleanup could downgrade confirmed delivery

Accepted repair:

`528c0b93ee013640c765fa0f4e81c5dc499b5a50` — `fix(orchestrator): preserve confirmed delivery after cleanup`

A cleanup failure after confirmed result delivery could still be interpreted as transport exhaustion. Cleanup became best-effort after terminal delivery proof, and the watcher remained resident after genuine exhaustion.

Permanent countermeasure:

**Once delivery is durably confirmed, later UI cleanup cannot revoke that workflow fact.**

### Failure B — fresh candidate reacquisition was too weak after ambiguous submission

Accepted repair:

`6896d732f4d2f1f450867a886d4efbb613c927c6` — `fix(orchestrator): reacquire proven fresh Architect candidate`

The recovery path gained deterministic proof based on:

- the authoritative handover;
- exact deterministic fresh bootstrap payload;
- a non-old ChatGPT conversation;
- exact terminal `ARCHITECT_SESSION_READY`.

Multiple proven candidates fail closed.

Permanent countermeasure:

**Fresh-session recovery must use transaction/content proof, not tab count or visual heuristics.**

### Failure C — rollover failure could erase the authority needed to recover it

Accepted repair:

`0bd18dad0b3f2766aedc1609f01b72ae1f44d3d7` — `fix(orchestrator): recover lost rollover handover authority`

A deferred rollover failure cleared `handoverRequested` and could remove `pending_handover` while candidate-recovery evidence remained. On restart, the watcher rediscovered the old handover but candidate lookup was still gated by the lost authority flag.

The repair reconstructs rollover authority from a valid visible/persisted handover before attempting candidate proof and preserves candidate-recovery evidence through deferred failure.

Permanent countermeasure:

**Recovery metadata must not destroy the transaction authority required to interpret that metadata.**

### Failure D — completed Executor PID remained active ownership

Accepted repair:

`5eeeae0401eb595389c8839822f93d70c8395219` — `fix(orchestrator): retire stale completed Executor ownership`

Exact live evidence showed:

```text
state=NEXT_PROMPT_READY
lastCompletedTaskId=000052
nextTaskId=000053
executorProcessState=COMPLETED_WITH_RESULT
codexPid=14872
```

Task `000052` could only have been recorded complete after its Executor was no longer active, but the historical PID remained in active ownership fields. If Windows later reused the same PID for another process, the NEXT_PROMPT_READY gate could interpret that unrelated process as the still-running Executor and silently block rollover/dispatch.

The repair:

- preserves the historical PID as `lastExecutorPid`;
- clears active `codexPid` / `active_codex_pid` ownership on proven completion;
- repairs old completed state on restart;
- never kills a process merely because its PID equals historical Executor metadata.

Permanent countermeasure:

**PID existence is not process ownership. Completed work must retire active ownership explicitly.**

### Failure E — stale persisted fresh-candidate ID outranked stronger live proof

Accepted repair:

`2cebe036eb89535ddd90cd52a679b6451ff112d6` — `fix(orchestrator): recover stale fresh candidate identity`

The exact persisted production state was:

```text
architectConversationId=6aac1369-7e48-83ec-b2bc-73797a88e6f5
rolloverFreshCandidateState=SUBMISSION_AMBIGUOUS
rolloverFreshCandidateConversationId=WEB:204cb572-6571-4b81-9bbd-25f5cedcb073
handoverRequested=True
pending_handover_present=True
```

The governed CDP endpoint `9333` showed the old Architect plus the actual already-ready fresh Architect:

```text
old = 6aac1369-7e48-83ec-b2bc-73797a88e6f5
fresh = 6aac98c9-571c-83ec-b11a-4bb8bc7744d1
```

The persisted candidate `204cb572...` no longer existed among live targets.

The bug was a stale-ID short circuit: when a persisted candidate ID existed but no live page matched it, candidate lookup returned `None` before reaching the deterministic bootstrap + `ARCHITECT_SESSION_READY` proof path. Because state remained `SUBMISSION_AMBIGUOUS`, reconciliation silently returned false forever.

The repair now treats a persisted candidate ID as a strong hint, not irreversible authority:

1. if the persisted ID is live, reuse it;
2. if it is stale, fall through to exact bootstrap + exact `ARCHITECT_SESSION_READY` proof;
3. if exactly one candidate is proven, replace the stale ID with the live identity and continue normal rollover commit;
4. if none are proven, fail closed with an explicit diagnostic;
5. if multiple are proven, fail closed as ambiguous;
6. do not create another tab merely because stale identity recovery is needed.

The exact regression fixture preserves:

```text
old candidate authority = 6aac1369-7e48-83ec-b2bc-73797a88e6f5
stale saved candidate   = WEB:204cb572-6571-4b81-9bbd-25f5cedcb073
actual ready candidate  = 6aac98c9-571c-83ec-b11a-4bb8bc7744d1
completed task          = 000052
staged next task        = 000053
```

Permanent countermeasure:

**Live transaction proof outranks stale persisted identity when the persisted identity is proven absent.**

A saved ID may accelerate recovery, but it must not block stronger deterministic evidence that the transaction continued under a different live conversation identity.

### Why the regression appeared after rollover had previously worked

The original rollover happy path was simpler:

```text
old Architect -> open fresh -> send handover -> wait ACK -> commit new -> close old
```

Later hardening added durable candidate identity/state so ambiguous submissions could survive restart without creating duplicate tabs. That improved safety but introduced new cross-state combinations. The content-proof recovery added later still retained an early stale-ID return, so the recovery machinery could block a happy path that had previously worked.

Permanent countermeasure:

**Every new durable recovery field creates cross-state combinations that must be qualified together, not only in isolated unit fixtures.**

## Why these defects escaped earlier tests

Earlier qualification was strong at the component level: parser behavior, IDLE continuation, transport, pause controls, remote monitoring, rollover helpers and recovery all had deterministic tests.

The missing proof was repeated composition through the real ChatGPT DOM, real process lifetime, real durable state and real project work.

The September 18 chain exposed a more specific qualification weakness: synthetic fixtures reproduced individual failure modes but did not initially replay the exact persisted production state across all interacting recovery fields. In particular, candidate-reacquisition tests covered no-ID recovery and still-live persisted IDs, but not the combination:

```text
persisted candidate ID exists
+ persisted ID is stale
+ exact ready fresh candidate still exists
+ candidate state is SUBMISSION_AMBIGUOUS
+ completed task and staged next task must both remain preserved
```

Permanent lessons:

- regression qualification is necessary, but browser-mediated orchestration also needs governed real production use;
- once a live incident supplies an exact `state.json` snapshot, that state should become a regression fixture;
- production diagnostics must identify fail-closed dispositions instead of silently returning in a polling loop;
- a real regression should reopen only the proven failing boundary—not trigger a full redesign of accepted foundations.

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
10. Stored process metadata is not liveness or ownership.
11. Production proof matters at browser/runtime boundaries.
12. Repair the proven boundary; do not reopen unrelated accepted foundations.
13. Recovery should prefer current durable facts over stale error labels.
14. Rollover is maintenance only and must never own project workflow authority.
15. HUMAN_REQUIRED may pause project execution while supervision remains resident.
16. Ordinary human/Architect discussion is not machine execution authority.
17. The Project Architect decides when human authority is sufficient; the Orchestrator does not infer business decisions.
18. Keep the Orchestrator boring.
19. Confirmed result delivery is terminal workflow evidence; later cleanup failure cannot revoke it.
20. Completed Executor ownership must be retired explicitly; historical PIDs must not block future work.
21. Persisted recovery identity is subordinate to stronger live transaction proof when the persisted identity is proven absent.
22. Ambiguous browser recovery must be based on exact transaction/content proof, not tab count or visual proximity.
23. Exact live incident state should be promoted into a deterministic regression fixture before further hardening.
24. Silent polling deadlocks are unacceptable at recovery boundaries; fail-closed outcomes need bounded diagnostics.

Canonical design target:

`recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action`

## Stabilization closure and universal-template implication

Universalization remains valid as the next separate engineering phase, but the extraction baseline must be refreshed before runtime implementation is copied into a generic repository.

The earlier universal-template extraction checkpoint `57a3a914...` predates the September 18 production lessons above. Any future `Orchestrator-Watcher` runtime extraction must start from a newly accepted production checkpoint that includes:

- confirmed-delivery terminal semantics;
- deterministic fresh-session bootstrap proof;
- lost-handover authority reconstruction;
- completed Executor ownership retirement;
- stale fresh-candidate identity correction;
- exact-state regression fixtures;
- bounded diagnostics for fail-closed recovery dispositions.

This does **not** mean the AFFOTECH source should be copied wholesale. AFFOTECH-specific repository paths, remote identity, Executor session, Architect conversation identity, bootstrap naming and project-specific validation assumptions must still be extracted into project profiles/configuration.

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
- completed process-ownership retirement;
- content-proven fresh-session reacquisition;
- stale persisted identity correction;
- one writer/project/task;
- project-profile isolation;
- one real browser-mediated smoke path before declaring a new project's production readiness.

## Remaining maintenance qualification gap

Deferred Architect rollover is now qualified through the exact September 18 NEXT_PROMPT_READY recovery state that had previously deadlocked, and production restart progressed again after the accepted stale-candidate-identity repair.

That does not prove every future browser/runtime failure mode. The protected invariant remains:

**rollover may be due, but maintenance must never halt or own project workflow authority.**
