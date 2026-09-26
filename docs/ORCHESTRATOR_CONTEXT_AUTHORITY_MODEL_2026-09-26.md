# AFFOTECH Orchestrator — Context Authority Model

Date: 2026-09-26
Status: ARCHITECTURAL FINDING — PRE-MERGE
Base commit: `4aba962310cabd75611d00315c214b35bf6f5822`

This document records an important architectural finding about how AFFOTECH context, memory, and authority are distributed between Git/repository documentation, the ChatGPT Architect, the persistent Codex Executor session, and the Documentation Curator.

It is intentionally separated from the active rollover state-machine repair. It does not change runtime behavior and must not expand the scope of `ORCHESTRATOR.ROLLOVER.STATE.MACHINE.CONSOLIDATION.1A` or its implementation phases.

## 1. Core finding

AFFOTECH must not be modeled as two stateless agents receiving a fresh context package on every task.

The Codex Executor is deliberately resumed from one governed persistent session across bounded tasks, while the Architect session is replaceable and may roll over when its browser/session resource boundary requires it.

The intended context architecture is therefore asymmetric:

```text
Git / curated documentation
    = durable institutional authority

Persistent Codex Executor session
    = implementation working memory and continuity

Current Architect conversation
    = governance/reasoning working memory, replaceable through rollover

Documentation Curator
    = mechanism that keeps durable institutional authority synchronized
```

These layers complement each other. None should be treated as a substitute for the others.

## 2. Source evidence: Executor continuity is enforced

The watcher defines one governed Executor session identity:

```python
AFFOTECH_EXECUTOR_SESSION_ID = "019f842e-98bc-7672-a619-51441d91be00"
```

`LocalFirstOrchestrator` persists:

```python
executorSessionId = AFFOTECH_EXECUTOR_SESSION_ID
executorSessionMode = "PERSISTENT"
```

The visible Executor launcher enforces this identity before each normal bounded launch:

```python
configured_session = watcher.state.get("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)
if configured_session != AFFOTECH_EXECUTOR_SESSION_ID:
    raise RuntimeError("EXECUTOR_SESSION_IDENTITY_MISMATCH")
verify_executor_session(AFFOTECH_EXECUTOR_SESSION_ID)
runner = CodexRunner(project, child_project_dir=target, session_id=AFFOTECH_EXECUTOR_SESSION_ID)
watcher.state.update({
    "executorSessionId": AFFOTECH_EXECUTOR_SESSION_ID,
    "executorSessionMode": "PERSISTENT",
})
```

Therefore:

- a new OS Codex child process is allowed;
- a different Executor conversation/session is not part of normal operation;
- persistent Executor continuity is a runtime invariant, not merely a convention.

## 3. Process continuity is not authority

Persistent Executor memory is valuable because it preserves implementation continuity across AFFOTECH milestones, including familiarity with:

- files and modules previously changed;
- earlier debugging history;
- architecture vocabulary;
- prior bounded tasks;
- previous test approaches;
- implementation rationale encountered during execution.

However, conversational continuity must not become the sole authority for project facts.

The authoritative basis for work remains:

```text
current Architect bounded instruction
+ current Git repository/worktree
+ accepted commits/tags/tests/evidence
+ canonical curated documentation
```

The persistent Executor session may remember how and why earlier work happened, but Git and curated documentation must remain independently inspectable institutional authority.

This distinction prevents a long-lived Executor conversation from silently outranking newer accepted source or documentation.

## 4. Architect and Executor intentionally use different continuity models

### Architect

The Architect conversation may be replaced through rollover. Its working-memory continuity therefore cannot be treated as permanent authority.

A fresh Architect should be able to reconstruct the authoritative working state from durable evidence plus a bounded rollover handover.

### Executor

The Executor should retain one persistent Codex session across bounded tasks unless Rony explicitly authorizes session replacement.

This persistent session is beneficial because implementation history is costly to repeatedly reconstruct and because the Executor performs sequential work inside the same evolving codebase.

### Consequence

The system should not attempt to make both roles stateless merely for symmetry.

The correct architecture is intentionally asymmetric:

```text
Architect:
replaceable working session
+ rehydration from durable authority

Executor:
persistent working session
+ durable authority remains Git/docs
```

## 5. Curator role

The Documentation Curator exists to ensure important accepted knowledge does not live only in either conversational session.

The desired knowledge flow is:

```text
implementation / accepted decision
        -> Architect acceptance
        -> Curator documentation synchronization
        -> canonical Git documentation
```

That makes repository documentation the institutional memory shared across future Architect sessions, Executor continuity, and human review.

The Curator therefore does not exist because both agents are expected to forget everything. It exists because conversational memory must never be the only place an important governance or architecture decision survives.

