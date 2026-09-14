# AFFOTECH Local Orchestrator — Universal Template Readiness

## Status

The current AFFOTECH Local Orchestrator is **ready to become the canonical reference implementation for a universal project Orchestrator template**.

It is **not yet a drop-in universal template**.

The distinction is intentional:

- the workflow model, state machine, transport rules, recovery rules, human-control boundaries and production lessons are mature enough to preserve as the universal design;
- the current source still contains AFFOTECH-specific constants, repository paths, session identity, browser conversation identity and bootstrap assumptions that must be parameterized before another project can adopt it safely.

Universalization must therefore be an extraction/configuration milestone, not a redesign of the proven workflow.

## Current source authority

Repository: `nakfreeajer/affotech-agent-orchestrator`

Branch: `main`

Accepted runtime/source checkpoint:

`57a3a914b35ed0c715aaaf0267ed0bd36a39cc78` — `fix(orchestrator): keep human decisions resident`

Deterministic qualification at that checkpoint:

- Python compile PASS;
- `244 passed / 0 failed`;
- `git diff --check` PASS;
- resident HUMAN_REQUIRED regression PASS;
- ordinary discussion regression PASS;
- later same-task EXECUTE-envelope regression PASS;
- repeated HUMAN_REQUIRED regression PASS;
- STOP-remains-resident regression PASS;
- attach/read retry regression PASS;
- exactly-one-next-task launch regression PASS;
- existing recovery, rollover and durable receipt regressions PASS.

The final human authority has subsequently reported the production watcher running smoothly and mature in normal AFFOTECH operation after the resident HUMAN_REQUIRED repair and the matching AFFOTECH Architect-governance synchronization.

This operational evidence is sufficient to move the project from **stabilization** to **universalization readiness**. It does not erase explicitly documented qualification gaps such as deferred live-Executor rollover servicing.

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

A first universalization milestone should be deliberately narrow:

`ORCH.UNIVERSAL.TEMPLATE.EXTRACTION.1A`

Goal:

**Parameterize project identity and runtime wiring without changing the proven workflow semantics.**

Expected scope:

- introduce one project-profile/config structure;
- move AFFOTECH repository/path/branch/session/conversation/bootstrap values into that profile;
- make runtime consume the profile;
- keep AFFOTECH as the first concrete profile and prove behavior is unchanged;
- create a synthetic second project profile for deterministic qualification only;
- do not redesign state names, envelope schema, result-delivery proof, recovery semantics, resident HUMAN_REQUIRED behavior or discussion-pause behavior.

## Universal template qualification gates

Do not call the extracted version universal until it proves at least:

- AFFOTECH profile regression parity with the accepted runtime;
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
- project-specific bootstrap/governance remains outside generic runtime;
- one real browser-mediated smoke path for the first non-AFFOTECH project before declaring that project's production readiness.

## Deferred rollover qualification

The current known deferred-rollover/live-Executor reachability concern remains a maintenance qualification gap.

Universalization must not use that gap as a reason to reopen the mature workflow core.

The generic template should preserve the rule:

**rollover may be due, but maintenance must never halt or own project workflow authority.**

A separate bounded rollover-maintenance milestone may later improve safe-boundary servicing if production evidence makes it necessary.

## Migration strategy for future projects

For a future project:

1. instantiate the universal runtime with a new project profile;
2. provide that project's Executor bootstrap and canonical governance docs;
3. configure its Architect conversation and Executor logical session;
4. run deterministic qualification with no real business mutation;
5. perform one bounded real project milestone as production qualification;
6. preserve the same human/Architect/Executor authority separation.

A future project should not spend weeks rebuilding transport, recovery, rollover, discussion pause, result delivery or HUMAN_REQUIRED semantics.

## Decision

Current maturity classification:

`REFERENCE_IMPLEMENTATION_MATURE`

Universalization readiness:

`READY_FOR_EXTRACTION`

Drop-in template status:

`NOT_YET_EXTRACTED`

The next Orchestrator engineering objective may therefore be universal template extraction, provided it remains bounded and does not interrupt authorized AFFOTECH product work.