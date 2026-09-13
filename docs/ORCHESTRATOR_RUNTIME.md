# AFFOTECH Local Orchestrator Runtime

## Current authority

Repository: `nakfreeajer/affotech-agent-orchestrator`

Branch authority: remote `main`

Current accepted Orchestrator source checkpoint: `e5b47ffc7892a208679dd1a9c54983e19e5982d8` (`fix(orchestrator): complete documentation envelope template`).

For this repository, the Architect also performs Documentation Curator duties. Runtime/source/test mutations remain bounded Maintainer/Executor work. The Architect independently verifies implementation evidence and may directly maintain Orchestrator governance/history documentation.

## Purpose

The Local Orchestrator automates the durable loop:

`Project Architect -> Orchestrator -> one visible Executor/Documentation task -> Orchestrator -> Project Architect`

GitHub is source/history/release authority. GitHub is not runtime message transport.

The Orchestrator is deliberately project-generic. Project architecture, roadmap choices, document names, milestone meaning, and business policy remain with the configured Project Architect.

The permanent invariant is:

`recover where stopped -> run one task -> capture one result -> give Architect once -> wait -> stage one next action -> repeat`

## Production status

The core local Orchestrator is production-qualified for the behavior already exercised in real AFFOTECH work:

- resident watcher startup/recovery;
- one visible Codex child at a time;
- persistent logical Codex session;
- Orchestrator-owned isolated task worktrees;
- sequential task allocation;
- exactly-once result delivery with ambiguous-send reconciliation;
- Architect response observation and next-task staging;
- durable runtime logging;
- IDLE continuation;
- Architect browser rollover foundation;
- deliberate `HUMAN_REQUIRED` stopping;
- documentation-closure governance protocol.

Latest full regression count after documentation-protocol closure: `247 passed / 0 failed`; Python compile PASS; `git diff --check` PASS.

The live AFFOTECH orchestration boundary after Receipt OCR 5G is intentional:

- `state=HUMAN_REQUIRED`
- `taskId=000025`
- `lastCompletedTaskId=000025`
- `humanRequiredReason=ARCHITECT_DECISION_HUMAN_REQUIRED`

Receipt OCR 5G is accepted, stable-tagged and documentation-complete. The watcher is not blocked by transport; it is waiting for fresh final-human roadmap authority for the next AFFOTECH direction.

## Canonical states

Normal durable states:

- `IDLE`
- `NEXT_PROMPT_READY`
- `EXECUTOR_RUNNING`
- `RESULT_READY`
- `ARCHITECT_RUNNING`
- `HUMAN_REQUIRED`

Exceptional state retained by runtime:

- `EXECUTOR_CRASHED`

Only the production main dispatcher may perform `NEXT_PROMPT_READY -> EXECUTOR_RUNNING`. Recovery, parsing, transport, documentation governance and rollover helpers may inspect/restore state but must not directly launch project work.

## Canonical sequence

1. Start watcher and acquire the single-instance lock.
2. Load durable state/configuration and recover exactly where execution stopped.
3. `NEXT_PROMPT_READY`: validate the owned task worktree and launch exactly one visible Executor child.
4. `EXECUTOR_RUNNING`: observe the existing child; no wall-clock timeout and no duplicate launch.
5. Child completion with a non-empty result -> `RESULT_READY`.
6. Deliver the result to the Project Architect exactly once.
7. Confirm or reconcile Architect delivery -> `ARCHITECT_RUNNING`.
8. Wait for one completed Architect decision.
9. Interpret that decision into exactly one of:
   - next sequential task -> `NEXT_PROMPT_READY`;
   - deliberate human boundary -> `HUMAN_REQUIRED`;
   - no current work -> `IDLE`.
10. Repeat.

A reviewed task ID is never reused. If task `000021` is reviewed and the Architect returns `EXECUTE`, the next task is `000022`.

## Canonical Architect envelope

New production Architect prompts advertise this schema:

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

The envelope must be the final authoritative content of the Architect response.

Legacy envelopes without `documentation=` remain parseable as `NOT_REQUIRED` only for backward compatibility with historical responses. All new production Architect-facing instructions include the documentation field.

## Documentation closure governance

Documentation is now an explicit governed milestone disposition rather than an informal reminder.

`documentation=NOT_REQUIRED`

- the current decision is not a milestone/release documentation closure;
- no documentation gate is opened.

`documentation=REQUIRED`

- valid only with an accepted decision and `action=EXECUTE` carrying one complete bounded documentation-closure prompt;
- the Orchestrator stages that closure through the normal sequential task path;
- durable `documentationClosurePending=true` prevents ordinary project advancement until completion;
- the Orchestrator does not know project-specific document names.

`documentation=COMPLETE`

- required documentation has already been synchronized and verified;
- clears the durable pending gate;
- then ordinary `EXECUTE`, `HUMAN_REQUIRED` or `STOP` semantics apply.

