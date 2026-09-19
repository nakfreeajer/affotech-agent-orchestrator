# AFFOTECH Local Orchestrator Runtime

## Current authority

Repository: `nakfreeajer/affotech-agent-orchestrator`

Branch authority: remote `main`

Accepted runtime/source/test checkpoint:

`49bf2c72699fdd8e0df2cbb31091b6751ffd6797` — `fix(orchestrator): wait for fresh bootstrap proof`

Direct parent:

`55ab455f6c8799805379481bc6f219f0b7e4aa7b` — `fix(orchestrator): require exact fresh bootstrap proof`

Immediately preceding rollover repairs:

- `710b177ad041ce2b8a7ab070f58e7d4b638e4b5a` — one fresh rollover tab + same-page send reconciliation;
- `9f1026cec05c56e67a164cead1fe61d5c0ba2ff4` — executable semantic-response script repair + error tracing;
- `ee59ce4c20f13e408f2f61af0c26d260440d2fbe` — semantic Architect-response extraction;
- `c7c6bbbc2959d1eb9e8b181802a8e2015df3580e` — opt-in rollover diagnostic trace;
- `1cdd74ecbb0fe286426bf4219feb370fb5adc3e1` — bounded rollover recovery safety cutout.

Accepted deterministic qualification for `49bf2c72...`:

- Python compile: PASS;
- first suite: `281` passing;
- watcher suite: `116` passing;
- Node suite: `158` passing;
- focused fresh-bootstrap/rollover regressions: PASS;
- exact submitted-user-message observation window: 2.0 s total, 0.1 s polling;
- delayed exact-message DOM appearance: PASS;
- composer-empty without exact user-message proof: rejected;
- generation-visible without exact user-message proof: rejected;
- assistant-started without exact user-message proof: rejected;
- same-page unsent fallback: PASS;
- one-fresh-tab invariant: PASS;
- restart with unidentifiable prior fresh candidate: fail closed, no replacement tab.

### Production qualification status — 2026-09-19

The repaired source is **not yet declared end-to-end production-closed**.

A clean production qualification was prepared with:

- task `000053` preserved as completed;
- task `000054` preserved as the staged next task;
- task `000054` prompt SHA-256 unchanged through state recovery;
- no fresh-candidate conversation ID carried forward from the failed prior transaction;
- no `rolloverFreshPageCreated` residue carried forward;
- a valid persisted `pending_handover` ending in `ARCHITECT_HANDOVER_READY`;
- `handoverRequested=False`, allowing the current code to reconstruct preserved handover authority rather than tripping the duplicate-response guard.

Before the clean run, Rony manually closed the two stale fresh tabs created by the older broken rollover build.

The current live qualification has progressed further than the earlier failures: Rony reported that the watcher remained running and a Codex child for the staged next task was launched.

That proves the transaction progressed beyond the earlier fresh-bootstrap/HUMAN_REQUIRED failure boundary, but final rollover acceptance still requires authoritative confirmation of the whole transaction:

1. exactly one fresh Architect tab created for the current rollover;
2. exact fresh bootstrap submitted exactly once;
3. `ARCHITECT_SESSION_READY` observed;
4. new Architect conversation identity durably committed;
5. old Architect closed only after authority commit;
6. task `000053` not rerun;
7. task `000054` launched exactly once;
8. no technical rollover safety cutout in the successful transaction.

Runtime/source/test authority is GitHub. Local `.agent-work/orchestrator/` state is durable workflow state and is not source/history authority.

For this repository, the Architect performs documentation-governance synchronization. Runtime/source/test mutation remains bounded Maintainer/Executor work.

## Purpose

The Local Orchestrator automates the durable loop:

`Project Architect -> Orchestrator -> one visible Executor task -> Orchestrator -> Project Architect`

The Orchestrator is deliberately project-generic in responsibility even though the current source still contains AFFOTECH-specific configuration.

It transports and supervises authority; it does not become the product Architect.

Project architecture, roadmap, business policy, permission decisions, document ownership and milestone meaning remain with the configured Project Architect and final human authority.

