# AFFOTECH Local Orchestrator — Production Incidents and Lessons

Original incident date: 2026-09-13

Latest incident/governance update: 2026-09-19

## Purpose

This document preserves the production failures discovered while moving the Local Orchestrator from deterministic qualification into real AFFOTECH use, the bounded repairs that followed, and the lessons that must remain part of future Orchestrator governance.

It is historical incident evidence. Current runtime authority and current qualification status live in:

`docs/ORCHESTRATOR_RUNTIME.md`

Universal-template readiness lives in:

`docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md`

Do not use an older incident-state snapshot below to override the current runtime contract.

## Current incident status

The original September 13 core-workflow stabilization remains accepted. September 18 and September 19 production use exposed additional Architect-rollover defects.

Current accepted source/test checkpoint for the rollover repair chain:

`49bf2c72699fdd8e0df2cbb31091b6751ffd6797` — `fix(orchestrator): wait for fresh bootstrap proof`

Current production rollover status:

`PRODUCTION_QUALIFICATION_IN_PROGRESS`

On the current September 19 qualification run, after Rony manually closed two stale fresh tabs created by an older broken build, the watcher remained running and the staged Codex child for task `000054` launched. This is meaningful progress beyond the previous failure boundary, but the incident is not yet declared closed until the entire real rollover transaction is independently verified.

Required closure evidence:

1. exactly one fresh Architect tab for the current transaction;
2. exact fresh bootstrap submitted exactly once;
3. `ARCHITECT_SESSION_READY` observed;
4. new Architect conversation identity durably committed;
5. old Architect closed only after authority commit;
6. task `000053` not rerun;
7. task `000054` launched exactly once;
8. no technical rollover safety cutout during the successful transaction.

Universal-template extraction is paused until this production qualification closes.

## Incident 1 — Playwright Sync bridge crossed thread ownership

### Symptom

Production restart crashed with Playwright/greenlet errors including thread-switch and Sync-API/async-loop failures. The watcher did not reach the intended next Executor task.

### Root cause

The remote discussion-control monitor created/first used a Playwright Sync bridge on the main thread and later reused it from a worker thread. Shutdown could also close a worker-owned bridge from the main thread.

### Repair

`1ec880e7d2bede1d56078cab433f3d2793ec008b` — `fix(orchestrator): keep remote control Playwright thread-affine`

### Permanent lesson

Browser automation object ownership includes thread/event-loop ownership. Create, use and close Playwright Sync objects on the same owning thread.

## Incident 2 — Remote startup declared unavailable too quickly

### Symptom

Production printed `REMOTE_CONTROL_UNAVAILABLE` even though the worker could still be initializing.

### Root cause

A two-second readiness wait was shorter than real Playwright attachment latency and could return failure before proving worker termination.

### Repair

`54a82e81be99eb1a85831cf27ccf37a634082191` — `fix(orchestrator): harden Architect bridge startup`

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

### Permanent lesson

There are three distinct input classes:

1. valid machine authority — process it;
2. intended but invalid machine authority — fail closed;
3. ordinary discussion — treat as discussion.

Malformed authority must never silently become non-authority.

## Incident 6 — Canonical envelope field-order mismatch

### Symptom

The Architect returned all required fields but in a different order from the parser contract.

### Canonical order

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

## Later stabilization repairs before the September 18 rollover chain

Key accepted additions:

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

### Failure C — rollover failure could erase authority needed to recover it

Accepted repair:

`0bd18dad0b3f2766aedc1609f01b72ae1f44d3d7` — `fix(orchestrator): recover lost rollover handover authority`

Permanent countermeasure:

**Recovery metadata must not destroy the transaction authority required to interpret that metadata.**

### Failure D — completed Executor PID remained active ownership

Accepted repair:

`5eeeae0401eb595389c8839822f93d70c8395219` — `fix(orchestrator): retire stale completed Executor ownership`

The repair preserves the historical PID as `lastExecutorPid`, clears active ownership after proven completion, repairs old completed state on restart, and never kills a process merely because its PID equals historical metadata.

Permanent countermeasure:

**PID existence is not process ownership. Completed work must retire active ownership explicitly.**

### Failure E — stale persisted fresh-candidate ID outranked stronger live proof

Accepted repair:

`2cebe036eb89535ddd90cd52a679b6451ff112d6` — `fix(orchestrator): recover stale fresh candidate identity`