The Project Architect decides whether documentation is needed, which documents are authoritative, and whether the Architect itself performs the documentation mutation or delegates one bounded documentation task.

A pending documentation closure cannot be bypassed by a missing or `NOT_REQUIRED` disposition. Format recovery explicitly preserves the existing documentation obligation.

## Recovery rules

- `EXECUTOR_RUNNING` + live PID -> observe only.
- `EXECUTOR_RUNNING` + dead PID + usable result -> `RESULT_READY`.
- `EXECUTOR_RUNNING` + dead PID + no usable result -> `HUMAN_REQUIRED`.
- `RESULT_READY` restart -> resume/reconcile result transport; never rerun Executor.
- `ARCHITECT_RUNNING` restart after confirmed delivery -> observe Architect; never resend confirmed result.
- `NEXT_PROMPT_READY` restart -> main dispatcher launches exactly one Executor.
- no blind post-launch retry.
- human-authorized recovery may restore `NEXT_PROMPT_READY`, but recovery helpers do not launch directly.
- deliberate Architect `HUMAN_REQUIRED` uses `humanRequiredReason=ARCHITECT_DECISION_HUMAN_REQUIRED` and must not enter transport-exhaustion recovery.

## Exactly-once Architect result transport

Executor-result delivery has durable task and payload identity.

Before send, user/assistant baselines are persisted. Confirmation may be proven by accepted composer transition, user-turn advancement, Architect generation, assistant advancement, or exact/normalized visible user payload evidence.

If a send action was attempted but acknowledgement is uncertain:

- state becomes ambiguous;
- automatic recovery is reconciliation-only;
- the same payload is not automatically repopulated or resent;
- inability to prove delivery is not proof of non-delivery;
- unprovable ambiguity eventually becomes `HUMAN_REQUIRED`.

If delivery is independently proven, any stale composer is cleared only when its content exactly/normalizes to the known payload. Unrelated human-authored text is never cleared.

Transport recovery must never rerun completed project work.

## HUMAN_REQUIRED reason hygiene

`humanRequiredReason` describes only the current authority boundary.

Successful Executor completion, successful transport recovery, Architect `EXECUTE`, and Architect `STOP` clear stale failure reasons.

Architect `HUMAN_REQUIRED` replaces any previous reason with `ARCHITECT_DECISION_HUMAN_REQUIRED`.

Historical transport reason `ARCHITECT_RESULT_TRANSPORT_EXHAUSTED` must never survive into later healthy task states.

## Persistent Executor model

AFFOTECH logical Codex session:

`019f842e-98bc-7672-a619-51441d91be00`

Normal operation uses a persistent logical Codex session with a short-lived OS child per bounded task via `codex exec resume`.

Rules:

- one writer/project/task;
- one visible child at a time;
- task-owned isolated Git worktree;
- no AFFOTECH mutation in the Orchestrator repository/base checkout;
- no Executor wall-clock timeout;
- child exit/result evidence determines completion;
- manually opened interactive Codex must not hold the same writer authority during automated execution.

## Executor visibility

Production launches print a visible task banner and allow live Codex activity to remain visible. Runtime captures stderr evidence without hiding all meaningful execution from the user.

`CREATE_NEW_CONSOLE` remains part of the Windows launch path, so an auxiliary console may still appear. This is a presentation caveat, not authority for a second Executor.

## Durable runtime logging

Durable logging is implemented and accepted.

Canonical log:

`.agent-work/orchestrator/logs/orchestrator.log`

Current design:

- Python standard logging;
- rotating file, 5 MiB active file with 5 backups;
- unique `runId=YYYYMMDD-HHMMSS-PID` per watcher invocation;
- local timestamp;
- lifecycle/state transition/task/transport/rollover/human-boundary events;
- traceback for unhandled watcher exceptions;
- hashes/identifiers rather than complete prompt/report bodies;
- no credentials, tokens, cookies or private customer payloads.

Important event families include:

- `WATCHER_STARTED`, `STATE_RECOVERED`, `STATE_TRANSITION`, `WATCHER_STOPPED`, `WATCHER_EXCEPTION`;
- `NEXT_PROMPT_READY`, `CODEX_STARTING`, `CODEX_STARTED`, `CODEX_FINISHED`, `EXECUTOR_RESULT_FOUND`, `RESULT_READY`;
- `ARCHITECT_ATTACH_START`, `ARCHITECT_ATTACH_SUCCESS`, `ARCHITECT_GENERATION_STARTED`, `ARCHITECT_GENERATION_FINISHED`, `ARCHITECT_RESPONSE_ACCEPTED`;
- `RESULT_DELIVERY_ATTEMPT`, `RESULT_DELIVERY_CONFIRMED`, `RESULT_DELIVERY_AMBIGUOUS`, `RESULT_DELIVERY_RECONCILED`, `RESULT_DELIVERY_EXHAUSTED`;
- `ARCHITECT_STALE_COMPOSER_CLEARED`;
- `HUMAN_REQUIRED`;
- `DOCUMENTATION_CLOSURE_REQUIRED`, `DOCUMENTATION_CLOSURE_TASK_STAGED`, `DOCUMENTATION_CLOSURE_ACCEPTED`, `DOCUMENTATION_CLOSURE_BYPASS_BLOCKED`;
- rollover lifecycle events.