Permanent workflow invariant:

`recover where stopped -> run one task -> capture one result -> deliver once -> wait -> stage one next action -> repeat`

Rollover, browser recovery, discussion pause and HUMAN_REQUIRED handling exist only to preserve this loop. They must never become alternate workflow authority.

## Maturity status

Core workflow classification:

`PRODUCTION_MATURE_REFERENCE_IMPLEMENTATION`

Current rollover maintenance status:

`PRODUCTION_QUALIFICATION_IN_PROGRESS`

Universalization readiness:

`PAUSED_PENDING_ROLLOVER_PRODUCTION_QUALIFICATION`

Drop-in universal-template status:

`NOT_YET_EXTRACTED`

The core workflow remains the reference implementation. The September 19 incident reopened only the browser-mediated rollover maintenance boundary; it does not invalidate unrelated accepted task/result/human-authority foundations.

Universal extraction must wait until the current rollover production qualification is closed.

The canonical universalization assessment is:

`docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md`

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

Only the production main dispatcher may perform `NEXT_PROMPT_READY -> EXECUTOR_RUNNING`. Recovery, transport, parsing, documentation, rollover and pause helpers may restore or inspect state but must not create an alternate launch path.

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
   - `HUMAN_REQUIRED` -> preserve the completed task and enter resident human-decision wait when the reason is a deliberate Architect decision;
   - `STOP` -> no currently authorized work; return to resident no-work/IDLE semantics.
10. Repeat without reusing a reviewed task ID.

The Project Architect uses the completed task ID in its envelope. The Orchestrator allocates the next sequential task ID.

## Canonical Architect envelope

Production machine authority is:

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

The field order above is parser-significant in the current protocol.

Ordinary discussion is not machine authority.

Presence of the literal opening marker `<ORCHESTRATOR_RESULT>` means the response is attempting machine authority. An attempted but invalid envelope fails closed and must never be silently downgraded into ordinary prose.

## Resident HUMAN_REQUIRED contract

`HUMAN_REQUIRED` has more than one cause. Technical failure states remain fail-closed according to their reason.

The deliberate Project Architect decision boundary is:

`humanRequiredReason=ARCHITECT_DECISION_HUMAN_REQUIRED`

For that reason:

- project execution pauses;
- the watcher process remains resident;
- no Executor launches while authority is missing;
- the completed task is preserved;
- the completed result is not resent;
- the watcher stays attached to the current authoritative Architect conversation;
- ordinary Architect discussion prose is expected and does not trigger format recovery;
- the observation baseline advances after ordinary discussion;
- a later valid envelope for the same completed task is consumed through the normal parser/staging path;
- a later `EXECUTE` stages exactly one next task;
- a later `HUMAN_REQUIRED` remains resident;
- a later `STOP` means no currently authorized work rather than terminating supervision;
- temporary Architect attach/read failure preserves HUMAN_REQUIRED, disconnects safely, waits, and retries.

The watcher does not interpret Rony's business discussion. The Project Architect decides when human authority is sufficient and then emits the same-task envelope.

Intended remote workflow:

`Architect asks Rony -> watcher remains resident -> Rony discusses remotely -> Architect resolves authority -> Architect emits same-task envelope -> watcher continues automatically`

This behavior remains part of the production-mature reference model. Future projects must adopt the matching project-side HUMAN AUTHORITY CONTINUATION governance rather than teaching the Orchestrator project-specific business rules.

## Project Architect / Orchestrator authority boundary

The Project Architect owns:

- project/product architecture;
- business decisions and permission semantics;
- milestone selection;
- independent verification of Executor evidence;
- exact acceptance classification;
- complete next bounded Executor prompt;
- project documentation synchronization according to project governance;
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

