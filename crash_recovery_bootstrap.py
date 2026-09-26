"""Read-only crash/reboot discovery for AFFOTECH-START.ps1.

This module deliberately does not mutate orchestrator state or start workflow
processes.  The PowerShell wrapper owns the operator confirmation and visible
watcher start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


KNOWN_STATES = {
    "IDLE", "NEXT_PROMPT_READY", "RESULT_READY", "ARCHITECT_RUNNING",
    "EXECUTOR_RUNNING", "EXECUTOR_CRASHED", "HUMAN_REQUIRED",
}
EXPECTED_REPOSITORY_IDENTITY = "nakfreeajer/affotech-agent-orchestrator"
EXPECTED_BRANCH = "main"
LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID = "7fdbd798659f42295a18dd2d"
LEGACY_DIAGNOSTIC_RETRY_TASK_ID = "000103"
LEGACY_DIAGNOSTIC_RETRY_COMPLETED_TASK_ID = "000102"
LEGACY_DIAGNOSTIC_RETRY_EXECUTOR_SESSION_ID = "019f842e-98bc-7672-a619-51441d91be00"
LEGACY_DIAGNOSTIC_RETRY_PROMPT_SHA256 = "70D6ECCAB4FED573CD03C4DDF3867073E087C47927EBDF25DBFC63554F6EDE85"
LEGACY_DIAGNOSTIC_RETRY_FAILED_EPOCH = 2
LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH = 3
LEGACY_POSTFIX_QUALIFICATION_FIX_COMMIT = "2fe16f5e387f7dc97142b08ea38b4824e6dc44d3"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8").strip()


def repository_identity(remote: str) -> str:
    value = str(remote or "").strip().replace("\\", "/")
    if "://" in value:
        value = value.split("://", 1)[1]
    elif value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    value = value.split("/", 1)[1] if "/" in value and value.split("/", 1)[0].lower() in {"github.com", "www.github.com"} else value
    return value.removesuffix(".git").strip("/").lower()


def valid_architect_baseline(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("count"), int)
        and not isinstance(value.get("count"), bool)
        and value["count"] >= 0
        and isinstance(value.get("text_hash"), str)
        and bool(re.fullmatch(r"[0-9a-fA-F]{64}", value["text_hash"]))
    )


def architect_baseline_advanced(persisted: Any, current: Any) -> bool:
    if not valid_architect_baseline(persisted) or not valid_architect_baseline(current):
        return False
    if current["count"] < persisted["count"]:
        return False
    return current["count"] > persisted["count"] or current["text_hash"].lower() != persisted["text_hash"].lower()


def _result_exists(state: dict[str, Any]) -> bool:
    path = state.get("executorResultPath")
    if not isinstance(path, str) or not path:
        return False
    candidate = Path(path)
    try:
        return candidate.is_file() and bool(candidate.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _default_process_records() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    command = "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    try:
        output = subprocess.check_output(["powershell", "-NoProfile", "-Command", command], text=True, encoding="utf-8")
        if not output.strip():
            return []
        value = json.loads(output)
        return value if isinstance(value, list) else [value]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def _pid_alive(pid: Any, process_records: list[dict[str, Any]]) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    return any(int(row.get("ProcessId", -1) or -1) == value for row in process_records)


def _session_writer_records(session_id: str, process_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    token = str(session_id or "")
    if not token:
        return []
    return [
        row for row in process_records
        if token in str(row.get("CommandLine") or "")
        and "local_orchestrator_watcher.py" not in str(row.get("CommandLine") or "")
    ]


def _session_exists(session_id: str, codex_home: Path | None = None) -> bool:
    root = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    index = root / "session_index.jsonl"
    try:
        return any(str(session_id) and str(session_id) in line for line in index.read_text(encoding="utf-8").splitlines())
    except OSError:
        return False


def discover(
    repository: str | os.PathLike[str],
    state_dir: str | os.PathLike[str] | None = None,
    process_records: list[dict[str, Any]] | None = None,
    codex_home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read repository/state identity and return a JSON-safe discovery record."""
    repo = Path(repository).resolve()
    durable_dir = Path(state_dir).resolve() if state_dir else repo / ".agent-work" / "orchestrator"
    state_path = durable_dir / "state.json"
    if not state_path.is_file():
        raise RuntimeError("CANONICAL_STATE_NOT_FOUND")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("CANONICAL_STATE_CORRUPT") from error
    if not isinstance(state, dict):
        raise RuntimeError("CANONICAL_STATE_CORRUPT")
    try:
        head = _git(repo, "rev-parse", "HEAD")
        branch = _git(repo, "branch", "--show-current")
        remote = _git(repo, "config", "--get", "remote.origin.url")
        cleanliness = not bool(_git(repo, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("REPOSITORY_IDENTITY_UNAVAILABLE") from error
    identity = repository_identity(remote)
    if not branch or not head or branch != EXPECTED_BRANCH or identity != EXPECTED_REPOSITORY_IDENTITY:
        raise RuntimeError("REPOSITORY_IDENTITY_INCONSISTENT")
    records = process_records if process_records is not None else _default_process_records()
    session_id = str(state.get("executorSessionId") or "")
    pid = state.get("codexPid") or state.get("active_codex_pid")
    watcher_records = [
        row for row in records
        if "local_orchestrator_watcher.py" in str(row.get("CommandLine") or "")
    ]
    writers = _session_writer_records(session_id, records)
    executor_process = next(
        (row for row in records if str(row.get("ProcessId")) == str(pid)), None
    )
    executor_pid_owned = bool(
        executor_process
        and session_id
        and session_id in str(executor_process.get("CommandLine") or "")
        and "local_orchestrator_watcher.py" not in str(executor_process.get("CommandLine") or "")
    )
    prompt_path = state.get("nextPromptPath")
    prompt = Path(prompt_path) if isinstance(prompt_path, str) and prompt_path else None
    result_path = state.get("executorResultPath")
    result = Path(result_path) if isinstance(result_path, str) and result_path else None
    return {
        "repository": str(repo),
        "stateDir": str(durable_dir),
        "statePath": str(state_path),
        "head": head,
        "branch": branch,
        "remoteOrigin": remote,
        "repositoryIdentity": identity,
        "repositoryIdentityValid": identity == EXPECTED_REPOSITORY_IDENTITY,
        "workingTreeClean": cleanliness,
        "state": state,
        "watcherRunning": bool(watcher_records),
        "watcherProcesses": watcher_records,
        "executorPidAlive": _pid_alive(pid, records),
        "executorPidAliveByField": {
            "codexPid": _pid_alive(state.get("codexPid"), records),
            "active_codex_pid": _pid_alive(state.get("active_codex_pid"), records),
        },
        "executorPidOwned": executor_pid_owned,
        "executorProcess": executor_process,
        "executorPid": pid,
        "activeWriterPresent": bool(writers),
        "activeWriters": writers,
        "executorSessionExists": _session_exists(session_id, Path(codex_home) if codex_home else None),
        "promptExists": bool(prompt and prompt.is_file()),
        "promptSha256": hashlib.sha256(prompt.read_bytes()).hexdigest() if prompt and prompt.is_file() else None,
        "resultExists": bool(result and result.is_file()),
        "resultNonEmpty": _result_exists(state),
    }


def classify_architect_observation(state: dict[str, Any], observation: dict[str, Any] | None) -> str:
    """Classify an ARCHITECT_RUNNING observation without changing state."""
    if state.get("state") != "ARCHITECT_RUNNING":
        return "NOT_APPLICABLE"
    if not observation:
        return "ARCHITECT_RECOVERY_INCONCLUSIVE"
    if observation.get("generationVisible"):
        return "WAIT_EXISTING_ARCHITECT"
    if (
        observation.get("newCompletedResponse")
        and observation.get("durableBaselineValid")
        and observation.get("durableBaselineAdvanced")
    ):
        return "CONSUME_EXISTING_ARCHITECT_RESPONSE"
    return "ARCHITECT_RECOVERY_INCONCLUSIVE"


def classify_workflow(discovery: dict[str, Any], architect_observation: dict[str, Any] | None = None) -> str:
    state = discovery["state"]
    workflow = state.get("state")
    if workflow not in KNOWN_STATES:
        return "BLOCK_INCONSISTENT_STATE"
    if workflow == "IDLE":
        return "SAFE_NORMAL_START"
    if workflow == "NEXT_PROMPT_READY":
        return "SAFE_NORMAL_START"
    if workflow == "RESULT_READY":
        return "SAFE_RESULT_RECOVERY" if discovery["resultNonEmpty"] else "BLOCK_RESULT_EVIDENCE_MISSING"
    if workflow == "ARCHITECT_RUNNING":
        return classify_architect_observation(state, architect_observation)
    if workflow == "EXECUTOR_RUNNING":
        executor_pid = discovery.get("executorPid")
        executor_owned = bool(discovery.get("executorPidOwned"))
        other_writers = [
            row for row in discovery.get("activeWriters", [])
            if str(row.get("ProcessId")) != str(executor_pid)
        ]
        if other_writers:
            return "EXECUTOR_SESSION_ACTIVE_WRITER"
        if discovery["executorPidAlive"] and executor_owned:
            return "WAIT_EXISTING_EXECUTOR"
        if discovery["executorPidAlive"] and not executor_owned:
            return "EXECUTOR_PID_OWNERSHIP_INCONCLUSIVE"
        if discovery["activeWriterPresent"]:
            return "EXECUTOR_SESSION_ACTIVE_WRITER"
        if discovery["resultNonEmpty"]:
            return "RECOVER_EXISTING_EXECUTOR_RESULT"
        return "EXECUTOR_INTERRUPTED_NO_RESULT"
    if workflow == "HUMAN_REQUIRED":
        return "HUMAN_REQUIRED_NO_AUTOMATIC_ACTION"
    return "BLOCK_INCONSISTENT_STATE"


def validate_retry(discovery: dict[str, Any], task_id: str) -> tuple[bool, str]:
    """Validate explicit retry authority without setting environment or state."""
    state = discovery["state"]
    if discovery["watcherRunning"]:
        return False, "WATCHER_ALREADY_RUNNING"
    if state.get("state") != "HUMAN_REQUIRED":
        return False, "RETRY_STATE_INVALID"
    if str(state.get("taskId") or "") != str(task_id):
        return False, "HUMAN_RECOVERY_TASK_MISMATCH"
    if state.get("humanRequiredReason") != "EXECUTOR_EXITED_WITHOUT_RESULT":
        return False, "RETRY_REASON_INVALID"
    if state.get("executorLaunchState") != "POSTLAUNCH_NO_RESULT":
        return False, "RETRY_LAUNCH_STATE_INVALID"
    if discovery["resultNonEmpty"]:
        return False, "RECOVERY_RESULT_ALREADY_EXISTS"
    if discovery["executorPidAlive"] or discovery["activeWriterPresent"]:
        return False, "EXECUTOR_SESSION_ACTIVE_WRITER"
    if not discovery["promptExists"]:
        return False, "RECOVERY_PROMPT_MISSING"
    if not discovery["executorSessionExists"]:
        return False, "EXECUTOR_SESSION_NOT_FOUND"
    if state.get("humanRecoveryAuthorizationConsumed") and state.get("humanRecoveryAuthorizedTaskId") == str(task_id):
        return False, "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED"
    if state.get("rolloverDue") or state.get("rolloverPending") or state.get("rolloverInProgress"):
        return False, "ROLLOVER_ACTIVE"
    return True, "SAFE_TO_AUTHORIZE_ONE_RETRY"


def _validated_pending_handover_exists(state: dict[str, Any]) -> bool:
    response = state.get("pending_handover")
    if not isinstance(response, str) or not response:
        return False
    try:
        from local_orchestrator_watcher import (
            _legacy_handover_compatibility_allowed,
            architect_handover_ready,
            handover_transaction_matches,
            parse_handover_envelope,
        )
        transaction_id = LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID
        task_id = LEGACY_DIAGNOSTIC_RETRY_TASK_ID
        parsed = parse_handover_envelope(response)
        valid = bool(
            parsed is not None
            and parsed.get("transactionId") == transaction_id
            and parsed.get("taskId") == task_id
        )
        if not valid and _legacy_handover_compatibility_allowed(state):
            valid = bool(
                architect_handover_ready(response)
                and handover_transaction_matches(response, transaction_id)
                and task_id in response
            )
        if not valid:
            return False
        digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
        stored = state.get("rolloverHandoverResponseIdentity")
        return not stored or stored == digest
    except (ImportError, AttributeError, TypeError, ValueError):
        return False


def validate_rollover_diagnostic_retry(discovery: dict[str, Any]) -> tuple[bool, str]:
    """Read-only exact-incident authorization preflight; never changes state."""
    state = discovery.get("state")
    if not isinstance(state, dict):
        return False, "STATE_INVALID"
    if discovery.get("watcherRunning"):
        return False, "WATCHER_ALREADY_RUNNING"
    if state.get("state") != "HUMAN_REQUIRED":
        return False, "STATE_NOT_HUMAN_REQUIRED"
    if state.get("humanRequiredReason") != "LEGACY_HANDOVER_REEMISSION_INVALID":
        return False, "HUMAN_REQUIRED_REASON_MISMATCH"
    if state.get("discussionPauseActive") not in (None, False):
        return False, "DISCUSSION_PAUSE_ACTIVE"
    if (state.get("taskId") != LEGACY_DIAGNOSTIC_RETRY_COMPLETED_TASK_ID
            or state.get("lastCompletedTaskId") != LEGACY_DIAGNOSTIC_RETRY_COMPLETED_TASK_ID):
        return False, "COMPLETED_TASK_BOUNDARY_MISMATCH"
    if state.get("nextTaskId") != LEGACY_DIAGNOSTIC_RETRY_TASK_ID:
        return False, "NEXT_TASK_MISMATCH"
    if (state.get("rolloverDue") is not True or state.get("rolloverPending") is not True
            or state.get("handoverRequested") is not True):
        return False, "ROLLOVER_AUTHORITY_MISMATCH"
    if (state.get("handoverReady") is True
            or state.get("rolloverFreshCandidateConversationId")
            or state.get("rolloverFreshCandidateState") in {"ACK_PENDING", "SUBMISSION_AMBIGUOUS"}):
        return False, "ROLLOVER_ALREADY_ADVANCED"
    if (state.get("rolloverTransactionId") != LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID
            or state.get("rolloverTransactionTaskId") != LEGACY_DIAGNOSTIC_RETRY_TASK_ID):
        return False, "TRANSACTION_IDENTITY_MISMATCH"
    if state.get("rolloverHandoverProtocolVersion") is not None:
        return False, "LEGACY_PROTOCOL_BOUNDARY_MISMATCH"
    prompt_path = state.get("nextPromptPath")
    state_dir = Path(str(discovery.get("stateDir") or ""))
    expected_prompt = state_dir / "prompts" / f"{LEGACY_DIAGNOSTIC_RETRY_TASK_ID}.txt"
    try:
        if (not isinstance(prompt_path, str) or Path(prompt_path).resolve() != expected_prompt.resolve()
                or not expected_prompt.is_file()
                or hashlib.sha256(expected_prompt.read_bytes()).hexdigest().upper() != LEGACY_DIAGNOSTIC_RETRY_PROMPT_SHA256):
            return False, "STAGED_PROMPT_IDENTITY_MISMATCH"
        worktrees = state.get("taskWorktrees")
        entry = worktrees.get(LEGACY_DIAGNOSTIC_RETRY_TASK_ID) if isinstance(worktrees, dict) else None
        if (not isinstance(entry, dict) or entry.get("taskId") != LEGACY_DIAGNOSTIC_RETRY_TASK_ID
                or not entry.get("worktreePath") or not Path(str(entry["worktreePath"])).is_dir()):
            return False, "STAGED_WORKTREE_EVIDENCE_INVALID"
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, "STAGED_PROMPT_IDENTITY_MISMATCH"
    if (state.get("executorSessionId") != LEGACY_DIAGNOSTIC_RETRY_EXECUTOR_SESSION_ID
            or state.get("executorSessionMode") != "PERSISTENT"
            or not discovery.get("executorSessionExists")):
        return False, "PERSISTENT_EXECUTOR_SESSION_MISMATCH"
    alive_by_field = discovery.get("executorPidAliveByField") or {}
    if (bool(state.get("executorActiveWriter")) or bool(state.get("governedExecutorActiveWriter"))
            or discovery.get("activeWriterPresent") or alive_by_field.get("codexPid")
            or alive_by_field.get("active_codex_pid") or discovery.get("executorPidAlive")
            or str(state.get("executorProcessState") or "").upper() in {"RUNNING", "STARTING", "ACTIVE"}):
        return False, "EXECUTOR_PROCESS_OR_WRITER_ACTIVE"
    epoch = state.get("rolloverRecoveryEpoch")
    if epoch is None:
        epoch = state.get("rolloverAutomaticRecoveryEpochCount")
    if (isinstance(epoch, bool) or epoch != LEGACY_DIAGNOSTIC_RETRY_FAILED_EPOCH
            or state.get("rolloverLegacyHandoverReemissionTransactionId") != LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID
            or state.get("rolloverLegacyHandoverReemissionAttemptedEpoch") != LEGACY_DIAGNOSTIC_RETRY_FAILED_EPOCH
            or state.get("rolloverLegacyHandoverReemissionState") != "INVALID_RESPONSE"):
        return False, "FAILED_REEMISSION_EPOCH_NOT_PROVEN"
    count = state.get("rolloverAutomaticRecoveryEpochCount")
    maximum = state.get("rolloverAutomaticRecoveryMaxEpochs")
    if (isinstance(count, bool) or not isinstance(count, int) or isinstance(maximum, bool)
            or not isinstance(maximum, int) or count < 0 or maximum <= 0 or count >= maximum):
        return False, "RECOVERY_EPOCH_BUDGET_UNAVAILABLE"
    if state.get("rolloverDiagnosticRetryAuthorizationConsumedTransactionId") == LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID:
        return False, "ROLLOVER_DIAGNOSTIC_AUTHORIZATION_ALREADY_CONSUMED"
    validated_handover = _validated_pending_handover_exists(state)
    if validated_handover:
        return False, "VALIDATED_DURABLE_HANDOVER_ALREADY_EXISTS"
    if (state.get("rolloverHandoverResponseIdentity")
            or state.get("rolloverFreshBootstrapPayloadHash")):
        return False, "DURABLE_HANDOVER_EVIDENCE_INCOMPLETE"
    return True, "SAFE_TO_AUTHORIZE_ONE_ROLLOVER_DIAGNOSTIC_RETRY"


def _legacy_postfix_epoch3_diagnostic_proven(discovery: dict[str, Any]) -> bool:
    state_dir = Path(str(discovery.get("stateDir") or ""))
    directory = state_dir / "logs" / "diagnostic" / "legacy-handover-reemission"
    try:
        artifacts = list(directory.glob(
            f"{LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID}-epoch-{LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH}-observation-*.json"
        ))
        if len(artifacts) != 1:
            return False
        artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(artifact, dict):
        return False
    try:
        if Path(str(artifact.get("artifactPath") or "")).resolve() != artifacts[0].resolve():
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    waiter = artifact.get("waiterDiagnostics")
    return bool(
        artifact.get("transactionId") == LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID
        and artifact.get("taskId") == LEGACY_DIAGNOSTIC_RETRY_TASK_ID
        and artifact.get("recoveryEpoch") == LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH
        and artifact.get("observedState") == "BLOCKED"
        and artifact.get("textLengthChars") == 0
        and artifact.get("textUtf8Bytes") == 0
        and artifact.get("textSha256") == hashlib.sha256(b"").hexdigest()
        and artifact.get("artifactWritten") is True
        and isinstance(waiter, dict)
        and waiter.get("completionReason") == "IDENTITY_CHANGED_WITH_EMPTY_TEXT"
        and waiter.get("pollCount") == 1
        and waiter.get("baselineAssistantCount") == 0
        and waiter.get("candidateAssistantCount") == 0
    )


def validate_rollover_postfix_qualification(discovery: dict[str, Any]) -> tuple[bool, str]:
    """Read-only eligibility for one human-authorized post-waiter-fix qualification."""
    state = discovery.get("state")
    if not isinstance(state, dict):
        return False, "STATE_INVALID"
    if discovery.get("watcherRunning"):
        return False, "WATCHER_ALREADY_RUNNING"
    if state.get("state") != "HUMAN_REQUIRED":
        return False, "STATE_NOT_HUMAN_REQUIRED"
    if state.get("humanRequiredReason") != "LEGACY_HANDOVER_REEMISSION_INVALID":
        return False, "HUMAN_REQUIRED_REASON_MISMATCH"
    if (state.get("taskId") != LEGACY_DIAGNOSTIC_RETRY_COMPLETED_TASK_ID
            or state.get("lastCompletedTaskId") != LEGACY_DIAGNOSTIC_RETRY_COMPLETED_TASK_ID):
        return False, "COMPLETED_TASK_BOUNDARY_MISMATCH"
    if state.get("nextTaskId") != LEGACY_DIAGNOSTIC_RETRY_TASK_ID:
        return False, "NEXT_TASK_MISMATCH"
    if (state.get("rolloverDue") is not True or state.get("rolloverPending") is not True
            or state.get("handoverRequested") is not True):
        return False, "ROLLOVER_AUTHORITY_MISMATCH"
    transaction_id = LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID
    task_id = LEGACY_DIAGNOSTIC_RETRY_TASK_ID
    if (state.get("rolloverTransactionId") != transaction_id
            or state.get("rolloverTransactionTaskId") != task_id):
        return False, "TRANSACTION_IDENTITY_MISMATCH"
    if state.get("rolloverRecoveryEpoch") != LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH:
        return False, "FAILED_EPOCH_MISMATCH"
    if (state.get("rolloverLegacyHandoverReemissionTransactionId") != transaction_id
            or state.get("rolloverLegacyHandoverReemissionAttemptedEpoch") != LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH
            or state.get("rolloverLegacyHandoverReemissionState") != "INVALID_RESPONSE"):
        return False, "FAILED_REEMISSION_EPOCH_NOT_PROVEN"
    if not _legacy_postfix_epoch3_diagnostic_proven(discovery):
        return False, "EPOCH3_DIAGNOSTIC_PROOF_MISSING_OR_INVALID"

    prior_authorization = state.get("rolloverDiagnosticRetryAuthorization")
    if not (
        state.get("rolloverDiagnosticRetryAuthorizationConsumedTransactionId") == transaction_id
        and isinstance(prior_authorization, dict)
        and prior_authorization.get("transactionId") == transaction_id
        and prior_authorization.get("taskId") == task_id
        and prior_authorization.get("previousEpoch") == LEGACY_DIAGNOSTIC_RETRY_FAILED_EPOCH
        and prior_authorization.get("newEpoch") == LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH
    ):
        return False, "PRIOR_AUTHORIZATION_EVIDENCE_MISSING"
    count = state.get("rolloverAutomaticRecoveryEpochCount")
    maximum = state.get("rolloverAutomaticRecoveryMaxEpochs")
    if (isinstance(count, bool) or count != 3 or isinstance(maximum, bool) or maximum != 3):
        return False, "AUTOMATIC_EPOCH_BUDGET_BOUNDARY_MISMATCH"

    records = state.get("rolloverPostfixQualificationAuthorizations")
    if records is not None and not isinstance(records, list):
        return False, "QUALIFICATION_AUTHORIZATION_LEDGER_INVALID"
    for record in records or []:
        if (isinstance(record, dict) and record.get("transactionId") == transaction_id
                and record.get("failedEpoch") == LEGACY_POSTFIX_QUALIFICATION_FAILED_EPOCH):
            return False, "FAILED_EPOCH_AUTHORIZATION_ALREADY_CONSUMED"

    if (state.get("handoverReady") is True or _validated_pending_handover_exists(state)):
        return False, "VALIDATED_DURABLE_HANDOVER_ALREADY_EXISTS"
    if state.get("rolloverHandoverResponseIdentity") or state.get("rolloverFreshBootstrapPayloadHash"):
        return False, "DURABLE_HANDOVER_EVIDENCE_INCOMPLETE"
    if (state.get("rolloverFreshCandidateConversationId")
            or state.get("rolloverFreshCandidateState") in {"ACK_PENDING", "SUBMISSION_AMBIGUOUS", "READY"}
            or state.get("postDiscussionProtocolRolloverCommittedTransactionId") == transaction_id):
        return False, "FRESH_ARCHITECT_AUTHORITY_ALREADY_ADVANCED"

    state_dir = Path(str(discovery.get("stateDir") or ""))
    expected_prompt = state_dir / "prompts" / f"{task_id}.txt"
    try:
        prompt_path = state.get("nextPromptPath")
        if (not isinstance(prompt_path, str) or Path(prompt_path).resolve() != expected_prompt.resolve()
                or not expected_prompt.is_file()
                or hashlib.sha256(expected_prompt.read_bytes()).hexdigest().upper() != LEGACY_DIAGNOSTIC_RETRY_PROMPT_SHA256):
            return False, "STAGED_PROMPT_IDENTITY_MISMATCH"
        worktrees = state.get("taskWorktrees")
        entry = worktrees.get(task_id) if isinstance(worktrees, dict) else None
        if (not isinstance(entry, dict) or entry.get("taskId") != task_id
                or not entry.get("worktreePath") or not Path(str(entry["worktreePath"])).is_dir()):
            return False, "STAGED_WORKTREE_EVIDENCE_INVALID"
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, "STAGED_PROMPT_IDENTITY_MISMATCH"
    if (state.get("executorSessionId") != LEGACY_DIAGNOSTIC_RETRY_EXECUTOR_SESSION_ID
            or state.get("executorSessionMode") != "PERSISTENT"
            or not discovery.get("executorSessionExists")):
        return False, "PERSISTENT_EXECUTOR_SESSION_MISMATCH"
    alive_by_field = discovery.get("executorPidAliveByField") or {}
    if (bool(state.get("executorActiveWriter")) or bool(state.get("governedExecutorActiveWriter"))
            or discovery.get("activeWriterPresent") or alive_by_field.get("codexPid")
            or alive_by_field.get("active_codex_pid") or discovery.get("executorPidAlive")
            or str(state.get("executorProcessState") or "").upper() in {"RUNNING", "STARTING", "ACTIVE"}):
        return False, "EXECUTOR_PROCESS_OR_WRITER_ACTIVE"
    try:
        ancestry_output = _git(
            Path(str(discovery.get("repository") or "")), "merge-base", "--is-ancestor",
            LEGACY_POSTFIX_QUALIFICATION_FIX_COMMIT, "HEAD",
        )
        if ancestry_output != "":
            return False, "WAITER_FIX_NOT_IN_BRANCH_ANCESTRY"
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        return False, "WAITER_FIX_NOT_IN_BRANCH_ANCESTRY"
    return True, "SAFE_TO_AUTHORIZE_ONE_POSTFIX_QUALIFICATION"


def architect_cdp_health(endpoint: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=2) as response:
            if response.status != 200:
                return False, "ARCHITECT_CDP_UNHEALTHY"
        return True, "ARCHITECT_CDP_HEALTHY"
    except (OSError, urllib.error.URLError):
        return False, "ARCHITECT_CDP_ABSENT"


def probe_architect(endpoint: str, conversation_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Attach read-only through the existing Playwright boundary and close once."""
    from local_orchestrator_watcher import ArchitectPlaywright
    bridge = None
    try:
        bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
        baseline = bridge.assistant_baseline()
        persisted = state.get("architectBaseline")
        newer = architect_baseline_advanced(persisted, baseline)
        observation = {
            "generationVisible": bool(bridge.generation_visible()),
            "newCompletedResponse": newer,
            "durableBaselineValid": valid_architect_baseline(persisted),
            "durableBaselineAdvanced": newer,
            "assistantCount": baseline.get("count"),
        }
        observation["classification"] = classify_architect_observation(state, observation)
        return observation
    finally:
        if bridge is not None:
            bridge.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--classify", action="store_true")
    parser.add_argument("--validate-retry")
    parser.add_argument("--validate-rollover-diagnostic-retry", action="store_true")
    parser.add_argument("--validate-rollover-postfix-qualification", action="store_true")
    parser.add_argument("--probe-architect", action="store_true")
    parser.add_argument("--endpoint", default="http://127.0.0.1:9333")
    args = parser.parse_args(argv)
    try:
        discovery = discover(args.repository, args.state_dir)
        architect = None
        state = discovery["state"]
        if args.probe_architect and state.get("architectConversationId"):
            architect = probe_architect(args.endpoint, str(state["architectConversationId"]), state)
        result = {
            "discovery": {key: value for key, value in discovery.items() if key != "state"},
            "state": state,
            "recoveryClassification": classify_workflow(discovery, architect),
            "architectObservation": architect,
        }
        if args.validate_retry is not None:
            result["retryEligible"], result["retryReason"] = validate_retry(discovery, args.validate_retry)
        if args.validate_rollover_diagnostic_retry:
            result["rolloverDiagnosticRetryEligible"], result["rolloverDiagnosticRetryReason"] = validate_rollover_diagnostic_retry(discovery)
        if args.validate_rollover_postfix_qualification:
            result["rolloverPostfixQualificationEligible"], result["rolloverPostfixQualificationReason"] = validate_rollover_postfix_qualification(discovery)
        print(json.dumps(result, ensure_ascii=True, default=str))
        return 0
    except Exception as error:
        print(json.dumps({"error": type(error).__name__ + ":" + str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