## 6. Rollover implication

The current fresh-Architect bootstrap is built from the outgoing Architect handover plus the machine protocol.

That is sufficient for the current transport design, but it does not yet fully exploit the Curator architecture.

A future bounded improvement should investigate rehydrating the fresh Architect from:

```text
1. project / repository / branch identity;
2. current accepted baseline;
3. current task and milestone identity;
4. relevant canonical current-state / handover / decision documentation;
5. permanent non-regression rules;
6. exact rollover handover;
7. machine protocol.
```

The objective is not to dump all project documentation into every fresh Architect session. The objective is to use the relevant curated subset as durable authority so the outgoing Architect handover does not have to carry the entire institutional memory itself.

This future improvement is separate from the current rollover scheduling/liveness repair.

## 7. Executor continuity invariant

The following rule is permanent unless Rony explicitly changes it:

> Every normal bounded AFFOTECH Executor launch must resume the governed persistent Executor session. A new operating-system process is allowed; a new Executor conversation/session is not allowed without explicit human authorization.

This invariant must survive:

- rollover state-machine consolidation;
- crash recovery;
- task-worktree changes;
- browser/API transport changes;
- future Architect rehydration changes.

A future refactor must not accidentally reinterpret process restart as permission to replace the governed Executor session.

## 8. Context-drift model

Persistent Executor continuity reduces one major source of context drift, but it does not eliminate all drift by itself.

Potential drift still exists if:

- repository source changes after an earlier conversational assumption;
- Architect governance changes;
- canonical documentation is updated;
- an accepted milestone supersedes an earlier implementation approach;
- a long-lived session retains obsolete assumptions.

Therefore each bounded task must still be anchored to current authority rather than relying solely on conversation memory.

The correct model is:

```text
persistent memory supports continuity
but
current durable authority governs correctness
```

## 9. Architectural layers

The combined context architecture should be understood as four layers:

### Layer A — durable institutional authority

- Git source;
- accepted commits/tags;
- tests and evidence;
- canonical curated Markdown.

### Layer B — persistent Executor working memory

- one governed Codex session;
- sequential implementation continuity;
- prior local reasoning and code familiarity.

### Layer C — replaceable Architect working memory

- current governance/reasoning session;
- independently verifies Executor evidence;
- may roll over and be rehydrated.

### Layer D — Curator synchronization

- promotes accepted durable knowledge into canonical documentation;
- prevents either conversation from becoming the sole historical record.

## 10. Implication for browser/API/hybrid evaluation

A future evaluation of browser-based Architect transport versus API or hybrid transport must preserve this context model.

Moving automated Architect turns to an API must NOT imply replacing the persistent Executor session with stateless execution.

Likewise, preserving the ChatGPT browser Architect must NOT imply that Architect chat history is the institutional knowledge store.

Transport and context authority are separate architectural decisions.

## 11. Relationship to the active rollover repair

The active repair remains focused on one confirmed problem class:

- duplicated rollover transition authority;
- safety without liveness;
- contradictory WAIT versus RECOVER predicates;
- inadequate journey-level qualification.

This context-authority finding does not modify that repair plan.

After rollover scheduling is stabilized and production-qualified, a separate bounded milestone may investigate:

`ARCHITECT.CONTEXT.REHYDRATION.FROM.CURATED.AUTHORITY`

Such a milestone must preserve the Executor continuity invariant above.

## 12. Permanent non-regression rules

1. Do not casually replace the governed persistent Executor session.
2. Do not equate a disposable Codex OS process with a disposable Codex conversation.
3. Do not treat persistent Executor memory as stronger authority than current Git/docs/evidence.
4. Do not make Architect conversation history the sole institutional memory.
5. Curated repository documentation remains the durable cross-session knowledge layer.
6. Architect rollover/replacement must preserve governance continuity through durable authority and bounded handover.
7. Future API/browser transport changes must preserve the separation between transport, working memory, and institutional authority.
8. Current rollover repair scope must not be expanded by this finding.

## 13. Summary

AFFOTECH context continuity is intentionally hybrid rather than stateless:

```text
Git/docs          = institutional authority
Persistent Codex  = implementation working memory
Architect session = replaceable governance working memory
Curator           = durable synchronization mechanism
```

This architecture allows the Executor to retain deep implementation continuity while allowing Architect sessions to be replaced safely and independently verified against durable project authority.

The key rule is:

> Conversation memory provides continuity; repository evidence provides authority.

For the Executor specifically, continuity is stronger than ordinary conversation continuity because the watcher deliberately resumes the same governed Codex session across bounded tasks. That behavior must remain an explicit non-regression invariant throughout all future orchestrator repairs.