"""Minimal local Architect ⇄ Codex watcher for the AFFOTECH workflow."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
        script = "Get-CimInstance Win32_Process | ForEach-Object { $p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; if ($p) { '{0}`t{1}`t{2}' -f $_.ProcessId, $_.ParentProcessId, $p.WorkingSet64 } }"
        try:
            raw = subprocess.check_output(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], text=True, encoding="utf-8", errors="strict")
            process_rows = []
            for line in raw.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    process_rows.append({"pid": int(parts[0]), "parentPid": int(parts[1]), "workingSet": int(parts[2])})
        except (OSError, subprocess.CalledProcessError, ValueError, UnicodeError) as error:
            raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED") from error
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
    return sum(int(row.get("workingSet", 0)) for row in process_rows if int(row["pid"]) in pids)


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
            return None
        try:
            memory_bytes = reader()
        except RuntimeError as error:
            self.watcher.state["architectMemoryOwnership"] = "INCONCLUSIVE"
            self.watcher.state["architectMemoryError"] = str(error)
            self.watcher.save()
            return None
        if not isinstance(memory_bytes, int) or memory_bytes < 0:
            raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_INVALID")
        self.watcher.state["architectMemoryBytes"] = memory_bytes
        self.watcher.state["architectMemoryMiB"] = round(memory_bytes / (1024 * 1024), 2)
        trigger = self.rollover_trigger(memory_bytes, int(self.watcher.state.get("architectResponseCount", 0)))
        if trigger and not self.watcher.state.get("rolloverPending"):
            self.watcher.state["rolloverPending"] = True
            self.watcher.state["rolloverTrigger"] = trigger
            self.watcher.save()
            emit(f"ROLLOVER_PENDING trigger={trigger}")
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

    def request_if_due(self, bridge: "ArchitectPlaywright", latest_prompt_dispatched: bool, executor_running: bool, emit: Callable[[str], None] = print, architect_generating: bool = False) -> bool:
        count = int(self.watcher.state.get("architectResponseCount", 0))
        memory_bytes = int(self.watcher.state.get("architectMemoryBytes", 0))
        trigger = self.watcher.state.get("rolloverTrigger") or self.rollover_trigger(memory_bytes, count)
        if not trigger:
            return False
        if architect_generating or not latest_prompt_dispatched or not executor_running or self.watcher.state.get("handoverRequested"):
            return False
        self.watcher.state["rolloverPending"] = True
        self.watcher.state["rolloverTrigger"] = trigger
        self.watcher.state["handoverRequested"] = True
        self.watcher.state["handoverReady"] = False
        self.watcher.save()
        try:
            bridge.submit_result_bounded(STANDARD_HANDOVER_REQUEST)
            emit("ARCHITECT_HANDOVER_REQUESTED")
            return True
        except Exception:
            emit("STATE=ROLLOVER_PENDING")
            return False

    def complete_from_response(self, bridge: "ArchitectPlaywright", response: str, emit: Callable[[str], None] = print) -> bool:
        if not self.watcher.state.get("handoverRequested") or not response.rstrip().endswith(HANDOVER_READY):
            return False
        self.watcher.state["handoverReady"] = True
        self.watcher.state["pending_handover"] = response
        self.watcher.save()
        old_page = bridge.page
        try:
            new_page = bridge.open_fresh_with_handover(response)
            current_id = getattr(new_page, "url", "")
            bridge.page = new_page
            if hasattr(old_page, "close"):
                old_page.close()
            self.watcher.state["currentArchitectConversationId"] = current_id() if callable(current_id) else current_id
            self.watcher.state["architectResponseCount"] = 0
            self.watcher.state["handoverRequested"] = False
            self.watcher.state["handoverReady"] = False
            self.watcher.state["rolloverPending"] = False
            self.watcher.state.pop("rolloverTrigger", None)
            self.watcher.state.pop("pending_handover", None)
            self.watcher.save()
            emit("ARCHITECT_SESSION_ROLLOVER_COMPLETE")
            return True
        except Exception:
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
            if identity_changed and text.rstrip().endswith(COMPLETE):
                self.last_state = "COMPLETED"
                return {"state": "COMPLETED", "text": text}
            fallback_prompt = extract_executor_prompt_envelope(text) if identity_changed else None
            if identity_changed and fallback_prompt is not None:
                current_hash = hashlib.sha256(text.encode()).hexdigest()
                if current_hash == stable_hash:
                    stable_polls += 1
                else:
                    stable_hash = current_hash
                    stable_polls = 1
                if stable_polls >= 2:
                    self.last_state = "COMPLETED"
                    return {"state": "COMPLETED", "text": text}
                self.last_state = "RUNNING"
                time.sleep(poll_interval)
                continue
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

    def submit_result_bounded(self, result: str, timeout: float = 30.0) -> None:
        """Submit a result with explicit, bounded stages and typed failures."""
        deadline = time.monotonic() + timeout
        last_error = None
        composer = None
        while time.monotonic() < deadline:
            try:
                composer = self.page.get_by_role("textbox").last
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
        try:
            composer.focus(timeout=1000)
            composer.press("ControlOrMeta+A", timeout=1000)
            keyboard = getattr(self.page, "keyboard", None)
            if keyboard is None:
                raise RuntimeError("KEYBOARD_INPUT_UNAVAILABLE")
            keyboard.insert_text(result)
        except Exception as error:
            code = "ARCHITECT_COMPOSER_POPULATE_OPERATION_TIMEOUT" if type(error).__name__ == "TimeoutError" else "ARCHITECT_COMPOSER_INPUT_REJECTED"
            raise ResultSubmissionError(code, type(error).__name__) from error

        try:
            observed = composer.inner_text(timeout=1000)
        except Exception as error:
            code = "ARCHITECT_COMPOSER_INPUT_ACCEPTANCE_TIMEOUT" if type(error).__name__ == "TimeoutError" else "ARCHITECT_COMPOSER_INPUT_REJECTED"
            raise ResultSubmissionError(code, type(error).__name__) from error
        if not isinstance(observed, str) or not observed.strip():
            raise ResultSubmissionError("ARCHITECT_COMPOSER_INPUT_REJECTED")
        if normalize_prompt(observed) != normalize_prompt(result):
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
                composer_empty = not composer.inner_text(timeout=1000).strip()
                stop = self.page.get_by_role("button", name=re.compile(r"stop(?: generating)?", re.I)).last
                generation_visible = stop.count() > 0 and stop.is_visible(timeout=1000)
                assistant_started = self.assistant_count() > assistant_count_before
                if composer_empty or generation_visible or assistant_started:
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
            return bool(evaluate("""() => [...document.querySelectorAll('button,[role="button"]')].some((button) => /stop/i.test(button.innerText || button.getAttribute('aria-label') || ''))"""))
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
        new_page.goto("https://chatgpt.com/")
        composer = new_page.get_by_role("textbox").last
        composer.fill(handover)
        composer.press("Enter")
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
            pages = [p for p in pages if f"/c/{conversation_id}" in p.url]
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
EXECUTOR_WORKTREE_RE = re.compile(r"(?im)^\s*WORKTREE\s*\r?\n\s*(.+?)\s*$")
ORCHESTRATOR_RESULT_RE = re.compile(
    r"<ORCHESTRATOR_RESULT>\s*"
    r"classification=(ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT)\s*"
    r"action=(EXECUTE|HUMAN_REQUIRED|STOP)\s*"
    r"taskId=([^\r\n]+)\s*"
    r"promptBegin\s*\r?\n?(.*?)\r?\npromptEnd\s*"
    r"</ORCHESTRATOR_RESULT>\s*$",
    re.S,
)


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
    prompt = match.group(4).replace("\r\n", "\n").replace("\r", "\n")
    action = match.group(2)
    if action == "EXECUTE" and not prompt.strip():
        raise ValueError("ARCHITECT_ENVELOPE_PROMPT_REQUIRED")
    if action != "EXECUTE" and prompt.strip():
        raise ValueError("ARCHITECT_ENVELOPE_PROMPT_FORBIDDEN")
    return {"classification": match.group(1), "action": action, "taskId": completed_task_id, "prompt": prompt}


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
    candidate = match.group(1).strip().strip("`") if match else (str(fallback_project) if fallback_project is not None else None)
    if not candidate:
        raise RuntimeError("EXECUTOR_WORKTREE_MISSING")
    path = Path(candidate)
    if not path.is_dir():
        raise RuntimeError(f"EXECUTOR_WORKTREE_INVALID:{candidate}")
    return str(path)


class LocalFirstOrchestrator:
    """Small durable local control loop; GitHub is deliberately absent from it."""
    def __init__(self, project_dir: str, state_dir: str | os.PathLike[str] | None = None, process_factory: Callable[[str, Path], Any] | None = None):
        self.project_dir = Path(project_dir)
        self.state_dir = Path(state_dir or self.project_dir / ".agent-work" / "orchestrator")
        self.results_dir, self.prompts_dir, self.logs_dir = (self.state_dir / name for name in ("results", "prompts", "logs"))
        self.state_path = self.state_dir / "state.json"
        self.process_factory = process_factory
        self.state = self._load_state()

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

    def _result_path(self, task_id: str) -> Path:
        return self.results_dir / f"{task_id}.txt"

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
            target = resolve_executor_worktree(Path(pending_prompt).read_text(encoding="utf-8"), self.project_dir)
            self.state.update({"targetProject": target, "targetRepo": target, "targetWorktree": target})
        pid = self.state.get("codexPid")
        if LocalWatcher.process_alive(pid):
            return "EXECUTOR_RUNNING"
        path = self.state.get("executorResultPath")
        if isinstance(path, str) and Path(path).is_file() and Path(path).read_text(encoding="utf-8", errors="replace").strip():
            self.state["state"] = "RESULT_READY"
        else:
            self.state["state"] = "EXECUTOR_CRASHED"
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
        self.state.update({"state": "EXECUTOR_RUNNING", "taskId": task_id, "taskSequence": int(self.state.get("taskSequence", 0)) + 1, "codexPid": pid, "codexStartedAt": time.time(), "targetProject": str(self.project_dir), "executorResultPath": str(result_path)})
        self.save()

    def mark_executor_exit(self, exit_code: int, result_path: str | os.PathLike[str]) -> str:
        task_id = str(self.state.get("taskId", "unknown"))
        try:
            result = self._capture_result(task_id, Path(result_path))
        except (OSError, RuntimeError):
            self.state["state"] = "EXECUTOR_CRASHED"
            self.save()
            return "EXECUTOR_CRASHED"
        self.state.update({"state": "RESULT_READY", "executorResultPath": str(result), "executorExitCode": exit_code, "repositoryEvidence": repository_evidence(self.project_dir)})
        self.save()
        return "RESULT_READY"

    def deliver_result(self, bridge: Any) -> None:
        if self.state.get("state") != "RESULT_READY":
            raise RuntimeError("RESULT_NOT_READY")
        path = Path(self.state["executorResultPath"])
        report = path.read_text(encoding="utf-8")
        task_id = str(self.state["taskId"])
        instruction = ("Verify the completed Executor report below, classify it, decide the next bounded action, "
                       "and finish with exactly one <ORCHESTRATOR_RESULT> envelope using taskId=" + task_id + ".\n"
                       "The envelope must end the response; action=EXECUTE requires the complete next Executor prompt.\n\n")
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        sender(instruction + report)
        baseline = bridge.assistant_baseline() if hasattr(bridge, "assistant_baseline") else None
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectResultFingerprint": None, "architectBaseline": baseline})
        self.save()

    def request_format_recovery(self, bridge: Any) -> None:
        """Request one machine-readable envelope without replaying the result."""
        if int(self.state.get("formatRecoveryCount", 0)) >= 1:
            self.state.update({"state": "HUMAN_REQUIRED", "formatRecoveryExhausted": True})
            self.save()
            return
        task_id = str(self.state["taskId"])
        message = "\n".join([
            f"Your previous response for task {task_id} was received successfully but did not contain a valid ORCHESTRATOR_RESULT envelope.",
            "Do not redo the underlying task.",
            "Do not request the Executor report again.",
            "Return only the machine-readable envelope for your already-completed decision:",
            "<ORCHESTRATOR_RESULT>",
            "classification=ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT",
            "action=EXECUTE|HUMAN_REQUIRED|STOP",
            f"taskId={task_id}",
            "promptBegin <complete next Executor prompt only when action=EXECUTE>",
            "promptEnd",
            "</ORCHESTRATOR_RESULT>",
        ])
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        sender(message)
        baseline = bridge.assistant_baseline() if hasattr(bridge, "assistant_baseline") else None
        self.state.update({"state": "ARCHITECT_RUNNING", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": task_id, "architectBaseline": baseline})
        self.save()

    def accept_architect_response(self, response: str) -> dict[str, str]:
        fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
        if fingerprint == self.state.get("architectResultFingerprint"):
            return {"action": "DUPLICATE"}
        decision = parse_orchestrator_result(response, str(self.state["taskId"]))
        if decision["action"] == "EXECUTE":
            target = resolve_executor_worktree(decision["prompt"], self.project_dir)
            self.state["architectResultFingerprint"] = fingerprint
            sequence = int(self.state.get("taskSequence", 0)) + 1
            next_id = f"{sequence:06d}"
            path = self.prompts_dir / f"{next_id}.txt"
            atomic_write(path, decision["prompt"].encode("utf-8"))
            self.state.update({"state": "NEXT_PROMPT_READY", "nextPromptPath": str(path), "nextTaskId": next_id, "targetProject": target, "targetRepo": target, "targetWorktree": target})
        elif decision["action"] == "HUMAN_REQUIRED":
            self.state["architectResultFingerprint"] = fingerprint
            self.state.update({"state": "HUMAN_REQUIRED", "nextPromptPath": None})
        else:
            self.state["architectResultFingerprint"] = fingerprint
            self.state.update({"state": "IDLE", "nextPromptPath": None})
        self.save()
        return decision

    def launch_next(self, launcher: Callable[[str, Path], Any]) -> Any:
        if self.state.get("state") != "NEXT_PROMPT_READY":
            return None
        prompt_path = Path(self.state["nextPromptPath"])
        prompt = prompt_path.read_text(encoding="utf-8")
        target = resolve_executor_worktree(prompt, self.project_dir)
        self.state.update({"targetProject": target, "targetRepo": target, "targetWorktree": target})
        self.save()
        process = launcher(prompt, self._result_path(str(self.state["nextTaskId"])))
        self.mark_executor_started(str(self.state["nextTaskId"]), int(process.pid), self._result_path(str(self.state["nextTaskId"])))
        return process


def main() -> None:
    project = os.environ.get("AFFOTECH_PROJECT_DIR", os.getcwd())
    watcher = LocalFirstOrchestrator(project, os.environ.get("AFFOTECH_ORCHESTRATOR_STATE_DIR"))
    endpoint = os.environ.get("ARCHITECT_CDP_ENDPOINT", "http://127.0.0.1:9333")
    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
    try:
        while True:
            state = watcher.state.get("state", "IDLE")
            if state == "IDLE":
                print("STATE=IDLE")
                watcher.wait_for_idle(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                continue
            if state == "EXECUTOR_RUNNING":
                state = watcher.wait_for_executor()
                if state == "EXECUTOR_RUNNING":
                    continue
                if state == "EXECUTOR_CRASHED":
                    watcher.state["state"] = "HUMAN_REQUIRED"
                    watcher.save()
                    print("STATE=HUMAN_REQUIRED")
                    return
                continue
            if state == "NEXT_PROMPT_READY":
                runner = CodexRunner(project, child_project_dir=AFFOTECH_CHILD_PROJECT_DIR, session_id=AFFOTECH_EXECUTOR_SESSION_ID)
                def launch(prompt: str, result_path: Path) -> Any:
                    command_args = (["exec", "resume", runner.session_id, "-o", str(result_path), "-"] if runner.session_id else ["exec", "--ephemeral", "--sandbox", "read-only", "-C", project, "-o", str(result_path), "-"])
                    command = (["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", runner.executable, *command_args] if os.name == "nt" and runner.launcher[0].lower().endswith(".ps1") else [*runner.launcher, *command_args])
                    target = watcher.state["targetWorktree"]
                    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=None, stderr=None, cwd=target, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                    assert child.stdin is not None
                    child.stdin.write(runner.assemble_prompt(prompt).encode("utf-8")); child.stdin.close()
                    return child
                process = watcher.launch_next(launch)
                print(f"CODEX_STARTED pid={process.pid}")
                continue
            if state == "HUMAN_REQUIRED":
                print("STATE=HUMAN_REQUIRED")
                return
            if state not in {"RESULT_READY", "ARCHITECT_RUNNING"}:
                print(f"STATE={state}")
                return

            bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
            watcher.state["architectConversationId"] = conversation_id
            watcher.save()
            try:
                if watcher.state.get("state") == "RESULT_READY":
                    watcher.deliver_result(bridge)
                baseline = watcher.state.get("architectBaseline")
                if not isinstance(baseline, dict):
                    entries = bridge._assistant_entries()
                    prior = entries[:-1] if entries else []
                    snapshot = json.dumps(prior, ensure_ascii=False, separators=(",", ":"))
                    baseline = {"count": len(prior), "text_hash": hashlib.sha256(snapshot.encode()).hexdigest(), "entries": prior}
                while True:
                    try:
                        observed = bridge.wait_for_new_response(baseline, poll_interval=5.0)
                    except Exception:
                        watcher.state["state"] = "ARCHITECT_RUNNING"
                        watcher.save()
                        bridge.close()
                        bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        time.sleep(1.0)
                        continue
                    try:
                        decision = watcher.accept_architect_response(observed["text"])
                    except ValueError:
                        if int(watcher.state.get("formatRecoveryCount", 0)) >= 1:
                            watcher.state.update({"state": "HUMAN_REQUIRED", "formatRecoveryExhausted": True})
                            watcher.save()
                            print(f"STATE={watcher.state['state']}")
                            return
                        watcher.request_format_recovery(bridge)
                        baseline = watcher.state.get("architectBaseline")
                        continue
                    if decision.get("action") != "EXECUTE":
                        print(f"STATE={watcher.state['state']}")
                        return
                    break
            finally:
                bridge.close()
    except KeyboardInterrupt:
        print("STATE=STOPPED")


if __name__ == "__main__":
    main()