Marked payloads use durable SHA-256 delivery proof. A send that may have occurred is never blindly repeated.

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
- `NEXT_PROMPT_READY` restart -> production dispatcher launches exactly one Executor unless discussion pause is active or rollover maintenance is being serviced at a safe boundary.
- deliberate Architect HUMAN_REQUIRED restart -> reattach to the existing Architect conversation and remain resident.
- discussion prose while deliberate HUMAN_REQUIRED -> update baseline, no format recovery, no task creation.
- valid same-task EXECUTE envelope while deliberate HUMAN_REQUIRED -> ordinary parser/staging path -> one next task.
- no blind post-launch retry.
- restart never clears an active discussion pause silently.
- a persisted valid handover may be reconstructed when `handoverRequested` is false; a persisted response identity must not be used to manufacture duplicate rollover work.

A stored PID by itself is not proof of a live Executor. Liveness must be checked.

## Human discussion pause

Discussion pause is separate from HUMAN_REQUIRED.

Local Windows controls:

- `F9` = pause for Architect discussion
- `F10` = resume Orchestrator

Remote exact USER commands in the configured Architect conversation:

- `ORCH:PAUSE`
- `ORCH:RESUME`

Pause semantics:

- never kill an already-running Executor;
- allow the running Executor to finish;
- hold `RESULT_READY` locally instead of delivering it;
- hold `NEXT_PROMPT_READY` instead of launching it;
- suppress new IDLE bootstrap/contact;
- preserve underlying HUMAN_REQUIRED truth;
- resume only clears the pause overlay; it does not synthesize project authority.

A deliberate `ARCHITECT_DECISION_HUMAN_REQUIRED` wait does not require a pause command.

## Documentation closure governance

The envelope's `documentation=` disposition is durable workflow authority.

`NOT_REQUIRED`

- no documentation closure gate is opened.

`REQUIRED`

- accepted work requires one bounded documentation closure before ordinary project advancement.

`COMPLETE`

- required project documentation has already been synchronized and verified;
- the pending gate is cleared and normal action semantics apply.

The Project Architect decides which project documents are authoritative. The Orchestrator must not hardcode project document names into generic workflow semantics.

## Persistent Executor model

AFFOTECH logical Codex session currently configured:

`019f842e-98bc-7672-a619-51441d91be00`

Normal execution uses a persistent logical Codex session and a short-lived OS child per bounded task via `codex exec resume`.

Rules:

- one writer per project/task;
- one visible child at a time;
- Orchestrator-owned isolated task worktree;
- no project mutation in the Orchestrator base checkout;
- no Executor wall-clock timeout;
- child completion/result evidence determines task completion;
- transport retry never reruns completed project work.

The session identity above is AFFOTECH configuration and must become project-profile data during universal extraction.

## Architect browser authority

The Orchestrator controls the dedicated ChatGPT Architect browser through Playwright.

It does not use RAW-CDP as the ChatGPT mutation/control path.

Project application/browser validation is separate and remains governed by that project's own rules.

Browser object ownership includes thread ownership. A Playwright Sync bridge must be created, used and closed on its owning thread.

## Architect rollover

Architect rollover is session maintenance only. It is not workflow authority.

Memory threshold remains 1 GiB (`1073741824` bytes).

The current intended rollover transaction is:

`old Architect -> authoritative handover -> one fresh tab -> exact bootstrap submission -> ARCHITECT_SESSION_READY -> authority commit -> close old Architect -> resume staged project work`

### Protected rollover invariants

A rollover must never:

- launch or rerun project work merely because rollover maintenance failed;
- regenerate an Executor result;
- renumber completed tasks;
- resend confirmed work;
- invent human/business authority;
- create more than one fresh Architect tab for one rollover transaction;
- create a replacement fresh tab after ambiguous submission;
- accept composer-empty, generation-visible or assistant-started as final bootstrap delivery proof;
- commit fresh Architect authority before exact `ARCHITECT_SESSION_READY`;
- close the old Architect before durable authority commit.

Fresh bootstrap delivery authority requires the exact deterministic bootstrap to be observed as a submitted user message on the same fresh page.

Supporting signals such as composer-empty, generation-visible or assistant-started may help classify UI progress, but they do not substitute for exact user-message proof.

