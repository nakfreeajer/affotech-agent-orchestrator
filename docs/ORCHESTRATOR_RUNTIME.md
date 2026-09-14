# AFFOTECH Local Orchestrator Runtime

## Current authority

Repository: `nakfreeajer/affotech-agent-orchestrator`

Branch authority: remote `main`

Current accepted Orchestrator runtime checkpoint:

`57a3a914b35ed0c715aaaf0267ed0bd36a39cc78` — `fix(orchestrator): keep human decisions resident`

Parent:

`26f6644af1703abd2068198708ae7dbe54544f6a`

Accepted deterministic qualification for `57a3a914...`:

- Python compile: PASS
- full regression: `244 passed / 0 failed`
- `git diff --check`: PASS
- resident HUMAN_REQUIRED regression: PASS
- ordinary human/Architect discussion regression: PASS
- later same-task EXECUTE envelope regression: PASS
- repeated HUMAN_REQUIRED regression: PASS
- STOP-remains-resident regression: PASS
- temporary Architect attach retry regression: PASS
- single-next-task launch regression: PASS
- no-format-recovery-during-discussion regression: PASS
- existing recovery, rollover and durable receipt regressions: PASS

Runtime/source/test authority is GitHub. Local `.agent-work/orchestrator/` state is durable workflow state and is not source/history authority.

For this repository, the Architect also performs Documentation Curator duties. Runtime/source/test mutation remains bounded Maintainer/Executor work. The Architect independently verifies implementation evidence and may directly synchronize Orchestrator governance/history Markdown after acceptance.

## Purpose

The Local Orchestrator automates the durable loop:

`Project Architect -> Orchestrator -> one visible Executor task -> Orchestrator -> Project Architect`

The Orchestrator is deliberately project-generic. It transports and supervises authority; it does not become the product Architect.

Project architecture, roadmap, business policy, permission decisions, document ownership and milestone meaning remain with the configured Project Architect and final human authority.

Permanent workflow invariant:

`recover where stopped -> run one task -> capture one result -> deliver once -> wait -> stage one next action -> repeat`

Rollover, browser recovery, discussion pause and HUMAN_REQUIRED handling exist only to preserve this loop. They must never become an alternate workflow authority.

## Current AFFOTECH production boundary

The latest observed AFFOTECH boundary before resident HUMAN_REQUIRED production requalification is:

- `state=HUMAN_REQUIRED`
- `taskId=000043`
- `lastCompletedTaskId=000043`
- `humanRequiredReason=ARCHITECT_DECISION_HUMAN_REQUIRED`
- task `000043` is completed, accepted and documented
- its Executor work must not be rerun and its result must not be resent

The Project Architect is waiting for Rony's next narrow quotation-permission business decision.

The runtime repair at `57a3a914...` is deterministically regression-qualified for keeping this state resident. A fresh real production run through the full remote human-decision continuation path is still required before that path should be called end-to-end production-proven.

## Canonical durable states

Normal states:

- `IDLE`
- `NEXT_PROMPT_READY`
- `EXECUTOR_RUNNING`
- `RESULT_READY`
- `ARCHITECT_RUNNING`
- `HUMAN_REQUIRED`

Exceptional retained state:

- `EXECUTOR_CRASHED`

There is deliberately no `PAUSED` workflow state. Human discussion pause is a durable overlay and never replaces the truthful underlying workflow state.

Only the production main dispatcher may perform `NEXT_PROMPT_READY -> EXECUTOR_RUNNING`. Recovery, transport, parsing, documentation, rollover and pause helpers may restore or inspect state but must not directly create an alternate launch path.

## Canonical sequence

1. Start watcher and acquire the single-instance lock.
2. Load durable state and recover exactly where execution stopped.
3. `NEXT_PROMPT_READY`: validate the owned task worktree and launch exactly one visible Executor child.
4. `EXECUTOR_RUNNING`: observe that child; no wall-clock timeout and no duplicate launch.
5. Child completion with a non-empty result -> `RESULT_READY`.
6. Deliver the result to the Project Architect exactly once.
7. Confirm or reconcile result delivery -> `ARCHITECT_RUNNING`.
8. Observe the Project Architect decision.
9. Interpret the decision into exactly one of:
   - `EXECUTE` -> stage exactly one next sequential task -> `NEXT_PROMPT_READY`;
   - `HUMAN_REQUIRED` -> preserve the completed task and enter resident human-decision wait;
   - `STOP` -> no currently authorized work; return to resident `IDLE` semantics.
10. Repeat without reusing a reviewed task ID.

If task `000043` is reviewed and a later same-task Architect envelope authorizes `EXECUTE`, the Orchestrator allocates the next sequential task. The Architect must not invent the next task ID.

## Canonical Architect envelope

