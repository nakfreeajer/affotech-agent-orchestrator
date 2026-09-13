# AFFOTECH Local Orchestrator — Production Incidents and Lessons (2026-09-13)

## Purpose

This document records the production failures discovered while moving the Local Orchestrator from isolated qualification into a real AFFOTECH milestone, the bounded repairs applied, and the lessons that must remain part of future Orchestrator governance.

It complements `docs/ORCHESTRATOR_RUNTIME.md`. It does not change runtime behavior.

Runtime source authority at the close of this incident chain:

- `216a57d0635bfa0e6342f276ab880b250afe4003` — `fix(orchestrator): fail closed on invalid Architect envelope`
- full regression at that checkpoint: `273 passed / 0 failed`
- Python compile: PASS
- `git diff --check`: PASS

The production run after the final envelope correction was reported by the final human authority as working and is being monitored. The current AFFOTECH milestone is `QUOTATION.LIFECYCLE.MUTATION.INTEGRITY.GUARD.1A`; this document does not alter that project milestone or its authority.

## Preserved production boundary

Before the new quotation milestone was allowed to start, the previously completed task boundary was:

- `taskId=000025`
- `lastCompletedTaskId=000025`
- task `000025` completed and must never be rerun or have its result resent
- the deliberate earlier `HUMAN_REQUIRED` boundary was released once, under human authority, to permit continuation after the new roadmap decision

The incident repairs were required to preserve that completed boundary while preventing accidental replay or unauthorized task creation.

## Incident 1 — Playwright Sync bridge crossed thread ownership

### Symptom

The first production restart crashed with Playwright/greenlet errors including:

- `cannot switch to a different thread`
- `It looks like you are using Playwright Sync API inside the asyncio loop`
- `TargetClosedError`

The watcher never reached the intended next Executor task.

### Root cause

The remote discussion-control monitor created and first used its Playwright Sync bridge on the main thread during startup baseline creation, then reused the same bridge from a worker thread.

The old shutdown path could also close that worker-used bridge from the main thread.

Playwright Sync objects are thread-affine. Creation, use and close must remain on the same owning thread.

### Repair

Milestone:

`ORCH.REMOTE.DISCUSSION.CONTROL.THREAD.AFFINITY.REPAIR.1B`

Commit:

`1ec880e7d2bede1d56078cab433f3d2793ec008b`

`fix(orchestrator): keep remote control Playwright thread-affine`

Repair behavior:

- remote worker thread owns bridge CREATE, USE, rollover/reattach and CLOSE;
- `start()` no longer creates the bridge on the main thread;
- worker signals startup readiness through thread-safe events;
- main-thread `stop()` signals and joins but does not close the worker-owned bridge;
- worker cleanup closes the bridge in its own `finally` path;
- remote monitor failures are isolated from project workflow state;
- conversation rollover remains thread-affine.

Regression result:

- focused tests: `12 passed / 0 failed`
- full suite: `262 passed / 0 failed`
- compile PASS
- diff check PASS

### Lesson

A browser automation object is not merely a data object. Ownership includes the thread/event-loop context that created it. Any background monitor must own the complete browser-object lifecycle on one thread.

Do not qualify thread-affinity by testing only method behavior; tests must record creator/use/close thread identities and prove they are the same.

## Incident 2 — Remote startup reported unavailable after only two seconds

### Symptom

After the thread-affinity repair, the next production restart printed:

`REMOTE_CONTROL_UNAVAILABLE`

while the rest of the watcher continued toward IDLE processing.

### Root cause

The remote worker startup handshake used only:

`self._ready.wait(2.0)`

Real Playwright attachment can legitimately take longer than two seconds. The main thread could therefore declare failure while the worker was still initializing, creating a false-unavailable race and risking an orphan worker that might later become active.

### Repair

Milestone:

`ORCH.PRODUCTION.ARCHITECT.BRIDGE.STARTUP.REPAIR.1C`

Commit:

`54a82e81be99eb1a85831cf27ccf37a634082191`

`fix(orchestrator): harden Architect bridge startup`

Repair behavior:

