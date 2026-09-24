# Crash / Reboot Recovery

From the orchestrator repository, run the visible operator bootstrap:

```powershell
.\AFFOTECH-START.ps1
```

It discovers the canonical state and infrastructure first, prints the recovery
summary, and asks for `START` before launching the resident watcher visibly in
the current terminal. It never automatically retries a failed Executor or
resends an Architect payload.

For read-only inspection:

```powershell
.\AFFOTECH-START.ps1 -StatusOnly
```

For one explicitly human-authorized post-launch retry, use the exact current
task ID only after reviewing the displayed preflight:

```powershell
.\AFFOTECH-START.ps1 -AuthorizeRetry <taskId>
```

The retry authorization is process-local and one-shot. `HUMAN_REQUIRED` with
no explicit authorization remains passive. `ARCHITECT_RUNNING` and
`EXECUTOR_RUNNING` are reconciled conservatively: existing generation/result
evidence is preferred, and ambiguous crash evidence fails closed. The watcher
is never launched hidden and an already-running watcher is not duplicated.