After the initial send action, the runtime gives ChatGPT a bounded 2.0-second observation window, polling every 0.1 seconds, for the exact user message to mount. If exact proof still does not appear, only the same page may be reconciled. If the exact payload is still in that page's composer, one same-page Enter fallback is allowed; if delivery still cannot be proven, the transaction fails closed.

Restart recovery may reuse a durably identified/proven candidate. If a previous ambiguous fresh page cannot be safely identified after restart, the Orchestrator must fail closed rather than create another fresh page automatically.

### Current rollover qualification

Source/test checkpoint `49bf2c72...` is accepted.

Production status remains:

`PRODUCTION_QUALIFICATION_IN_PROGRESS`

The current real run has progressed to launching the staged Codex child after Rony manually removed two stale tabs created by the prior broken implementation. Final production acceptance awaits verification of the complete one-tab/bootstrap/ready/authority-switch/old-tab-close/exactly-once-task-launch transaction.

## Durable runtime logging

Canonical local log:

`.agent-work/orchestrator/logs/orchestrator.log`

Opt-in deep diagnostic trace:

`ORCHESTRATOR_DIAGNOSTIC_TRACE=1`

Diagnostic root:

`.agent-work/orchestrator/logs/diagnostic/<runId>/`

Logs cover workflow state, Executor lifecycle, result delivery/reconciliation, Architect attach/generation/decision, pause control, documentation closure, rollover and HUMAN_REQUIRED boundaries.

Rollover diagnostic tracing includes Playwright attach/close, assistant semantic extraction, raw/sanitized response snapshots, remote-control polling, fresh-session creation/submission, fresh-session ready wait, authority commit and error stacks at the traced boundary.

Logs must prefer hashes/identifiers over prompt/report bodies and must not contain credentials, cookies, private customer payloads or other secrets.

## Accepted repair chain

Do not re-audit these milestones absent direct regression evidence. Direct production regression did reopen several rollover boundaries during September 18–19; their superseding repairs are listed below.

Foundational accepted chain:

- `3e6efeb8b886ec377110c1c5d1740070483df4f9` — canonical state-machine consolidation.
- `b5ed8dcde5de92c5f6de246dac9292bd47800f0b` — durable runtime logging.
- `06a743f986d462f5f6de246dac9292bd47800f0b` — historical IDLE-continuation lineage reference as previously documented.
- `cdd49f2a9f79a3ff62d5a5cc5945583fcafa7c0b` — ambiguous Architect delivery reconciliation.
- `944597152bdbb2872ca7941ac674ca745973f69d` — HUMAN_REQUIRED reason hygiene.
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

September 18–19 rollover recovery chain:

- `528c0b93ee013640c765fa0f4e81c5dc499b5a50` — preserve confirmed delivery after cleanup.
- `6896d732f4d2f1f450867a886d4efbb613c927c6` — reacquire proven fresh Architect candidate.
- `0bd18dad0b3f2766aedc1609f01b72ae1f44d3d7` — recover lost rollover handover authority.
- `5eeeae0401eb595389c8839822f93d70c8395219` — retire stale completed Executor ownership.
- `2cebe036eb89535ddd90cd52a679b6451ff112d6` — recover stale fresh candidate identity.
- `3da7f7a5cd9d9073264134e2f749df7b32c40196` — initial Architect assistant extraction sanitizer.
- `1cdd74ecbb0fe286426bf4219feb370fb5adc3e1` — bound rollover recovery failure and safety cutout.
- `c7c6bbbc2959d1eb9e8b181802a8e2015df3580e` — opt-in rollover diagnostic trace.
- `ee59ce4c20f13e408f2f61af0c26d260440d2fbe` — semantic Architect response extraction and remote-control poll trace.
- `9f1026cec05c56e67a164cead1fe61d5c0ba2ff4` — repair executable semantic extraction script and diagnostic error trace.
- `710b177ad041ce2b8a7ab070f58e7d4b638e4b5a` — enforce single fresh rollover tab and same-page ambiguous-send reconciliation.
- `55ab455f6c8799805379481bc6f219f0b7e4aa7b` — require exact fresh bootstrap submitted-user-message proof.
- `49bf2c72699fdd8e0df2cbb31091b6751ffd6797` — bounded wait for exact fresh bootstrap proof.