- remote startup gets a realistic bounded attachment budget;
- startup timeout/failure signals the worker to stop;
- failed startup joins the worker within a bounded shutdown timeout;
- `start()` cannot return false while a worker can later become active;
- workflow state remains unchanged on remote-monitor startup failure;
- worker-thread Playwright ownership from 1B remains preserved.

Regression result at this repair:

- focused tests: `14 passed / 0 failed`
- full suite: `267 passed / 0 failed`
- compile PASS
- diff check PASS

### Lesson

A timeout is part of protocol semantics. An unrealistically short timeout can turn a healthy asynchronous startup into a false failure and can create dual authority if the supposedly failed worker later activates.

Whenever startup is bounded, failure must also prove termination, not merely stop waiting.

## Incident 3 — Stale composer locator and one-second verification crash

### Symptom

The second production restart reached IDLE, attempted Architect contact, then crashed with:

`Locator.inner_text: Timeout 1000ms exceeded`

while waiting for:

`locator("#prompt-textarea").last`

The generic IDLE bootstrap text was visibly left inside the Architect composer but had not been sent.

### Root cause

The composer resolver treated DOM presence of `#prompt-textarea` as sufficient authority. ChatGPT can rerender the editor, leaving a stale/hidden selector while the semantic live textbox is the actual current composer.

The send pipeline then attempted post-input verification through a locator that was no longer the live actionable composer and used a very short one-second operation timeout.

The pre-send failure propagated far enough to terminate the resident watcher.

### Repair

Included in `ORCH.PRODUCTION.ARCHITECT.BRIDGE.STARTUP.REPAIR.1C` at commit `54a82e81...`.

Repair behavior:

- prefer the current semantic textbox;
- require composer candidates to be visible and editable;
- retain `#prompt-textarea` only as an actionable compatibility fallback;
- re-resolve the composer after DOM rerenders instead of trusting one stale locator;
- use bounded verification appropriate to the live composer path;
- a proven pre-send failure is nonfatal to the resident state machine;
- IDLE remains IDLE when no send occurred;
- ambiguous/attempted send remains fail-closed and is never treated as a safe retry.

### Lesson

DOM identity is not UI authority. `count() > 0` proves only that a node exists, not that it is the current interactive control.

For dynamic web UIs, every mutation/verification stage must reacquire and prove the current actionable element.

## Incident 4 — Unsent Orchestrator payload remained in the human composer

### Symptom

The production failure left the full generic IDLE bootstrap request visibly populated in the Project Architect composer even though the send button had never been activated.

This could confuse the human into believing the text was intentional pending work or tempt a manual send that would alter workflow authority.

### Root cause

Composer population succeeded before verification failed. The old pipeline had no exact pre-send cleanup rule for its own known payload.

### Repair

Also included in `54a82e81...`.

Repair behavior:

- after a proven pre-send failure, re-resolve the live composer;
- compare normalized current composer text to the exact known Orchestrator payload;
- clear only on exact match;
- never clear unrelated human-authored text;
- never clear on an attempted or ambiguous send;
- log `ARCHITECT_UNSENT_PAYLOAD_CLEARED` only after safe exact-match cleanup.

### Lesson

Cleanup is an authority-sensitive action. The Orchestrator may clean up only text it can prove it owns.

A non-empty composer is never sufficient authority to clear it.

## Incident 5 — Malformed Architect envelope was mistaken for ordinary discussion

### Symptom

The intended quotation roadmap decision did not start the Executor. Instead, the watcher attempted to send a new generic message:

`Review the current authoritative project state after the completed work...`

This was the wrong action because the Architect had already made the roadmap decision.

### Root cause

The first corrected Architect response still had an invalid machine envelope. Earlier, the original envelope had omitted `taskId=000025`.

The IDLE logic determined `response_has_envelope` by asking whether the strict parser could extract a task ID. A malformed `<ORCHESTRATOR_RESULT>` therefore returned no task ID and was incorrectly reclassified as ordinary non-envelope content.

That allowed the generic IDLE continuation/bootstrap path to run, effectively requesting a second roadmap decision instead of failing closed on a malformed existing decision.

### Repair

Milestone:

`ORCH.IDLE.ARCHITECT.ENVELOPE.FAIL.CLOSED.1D`

Commit:

`216a57d0635bfa0e6342f276ab880b250afe4003`

