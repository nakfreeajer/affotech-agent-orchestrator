# AFFOTECH Local Orchestrator — Universal Template Readiness

## Status

The AFFOTECH Local Orchestrator remains the **canonical reference design** for a future universal project Orchestrator template, but implementation extraction is currently **paused pending closure of the September 19 Architect-rollover production qualification**.

It is **not yet a drop-in universal template**.

The distinction is intentional:

- the workflow model, state machine, transport rules, recovery rules, human-control boundaries and production lessons remain the protected reference design;
- the September 19 production incident proved that browser-mediated rollover still required source/test repair beyond the earlier September 18 checkpoint;
- the current source now contains the one-fresh-tab / exact-bootstrap-delivery repair chain, but the real production qualification is still being verified end to end;
- AFFOTECH-specific constants, repository paths, session identity, browser conversation identity and bootstrap assumptions still must be parameterized before another project can adopt the runtime safely.

Universalization must therefore remain an extraction/configuration milestone, not a redesign of the proven workflow, and must not resume until the current rollover production qualification is closed.

## Current source authority

Repository: `nakfreeajer/affotech-agent-orchestrator`

Branch: `main`

Accepted source/test checkpoint:

`49bf2c72699fdd8e0df2cbb31091b6751ffd6797` — `fix(orchestrator): wait for fresh bootstrap proof`

Direct parent:

`55ab455f6c8799805379481bc6f219f0b7e4aa7b` — `fix(orchestrator): require exact fresh bootstrap proof`

Relevant immediately preceding rollover repairs:

- `710b177ad041ce2b8a7ab070f58e7d4b638e4b5a` — enforce one fresh rollover tab and same-page send reconciliation;
- `9f1026cec05c56e67a164cead1fe61d5c0ba2ff4` — repair executable semantic-response extraction JavaScript and diagnostic error tracing;
- `ee59ce4c20f13e408f2f61af0c26d260440d2fbe` — semantic Architect-response extraction and remote-control poll diagnostics;
- `c7c6bbbc2959d1eb9e8b181802a8e2015df3580e` — opt-in rollover diagnostic trace;
- `1cdd74ecbb0fe286426bf4219feb370fb5adc3e1` — bounded rollover recovery safety cutout.

Deterministic qualification reported and independently source-reviewed for `49bf2c72...` includes:

- Python compile PASS;
- first suite `281` passing;
- watcher suite `116` passing;
- Node suite `158` passing;
- focused fresh-bootstrap qualification PASS;
- bounded exact submitted-user-message observation: 2.0 s total / 0.1 s polling;
- delayed exact-message DOM appearance regression PASS;
- composer-empty without exact user-message proof rejected;
- generation-visible without exact user-message proof rejected;
- assistant-started without exact user-message proof rejected;
- same-page unsent fallback preserved;
- one-fresh-tab invariant preserved;
- restart with an unidentifiable prior fresh candidate fails closed rather than creating a replacement tab.

### Current production qualification status

On 2026-09-19, after manual closure of two stale fresh tabs created by an older broken build, the recovered production state preserved completed task `000053` and staged task `000054` without rerun or prompt mutation.

The current live qualification has progressed further than the previous failed runs: Rony reported that the watcher remained running and a Codex child for the staged next task was launched.

That is strong progress evidence, but it is **not yet final rollover closure**. The production milestone remains open until authoritative evidence confirms the complete transaction:

1. exactly one fresh Architect tab for the current transaction;
2. exact bootstrap submitted exactly once;
3. `ARCHITECT_SESSION_READY` observed;
4. new Architect conversation identity committed;
5. old Architect closed after authority commit;
6. task `000053` not rerun;
7. task `000054` launched exactly once;
8. no technical `HUMAN_REQUIRED` cutout during the successful transaction.

Until that verification is complete, universal-template extraction remains paused.

## Mature universal invariants

The following behavior should be treated as the template's protected core rather than redesigned project by project:

1. `recover where stopped -> run one task -> capture one result -> deliver once -> wait -> stage one next action`.
2. One visible Executor child at a time.
3. One writer/project/task.
4. Durable task identity and sequential task allocation owned by the Orchestrator.
5. Persistent logical Executor session with short-lived bounded task processes.
6. Orchestrator-owned isolated task worktrees.
7. No wall-clock timeout as task-completion authority.
8. Exactly-once result delivery with reconciliation before retry.
9. A completed project task is never rerun because transport or browser maintenance failed.
10. Project Architect independently verifies Executor evidence and owns project meaning.
11. Final human authority remains above Architect and Orchestrator.
12. Machine authority is an explicit `ORCHESTRATOR_RESULT` envelope; ordinary discussion is not executable authority.
13. `HUMAN_REQUIRED` can remain resident while the final human and Architect discuss naturally.
14. Once human authority is sufficient, the Project Architect emits a same-task envelope and the Orchestrator allocates the next task.
15. Discussion pause is a transport overlay, not a replacement workflow state.
16. Browser/session rollover is maintenance only and never business/workflow authority.
17. Recovery uses current durable facts rather than stale historical error labels.
18. Ambiguous external actions reconcile read-only before retry.
19. Durable privacy-safe runtime logging is mandatory.
20. Project-specific governance remains in the project, not in generic Orchestrator business logic.
21. One rollover transaction may create at most one fresh Architect tab.
22. Fresh-bootstrap delivery authority requires the exact bootstrap to be observed as a submitted user message on that same page.
23. Composer-empty, generation-visible or assistant-started signals are supporting evidence only and cannot substitute for exact fresh-bootstrap submission proof.
24. A successful but delayed ChatGPT DOM transition must be given a bounded observation window before a send is classified ambiguous.
25. After ambiguous fresh-session submission, recovery must reuse the same page or fail closed; it must never create a replacement tab automatically.