Production Architect machine authority is:

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

The envelope is the final machine-authoritative content of the Architect response.

The field order above is parser-significant in the current protocol. Legacy envelopes without `documentation=` remain supported only for backward compatibility.

Ordinary discussion text is not machine authority.

Presence of the literal opening marker `<ORCHESTRATOR_RESULT>` means the response is attempting machine authority. An attempted but invalid envelope fails closed and must never be silently downgraded into ordinary prose.

## Resident HUMAN_REQUIRED contract

`HUMAN_REQUIRED` has more than one cause. Technical failure states remain fail-closed according to their reason. The special deliberate Project Architect decision boundary is:

`humanRequiredReason=ARCHITECT_DECISION_HUMAN_REQUIRED`

For this reason, `57a3a914...` changes supervision semantics:

- project execution pauses;
- the watcher process remains resident;
- no Executor launches while authority is missing;
- the completed task is preserved;
- the completed result is not resent;
- the watcher stays attached to the current authoritative Architect conversation;
- ordinary Architect discussion prose is expected and does not trigger format recovery;
- the observation baseline advances after ordinary discussion;
- a later valid envelope for the same completed task is consumed through the normal parser and staging path;
- a later `EXECUTE` stages exactly one next task;
- a later `HUMAN_REQUIRED` remains resident;
- a later `STOP` means no currently authorized work and returns to resident no-work/IDLE semantics rather than terminating supervision;
- temporary Architect attach/read failure preserves HUMAN_REQUIRED, disconnects safely, waits, and retries instead of killing the watcher.

The watcher does not interpret Rony's business discussion. It waits for the Project Architect to convert resolved human authority into a valid same-task envelope.

This creates the intended remote workflow:

`Architect asks Rony -> watcher remains resident -> Rony discusses remotely -> Architect resolves authority -> Architect emits same-task envelope -> watcher continues automatically`

No special human command such as “generate the envelope” is supposed to be necessary once the Project Architect's own governance adopts that continuation rule.

## Project Architect / Orchestrator authority boundary

The Project Architect owns:

- project/product architecture;
- business decisions and permission semantics;
- milestone selection;
- independent verification of Executor evidence;
- ACCEPTED/BLOCKED/INCONCLUSIVE/NO_NEW_REPORT classification;
- complete next bounded Executor prompt;
- project documentation synchronization according to that project's governance;
- deciding when human authority is sufficient to continue.

The Orchestrator owns:

- durable workflow state;
- one-at-a-time task numbering and dispatch;
- worktree/session transport mechanics;
- exactly-once result delivery/reconciliation;
- observing a valid Architect envelope;
- resident waiting while human authority is unresolved;
- discussion pause transport controls;
- browser/session rollover maintenance.

The Orchestrator must not infer business authority from discussion prose, independently accept project milestones, broaden permissions, invent roadmap decisions, or replace Architect judgment.

## Exactly-once result transport

Executor result delivery has durable task and payload identity.

Future marked payloads use durable SHA-256 delivery proof. Confirmation/reconciliation may use the known delivery marker and bounded browser evidence. A send that may have occurred is never blindly repeated.

Rules:

- confirmed delivery is never resent;
- ambiguous attempted delivery reconciles read-only first;
- inability to prove delivery is not proof that delivery did not occur;
- a completed project task is never rerun because result transport failed;
- stale composer cleanup is allowed only when the Orchestrator can prove the text is its own exact known payload;
- unrelated human-authored composer text is never cleared.

Completed-confirmed workflow recovery is fact-based. Historical failure labels must not outweigh stronger durable evidence that the current task is completed, the result exists, delivery is confirmed, no newer prompt is staged and no Executor is active.

## Recovery rules

- `EXECUTOR_RUNNING` + live owned child -> observe only.
- `EXECUTOR_RUNNING` + dead child + usable result -> `RESULT_READY`.
- `EXECUTOR_RUNNING` + dead child + no usable result -> fail closed / `HUMAN_REQUIRED`.
- `RESULT_READY` restart -> resume or reconcile result delivery; never rerun Executor.
- `ARCHITECT_RUNNING` restart after confirmed delivery -> observe Architect; never resend confirmed result.
- completed + confirmed + no active Executor + no newer staged work -> recover by facts, not stale historical reason labels.
- `NEXT_PROMPT_READY` restart -> production dispatcher launches exactly one Executor unless discussion pause is active.
- deliberate Architect `HUMAN_REQUIRED` restart -> reattach to the existing Architect conversation and remain resident.
- discussion prose while deliberate HUMAN_REQUIRED -> update baseline, no format recovery, no task creation.
- valid same-task EXECUTE envelope while deliberate HUMAN_REQUIRED -> ordinary parser/staging path -> one next task.
- no blind post-launch retry.
- restart never clears an active discussion pause silently.

