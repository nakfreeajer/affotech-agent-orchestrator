"""Minimal local Architect ⇄ Codex watcher for the AFFOTECH workflow."""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

COMPLETE = "ARCHITECT_RESPONSE_COMPLETE"
BEGIN = "EXECUTOR_PROMPT_BEGIN"
END = "EXECUTOR_PROMPT_END"
HANDOVER_BEGIN = "ARCHITECT_HANDOVER_BEGIN"
HANDOVER_END = "ARCHITECT_HANDOVER_END"
HANDOVER_READY = "ARCHITECT_HANDOVER_READY"
READY = "ARCHITECT_SESSION_READY"
DOCUMENTATION_SYNC = "DOCUMENTATION_SYNC_COMPLETE"
RELAY_REPOSITORY = "https://github.com/nakfreeajer/affotech-agent-relay.git"
RELAY_POINTER = "relay/current/LATEST_ARCHITECT_PROMPT.json"
RESULT_SCHEMA_VERSION = "1.0"
ARCHITECT_MEMORY_THRESHOLD_BYTES = 1_073_741_824
ARCHITECT_MEMORY_THRESHOLD_MIB = 1024
AFFOTECH_EXECUTOR_SESSION_ID = "019f842e-98bc-7672-a619-51441d91be00"
VERIFIED_ARCHITECT_CONVERSATION_ID = "6a9d6645-eebc-83ec-8367-d193f1cb18e9"
ARCHITECT_CONVERSATION_URL_RE = re.compile(r"/c/([^/?#]+)")
RUNTIME_LOGGER_NAME = "affotech.orchestrator.runtime"
RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING = "DEFERRED_ARCHITECT_GENERATING"
ARCHITECT_DELIVERY_PROOF_VERSION = "SHA256_MARKER_V1"


def initialize_runtime_logging(state_dir: str | os.PathLike[str], run_id: str | None = None) -> tuple[logging.Logger, str, str]:
    """Initialize the one durable, privacy-safe watcher log before workflow work."""
    state_path = Path(state_dir)
    log_path = state_path / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    logger = logging.getLogger(RUNTIME_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s level=%(levelname)s runId=%(runId)s state=%(state)s taskId=%(taskId)s event=%(event)s %(message)s"))
    logger.addHandler(handler)
    return logger, run_id, str(log_path)


def runtime_log(logger: logging.Logger | None, run_id: str | None, event: str, state: dict[str, Any] | None = None, level: int = logging.INFO, **fields: Any) -> None:
    if logger is None:
        return
    current = state or {}
    safe = {"runId": run_id or "UNKNOWN", "state": current.get("state", "UNKNOWN"), "taskId": current.get("taskId") or current.get("nextTaskId") or "NONE", "event": event}
    safe.update({key: str(value).replace("\n", " ") for key, value in fields.items() if value is not None})
    logger.log(level, " ".join(f"{key}={value}" for key, value in fields.items() if value is not None), extra=safe)


def run_owned_recovery_bridge(bridge: Any, operation: Callable[[Any], Any]) -> Any:
    """Run one recovery operation and always disconnect its owned bridge."""
    try:
        return operation(bridge)
    finally:
        try:
            bridge.close()
        except Exception:
            pass
AFFOTECH_CHILD_PROJECT_DIR = r"C:\Users\nitro\affotech-system-v2-hybrid"
AFFOTECH_CHILD_REMOTE = "https://github.com/nakfreeajer/affotech-system-v2-hybrid.git"
DOCUMENTATION_KINDS = frozenset({"IMPLEMENTATION", "BUG_FIX", "REPAIR", "RECOVERY", "ARCHITECTURE_CHANGE", "GOVERNANCE_CHANGE", "INCIDENT_CLOSURE"})
DOCUMENTATION_REQUIRED = "REQUIRED"
DOCUMENTATION_NONE = "NONE"


class RelayAuthorityError(RuntimeError):
    pass


class ResultSubmissionError(RuntimeError):
    """Typed, fail-closed errors for the Architect result-submit pipeline."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        super().__init__(f"{code}{':' + detail if detail else ''}")


class WatcherInstanceLock:
    """Own one canonical watcher state directory for this process lifetime."""
    def __init__(self, state_dir: str | os.PathLike[str]):
        self.path = Path(state_dir) / "watcher-instance.lock"
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as error:
            handle.close()
            raise RuntimeError("ORCHESTRATOR_ALREADY_RUNNING") from error
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "WatcherInstanceLock":
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()


def verify_executor_session(session_id: str) -> bool:
    """Verify a persistent Codex session from Codex's read-only local index."""
    authority_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    index_path = authority_root / "session_index.jsonl"
    try:
        records = index_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("EXECUTOR_SESSION_AUTHORITY_UNAVAILABLE") from error
    for line in records:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("id") == session_id:
            return True
    raise RuntimeError("EXECUTOR_SESSION_NOT_FOUND")


def documentation_requirement(accepted_record: dict[str, Any]) -> tuple[bool, str | None]:
    """Evaluate structured accepted-state fields only; prose is never inspected."""
    if accepted_record.get("classification") != "ACCEPTED" and accepted_record.get("accepted") is not True:
        return False, None
    override = accepted_record.get("documentationOnAcceptance")
    if override == DOCUMENTATION_NONE:
        return False, None
    if override == DOCUMENTATION_REQUIRED:
        return True, "EXPLICIT_DOCUMENTATION_REQUIRED"
    kind = accepted_record.get("milestoneKind")
    if kind in DOCUMENTATION_KINDS:
        if kind == "INCIDENT_CLOSURE":
            return True, "ACCEPTED_INCIDENT_CLOSURE"
        if kind == "REPAIR":
            return True, "ACCEPTED_REPAIR"
        if kind == "BUG_FIX":
            return True, "ACCEPTED_BUG_FIX"
        if kind == "RECOVERY":
            return True, "ACCEPTED_RECOVERY"
        return True, f"ACCEPTED_{kind}"
    if accepted_record.get("implementationChanged") is True or accepted_record.get("implementationCommit"):
        return True, "ACCEPTED_IMPLEMENTATION_CHANGE"
    if accepted_record.get("problemDetected") is True and accepted_record.get("problemResolved") is True:
        return True, "ACCEPTED_DISCOVERED_AND_RESOLVED_PROBLEM"
    return False, None


class DocumentationDoorbell:
    """Exactly-once Architect doorbell for structured accepted milestones."""
    def __init__(self, watcher: "LocalWatcher"):
        self.watcher = watcher

    def evaluate_and_trigger(self, accepted_record: dict[str, Any], bridge: Any, emit: Callable[[str], None] = print) -> str:
        required, reason = documentation_requirement(accepted_record)
        if not required:
            self.watcher.state["documentationStatus"] = "NOT_REQUIRED"
            self.watcher.save()
            return "NOT_REQUIRED"
        milestone_id = accepted_record.get("milestoneId") or accepted_record.get("milestone")
        publication_id = accepted_record.get("acceptedPublicationId") or accepted_record.get("publicationId")
        if not isinstance(milestone_id, str) or not isinstance(publication_id, str):
            raise RuntimeError("DOCUMENTATION_ACCEPTED_IDENTITY_MISSING")
        key = f"{milestone_id}:{publication_id}"
        if self.watcher.state.get("documentationStatus") in {"TRIGGER_SENT", "CURATOR_PENDING", "COMPLETE"} and self.watcher.state.get("docTriggerKey") == key:
            return self.watcher.state["documentationStatus"]
        message = "\n".join([
            "DOCUMENTATION_SYNC_REQUIRED",
            f"reason={reason}",
            f"milestone={milestone_id}",
            f"acceptedPublication={publication_id}",
            f"implementationCommit={accepted_record.get('implementationCommit') or 'NONE'}",
            f"problemId={accepted_record.get('problemId') or 'NONE'}",
            f"milestoneKind={accepted_record.get('milestoneKind') or 'NONE'}",
            "documentationStatus=PENDING",
            "Issue the bounded Documentation Curator instruction for this accepted milestone.",
            "Do not reopen accepted implementation.",
        ])
        send = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result", None)
        if send is None:
            raise RuntimeError("ARCHITECT_DOORBELL_UNAVAILABLE")
        self.watcher.state["documentationStatus"] = "PENDING"
        self.watcher.state["documentationReason"] = reason
        self.watcher.state["docTriggerKey"] = key
        self.watcher.save()
        send(message)
        self.watcher.state["documentationStatus"] = "TRIGGER_SENT"
        self.watcher.state["documentationTriggerCount"] = int(self.watcher.state.get("documentationTriggerCount", 0)) + 1
        self.watcher.save()
        emit("DOCUMENTATION_SYNC_REQUIRED")
        return "TRIGGER_SENT"


def architect_process_tree_memory_bytes(root_pid: int, process_rows: list[dict[str, Any]] | None = None) -> int:
    """Return working-set bytes for one explicitly governed Windows process tree.

    Ownership is established by the caller-provided root PID; process names are
    never used as an identity heuristic.  The optional rows argument makes the
    aggregation deterministic in tests.
    """
    if not isinstance(root_pid, int) or root_pid <= 0:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE")
    if process_rows is None:
        if os.name != "nt":
            raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE")
        script = ("Get-CimInstance Win32_Process | ForEach-Object { "
                  "$p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
                  "if ($p) { [pscustomobject][ordered]@{ pid=[int]$_.ProcessId; "
                  "parentPid=[int]$_.ParentProcessId; workingSet=[int64]$p.WorkingSet64 } } "
                  "} | ConvertTo-Json -Compress")
        try:
            raw = subprocess.check_output(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], text=True, encoding="utf-8", errors="strict")
            decoded = json.loads(raw)
            process_rows = decoded if isinstance(decoded, list) else [decoded]
        except (OSError, subprocess.CalledProcessError, ValueError, UnicodeError) as error:
            raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED") from error
    if not isinstance(process_rows, list) or not process_rows:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED")
    normalized_rows: list[dict[str, int]] = []
    seen_pids: set[int] = set()
    try:
        for row in process_rows:
            if not isinstance(row, dict):
                raise ValueError("invalid process row")
            pid = int(row["pid"])
            parent_pid = int(row["parentPid"])
            working_set = int(row["workingSet"])
            # Win32 enumeration includes the synthetic System Idle Process;
            # it cannot own a governed browser tree and has no usable PID.
            if pid == 0:
                continue
            if pid < 0 or parent_pid < 0 or working_set < 0 or pid in seen_pids:
                raise ValueError("invalid process row")
            seen_pids.add(pid)
            normalized_rows.append({"pid": pid, "parentPid": parent_pid, "workingSet": working_set})
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED") from error
    if root_pid not in seen_pids:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE")
    process_rows = normalized_rows
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for row in process_rows:
        by_parent.setdefault(int(row["parentPid"]), []).append(row)
    pids = {root_pid}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for row in by_parent.get(parent, []):
            pid = int(row["pid"])
            if pid not in pids:
                pids.add(pid)
                pending.append(pid)
    return sum(row["workingSet"] for row in process_rows if row["pid"] in pids)


def architect_conversation_id_from_url(url: str) -> str:
    """Extract the bare conversation identity from a ChatGPT conversation URL."""
    match = ARCHITECT_CONVERSATION_URL_RE.search(str(url or ""))
    if not match:
        raise RuntimeError("ARCHITECT_CONVERSATION_ID_UNAVAILABLE")
    return match.group(1)


ARCHITECT_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def canonical_architect_conversation_id(identity: str) -> str:
    """Remove only the supported WEB: presentation prefix from UUID IDs."""
    value = str(identity or "")
    suffix = value[4:] if value.startswith("WEB:") else value
    return suffix if ARCHITECT_UUID_RE.fullmatch(suffix) else value


def architect_conversation_ids_equivalent(left: str, right: str) -> bool:
    """Compare Architect IDs exactly, with only WEB:<uuid> compatibility."""
    return str(left or "") == str(right or "") or canonical_architect_conversation_id(left) == canonical_architect_conversation_id(right)


def canonicalize_attached_architect_conversation(watcher: Any, bridge: Any, requested_id: str | None) -> str:
    """Validate an attached page and persist its actual URL representation."""
    page = getattr(bridge, "page", None)
    if page is None:
        return requested_id or ""
    actual_id = architect_conversation_id_from_url(page.url)
    if requested_id and not architect_conversation_ids_equivalent(requested_id, actual_id):
        raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
    if requested_id and requested_id != actual_id:
        watcher.state["architectConversationId"] = actual_id
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_CONVERSATION_ID_CANONICALIZED", watcher.state, **{"from": requested_id, "to": actual_id})
    return actual_id


def legacy_provisional_architect_recovery_allowed(watcher: Any, requested_id: str | None) -> bool:
    """Permit only the narrowly bounded recovery for pre-final-URL state."""
    state = getattr(watcher, "state", {})
    task_id = str(state.get("taskId") or "")
    result_path = state.get("executorResultPath")
    result_exists = False
    if isinstance(result_path, str) and result_path.strip():
        try:
            result_exists = Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            result_exists = False
    active_pid = state.get("codexPid") or state.get("active_codex_pid")
    executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
    return bool(
        isinstance(requested_id, str)
        and requested_id.startswith("WEB:")
        and state.get("state") == "RESULT_READY"
        and task_id
        and task_id == str(state.get("lastCompletedTaskId") or "")
        and result_exists
        and not executor_active
        and state.get("handoverRequested") is False
    )


def attach_legacy_provisional_architect(endpoint: str, requested_id: str, watcher: Any) -> "ArchitectPlaywright":
    """Recover one stale WEB: identity only at the interrupted-result boundary."""
    if not legacy_provisional_architect_recovery_allowed(watcher, requested_id):
        raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
    from playwright.sync_api import sync_playwright

    runtime = sync_playwright().start()
    try:
        browser = runtime.chromium.connect_over_cdp(endpoint, timeout=10000)
        pages = [page for context in browser.contexts for page in context.pages]
        eligible = []
        for page in pages:
            url = str(getattr(page, "url", "") or "")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc.lower() != "chatgpt.com" or not parsed.path.startswith("/c/"):
                continue
            try:
                actual_id = architect_conversation_id_from_url(url)
            except RuntimeError:
                continue
            eligible.append((page, actual_id))
        if len(eligible) != 1:
            raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
        page, actual_id = eligible[0]
        bridge = ArchitectPlaywright(page)
        try:
            entries = bridge._assistant_entries()
        except Exception:
            entries = None
        if entries is not None:
            latest = next((entry.get("text") for entry in reversed(entries) if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry.get("text")), None)
            if latest is None or not architect_handover_ready(latest):
                raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
        stale_rollover_pending = watcher.state.get("rolloverPending") is True
        watcher.state["architectConversationId"] = actual_id
        watcher.state["rolloverPending"] = False
        watcher.state.pop("rolloverTrigger", None)
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_PROVISIONAL_CONVERSATION_RECOVERED", watcher.state, **{"from": requested_id, "to": actual_id, "staleRolloverPendingCleared": stale_rollover_pending})
        bridge._runtime = runtime
        bridge._browser = browser
        return bridge
    except Exception:
        runtime.stop()
        raise


def architect_handover_ready(response: str) -> bool:
    """Recognize only a terminal Architect handover protocol marker."""
    candidate = str(response or "").rstrip()
    if candidate.endswith(COMPLETE):
        candidate = candidate[:-len(COMPLETE)].rstrip()
    return candidate.endswith(HANDOVER_READY) or candidate.endswith(r"ARCHITECT\_HANDOVER\_READY")


def resolve_architect_browser_root_pid(endpoint: str) -> int:
    """Resolve the unique Windows listener owner for the governed CDP endpoint."""
    parts = urlsplit(endpoint)
    port = parts.port
    if port is None or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
    if os.name != "nt":
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
    script = f"Get-NetTCPConnection -State Listen -LocalPort {port} | Select-Object -ExpandProperty OwningProcess"
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True, encoding="utf-8", errors="strict",
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED") from error
    owners = set()
    for line in raw.splitlines():
        value = line.strip()
        if value:
            try:
                pid = int(value)
            except ValueError as error:
                raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED") from error
            if pid > 0:
                owners.add(pid)
    if len(owners) != 1:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
    return next(iter(owners))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_result_envelope(dispatch_id: str, status: str, publication_id: str | None = None, evidence_commit: str | None = None) -> dict[str, Any]:
    if not isinstance(dispatch_id, str) or not dispatch_id or status not in {"PASS", "BLOCKED", "FAILED", "STOP"}:
        raise ValueError("CHILD_RESULT_INVALID")
    if publication_id is not None and not isinstance(publication_id, str):
        raise ValueError("CHILD_RESULT_INVALID")
    return {"schemaVersion": RESULT_SCHEMA_VERSION, "dispatchId": dispatch_id, "resultType": "EXECUTOR_RESULT", "status": status, "publicationId": publication_id, "evidenceCommit": evidence_commit}


def validate_result_envelope(envelope: Any) -> bool:
    return (isinstance(envelope, dict) and envelope.get("schemaVersion") == RESULT_SCHEMA_VERSION
            and envelope.get("resultType") == "EXECUTOR_RESULT"
            and isinstance(envelope.get("dispatchId"), str) and bool(envelope["dispatchId"])
            and envelope.get("status") in {"PASS", "BLOCKED", "FAILED", "STOP"}
            and (envelope.get("publicationId") is None or isinstance(envelope.get("publicationId"), str))
            and (envelope.get("evidenceCommit") is None or isinstance(envelope.get("evidenceCommit"), str)))


def read_result_envelope(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("CHILD_RESULT_MISSING" if isinstance(error, FileNotFoundError) else "CHILD_RESULT_INVALID") from error
    if not validate_result_envelope(envelope):
        raise RuntimeError("CHILD_RESULT_INVALID")
    return envelope


def read_matching_architect_decision(evidence_repo: str | os.PathLike[str], terminal_publication_id: str) -> dict[str, Any] | None:
    """Read the matching Architect decision and accepted pointer from one evidence ref."""
    repo = Path(evidence_repo)
    try:
        subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"], check=True, capture_output=True, text=True)
        ref = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"], text=True, encoding="utf-8").strip()
        names = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref], text=True, encoding="utf-8").splitlines()
        decisions: list[dict[str, Any]] = []
        for name in names:
            if not name.startswith("evidence/architect-decisions/") or not name.endswith("/decision.json"):
                continue
            value = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{name}"]).decode("utf-8"))
            if isinstance(value, dict) and value.get("reviewedPublicationId") == terminal_publication_id:
                decisions.append(value)
        if len(decisions) != 1:
            return None
        pointer = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:evidence/current/LATEST_EXECUTOR_ACCEPTED.json"]).decode("utf-8"))
        if not isinstance(pointer, dict):
            return None
        return {"decision": decisions[0], "acceptedPointer": pointer, "snapshotCommit": ref}
    except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError):
        return None