`fix(orchestrator): fail closed on invalid Architect envelope`

Repair behavior:

- presence of the exact canonical opening marker `<ORCHESTRATOR_RESULT>` means the response is attempting machine authority;
- the marker is recognized before successful strict parsing is required;
- if that intended envelope fails canonical validation, IDLE does not bootstrap again;
- state moves to `HUMAN_REQUIRED`;
- `humanRequiredReason=ARCHITECT_ENVELOPE_INVALID`;
- no next task is created;
- no Executor launches;
- task `000025` remains preserved;
- ordinary prose that merely mentions “orchestrator” remains ordinary discussion.

Regression result:

- focused tests: `6 passed / 0 failed`
- full suite: `273 passed / 0 failed`
- compile PASS
- diff check PASS

### Lesson

Malformed authority must never be silently downgraded into non-authority.

There are three distinct input classes:

1. valid machine authority — process it;
2. intended but invalid machine authority — fail closed;
3. ordinary discussion — follow discussion/continuation rules.

The boundary between classes 2 and 3 must be explicit and deterministic.

## Incident 6 — Canonical envelope field order mismatch

### Symptom

After adding the missing `taskId`, the Project Architect returned all required fields but in this order:

`classification`

`action`

`documentation`

`taskId`

The runtime parser expected:

`classification`

`action`

`taskId`

`documentation`

The content was semantically correct but still parser-invalid.

### Action taken

The Architect was given a format-correction-only instruction. The audit and complete Executor prompt were preserved unchanged; only the machine envelope field order was corrected.

The final accepted envelope became:

```text
<ORCHESTRATOR_RESULT>
classification=ACCEPTED
action=EXECUTE
taskId=000025
documentation=COMPLETE
promptBegin
<existing QUOTATION.LIFECYCLE.MUTATION.INTEGRITY.GUARD.1A prompt>
promptEnd
</ORCHESTRATOR_RESULT>
```

No audit was redone and no milestone was changed.

### Lesson

Producer and parser contracts must be verified literally, not semantically.

When a machine protocol is order-sensitive, “all required fields are present” is not enough. The Architect must validate exact canonical order before allowing a production restart.

This also exposes design debt: a future protocol revision may consider key-based/order-insensitive parsing, but such a change must be separately governed. The current production contract remains the canonical ordered envelope.

## Incident 7 — One-time HUMAN_REQUIRED release encountered UTF-8 BOM

### Symptom

The first guarded state-release attempt failed before mutation with:

`json.decoder.JSONDecodeError: Unexpected UTF-8 BOM`

### Root cause

The durable state file contained a UTF-8 BOM while the recovery read used plain UTF-8.

### Action taken

The one-time human-authorized recovery read was changed to `utf-8-sig` and then successfully transitioned only the deliberate boundary:

- `HUMAN_REQUIRED -> IDLE`
- `taskId` remained `000025`
- `lastCompletedTaskId` remained `000025`
- no `000026` was manually created
- the previous state was backed up

The release script was explicitly one-time and must not be rerun because later browser/runtime failures do not recreate roadmap authority.

### Lesson

Recovery tooling must tolerate the durable encoding actually present on disk and must fail before mutation when parsing is uncertain.

A failed startup after a human boundary release does not authorize repeating the release.

## Incident 8 — Recorded Codex PID was mistaken for possible active work

### Observation

A separate status terminal showed:

- `state=IDLE`
- `taskId=000025`
- `lastCompletedTaskId=000025`
- recorded `codexPid=15144`
- `processAlive=False`
- prior Executor state `COMPLETED_WITH_RESULT`

### Conclusion

The PID was historical state, not proof of a running Executor. The status monitor was read-only and was safe to leave open.

### Lesson

A stored PID is evidence only when paired with a current liveness check. Operator displays should distinguish “recorded PID” from “live child” clearly.

Never kill broad Codex process groups based solely on a stale recorded PID.

## Why these defects escaped earlier tests

Most prior qualification was component-level and deterministic:

- parser behavior;
- IDLE continuation;
- exactly-once transport;
- pause controls;
- remote monitor behavior;
- rollover;
- state recovery.

The missing proof was the exact real-world composition:

`completed task -> deliberate HUMAN_REQUIRED -> new roadmap authority -> corrected Architect response -> real ChatGPT DOM -> remote monitor + normal Architect bridge -> IDLE envelope consumption -> next task staging`

The first production attempt exposed thread ownership.

The second exposed startup timing and live-composer behavior.

The visible unsent draft then exposed the malformed-envelope governance hole.

The final manual parser check exposed the field-order contract mismatch before another restart.

### Lesson

Passing unit/regression suites is necessary but does not equal production proof for browser-mediated orchestration.

For browser/runtime boundaries, acceptance requires both:

- deterministic regression qualification; and
- at least one real governed production path through the exact boundary.

A previously accepted milestone may be reopened only when direct regression evidence exists. That is what happened here; reopening was justified and bounded rather than a full re-audit.

## Permanent governance lessons

1. **Preserve completed work first.** A transport/browser failure must never cause task `000025` to rerun or its result to be resent.
2. **Browser object ownership includes thread ownership.** Create/use/close Playwright Sync objects on one thread.
3. **Startup failure must prove shutdown.** Timeout without termination proof is unsafe.
4. **Dynamic DOM presence is not actionability.** Reacquire current visible/editable controls after rerender.
5. **Pre-send and post-send failures are different authority classes.** Proven pre-send failure can safely remain/retry; attempted or ambiguous send must reconcile, never blindly retry.
6. **Only clear text the Orchestrator can prove it owns.** Exact normalized payload match is required.
7. **Malformed machine authority fails closed.** Never reinterpret an invalid `<ORCHESTRATOR_RESULT>` as ordinary discussion.
8. **Machine protocols are literal contracts.** Validate exact required field order and delimiters before production continuation.
9. **Human boundary release is one-time authority.** A later technical crash does not recreate that authority.
10. **Stored process metadata is not liveness.** Verify the process before acting.
11. **Production proof matters.** Real ChatGPT/Playwright behavior can reveal integration defects that fakes cannot.
12. **Do not overreact to regression.** Repair the smallest proven boundary; do not reopen unrelated accepted foundations.
13. **Keep the Orchestrator boring.** `recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action` remains the design target.

## Repair chain preserved by this incident record

- `9e56d5655a1e40050269245c1b35996f67ddd709` — initial remote discussion-control implementation.
- `dcc51aa59d6162c607306fd60e330d5b1d8a26f9` — documentation closure for remote discussion control; production later exposed a direct regression in the accepted runtime behavior.
- `1ec880e7d2bede1d56078cab433f3d2793ec008b` — Playwright thread-affinity repair.
- `54a82e81be99eb1a85831cf27ccf37a634082191` — remote startup/composer/pre-send cleanup hardening.
- `216a57d0635bfa0e6342f276ab880b250afe4003` — fail closed on invalid intended Architect envelope.

The accepted repairs are additive. Do not re-audit them without new direct regression evidence.

## Current operational rule after recovery

For the currently running production path:

- monitor the Orchestrator rather than manually sending the quotation Executor prompt;
- do not rerun the one-time HUMAN_REQUIRED release;
- do not manually reuse task `000025`;
- allow the canonical envelope for `000025` to stage the next sequential task through the normal dispatcher;
- use F9/F10 locally or exact `ORCH:PAUSE` / `ORCH:RESUME` remotely only for discussion-pause control;
- do not introduce new Orchestrator engineering while the real AFFOTECH milestone is proving the current protocol unless new regression evidence appears.

## Future template implication

After a real AFFOTECH milestone completes successfully through this final protocol, universal-template work should carry these lessons forward so a new project does not repeat the same month-long transport/recovery stabilization.

At minimum, the template qualification suite should include:

- thread-aware browser bridge ownership tests;
- delayed-start and failed-start termination tests;
- live-composer rerender tests;
- exact unsent-payload cleanup tests;
- malformed machine-envelope fail-closed tests;
- producer/parser canonical-envelope compatibility tests;
- deliberate HUMAN_REQUIRED continuation tests;
- exactly-once result transport tests;
- discussion-pause tests during Executor and RESULT_READY states;
- one real browser-mediated smoke path before declaring production readiness.

The template must preserve final human authority and must not turn discussion text, malformed envelopes, stale process metadata, or browser ambiguity into execution authority.