A stored PID by itself is not proof of a live Executor. Liveness must be checked and process ownership must not be inferred merely because Windows currently has the same numeric PID.

## Human discussion pause

Discussion pause is separate from `HUMAN_REQUIRED`.

Local Windows controls:

- `F9` = pause for Architect discussion
- `F10` = resume Orchestrator

Remote exact USER commands in the configured Architect conversation:

- `ORCH:PAUSE`
- `ORCH:RESUME`

The commands are exact-match only. Fuzzy wording and assistant-authored text have no command authority.

Pause semantics:

- never kill an already-running Executor;
- allow the running Executor to finish;
- hold `RESULT_READY` locally instead of delivering it;
- hold `NEXT_PROMPT_READY` instead of launching it;
- suppress new IDLE bootstrap/contact;
- preserve underlying HUMAN_REQUIRED truth;
- resume only clears the pause overlay; it does not synthesize project authority.

A deliberate `ARCHITECT_DECISION_HUMAN_REQUIRED` wait does not require Rony to issue `ORCH:PAUSE`; resident human-decision waiting is a normal workflow boundary of its own.

## Documentation closure governance

The envelope's `documentation=` disposition is durable workflow authority.

`NOT_REQUIRED`

- no documentation closure gate is opened.

`REQUIRED`

- valid only when the accepted Architect decision carries one complete bounded documentation-closure action;
- ordinary project advancement is blocked until closure.

`COMPLETE`

- required project documentation has already been synchronized and verified;
- clears the pending gate and permits the envelope's normal EXECUTE/HUMAN_REQUIRED/STOP semantics.

The Project Architect decides what project documentation is authoritative. The Orchestrator is project-generic and must not hardcode project document names.

## Persistent Executor model

AFFOTECH logical Codex session currently configured:

`019f842e-98bc-7672-a619-51441d91be00`

Normal execution uses a persistent logical Codex session and a short-lived OS child per bounded task via `codex exec resume`.

Rules:

- one writer per project/task;
- one visible child at a time;
- Orchestrator-owned isolated task worktree;
- no AFFOTECH mutation in the Orchestrator base checkout;
- no Executor wall-clock timeout;
- child completion/result evidence determines task completion;
- transport retry never reruns completed project work.

## Architect browser authority

The Orchestrator controls the dedicated ChatGPT Architect browser through Playwright.

It does not use RAW-CDP as the ChatGPT mutation/control path.

RAW-CDP remains a separate AFFOTECH application validation mechanism and must not be confused with Project Architect browser control.

Browser object ownership includes thread ownership. A Playwright Sync bridge must be created, used and closed on its owning thread.

## Architect rollover

Architect rollover is session maintenance only. It is not workflow authority.

Memory threshold remains 1 GiB (`1073741824` bytes). Crossing the threshold records rollover due; it must not preempt result delivery, Architect decision processing, a recoverable human boundary, or other workflow authority.

A qualified rollover may:

1. request a complete handover from the old Project Architect;
2. open a fresh authenticated Architect conversation;
3. submit that handover;
4. verify the new conversation identity;
5. persist the new canonical Architect conversation ID;
6. close the old page;
7. resume the same durable workflow state.

Rollover must never:

- launch project work;
- regenerate an Executor result;
- renumber completed tasks;
- resend confirmed work;
- invent human/business authority;
- turn a maintenance failure into a different workflow decision.

If rollover maintenance fails, the intended invariant is: leave rollover due for later and preserve the workflow state.

### Known rollover qualification gap

The current deferred-rollover service is not yet end-to-end production-qualified after the recent repairs. Source-flow inspection shows the main production path waits inside Executor observation while the child is alive, while deferred rollover service is scheduled after that observation returns. Therefore the intended live-Executor maintenance boundary may remain unreachable in normal execution and `rolloverDue` may simply remain pending until a later safe opportunity.

This limitation must not stop authorized AFFOTECH work. Do not redesign rollover inside unrelated product milestones. Rollover remains maintenance and must never halt the business workflow merely because maintenance is due.

## Durable runtime logging

Canonical local log:

`.agent-work/orchestrator/logs/orchestrator.log`

Important event families include workflow state changes, Executor launch/completion, result delivery/reconciliation, Architect attach/generation/decision, pause control, documentation closure, rollover, and HUMAN_REQUIRED boundaries.

Resident human-decision handling additionally uses events such as:

- `ARCHITECT_HUMAN_WAIT_ATTACH_SUCCESS`
- `ARCHITECT_HUMAN_WAIT_ATTACH_FAILED`
- `ARCHITECT_HUMAN_WAIT_READ_FAILED`
- `ARCHITECT_HUMAN_DECISION_RESPONSE_INVALID`