## Why the current source is not yet drop-in universal

The current runtime still contains AFFOTECH-specific implementation identity, including examples such as:

- `AFFOTECH_EXECUTOR_SESSION_ID`;
- `AFFOTECH_CHILD_PROJECT_DIR`;
- `AFFOTECH_CHILD_REMOTE`;
- `VERIFIED_ARCHITECT_CONVERSATION_ID`;
- `RELAY_REPOSITORY` and relay-path assumptions retained from earlier architecture;
- `AFFOTECH_EXECUTOR_BOOTSTRAP.md`;
- AFFOTECH-specific logger/module naming;
- Windows paths rooted in Rony's current workstation;
- project/branch assumptions embedded in tests and fixtures.

These are configuration/extraction concerns. They are not reasons to redesign the proven state machine.

## Universal template target

The universal template should separate three layers.

### 1. Generic runtime core

Project-neutral code owns:

- durable state machine;
- single-instance lock;
- task numbering;
- Executor child lifecycle;
- result capture;
- exactly-once Architect delivery;
- Architect envelope parsing;
- resident HUMAN_REQUIRED observation;
- discussion pause;
- logging;
- browser bridge lifecycle;
- rollover maintenance;
- recovery and duplicate protection.

### 2. Project configuration

A new project supplies configuration such as:

- project name/id;
- local repository path;
- remote repository;
- authoritative branch;
- Executor logical session ID;
- Architect conversation ID / browser endpoint;
- worktree/state roots;
- project Executor bootstrap path;
- optional project-specific validation hooks;
- any project-specific browser/application qualification adapters.

Configuration must be data, environment or a small project profile—not hardcoded branches inside the generic workflow engine.

### 3. Project governance

The Project Architect remains responsible for:

- architecture and roadmap;
- business policy;
- permission semantics;
- accepted milestone boundaries;
- project documentation ownership;
- protected areas;
- validation policy;
- the standing HUMAN AUTHORITY CONTINUATION rule;
- complete bounded Executor prompts.

The universal Orchestrator must never absorb those decisions.

## Minimum extraction milestone

A first universalization milestone remains deliberately narrow:

`ORCH.UNIVERSAL.TEMPLATE.EXTRACTION.1A`

Goal:

**Parameterize project identity and runtime wiring without changing the proven workflow semantics.**

Expected scope:

- introduce one project-profile/config structure;
- move AFFOTECH repository/path/branch/session/conversation/bootstrap values into that profile;
- make runtime consume the profile;
- keep AFFOTECH as the first concrete profile and prove behavior is unchanged;
- create a synthetic second project profile for deterministic qualification only;
- do not redesign state names, envelope schema, result-delivery proof, recovery semantics, resident HUMAN_REQUIRED behavior, discussion-pause behavior or the one-fresh-tab rollover contract.

This milestone is **not currently authorized to start** while September 19 rollover production qualification remains open.

## Universal template qualification gates

Do not call the extracted version universal until it proves at least:

- AFFOTECH profile regression parity with the then-current accepted production runtime;
- synthetic second-project isolation;
- no AFFOTECH-specific repository/path/session identity in generic runtime logic;
- task/state directories separated by project profile;
- no cross-project worktree or result-path collision;
- no cross-project Architect conversation leakage;
- exactly-once result delivery preserved;
- resident HUMAN_REQUIRED preserved;
- discussion pause preserved;
- restart/recovery preserved;
- malformed envelope fail-closed behavior preserved;
- one writer/project/task preserved;
- one-fresh-tab rollover invariant preserved;
- exact fresh-bootstrap submitted-user-message proof preserved;
- no replacement-tab creation after ambiguous fresh-session submission;
- project-specific bootstrap/governance remains outside generic runtime;
- one real browser-mediated smoke path for the first non-AFFOTECH project before declaring that project's production readiness.

## Deferred rollover qualification

The old documentation described deferred rollover as a limited maintenance gap. Real production use on September 19 proved a broader issue: the actual rollover transaction itself had multiple browser/DOM/recovery defects despite earlier component-level PASS results.

The repaired source now enforces the intended transaction:

`old Architect -> authoritative handover -> one fresh tab -> exact bootstrap submission -> ARCHITECT_SESSION_READY -> authority commit -> old Architect close -> resume staged project work`

Current status:

`PRODUCTION_QUALIFICATION_IN_PROGRESS`

The generic template must preserve the rule:

**rollover may be due, but maintenance must never rerun completed work, duplicate a fresh Architect tab, duplicate the handover/bootstrap, or invent project authority.**

## Migration strategy for future projects

For a future project, after the current AFFOTECH production qualification is closed:

1. instantiate the universal runtime with a new project profile;
2. provide that project's Executor bootstrap and canonical governance docs;
3. configure its Architect conversation and Executor logical session;
4. run deterministic qualification with no real business mutation;
5. perform one bounded real project milestone as production qualification;
6. preserve the same human/Architect/Executor authority separation.

A future project should not spend weeks rebuilding transport, recovery, rollover, discussion pause, result delivery or HUMAN_REQUIRED semantics. The purpose of the template is to preserve the already-proven contract, not copy unresolved runtime defects.

## Decision

Current design maturity classification:

`REFERENCE_DESIGN_MATURE`

Current AFFOTECH runtime source/test checkpoint:

`49bf2c72699fdd8e0df2cbb31091b6751ffd6797`

Universalization readiness:

`PAUSED_PENDING_ROLLOVER_PRODUCTION_QUALIFICATION`

Drop-in template status:

`NOT_YET_EXTRACTED`

The next Orchestrator engineering objective is **not** universal extraction until the current rollover production qualification is verified and documented closed. AFFOTECH product progress remains the higher priority once the maintenance incident is resolved.