The persisted candidate ID no longer existed among live targets, while one live fresh Architect already contained the exact bootstrap and `ARCHITECT_SESSION_READY`.

The repair treats a persisted candidate ID as a strong hint, not irreversible authority:

1. if the persisted ID is live, reuse it;
2. if it is stale, fall through to exact bootstrap + exact `ARCHITECT_SESSION_READY` proof;
3. if exactly one candidate is proven, replace the stale ID with the live identity;
4. if none are proven, fail closed;
5. if multiple are proven, fail closed as ambiguous;
6. do not create another tab merely because stale identity recovery is needed.

Permanent countermeasure:

**Live transaction proof outranks stale persisted identity when the persisted identity is proven absent.**

## 2026-09-19 rollover production regression chain

September 19 proved that the September 18 recovery was not the final rollover qualification. The watcher later remained stuck overnight with task `000053` completed, task `000054` staged and rollover due. Browser memory grew to multiple GiB while visible browser activity continued and no project progress occurred.

The repair chain below was intentionally narrowed around directly observed production failures.

### Failure F — broad assistant DOM extraction mixed semantic response with ChatGPT UI

Initial repair:

`3da7f7a5cd9d9073264134e2f749df7b32c40196` — `fix(orchestrator): sanitize Architect assistant message extraction`

Later production diagnostics proved button removal alone was insufficient. A valid `ARCHITECT_HANDOVER_READY` writing-block response was followed in the broad assistant surface by ChatGPT suggested-followup UI. The strict handover predicate therefore rejected a semantically valid handover.

DOM diagnostics established the authoritative semantic boundary:

`[data-testid="writing-block-container"]`

and the contaminating UI boundaries:

- `[data-testid="writing-block-suggested-followups"]`;
- `[data-testid="writing-block-suggested-followups-surface"]`;
- visible follow-up buttons;
- `aria-hidden=true` helper duplicates.

Permanent countermeasure:

**Extract the semantic response payload structurally; never weaken a strict authority predicate merely because surrounding product UI contains extra text.**

### Failure G — rollover recovery could repeat while browser memory/resource usage grew

Accepted repair:

`1cdd74ecbb0fe286426bf4219feb370fb5adc3e1` — `fix(orchestrator): bound rollover recovery failure`

The repair introduced bounded recovery attempts, retry timing, a safety window, memory safety cutout and terminal `HUMAN_REQUIRED` behavior while preserving the staged next task and completed task identity.

Permanent countermeasure:

**Maintenance recovery must be bounded. A broken rollover may fail closed, but it may not retry indefinitely or rerun completed project work.**

### Failure H — production behavior was not observable enough to diagnose safely

Accepted instrumentation:

`c7c6bbbc2959d1eb9e8b181802a8e2015df3580e` — `chore(orchestrator): add opt-in rollover diagnostic trace`

Diagnostic mode:

`ORCHESTRATOR_DIAGNOSTIC_TRACE=1`

Diagnostic root:

`.agent-work/orchestrator/logs/diagnostic/<runId>/`

The trace records state transitions, Playwright attach/close, handover/fresh-session operations, memory samples, snapshots, errors and shutdown evidence. Later tracing also included remote-control poll activity and exact assistant-entry errors.

Permanent countermeasure:

**A browser-mediated state machine must expose the exact operation boundary that failed; generic `Error` labels are not sufficient repair authority.**

### Failure I — semantic extraction source was correct in concept but invalid JavaScript in production

Initial semantic repair:

`ee59ce4c20f13e408f2f61af0c26d260440d2fbe` — `fix(orchestrator): extract semantic Architect responses`

Production immediately failed inside the first `_assistant_entries()` `page.evaluate()`.

Architect source inspection found the exact defect: the embedded JavaScript lived in a normal Python triple-quoted string and contained:

```text
.join('\n')
```

Python converted the escape before Playwright received it, producing a literal newline inside a JavaScript quoted string and making the emitted script syntactically invalid.

The previous unit test had not parsed or executed the emitted JavaScript. Its fake `Page.evaluate()` merely asserted strings were present and returned a fabricated semantic result, allowing all tests to pass while production JavaScript was broken.

Accepted repair:

`9f1026cec05c56e67a164cead1fe61d5c0ba2ff4` — `fix(orchestrator): repair executable semantic extraction script`