Logs must prefer hashes/identifiers over prompt/report bodies and must not contain credentials, cookies, private customer payloads or other secrets.

## Accepted repair chain

Do not re-audit these accepted milestones absent direct regression evidence:

- `3e6efeb8b886ec377110c1c5d1740070483df4f9` — canonical state-machine consolidation.
- `b5ed8dcde5de92c5b7fe8df08b84243dc3a6dd98` — durable runtime logging.
- `06a743f986d462f5f6de246dac9292bd47800f0b` — IDLE continuation gate.
- `cdd49f2a9f79a3ff62d5a5cc5945583fcafa7c0b` — ambiguous Architect delivery reconciliation.
- `944597152bdb2872ca7941ac674ca745973f69d` — HUMAN_REQUIRED reason hygiene.
- `e5b47ffc7892a208679dd1a9c54983e19e5982d8` — canonical documentation envelope closure.
- `78ba0a5d8f56bae2dece6ba1a07fa70594cc21de` — durable F9/F10 human discussion pause.
- `9e56d5655a1e40050269245c1b35996f67ddd709` — remote discussion control.
- `1ec880e7d2bede1d56078cab433f3d2793ec008b` — Playwright thread-affinity repair.
- `54a82e81be99eb1a85831cf27ccf37a634082191` — Architect bridge startup/composer hardening.
- `216a57d0635bfa0e6342f276ab880b250afe4003` — fail closed on invalid intended Architect envelope.
- `fce038e29698f18e13c7f9624bf6a80337b2cdc6` — exact result-delivery proof and false-reconciliation recovery foundation.
- `87a214fe98a190827dd06f0456e0d34a687410d5` — same-task false-reconciliation correction.
- `9bc1d9cc030d0e0279bc06b953f354c01533df30` — durable SHA-256 result-delivery receipt.
- `ad29cca8b4eac1bf039c3e3b5a017a358fd57528` — rollover made non-preemptive maintenance.
- `67d1a8d911f5bc839441eadeb69e2f20c16c4651` — confirmed-result recovery after rollover failure.
- `26f6644af1703abd2068198708ae7dbe54544f6a` — fact-based completed-confirmed workflow recovery.
- `57a3a914b35ed0c715aaaf0267ed0bd36a39cc78` — resident human-decision continuation.

Earlier accepted worktree/session/retry/locking foundations remain authoritative dependencies unless direct regression evidence proves otherwise.

## Current qualification status

Already demonstrated in real production across the repair chain:

- one Executor at a time;
- result capture and exactly-once delivery/reconciliation;
- Architect review and next-task staging;
- documentation closure;
- discussion pause/remote control foundations;
- completed-result recovery through multiple transport/rollover incidents;
- successful execution through task `000041` and later quotation milestones through `000043`.

Deterministically regression-qualified at `57a3a914...`, but still requiring a fresh real production proof:

- watcher remains resident during a genuine `ARCHITECT_DECISION_HUMAN_REQUIRED` boundary;
- ordinary remote human/Architect discussion does not trigger format recovery;
- after Rony resolves the decision, the Project Architect emits a valid same-task envelope and the resident watcher consumes it without manual PC restart;
- exactly one next sequential Executor task launches.

Separately still not qualified end-to-end:

- smooth deferred Architect rollover after the latest non-preemptive maintenance repairs.

## Cross-project integration contract

A Project Architect integrating with this Orchestrator should read this document as the current Orchestrator runtime contract.

The Project Architect must preserve its own project governance separately. In particular, the Orchestrator cannot decide when a human business question is resolved. The Project Architect must define its own standing rule for HUMAN_REQUIRED continuation:

- remain responsible for the same completed task;
- allow natural discussion with the final human authority;
- do not invent missing authority;
- once authority is sufficient, emit a valid same-task ORCHESTRATOR_RESULT automatically;
- let the Orchestrator allocate the next sequential task.

That project-side rule belongs in the project's own canonical Architect governance/handover documentation, not in Orchestrator business logic.

## Permanent design principles

1. Human authority remains final.
2. The Project Architect decides project meaning; the Orchestrator transports and supervises.
3. Completed work is preserved before transport/browser maintenance.
4. Ambiguous external actions reconcile read-only before retry.
5. Machine authority is explicit and literal; ordinary discussion is not executable authority.
6. `HUMAN_REQUIRED` pauses project execution; for deliberate Architect decisions it must not unnecessarily kill supervision.
7. Recovery should use current durable facts rather than stale historical error labels.
8. Rollover is maintenance only and must never own workflow authority.
9. One task, one Executor, one result, one Architect decision, one next action.
10. Keep the Orchestrator boring.

Canonical loop:

`recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action`
