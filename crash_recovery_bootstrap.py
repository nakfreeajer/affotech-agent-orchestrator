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


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8").strip()


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
        cleanliness = not bool(_git(repo, "status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("REPOSITORY_IDENTITY_UNAVAILABLE") from error
    if not branch or not head:
        raise RuntimeError("REPOSITORY_IDENTITY_INCONSISTENT")
    records = process_records if process_records is not None else _default_process_records()
    session_id = str(state.get("executorSessionId") or "")
    pid = state.get("codexPid") or state.get("active_codex_pid")
    watcher_records = [
        row for row in records
        if "local_orchestrator_watcher.py" in str(row.get("CommandLine") or "")
    ]
    writers = _session_writer_records(session_id, records)
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
        "workingTreeClean": cleanliness,
        "state": state,
        "watcherRunning": bool(watcher_records),
        "watcherProcesses": watcher_records,
        "executorPidAlive": _pid_alive(pid, records),
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
    if observation.get("newCompletedResponse"):
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
        return "SAFE_RESULT_RECOVERY"
    if workflow == "ARCHITECT_RUNNING":
        return classify_architect_observation(state, architect_observation)
    if workflow == "EXECUTOR_RUNNING":
        executor_pid = discovery.get("executorPid")
        owned_writer = any(
            str(row.get("ProcessId")) == str(executor_pid)
            for row in discovery.get("activeWriters", [])
        )
        if discovery["activeWriterPresent"] and not (discovery["executorPidAlive"] and owned_writer):
            return "EXECUTOR_SESSION_ACTIVE_WRITER"
        if discovery["executorPidAlive"]:
            return "WAIT_EXISTING_EXECUTOR"
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
        persisted = state.get("architectBaseline") or {}
        newer = bool(
            isinstance(persisted, dict)
            and (baseline.get("count") != persisted.get("count") or baseline.get("text_hash") != persisted.get("text_hash"))
        )
        observation = {
            "generationVisible": bool(bridge.generation_visible()),
            "newCompletedResponse": newer,
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
        print(json.dumps(result, ensure_ascii=True, default=str))
        return 0
    except Exception as error:
        print(json.dumps({"error": type(error).__name__ + ":" + str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