Earlier accepted worktree/session/retry/locking foundations remain authoritative dependencies unless direct regression evidence proves otherwise.

## Current qualification status

Production-mature core behavior includes:

- one Executor at a time;
- visible bounded Executor execution;
- isolated task worktrees;
- result capture and exactly-once delivery/reconciliation;
- Architect review and next-task staging;
- documentation closure;
- local/remote discussion pause foundations;
- completed-result recovery through transport incidents;
- resident HUMAN_REQUIRED supervision and natural human/Architect discussion handling;
- continuation without requiring the final human to restart the watcher manually during ordinary human-authority waits.

Current maintenance qualification boundary:

- Architect rollover source/test repair is accepted at `49bf2c72...`;
- real production rollover qualification is in progress;
- the current run has reached staged Codex-child launch;
- final end-to-end rollover acceptance remains pending exact transaction evidence.

Universal extraction remains paused until that boundary closes.

## Cross-project integration contract

A Project Architect integrating with this Orchestrator should read this document as the current runtime contract.

The Project Architect must preserve its own project governance separately. In particular, the Orchestrator cannot decide when a human business question is resolved.

Each project must define a standing HUMAN AUTHORITY CONTINUATION rule:

- remain responsible for the same completed task;
- allow natural discussion with the final human authority;
- do not invent missing authority;
- once authority is sufficient, emit a valid same-task ORCHESTRATOR_RESULT automatically;
- let the Orchestrator allocate the next sequential task.

That rule belongs in the project's own canonical Architect governance/handover documentation, not in Orchestrator business logic.

## Universal template readiness

The workflow model remains the reference design for future projects, but universal extraction is currently paused.

The generic runtime must preserve the current state machine, envelope, exactly-once transport, resident HUMAN_REQUIRED behavior, discussion pause, recovery, human-control boundaries and the one-fresh-tab/exact-bootstrap rollover contract.

Project-specific values that must become configuration include at least:

- project repository and local path;
- authoritative branch;
- Executor logical session ID;
- Architect conversation identity/browser endpoint;
- state/worktree roots;
- Executor bootstrap path;
- project-specific validation/browser hooks.

Current source still contains AFFOTECH-specific constants such as `AFFOTECH_EXECUTOR_SESSION_ID`, `AFFOTECH_CHILD_PROJECT_DIR`, `AFFOTECH_CHILD_REMOTE`, `VERIFIED_ARCHITECT_CONVERSATION_ID`, retained relay identity and AFFOTECH-specific bootstrap naming. Therefore another project must not simply copy the current source and edit constants ad hoc.

See `docs/ORCHESTRATOR_UNIVERSAL_TEMPLATE_READINESS.md` for extraction scope and qualification gates.

## Permanent design principles

1. Human authority remains final.
2. The Project Architect decides project meaning; the Orchestrator transports and supervises.
3. Completed work is preserved before transport/browser maintenance.
4. Ambiguous external actions reconcile read-only before retry.
5. Machine authority is explicit and literal; ordinary discussion is not executable authority.
6. HUMAN_REQUIRED pauses project execution; deliberate Architect decisions must not unnecessarily kill supervision.
7. Recovery uses current durable facts rather than stale historical error labels.
8. Rollover is maintenance only and must never own workflow authority.
9. One task, one Executor, one result, one Architect decision, one next action.
10. Universalization parameterizes project identity; it does not redesign proven workflow semantics.
11. One rollover transaction creates at most one fresh Architect tab.
12. Exact fresh-bootstrap submitted-user-message proof outranks weaker UI transition signals.
13. Browser/DOM timing must use bounded observation rather than immediate false-negative classification.
14. Ambiguous fresh-session recovery reuses the same page or fails closed; it never allocates a replacement page automatically.
15. Component-level PASS results are not sufficient production proof for browser-mediated orchestration; the complete real transaction remains the acceptance boundary.
16. Keep the Orchestrator boring.

Canonical loop:

`recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action`
