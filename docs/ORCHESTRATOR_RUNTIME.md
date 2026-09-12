# AFFOTECH Local Orchestrator Runtime

## Role model

For this repository, the Architect also performs Documentation Curator duties. Runtime/source/test mutations remain Maintainer/Executor work. The Architect may verify implementation evidence and maintain governance/history documentation directly.

## Purpose

The Local Orchestrator automates the relay:

Project Architect -> Orchestrator -> Executor/Curator -> Orchestrator -> Project Architect

GitHub is source/history/release authority. GitHub is not runtime message transport.

The Orchestrator must remain a simple durable relay rather than becoming a second project-management system.

## Canonical production state machine

Normal states:

- IDLE
- NEXT_PROMPT_READY
- EXECUTOR_RUNNING
- RESULT_READY
- ARCHITECT_RUNNING
- HUMAN_REQUIRED

Canonical sequence:

WATCHER START -> recover durable state -> determine exactly where execution stopped -> NEXT_PROMPT_READY -> launch exactly one Executor -> EXECUTOR_RUNNING -> wait for child completion -> capture exactly one result -> RESULT_READY -> deliver result to Architect exactly once -> ARCHITECT_RUNNING -> wait for completed Architect decision -> stage next action -> NEXT_PROMPT_READY / HUMAN_REQUIRED / IDLE -> repeat.

Only the production main state dispatcher may perform NEXT_PROMPT_READY -> EXECUTOR_RUNNING. Parsing, recovery, transport, and rollover helpers may inspect, validate, and restore state, but must not directly launch project work.

## Architect decision rule

A completed task N reviewed by Architect with action=EXECUTE always creates a new sequential task. Example: reviewed task 000021 -> next task 000022.

The reviewed/completed task ID must never be reused for the next milestone. Architect decision interpretation must be identical whether observed during normal operation, IDLE recovery, or watcher restart.

## Recovery rules

- EXECUTOR_RUNNING + PID alive -> observe existing Executor only.
- EXECUTOR_RUNNING + PID dead + usable result -> RESULT_READY.
- EXECUTOR_RUNNING + PID dead + no result -> HUMAN_REQUIRED.
- RESULT_READY restart -> resume result delivery; never rerun Executor.
- ARCHITECT_RUNNING restart with confirmed delivery -> wait for Architect; never resend confirmed report.
- NEXT_PROMPT_READY restart -> main launches exactly one Executor.
- No blind post-launch retry.
- Explicit human-authorized retry may restore NEXT_PROMPT_READY but must not launch from the recovery helper.

## Exactly-once result delivery

Executor result delivery uses durable payload identity.

Once Architect delivery is CONFIRMED, the same result must never be resent.

Ambiguous send -> reconcile against the visible Architect user message.

Transport failure -> bounded transport recovery only.

Transport exhaustion -> HUMAN_REQUIRED.

Transport recovery must never rerun completed project work.

## Architect session rollover

Architect browser memory threshold: 1073741824 bytes (1 GiB).

When threshold is reached during Executor activity, mark rollover pending and do not interrupt the Executor.

Before the next pending Executor result is delivered:

1. Request handover from the old Architect.
2. Wait for HANDOVER_READY.
3. Open a fresh authenticated Architect conversation.
4. Send the handover.
5. Capture and persist the new architectConversationId.
6. Verify future attachment resolves the new conversation.
7. Close the old page.
8. Resume the same durable orchestration state.
9. Deliver the pending result exactly once to the new Architect.

Rollover must never launch Executor work, regenerate a result, renumber completed tasks, resend completed work, or require a manual NEW_ARCHITECT_ID update after successful rollover.

## Executor visibility

Production Executor execution must be visibly identifiable. The watcher must expose task identity, Executor running state, live Codex activity, completion, and exit code.

A blank node.exe console is not accepted as visible execution.

Persistent logical Codex session:

019f842e-98bc-7672-a619-51441d91be00

One writer only. Task-owned isolated worktree required. No Executor wall-clock timeout.

## Git source synchronization rule

A successful Orchestrator source-maintenance milestone is not complete until:

1. git fetch origin
2. validate working/source state
3. commit bounded changes
4. git push origin main
5. git fetch origin
6. git rev-parse HEAD
7. git rev-parse origin/main
8. require HEAD == origin/main

Never force-push. Never claim PASS when a required push failed.

Required reporting should include sourceBase, implementationCommit, localHeadAfter, remoteMainAfter, and localRemoteSynchronized.