Accepted logging closure: `b5ed8dcde5de92c5b7fe8df08b84243dc3a6dd98`.

## IDLE continuation

Accepted closure: `06a743f986d462f5f6de246dac9292bd47800f0b`.

Consumed result-review responses may request exactly one continuation bootstrap. A bootstrap-origin STOP remains quietly IDLE and cannot recursively bootstrap itself. Ambiguous legacy consumed responses fail closed unless they satisfy the qualified migration conditions.

## Architect browser rollover

Architect browser memory threshold: `1073741824` bytes (1 GiB).

At a safe point, rollover may:

1. request handover from the old Architect;
2. wait for handover completion;
3. create a fresh authenticated Architect conversation;
4. send the handover;
5. persist the new conversation ID;
6. verify future attachment to the new conversation;
7. close the old page;
8. resume the same durable orchestration state;
9. deliver any pending result exactly once to the new Architect.

Rollover must never launch project work, regenerate results, renumber completed tasks, or resend already-confirmed work.

## Accepted repair chain

Do not re-audit these milestones absent regression evidence:

- `3e6efeb8b886ec377110c1c5d1740070483df4f9` — canonical state-machine consolidation closure.
- `b5ed8dcde5de92c5b7fe8df08b84243dc3a6dd98` — runtime logging lifecycle accepted.
- `06a743f986d462f5f6de246dac9292bd47800f0b` — IDLE continuation gate accepted.
- `cdd49f2a9f79a3ff62d5a5cc5945583fcafa7c0b` — ambiguous Architect delivery reconciliation closed.
- `944597152bdbb2872ca7941ac674ca745973f69d` — stale HUMAN_REQUIRED reason hygiene accepted.
- `cfd673e3f278c4269fe252f40210b935eb3fb622` — milestone documentation governance implemented.
- `38b3aeca711b36f19f1ad612b6ec223eea1691c3` — documentation disposition propagated to production Architect prompts.
- `e5b47ffc7892a208679dd1a9c54983e19e5982d8` — canonical IDLE documentation envelope template completed.

Earlier accepted foundations also include Orchestrator-owned worktrees, persistent session identity, postlaunch retry fencing, human recovery/single-instance locking, post-result worktree inheritance, and cross-task transport/state hygiene.

## Production lessons

The month-long stabilization exposed permanent lessons:

1. recovery must restore canonical state, not implement an alternate workflow;
2. ambiguous post-send transport must reconcile, not blindly resend;
3. state reasons must describe current authority only;
4. browser/composer state is evidence, not a reason to duplicate actions;
5. project documentation closure must be explicit and durable;
6. the generic Orchestrator must not contain project-specific roadmap/document knowledge;
7. production prompts must advertise the same protocol the parser enforces;
8. a completed task/result must never be rerun simply because transport failed;
9. visible execution and durable logs are operational requirements, not optional diagnostics;
10. human authority remains the boundary for new product direction.

## Next qualification gate

Do not manufacture another Orchestrator-only milestone merely to exercise the final protocol.

The next real AFFOTECH roadmap milestone should be used as the production proof of the final documentation-governance protocol. Observe that:

- the final Architect envelope includes `documentation=`;
- any required documentation closure is staged exactly once;
- pending documentation survives restart and cannot be bypassed;
- `COMPLETE` clears the gate;
- no duplicate Executor/result transport occurs;
- deliberate `HUMAN_REQUIRED` remains safe.

## Next project after successful real proof: universal template

After one real post-5G AFFOTECH milestone completes successfully through the final protocol, the next Orchestrator engineering objective is to create a **universal Orchestrator template for new projects**.

Goal: a new project should start from the proven runtime instead of spending weeks rebuilding transport, recovery, state, logging, worktree, rollover and documentation-governance behavior.

The universalization work must extract configuration from project-specific assumptions while preserving this accepted state machine and safety behavior. At minimum, it should parameterize project repository/branch, Architect conversation, Executor session identity, workspace/worktree roots, role/bootstrap context and project-specific validation hooks.

Do not begin universalization before the next real milestone proves the current final protocol in production.

## Git source synchronization rule

A source-maintenance milestone is complete only when the bounded change is committed, pushed, fetched/read back, and local/remote authority is synchronized. Never force-push and never claim PASS when a required push fails.

## Permanent design principle

Keep the Orchestrator boring:

`recover -> run one task -> capture one result -> deliver once -> wait -> stage one next action`

Everything else exists only to preserve that loop.