The runtime JavaScript was factored into the exact string used by production, emitted safely, and parsed by Node in regression qualification. Diagnostic error tracing was also added for assistant-entry evaluation failures.

Permanent countermeasures:

- **Injected JavaScript must be parsed/executed by a real JavaScript engine in qualification; fake `evaluate()` return values are not sufficient.**
- **Executor/Maintainer PASS reports are claims, not acceptance evidence; the Architect must inspect the actual repo and the complete affected control path.**

### Failure J — fresh bootstrap was pasted but Send acknowledgement timed out

After semantic extraction was repaired, production progressed further. The watcher found `ARCHITECT_HANDOVER_READY`, opened a fresh Architect tab and pasted the handover/bootstrap, but the prompt did not visibly submit.

The runtime logged:

```text
ARCHITECT_SESSION_ROLLOVER_FAILED
phase=OPEN_FRESH_WITH_HANDOVER
errorClass=ResultSubmissionError
errorMessage=ARCHITECT_SUBMISSION_ACK_TIMEOUT:TimeoutError
```

The broken recovery then opened another fresh tab on the next attempt, producing two stale fresh tabs with pasted handovers.

Source inspection found two defects:

1. if `send.click()` returned without actually submitting, Enter fallback was not attempted because fallback was only tied to a click exception;
2. an unsent fresh page at `https://chatgpt.com/` had no conversation ID and could not satisfy existing candidate proof, so recovery could allocate another fresh page.

Accepted repair:

`710b177ad041ce2b8a7ab070f58e7d4b638e4b5a` — `fix(orchestrator): enforce single fresh rollover tab`

The repair:

- enforces at most one fresh page per rollover transaction;
- reuses the in-process owned page;
- forbids replacement fresh-page allocation once creation is recorded;
- adds same-page unsent submission reconciliation;
- fails closed if restart cannot safely identify the previous ambiguous page.

Permanent countermeasure:

**A rollover transaction owns one fresh tab. Submission ambiguity is reconciled on that same page or fails closed; it never authorizes another fresh page.**

### Failure K — weak UI signals could falsely count as fresh-bootstrap delivery

Source review of `710b177a...` found that `submit_result_bounded()` could still treat any of these as acknowledgement:

- composer empty;
- generation visible;
- assistant count increased.

Those signals did not prove the exact fresh bootstrap existed as a submitted user message.

Accepted repair:

`55ab455f6c8799805379481bc6f219f0b7e4aa7b` — `fix(orchestrator): require exact fresh bootstrap proof`

`open_fresh_with_handover()` now requires the exact deterministic bootstrap to be observed as a submitted user message. If exact proof is absent, only same-page reconciliation is allowed.

Permanent countermeasure:

**Fresh-bootstrap authority requires exact submitted-user-message proof. Composer state or generation state alone is supporting evidence, not delivery authority.**

### Failure L — immediate exact-message check could create a false negative during normal DOM delay

Architect review of `55ab455f...` found a timing race: after a successful send, ChatGPT could clear the composer before mounting the submitted user-message DOM. A single immediate exact-message check could therefore fail and classify a successful send as ambiguous.

Accepted source/test repair:

`49bf2c72699fdd8e0df2cbb31091b6751ffd6797` — `fix(orchestrator): wait for fresh bootstrap proof`

The repair adds:

- `FRESH_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS = 2.0`;
- `FRESH_BOOTSTRAP_OBSERVATION_POLL_SECONDS = 0.1`;
- bounded polling for the exact submitted bootstrap before same-page reconciliation;
- regression where exact message observation is false, false, then true without invoking the fallback;
- real weak-signal fixtures for composer-empty, generation-visible and assistant-started behavior.

Permanent countermeasure:

**Dynamic DOM proof must use a bounded observation window where eventual mounting is expected. Immediate absence is not proof of failed submission.**

## Why the September 19 defects escaped earlier tests

Earlier qualification was strong at the component level, but several tests simulated away the browser behavior that production depended on.

The most important examples were:

- fake `Page.evaluate()` returned the desired semantic result without parsing the JavaScript sent to Playwright;
- a successful rollover fixture could immediately create the fresh conversation ID and `ARCHITECT_SESSION_READY`, bypassing the timing and acknowledgement behavior of the real ChatGPT UI;
- weak-signal tests initially named composer/generation/assistant conditions without actually driving those runtime signals;
- separate helper PASS results were treated too readily as evidence that the whole rollover transaction worked.