## Consolidation history

### ORCH.SINGLE.STATE.MACHINE.CONSOLIDATION.1A

Commit: 7c7a75c37b991d38ddb9e34b8fc61cfcb6012c3a

Status: BLOCKED / NOT ACCEPTED.

Verified improvements:

- canonical Architect decision staging;
- sequential next-task allocation;
- helper direct launches removed in major recovery paths;
- startup recovery diagnostics;
- null-safe main bridge cleanup.

Verified remaining defects after 1A:

- production visible Executor path still blank;
- rollover did not yet block pending result delivery until fresh Architect was established;
- old tests still encoded superseded helper-direct-launch behavior.

### ORCH.SINGLE.STATE.MACHINE.CONSOLIDATION.1B.CLOSURE

Commit: 3e6efeb8b886ec377110c1c5d1740070483df4f9

Status: ACCEPTED by Architect after independent GitHub verification.

Verified:

- main-owned Executor launch sequence preserved;
- helper/recovery direct launch removed from the canonical path;
- sequential next-task staging;
- rollover blocks pending RESULT_READY delivery to the old Architect;
- after successful rollover the pending result resumes against the new Architect;
- null-safe transport exhaustion handling;
- visible Executor stderr tee added;
- focused tests 207 passed / 0 failed;
- full tests 213 passed / 0 failed;
- Python compile PASS;
- git diff --check PASS;
- no live Architect contact, no live Codex launch, no AFFOTECH source mutation during maintenance.

## Runtime logging requirement

Durable watcher logging is REQUIRED but is not yet implemented as of commit 3e6efeb8b886ec377110c1c5d1740070483df4f9.

Target canonical log path:

.agent-work/orchestrator/logs/orchestrator.log

Required design:

- Python standard logging;
- rotating file, recommended 5 MiB x 5 backups;
- unique runId per watcher invocation;
- local timestamp with seconds;
- lifecycle/state-transition logging;
- Executor task/PID/start/finish/result evidence;
- Architect attach/delivery/generation/decision evidence;
- rollover old/new conversation evidence;
- HUMAN_REQUIRED reason;
- complete traceback for unhandled watcher exceptions;
- no complete prompts, reports, handover bodies, credentials, tokens, cookies, or private customer data.

Important events should include WATCHER_STARTED, STATE_RECOVERED, STATE_TRANSITION, NEXT_PROMPT_READY, CODEX_STARTING, CODEX_STARTED, CODEX_FINISHED, EXECUTOR_RESULT_FOUND, EXECUTOR_RESULT_MISSING, RESULT_READY, ARCHITECT_ATTACH_START, ARCHITECT_ATTACH_SUCCESS, RESULT_DELIVERY_ATTEMPT, RESULT_DELIVERY_CONFIRMED, RESULT_DELIVERY_AMBIGUOUS, RESULT_DELIVERY_RECONCILED, RESULT_DELIVERY_EXHAUSTED, ARCHITECT_RESPONSE_ACCEPTED, ARCHITECT_RESPONSE_DUPLICATE, HUMAN_REQUIRED, ROLLOVER_PENDING, ARCHITECT_HANDOVER_REQUESTED, ARCHITECT_HANDOVER_READY, ARCHITECT_CONVERSATION_SWITCHED, ARCHITECT_SESSION_ROLLOVER_COMPLETE, WATCHER_STOPPED, and WATCHER_EXCEPTION.

Do not restart production as fully qualified until the runtime logging milestone is implemented and independently verified.

## Incident lessons - 2026-09-12

Observed production failures included:

1. blank node.exe Executor consoles because meaningful stderr was redirected away from the visible console;
2. Architect result transport exhaustion followed by AttributeError: 'NoneType' object has no attribute 'close';
3. alternate Architect/IDLE paths interpreting and launching next work differently from the normal state-machine path;
4. helper/recovery functions directly launching project work instead of restoring NEXT_PROMPT_READY;
5. rollover integration requesting handover while still allowing pending result delivery before rollover completion;
6. obsolete tests preserving superseded direct-launch behavior.

Primary lesson: recovery mechanisms must protect the canonical state machine and must never become alternate workflow implementations.

## Permanent design principle

Recover where we stopped -> run one task -> capture one result -> give it to Architect once -> wait for Architect -> stage one next task -> repeat.

Additional recovery mechanisms exist only to preserve this loop. They must never replace it.