def read_durable_consumed_relay_key(evidence_repo: str | os.PathLike[str], publication_id: str, content_sha256: str) -> dict[str, Any] | None:
    """Find execution-and-acceptance evidence for one relay publication."""
    repo = Path(evidence_repo)
    try:
        ref = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"], text=True, encoding="utf-8").strip()
        needle = f'"publicationId": "{publication_id}"'
        commits = subprocess.check_output(["git", "-C", str(repo), "log", ref, "--all", "--format=%H", "-S", needle, "--", "evidence/terminal"], text=True, encoding="utf-8").splitlines()
        names = []
        for commit in commits[:20]:
            names.extend(subprocess.check_output(["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit, "--", "evidence/terminal"], text=True, encoding="utf-8").splitlines())
        names = list(dict.fromkeys(names))
        terminal_ids = []
        for result in names:
            name = result.split(":", 1)[1] if ":" in result else result
            if not name.endswith("/terminal.json"):
                continue
            value = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{name}"]).decode("utf-8"))
            context = value.get("authorityContext") if isinstance(value, dict) else None
            execution = value.get("execution") if isinstance(value, dict) else None
            if ((isinstance(context, dict) and context.get("publicationId") == publication_id)
                    or (isinstance(value, dict) and value.get("executedPublicationId") == publication_id)) and isinstance(execution, dict) and execution.get("publicationExecutionCount") == 1:
                terminal_ids.append(Path(name).parent.name)
        if len(terminal_ids) != 1:
            return None
        decision_needle = f'"reviewedPublicationId": "{terminal_ids[0]}"'
        decision_commits = subprocess.check_output(["git", "-C", str(repo), "log", ref, "--all", "--format=%H", "-S", decision_needle, "--", "evidence/architect-decisions"], text=True, encoding="utf-8").splitlines()
        decision_names = []
        for commit in decision_commits[:20]:
            decision_names.extend(subprocess.check_output(["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit, "--", "evidence/architect-decisions"], text=True, encoding="utf-8").splitlines())
        for result in decision_names:
            name = result.split(":", 1)[1] if ":" in result else result
            if not name.endswith("/decision.json"):
                continue
            value = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{name}"]).decode("utf-8"))
            if isinstance(value, dict) and value.get("reviewedPublicationId") == terminal_ids[0] and value.get("decision") == "ACCEPTED":
                return {"publicationId": publication_id, "contentSha256": content_sha256, "terminalPublicationId": terminal_ids[0], "decisionPublicationId": Path(name).parent.name}
        return None
    except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError):
        return None


def reconcile_executor_result(envelope_path: str | os.PathLike[str], current_terminal_publication: str | None = None, publication_exists: Callable[[str], bool] | None = None, advance_pointer: Callable[[str], None] | None = None) -> str:
    """Reconcile machine evidence without inspecting Codex prose."""
    envelope = read_result_envelope(envelope_path)
    publication_id = envelope.get("publicationId")
    if publication_id:
        if publication_exists is not None and not publication_exists(publication_id):
            return "CHILD_PUBLICATION_MISSING"
        if current_terminal_publication != publication_id:
            if advance_pointer is None:
                return "POINTER_STALE"
            advance_pointer(publication_id)
            return "POINTER_RECONCILED"
    return envelope["status"]


class RelayPromptSource:
    """Read one immutable relay publication from a captured Git ref."""
    def __init__(self, cache_dir: str | os.PathLike[str], remote: str = RELAY_REPOSITORY, refresh: bool = True):
        self.cache_dir = Path(cache_dir)
        self.remote = remote
        self.refresh_enabled = refresh
        self.captured_ref: str | None = None

    def _git(self, *args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(self.cache_dir), *args], text=True, encoding="utf-8").strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise RelayAuthorityError(f"RELAY_GIT_UNAVAILABLE:{error}") from error

    def refresh(self) -> str:
        if not (self.cache_dir / ".git").exists():
            self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", self.remote, str(self.cache_dir)], check=True, capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError) as error:
                raise RelayAuthorityError(f"RELAY_CLONE_FAILED:{error}") from error
        try:
            subprocess.run(["git", "-C", str(self.cache_dir), "fetch", "--quiet", "origin", "main"], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RelayAuthorityError(f"RELAY_FETCH_FAILED:{error}") from error
        # Pin every authority read to the remote-tracking ref resolved after
        # fetch.  FETCH_HEAD is mutable fetch bookkeeping and may be stale or
        # refer to a different fetch in a shared cache.
        self.captured_ref = self._git("rev-parse", "refs/remotes/origin/main")
        return self.captured_ref

    def _show_bytes(self, path: str) -> bytes:
        if not self.captured_ref:
            raise RelayAuthorityError("RELAY_REF_NOT_CAPTURED")
        try:
            return subprocess.check_output(["git", "-C", str(self.cache_dir), "show", f"{self.captured_ref}:{path}"])
        except (OSError, subprocess.CalledProcessError) as error:
            raise RelayAuthorityError(f"RELAY_OBJECT_INVALID:{path}") from error

    def _show_json(self, path: str) -> dict[str, Any]:
        try:
            value = json.loads(self._show_bytes(path).decode("utf-8"))
        except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError) as error:
            raise RelayAuthorityError(f"RELAY_OBJECT_INVALID:{path}") from error
        if not isinstance(value, dict):
            raise RelayAuthorityError(f"RELAY_OBJECT_NOT_OBJECT:{path}")
        return value

    def read_current(self) -> dict[str, Any]:
        if self.refresh_enabled:
            self.refresh()
        pointer = self._show_json(RELAY_POINTER)
        publication_id = pointer.get("publicationId")
        pointer_hash = pointer.get("contentSha256")
        if not isinstance(publication_id, str) or not isinstance(pointer_hash, str):
            raise RelayAuthorityError("RELAY_POINTER_INVALID")
        manifest_path = f"relay/architect/prompts/{publication_id}/manifest.json"
        manifest = self._show_json(manifest_path)
        if manifest.get("protocolVersion") != "1.0":
            raise RelayAuthorityError("RELAY_PROTOCOL_UNSUPPORTED")
        if manifest.get("publicationId") != publication_id:
            raise RelayAuthorityError("RELAY_PUBLICATION_ID_MISMATCH")
        if manifest.get("contentSha256") != pointer_hash:
            raise RelayAuthorityError("RELAY_POINTER_MANIFEST_HASH_MISMATCH")
        for key, expected in (("recipientRole", "EXECUTOR"), ("status", "READY_FOR_EXECUTION"), ("executionTarget", "WINDOWS_LOCAL_CODEX")):
            if manifest.get(key) != expected:
                raise RelayAuthorityError(f"RELAY_{key.upper()}_INVALID")
        if not isinstance(manifest.get("requiredInvariantSetId"), str) or not manifest["requiredInvariantSetId"]:
            raise RelayAuthorityError("RELAY_REQUIRED_INVARIANT_SET_INVALID")
        if not isinstance(manifest.get("requiredInvariantContentSha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["requiredInvariantContentSha256"]):
            raise RelayAuthorityError("RELAY_REQUIRED_INVARIANT_HASH_INVALID")
        prompt = manifest.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise RelayAuthorityError("RELAY_PROMPT_EMPTY")
        prompt_bytes = prompt.encode("utf-8")
        if hashlib.sha256(prompt_bytes).hexdigest() != pointer_hash:
            raise RelayAuthorityError("RELAY_CONTENT_HASH_MISMATCH")
        # Real relay publications carry prompt.md as the immutable prompt
        # artifact.  Compare its raw bytes to the decoded manifest prompt,
        # while allowing lightweight unit doubles without a Git object store.
        if (self.cache_dir / ".git").exists():
            try:
                published_prompt_bytes = self._show_bytes(manifest_path.rsplit("/", 1)[0] + "/prompt.md")
            except RelayAuthorityError as error:
                if "prompt.md" in str(error):
                    raise RelayAuthorityError("RELAY_PROMPT_ARTIFACT_MISSING") from error
                raise
            if published_prompt_bytes != prompt_bytes:
                raise RelayAuthorityError("RELAY_PROMPT_ARTIFACT_MISMATCH")
        return {"snapshotCommit": self.captured_ref, "publicationId": publication_id, "contentSha256": pointer_hash,
                "promptBytes": prompt_bytes, "prompt": prompt, "manifest": manifest}

    def legacy_supersession_proof(self, publication_id: str) -> dict[str, Any] | None:
        """Return safe, snapshot-pinned proof that a legacy prompt was superseded."""
        if not self.captured_ref:
            raise RelayAuthorityError("RELAY_REF_NOT_CAPTURED")
        try:
            current = self._show_json(RELAY_POINTER).get("publicationId")
            if current == publication_id:
                return None
            needle = publication_id
            names = subprocess.check_output(["git", "-C", str(self.cache_dir), "grep", "-l", "-F", "--", needle, self.captured_ref, "--", "relay/architect/decisions"], text=True, encoding="utf-8").splitlines()
            matches = []
            for result in names:
                name = result.split(":", 1)[1] if ":" in result else result
                if not name.endswith("/decision.json"):
                    continue
                value = json.loads(self._show_bytes(name).decode("utf-8"))
                if (value.get("decision") == "SUPERSEDED"
                        and (value.get("supersededPublicationId") == publication_id or value.get("parentPublicationId") == publication_id)):
                    decision_dir = Path(name).parent.as_posix()
                    manifest = self._show_json(decision_dir + "/manifest.json")
                    replacement = value.get("replacementPublicationId") or manifest.get("replacementPublicationId")
                    if isinstance(replacement, str) and replacement:
                        matches.append({"decisionPublicationId": Path(name).parent.name, "replacementPublicationId": replacement, "reason": value.get("reason"), "currentPublicationId": current, "snapshotCommit": self.captured_ref})
            return matches[0] if len(matches) == 1 else None
        except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError):
            return None


def normalize_prompt(text: str) -> str:
    return " ".join(text.strip().split())


def relay_task_key(publication_id: str, content_sha256: str) -> str:
    if not isinstance(publication_id, str) or not isinstance(content_sha256, str):
        raise ValueError("RELAY_KEY_INVALID")
    return f"{publication_id}:{content_sha256}"


def result_submission_key(publication_id: str, result_text: str) -> str:
    if not isinstance(publication_id, str) or not publication_id or not isinstance(result_text, str):
        raise ValueError("RESULT_SUBMISSION_KEY_INVALID")
    return f"{publication_id}:{hashlib.sha256(result_text.encode('utf-8')).hexdigest()}"


def publish_durable_executor_terminal(evidence_repo: str | os.PathLike[str], relay_publication_id: str, result_text: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Publish one immutable terminal and advance the governed terminal pointer."""
    root = Path(evidence_repo).resolve()
    result_sha = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
    publication_id = f"GH-PUB-{hashlib.sha256((relay_publication_id + ':' + result_sha).encode()).hexdigest()[:32]}-EXECUTOR-TERMINAL"
    terminal_dir = root / "evidence" / "terminal" / "executor" / publication_id
    terminal_dir.mkdir(parents=True, exist_ok=True)
    terminal = {"schemaVersion": "1.0", "recordType": "EXECUTOR_TERMINAL", "publicationId": publication_id, "executedRelayPublicationId": relay_publication_id, "resultSha256": result_sha, "status": envelope["status"], "machineResultEnvelope": envelope}
    receipt = {"schemaVersion": "1.0", "recordType": "EXECUTOR_RECEIPT", "publicationId": publication_id, "executedRelayPublicationId": relay_publication_id, "resultSha256": result_sha, "terminalDurable": True}
    files = {"terminal.json": stable_json(terminal).encode("utf-8"), "report.md": result_text.encode("utf-8"), "receipt.json": stable_json(receipt).encode("utf-8")}
    for name, data in files.items():
        path = terminal_dir / name
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise RuntimeError("DURABLE_TERMINAL_IMMUTABLE_COLLISION")
        if path.read_bytes() != data:
            raise RuntimeError("DURABLE_TERMINAL_READBACK_FAILED")
    pointer = {"schemaVersion": "1.0", "pointerKind": "LATEST_EXECUTOR_TERMINAL", "publicationId": publication_id, "terminalPath": f"evidence/terminal/executor/{publication_id}/terminal.json", "reportPath": f"evidence/terminal/executor/{publication_id}/report.md", "receiptPath": f"evidence/terminal/executor/{publication_id}/receipt.json", "status": envelope["status"], "executedRelayPublicationId": relay_publication_id, "resultSha256": result_sha}
    current_dir = root / "evidence" / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = current_dir / "LATEST_EXECUTOR_TERMINAL.json"
    pointer_data = stable_json(pointer).encode("utf-8")
    if pointer_path.exists() and pointer_path.read_bytes() != pointer_data:
        temp = pointer_path.with_name(pointer_path.name + ".pending")
        try:
            with temp.open("xb") as handle:
                handle.write(pointer_data)
            os.replace(temp, pointer_path)
        finally:
            temp.unlink(missing_ok=True)
    elif not pointer_path.exists():
        temp = pointer_path.with_name(pointer_path.name + ".pending")
        try:
            with temp.open("xb") as handle:
                handle.write(pointer_data)
            os.replace(temp, pointer_path)
        finally:
            temp.unlink(missing_ok=True)
    if pointer_path.read_bytes() != pointer_data:
        raise RuntimeError("DURABLE_TERMINAL_POINTER_READBACK_FAILED")
    return {"publicationId": publication_id, "pointerPath": str(pointer_path), "resultSha256": result_sha}


def verify_child_project_binding(child_cwd: str | os.PathLike[str], expected_remote: str = AFFOTECH_CHILD_REMOTE) -> dict[str, Any]:
    """Read-only project identity gate for the AFFOTECH child boundary."""
    path = Path(child_cwd).resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise RuntimeError("CODEX_CHILD_PROJECT_NOT_A_GIT_REPOSITORY")
    try:
        root = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True, encoding="utf-8", errors="strict").strip()
        remotes = subprocess.check_output(["git", "-C", str(path), "remote", "-v"], text=True, encoding="utf-8", errors="strict")
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise RuntimeError("CODEX_CHILD_PROJECT_IDENTITY_UNAVAILABLE") from error
    if Path(root).resolve() != path or expected_remote not in remotes:
        raise RuntimeError("CODEX_CHILD_PROJECT_IDENTITY_MISMATCH")
    return {"childCwd": str(path), "repositoryIdentity": expected_remote, "sandbox": "read-only", "writableBoundary": str(path)}


def choose_conversation_scroll_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose a real conversation viewport, rejecting incidental overflow."""
    eligible = [
        item for item in candidates
        if item.get("scrollHeight", 0) - item.get("clientHeight", 0) >= max(200, item.get("clientHeight", 0) * 0.25)
        and item.get("clientHeight", 0) >= 400
    ]
    if not eligible:
        return None
    def score(item: dict[str, Any]) -> tuple[int, int, int, int]:
        class_summary = str(item.get("classSummary", ""))
        root_signal = 1 if "scroll-root" in class_summary else 0
        active_signal = 1 if item.get("scrollTop", 0) > 0 else 0
        scroll_range = int(item.get("scrollHeight", 0) - item.get("clientHeight", 0))
        return (root_signal, active_signal, min(scroll_range, 100000), int(item.get("clientHeight", 0)))
    return max(eligible, key=score)


def extract_executor_prompt(response: str) -> str | None:
    if not response.rstrip().endswith(COMPLETE):
        return None
    return extract_executor_prompt_envelope(response)


def extract_executor_prompt_envelope(response: str) -> str | None:
    """Extract exactly one structurally valid Executor envelope, marker optional."""
    if response.count(BEGIN) != 1 or response.count(END) != 1:
        return None
    begin = response.find(BEGIN)
    end = response.find(END)
    if begin < 0 or end < begin:
        return None
    match = re.search(rf"{re.escape(BEGIN)}\s*\n?(.*?){re.escape(END)}", response[begin:], re.DOTALL)
    return match.group(1).strip() if match else None


def architect_response_finished(response: str, generation_control_visible: bool = False) -> bool:
    return not generation_control_visible and response.rstrip().endswith(COMPLETE)


def extract_handover(response: str) -> str | None:
    if not response.rstrip().endswith(COMPLETE):
        return None
    match = re.search(rf"{re.escape(HANDOVER_BEGIN)}\s*\n(.*?){re.escape(HANDOVER_END)}", response, re.DOTALL)
    return match.group(1).strip() if match else None


STANDARD_HANDOVER_REQUEST = """ARCHITECT SESSION ROLLOVER

This Architect conversation has reached the configured response limit.

Prepare a complete handover prompt for a fresh ChatGPT Architect conversation.

Preserve only the current authoritative working state required to continue safely, including:

* Architect role and authority model
* project/repository/branch identity
* current accepted baseline
* current milestone and Executor activity
* latest verified results
* unresolved blockers
* permanent non-regression rules
* important recent user corrections and lessons
* what must NOT be repeated or reopened
* browser/session/port state if relevant
* authoritative evidence pointers
* exact next expected Architect action

Do not perform new project work. Do not issue another Executor milestone.
Do not summarize obsolete history unless necessary to prevent regression.

The output itself must be directly usable as the bootstrap prompt for the new Architect conversation.

End with:

ARCHITECT_HANDOVER_READY"""


class ArchitectSessionRollover:
    """Small crash-safe state machine with memory-first rollover policy."""
    def __init__(self, watcher: "LocalWatcher"):
        self.watcher = watcher
        self._last_logged_memory_bytes: int | None = None
        self._last_sampled_memory_bytes: int | None = None
        self._last_memory_error: str | None = None

    def initialize_current_session(self) -> None:
        self.watcher.state["architectResponseCount"] = 0
        self.watcher.state["handoverRequested"] = False
        self.watcher.state["handoverReady"] = False
        self.watcher.state["rolloverPending"] = False
        self.watcher.state.pop("rolloverTrigger", None)
        self.watcher.save()

    def sample_memory(self, memory_reader: Callable[[], int] | None = None, emit: Callable[[str], None] = print) -> str | None:
        """Sample only the explicitly governed Architect process tree."""
        reader = memory_reader or getattr(self.watcher, "architect_memory_reader", None)
        if reader is None:
            if getattr(self.watcher, "memory_ownership_required", False) and self.watcher.state.get("architectMemoryOwnershipWarningEmitted") is not True:
                self.watcher.state["architectMemoryOwnership"] = "UNCONFIGURED"
                self.watcher.state["architectMemoryError"] = "ARCHITECT_BROWSER_ROOT_PID_REQUIRED"
                self.watcher.state["architectMemoryOwnershipWarningEmitted"] = True
                self.watcher.save()
                emit("ARCHITECT_MEMORY_OWNERSHIP_REQUIRED")
            return None
        try:
            memory_bytes = reader()
            if not isinstance(memory_bytes, int) or memory_bytes < 0:
                raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_INVALID")
        except Exception as error:
            candidate = str(error)
            reason = candidate if re.fullmatch(r"[A-Z0-9_]+", candidate) else f"ARCHITECT_MEMORY_SAMPLE_{type(error).__name__.upper()}"
            self.watcher.state["architectMemoryOwnership"] = "INCONCLUSIVE"
            self.watcher.state["architectMemoryError"] = reason
            self.watcher.save()
            if reason != self._last_memory_error:
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_FAILED", self.watcher.state, error=reason)
                self._last_memory_error = reason
            return None
        recovering_from_error = self._last_memory_error is not None
        self._last_memory_error = None
        self.watcher.state["architectMemoryBytes"] = memory_bytes
        self.watcher.state["architectMemoryMiB"] = round(memory_bytes / (1024 * 1024), 2)
        memory_mib = self.watcher.state["architectMemoryMiB"]
        if (self._last_logged_memory_bytes is None
                or recovering_from_error
                or abs(memory_bytes - self._last_logged_memory_bytes) >= 64 * 1024 * 1024):
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE", self.watcher.state, memoryMiB=memory_mib, thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB)
            self._last_logged_memory_bytes = memory_bytes
        previous_memory_bytes = self._last_sampled_memory_bytes
        self._last_sampled_memory_bytes = memory_bytes
        trigger = self.rollover_trigger(memory_bytes, int(self.watcher.state.get("architectResponseCount", 0)))
        if memory_bytes >= ARCHITECT_MEMORY_THRESHOLD_BYTES and (previous_memory_bytes is None or previous_memory_bytes < ARCHITECT_MEMORY_THRESHOLD_BYTES):
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_THRESHOLD", self.watcher.state, memoryMiB=memory_mib, thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB, trigger="MEMORY_THRESHOLD")
        if trigger and not self.watcher.state.get("rolloverDue"):
            self.watcher.state["rolloverDue"] = True
            self.watcher.state["rolloverTrigger"] = trigger
            self.watcher.save()
            emit(f"ROLLOVER_DUE trigger={trigger}")
        return trigger

    @staticmethod
    def rollover_trigger(memory_bytes: int, response_count: int = 0) -> str | None:
        if memory_bytes >= ARCHITECT_MEMORY_THRESHOLD_BYTES:
            return "MEMORY_THRESHOLD"
        if response_count >= 30:
            return "RESPONSE_COUNT_FALLBACK"
        return None

    def observe_complete_response(self, response: str, response_id: str | None = None) -> bool:
        if self.watcher.state.get("handoverRequested") or not architect_response_finished(response):
            return False
        identity = response_id or hashlib.sha256(response.encode("utf-8")).hexdigest()
        if identity == self.watcher.state.get("lastArchitectResponseIdentity"):
            return False
        self.watcher.state["lastArchitectResponseIdentity"] = identity
        self.watcher.state["architectResponseCount"] = int(self.watcher.state.get("architectResponseCount", 0)) + 1
        self.watcher.save()
        return True

    def request_if_due(self, bridge: "ArchitectPlaywright", latest_prompt_dispatched: bool, executor_running: bool, emit: Callable[[str], None] = print, architect_generating: bool = False, safe_boundary_state: str | None = None) -> bool:
        workflow_state = self.watcher.state.get("state", "IDLE")
        boundary_state = safe_boundary_state or workflow_state
        if boundary_state == "IDLE":
            if any(self.watcher.state.get(key) for key in ("taskId", "executorResultPath", "architectDeliveryPayloadHash", "handoverRequested")):
                return False
        elif boundary_state not in {"EXECUTOR_RUNNING", "NEXT_PROMPT_READY"}:
            return False
        if boundary_state == "EXECUTOR_RUNNING" and self.watcher.state.get("architectSendState") in {"PENDING", "AMBIGUOUS", "FAILED"}:
            return False
        if boundary_state == "NEXT_PROMPT_READY":
            prompt_path = self.watcher.state.get("nextPromptPath")
            next_task = self.watcher.state.get("nextTaskId")
            if not next_task or not isinstance(prompt_path, str) or not Path(prompt_path).is_file() or self.watcher.state.get("handoverRequested"):
                return False
        count = int(self.watcher.state.get("architectResponseCount", 0))
        memory_bytes = int(self.watcher.state.get("architectMemoryBytes", 0))
        trigger = self.watcher.state.get("rolloverTrigger") or self.rollover_trigger(memory_bytes, count)
        if not trigger:
            return False
        task_id = str(self.watcher.state.get("taskId") or "")
        if task_id and self.watcher.state.get("rolloverAttemptedForTaskId") == task_id:
            return False
        if architect_generating or not latest_prompt_dispatched or not executor_running or self.watcher.state.get("handoverRequested"):
            return False
        if task_id:
            self.watcher.state["rolloverAttemptedForTaskId"] = task_id
        self.watcher.state["rolloverDue"] = True
        self.watcher.state["rolloverInProgress"] = True
        self.watcher.state["rolloverPending"] = True
        self.watcher.state["rolloverTrigger"] = trigger
        self.watcher.state["handoverRequested"] = True
        self.watcher.state["handoverReady"] = False
        self.watcher.save()
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ROLLOVER_PENDING", self.watcher.state, trigger=trigger)
        try:
            bridge.submit_result_bounded(STANDARD_HANDOVER_REQUEST)
            emit("ARCHITECT_HANDOVER_REQUESTED")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_HANDOVER_REQUESTED", self.watcher.state)
            return True
        except Exception:
            self.watcher.state["rolloverInProgress"] = False
            self.watcher.state["handoverRequested"] = False
            self.watcher.state["handoverReady"] = False
            self.watcher.state["rolloverDue"] = True
            self.watcher.save()
            emit("STATE=ROLLOVER_PENDING")
            return False

    def complete_from_response(self, bridge: "ArchitectPlaywright", response: str, emit: Callable[[str], None] = print) -> bool:
        if not self.watcher.state.get("handoverRequested") or not architect_handover_ready(response):
            return False
        self.watcher.state["handoverReady"] = True
        self.watcher.state["pending_handover"] = response
        self.watcher.save()
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_HANDOVER_READY", self.watcher.state)
        old_page = bridge.page
        new_page = None
        committed = False
        phase = "OPEN_FRESH_WITH_HANDOVER"
        workflow_snapshot = {
            key: self.watcher.state.get(key)
            for key in ("state", "taskId", "lastCompletedTaskId", "executorResultPath", "humanRequiredReason", "architectSendState", "architectDeliveryFailureClass")
        }
        self.watcher.state["rolloverInProgress"] = True
        try:
            new_page = bridge.open_fresh_with_handover(response)
            phase = "WAIT_NEW_CONVERSATION_ID"
            deadline = time.monotonic() + 15.0
            conversation_id = None
            while time.monotonic() < deadline:
                current_url = getattr(new_page, "url", "")
                current_url = current_url() if callable(current_url) else current_url
                try:
                    conversation_id = architect_conversation_id_from_url(current_url)
                    break
                except RuntimeError:
                    time.sleep(0.1)
            if conversation_id is None:
                raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ID_TIMEOUT")
            phase = "WAIT_NEW_CONVERSATION_ACK"
            ack_deadline = time.monotonic() + 30.0
            new_bridge = ArchitectPlaywright(new_page)
            while time.monotonic() < ack_deadline:
                if new_bridge.generation_visible():
                    time.sleep(0.25)
                    continue
                entries = new_bridge._assistant_entries()
                if entries:
                    latest = entries[-1].get("text", "") if isinstance(entries[-1], dict) else ""
                    if architect_handover_ready(latest):
                        break
                    if latest.strip():
                        raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ACK_INVALID")
                time.sleep(0.25)
            else:
                raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ACK_TIMEOUT")
            final_url = getattr(new_page, "url", "")
            final_url = final_url() if callable(final_url) else final_url
            conversation_id = architect_conversation_id_from_url(final_url)
            phase = "COMMIT_NEW_CONVERSATION_AUTHORITY"
            self.watcher.state["architectConversationId"] = conversation_id
            self.watcher.state.pop("currentArchitectConversationId", None)
            self.watcher.state["architectResponseCount"] = 0
            self.watcher.state["handoverRequested"] = False
            self.watcher.state["handoverReady"] = False
            self.watcher.state["rolloverPending"] = False
            self.watcher.state["rolloverDue"] = False
            self.watcher.state["rolloverInProgress"] = False
            self.watcher.state.pop("rolloverAttemptedForTaskId", None)
            self.watcher.state.pop("rolloverTrigger", None)
            self.watcher.state.pop("pending_handover", None)
            self.watcher.save()
            committed = True
            bridge.page = new_page
            if hasattr(old_page, "close"):
                try:
                    old_page.close()
                except Exception:
                    pass
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_NEW_CONVERSATION_CREATED", self.watcher.state, conversationId=conversation_id)
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_CONVERSATION_SWITCHED", self.watcher.state, conversationId=conversation_id)
            emit("ARCHITECT_SESSION_ROLLOVER_COMPLETE")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_SESSION_ROLLOVER_COMPLETE", self.watcher.state)
            return True
        except Exception as error:
            if not committed and new_page is not None and new_page is not old_page and hasattr(new_page, "close"):
                try:
                    new_page.close()
                except Exception:
                    pass
            for key, value in workflow_snapshot.items():
                if value is None:
                    self.watcher.state.pop(key, None)
                else:
                    self.watcher.state[key] = value
            self.watcher.state["rolloverInProgress"] = False
            self.watcher.state["rolloverDue"] = True
            self.watcher.state["rolloverPending"] = True
            self.watcher.state["handoverReady"] = False
            self.watcher.save()
            candidate = str(error)
            code = candidate if re.fullmatch(r"[A-Z0-9_:]+", candidate) else "ARCHITECT_SESSION_ROLLOVER_FAILED"
            error_message = candidate.replace("\r", " ").replace("\n", " ")[:500]
            emit(f"{code} phase={phase} errorClass={type(error).__name__}")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_SESSION_ROLLOVER_FAILED", self.watcher.state, error=code, errorClass=type(error).__name__, errorMessage=error_message, phase=phase)
            emit("STATE=ROLLOVER_PENDING")
            return False


class LoopGuard:
    def __init__(self, state: dict[str, Any] | None = None):
        self.last_prompt_hash = (state or {}).get("last_prompt_hash")
        self.last_result_hash = (state or {}).get("last_result_hash")

    def check(self, prompt: str, milestone: str | None = None, blocked: bool = False) -> str:
        prompt_hash = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        if self.last_prompt_hash == prompt_hash and (blocked or self.last_result_hash is None):
            return "LOOP_SUSPECTED"
        self.last_prompt_hash = prompt_hash
        return "FORWARD"

    def record_result(self, result: str) -> None:
        self.last_result_hash = hashlib.sha256(result.encode()).hexdigest()


@dataclass
class CodexResult:
    state: str
    output: str
    exit_code: int | None
    timed_out: bool
    stdout: str = ""
    stderr: str = ""
    last_message_path: str | None = None
    last_message_exists: bool = False

    @property
    def command_form(self) -> str:
        return "codex exec --ephemeral --sandbox read-only -C <project> -o <unique-last-message-file> -"


class CodexRunner:
    def __init__(self, project_dir: str | os.PathLike[str], executable: str = "codex", bootstrap_path: str | os.PathLike[str] | None = None, child_project_dir: str | os.PathLike[str] | None = None, child_identity_verifier: Callable[[str], dict[str, Any]] | None = None, session_id: str | None = None):
        self.project_dir = str(project_dir)
        self.child_project_dir = str(child_project_dir) if child_project_dir else None
        self.child_identity_verifier = child_identity_verifier or verify_child_project_binding
        self.session_id = session_id
        self.executable = executable
        self.bootstrap_path = Path(bootstrap_path) if bootstrap_path else Path(self.project_dir) / "AFFOTECH_EXECUTOR_BOOTSTRAP.md"
        self.launcher = discover_codex_launcher(executable)
        self.running_observed = False
        self.last_pid: int | None = None
        self.on_start: Callable[[int], None] | None = None
        self.lifecycle_state = "CLOSED"
        self.active_child_pid: int | None = None
        self.relay_authority: dict[str, Any] | None = None

    def run(self, prompt: str, timeout: float = 300.0) -> CodexResult:
        """Run until the child exits; ``timeout`` is retained for API compatibility.

        Executor duration is deliberately not governed by a wall-clock deadline.
        Callers may use short timers for transport/polling, but only child exit
        evidence determines executor completion.
        """
        assembled_prompt = self.assemble_prompt(prompt, self.relay_authority)
        try:
            assembled_prompt_bytes = assembled_prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise RuntimeError("CODEX_PROMPT_UTF8_ENCODE_FAILED") from error
        handle, last_message_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
        os.close(handle)
        try:
            os.unlink(last_message_path)
        except FileNotFoundError:
            pass
        if self.session_id:
            verify_executor_session(self.session_id)
            args = ["exec", "resume", self.session_id, "-o", last_message_path, "-"]
        else:
            args = ["exec", "--ephemeral", "--sandbox", "read-only", "-C", self.project_dir, "-o", last_message_path, "-"]
        if os.name == "nt" and self.launcher[0].lower().endswith(".ps1"):
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.executable, *args]
        else:
            command = [*self.launcher, *args]
        child_cwd = self.child_project_dir or self.project_dir
        if self.child_project_dir:
            self.child_identity = self.child_identity_verifier(child_cwd)
            if "-C" in command:
                command[command.index("-C") + 1] = child_cwd
        if self.lifecycle_state != "CLOSED" or self.running_observed:
            raise RuntimeError("CODEX_CHILD_LIFECYCLE_NOT_CLOSED")
        self.lifecycle_state = "STARTING"
        if self._use_visible_windows_console():
            try:
                return self._run_visible_windows_console(assembled_prompt, timeout, command, last_message_path, child_cwd)
            finally:
                self.lifecycle_state = "CLOSED"
                self.running_observed = False
                self.active_child_pid = None
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict", cwd=child_cwd)
        self.last_pid = process.pid
        self.active_child_pid = process.pid
        if self.on_start:
            self.on_start(process.pid)
        self.running_observed = process.poll() is None
        stdout, stderr = process.communicate(input=assembled_prompt)
        returncode = process.wait()
        if not isinstance(returncode, int):
            raise RuntimeError("CODEX_RETURN_CODE_NOT_INTEGER")
        completed = type("Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
        self.lifecycle_state = "CLOSED"
        self.running_observed = False
        self.active_child_pid = None
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        last_message = Path(last_message_path).read_text(encoding="utf-8") if os.path.exists(last_message_path) else ""
        output = last_message.strip() or stdout.strip() or stderr.strip()
        if completed.returncode != 0:
            return CodexResult("BLOCKED", output, completed.returncode, False, stdout, stderr, last_message_path, bool(last_message))
        return CodexResult("COMPLETED" if last_message.strip() else "FAILED", output, completed.returncode, False, stdout, stderr, last_message_path, bool(last_message))

    def _use_visible_windows_console(self) -> bool:
        """Use the visible host only for an actual production subprocess call."""
        return (
            os.name == "nt"
            and self.executable == "codex"
            and os.environ.get("AFFOTECH_VISIBLE_EXECUTOR", "1") != "0"
            and getattr(subprocess.Popen, "__module__", "subprocess") == "subprocess"
        )

    def _run_visible_windows_console(self, assembled_prompt: str, timeout: float, command: list[str], last_message_path: str, child_cwd: str | None = None) -> CodexResult:
        """Run the real Codex child in a new visible console and poll its status."""
        try:
            assembled_prompt_bytes = assembled_prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise RuntimeError("CODEX_PROMPT_UTF8_ENCODE_FAILED") from error
        status_handle, status_path = tempfile.mkstemp(prefix="codex-visible-status-", suffix=".json")
        os.close(status_handle)
        Path(status_path).write_text("{}", encoding="utf-8")
        launcher_handle, launcher_path = tempfile.mkstemp(prefix="codex-visible-console-", suffix=".js")
        os.close(launcher_handle)
        launcher = r'''const fs = require("fs");
const { spawn } = require("child_process");
const statusPath = process.argv[2];
const command = process.argv.slice(3);
const write = (value) => fs.writeFileSync(statusPath, JSON.stringify(value));
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  console.log("==================================================");
  console.log("AFFOTECH AUTOMATED EXECUTOR");
  console.log("==================================================");
  console.log("Prompt received from Architect");
  console.log("Executor is running");
  console.log("Do not close this window while execution is active");
  console.log("==================================================");
  const child = spawn(command[0], command.slice(1), { stdio: ["pipe", "inherit", "inherit"] });
  write({ phase: "STARTED", pid: child.pid });
  child.stdin.end(input);
  child.on("close", (code) => {
    write({ phase: "FINISHED", pid: child.pid, exitCode: code });
    console.log("==================================================");
    console.log("EXECUTOR FINISHED");
    console.log("Exit code: " + code);
    console.log("Result returned to Architect");
    console.log("==================================================");
    console.log("Executor console closing after result capture.");
  });
});
'''
        with open(launcher_path, "w", encoding="utf-8", newline="\n") as launcher_file:
            launcher_file.write(launcher)
        host_command = [self.launcher[0], launcher_path, status_path, *command]
        try:
            host = subprocess.Popen(
                host_command,
                stdin=subprocess.PIPE,
                stdout=None,
                stderr=None,
                text=False,
                cwd=child_cwd or self.project_dir,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            if host.stdin is None:
                raise RuntimeError("VISIBLE_EXECUTOR_STDIN_UNAVAILABLE")
            host.stdin.write(assembled_prompt_bytes)
            host.stdin.close()
            status: dict[str, Any] = {}
            while True:
                try:
                    candidate = json.loads(Path(status_path).read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        status = candidate
                except (OSError, json.JSONDecodeError):
                    pass
                if status.get("phase") == "STARTED" and isinstance(status.get("pid"), int):
                    self.last_pid = status["pid"]
                    if self.on_start:
                        self.on_start(status["pid"])
                        self.on_start = None
                if status.get("phase") == "FINISHED":
                    exit_code = status.get("exitCode")
                    if not isinstance(exit_code, int):
                        raise RuntimeError("CODEX_RETURN_CODE_NOT_INTEGER")
                    break
                time.sleep(0.05)
            stdout = ""
            stderr = ""
            exit_code = int(status["exitCode"])
        finally:
            try:
                os.unlink(launcher_path)
            except FileNotFoundError:
                pass
            try:
                os.unlink(status_path)
            except FileNotFoundError:
                pass
        last_message = Path(last_message_path).read_text(encoding="utf-8") if os.path.exists(last_message_path) else ""
        output = last_message.strip() or stderr.strip()
        if exit_code != 0:
            return CodexResult("BLOCKED", output, exit_code, False, stdout, stderr, last_message_path, bool(last_message))
        return CodexResult("COMPLETED" if last_message.strip() else "FAILED", output, exit_code, False, stdout, stderr, last_message_path, bool(last_message))

    def assemble_prompt(self, task_prompt: str, relay_authority: dict[str, Any] | None = None) -> str:
        try:
            bootstrap = self.bootstrap_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"AFFOTECH_EXECUTOR_BOOTSTRAP_UNAVAILABLE:{self.bootstrap_path}") from error
        if not bootstrap.strip():
            raise RuntimeError("AFFOTECH_EXECUTOR_BOOTSTRAP_EMPTY")
        context = ""
        if relay_authority is not None:
            snapshot = relay_authority.get("snapshotCommit")
            publication = relay_authority.get("publicationId")
            content_hash = relay_authority.get("contentSha256")
            if not all(isinstance(value, str) and value for value in (snapshot, publication, content_hash)):
                raise RuntimeError("RELAY_AUTHORITY_CONTEXT_INVALID")
            context = "\n\n".join((
                "ORCHESTRATOR_VALIDATED_RELAY_SNAPSHOT",
                f"snapshotCommit={snapshot}",
                f"publicationId={publication}",
                f"contentSha256={content_hash}",
                "Use this already-validated immutable snapshot for the current task; do not substitute another relay generation.",
            ))
        return bootstrap.rstrip("\r\n") + ("\n\n" + context if context else "") + "\n\n" + task_prompt


def discover_codex_launcher(executable: str = "codex") -> list[str]:
    if os.name != "nt" or executable != "codex":
        return [executable]
    cmd = shutil.which("codex.cmd") or shutil.which("codex")
    if not cmd:
        raise FileNotFoundError("CODEX_COMMAND_NOT_FOUND")
    text = Path(cmd).read_text(encoding="utf-8", errors="replace")
    match = re.search(r'node_modules[\\/]+@openai[\\/]codex[\\/]bin[\\/]codex\.js', text, re.I)
    if not match:
        raise RuntimeError("CODEX_SHIM_TARGET_NOT_RESOLVED")
    script = str(Path(cmd).parent / Path(match.group(0).replace("\\", "/")).as_posix())
    node = str(Path(cmd).parent / "node.exe")
    if not Path(node).exists():
        node = shutil.which("node") or "node"
    return [node, script]


class ArchitectPlaywright:
    """Semantic Playwright boundary; it never targets AFFOTECH pages."""
    def __init__(self, page: Any):
        self.page = page
        self.last_state = "NOT_YET"

    def latest_response(self) -> str:
        return self.page.get_by_role("main").inner_text()

    def _assistant_entries(self) -> list[dict[str, str | None]]:
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is not None:
            script = """
            () => [...document.querySelectorAll('[data-message-author-role="assistant"]')]
              .filter((node) => node.isConnected)
              .map((node) => ({
                id: node.getAttribute('data-message-id'),
                text: node.innerText || node.textContent || ''
              }))
            """
            last_error = None
            for _ in range(3):
                try:
                    result = evaluate(script)
                    if not isinstance(result, list):
                        last_error = RuntimeError("ASSISTANT_SNAPSHOT_INVALID")
                        break
                    return [
                        {"id": item.get("id"), "text": item.get("text", "")}
                        for item in (result or [])
                        if isinstance(item, dict)
                    ]
                except Exception as error:  # transient DOM replacement; retry the whole snapshot
                    last_error = error
                    time.sleep(0.05)
            if last_error:
                raise last_error
            raise RuntimeError("ASSISTANT_SNAPSHOT_INVALID")
        messages = self.page.locator('[data-message-author-role="assistant"]')
        entries = []
        for index in range(messages.count()):
            message = messages.nth(index)
            get_attribute = getattr(message, "get_attribute", lambda name: None)
            entries.append({"id": get_attribute("data-message-id"), "text": message.inner_text()})
        return entries

    def assistant_baseline(self) -> dict[str, Any]:
        entries = self._assistant_entries()
        snapshot = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        return {"count": len(entries), "text_hash": hashlib.sha256(snapshot.encode()).hexdigest(), "entries": entries}

    def assistant_count(self) -> int:
        return self.page.locator('[data-message-author-role="assistant"]').count()

    def _assistant_ids(self) -> list[str]:
        return [entry["id"] for entry in self._assistant_entries() if entry.get("id")]

    def latest_completed_executor_prompt(self) -> tuple[str, str] | None:
        """Return the newest valid completed Executor block already in the DOM."""
        for entry in reversed(self._assistant_entries()):
            response = entry.get("text") or ""
            prompt = extract_executor_prompt(response)
            if prompt is not None:
                return response, prompt
        return None

    def latest_executor_prompt(self) -> tuple[str, str] | None:
        for entry in reversed(self._assistant_entries()):
            response = entry.get("text") or ""
            prompt = extract_executor_prompt_envelope(response)
            if prompt is not None:
                return response, prompt
        return None

    def load_older_history(self, max_steps: int = 64, max_seconds: float = 90.0, delay: float = 0.25, emit: Callable[[str], None] | None = None, stop_when: Callable[[], bool] | None = None) -> int:
        """Scroll likely conversation containers upward a bounded number of times."""
        self.last_history_stop_reason = "SAFETY_CAP"
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is None:
            self.last_history_stop_reason = "NO_PROGRESS"
            return 0
        loaded_steps = 0
        no_progress_steps = 0
        previous_key = None
        deadline = time.monotonic() + max_seconds
        script = """
        () => {
          const assistants = () => [...document.querySelectorAll('[data-message-author-role="assistant"]')];
          const ids = () => assistants().map((node) => node.getAttribute('data-message-id')).filter(Boolean);
          const seen = new Set(); const candidates = [];
          for (const node of assistants()) {
            let el = node.parentElement;
            while (el) { if (!seen.has(el)) { seen.add(el); candidates.push(el); } el = el.parentElement; }
          }
          const main = document.querySelector('main');
          if (main && !seen.has(main)) candidates.push(main);
          for (const el of document.querySelectorAll('[class*="scroll-root"]')) if (!seen.has(el)) candidates.push(el);
          const measured = candidates.map((el) => ({
            el, tag: el.tagName.toLowerCase(), classSummary: typeof el.className === 'string' ? el.className.slice(0,180) : '',
            scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight
          })).filter((item) => item.scrollHeight - item.clientHeight >= Math.max(200, item.clientHeight * 0.25) && item.clientHeight >= 400);
          const score = (item) => [item.classSummary.includes('scroll-root') ? 1 : 0, item.scrollTop > 0 ? 1 : 0,
            Math.min(item.scrollHeight - item.clientHeight, 100000), item.clientHeight];
          measured.sort((a, b) => { const sa = score(a), sb = score(b); for (let i=0;i<sa.length;i++) if (sa[i] !== sb[i]) return sb[i]-sa[i]; return 0; });
          const selected = measured[0];
          if (!selected) return {found:false, idsBefore:ids()};
          const before = selected.el.scrollTop; const idsBefore = ids();
          selected.el.scrollTop = Math.max(0, before - Math.max(500, selected.el.clientHeight * 0.85));
          return {found:true, tag:selected.tag, classSummary:selected.classSummary, scrollTopBefore:before,
            scrollTopAfter:selected.el.scrollTop, scrollHeight:selected.scrollHeight, clientHeight:selected.clientHeight,
            idsBefore, idsAfter:ids()};
        }
        """
        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                self.last_history_stop_reason = "SAFETY_CAP"
                break
            result = evaluate(script)
            loaded_steps += 1
            if isinstance(result, dict):
                if not result.get("found"):
                    self.last_history_stop_reason = "TOP_REACHED"
                    break
                before = result.get("scrollTopBefore")
                after = result.get("scrollTopAfter")
                if delay:
                    time.sleep(delay)
                ids_after = self._assistant_ids()
                if emit:
                    emit(f"HISTORY_SCROLL_CONTAINER overflow={result.get('scrollHeight', 0) - result.get('clientHeight', 0)} scrollTop={after}")
                    emit(f"HISTORY_SCROLL_STEP step={loaded_steps} before={before} after={after} assistants={len(result.get('idsBefore', []))}->{len(ids_after)}")
                key = (round(float(after), 3), tuple(ids_after))
                if key == previous_key:
                    no_progress_steps += 1
                else:
                    no_progress_steps = 0
                previous_key = key
                if stop_when and stop_when():
                    self.last_history_stop_reason = "FOUND"
                    break
                if after >= before:
                    self.last_history_stop_reason = "TOP_REACHED" if float(after or 0) <= 1 else "NO_PROGRESS"
                    break
                if float(after or 0) <= 1:
                    self.last_history_stop_reason = "TOP_REACHED"
                    break
                if no_progress_steps >= 2:
                    self.last_history_stop_reason = "NO_PROGRESS"
                    break
            elif not result:
                self.last_history_stop_reason = "TOP_REACHED"
                break
            else:
                if stop_when and stop_when():
                    self.last_history_stop_reason = "FOUND"
                    break
                if delay:
                    time.sleep(delay)
        else:
            self.last_history_stop_reason = "SAFETY_CAP"
        return loaded_steps

    def restore_live_bottom(self, delay: float = 0.5) -> dict[str, Any]:
        """Return the conversation viewport to the latest virtualized window."""
        result = self.page.evaluate("""
        () => {
          const assistants = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
          const seen = new Set(), candidates = [];
          for (const node of assistants) { let el=node.parentElement; while(el){ if(!seen.has(el)){seen.add(el);candidates.push(el)} el=el.parentElement; } }
          const main=document.querySelector('main'); if(main&&!seen.has(main)) candidates.push(main);
          for(const el of document.querySelectorAll('[class*="scroll-root"]')) if(!seen.has(el)) candidates.push(el);
          const eligible=candidates.map(el=>({el,tag:el.tagName.toLowerCase(),classSummary:typeof el.className==='string'?el.className.slice(0,180):'',scrollTop:el.scrollTop,scrollHeight:el.scrollHeight,clientHeight:el.clientHeight}))
            .filter(x=>x.scrollHeight-x.clientHeight>=Math.max(200,x.clientHeight*.25)&&x.clientHeight>=400);
          eligible.sort((a,b)=>{const sa=[a.classSummary.includes('scroll-root')?1:0,a.scrollTop>0?1:0,Math.min(a.scrollHeight-a.clientHeight,100000),a.clientHeight],sb=[b.classSummary.includes('scroll-root')?1:0,b.scrollTop>0?1:0,Math.min(b.scrollHeight-b.clientHeight,100000),b.clientHeight];for(let i=0;i<sa.length;i++)if(sa[i]!=sb[i])return sb[i]-sa[i];return 0});
          const selected=eligible[0]; if(!selected)return {found:false,before:null,after:null};
          const before=selected.el.scrollTop; selected.el.scrollTop=selected.el.scrollHeight-selected.el.clientHeight;
          return {found:true,before,after:selected.el.scrollTop};
        }
        """)
        if delay:
            time.sleep(delay)
        return result if isinstance(result, dict) else {"found": False, "before": None, "after": None}

    def user_baseline(self) -> dict[str, Any]:
        messages = self.page.locator('[data-message-author-role="user"]')
        count = messages.count()
        text = messages.nth(count - 1).inner_text() if count else ""
        return {"count": count, "text_hash": hashlib.sha256(text.encode()).hexdigest()}

    def user_message_texts(self) -> list[str]:
        """Read all user messages without including expandable UI controls."""
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is not None:
            script = """
            () => [...document.querySelectorAll('[data-message-author-role="user"]')].map(node => {
              const clone = node.cloneNode(true);
              clone.querySelectorAll('button,[role="button"]').forEach(control => control.remove());
              return clone.innerText || clone.textContent || '';
            })
            """
            result = evaluate(script)
            if isinstance(result, list):
                return [item for item in result if isinstance(item, str)]
        messages = self.page.locator('[data-message-author-role="user"]')
        return [messages.nth(index).inner_text() for index in range(messages.count())]

    def exact_user_message_payload_observed(self, payload: str) -> bool:
        target = normalize_prompt(payload)
        return any(normalize_prompt(text) == target for text in self.user_message_texts())

    def control_user_messages(self) -> list[dict[str, str | None]]:
        """Read only user-message identities/text for the remote pause monitor."""
        messages = self.page.locator('[data-message-author-role="user"]')
        result = []
        for index in range(messages.count()):
            message = messages.nth(index)
            text = message.inner_text()
            identity = message.get_attribute("data-message-id") or message.get_attribute("id")
            result.append({"id": identity, "text": text})
        return result

    def latest_user_message(self) -> str | None:
        messages = self.page.locator('[data-message-author-role="user"]')
        count = messages.count()
        return messages.nth(count - 1).inner_text() if count else None

    def submit_user_and_confirm(self, message: str, timeout: float = 15.0) -> bool:
        baseline = self.user_baseline()
        self.submit_result(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = self.page.locator('[data-message-author-role="user"]')
            count = messages.count()
            text = messages.nth(count - 1).inner_text() if count else ""
            if count > baseline["count"] or hashlib.sha256(text.encode()).hexdigest() != baseline["text_hash"]:
                return True
            time.sleep(0.25)
        return False

    def wait_for_new_completed_response(self, baseline: dict[str, Any], poll_interval: float = 0.5) -> str:
        observed = self.wait_for_new_response(baseline, poll_interval)
        if observed["state"] != "COMPLETED":
            raise TimeoutError("ARCHITECT_NEW_RESPONSE_NOT_READY")
        return observed["text"]

    def wait_for_new_response(self, baseline: dict[str, Any], poll_interval: float = 0.5) -> dict[str, Any]:
        stable_hash = None
        stable_polls = 0
        while True:
            entries = self._assistant_entries()
            snapshot = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
            current = {"count": len(entries), "text_hash": hashlib.sha256(snapshot.encode()).hexdigest(), "entries": entries}
            identity_changed = current["count"] != baseline.get("count", 0) or current["text_hash"] != baseline.get("text_hash")
            baseline_entries = baseline.get("entries", [])
            baseline_pairs = {(entry.get("id"), entry.get("text")) for entry in baseline_entries}
            changed_entries = [entry for entry in entries if (entry.get("id"), entry.get("text")) not in baseline_pairs]
            text = changed_entries[-1].get("text", "") if changed_entries else (entries[-1].get("text", "") if entries else "")
            if self.generation_visible():
                stable_hash = None
                stable_polls = 0
                if identity_changed:
                    self.last_state = "RUNNING"
                else:
                    self.last_state = "NOT_YET"
                time.sleep(poll_interval)
                continue
            if identity_changed and text.strip():
                if text.rstrip().endswith(COMPLETE):
                    self.last_state = "COMPLETED"
                    return {"state": "COMPLETED", "text": text}
                current_hash = hashlib.sha256(text.encode()).hexdigest()
                if current_hash == stable_hash:
                    stable_polls += 1
                else:
                    stable_hash = current_hash
                    stable_polls = 1
                if stable_polls < 2:
                    self.last_state = "RUNNING"
                    time.sleep(poll_interval)
                    continue
                if text.rstrip().endswith(COMPLETE) or extract_executor_prompt_envelope(text) is not None:
                    self.last_state = "COMPLETED"
                    return {"state": "COMPLETED", "text": text}
                self.last_state = "BLOCKED"
                return {"state": "BLOCKED", "text": text}
            stable_hash = None
            stable_polls = 0
            if identity_changed:
                self.last_state = "BLOCKED"
                return {"state": "BLOCKED", "text": text}
            self.last_state = "NOT_YET"
            time.sleep(poll_interval)

    def submit_and_wait(self, message: str, poll_interval: float = 0.5) -> str:
        baseline = self.assistant_baseline()
        if not self.submit_user_and_confirm(message):
            raise RuntimeError("ARCHITECT_SUBMISSION_NOT_CONFIRMED")
        return self.wait_for_new_completed_response(baseline, poll_interval)

    def submit_result(self, result: str) -> None:
        composer = self.page.get_by_role("textbox").last
        composer.fill(result)
        composer.press("Enter")

    def _live_composer(self) -> Any:
        """Resolve the current editor, never reusing a locator across rerenders."""
        def actionable(candidate: Any, require_count: bool = False) -> bool:
            try:
                count = getattr(candidate, "count", None)
                if require_count and callable(count) and count() < 1:
                    return False
                return bool(getattr(candidate, "is_visible", lambda **_: True)(timeout=1000)) and bool(
                    getattr(candidate, "is_editable", lambda **_: True)(timeout=1000)
                )
            except Exception:
                return False

        # The semantic textbox is the authoritative current composer.  The
        # id selector is only a compatibility fallback and must itself pass
        # actionability checks; DOM presence alone is not sufficient.
        try:
            semantic = self.page.get_by_role("textbox").last
            if actionable(semantic):
                return semantic
        except Exception:
            pass
        locator = getattr(self.page, "locator", None)
        if locator is not None:
            try:
                fallback_locator = locator("#prompt-textarea")
                fallback = getattr(fallback_locator, "last", fallback_locator)
                if actionable(fallback, require_count=True):
                    return fallback
            except Exception:
                pass
        raise ResultSubmissionError("ARCHITECT_COMPOSER_UNAVAILABLE")

    def clear_unsent_payload(self, payload: str) -> bool:
        """Clear only an exactly matching pre-send draft after safe failure."""
        try:
            composer = self._live_composer()
            observed = composer.inner_text(timeout=1000)
            if not isinstance(observed, str) or normalize_prompt(observed) != normalize_prompt(payload):
                return False
            composer.focus(timeout=1000)
            composer.press("ControlOrMeta+A", timeout=1000)
            composer.press("Backspace", timeout=1000)
            cleared = composer.inner_text(timeout=1000)
            return isinstance(cleared, str) and not cleared.strip()
        except Exception:
            return False

    def submit_result_bounded(self, result: str, timeout: float = 30.0) -> None:
        """Submit a result with explicit, bounded stages and typed failures."""
        self.last_send_method = None
        self.sendActionAttempted = False
        self.sendActionAcknowledged = False
        self.last_unsent_payload_cleared = False
        deadline = time.monotonic() + timeout
        last_error = None
        populated = False
        try:
            self.restore_live_bottom(delay=0)
        except Exception:
            pass
        while time.monotonic() < deadline:
            try:
                composer = self._live_composer()
                visible = getattr(composer, "is_visible", lambda **_: True)(timeout=1000)
                editable = getattr(composer, "is_editable", lambda **_: True)(timeout=1000)
                if visible and editable:
                    break
            except Exception as error:
                last_error = error
            time.sleep(0.25)
        else:
            detail = type(last_error).__name__ if last_error else None
            raise ResultSubmissionError("ARCHITECT_COMPOSER_UNAVAILABLE", detail) from last_error

        # ChatGPT's current rich editor can remain actionability-blocked for
        # locator.fill even when it is visible/editable.  Use the native
        # keyboard route after explicit focus; this updates the same editor
        # state as user typing/pasting and handles multiline Markdown.
        while time.monotonic() < deadline:
            try:
                # Re-resolve on every attempt because the rich editor can be
                # replaced while the fresh page hydrates.
                composer = self._live_composer()
                visible = getattr(composer, "is_visible", lambda **_: True)(timeout=1000)
                editable = getattr(composer, "is_editable", lambda **_: True)(timeout=1000)
                if not visible or not editable:
                    raise TimeoutError("ARCHITECT_COMPOSER_NOT_ACTIONABLE")
                composer.focus(timeout=1000)
                composer.press("ControlOrMeta+A", timeout=1000)
                keyboard = getattr(self.page, "keyboard", None)
                if keyboard is None:
                    raise RuntimeError("KEYBOARD_INPUT_UNAVAILABLE")
                keyboard.insert_text(result)
                populated = True
                break
            except Exception as error:
                if type(error).__name__ == "TimeoutError":
                    last_error = error
                    # A timed-out input may have left a partial unsent draft.
                    # A fresh composer reference and select-all/backspace keep
                    # the next attempt replacement-safe without sending.
                    try:
                        retry_composer = self._live_composer()
                        retry_composer.focus(timeout=1000)
                        retry_composer.press("ControlOrMeta+A", timeout=1000)
                        retry_composer.press("Backspace", timeout=1000)
                    except Exception:
                        pass
                    if time.monotonic() < deadline:
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                else:
                    code = "ARCHITECT_COMPOSER_INPUT_REJECTED"
                    if populated and not self.sendActionAttempted:
                        self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
                    raise ResultSubmissionError(code, type(error).__name__) from error
        else:
            detail = type(last_error).__name__ if last_error else None
            raise ResultSubmissionError("ARCHITECT_COMPOSER_POPULATE_OPERATION_TIMEOUT", detail) from last_error

        try:
            composer = self._live_composer()
            observed = composer.inner_text(timeout=1000)
        except Exception as error:
            code = "ARCHITECT_COMPOSER_INPUT_ACCEPTANCE_TIMEOUT" if type(error).__name__ == "TimeoutError" else "ARCHITECT_COMPOSER_INPUT_REJECTED"
            if populated and not self.sendActionAttempted:
                self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
            raise ResultSubmissionError(code, type(error).__name__) from error
        if not isinstance(observed, str) or not observed.strip():
            if populated and not self.sendActionAttempted:
                self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
            raise ResultSubmissionError("ARCHITECT_COMPOSER_INPUT_REJECTED")
        if normalize_prompt(observed) != normalize_prompt(result):
            if populated and not self.sendActionAttempted:
                self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
            raise ResultSubmissionError("ARCHITECT_COMPOSER_INPUT_REJECTED", "CONTENT_MISMATCH")
        assistant_count_before = self.assistant_count()

        try:
            send = self.page.get_by_role("button", name=re.compile(r"^\s*send(?:\s+prompt)?\s*$", re.I)).last
            send_visible = getattr(send, "is_visible", lambda **_: True)(timeout=1000)
        except Exception as error:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE", type(error).__name__) from error
        if not send_visible:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE")
        try:
            enabled = getattr(send, "is_enabled", lambda **_: True)(timeout=1000)
        except Exception as error:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE", type(error).__name__) from error
        if not enabled:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_DISABLED")
        try:
            self.sendActionAttempted = True
            send.click(timeout=1000)
            self.last_send_method = "playwright.click"
        except Exception as error:
            if type(error).__name__ != "TimeoutError":
                raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", type(error).__name__) from error
            # A visible, enabled button can still be actionability-blocked by
            # transient layout.  Enter is a Playwright-only fallback on the
            # already confirmed active composer; transition confirmation below
            # remains the delivery authority.
            try:
                self.sendActionAttempted = True
                composer = self._live_composer()
                composer.focus(timeout=1000)
                composer.press("Enter", timeout=1000)
                self.last_send_method = "playwright.composer.press(Enter)"
            except Exception as fallback_error:
                raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", type(fallback_error).__name__) from fallback_error

        # A bounded acknowledgement is the first observable post-send state:
        # the live composer no longer contains the submitted result.  Do not
        # interpret a timeout as a composer-discovery failure.
        ack_deadline = min(deadline, time.monotonic() + 5.0)
        while time.monotonic() < ack_deadline:
            try:
                composer_empty = not self._live_composer().inner_text(timeout=1000).strip()
                stop = self.page.get_by_role("button", name=re.compile(r"stop(?: generating)?", re.I)).last
                generation_visible = stop.count() > 0 and stop.is_visible(timeout=1000)
                assistant_started = self.assistant_count() > assistant_count_before
                if composer_empty or generation_visible or assistant_started:
                    self.sendActionAcknowledged = True
                    return
            except Exception as error:
                last_error = error
            time.sleep(0.1)
        detail = type(last_error).__name__ if last_error else None
        raise ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT", detail) from last_error

    def generation_visible(self) -> bool:
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is None:
            return False
        try:
            return bool(evaluate("""
            () => [...document.querySelectorAll('[data-testid="stop-button"]')]
              .filter((button) => button.isConnected && !button.disabled)
              .some((button) => {
                const style = getComputedStyle(button);
                const rect = button.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              })
            """))
        except Exception:
            return False

    def close(self) -> None:
        runtime = getattr(self, "_runtime", None)
        browser = getattr(self, "_browser", None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:
                pass

    def open_fresh_and_wait_ready(self, handover: str) -> bool:
        new_page = self.page.context.new_page()
        new_page.goto("https://chatgpt.com/")
        new_page.get_by_role("textbox").last.fill(f"{handover}\nReply exactly {READY}")
        new_page.get_by_role("textbox").last.press("Enter")
        ready = READY in new_page.get_by_role("main").inner_text()
        if ready:
            self.page = new_page
        return ready

    def open_fresh_with_handover(self, handover: str) -> Any:
        """Create one fresh authenticated-context tab and submit unchanged handover."""
        new_page = self.page.context.new_page()
        try:
            new_page.goto("https://chatgpt.com/")
            ArchitectPlaywright(new_page).submit_result_bounded(handover)
        except Exception:
            try:
                new_page.close()
            except Exception:
                pass
            raise
        return new_page

    @staticmethod
    def attach(endpoint: str, conversation_id: str | None = None) -> "ArchitectPlaywright":
        from playwright.sync_api import sync_playwright
        runtime = sync_playwright().start()
        try:
            browser = runtime.chromium.connect_over_cdp(endpoint, timeout=10000)
        except Exception as error:
            runtime.stop()
            raise RuntimeError("ARCHITECT_CDP_WEBSOCKET_ATTACHMENT_TIMEOUT") from error
        pages = [p for context in browser.contexts for p in context.pages]
        if conversation_id:
            candidates = []
            for page in pages:
                try:
                    actual_id = architect_conversation_id_from_url(page.url)
                except RuntimeError:
                    continue
                if architect_conversation_ids_equivalent(conversation_id, actual_id):
                    candidates.append((page, actual_id))
            distinct_ids = {actual_id for _page, actual_id in candidates}
            if len(distinct_ids) > 1:
                runtime.stop()
                raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
            exact = [page for page, actual_id in candidates if actual_id == conversation_id]
            if exact:
                pages = exact
            elif len(candidates) == 1:
                pages = [candidates[0][0]]
            elif candidates:
                runtime.stop()
                raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
            else:
                pages = []
        if not pages:
            runtime.stop()
            raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND") if conversation_id else RuntimeError("ARCHITECT_PAGE_NOT_FOUND")
        bridge = ArchitectPlaywright(pages[-1])
        bridge._runtime = runtime
        bridge._browser = browser
        return bridge


class LocalWatcher:
    def __init__(self, project_dir: str, state_path: str | os.PathLike[str] = "orchestrator-state.json", runner: CodexRunner | None = None, durable_decision_reader: Callable[[str], dict[str, Any] | None] | None = None, durable_terminal_publisher: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None):
        self.project_dir = project_dir
        self.state_path = Path(state_path)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"cycle_count": 0, "in_flight": False}
        if "architectResponseCount" not in self.state:
            self.state["architectResponseCount"] = 0
        self.loop_guard = LoopGuard(self.state)
        root_pid = os.environ.get("ARCHITECT_BROWSER_ROOT_PID")
        self.architect_memory_reader = (lambda: architect_process_tree_memory_bytes(int(root_pid))) if root_pid else None
        self.state.setdefault("memoryThresholdBytes", ARCHITECT_MEMORY_THRESHOLD_BYTES)
        self.runner = runner or CodexRunner(project_dir, child_project_dir=AFFOTECH_CHILD_PROJECT_DIR, session_id=AFFOTECH_EXECUTOR_SESSION_ID)
        if durable_decision_reader is not None:
            self.durable_decision_reader = durable_decision_reader
        else:
            evidence_repo = self.state.get("evidenceRepository") or str(Path(project_dir) / ".agent-work" / "evidence-repo")
            self.durable_decision_reader = lambda publication_id: read_matching_architect_decision(evidence_repo, publication_id)
        evidence_repo = self.state.get("evidenceRepository") or str(Path(project_dir) / ".agent-work" / "evidence-repo")
        self.durable_terminal_publisher = durable_terminal_publisher or (lambda publication_id, result_text, envelope: publish_durable_executor_terminal(evidence_repo, publication_id, result_text, envelope))
        self.session_rollover = ArchitectSessionRollover(self)
        self.documentation_doorbell = DocumentationDoorbell(self)

    def startup_candidate(self, bridge: ArchitectPlaywright, scan_history: bool = True, emit: Callable[[str], None] | None = None) -> str | None:
        """Find a fresh completed prompt without requiring a new response."""
        found = bridge.latest_completed_executor_prompt()
        if found is None:
            latest_prompt = getattr(bridge, "latest_executor_prompt", None)
            generation_visible = getattr(bridge, "generation_visible", lambda: False)
            fallback = latest_prompt() if latest_prompt is not None else None
            if fallback is not None and not generation_visible():
                first_hash = hashlib.sha256(fallback[0].encode()).hexdigest()
                time.sleep(0.5)
                second = latest_prompt() if latest_prompt is not None else None
                if second is not None and hashlib.sha256(second[0].encode()).hexdigest() == first_hash and not generation_visible():
                    found = second
        if found is None and scan_history:
            steps = bridge.load_older_history(emit=emit, stop_when=lambda: self._fresh_prompt(bridge) is not None)
            found = bridge.latest_completed_executor_prompt()
            if found is None and emit:
                emit(f"STARTUP_HISTORY_EXHAUSTED reason={getattr(bridge, 'last_history_stop_reason', 'SAFETY_CAP')} steps={steps}")
        if found is None:
            return None
        _, prompt = found
        prompt_hash = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        if prompt_hash == self.state.get("last_prompt_hash") or prompt_hash == self.state.get("in_flight_prompt_hash"):
            return None
        return prompt

    def _fresh_prompt(self, bridge: ArchitectPlaywright) -> str | None:
        found = bridge.latest_completed_executor_prompt()
        if found is None:
            return None
        prompt = found[1]
        prompt_hash = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        if prompt_hash == self.state.get("last_prompt_hash") or prompt_hash == self.state.get("in_flight_prompt_hash"):
            return None
        return prompt

    def save(self) -> None:
        self.state.update({"last_prompt_hash": self.loop_guard.last_prompt_hash, "last_result_hash": self.loop_guard.last_result_hash})
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")

    @staticmethod
    def process_alive(pid: Any) -> bool:
        """Return whether an independently launched executor is still alive."""
        if not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            ERROR_INVALID_PARAMETER = 87
            ERROR_NOT_FOUND = 1168
            STILL_ACTIVE = 259

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error in (ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND):
                    return False
                if error == ERROR_ACCESS_DENIED:
                    return True
                # Any other uncertain result fails safe.
                return True
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False
        return True

    def _record_executor_start(self, pid: int, emit: Callable[[str], None]) -> None:
        # Persist before any result handling so a watcher restart cannot launch
        # a second child for the same immutable relay task.
        self.state["active_codex_pid"] = pid
        self.state["codex_pid"] = pid
        self.state["executor_state"] = "RUNNING"
        self.state["in_flight"] = True
        self.save()
        emit(f"CODEX_STARTED pid={pid}")

    def _clear_executor_start(self) -> None:
        self.state.pop("active_codex_pid", None)
        self.state.pop("codex_pid", None)
        self.state["executor_state"] = "EXITED"
        self.save()

    def _recoverable_result_exists(self) -> bool:
        result_path = self.state.get("result_file")
        if not isinstance(result_path, str) or not Path(result_path).is_file():
            return False
        envelope_path = self.state.get("result_envelope_file") or f"{result_path}.envelope.json"
        try:
            read_result_envelope(envelope_path)
            Path(result_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError):
            return False
        return True

    def _reconcile_inflight_executor(self, source: RelayPromptSource, bridge: ArchitectPlaywright | None, timeout: float, emit: Callable[[str], None]) -> str | None:
        """Reconcile an in-flight child without inferring failure from age."""
        pid = self.state.get("active_codex_pid") or self.state.get("codex_pid")
        if self.process_alive(pid):
            self.state["in_flight"] = True
            self.state["executor_state"] = "RUNNING"
            self.save()
            emit(f"CODEX_RUNNING pid={pid}")
            return "RUNNING"
        if self._recoverable_result_exists():
            self.state["result_pending"] = True
            self.state["executor_completed"] = True
            self.state["executor_state"] = "EXITED_RESULT_RECOVERABLE"
            self.save()
            # Re-enter the durable-result path; this never launches Codex.
            return self.run_relay_once(source, bridge, timeout, emit)
        if pid is not None:
            self.state["executor_state"] = "EXITED_WITHOUT_RECOVERABLE_RESULT"
            self.save()
        return None

    def retire_unrecoverable_relay(self, publication_id: str, content_sha256: str, reason: str = "SUPERSEDED_UNRECOVERABLE") -> bool:
        """Retire one proven-lost execution without manufacturing a result."""
        key = f"{publication_id}:{content_sha256}"
        if self.state.get("in_flight_relay_key") != key or self.state.get("result_pending"):
            return False
        retired = self.state.setdefault("retired_relay_keys", {})
        retired[key] = {"publicationId": publication_id, "contentSha256": content_sha256, "state": reason, "resultRecovered": False}
        self.state.pop("in_flight_relay_key", None)
        self.state["relay_recovery_state"] = reason
        self.save()
        return True

    def migrate_legacy_inflight_state(self, supersession_proof: dict[str, Any] | None, consumed_relay_keys: dict[str, dict[str, Any]] | None = None) -> bool:
        """Atomically retire a proven legacy in-flight record without calling it PASS."""
        key = self.state.get("in_flight_relay_key")
        publication_id = self.state.get("relay_publication_id")
        if not self.state.get("in_flight") or not isinstance(key, str) or not isinstance(publication_id, str):
            return False
        if self.state.get("result_pending") or self.state.get("executor_completed") or self.state.get("active_codex_pid") or self.state.get("codex_pid"):
            return False
        if not isinstance(supersession_proof, dict) or supersession_proof.get("currentPublicationId") == publication_id:
            return False
        backup = self.state_path.with_name(self.state_path.name + ".pre-legacy-migration.bak")
        if backup.exists():
            raise RuntimeError("LEGACY_STATE_BACKUP_ALREADY_EXISTS")
        original = self.state_path.read_bytes()
        backup.write_bytes(original)
        if backup.read_bytes() != original:
            raise RuntimeError("LEGACY_STATE_BACKUP_HASH_MISMATCH")
        retired = self.state.setdefault("retired_relay_keys", {})
        retired[key] = {"publicationId": publication_id, "resolution": "SUPERSEDED_WITHOUT_RETRY", "executionAuthorized": False, "historical": True, "resolvedFromLegacyState": True, "supersessionEvidence": supersession_proof}
        for consumed_key, evidence in (consumed_relay_keys or {}).items():
            retired.setdefault(consumed_key, {"resolution": "ALREADY_EXECUTED_AND_REVIEWED", "executionAuthorized": False, "historical": True, "supersessionEvidence": evidence})
        self.state.pop("in_flight_relay_key", None)
        self.state["in_flight"] = False
        self.state["relay_recovery_state"] = "SUPERSEDED_WITHOUT_RETRY"
        self.state["legacy_inflight_migration"] = {"publicationId": publication_id, "resolution": "SUPERSEDED_WITHOUT_RETRY", "executionAuthorized": False, "historical": True, "resolvedFromLegacyState": True, "supersessionEvidence": supersession_proof}
        self.save()
        try:
            json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.state_path.write_bytes(original)
            self.state = json.loads(original.decode("utf-8"))
            raise RuntimeError("LEGACY_STATE_ATOMIC_WRITE_FAILED")
        return True

    def _ensure_durable_terminal(self, relay_publication_id: str, result_text: str, envelope: dict[str, Any]) -> dict[str, Any]:
        existing = self.state.get("durable_terminal_publication_id")
        if self.state.get("durable_terminal_published") and isinstance(existing, str) and existing:
            return {"publicationId": existing, "resultSha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(), "alreadyPublished": True}
        published = self.durable_terminal_publisher(relay_publication_id, result_text, envelope)
        if not isinstance(published, dict) or not isinstance(published.get("publicationId"), str):
            raise RuntimeError("DURABLE_TERMINAL_PUBLICATION_INVALID")
        self.state["durable_terminal_publication_id"] = published["publicationId"]
        self.state["durable_terminal_result_sha256"] = published.get("resultSha256") or hashlib.sha256(result_text.encode("utf-8")).hexdigest()
        self.state["durable_terminal_published"] = True
        self.state["durable_terminal_readback"] = True
        self.save()
        return published

    def _retire_after_durable_terminal(self, relay_key: str) -> None:
        self.state["last_completed_relay_key"] = relay_key
        self.state.pop("in_flight_relay_key", None)
        self.state["relay_execution_retired"] = True
        self.save()

    def reconcile_durable_recovery(self, emit: Callable[[str], None] = print) -> bool:
        """Clear recovery only after an exact durable Architect decision match."""
        if not self.state.get("in_flight_relay_key"):
            return False
        terminal_publication = (self.state.get("recovery_terminal_publication_id")
                                or self.state.get("in_flight_terminal_publication_id")
                                or self.state.get("executor_terminal_publication_id"))
        if not isinstance(terminal_publication, str) or not terminal_publication:
            return False
        record = self.durable_decision_reader(terminal_publication)
        if not isinstance(record, dict):
            return False
        decision, pointer = record.get("decision"), record.get("acceptedPointer")
        if not isinstance(decision, dict) or not isinstance(pointer, dict):
            return False
        if decision.get("reviewedPublicationId") != terminal_publication:
            return False
        if decision.get("requiresArchitectDecision") is not False:
            return False
        if decision.get("decision") not in {None, "ACCEPTED"} and decision.get("classification") != "ACCEPTED":
            return False
        if pointer.get("accepted") is not True or pointer.get("publicationId") != terminal_publication:
            return False
        relay_key = self.state["in_flight_relay_key"]
        self.state["last_completed_relay_key"] = relay_key
        self.state.pop("in_flight_relay_key", None)
        self.state.pop("in_flight_prompt_hash", None)
        self.state["in_flight"] = False
        self.state["relay_recovery_state"] = "ARCHITECT_REVIEWED"
        self.state["recovery_reconciled"] = True
        self.state["recovery_reconciled_publication_id"] = terminal_publication
        self.save()
        emit(f"RECOVERY_RECONCILED publication={terminal_publication}")
        return True

    def run_relay_once(self, source: RelayPromptSource, bridge: ArchitectPlaywright | None, timeout: float = 300.0, emit: Callable[[str], None] = print) -> str:
        observation = source.read_current()
        publication_id = observation["publicationId"]
        relay_key = relay_task_key(publication_id, observation["contentSha256"])
        emit(f"LATEST_PROMPT publication={publication_id}")
        if self.state.get("rolloverPending") and not self.state.get("handoverRequested"):
            emit("STATE=ROLLOVER_PENDING")
            return "ROLLOVER_PENDING"
        if self.state.get("result_pending"):
            pending_key = self.state.get("in_flight_relay_key") or relay_key
            result_path = self.state.get("result_file")
            emit("RECOVERING_PENDING_RESULT")
            emit(f"TASK_PUBLICATION={self.state.get('relay_publication_id', publication_id)}")
            if not bridge or not result_path or not Path(result_path).exists():
                emit("STATE=RESULT_PENDING")
                return "RESULT_PENDING"
            result_text = None
            try:
                envelope_path = self.state.get("result_envelope_file") or f"{result_path}.envelope.json"
                envelope = read_result_envelope(envelope_path)
                result_text = Path(result_path).read_text(encoding="utf-8")
                self._ensure_durable_terminal(self.state.get("relay_publication_id", publication_id), result_text, envelope)
                self._retire_after_durable_terminal(pending_key)
                submission_key = result_submission_key(self.state.get("relay_publication_id", publication_id), result_text)
                if self.state.get("last_submitted_result_key") != submission_key:
                    submit = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
                    submit(result_text)
                    self.state["last_submitted_result_key"] = submission_key
            except Exception as error:
                emit("STATE=RESULT_PENDING")
                emit(f"RESULT_SUBMISSION_DEFERRED reason={type(error).__name__}:{error}")
                emit(f"RESULT_FILE={result_path}")
                return "RESULT_PENDING"
            self.state["last_completed_relay_key"] = pending_key
            self.state.pop("in_flight_relay_key", None)
            self.state["result_pending"] = False
            self.state["executor_completed"] = False
            self.save()
            emit("RESULT_SENT_TO_ARCHITECT")
            emit("STATE=IDLE")
            return "COMPLETED"
        if relay_key in self.state.get("retired_relay_keys", {}):
            emit("STATE=IDLE")
            return "IDLE"
        if self.state.get("in_flight_relay_key"):
            if self.state.get("in_flight_relay_key") in self.state.get("retired_relay_keys", {}):
                emit("STATE=IDLE")
                return "IDLE"
            if self.reconcile_durable_recovery(emit=emit):
                if self.state.get("last_completed_relay_key") == relay_key:
                    emit("STATE=IDLE")
                    return "IDLE"
            reconciled = self._reconcile_inflight_executor(source, bridge, timeout, emit)
            if reconciled is not None:
                return reconciled
            emit("STATE=RECOVERY_REQUIRED")
            return "RECOVERY_REQUIRED"
        if self.state.get("last_completed_relay_key") == relay_key:
            emit("STATE=IDLE")
            return "IDLE"
        self.state["in_flight_relay_key"] = relay_key
        self.state["relay_publication_id"] = publication_id
        self.state["relay_content_sha256"] = observation["contentSha256"]
        self.save()
        emit(f"RELAY_PROMPT_DETECTED publication={publication_id}")
        try:
            self._execution_publication_id = publication_id
            self._execution_dispatch_id = observation.get("dispatchId")
            self._execution_relay_key = relay_key
            authority = observation if observation.get("snapshotCommit") else None
            submitted = self._execute_prompt(bridge, observation["prompt"], timeout, emit, relay_authority=authority)
        except Exception:
            self.save()
            raise
        if not submitted:
            return "RESULT_PENDING"
        self.state["last_completed_relay_key"] = relay_key
        self.state.pop("in_flight_relay_key", None)
        self.save()
        emit("STATE=IDLE")
        return "COMPLETED"

    def run_relay_forever(self, source: RelayPromptSource, bridge: ArchitectPlaywright | None, poll_seconds: float = 7.0, response_timeout: float = 300.0, emit: Callable[[str], None] = print) -> None:
        emit("RELAY_CONNECTED")
        reported_error = None
        idle_reported = False
        def cycle_emit(line: str) -> None:
            nonlocal idle_reported
            if line == "STATE=IDLE":
                if idle_reported:
                    return
                idle_reported = True
            elif line.startswith("RELAY_PROMPT_DETECTED") or line.startswith("STATE="):
                idle_reported = False
            emit(line)
        while True:
            try:
                source.refresh_enabled = True
                result = self.run_relay_once(source, bridge, response_timeout, cycle_emit)
                reported_error = None
                if result == "RECOVERY_REQUIRED":
                    time.sleep(poll_seconds)
                else:
                    time.sleep(poll_seconds)
            except RelayAuthorityError as error:
                code = str(error)
                if code != reported_error:
                    emit(f"STATE=RELAY_UNAVAILABLE" if "GIT" in code else "STATE=AUTHORITY_INVALID")
                    reported_error = code
                time.sleep(poll_seconds)

    def forward(self, prompt: str, milestone: str | None = None, blocked: bool = False, timeout: float = 300.0) -> CodexResult | dict[str, str]:
        if self.loop_guard.check(prompt, milestone, blocked) == "LOOP_SUSPECTED":
            return {"state": "LOOP_SUSPECTED"}
        self.state["in_flight"] = True
        self.state["in_flight_prompt_hash"] = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        self.state["cycle_count"] = int(self.state.get("cycle_count", 0)) + 1
        self.save()
        result = self.runner.run(prompt, timeout)
        self.state["in_flight"] = False
        self.state.pop("in_flight_prompt_hash", None)
        self.loop_guard.record_result(result.output)
        self.save()
        return result

    def handover_due(self) -> bool:
        return int(self.state.get("cycle_count", 0)) >= 30 and not self.state.get("in_flight", False)

    def accept_documentation_closure(self, response: str) -> bool:
        if not self.handover_due() or DOCUMENTATION_SYNC not in response or not response.rstrip().endswith(COMPLETE):
            return False
        self.state["documentation_sync_complete"] = True
        self.save()
        return True

    def rotate_after_ready(self, handover: str, ready: bool) -> bool:
        if not self.state.get("documentation_sync_complete", False) or not ready:
            self.state["pending_handover"] = handover
            self.save()
            return False
        self.state["cycle_count"] = 0
        self.state.pop("documentation_sync_complete", None)
        self.state.pop("pending_handover", None)
        self.save()
        return True

    def observe_response(self, bridge: ArchitectPlaywright, baseline: dict[str, Any], timeout: float = 120.0) -> tuple[dict[str, Any], dict[str, Any]]:
        observed = bridge.wait_for_new_response(baseline, timeout)
        if observed["state"] != "COMPLETED":
            return observed, baseline
        self.session_rollover.observe_complete_response(observed["text"])
        prompt = extract_executor_prompt(observed["text"]) or extract_executor_prompt_envelope(observed["text"])
        next_baseline = bridge.assistant_baseline()
        if prompt is None:
            return observed, next_baseline
        return {**observed, "prompt": prompt}, next_baseline

    def run_forever(self, bridge: ArchitectPlaywright, sleep_seconds: float = 0.5, response_timeout: float = 120.0, emit: Callable[[str], None] = print) -> None:
        self.session_rollover.sample_memory(emit=emit)
        baseline = bridge.assistant_baseline()
        emit("ARCHITECT_CONNECTED")
        emit(f"STARTUP_SCAN_MOUNTED count={bridge.assistant_count()}")
        startup_prompt = self.startup_candidate(bridge, scan_history=False)
        history_scanned = False
        if startup_prompt is None:
            emit("STARTUP_HISTORY_SCAN")
            history_scanned = True
            startup_prompt = self.startup_candidate(bridge, scan_history=True, emit=emit)
        if history_scanned:
            bottom = bridge.restore_live_bottom()
            emit(f"LIVE_BOTTOM_RESTORE before={bottom.get('before')} after={bottom.get('after')}")
            baseline = bridge.assistant_baseline()
            emit(f"LIVE_BOTTOM_READY assistants={len(baseline.get('entries', []))}")
            if startup_prompt is None:
                startup_prompt = self.startup_candidate(bridge, scan_history=False)
        if startup_prompt is not None:
            self._execute_prompt(bridge, startup_prompt, response_timeout, emit)
            baseline = bridge.assistant_baseline()
        else:
            emit("STATE=IDLE")
        while True:
            self.session_rollover.sample_memory(emit=emit)
            observed, next_baseline = self.observe_response(bridge, baseline, response_timeout)
            if observed["state"] == "NOT_YET":
                continue
            baseline = next_baseline
            if observed["state"] != "COMPLETED":
                emit("STATE=IDLE")
                time.sleep(sleep_seconds)
                continue
            emit("ARCHITECT_NEW_RESPONSE")
            prompt = observed.get("prompt")
            if prompt is None:
                emit("STATE=IDLE")
                time.sleep(sleep_seconds)
                continue
            self._execute_prompt(bridge, prompt, response_timeout, emit)
            emit("STATE=IDLE")
            time.sleep(sleep_seconds)

    def _execute_prompt(self, bridge: ArchitectPlaywright | None, prompt: str, timeout: float, emit: Callable[[str], None], publication_id: str | None = None, dispatch_id: str | None = None, relay_authority: dict[str, Any] | None = None) -> bool:
        emit("EXECUTOR_PROMPT_READY")
        if relay_authority is not None:
            self.runner.relay_authority = relay_authority
        if hasattr(self.runner, "on_start"):
            self.runner.on_start = lambda pid: self._record_executor_start(pid, emit)
        result = self.forward(prompt, timeout=timeout)
        self._clear_executor_start()
        if isinstance(result, dict):
            emit(f"STATE={result['state']}")
            return False
        if not isinstance(result.exit_code, int):
            emit("CODEX_FAILED exit=unknown reason=missing_exit_evidence")
            return False
        emit(f"CODEX_COMPLETED exit=0" if result.exit_code == 0 else f"CODEX_FAILED exit={result.exit_code}")
        self.state["executor_completed"] = True
        self.state["result_pending"] = True
        result_path = result.last_message_path
        if not result_path:
            result_dir = Path(self.project_dir) / ".agent-work" / "executor-results"
            result_dir.mkdir(parents=True, exist_ok=True)
            result_path = str(result_dir / f"result-{hashlib.sha256(result.output.encode()).hexdigest()}.txt")
            Path(result_path).write_text(result.output, encoding="utf-8")
        self.state["result_file"] = result_path
        result_publication_id = publication_id or getattr(self, "_execution_publication_id", None) or self.state.get("relay_publication_id")
        submission_key = result_submission_key(result_publication_id or "LOCAL", result.output)
        self.state["pending_result_submission_key"] = submission_key
        envelope_path = f"{result_path}.envelope.json"
        envelope_status = "PASS" if result.state == "COMPLETED" and result.exit_code == 0 else ("BLOCKED" if result.state == "BLOCKED" else "FAILED")
        envelope = build_result_envelope(dispatch_id or getattr(self, "_execution_dispatch_id", None) or self.state.get("in_flight_relay_key") or f"LOCAL-{hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()}", envelope_status, result_publication_id)
        Path(envelope_path).write_text(stable_json(envelope), encoding="utf-8")
        self.state["result_envelope_file"] = envelope_path
        self.save()
        try:
            self._ensure_durable_terminal(result_publication_id or "LOCAL", result.output, envelope)
            self._retire_after_durable_terminal(getattr(self, "_execution_relay_key", self.state.get("in_flight_relay_key")))
        except Exception as error:
            emit("STATE=RESULT_PENDING")
            emit(f"DURABLE_TERMINAL_PUBLICATION_DEFERRED reason={type(error).__name__}:{error}")
            emit(f"RESULT_FILE={result_path}")
            return False
        if bridge is None or not Path(result_path).exists():
            emit("STATE=RESULT_PENDING")
            reason = "ARCHITECT_BRIDGE_UNAVAILABLE" if bridge is None else "RESULT_FILE_UNAVAILABLE"
            emit(f"RESULT_SUBMISSION_DEFERRED reason={reason}")
            emit(f"RESULT_FILE={result_path}")
            return False
        try:
            read_result_envelope(envelope_path)
            submit = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
            submit(result.output)
        except Exception as error:
            emit("STATE=RESULT_PENDING")
            emit(f"RESULT_SUBMISSION_DEFERRED reason={type(error).__name__}:{error}")
            emit(f"RESULT_FILE={result_path}")
            return False
        self.state["last_submitted_result_key"] = submission_key
        self.state.pop("pending_result_submission_key", None)
        self.state["result_pending"] = False
        self.state["executor_completed"] = False
        self.save()
        emit("RESULT_SENT_TO_ARCHITECT")
        return True


ORCHESTRATOR_STATES = {"IDLE", "EXECUTOR_RUNNING", "RESULT_READY", "ARCHITECT_RUNNING", "NEXT_PROMPT_READY", "HUMAN_REQUIRED", "EXECUTOR_CRASHED"}
DOCUMENTATION_DISPOSITIONS = {"NOT_REQUIRED", "REQUIRED", "COMPLETE"}
EXECUTOR_WORKTREE_RE = re.compile(r"(?im)^[ \t]*WORKTREE[ \t]*\r?\n[ \t]*(?P<path>(?:[A-Za-z]:[\\/]|/)[^\r\n]+)[ \t]*(?=\r?$|\r?\n)")
ORCHESTRATOR_RESULT_RE = re.compile(
    r"<ORCHESTRATOR_RESULT>\s*"
    r"classification=(ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT)\s*"
    r"action=(EXECUTE|HUMAN_REQUIRED|STOP)\s*"
    r"taskId=([^\r\n]+)\s*"
    r"(?:documentation=(NOT_REQUIRED|REQUIRED|COMPLETE)\s*)?"
    r"promptBegin\s*\r?\n?(.*?)\r?\npromptEnd\s*"
    r"</ORCHESTRATOR_RESULT>\s*$",
    re.S,
)
PROJECT_RE = re.compile(r"(?im)^\s*repository\s*=\s*(.+?)\s*$")
BRANCH_RE = re.compile(r"(?im)^\s*branch\s*=\s*(.+?)\s*$")
AUTHORITY_HEAD_RE = re.compile(r"(?im)^\s*currentBranchHeadAtArchitectDecision\s*=\s*([0-9a-f]{40})\s*$")


def atomic_write(path: str | os.PathLike[str], data: bytes) -> None:
    """Durably replace one local file without exposing a partial document."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def parse_orchestrator_result(text: str, completed_task_id: str) -> dict[str, str]:
    candidate = text.rstrip()
    if candidate.endswith(COMPLETE):
        candidate = candidate[: -len(COMPLETE)].rstrip()
    match = ORCHESTRATOR_RESULT_RE.search(candidate)
    if not match or match.group(3).strip() != completed_task_id:
        raise ValueError("ARCHITECT_ENVELOPE_INVALID")
    prompt = match.group(5).replace("\r\n", "\n").replace("\r", "\n")
    action = match.group(2)
    if action == "EXECUTE" and not prompt.strip():
        raise ValueError("ARCHITECT_ENVELOPE_PROMPT_REQUIRED")
    if action != "EXECUTE" and prompt.strip():
        raise ValueError("ARCHITECT_ENVELOPE_PROMPT_FORBIDDEN")
    return {"classification": match.group(1), "action": action, "taskId": completed_task_id, "prompt": prompt, "documentation": match.group(4) or "NOT_REQUIRED"}


def repository_evidence(repo: str | os.PathLike[str]) -> dict[str, str]:
    def git(*args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8", errors="replace").strip()
        except (OSError, subprocess.CalledProcessError):
            return "UNAVAILABLE"
    status = git("status", "--porcelain")
    return {"head": git("rev-parse", "HEAD"), "statusPorcelain": status, "changedFileSummary": status}


def resolve_executor_worktree(prompt: str, fallback_project: str | os.PathLike[str] | None = None) -> str:
    """Resolve an explicit Executor WORKTREE and fail closed if it is invalid."""
    match = EXECUTOR_WORKTREE_RE.search(prompt)
    candidate = match.group("path").strip().strip("`") if match else (str(fallback_project) if fallback_project is not None else None)
    if not candidate:
        raise RuntimeError("EXECUTOR_WORKTREE_MISSING")
    path = Path(candidate)
    if not path.is_absolute():
        raise RuntimeError("EXECUTOR_WORKTREE_NOT_ABSOLUTE")
    if not path.is_dir():
        raise RuntimeError(f"EXECUTOR_WORKTREE_INVALID:{candidate}")
    return str(path)


class LocalFirstOrchestrator:
    """Small durable local control loop; GitHub is deliberately absent from it."""
    def __init__(self, project_dir: str, state_dir: str | os.PathLike[str] | None = None, process_factory: Callable[[str, Path], Any] | None = None):
        self.project_dir = Path(project_dir)
        self.state_dir = Path(state_dir or self.project_dir / ".agent-work" / "orchestrator")
        self.results_dir, self.prompts_dir, self.logs_dir = (self.state_dir / name for name in ("results", "prompts", "logs"))
        self.inbox_dir = self.state_dir / "inbox"
        self.state_path = self.state_dir / "state.json"
        self.process_factory = process_factory
        self._live_bottom_recovery_attempted: set[str] = set()
        self.state = self._load_state()
        self.state.setdefault("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)
        self.state.setdefault("executorSessionMode", "PERSISTENT")
        root_pid = os.environ.get("ARCHITECT_BROWSER_ROOT_PID")
        if root_pid:
            try:
                self.architect_browser_root_pid = int(root_pid)
            except ValueError as error:
                raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED") from error
            if self.architect_browser_root_pid <= 0:
                raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
        else:
            self.architect_browser_root_pid = None
        self.architect_memory_reader = (lambda: architect_process_tree_memory_bytes(self.architect_browser_root_pid)) if self.architect_browser_root_pid else None
        self.memory_ownership_required = not bool(root_pid)
        if not root_pid:
            self.state["architectMemoryOwnership"] = "UNCONFIGURED"
            self.state["architectMemoryError"] = "ARCHITECT_BROWSER_ROOT_PID_REQUIRED"
        else:
            self.state["architectMemoryOwnership"] = "CONFIGURED"
            self.state.pop("architectMemoryError", None)
        self.state.setdefault("memoryThresholdBytes", ARCHITECT_MEMORY_THRESHOLD_BYTES)
        self.session_rollover = ArchitectSessionRollover(self)

    def bind_architect_memory_owner(self, endpoint: str, emit: Callable[[str], None] = print) -> int:
        """Bind rollover sampling to the explicit PID or exact CDP listener owner."""
        if self.architect_browser_root_pid is not None:
            pid = self.architect_browser_root_pid
            source = "ENVIRONMENT"
        else:
            pid = resolve_architect_browser_root_pid(endpoint)
            source = "CDP_LISTENER"
        self.architect_browser_root_pid = pid
        self.architect_memory_reader = lambda: architect_process_tree_memory_bytes(pid)
        self.memory_ownership_required = False
        self.state["architectMemoryOwnership"] = "CONFIGURED"
        self.state["architectMemoryOwnershipSource"] = source
        self.state["architectBrowserRootPid"] = pid
        self.state.pop("architectMemoryError", None)
        self.state.pop("architectMemoryOwnershipWarningEmitted", None)
        self.save()
        emit(f"ARCHITECT_MEMORY_OWNER pid={pid} source={source}")
        return pid

    def _configured_fallback_project(self) -> Path | None:
        """Use a caller-provided project root, never this Orchestrator source root."""
        if self.project_dir.resolve() == Path(__file__).resolve().parent:
            return None
        return self.project_dir

    def _project_config_path(self) -> Path:
        return self.state_dir / "project-config.json"

    def _project_config(self) -> dict[str, Any]:
        path = self._project_config_path()
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("projects"), dict):
                raise RuntimeError("PROJECT_CONFIG_INVALID")
            migrated = False
            for spec in value["projects"].values():
                if isinstance(spec, dict) and "branch" in spec:
                    spec.setdefault("defaultBranch", spec["branch"])
                    spec.pop("branch", None)
                    migrated = True
            if migrated:
                atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            return value
        base = Path(AFFOTECH_CHILD_PROJECT_DIR)
        if not base.is_dir():
            raise RuntimeError("PROJECT_CONFIG_MISSING")
        branch = self._git(base, "branch", "--show-current")
        if not branch:
            raise RuntimeError("PROJECT_CONFIG_BRANCH_MISSING")
        repository = AFFOTECH_CHILD_REMOTE
        config = {"version": 1, "projects": {self._repository_key(repository): {
            "repository": repository,
            "baseRepo": str(base), "defaultBranch": branch, "remote": "origin"
        }}}
        atomic_write(path, (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        return config

    @staticmethod
    def _repository_key(value: str) -> str:
        key = re.split(r"[/\\]", value.rstrip("/\\"))[-1]
        return key[:-4] if key.lower().endswith(".git") else key

    def _project_spec(self, prompt: str, context_task_id: str | None = None) -> dict[str, Any] | None:
        project_match = PROJECT_RE.search(prompt)
        config = self._project_config()
        projects = config["projects"]
        if project_match:
            requested_repository = project_match.group(1).strip().strip("`")
            key = self._repository_key(requested_repository)
        else:
            record = self.state.get("taskWorktrees", {}).get(context_task_id or "")
            key = str(record.get("project", "")) if isinstance(record, dict) else ""
            if not key:
                if context_task_id:
                    raise RuntimeError("EXECUTOR_PROJECT_CONTEXT_MISSING")
                return None
        spec = projects.get(key)
        if not isinstance(spec, dict):
            if project_match:
                raise RuntimeError(f"PROJECT_CONFIG_UNKNOWN:{requested_repository}")
            raise RuntimeError("EXECUTOR_PROJECT_CONTEXT_MISSING")
        configured_repository = str(spec.get("repository", ""))
        if self._repository_key(configured_repository) != key:
            raise RuntimeError("PROJECT_CONFIG_REPOSITORY_MISMATCH")
        branch_match = BRANCH_RE.search(prompt)
        requested_branch = branch_match.group(1).strip() if branch_match else None
        record = self.state.get("taskWorktrees", {}).get(context_task_id or "")
        inherited_branch = record.get("branch") if isinstance(record, dict) else None
        return {**spec, "key": key, "repository": configured_repository, "branch": requested_branch or inherited_branch or spec.get("defaultBranch")}

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8", errors="strict").strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"GIT_COMMAND_FAILED:{' '.join(args)}") from error

    def _owned_task_worktree(self, task_id: str, prompt: str, context_task_id: str | None = None) -> Path | None:
        spec = self._project_spec(prompt, context_task_id)
        if spec is None:
            return None
        base = Path(str(spec.get("baseRepo", ""))).resolve()
        if not base.is_dir() or self._repository_key(self._git(base, "config", "--get", f"remote.{spec.get('remote', 'origin')}.url")) != self._repository_key(str(spec["repository"])):
            raise RuntimeError("PROJECT_BASE_REPOSITORY_INVALID")
        remote = str(spec.get("remote", "origin"))
        branch = str(spec.get("branch") or "")
        if not branch:
            raise RuntimeError("EXECUTOR_BRANCH_MISSING")
        try:
            subprocess.run(["git", "-C", str(base), "fetch", "--quiet", remote, branch], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("EXECUTOR_SOURCE_FETCH_FAILED") from error
        remote_ref = f"refs/remotes/{remote}/{branch}"
        base_commit = self._git(base, "rev-parse", remote_ref)
        expected_head = AUTHORITY_HEAD_RE.search(prompt)
        if expected_head and base_commit.lower() != expected_head.group(1).lower():
            raise RuntimeError("EXECUTOR_SOURCE_AUTHORITY_ADVANCED")
        root = (self.state_dir / "worktrees").resolve()
        path = (root / task_id).resolve()
        ownership = self.state.setdefault("taskWorktrees", {})
        existing = ownership.get(task_id)
        if existing:
            if Path(str(existing.get("worktreePath", ""))).resolve() != path or existing.get("baseRepo") != str(base):
                raise RuntimeError("EXECUTOR_WORKTREE_OWNERSHIP_MISMATCH")
            if not path.is_dir() or Path(self._git(path, "rev-parse", "--show-toplevel")).resolve() != path:
                raise RuntimeError("EXECUTOR_WORKTREE_INVALID")
            if self._git(path, "rev-parse", "HEAD") != base_commit:
                raise RuntimeError("EXECUTOR_WORKTREE_SOURCE_MISMATCH")
            return path
        if path.exists():
            raise RuntimeError("EXECUTOR_WORKTREE_OWNERSHIP_CONFLICT")
        root.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "-C", str(base), "worktree", "add", "--detach", str(path), base_commit], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("EXECUTOR_WORKTREE_CREATE_FAILED") from error
        ownership[task_id] = {"taskId": task_id, "project": spec["key"], "baseRepo": str(base), "branch": branch, "baseCommit": base_commit, "worktreePath": str(path)}
        self.state.update({"taskWorktree": ownership[task_id], "targetProject": str(path), "targetRepo": str(path), "targetWorktree": str(path)})
        self.save()
        return path

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"state": "IDLE", "targetProject": str(self.project_dir), "targetRepo": str(self.project_dir)}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("ORCHESTRATOR_STATE_INVALID")
        value.setdefault("state", "IDLE")
        return value

    def save(self) -> None:
        atomic_write(self.state_path, (json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    def discussion_pause_active(self) -> bool:
        marker = self.state_dir / "discussion-pause.marker"
        try:
            value = marker.read_text(encoding="ascii").strip()
            if value in {"PAUSED", "RESUMED"}:
                return value == "PAUSED"
        except OSError:
            pass
        return bool(self.state.get("discussionPauseActive"))

    def _write_discussion_pause_marker(self, active: bool) -> None:
        atomic_write(self.state_dir / "discussion-pause.marker", ("PAUSED\n" if active else "RESUMED\n").encode("ascii"))

    def request_discussion_pause(self) -> None:
        if self.discussion_pause_active():
            return
        self._write_discussion_pause_marker(True)
        self.state["discussionPauseActive"] = True
        self.save()
        task_id = self.state.get("taskId") or self.state.get("nextTaskId") or "NONE"
        print(f"ORCHESTRATOR PAUSED BY HUMAN state={self.state.get('state')} taskId={task_id} F10=RESUME")
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_DISCUSSION_PAUSE_REQUESTED", self.state, taskId=task_id)

    def request_discussion_resume(self) -> None:
        if not self.discussion_pause_active():
            return
        self._write_discussion_pause_marker(False)
        self.state["discussionPauseActive"] = False
        self.save()
        task_id = self.state.get("taskId") or self.state.get("nextTaskId") or "NONE"
        print(f"ORCHESTRATOR RESUMED BY HUMAN state={self.state.get('state')} taskId={task_id}")
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_DISCUSSION_RESUME_REQUESTED", self.state, taskId=task_id)

    def request_remote_discussion_pause(self) -> None:
        if self.discussion_pause_active():
            return
        self._write_discussion_pause_marker(True)
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "REMOTE_DISCUSSION_PAUSE_REQUESTED", self.state)

    def request_remote_discussion_resume(self) -> None:
        if not self.discussion_pause_active():
            return
        self._write_discussion_pause_marker(False)
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "REMOTE_DISCUSSION_RESUME_REQUESTED", self.state)


    def _result_path(self, task_id: str) -> Path:
        return self.results_dir / f"{task_id}.txt"

    def _reset_format_recovery_for_task(self, task_id: str) -> None:
        if self.state.get("architectFormatRecoveryTaskId") != task_id:
            self.state.update({"formatRecoveryCount": 0, "formatRecoveryExhausted": False, "architectFormatRecoveryTaskId": None})

    def _record_executor_success(self, task_id: str, result: Path, exit_code: int | None = None) -> None:
        self.state.update({
            "state": "RESULT_READY",
            "taskId": task_id,
            "lastCompletedTaskId": task_id,
            "executorResultPath": str(result),
            "executorExitCode": exit_code,
            "executorProcessState": "COMPLETED_WITH_RESULT",
            "executorFailureClass": None,
            "executorCrash": None,
            "humanRequiredReason": None,
            "repositoryEvidence": repository_evidence(self.project_dir),
            "formatRecoveryCount": 0,
            "formatRecoveryExhausted": False,
            "architectFormatRecoveryTaskId": None,
        })

    def intake_inbox(self, launcher: Callable[[str, Path], Any]) -> bool:
        """Consume and launch one approved local prompt while IDLE."""
        if self.state.get("state") != "IDLE" or self.discussion_pause_active():
            return False
        consumed = self.state.setdefault("consumedInboxItems", {})
        failures = self.state.setdefault("inboxFailures", {})
        for source in sorted(self.inbox_dir.glob("*.txt")):
            identity = source.stem
            if identity in consumed or identity in failures:
                continue
            try:
                prompt = source.read_text(encoding="utf-8")
                if not prompt.strip():
                    raise RuntimeError("INBOX_PROMPT_EMPTY")
                target = resolve_executor_worktree(prompt, self.project_dir)
                canonical = self.prompts_dir / f"{identity}.txt"
                if canonical.exists():
                    raise RuntimeError("INBOX_IDENTITY_ALREADY_CANONICAL")
                atomic_write(canonical, prompt.encode("utf-8"))
                consumed[identity] = {"source": str(source), "canonicalPrompt": str(canonical), "status": "LAUNCH_AUTHORIZED"}
                self.state.update({"state": "NEXT_PROMPT_READY", "taskId": identity, "nextTaskId": identity, "nextPromptPath": str(canonical), "targetProject": target, "targetRepo": target, "targetWorktree": target})
                self.save()
                return True
            except (OSError, UnicodeError, RuntimeError) as error:
                failures[identity] = {"source": str(source), "error": str(error)}
                self.state.update({"state": "HUMAN_REQUIRED", "inboxFailure": str(error)})
                self.save()
                return True
        return False

    def _architect_response_task_id(self, response: str) -> str | None:
        candidate = response.rstrip()
        if candidate.endswith(COMPLETE):
            candidate = candidate[: -len(COMPLETE)].rstrip()
        match = ORCHESTRATOR_RESULT_RE.search(candidate)
        return match.group(3).strip() if match else None

    def _architect_response_attempts_authority(self, response: str) -> bool:
        """Recognize only the canonical opening marker, not ordinary prose."""
        return "<ORCHESTRATOR_RESULT>" in response

    def _idle_invalid_envelope(self, response: str) -> bool:
        if not self._architect_response_attempts_authority(response):
            return False
        if not str(self.state.get("taskId") or ""):
            return False
        try:
            parse_orchestrator_result(response, str(self.state.get("taskId") or ""))
        except ValueError:
            return True
        return False

    def reject_invalid_handover_response(self) -> None:
        self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_HANDOVER_RESPONSE_INVALID"})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_HANDOVER_RESPONSE_INVALID", self.state)

    def defer_failed_rollover(self, reason: str = "ARCHITECT_HANDOVER_RESPONSE_INVALID") -> None:
        """Drop a failed maintenance attempt without changing workflow authority."""
        self.state.update({"rolloverInProgress": False, "rolloverDue": True, "rolloverPending": True, "handoverRequested": False, "handoverReady": False})
        self.state.pop("pending_handover", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ROLLOVER_MAINTENANCE_FAILED", self.state, reason=reason)

    def process_pending_handover_response(self, bridge: Any, response: str) -> bool:
        """Keep pending handover responses out of ordinary task decision parsing."""
        if not self.state.get("handoverRequested"):
            return False
        if self.session_rollover.complete_from_response(bridge, response):
            return True
        if self.state.get("rolloverInProgress") or (self.state.get("rolloverDue") and self.state.get("rolloverPending")):
            self.defer_failed_rollover()
            return False
        self.reject_invalid_handover_response()
        return False

    def recover_pending_rollover_handover(self, bridge: Any) -> bool:
        """Recover one already-generated handover after the known format-recovery incident."""
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        result_ready = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        eligible = (
            self.state.get("state") == "HUMAN_REQUIRED"
            and self.state.get("humanRequiredReason") == "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED"
            and self.state.get("handoverRequested") is True
            and self.state.get("rolloverPending") is True
            and task_id
            and task_id == str(self.state.get("lastCompletedTaskId") or "")
            and result_ready
            and not executor_active
        )
        if not eligible:
            return False
        entries = bridge._assistant_entries()
        handover = next((entry.get("text") for entry in reversed(entries) if isinstance(entry, dict) and isinstance(entry.get("text"), str) and architect_handover_ready(entry["text"])), None)
        if not handover:
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_HANDOVER_RESPONSE_INVALID", self.state, reason="ARCHITECT_HANDOVER_NOT_FOUND")
            return False
        if not self.session_rollover.complete_from_response(bridge, handover):
            return False
        self.state.update({"state": "RESULT_READY", "taskId": task_id, "lastCompletedTaskId": task_id, "executorResultPath": result_path, "humanRequiredReason": None})
        self.save()
        self.deliver_result(bridge)
        return True

    def recover_preempted_rollover_failure(self) -> bool:
        """Remove only maintenance contamination from an interrupted result recovery."""
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            result_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        try:
            executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        except (TypeError, ValueError):
            executor_active = True
        if (self.state.get("state") != "HUMAN_REQUIRED"
                or self.state.get("humanRequiredReason") not in {"ARCHITECT_HANDOVER_RESPONSE_INVALID", "ARCHITECT_NEW_CONVERSATION_ACK_INVALID", "ARCHITECT_NEW_CONVERSATION_ACK_TIMEOUT"}
                or not task_id or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not result_ready or not self.state.get("architectDeliveryPayloadHash")
                or self.state.get("architectSendState") not in {"PENDING", "FAILED", "AMBIGUOUS", "CONFIRMED"}
                or self.state.get("nextPromptPath")
                or (self.state.get("nextTaskId") and self.state.get("nextTaskId") not in {task_id, str(self.state.get("lastCompletedTaskId") or "")})
                or executor_active):
            return False
        confirmed = self.state.get("architectSendState") == "CONFIRMED"
        self.state.update({
            "state": "ARCHITECT_RUNNING" if confirmed else "HUMAN_REQUIRED",
            "humanRequiredReason": None if confirmed else "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED",
            "handoverRequested": False,
            "handoverReady": False,
            "rolloverInProgress": False,
            "rolloverDue": True,
            "rolloverPending": False,
        })
        self.state.pop("pending_handover", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ROLLOVER_MAINTENANCE_RECOVERY", self.state, taskId=task_id)
        return True

    def recover_completed_confirmed_workflow(self) -> bool:
        """Restore a completed result workflow from durable facts, independent of its old error label."""
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            result_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        try:
            executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        except (TypeError, ValueError):
            executor_active = True
        if (self.state.get("state") != "HUMAN_REQUIRED" or not task_id
                or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not result_ready or not self.state.get("architectDeliveryPayloadHash")
                or self.state.get("architectSendState") != "CONFIRMED"
                or self.state.get("nextPromptPath")
                or (self.state.get("nextTaskId") and self.state.get("nextTaskId") not in {task_id, str(self.state.get("lastCompletedTaskId") or "")})
                or executor_active):
            return False
        self.state.update({"state": "ARCHITECT_RUNNING", "humanRequiredReason": None,
                           "handoverRequested": False, "handoverReady": False,
                           "rolloverInProgress": False, "rolloverPending": False})
        self.state.pop("pending_handover", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "COMPLETED_CONFIRMED_WORKFLOW_RECOVERED", self.state, taskId=task_id)
        return True

    def _fail_closed_idle_envelope(self, response: str, fingerprint: str | None) -> str:
        self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_ENVELOPE_INVALID"})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_INVALID", self.state, hash=fingerprint)
        print("IDLE_GATE=invalid_architect_envelope taskId=%s responseHash=%s correctionRequired=True" % (self.state.get("taskId"), fingerprint))
        return "HUMAN_REQUIRED"

    def consume_idle_architect_response(self, response: str, launcher: Callable[[str, Path], Any] | None = None) -> str:
        """Use the canonical Architect decision staging path after IDLE recovery."""
        return self.accept_architect_response(response)["action"]

    def _prelaunch_incomplete(self, task_id: str) -> bool:
        """Recognize an EXECUTE transaction that never reached a child or result."""
        if task_id == self.state.get("lastCompletedTaskId"):
            return False
        if self.state.get("executorLaunchState") in {"LAUNCHED", "POSTLAUNCH_NO_RESULT"} or self.state.get("prelaunchRecoveryState") == "RECOVERED_AND_LAUNCHED":
            return False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if active_pid:
            try:
                if LocalWatcher.process_alive(int(active_pid)):
                    return False
            except (TypeError, ValueError):
                return False
        result_path = self.state.get("executorResultPath")
        result_exists = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        return not result_exists

    def _validate_prelaunch_recovery(self, task_id: str, prompt: str) -> Path | None:
        """Require unchanged task inputs before any same-task automatic relaunch."""
        result_path = self.state.get("executorResultPath")
        if isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip():
            raise RuntimeError("RECOVERY_RESULT_ALREADY_EXISTS")
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if active_pid and LocalWatcher.process_alive(int(active_pid)):
            raise RuntimeError("RECOVERY_CHILD_STILL_ALIVE")
        record = self.state.get("taskWorktrees", {}).get(task_id)
        if not isinstance(record, dict):
            return None
        expected_head = str(record.get("baseCommit", ""))
        persisted_path = Path(str(record.get("worktreePath", ""))).resolve()
        if not persisted_path.is_dir():
            raise RuntimeError("RECOVERY_WORKTREE_MISSING")
        if self._git(persisted_path, "rev-parse", "HEAD").lower() != expected_head.lower():
            raise RuntimeError("RECOVERY_WORKTREE_HEAD_CHANGED")
        if self._git(persisted_path, "status", "--porcelain"):
            raise RuntimeError("RECOVERY_WORKTREE_DIRTY")
        try:
            owned = self._owned_task_worktree(task_id, prompt)
        except RuntimeError as error:
            if "AUTHORITY_ADVANCED" in str(error) or "SOURCE_MISMATCH" in str(error):
                raise RuntimeError("RECOVERY_SOURCE_ADVANCED") from error
            raise
        if self._git(owned, "rev-parse", "HEAD").lower() != expected_head.lower():
            raise RuntimeError("RECOVERY_WORKTREE_HEAD_CHANGED")
        if self._git(owned, "status", "--porcelain"):
            raise RuntimeError("RECOVERY_WORKTREE_DIRTY")
        return owned

    def recover_prelaunch_incomplete(self, launcher: Callable[[str, Path], Any]) -> Any | None:
        """Recover one safely verified EXECUTE that died before producing a result."""
        task_id = str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
        if not task_id or task_id == self.state.get("lastCompletedTaskId"):
            return None
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if active_pid:
            if LocalWatcher.process_alive(int(active_pid)):
                self.state.update({"state": "HUMAN_REQUIRED", "prelaunchRecoveryError": "RECOVERY_CHILD_STILL_ALIVE", "automaticRetryAuthorized": False})
                self.save()
                return None
            self.state.update({"state": "HUMAN_REQUIRED", "executorLaunchState": "POSTLAUNCH_NO_RESULT", "automaticRetryAuthorized": False})
            self.save()
            print("EXECUTOR_RETRY_BLOCKED reason=POSTLAUNCH_NO_RESULT taskId=%s" % task_id)
            return None
        prompt_path = self.state.get("nextPromptPath")
        if not isinstance(prompt_path, str) or not Path(prompt_path).is_file():
            self.state["prelaunchRecoveryError"] = "PRELAUNCH_PROMPT_MISSING"
            self.save()
            return None
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
            owned = self._validate_prelaunch_recovery(task_id, prompt)
            if owned is None:
                raise RuntimeError("PRELAUNCH_WORKTREE_NOT_OWNED")
            self.state.update({"state": "NEXT_PROMPT_READY", "taskId": task_id, "nextTaskId": task_id, "targetProject": str(owned), "targetRepo": str(owned), "targetWorktree": str(owned), "prelaunchRecoveryState": "PRELAUNCH_INCOMPLETE"})
            self.save()
            self.state["prelaunchRecoveryState"] = "RECOVERABLE"
            self.save()
            return True
        except (OSError, UnicodeError, RuntimeError) as error:
            self.state.update({"state": "HUMAN_REQUIRED", "prelaunchRecoveryError": str(error)})
            self.save()
            return None

    def authorize_postlaunch_retry(self, launcher: Callable[[str, Path], Any]) -> Any | None:
        """Perform one explicit human-authorized retry of a post-launch failure."""
        task_id = str(self.state.get("taskId") or "")
        supplied = os.environ.get("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY")
        if self.state.get("state") != "HUMAN_REQUIRED" or self.state.get("executorLaunchState") != "POSTLAUNCH_NO_RESULT":
            return None
        if not supplied:
            return None
        if supplied != task_id:
            self.state["humanRecoveryAuthorizationError"] = "HUMAN_RECOVERY_TASK_MISMATCH"
            self.save()
            return None
        if self.state.get("humanRecoveryAuthorizationConsumed"):
            self.state["humanRecoveryAuthorizationError"] = "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED"
            self.save()
            return None
        prompt_path = self.state.get("nextPromptPath")
        if not isinstance(prompt_path, str) or not Path(prompt_path).is_file():
            self.state.update({"humanRecoveryAuthorizationError": "RECOVERY_PROMPT_MISSING", "automaticRetryAuthorized": False})
            self.save()
            return None
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
            owned = self._validate_prelaunch_recovery(task_id, prompt)
            if owned is None:
                raise RuntimeError("PRELAUNCH_WORKTREE_NOT_OWNED")
            verify_executor_session(str(self.state.get("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)))
            self.state.update({"humanRecoveryAuthorizationConsumed": True, "humanRecoveryAuthorizedTaskId": task_id, "automaticRetryAuthorized": False, "state": "NEXT_PROMPT_READY", "nextTaskId": task_id, "targetProject": str(owned), "targetRepo": str(owned), "targetWorktree": str(owned)})
            self.save()
            return True
        except (OSError, UnicodeError, RuntimeError) as error:
            self.state.update({"state": "HUMAN_REQUIRED", "automaticRetryAuthorized": False, "humanRecoveryAuthorizationError": str(error)})
            self.save()
            return None

    def request_architect_bootstrap(self, bridge: Any) -> bool:
        """Ask Architect once to evaluate current project state and choose the next action."""
        if self.discussion_pause_active():
            return False
        source_fingerprint = self.state.get("continuationSourceFingerprint")
        if source_fingerprint and source_fingerprint == self.state.get("lastContinuationSourceFingerprint"):
            self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False})
            self.save()
            print("IDLE_GATE=continuation_fingerprint_duplicate")
            return False
        message = "\n".join([
            "Review the current authoritative project state after the completed work.",
            "You are the project Architect.",
            "Decide the next bounded action according to the project's existing governance, roadmap, accepted milestones, documentation, and current authoritative state.",
            "Do not repeat already accepted work.",
            "Do not invent work outside the established project direction.",
            "If an Executor or Documentation Curator action is appropriate, return the complete next instruction using the canonical ORCHESTRATOR_RESULT envelope.",
            "Set documentation=NOT_REQUIRED|REQUIRED|COMPLETE.",
            "NOT_REQUIRED means this decision does not require milestone/release documentation closure.",
            "REQUIRED means the accepted milestone/release needs one bounded documentation closure task before ordinary advancement.",
            "COMPLETE means required milestone documentation is already synchronized and verified.",
            "Do not choose NOT_REQUIRED merely to continue execution; use REQUIRED or COMPLETE when governance requires it.",
            "If genuine human authority is required, return HUMAN_REQUIRED.",
            "Return STOP only if there is genuinely no further currently authorized project work.",
            "<ORCHESTRATOR_RESULT>",
            "classification=ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT",
            "action=EXECUTE|HUMAN_REQUIRED|STOP",
            "taskId=<task id>",
            "documentation=NOT_REQUIRED|REQUIRED|COMPLETE",
            "promptBegin <complete Executor prompt only when action=EXECUTE>",
            "promptEnd",
            "</ORCHESTRATOR_RESULT>",
        ])
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            source_fingerprint = "UNSPECIFIED"
        self.state.update({"lastContinuationSourceFingerprint": source_fingerprint, "architectBootstrapCount": int(self.state.get("architectBootstrapCount", 0)) + 1, "architectBootstrapAwaiting": True, "architectSendState": "PENDING", "state": "IDLE"})
        self.save()
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        try:
            sender(message)
        except Exception as error:
            attempted = bool(getattr(bridge, "sendActionAttempted", False)) or bool(getattr(bridge, "last_send_method", None))
            ambiguous = attempted or (isinstance(error, ResultSubmissionError) and error.code == "ARCHITECT_SUBMISSION_ACK_TIMEOUT")
            if isinstance(error, ResultSubmissionError) and not attempted:
                if bool(getattr(bridge, "last_unsent_payload_cleared", False)):
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_UNSENT_PAYLOAD_CLEARED", self.state, taskId=self.state.get("taskId"))
                self.state.pop("lastContinuationSourceFingerprint", None)
                self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False, "architectSendState": "FAILED"})
                self.save()
                print("IDLE_GATE=pre_send_failed")
                return False
            if not ambiguous:
                self.state.pop("lastContinuationSourceFingerprint", None)
                self.state["architectSendState"] = "FAILED"
            else:
                self.state["architectSendState"] = "AMBIGUOUS"
            self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False})
            self.save()
            print("IDLE_GATE=send_failed")
            raise
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectContactCount": int(self.state.get("architectContactCount", 0)) + 1})
        self.save()
        if hasattr(bridge, "assistant_baseline"):
            self.state["architectBaseline"] = bridge.assistant_baseline()
            self.save()
        print("IDLE_GATE=continuation_sent")
        return True

    def inspect_idle_architect(self, bridge: Any, launcher: Callable[[str, Path], Any]) -> str:
        """Inspect the configured Architect conversation while IDLE."""
        generation_visible = bridge.generation_visible()
        if generation_visible:
            self.state.update({"state": "ARCHITECT_RUNNING", "architectBaseline": bridge.assistant_baseline()})
            self.save()
            print("IDLE_GATE=generation_visible")
            print("IDLE_DIAGNOSTIC latestResponseFound=False latestResponseId=None latestResponseFingerprint=None latestResponseHasEnvelope=False generationVisible=True architectBootstrapAwaiting=%s architectBootstrapCount=%s architectContactCount=%s architectSendState=%s architectResultFingerprint=%s continuationSourceFingerprint=%s lastContinuationSourceFingerprint=%s responseConsumed=False continuationEligible=False requestArchitectBootstrapCalled=False requestArchitectBootstrapSent=False resultingState=%s" % (self.state.get("architectBootstrapAwaiting"), self.state.get("architectBootstrapCount", 0), self.state.get("architectContactCount", 0), self.state.get("architectSendState"), self.state.get("architectResultFingerprint"), self.state.get("continuationSourceFingerprint"), self.state.get("lastContinuationSourceFingerprint"), self.state.get("state")))
            return "ARCHITECT_RUNNING"
        def latest_substantive(entries: list[dict[str, Any]]) -> tuple[str, str | None]:
            substantive = [
                entry for entry in entries
                if not str(entry.get("id") or "").startswith("request-placeholder-")
                and (entry.get("text") or "").strip().lower() != "thinking"
            ]
            if not substantive:
                return "", None
            return substantive[-1].get("text", ""), substantive[-1].get("id")

        response, response_id = latest_substantive(bridge._assistant_entries())
        response_fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest() if response else None
        response_has_envelope = bool(self._architect_response_task_id(response)) if response else False
        consumed = self.state.setdefault("consumedArchitectResponses", {})
        response_consumed = bool(response_fingerprint and response_fingerprint in consumed)
        if response and not response_consumed and self._idle_invalid_envelope(response):
            return self._fail_closed_idle_envelope(response, response_fingerprint)
        continuation_eligible = not response_has_envelope and not response_consumed and response_fingerprint != self.state.get("lastContinuationSourceFingerprint")
        if response:
            consumed_record = consumed.get(response_fingerprint, {}) if response_fingerprint else {}
            origin = consumed_record.get("origin") if isinstance(consumed_record, dict) else None
            if response_consumed:
                legacy_review = (
                    not origin
                    and str(self.state.get("taskId") or "") == str(self.state.get("lastCompletedTaskId") or "")
                    and str(self.state.get("architectDeliveryTaskId") or "") == str(self.state.get("taskId") or "")
                    and self.state.get("architectSendState") == "CONFIRMED"
                    and isinstance(self.state.get("executorResultPath"), str)
                    and Path(self.state["executorResultPath"]).is_file()
                    and Path(self.state["executorResultPath"]).read_text(encoding="utf-8", errors="replace").strip()
                    and self.state.get("state") == "IDLE"
                )
                if origin == "RESULT_REVIEW" or legacy_review:
                    if legacy_review:
                        consumed[response_fingerprint]["origin"] = "RESULT_REVIEW"
                        self.save()
                    if response_fingerprint != self.state.get("lastContinuationSourceFingerprint"):
                        self.state["continuationSourceFingerprint"] = response_fingerprint
                        self.save()
                        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONSUMED_RESPONSE", self.state, hash=response_fingerprint, origin="RESULT_REVIEW")
                        sent = self.request_architect_bootstrap(bridge)
                        if sent:
                            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONTINUATION_BOOTSTRAP_REQUESTED", self.state, hash=response_fingerprint, origin="RESULT_REVIEW")
                            return "ARCHITECT_RUNNING"
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONTINUATION_SUPPRESSED", self.state, hash=response_fingerprint, origin="RESULT_REVIEW", reason="already_requested")
                    return "IDLE"
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONTINUATION_SUPPRESSED", self.state, hash=response_fingerprint, origin=origin or "BOOTSTRAP", reason="consumed")
                return "IDLE"
            for attempt in range(2):
                try:
                    result = self.consume_idle_architect_response(response, launcher)
                except ValueError:
                    break
                if result != "DUPLICATE":
                    print("IDLE_GATE=valid_envelope_processed")
                    print("IDLE_DIAGNOSTIC latestResponseFound=True latestResponseId=%s latestResponseFingerprint=%s latestResponseHasEnvelope=True generationVisible=False architectBootstrapAwaiting=%s architectBootstrapCount=%s architectContactCount=%s architectSendState=%s architectResultFingerprint=%s continuationSourceFingerprint=%s lastContinuationSourceFingerprint=%s responseConsumed=%s continuationEligible=False requestArchitectBootstrapCalled=False requestArchitectBootstrapSent=False resultingState=%s" % (response_id, response_fingerprint, self.state.get("architectBootstrapAwaiting"), self.state.get("architectBootstrapCount", 0), self.state.get("architectContactCount", 0), self.state.get("architectSendState"), self.state.get("architectResultFingerprint"), self.state.get("continuationSourceFingerprint"), self.state.get("lastContinuationSourceFingerprint"), response_consumed, self.state.get("state")))
                    return result
                baseline_entries = self.state.get("architectBaseline", {}).get("entries", [])
                baseline_response, baseline_id = latest_substantive(baseline_entries if isinstance(baseline_entries, list) else [])
                baseline_fingerprint = hashlib.sha256(baseline_response.encode("utf-8")).hexdigest() if baseline_response else None
                baseline_is_new = bool(baseline_fingerprint and baseline_fingerprint != response_fingerprint and baseline_fingerprint not in consumed and baseline_fingerprint != self.state.get("lastContinuationSourceFingerprint"))
                if baseline_is_new:
                    self.state.pop("architectLiveBottomFingerprint", None)
                    self.state["continuationSourceFingerprint"] = baseline_fingerprint
                    self.save()
                    print("IDLE_RECOVERY_SOURCE=persisted_baseline")
                    sent = self.request_architect_bootstrap(bridge)
                    if sent:
                        return "ARCHITECT_RUNNING"
                    return self.state.get("state", "IDLE")
                if attempt or response_fingerprint in self._live_bottom_recovery_attempted:
                    print("IDLE_GATE=response_already_consumed")
                    return result
                self._live_bottom_recovery_attempted.add(response_fingerprint)
                self.state.pop("architectLiveBottomFingerprint", None)
                self.save()
                try:
                    print("IDLE_RECOVERY_SOURCE=live_bottom_restore")
                    bridge.restore_live_bottom()
                    response, response_id = latest_substantive(bridge._assistant_entries())
                except Exception:
                    self.save()
                    print("IDLE_GATE=live_bottom_restore_failed")
                    return "DUPLICATE"
                response_fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest() if response else None
                response_has_envelope = bool(self._architect_response_task_id(response)) if response else False
                response_consumed = bool(response_fingerprint and response_fingerprint in consumed)
                if response and not response_consumed and self._idle_invalid_envelope(response):
                    return self._fail_closed_idle_envelope(response, response_fingerprint)
                continuation_eligible = not response_has_envelope and not response_consumed and response_fingerprint != self.state.get("lastContinuationSourceFingerprint")
        self.state["continuationSourceFingerprint"] = hashlib.sha256(response.encode("utf-8")).hexdigest()
        sent = self.request_architect_bootstrap(bridge)
        print("IDLE_DIAGNOSTIC latestResponseFound=%s latestResponseId=%s latestResponseFingerprint=%s latestResponseHasEnvelope=%s generationVisible=False architectBootstrapAwaiting=%s architectBootstrapCount=%s architectContactCount=%s architectSendState=%s architectResultFingerprint=%s continuationSourceFingerprint=%s lastContinuationSourceFingerprint=%s responseConsumed=%s continuationEligible=%s requestArchitectBootstrapCalled=True requestArchitectBootstrapSent=%s resultingState=%s" % (bool(response), response_id, response_fingerprint, response_has_envelope, self.state.get("architectBootstrapAwaiting"), self.state.get("architectBootstrapCount", 0), self.state.get("architectContactCount", 0), self.state.get("architectSendState"), self.state.get("architectResultFingerprint"), self.state.get("continuationSourceFingerprint"), self.state.get("lastContinuationSourceFingerprint"), response_consumed, continuation_eligible, sent, self.state.get("state")))
        if sent:
            return "ARCHITECT_RUNNING"
        print("IDLE_GATE=send_suppressed")
        return self.state.get("state", "IDLE")

    def recover_5c(self, legacy_path: str | os.PathLike[str] = r"C:\Users\nitro\AppData\Local\Temp\codex-last-message-h_2bryhl.txt") -> bool:
        source = Path(legacy_path)
        if not source.is_file() or not source.read_text(encoding="utf-8", errors="replace").strip():
            self.state.update({"state": "HUMAN_REQUIRED", "current5CRecoverableResult": False})
            self.save()
            return False
        destination = self._result_path("PUB-aa3b4121887c4047b3c056bcccaa6a96")
        atomic_write(destination, source.read_bytes())
        self.state.update({"state": "RESULT_READY", "taskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96", "executorResultPath": str(destination), "current5CRecoverableResult": True, "current5CRecoveredResultPath": str(destination), "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96"})
        self.save()
        return True

    def _capture_result(self, task_id: str, source: Path) -> Path:
        destination = self._result_path(task_id)
        if source.resolve() != destination.resolve():
            atomic_write(destination, source.read_bytes())
        if not destination.is_file() or not destination.read_text(encoding="utf-8", errors="replace").strip():
            raise RuntimeError("EXECUTOR_RESULT_MISSING")
        return destination

    def reconcile_executor(self) -> str:
        if self.state.get("state") != "EXECUTOR_RUNNING":
            return self.state.get("state", "IDLE")
        pending_prompt = self.state.get("nextPromptPath")
        if isinstance(pending_prompt, str) and Path(pending_prompt).is_file():
            task_id = str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
            owned = self.state.get("taskWorktrees", {}).get(task_id)
            target = str(owned["worktreePath"]) if isinstance(owned, dict) and owned.get("worktreePath") else resolve_executor_worktree(Path(pending_prompt).read_text(encoding="utf-8"), self._configured_fallback_project())
            self.state.update({"targetProject": target, "targetRepo": target, "targetWorktree": target})
        pid = self.state.get("codexPid")
        if LocalWatcher.process_alive(pid):
            return "EXECUTOR_RUNNING"
        path = self.state.get("executorResultPath")
        if isinstance(path, str) and Path(path).is_file() and Path(path).read_text(encoding="utf-8", errors="replace").strip():
            task_id = str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
            self._record_executor_success(task_id, Path(path), self.state.get("executorExitCode"))
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_RESULT_FOUND", self.state, resultPath=path)
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_READY", self.state)
        else:
            result_exists = bool(isinstance(path, str) and Path(path).is_file())
            process = getattr(self, "_active_process", None)
            exit_code = None
            if process is not None:
                try:
                    exit_code = process.poll()
                except (OSError, AttributeError):
                    pass
            stderr_path = self.state.get("stderrLogPath")
            stderr_summary = ""
            if isinstance(stderr_path, str) and Path(stderr_path).is_file():
                stderr_summary = Path(stderr_path).read_text(encoding="utf-8", errors="replace").strip()[:500]
            failure_class = "EXECUTOR_SESSION_ACTIVE_WRITER" if "already has an active writer" in stderr_summary else "POSTLAUNCH_NO_RESULT"
            self.state.update({
                "state": "EXECUTOR_CRASHED",
                "executorLaunchState": "POSTLAUNCH_NO_RESULT",
                "automaticRetryAuthorized": False,
                "executorProcessState": "EXITED_WITHOUT_RESULT",
                "executorFailureClass": failure_class,
                "executorExitCode": exit_code,
                "stderrSummary": stderr_summary,
                "executorCrash": {
                    "taskId": self.state.get("taskId"),
                    "pid": pid,
                    "targetWorktree": self.state.get("targetWorktree"),
                    "resultPath": path,
                    "resultExists": result_exists,
                    "exitCode": exit_code,
                    "stderrLogPath": stderr_path,
                    "stderrSummary": stderr_summary,
                },
            })
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_RESULT_MISSING", self.state, resultPath=path, pid=pid)
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_CRASHED", self.state, pid=pid, exitCode=exit_code)
            print("EXECUTOR_PROCESS_STATE=EXITED_WITHOUT_RESULT taskId=%s pid=%s exitCode=%s stderrLogPath=%s stderrSummary=%s" % (self.state.get("taskId"), pid, exit_code, stderr_path, stderr_summary))
        self.save()
        return self.state["state"]

    def wait_for_executor(self, poll_interval: float = 2.0) -> str:
        """Remain resident while the independently launched Executor runs."""
        while self.state.get("state") == "EXECUTOR_RUNNING":
            state = self.reconcile_executor()
            if state == "EXECUTOR_RUNNING":
                time.sleep(poll_interval)
            else:
                return state
        return self.state.get("state", "IDLE")

    def wait_for_idle(self, poll_interval: float = 2.0) -> str:
        """Remain resident in normal IDLE until local state requests work."""
        while self.state.get("state") == "IDLE":
            time.sleep(poll_interval)
            self.state = self._load_state()
        return self.state.get("state", "IDLE")

    def mark_executor_started(self, task_id: str, pid: int, result_path: str | os.PathLike[str]) -> None:
        target = self.state.get("targetWorktree") or self.state.get("targetProject")
        attempt = int(self.state.get("executorAttemptNumber", 0)) + 1
        log_path = self.state.get("stderrLogPath") or str(self.state_dir / "executor-logs" / f"{task_id}-attempt-{attempt}.stderr.txt")
        self.state.update({"state": "EXECUTOR_RUNNING", "executorLaunchState": "LAUNCHED", "automaticRetryAuthorized": False, "taskId": task_id, "taskSequence": int(self.state.get("taskSequence", 0)) + 1, "executorAttemptNumber": attempt, "codexPid": pid, "codexStartedAt": time.time(), "targetProject": target, "executorResultPath": str(result_path), "stderrLogPath": str(log_path), "executorFailureClass": None, "executorProcessState": None, "executorCrash": None, "stderrSummary": "", "executorExitCode": None})
        self._active_process = getattr(self, "_active_process", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "CODEX_STARTED", self.state, pid=pid, attempt=attempt)

    def mark_executor_exit(self, exit_code: int, result_path: str | os.PathLike[str]) -> str:
        task_id = str(self.state.get("taskId", "unknown"))
        try:
            result = self._capture_result(task_id, Path(result_path))
        except (OSError, RuntimeError):
            self.state["state"] = "EXECUTOR_CRASHED"
            self.save()
            return "EXECUTOR_CRASHED"
        self._record_executor_success(task_id, result, exit_code)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_RESULT_FOUND", self.state, pid=self.state.get("codexPid"), exitCode=exit_code, resultPath=result_path)
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_READY", self.state)
        return "RESULT_READY"

    def _result_delivery_payload(self) -> tuple[str, str]:
        path = Path(self.state["executorResultPath"])
        report = path.read_text(encoding="utf-8")
        task_id = str(self.state["taskId"])
        instruction = ("Verify the completed Executor report below, classify it, decide the next bounded action, "
                       "and finish with exactly one <ORCHESTRATOR_RESULT> envelope using taskId=" + task_id + ".\n"
            "The envelope must end the response; action=EXECUTE requires the complete next Executor prompt.\n\n")
        instruction += ("The Architect must also set documentation=NOT_REQUIRED|REQUIRED|COMPLETE: "
                        "NOT_REQUIRED when no milestone/release documentation closure applies; "
                        "REQUIRED when accepted work needs one bounded documentation closure task; "
                        "COMPLETE when required documentation is synchronized and verified. "
                        "Do not use NOT_REQUIRED merely to advance.\n\n")
        payload = instruction + report
        return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _delivery_evidence_advanced(self, bridge: Any, payload: str) -> bool:
        def usable_baseline(value: Any) -> bool:
            return isinstance(value, dict) and isinstance(value.get("count"), int) and value.get("count") >= 0 and isinstance(value.get("text_hash"), str) and bool(value.get("text_hash"))

        user_baseline = self.state.get("architectDeliveryUserBaseline")
        if usable_baseline(user_baseline) and callable(getattr(bridge, "user_baseline", None)):
            current = bridge.user_baseline()
            if usable_baseline(current):
                if int(current.get("count", 0)) > int(user_baseline.get("count", 0)):
                    return True
                if current.get("text_hash") and current.get("text_hash") != user_baseline.get("text_hash"):
                    return True
        assistant_baseline = self.state.get("architectDeliveryBaseline")
        if usable_baseline(assistant_baseline) and callable(getattr(bridge, "assistant_baseline", None)):
            current = bridge.assistant_baseline()
            if usable_baseline(current):
                if int(current.get("count", 0)) > int(assistant_baseline.get("count", 0)):
                    return True
                if current.get("text_hash") and current.get("text_hash") != assistant_baseline.get("text_hash"):
                    return True
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            return True
        latest = getattr(bridge, "latest_user_message", None)
        if callable(latest):
            observed = latest()
            if isinstance(observed, str) and observed.strip() and normalize_prompt(observed) == normalize_prompt(payload):
                return True
        return False

    @staticmethod
    def _delivery_tokens(text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9_]+", text)

    def _result_delivery_wire_payload(self, payload: str, payload_hash: str) -> str:
        return f"ORCHESTRATOR_DELIVERY_SHA256={payload_hash}\n\n{payload}"

    def _result_payload_proof(self, bridge: Any, payload: str, payload_hash: str | None = None) -> bool:
        """Prove delivery from a user message, using marker or legacy proof."""
        expected_hash = payload_hash or hashlib.sha256(payload.encode("utf-8")).hexdigest()
        messages = getattr(bridge, "user_message_texts", None)
        if callable(messages):
            try:
                observed_messages = messages()
            except Exception:
                observed_messages = []
        else:
            latest = getattr(bridge, "latest_user_message", None)
            try:
                observed = latest() if callable(latest) else None
            except Exception:
                observed = None
            observed_messages = [observed] if isinstance(observed, str) else []
        observed_messages = [text for text in observed_messages if isinstance(text, str)]
        if self.state.get("architectDeliveryProofVersion") == ARCHITECT_DELIVERY_PROOF_VERSION:
            marker = f"ORCHESTRATOR_DELIVERY_SHA256={expected_hash}"
            return any(any(line.strip() == marker for line in text.splitlines()) for text in observed_messages)
        expected_tokens = self._delivery_tokens(payload)
        expected_token_hash = hashlib.sha256(" ".join(expected_tokens).encode("utf-8")).hexdigest()
        return any(
            (tokens := self._delivery_tokens(text)) == expected_tokens
            and len(tokens) == len(expected_tokens)
            and hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest() == expected_token_hash
            for text in observed_messages
        )

    def _exact_result_payload_observed(self, bridge: Any, payload: str) -> bool:
        return self._result_payload_proof(bridge, payload)

    def _wait_for_exact_result_payload(self, bridge: Any, payload: str, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self._exact_result_payload_observed(bridge, payload):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def clear_confirmed_stale_composer(self, bridge: Any, payload: str, payload_hash: str) -> bool:
        composer = getattr(bridge, "_live_composer", lambda: None)()
        if composer is None:
            return False
        try:
            observed = composer.inner_text(timeout=1000)
            if isinstance(observed, str) and observed.strip() and normalize_prompt(observed) == normalize_prompt(payload):
                composer.focus(timeout=1000)
                composer.press("ControlOrMeta+A", timeout=1000)
                composer.press("Backspace", timeout=1000)
                cleared = composer.inner_text(timeout=1000)
                if not isinstance(cleared, str) or cleared.strip():
                    return False
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_STALE_COMPOSER_CLEARED", self.state, hash=payload_hash)
                return True
        except Exception:
            return False
        return False

    def reconcile_exhausted_result_delivery(self, bridge: Any) -> bool:
        if self.state.get("state") != "HUMAN_REQUIRED" or self.state.get("humanRequiredReason") != "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED":
            return False
        payload, payload_hash = self._result_delivery_payload()
        if self.state.get("architectDeliveryPayloadHash") != payload_hash or not self._exact_result_payload_observed(bridge, payload):
            return False
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectDeliveryFailureClass": None, "architectSendError": None, "humanRequiredReason": None})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECONCILED", self.state, hash=payload_hash)
        self.clear_confirmed_stale_composer(bridge, payload, payload_hash)
        return True

    def recover_false_result_reconciliation(self, bridge: Any) -> bool:
        """Undo only a proven IDLE STOP caused by a false result reconciliation."""
        task_id = str(self.state.get("taskId") or "")
        fingerprint = self.state.get("architectResultFingerprint")
        result_path = self.state.get("executorResultPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            result_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        try:
            executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        except (TypeError, ValueError):
            executor_active = True
        consumed = self.state.get("consumedArchitectResponses") or {}
        record = consumed.get(fingerprint) if isinstance(fingerprint, str) else None
        if (self.state.get("state") != "IDLE" or not task_id or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not result_ready or self.state.get("architectSendState") != "CONFIRMED"
                or not isinstance(fingerprint, str) or not isinstance(record, dict)
                or record.get("taskId") != task_id or record.get("action") != "STOP"
                or self.state.get("nextPromptPath")
                or (self.state.get("nextTaskId") and self.state.get("nextTaskId") not in {task_id, str(self.state.get("lastCompletedTaskId") or "")})
                or executor_active):
            return False
        payload, payload_hash = self._result_delivery_payload()
        if self.state.get("architectDeliveryPayloadHash") != payload_hash:
            return False
        if self._exact_result_payload_observed(bridge, payload):
            return False
        messages = getattr(bridge, "user_message_texts", None)
        if not callable(messages):
            return False
        task_marker = f"previous response for task {task_id}"
        try:
            current_messages = messages()
        except Exception:
            return False
        recovery_present = any(isinstance(text, str) and task_marker in text and "Return only the machine-readable envelope" in text for text in current_messages)
        if not recovery_present:
            return False
        self.state.update({
            "state": "RESULT_READY", "architectSendState": "FAILED",
            "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_PRE_SEND_FAILURE",
            "humanRequiredReason": None, "architectResultFingerprint": None,
            "formatRecoveryCount": 0, "formatRecoveryExhausted": False,
            "architectFormatRecoveryTaskId": None, "architectBaseline": None,
        })
        consumed.pop(fingerprint, None)
        if self.state.get("documentationClosureFingerprint") == fingerprint:
            self.state["documentationClosureFingerprint"] = None
            self.state["documentationClosureCompletedTaskId"] = None
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "FALSE_RESULT_RECONCILIATION_RECOVERED", self.state, taskId=task_id, payloadHash=payload_hash, invalidArchitectResultFingerprint=fingerprint, exactPayloadObserved=False)
        return True

    def deliver_result(self, bridge: Any) -> str | None:
        if self.state.get("state") != "RESULT_READY":
            raise RuntimeError("RESULT_NOT_READY")
        if self.discussion_pause_active():
            return
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            if not getattr(self, "_result_delivery_deferred_logged", False):
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_DEFERRED", self.state, reason="ARCHITECT_GENERATING")
                self._result_delivery_deferred_logged = True
            return RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING
        self._result_delivery_deferred_logged = False
        path = Path(self.state["executorResultPath"])
        payload, payload_hash = self._result_delivery_payload()
        task_id = str(self.state["taskId"])
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_ATTEMPT", self.state, hash=payload_hash)
        prior_hash = self.state.get("architectDeliveryPayloadHash")
        delivery_state = self.state.get("architectSendState")
        if prior_hash == payload_hash and delivery_state == "CONFIRMED":
            if not self._exact_result_payload_observed(bridge, payload):
                self.state.update({"architectSendState": "AMBIGUOUS", "architectSendError": "ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
                self.save()
                raise ResultSubmissionError("ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED")
            self.state.update({"state": "ARCHITECT_RUNNING", "architectResultFingerprint": None})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_CONFIRMED", self.state, hash=payload_hash, disposition="ALREADY_DELIVERED")
            return
        ambiguous_history = self.state.get("architectDeliveryFailureClass") == "ARCHITECT_DELIVERY_AMBIGUOUS"
        pre_send_failure = (delivery_state == "FAILED" and self.state.get("architectDeliveryFailureClass") == "ARCHITECT_DELIVERY_PRE_SEND_FAILURE")
        if prior_hash == payload_hash and pre_send_failure:
            if self._exact_result_payload_observed(bridge, payload):
                baseline = self.state.get("architectDeliveryBaseline")
                self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectSendError": None, "architectDeliveryFailureClass": None, "architectResultFingerprint": None, "architectBaseline": baseline, "humanRequiredReason": None})
                self.save()
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECONCILED", self.state, hash=payload_hash)
                self.clear_confirmed_stale_composer(bridge, payload, payload_hash)
                return
        elif prior_hash == payload_hash and (delivery_state in {"PENDING", "AMBIGUOUS"} or (delivery_state == "FAILED" and ambiguous_history)):
            if self._exact_result_payload_observed(bridge, payload):
                baseline = self.state.get("architectDeliveryBaseline")
                self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectSendError": None, "architectDeliveryFailureClass": None, "architectResultFingerprint": None, "architectBaseline": baseline, "humanRequiredReason": None})
                self.save()
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECONCILED", self.state, hash=payload_hash)
                self.clear_confirmed_stale_composer(bridge, payload, payload_hash)
                return
            self.state.update({"architectSendState": "AMBIGUOUS", "architectSendError": "ARCHITECT_DELIVERY_AMBIGUOUS", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_AMBIGUOUS", self.state, hash=payload_hash, errorCode="ARCHITECT_DELIVERY_AMBIGUOUS")
            raise ResultSubmissionError("ARCHITECT_DELIVERY_AMBIGUOUS")
        baseline = bridge.assistant_baseline() if hasattr(bridge, "assistant_baseline") else None
        user_baseline = bridge.user_baseline() if hasattr(bridge, "user_baseline") else None
        self.state.update({
            "architectDeliveryTaskId": task_id,
            "architectDeliveryPayloadHash": payload_hash,
            "architectDeliveryBaseline": baseline,
            "architectDeliveryUserBaseline": user_baseline,
            "architectSendState": "PENDING",
            "architectSendError": None,
            "state": "RESULT_READY",
        })
        self.save()
        use_receipt = prior_hash != payload_hash or not prior_hash or self.state.get("architectDeliveryProofVersion") == ARCHITECT_DELIVERY_PROOF_VERSION
        wire_payload = self._result_delivery_wire_payload(payload, payload_hash) if use_receipt else payload
        if use_receipt:
            self.state["architectDeliveryProofVersion"] = ARCHITECT_DELIVERY_PROOF_VERSION
            self.save()
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        try:
            sender(wire_payload)
        except Exception as error:
            code = getattr(error, "code", None) or type(error).__name__
            attempted = bool(getattr(bridge, "sendActionAttempted", False)) or bool(getattr(bridge, "last_send_method", None))
            acknowledged = bool(getattr(bridge, "sendActionAcknowledged", False))
            ambiguous = (attempted and not acknowledged) or code == "ARCHITECT_SUBMISSION_ACK_TIMEOUT"
            failure_class = "ARCHITECT_DELIVERY_AMBIGUOUS" if ambiguous else "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"
            self.state.update({"state": "RESULT_READY", "architectSendState": "AMBIGUOUS" if ambiguous else "FAILED", "architectSendError": code, "architectDeliveryFailureClass": failure_class})
            self.save()
            if ambiguous:
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_AMBIGUOUS", self.state, hash=payload_hash, errorCode=code, failureClass=failure_class, attempt=self.state.get("architectTransportRecoveryCount"))
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_FAILED", self.state, errorClass=code, failureClass=failure_class)
            raise
        if not self._wait_for_exact_result_payload(bridge, payload):
            self.state.update({"state": "RESULT_READY", "architectSendState": "AMBIGUOUS", "architectSendError": "ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_AMBIGUOUS", self.state, hash=payload_hash, errorCode="ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED")
            raise ResultSubmissionError("ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED")
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectSendError": None, "architectDeliveryFailureClass": None, "architectResultFingerprint": None, "architectBaseline": baseline, "humanRequiredReason": None})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_CONFIRMED", self.state, hash=payload_hash)
        self.clear_confirmed_stale_composer(bridge, payload, payload_hash)

    def deliver_result_with_recovery(self, bridge_factory: Callable[[], Any], max_attempts: int = 3, initial_bridge: Any | None = None) -> Any | None:
        """Reconcile or deliver one result without exiting the resident watcher."""
        bridge = initial_bridge
        attempt = 0
        while attempt < max_attempts:
            try:
                if bridge is None:
                    bridge = bridge_factory()
                if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
                    self.deliver_result(bridge)
                    time.sleep(0.25)
                    continue
                self.state["architectTransportRecoveryPayloadHash"] = self.state.get("architectDeliveryPayloadHash")
                self.state["architectTransportRecoveryCount"] = attempt
                self.save()
                disposition = self.deliver_result(bridge)
                if disposition == RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING:
                    time.sleep(0.25)
                    continue
                self.state["architectTransportRecoveryCount"] = 0
                self.save()
                return bridge
            except Exception as error:
                try:
                    bridge.close()
                except Exception:
                    pass
                bridge = None
                attempt += 1
                if attempt >= max_attempts:
                    self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED", "architectTransportRecoveryCount": attempt})
                    self.save()
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_EXHAUSTED", self.state, reason="ARCHITECT_RESULT_TRANSPORT_EXHAUSTED")
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_REQUIRED", self.state, reason="ARCHITECT_RESULT_TRANSPORT_EXHAUSTED")
                    return None
                time.sleep(0.25)
        return None

    def request_format_recovery(self, bridge: Any) -> None:
        """Request one machine-readable envelope without replaying the result."""
        if self.discussion_pause_active():
            return
        if (self.state.get("state") == "RESULT_READY" or self.state.get("architectDeliveryPayloadHash")
                and self.state.get("architectSendState") in {"PENDING", "FAILED", "AMBIGUOUS"}):
            raise RuntimeError("RESULT_DELIVERY_NOT_CONFIRMED")
        if self.state.get("architectDeliveryPayloadHash"):
            payload, payload_hash = self._result_delivery_payload()
            if payload_hash != self.state.get("architectDeliveryPayloadHash") or not self._exact_result_payload_observed(bridge, payload):
                raise RuntimeError("RESULT_DELIVERY_NOT_CONFIRMED")
        task_id = str(self.state["taskId"])
        self._reset_format_recovery_for_task(task_id)
        if int(self.state.get("formatRecoveryCount", 0)) >= 1:
            self.state.update({"state": "HUMAN_REQUIRED", "formatRecoveryExhausted": True})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_REQUIRED", self.state, reason="FORMAT_RECOVERY_EXHAUSTED")
            return
        message = "\n".join([
            f"Your previous response for task {task_id} was received successfully but did not contain a valid ORCHESTRATOR_RESULT envelope.",
            "Do not redo the underlying task.",
            "Do not request the Executor report again.",
            "Return only the machine-readable envelope for your already-completed decision:",
            "<ORCHESTRATOR_RESULT>",
            "classification=ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT",
            "action=EXECUTE|HUMAN_REQUIRED|STOP",
            f"taskId={task_id}",
            "documentation=NOT_REQUIRED|REQUIRED|COMPLETE",
            "Preserve the documentation disposition from the already-completed decision; do not downgrade a pending documentation closure to NOT_REQUIRED.",
            "promptBegin <complete next Executor prompt only when action=EXECUTE>",
            "promptEnd",
            "</ORCHESTRATOR_RESULT>",
        ])
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "FORMAT_RECOVERY_REQUESTED", self.state)
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        sender(message)
        baseline = bridge.assistant_baseline() if hasattr(bridge, "assistant_baseline") else None
        self.state.update({"state": "ARCHITECT_RUNNING", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": task_id, "architectBaseline": baseline, "humanRequiredReason": None})
        self.save()

    def recover_stale_format_human_required(self) -> bool:
        """Re-enter Architect observation only for the known cross-task stale gate."""
        task_id = str(self.state.get("taskId") or "")
        recovery_task = self.state.get("architectFormatRecoveryTaskId")
        reason = self.state.get("humanRequiredReason")
        result_path = self.state.get("executorResultPath")
        result_ready = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        if (self.state.get("state") != "HUMAN_REQUIRED" or not self.state.get("formatRecoveryExhausted")
                or recovery_task in (None, "", task_id) or reason not in (None, "") or not result_ready):
            return False
        pid = self.state.get("codexPid")
        if pid and LocalWatcher.process_alive(int(pid)):
            return False
        self.state.update({"formatRecoveryCount": 0, "formatRecoveryExhausted": False, "architectFormatRecoveryTaskId": None})
        self.state["state"] = "ARCHITECT_RUNNING" if self.state.get("architectSendState") == "CONFIRMED" else "RESULT_READY"
        self.save()
        return True

    def accept_architect_response(self, response: str) -> dict[str, str]:
        fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
        consumed = self.state.setdefault("consumedArchitectResponses", {})
        if fingerprint == self.state.get("architectResultFingerprint") or fingerprint in consumed:
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_RESPONSE_DUPLICATE", self.state, hash=fingerprint)
            return {"action": "DUPLICATE"}
        task_id = str(self.state.get("taskId") or self._architect_response_task_id(response) or "")
        if not task_id:
            raise ValueError("ARCHITECT_ENVELOPE_INVALID")
        decision = parse_orchestrator_result(response, task_id)
        documentation = decision["documentation"]
        pending_documentation = bool(self.state.get("documentationClosurePending"))
        if pending_documentation and documentation != "COMPLETE":
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "DOCUMENTATION_CLOSURE_REQUIRED"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_BYPASS_BLOCKED", self.state, disposition=documentation)
            raise ValueError("DOCUMENTATION_CLOSURE_REQUIRED")
        if documentation == "REQUIRED" and (decision["classification"] != "ACCEPTED" or decision["action"] != "EXECUTE" or not decision["prompt"].strip()):
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "DOCUMENTATION_DISPOSITION_INVALID"})
            self.save()
            raise ValueError("DOCUMENTATION_DISPOSITION_INVALID")
        self.state.update({"formatRecoveryCount": 0, "formatRecoveryExhausted": False, "architectFormatRecoveryTaskId": None})
        if documentation == "REQUIRED":
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_REQUIRED", self.state, taskId=task_id, disposition=documentation)
        if documentation == "COMPLETE":
            self.state.update({"documentationClosurePending": False, "documentationClosureCompletedTaskId": task_id, "documentationClosureFingerprint": fingerprint})
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_ACCEPTED", self.state, taskId=task_id, disposition=documentation)
        if decision["action"] == "EXECUTE":
            current_sequence = int(self.state.get("taskSequence", 0))
            current_task = str(self.state.get("taskId") or "")
            if current_task.isdigit():
                current_sequence = max(current_sequence, int(current_task))
            sequence = current_sequence + 1
            next_id = f"{sequence:06d}"
            path = self.prompts_dir / f"{next_id}.txt"
            target = self._owned_task_worktree(next_id, decision["prompt"], context_task_id=str(self.state.get("taskId") or ""))
            if target is None:
                raise RuntimeError("EXECUTOR_PROJECT_CONTEXT_MISSING")
            prompt_bytes = decision["prompt"].encode("utf-8")
            if path.exists() and path.read_bytes() != prompt_bytes:
                raise RuntimeError("EXECUTOR_PROMPT_EVIDENCE_MISMATCH")
            if not path.exists():
                atomic_write(path, prompt_bytes)
            self.state["architectResultFingerprint"] = fingerprint
            target_text = str(target)
            self.state.update({"state": "NEXT_PROMPT_READY", "nextPromptPath": str(path), "nextTaskId": next_id, "targetProject": target_text, "targetRepo": target_text, "targetWorktree": target_text, "humanRequiredReason": None})
            if documentation == "REQUIRED":
                self.state.update({"documentationClosurePending": True, "documentationClosureSourceTaskId": task_id, "documentationClosureTaskId": next_id})
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_TASK_STAGED", self.state, taskId=next_id, sourceTaskId=task_id)
        elif decision["action"] == "HUMAN_REQUIRED":
            self.state["architectResultFingerprint"] = fingerprint
            self.state.update({"state": "HUMAN_REQUIRED", "nextPromptPath": None, "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED"})
        else:
            self.state["architectResultFingerprint"] = fingerprint
            self.state.update({"state": "IDLE", "nextPromptPath": None, "humanRequiredReason": None})
        consumed[fingerprint] = {"taskId": task_id, "classification": decision["classification"], "action": decision["action"], "state": "RECEIVED", "origin": "BOOTSTRAP" if self.state.get("architectBootstrapAwaiting") else "RESULT_REVIEW"}
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_RESPONSE_ACCEPTED", self.state, hash=fingerprint, classification=decision["classification"], action=decision["action"])
        return decision

    def launch_next(self, launcher: Callable[[str, Path], Any]) -> Any:
        if self.state.get("state") != "NEXT_PROMPT_READY" or self.discussion_pause_active():
            return None
        prompt_path = Path(self.state["nextPromptPath"])
        prompt = prompt_path.read_text(encoding="utf-8")
        task_id = str(self.state.get("nextTaskId") or self.state.get("taskId") or "")
        owned = self.state.get("taskWorktrees", {}).get(task_id)
        target = str(owned["worktreePath"]) if isinstance(owned, dict) and owned.get("worktreePath") else resolve_executor_worktree(prompt, self._configured_fallback_project())
        self.state.update({"targetProject": target, "targetRepo": target, "targetWorktree": target})
        self.save()
        process = launcher(prompt, self._result_path(str(self.state["nextTaskId"])))
        self._active_process = process
        self.mark_executor_started(str(self.state["nextTaskId"]), int(process.pid), self._result_path(str(self.state["nextTaskId"])))
        return process


class DiscussionHotkeyController:
    """Small process-local F9/F10 controller; the durable flag remains watcher-owned."""
    VK_F9 = 0x78
    VK_F10 = 0x79
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_NOREPEAT = 0x4000

    def __init__(self, watcher: LocalFirstOrchestrator, emit: Callable[[str], None] = print):
        self.watcher = watcher
        self.emit = emit
        self.active = False
        self._thread = None
        self._thread_id = None

    def dispatch(self, key: str) -> bool:
        if key == "F9":
            self.watcher.request_discussion_pause()
            return True
        if key == "F10":
            self.watcher.request_discussion_resume()
            return True
        return False

    def start(self, log: Callable[[str], None] | None = None) -> bool:
        if os.name != "nt":
            self.emit("HUMAN_DISCUSSION_HOTKEYS_UNAVAILABLE platform=non_windows")
            return False
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
        registration = {"ok": False, "thread_id": None}

        def run() -> None:
            thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            registration["thread_id"] = thread_id
            if not user32.RegisterHotKey(None, 9, self.MOD_NOREPEAT, self.VK_F9) or not user32.RegisterHotKey(None, 10, self.MOD_NOREPEAT, self.VK_F10):
                user32.UnregisterHotKey(None, 9)
                user32.UnregisterHotKey(None, 10)
                return
            registration["ok"] = True
            message = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == self.WM_HOTKEY:
                    self.dispatch("F9" if message.wParam == 9 else "F10" if message.wParam == 10 else "")
            user32.UnregisterHotKey(None, 9)
            user32.UnregisterHotKey(None, 10)

        self._thread = threading.Thread(target=run, name="orchestrator-hotkeys", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 1.0
        while registration["thread_id"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        while not registration["ok"] and registration["thread_id"] is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not registration["ok"]:
            self.emit("HUMAN_DISCUSSION_HOTKEY_REGISTRATION_FAILED")
            if log:
                log("HUMAN_DISCUSSION_HOTKEY_REGISTRATION_FAILED")
            return False
        self._thread_id = registration["thread_id"]
        self.active = True
        self.emit("F9 = PAUSE FOR ARCHITECT DISCUSSION")
        self.emit("F10 = RESUME ORCHESTRATOR")
        return True

    def stop(self) -> None:
        if self.active and self._thread_id and os.name == "nt":
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.active = False


class RemoteDiscussionControlMonitor:
    """Read-only Architect user-message monitor for exact pause/resume commands."""
    def __init__(self, watcher: LocalFirstOrchestrator, bridge_factory: Callable[[], Any], emit: Callable[[str], None] = print, startup_timeout: float = 45.0, shutdown_timeout: float = 5.0):
        self.watcher = watcher
        self.bridge_factory = bridge_factory
        self.emit = emit
        self.bridge = None
        self.active = False
        self._thread = None
        self._cursor = None
        self._conversation_id = None
        self._consumed_command_ids: list[str] = []
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._startup_ok = False
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout

    @staticmethod
    def _identity(message: dict[str, Any], index: int) -> str:
        identity = message.get("id")
        return str(identity) if identity else hashlib.sha256(str(message.get("text") or "").encode("utf-8")).hexdigest() + f":{index}"

    def _messages(self, bridge: Any) -> list[dict[str, Any]]:
        reader = getattr(bridge, "control_user_messages", None) or getattr(bridge, "user_messages", None)
        if not callable(reader):
            raise RuntimeError("REMOTE_CONTROL_USER_READER_UNAVAILABLE")
        messages = reader()
        return [message for message in messages if isinstance(message, dict) and message.get("author", "user") == "user"] if isinstance(messages, list) else []

    def establish_startup_baseline(self, bridge: Any | None = None) -> None:
        self.bridge = bridge or self.bridge_factory()
        requested_id = self.watcher.state.get("architectConversationId")
        self._conversation_id = canonicalize_attached_architect_conversation(self.watcher, self.bridge, requested_id)
        messages = self._messages(self.bridge)
        self._cursor = self._identity(messages[-1], len(messages) - 1) if messages else None
        atomic_write(self.watcher.state_dir / "remote-control.json", json.dumps({"cursor": self._cursor, "count": len(messages)}).encode("utf-8"))

    def poll_once(self) -> int:
        messages = self._messages(self.bridge)
        start = 0
        if self._cursor is not None:
            positions = [index for index, message in enumerate(messages) if self._identity(message, index) == self._cursor]
            if positions:
                start = positions[-1] + 1
            else:
                start = len(messages)
        observed = 0
        for index, message in enumerate(messages[start:], start=start):
            text = message.get("text")
            if isinstance(text, str):
                command = text.strip()
                if command in {"ORCH:PAUSE", "ORCH:RESUME"}:
                    identity = self._identity(message, index)
                    if identity in self._consumed_command_ids:
                        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_COMMAND_DUPLICATE_IGNORED", self.watcher.state, messageHash=hashlib.sha256(text.encode("utf-8")).hexdigest())
                        continue
                    runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_COMMAND_OBSERVED", self.watcher.state, command=command, messageHash=hashlib.sha256(text.encode("utf-8")).hexdigest())
                    if command == "ORCH:PAUSE":
                        self.watcher.request_remote_discussion_pause()
                    else:
                        self.watcher.request_remote_discussion_resume()
                    self._consumed_command_ids.append(identity)
                    self._consumed_command_ids = self._consumed_command_ids[-32:]
                    observed += 1
                    self._cursor = identity
        if messages:
            self._cursor = self._identity(messages[-1], len(messages) - 1)
        atomic_write(self.watcher.state_dir / "remote-control.json", json.dumps({"cursor": self._cursor, "count": len(messages)}).encode("utf-8"))
        return observed

    def start(self) -> bool:
        def run() -> None:
            try:
                self.establish_startup_baseline()
                if self._stop_event.is_set():
                    return
                self._startup_ok = True
                self.active = True
                self._ready.set()
                while not self._stop_event.is_set():
                    current = self.watcher.state.get("architectConversationId")
                    if current and self._conversation_id and current != self._conversation_id:
                        self.bridge.close()
                        self.bridge = None
                        self.establish_startup_baseline()
                    self.poll_once()
                    self._stop_event.wait(1.0)
            except Exception as error:
                self._startup_ok = False
                self._ready.set()
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_MONITOR_ERROR", self.watcher.state, errorClass=type(error).__name__)
            finally:
                self.active = False
                if self.bridge is not None:
                    try:
                        self.bridge.close()
                    except Exception:
                        pass
                    self.bridge = None

        self._ready.clear()
        self._stop_event.clear()
        self._startup_ok = False
        self._thread = threading.Thread(target=run, name="orchestrator-remote-control", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.startup_timeout) or not self._startup_ok:
            self._stop_event.set()
            if self._thread.is_alive():
                self._thread.join(timeout=self.shutdown_timeout)
            self.emit("REMOTE_CONTROL_UNAVAILABLE")
            return False
        self.emit("REMOTE CONTROL ACTIVE")
        self.emit("ORCH:PAUSE = pause Architect relay")
        self.emit("ORCH:RESUME = resume Orchestrator")
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_MONITOR_STARTED", self.watcher.state)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self.active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


def visible_executor_launcher(project: str, watcher: LocalFirstOrchestrator) -> Callable[[str, Path], Any]:
    """Build the existing visible Codex launch surface for one owned child."""
    def launch(prompt: str, result_path: Path) -> Any:
        target = watcher.state["targetWorktree"]
        configured_session = watcher.state.get("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)
        if configured_session != AFFOTECH_EXECUTOR_SESSION_ID:
            raise RuntimeError("EXECUTOR_SESSION_IDENTITY_MISMATCH")
        verify_executor_session(AFFOTECH_EXECUTOR_SESSION_ID)
        runner = CodexRunner(project, child_project_dir=target, session_id=AFFOTECH_EXECUTOR_SESSION_ID)
        watcher.state.update({"executorSessionId": AFFOTECH_EXECUTOR_SESSION_ID, "executorSessionMode": "PERSISTENT"})
        attempt = int(watcher.state.get("executorAttemptNumber", 0)) + 1
        stderr_identity = watcher.state.get("nextTaskId") or watcher.state.get("taskId") or "unknown"
        stderr_path = watcher.state_dir / "executor-logs" / f"{stderr_identity}-attempt-{attempt}.stderr.txt"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.touch(exist_ok=True)
        watcher.state.update({"stderrLogPath": str(stderr_path)})
        watcher.save()
        command_args = ["exec", "resume", runner.session_id, "-o", str(result_path), "-"]
        command = (["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", runner.executable, *command_args] if os.name == "nt" and runner.launcher[0].lower().endswith(".ps1") else [*runner.launcher, *command_args])
        print("==================================================")
        print(f"AFFOTECH AUTOMATED EXECUTOR taskId={stderr_identity}")
        print("Executor is running")
        print("==================================================")
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=None, stderr=subprocess.PIPE, cwd=target, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        child_stderr = getattr(child, "stderr", None)
        if child_stderr is not None:
            def capture_stderr() -> None:
                with stderr_path.open("a", encoding="utf-8", errors="replace") as log:
                    while True:
                        chunk = child_stderr.readline()
                        if not chunk:
                            break
                        if isinstance(chunk, bytes):
                            text = chunk.decode("utf-8", errors="replace")
                        else:
                            text = str(chunk)
                        log.write(text)
                        log.flush()
                        try:
                            sys.stderr.write(text)
                            sys.stderr.flush()
                        except (OSError, AttributeError):
                            pass
            threading.Thread(target=capture_stderr, name="executor-stderr", daemon=True).start()
        assert child.stdin is not None
        child.stdin.write(runner.assemble_prompt(prompt).encode("utf-8")); child.stdin.close()
        return child

    return launch


def run_executor_state_once(watcher: LocalFirstOrchestrator, launch: Callable[[str, Path], Any]) -> str:
    """Advance one executor/crash/human state cycle, including same-invocation authorization."""
    state = watcher.state.get("state", "IDLE")
    if state == "EXECUTOR_RUNNING":
        state = watcher.wait_for_executor()
        if state != "EXECUTOR_RUNNING":
            print("CODEX_FINISHED taskId=%s pid=%s exitCode=%s" % (watcher.state.get("taskId"), watcher.state.get("codexPid"), watcher.state.get("executorExitCode")))
            runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "CODEX_FINISHED", watcher.state, pid=watcher.state.get("codexPid"), exitCode=watcher.state.get("executorExitCode"))
        if state == "EXECUTOR_CRASHED":
            watcher.state["state"] = "HUMAN_REQUIRED"
            watcher.save()
            print("STATE=HUMAN_REQUIRED reason=EXECUTOR_EXITED_WITHOUT_RESULT")
            runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "HUMAN_REQUIRED", watcher.state, reason="EXECUTOR_EXITED_WITHOUT_RESULT")
        else:
            return state
    if watcher.state.get("state") == "HUMAN_REQUIRED":
        if watcher.state.get("humanRequiredReason") == "ARCHITECT_DECISION_HUMAN_REQUIRED":
            return "HUMAN_REQUIRED"
        recovered = watcher.recover_prelaunch_incomplete(launch)
        if recovered is not None:
            return "NEXT_PROMPT_READY"
        recovered = watcher.authorize_postlaunch_retry(launch)
        if recovered is not None:
            return "NEXT_PROMPT_READY"
        print("STATE=HUMAN_REQUIRED")
        return "STOP"
    return watcher.state.get("state", "IDLE")


def resident_human_decision_response(watcher: LocalFirstOrchestrator, response: str, baseline: dict[str, Any] | None = None) -> str:
    """Consume one response while waiting for a business decision."""
    if "<ORCHESTRATOR_RESULT>" not in response:
        if isinstance(baseline, dict):
            watcher.state["architectBaseline"] = baseline
            watcher.save()
        return "DISCUSSION"
    try:
        decision = watcher.accept_architect_response(response)
    except (ValueError, RuntimeError):
        watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED"})
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_HUMAN_DECISION_RESPONSE_INVALID", watcher.state)
        return "INVALID"
    watcher.state["architectBaseline"] = baseline if isinstance(baseline, dict) else watcher.state.get("architectBaseline")
    watcher.save()
    return str(decision.get("action") or "")


def service_deferred_rollover_once(watcher: LocalFirstOrchestrator, endpoint: str, paused: Callable[[], bool], safe_boundary_state: str | None = None) -> bool:
    """Service maintenance once at the live-executor boundary only."""
    boundary_state = safe_boundary_state or watcher.state.get("state")
    if paused() or boundary_state not in {"EXECUTOR_RUNNING", "NEXT_PROMPT_READY"} or watcher.state.get("state") != boundary_state or not watcher.state.get("rolloverDue"):
        return False
    pid = watcher.state.get("codexPid")
    if boundary_state == "EXECUTOR_RUNNING" and (not pid or not LocalWatcher.process_alive(int(pid))):
        return False
    if boundary_state == "NEXT_PROMPT_READY" and pid and LocalWatcher.process_alive(int(pid)):
        return False
    bridge = None
    try:
        conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
        bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
        conversation_id = canonicalize_attached_architect_conversation(watcher, bridge, conversation_id)
        baseline = bridge.assistant_baseline()
        rollover = watcher.session_rollover
        if not rollover.request_if_due(bridge, latest_prompt_dispatched=True, executor_running=boundary_state == "EXECUTOR_RUNNING", architect_generating=bridge.generation_visible(), safe_boundary_state=boundary_state):
            return False
        observed = bridge.wait_for_new_response(baseline, poll_interval=0.5)
        if observed.get("state") != "COMPLETED" or not watcher.process_pending_handover_response(bridge, observed.get("text", "")):
            if watcher.state.get("rolloverInProgress"):
                watcher.defer_failed_rollover("ARCHITECT_HANDOVER_RESPONSE_INVALID")
            return False
        return True
    except Exception as error:
        if watcher.state.get("rolloverInProgress"):
            watcher.defer_failed_rollover(type(error).__name__)
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_MAINTENANCE_FAILED", watcher.state, errorClass=type(error).__name__)
        return False
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass


def dispatch_next_prompt_once(watcher: LocalFirstOrchestrator, launch: Callable[[str, Path], Any], endpoint: str, paused: Callable[[], bool], logger: logging.Logger | None = None, run_id: str | None = None) -> Any:
    """Gate NEXT_PROMPT_READY dispatch on due rollover maintenance."""
    if watcher.state.get("state") != "NEXT_PROMPT_READY":
        return None
    if watcher.state.get("rolloverDue") and not service_deferred_rollover_once(watcher, endpoint, paused, "NEXT_PROMPT_READY"):
        return None
    runtime_log(logger, run_id, "NEXT_PROMPT_READY", watcher.state)
    runtime_log(logger, run_id, "CODEX_STARTING", watcher.state, attempt=int(watcher.state.get("executorAttemptNumber", 0)) + 1)
    process = watcher.launch_next(launch)
    print(f"CODEX_STARTED taskId={watcher.state.get('taskId') or watcher.state.get('nextTaskId')} pid={process.pid}")
    return process


def handle_architect_value_error(watcher: LocalFirstOrchestrator, bridge: Any) -> bool:
    """Apply task-scoped format recovery from the resident main-loop path."""
    try:
        watcher.request_format_recovery(bridge)
    except ResultSubmissionError as error:
        watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED", "architectSendError": getattr(error, "code", type(error).__name__)})
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "HUMAN_REQUIRED", watcher.state, reason="ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED")
        return False
    return watcher.state.get("state") == "ARCHITECT_RUNNING"


def run_human_required_startup_once(watcher: LocalFirstOrchestrator, launch: Callable[[str, Path], Any]) -> str:
    """Run the production HUMAN_REQUIRED entry, including stale-format recovery."""
    if watcher.recover_stale_format_human_required():
        return watcher.state.get("state", "HUMAN_REQUIRED")
    return run_executor_state_once(watcher, launch)


def passive_human_required_wait(watcher: LocalFirstOrchestrator, poll_interval: float | None = None) -> str:
    """Wait once without changing HUMAN_REQUIRED workflow authority."""
    interval = poll_interval if poll_interval is not None else float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0"))
    time.sleep(interval)
    watcher.state = watcher._load_state()
    return watcher.state.get("state", "HUMAN_REQUIRED")


def main() -> None:
    project = os.environ.get("AFFOTECH_PROJECT_DIR", os.getcwd())
    state_dir = Path(os.environ.get("AFFOTECH_ORCHESTRATOR_STATE_DIR") or (Path(project) / ".agent-work" / "orchestrator"))
    instance_lock = WatcherInstanceLock(state_dir)
    try:
        instance_lock.acquire()
    except RuntimeError as error:
        print(str(error))
        return
    logger = None
    run_id = None
    try:
        logger, run_id, log_path = initialize_runtime_logging(state_dir)
    except Exception as error:
        print(f"ORCHESTRATOR_LOGGING_INIT_FAILED error={type(error).__name__}:{error}")
        instance_lock.release()
        return
    watcher = LocalFirstOrchestrator(project, state_dir)
    watcher.runtime_logger = logger
    watcher.runtime_run_id = run_id
    endpoint = os.environ.get("ARCHITECT_CDP_ENDPOINT", "http://127.0.0.1:9333")
    bind_memory_owner = getattr(watcher, "bind_architect_memory_owner", None)
    if bind_memory_owner is not None:
        try:
            bind_memory_owner(endpoint)
        except RuntimeError as error:
            print(f"ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED reason={error}")
            instance_lock.release()
            return
    recovery_state = watcher.state.get("state", "IDLE")
    recovery_action = {
        "EXECUTOR_RUNNING": "WAIT_EXISTING_EXECUTOR",
        "RESULT_READY": "DELIVER_RESULT",
        "ARCHITECT_RUNNING": "WAIT_ARCHITECT",
        "NEXT_PROMPT_READY": "LAUNCH_EXECUTOR",
        "HUMAN_REQUIRED": "STOP_FOR_HUMAN",
        "IDLE": "WAIT_IDLE",
    }.get(recovery_state, "STOP_FOR_HUMAN")
    print("WATCHER_STARTED state=%s taskId=%s nextTaskId=%s lastCompletedTaskId=%s codexPid=%s architectSendState=%s architectConversationId=%s recoveryAction=%s" % (
        recovery_state, watcher.state.get("taskId") or "NONE", watcher.state.get("nextTaskId") or "NONE",
        watcher.state.get("lastCompletedTaskId") or "NONE", watcher.state.get("codexPid") or "NONE",
        watcher.state.get("architectSendState") or "NONE", watcher.state.get("architectConversationId") or "NONE", recovery_action))
    runtime_log(logger, run_id, "WATCHER_STARTED", watcher.state, source=log_path)
    runtime_log(logger, run_id, "STATE_RECOVERED", watcher.state, recoveryAction=recovery_action)
    hotkeys = DiscussionHotkeyController(watcher)
    if not hotkeys.start(lambda event: runtime_log(logger, run_id, event, watcher.state)) and os.name == "nt":
        print("ORCHESTRATOR_HOTKEY_REGISTRATION_FAILED")
        instance_lock.release()
        return
    discussion_paused = getattr(watcher, "discussion_pause_active", lambda: bool(watcher.state.get("discussionPauseActive")))
    if discussion_paused():
        print(f"ORCHESTRATOR PAUSED BY HUMAN state={watcher.state.get('state')} taskId={watcher.state.get('taskId') or watcher.state.get('nextTaskId') or 'NONE'}")
        runtime_log(logger, run_id, "HUMAN_DISCUSSION_PAUSE_ACTIVE", watcher.state)
    launch = visible_executor_launcher(project, watcher)
    remote_monitor = None
    if hasattr(watcher, "discussion_pause_active"):
        remote_monitor = RemoteDiscussionControlMonitor(
            watcher,
            lambda: ArchitectPlaywright.attach(
                endpoint,
                watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID,
            ),
        )
        if not remote_monitor.start():
            print("REMOTE CONTROL UNAVAILABLE; F9/F10 remain available")
    idle_bridge = None
    human_wait_bridge = None
    last_logged_state = recovery_state
    stop_logged = False
    pause_notice = None
    try:
        while True:
            rollover = getattr(watcher, "session_rollover", None)
            if rollover is not None:
                rollover.sample_memory()
            state = watcher.state.get("state", "IDLE")
            if state != "HUMAN_REQUIRED" and human_wait_bridge is not None:
                try:
                    human_wait_bridge.close()
                except Exception:
                    pass
                human_wait_bridge = None
            if state != last_logged_state:
                runtime_log(logger, run_id, "STATE_TRANSITION", watcher.state, **{"from": last_logged_state, "to": state, "reason": watcher.state.get("humanRequiredReason")})
                last_logged_state = state
            if discussion_paused() and state in {"IDLE", "RESULT_READY", "NEXT_PROMPT_READY"}:
                notice = (state, watcher.state.get("taskId") or watcher.state.get("nextTaskId"))
                if notice != pause_notice:
                    print(f"ORCHESTRATOR PAUSED BY HUMAN state={state} taskId={notice[1] or 'NONE'} architectRelay=BLOCKED F10=RESUME")
                    pause_notice = notice
                time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                watcher.state = watcher._load_state()
                continue
            pause_notice = None
            if state != "IDLE" and idle_bridge is not None:
                idle_bridge.close()
                idle_bridge = None
            if state == "IDLE":
                print("STATE=IDLE")
                if watcher.intake_inbox(launch):
                    continue
                if idle_bridge is None:
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                    try:
                        idle_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        conversation_id = canonicalize_attached_architect_conversation(watcher, idle_bridge, conversation_id)
                    except Exception as error:
                        if idle_bridge is not None:
                            idle_bridge.close()
                            idle_bridge = None
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                        raise
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                recover_false = getattr(watcher, "recover_false_result_reconciliation", None)
                if callable(recover_false) and recover_false(idle_bridge):
                    idle_bridge.close()
                    idle_bridge = None
                    continue
                try:
                    watcher.inspect_idle_architect(idle_bridge, launch)
                except Exception:
                    if idle_bridge is not None:
                        idle_bridge.close()
                    idle_bridge = None
                    raise
                if watcher.state.get("state") == "IDLE":
                    time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                    watcher.state = watcher._load_state()
                continue
            if state == "EXECUTOR_RUNNING":
                state = run_executor_state_once(watcher, launch)
                if state == "EXECUTOR_RUNNING":
                    service_deferred_rollover_once(watcher, endpoint, discussion_paused)
                    continue
                if state == "STOP":
                    return
                continue
            if state == "NEXT_PROMPT_READY":
                process = dispatch_next_prompt_once(watcher, launch, endpoint, discussion_paused, logger, run_id)
                if process is None and watcher.state.get("rolloverDue"):
                    time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                    watcher.state = watcher._load_state()
                continue
            if state == "HUMAN_REQUIRED":
                if watcher.state.get("humanRequiredReason") == "ARCHITECT_DECISION_HUMAN_REQUIRED":
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    if human_wait_bridge is None:
                        try:
                            human_wait_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                            conversation_id = canonicalize_attached_architect_conversation(watcher, human_wait_bridge, conversation_id)
                            runtime_log(logger, run_id, "ARCHITECT_HUMAN_WAIT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        except Exception as error:
                            if human_wait_bridge is not None:
                                try:
                                    human_wait_bridge.close()
                                except Exception:
                                    pass
                                human_wait_bridge = None
                            runtime_log(logger, run_id, "ARCHITECT_HUMAN_WAIT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                            time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                            watcher.state = watcher._load_state()
                            continue
                    baseline = watcher.state.get("architectBaseline")
                    if not isinstance(baseline, dict):
                        baseline = human_wait_bridge.assistant_baseline()
                    try:
                        observed = human_wait_bridge.wait_for_new_response(baseline, poll_interval=1.0)
                    except Exception as error:
                        try:
                            human_wait_bridge.close()
                        except Exception:
                            pass
                        human_wait_bridge = None
                        runtime_log(logger, run_id, "ARCHITECT_HUMAN_WAIT_READ_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error))
                        time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                        watcher.state = watcher._load_state()
                        continue
                    response = str(observed.get("text") or "")
                    disposition = resident_human_decision_response(watcher, response, human_wait_bridge.assistant_baseline())
                    if disposition in {"EXECUTE", "STOP"}:
                        try:
                            human_wait_bridge.close()
                        except Exception:
                            pass
                        human_wait_bridge = None
                    continue
                if watcher.recover_completed_confirmed_workflow():
                    continue
                if watcher.recover_preempted_rollover_failure():
                    continue
                if watcher.state.get("humanRequiredReason") == "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED":
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                    recovery_bridge = None
                    try:
                        recovery_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        try:
                            recovered = run_owned_recovery_bridge(recovery_bridge, watcher.recover_pending_rollover_handover)
                        except Exception as error:
                            runtime_log(logger, run_id, "ARCHITECT_PENDING_ROLLOVER_RECOVERY_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error))
                            recovered = False
                        if recovered:
                            continue
                    except Exception as error:
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                if watcher.state.get("humanRequiredReason") == "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED":
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                    recovery_bridge = None
                    try:
                        recovery_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        if run_owned_recovery_bridge(recovery_bridge, watcher.reconcile_exhausted_result_delivery):
                            continue
                    except Exception as error:
                        if recovery_bridge is None:
                            runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                        else:
                            runtime_log(logger, run_id, "ARCHITECT_RESULT_DELIVERY_RECOVERY_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                state = run_human_required_startup_once(watcher, launch)
                if state in {"EXECUTOR_RUNNING", "RESULT_READY", "ARCHITECT_RUNNING", "NEXT_PROMPT_READY"}:
                    continue
                print("STATE=HUMAN_REQUIRED")
                passive_human_required_wait(watcher)
                continue
            if state not in {"RESULT_READY", "ARCHITECT_RUNNING"}:
                print(f"STATE={state}")
                return

            conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
            runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
            bridge = None
            try:
                legacy_recovery_used = False
                try:
                    bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                except Exception as attach_error:
                    if (
                        watcher.state.get("state") == "RESULT_READY"
                        and str(attach_error) == "ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"
                    ):
                        bridge = attach_legacy_provisional_architect(endpoint, conversation_id, watcher)
                        legacy_recovery_used = True
                    else:
                        raise
                if legacy_recovery_used:
                    conversation_id = watcher.state.get("architectConversationId") or conversation_id
                conversation_id = canonicalize_attached_architect_conversation(watcher, bridge, conversation_id)
            except Exception as error:
                if bridge is not None:
                    bridge.close()
                runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                raise
            runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
            if bridge.generation_visible():
                runtime_log(logger, run_id, "ARCHITECT_GENERATION_STARTED", watcher.state, conversationId=conversation_id)
            watcher.state["architectConversationId"] = conversation_id
            watcher.save()
            try:
                if rollover is not None:
                    rollover.sample_memory()
                if (watcher.state.get("state") == "RESULT_READY" and not discussion_paused()
                        and (not watcher.state.get("handoverRequested", False) or not watcher.state.get("rolloverInProgress", False))):
                    bridge = watcher.deliver_result_with_recovery(
                        lambda: ArchitectPlaywright.attach(endpoint, conversation_id),
                        initial_bridge=bridge,
                    )
                    if bridge is None:
                        print(f"STATE=HUMAN_REQUIRED reason={watcher.state.get('humanRequiredReason', 'ARCHITECT_RESULT_TRANSPORT_EXHAUSTED')}")
                        break
                baseline = watcher.state.get("architectBaseline")
                if not isinstance(baseline, dict):
                    entries = bridge._assistant_entries()
                    prior = entries[:-1] if entries else []
                    snapshot = json.dumps(prior, ensure_ascii=False, separators=(",", ":"))
                    baseline = {"count": len(prior), "text_hash": hashlib.sha256(snapshot.encode()).hexdigest(), "entries": prior}
                while True:
                    try:
                        observed = bridge.wait_for_new_response(baseline, poll_interval=5.0)
                        runtime_log(logger, run_id, "ARCHITECT_GENERATION_FINISHED", watcher.state, conversationId=conversation_id)
                    except Exception as error:
                        watcher.state["state"] = "ARCHITECT_RUNNING"
                        watcher.save()
                        runtime_log(logger, run_id, "ARCHITECT_WAIT_INTERRUPTED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                        if bridge is not None:
                            bridge.close()
                        conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                        try:
                            bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        except Exception as attach_error:
                            runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(attach_error).__name__, errorMessage=str(attach_error), conversationId=conversation_id)
                            raise
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        time.sleep(1.0)
                        continue
                    if watcher.state.get("handoverRequested") and watcher.state.get("rolloverInProgress") and rollover is not None:
                        if watcher.process_pending_handover_response(bridge, observed["text"]):
                            baseline = bridge.assistant_baseline()
                            if watcher.state.get("state") == "RESULT_READY":
                                bridge = watcher.deliver_result_with_recovery(
                                    lambda: ArchitectPlaywright.attach(endpoint, watcher.state.get("architectConversationId") or conversation_id),
                                    initial_bridge=bridge,
                                )
                                if bridge is None:
                                    print(f"STATE=HUMAN_REQUIRED reason={watcher.state.get('humanRequiredReason', 'ARCHITECT_RESULT_TRANSPORT_EXHAUSTED')}")
                                    break
                                baseline = watcher.state.get("architectBaseline") or bridge.assistant_baseline()
                            continue
                        if not watcher.state.get("handoverRequested"):
                            baseline = bridge.assistant_baseline()
                            continue
                        watcher.reject_invalid_handover_response()
                        print("STATE=HUMAN_REQUIRED reason=ARCHITECT_HANDOVER_RESPONSE_INVALID")
                        break
                    try:
                        if watcher.state.get("architectBootstrapAwaiting"):
                            decision = watcher.consume_idle_architect_response(observed["text"], launch)
                            watcher.state["architectBootstrapAwaiting"] = False
                            watcher.save()
                        else:
                            decision = watcher.accept_architect_response(observed["text"])
                    except ValueError:
                        if not handle_architect_value_error(watcher, bridge):
                            print(f"STATE={watcher.state['state']}")
                            break
                        baseline = watcher.state.get("architectBaseline")
                        continue
                    if decision == "EXECUTE":
                        break
                    if not isinstance(decision, dict) or decision.get("action") != "EXECUTE":
                        if watcher.state.get("state") in {"HUMAN_REQUIRED", "IDLE"}:
                            if watcher.state.get("state") == "HUMAN_REQUIRED":
                                watcher.state["architectBaseline"] = bridge.assistant_baseline()
                                watcher.save()
                            break
                        print(f"STATE={watcher.state['state']}")
                        return
                    break
            finally:
                if bridge is not None:
                    bridge.close()
    except KeyboardInterrupt:
        print("STATE=STOPPED")
        runtime_log(logger, run_id, "WATCHER_STOPPED", watcher.state, reason="keyboard_interrupt")
        stop_logged = True
    except Exception as error:
        if logger is not None:
            logger.exception("unhandled production main-loop exception", extra={"runId": run_id or "UNKNOWN", "state": watcher.state.get("state", "UNKNOWN"), "taskId": watcher.state.get("taskId") or watcher.state.get("nextTaskId") or "NONE", "event": "WATCHER_EXCEPTION", "errorClass": type(error).__name__, "errorMessage": str(error)})
        raise
    finally:
        if remote_monitor is not None:
            remote_monitor.stop()
        hotkeys.stop()
        if idle_bridge is not None:
            idle_bridge.close()
        instance_lock.release()
        if not stop_logged and logger is not None and 'watcher' in locals():
            runtime_log(logger, run_id, "WATCHER_STOPPED", watcher.state, reason="shutdown")


if __name__ == "__main__":
    main()