The corrected acceptance rule is:

**For browser-mediated rollover, component tests are necessary but not sufficient. Production closure requires the complete real transaction to succeed as one observable unit.**

Executor/Maintainer reports remain useful evidence pointers, but the Architect must inspect the GitHub implementation, trace the complete affected control path and independently determine what the tests really prove before acceptance.

## Current one-rollover contract

The intended transaction is now:

```text
old Architect
-> obtain/reuse one authoritative handover
-> create ONE fresh tab
-> populate deterministic fresh bootstrap
-> perform initial send
-> wait bounded time for exact submitted-user-message proof
-> if safely unsent, perform at most one same-page alternate send
-> require exact bootstrap as submitted user message
-> wait for exact ARCHITECT_SESSION_READY
-> durably commit new Architect identity
-> close old Architect
-> allow normal dispatcher to launch the already-staged next task exactly once
```

Failure rules:

- no second fresh tab in one rollover transaction;
- no duplicate bootstrap submission;
- no task rerun;
- no authority switch without exact fresh-session ready proof;
- no replacement-tab creation after restart when an ambiguous prior page cannot be identified;
- fail closed to technical `HUMAN_REQUIRED` when exact authority cannot be proven.

## Current production qualification evidence

Before the current clean qualification:

- the two stale fresh tabs from the previous broken run were manually closed by Rony;
- task `000053` remained completed;
- task `000054` remained staged;
- the task `000054` prompt SHA-256 remained unchanged through state recovery;
- `freshCandidateConversationId=None`;
- `freshPageCreated=None`;
- valid `pending_handover` remained present and ended with `ARCHITECT_HANDOVER_READY`;
- `handoverRequested=False`, which matches the source branch that reconstructs persisted handover authority safely.

Current live observation:

- watcher is running;
- a Codex child for the staged next task has launched.

This is not yet enough to mark the rollover incident closed. Final verification still requires the one-tab / one-bootstrap / ready / authority-switch / old-tab-close / exactly-once `000054` launch evidence described at the top of this document.

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
25. Semantic response extraction must exclude product UI structurally rather than weakening machine-authority predicates.
26. Browser-injected JavaScript must be validated as the exact emitted script by a real JavaScript engine.
27. One rollover transaction owns at most one fresh tab.
28. Exact fresh-bootstrap submitted-user-message proof outranks composer/generation/assistant transition signals.
29. Dynamic ChatGPT DOM transitions require bounded observation before absence becomes failure evidence.
30. Executor/Maintainer PASS reports are claims; Architect acceptance requires independent repository and control-path inspection.
31. Browser-mediated feature acceptance is end-to-end: helper/unit PASS counts cannot substitute for a successful real transaction.

Canonical design target:

`recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action`

## Universal-template implication

The reference design remains suitable for future universal extraction, but extraction is paused until the current rollover production qualification closes.

Any future universal runtime must start from the then-current accepted production checkpoint and preserve at minimum:

- confirmed-delivery terminal semantics;
- semantic response boundaries rather than broad product-surface text;
- executable/validated injected JavaScript;
- deterministic fresh-session bootstrap proof;
- lost-handover authority reconstruction;
- completed Executor ownership retirement;
- stale fresh-candidate identity correction;
- one-fresh-tab rollover transaction;
- same-page ambiguous-send reconciliation;
- exact fresh-bootstrap submitted-user-message proof;
- bounded observation for delayed DOM mounting;
- exact-state regression fixtures;
- bounded diagnostics for fail-closed recovery dispositions;
- one real browser-mediated smoke path before declaring a new project's production readiness.

This does **not** mean the AFFOTECH source should be copied wholesale. AFFOTECH-specific repository paths, remote identity, Executor session, Architect conversation identity, bootstrap naming and project-specific validation assumptions must still be extracted into project profiles/configuration.

See:

`docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md`

## Remaining qualification boundary

The September 19 source/test repair is accepted at `49bf2c72...`.

The real production rollover qualification is still open while the current run proceeds.

Protected invariant:

**rollover may be due, but maintenance must never rerun completed work, duplicate the fresh Architect page/bootstrap, or own project workflow authority.**
