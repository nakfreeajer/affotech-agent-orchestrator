"""Independently resumable real-browser qualification gates.

Gate actions call the same ArchitectPlaywright, rollover, parser, and dispatch
operations used by the existing full qualification. Each gate writes an
isolated evidence bundle and consumes the preceding gate's persisted proof.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import local_orchestrator_watcher as runtime
import orchestrator_real_browser_qualification as qualification


GATE_ORDER = (
    "g1-cdp-attach", "g2-old-handover", "g3-fresh-bootstrap",
    "g4-authority-switch", "g5-post-discussion-envelope",
    "g6-executor-dispatch",
)
GATE_PREREQUISITES = {
    "g1-cdp-attach": (),
    "g2-old-handover": ("g1-cdp-attach",),
    "g3-fresh-bootstrap": ("g2-old-handover",),
    "g4-authority-switch": ("g3-fresh-bootstrap",),
    "g5-post-discussion-envelope": ("g4-authority-switch",),
    "g6-executor-dispatch": ("g5-post-discussion-envelope",),
    "g7-full-chain": GATE_ORDER,
}
GATE_NAMES = {f"g{index + 1}": gate for index, gate in enumerate(GATE_ORDER)}
GATE_NAMES.update({
    "cdp-attach": "g1-cdp-attach", "old-handover": "g2-old-handover",
    "fresh-bootstrap": "g3-fresh-bootstrap", "authority-switch": "g4-authority-switch",
    "post-discussion-envelope": "g5-post-discussion-envelope",
    "executor-dispatch": "g6-executor-dispatch",
    "full": "g7-full-chain", "full-chain": "g7-full-chain",
})
GATE_NAMES.update({gate: gate for gate in GATE_ORDER})
GATE_NAMES["g7-full-chain"] = "g7-full-chain"


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _load_evidence(path_value: str | os.PathLike[str], repository: Path,
                   expected_gate: str) -> tuple[dict[str, Any], Path, str]:
    path = Path(path_value).resolve()
    root = (repository / ".agent-work" / "orchestrator" / "qualification").resolve()
    if not _inside(path, root) or path.name != "qualification-evidence.json":
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PATH_INVALID:" + expected_gate)
    try:
        raw = path.read_bytes()
        evidence = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("QUALIFICATION_PREREQUISITE_EVIDENCE_UNREADABLE:" + expected_gate) from error
    if (not isinstance(evidence, dict) or evidence.get("gateId") != expected_gate
            or evidence.get("result") != "PASS"):
        raise RuntimeError("QUALIFICATION_PREREQUISITE_NOT_PASSED:" + expected_gate)
    return evidence, path, _sha_bytes(raw)


def _read_evidence_artifact(evidence: dict[str, Any], evidence_path: Path,
                            path_key: str, hash_key: str) -> bytes:
    artifact = Path(str(evidence.get(path_key) or "")).resolve()
    run_root = evidence_path.parent.resolve()
    if not _inside(artifact, run_root) or not artifact.is_file():
        raise RuntimeError("QUALIFICATION_EVIDENCE_ARTIFACT_PATH_INVALID:" + path_key)
    data = artifact.read_bytes()
    if _sha_bytes(data) != evidence.get(hash_key):
        raise RuntimeError("QUALIFICATION_EVIDENCE_ARTIFACT_HASH_MISMATCH:" + path_key)
    return data


def _materialize_g5_staged_prompt(session: "_GateSession", g2: dict[str, Any],
                                 g2_path: Path, task_id: str) -> tuple[Path, bytes, dict[str, Any]]:
    """Copy the hash-verified G2 staged prompt into G5's isolated runtime root."""
    if not task_id or str(g2.get("taskId") or "") != task_id:
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_TASK_MISMATCH")
    source_bytes = _read_evidence_artifact(g2, g2_path, "stagedPromptPath", "stagedPromptSha256")
    expected_sha = str(g2.get("stagedPromptSha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or _sha_bytes(source_bytes) != expected_sha:
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_SOURCE_INVALID")
    prompts_root = (session.state_dir / "prompts").resolve()
    local_path = prompts_root / f"{task_id}.txt"
    if local_path.parent.resolve() != prompts_root or not _inside(local_path, session.state_dir):
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_PATH_INVALID")
    if local_path.exists():
        existing = local_path.read_bytes()
        if existing != source_bytes:
            raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_CONFLICT")
    else:
        temporary = prompts_root / f".{task_id}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(source_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.read_bytes() != source_bytes:
                raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_TEMP_VERIFY_FAILED")
            # The run directory is unique. Refuse a target created concurrently.
            if local_path.exists():
                raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_CONFLICT")
            os.replace(temporary, local_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    materialized = local_path.read_bytes()
    if materialized != source_bytes or len(materialized) != len(source_bytes):
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_BYTES_MISMATCH")
    materialized_sha = _sha_bytes(materialized)
    if materialized_sha != expected_sha:
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_HASH_MISMATCH")
    provenance = {
        "stagedPromptSourceGateId": "g2-old-handover",
        "stagedPromptSourceEvidencePath": str(g2_path),
        "stagedPromptSourceSha256": expected_sha,
        "stagedPromptMaterializedPath": str(local_path),
        "stagedPromptMaterializedSha256": materialized_sha,
    }
    return local_path, materialized, provenance


def _verify_g5_local_staged_prompt(path: Path, state_dir: Path, task_id: str,
                                   expected_sha: str) -> bytes:
    """Enforce G5's canonical task path and the unchanged staged-prompt hash."""
    canonical = (state_dir / "prompts" / f"{task_id}.txt").resolve()
    candidate = path.resolve()
    if candidate != canonical or not _inside(candidate, state_dir):
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_PATH_INVALID")
    try:
        data = candidate.read_bytes()
    except OSError as error:
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_UNAVAILABLE") from error
    if _sha_bytes(data) != expected_sha:
        raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_LOCAL_HASH_MISMATCH")
    return data


def _restore_g5_watcher_state(watcher: Any, accepted_state: dict[str, Any],
                              local_prompt_path: Path) -> None:
    """Restore accepted G4 state, rebinding only its qualification prompt path."""
    watcher.state = dict(accepted_state)
    watcher.state["nextPromptPath"] = str(local_prompt_path)
    watcher.save()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    qualification._atomic_json(path, value)


def _bootstrap_timing_metrics(log_path: Path) -> dict[str, Any]:
    events = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if not any(token in line for token in (
                "FRESH_BOOTSTRAP_RECONCILIATION_", "FRESH_BOOTSTRAP_LATE_OBSERVATION_")):
            continue
        fields = dict(re.findall(r"([A-Za-z][A-Za-z0-9_]*)=([^ ]*)", line))
        event_match = re.search(r"event=([^ ]+)", line)
        if event_match:
            fields["event"] = event_match.group(1)
            events.append(fields)
    polls = [event for event in events if event.get("event") == "FRESH_BOOTSTRAP_RECONCILIATION_POLL"]
    late_polls = [event for event in events if event.get("event") == "FRESH_BOOTSTRAP_LATE_OBSERVATION_POLL"]
    exact = [event for event in polls + late_polls
             if event.get("observedExactMatch", "false").lower() == "true"]
    observed_polls = polls + late_polls
    semantic_exceptions = sorted({event["semanticReaderExceptionClass"] for event in observed_polls
                                  if event.get("semanticReaderExceptionClass") not in (None, "", "None")})
    visible_exceptions = sorted({event["visibleReaderExceptionClass"] for event in observed_polls
                                 if event.get("visibleReaderExceptionClass") not in (None, "", "None")})
    complete = next((event for event in reversed(events)
                     if event.get("event") == "FRESH_BOOTSTRAP_RECONCILIATION_COMPLETE"), {})
    late = [event for event in events if "LATE_OBSERVATION" in event.get("event", "")]
    stable = next((event for event in late if event.get("event", "").endswith("STABLE")), {})
    return {
        "normalReconciliationResult": complete.get("disposition", "NOT_REACHED"),
        "normalReconciliationDeadlineMs": complete.get("deadlineMs"),
        "normalPollCount": len(polls),
        "semanticObservationExceptions": semantic_exceptions,
        "visibleObservationExceptions": visible_exceptions,
        "maxSemanticUserCount": max((int(event.get("semanticUserMessageCount", 0)) for event in observed_polls), default=0),
        "maxVisibleUserCount": max((int(event.get("visibleUserMessageCount", 0)) for event in observed_polls), default=0),
        "maxSemanticExactMatches": max((int(event.get("semanticExactMatchCount", 0)) for event in observed_polls), default=0),
        "maxVisibleExactMatches": max((int(event.get("visibleExactMatchCount", 0)) for event in observed_polls), default=0),
        "firstExactBootstrapObservedAtMs": exact[0].get("elapsedMs") if exact else None,
        "firstExactBootstrapObservedSource": exact[0].get("selectedSource") if exact else None,
        "firstExactBootstrapObservedWithinNormalWindow": any(
            event.get("event") == "FRESH_BOOTSTRAP_RECONCILIATION_POLL" for event in exact),
        "lateObservationExecuted": bool(late),
        "lateObservationExactBootstrapFound": next((event.get("found", "false").lower() == "true"
                                                     for event in reversed(late)
                                                     if event.get("event") == "FRESH_BOOTSTRAP_LATE_OBSERVATION_COMPLETE"), False),
        "lateObservationStableAtMs": stable.get("stableConfirmationAtMs"),
    }


class _GateSession:
    """One run ID, evidence/log pair, CDP inventory, and isolated runtime."""

    def __init__(self, repository: Path, endpoint: str, gate_id: str):
        self.repository = repository.resolve()
        self.endpoint = endpoint
        self.gate_id = gate_id
        self.run_id = f"qualification-{gate_id}-{uuid.uuid4().hex}"
        self.root = self.repository / ".agent-work" / "orchestrator" / "qualification" / self.run_id
        self.state_dir = self.root / "state"
        self.evidence_path = self.root / "qualification-evidence.json"
        self.log_path = self.repository / ".agent-work" / "orchestrator" / "logs" / "qualification" / self.run_id / "qualification.log"
        self.state_path = self.repository / ".agent-work" / "orchestrator" / "state.json"
        self.production_log_path = self.repository / ".agent-work" / "orchestrator" / "logs" / "orchestrator.log"
        self.state_hash_before, self.state_size_before = qualification._sha(self.state_path)
        self.log_hash_before, self.log_size_before = qualification._sha(self.production_log_path)
        if self.state_hash_before is None:
            raise RuntimeError("QUALIFICATION_PRODUCTION_STATE_INTEGRITY_BASELINE_UNAVAILABLE")
        self.evidence: dict[str, Any] = {
            "runId": self.run_id, "gateId": gate_id, "result": "INCONCLUSIVE",
            "runtimeContext": "QUALIFICATION", "realBrowser": True, "fakeExecutor": True,
            "productionStateMutation": False, "productionArchitectMutation": False,
            "productionStateSha256Before": self.state_hash_before,
            "productionStateBytesBefore": self.state_size_before,
            "productionLogSha256Before": self.log_hash_before,
            "productionLogBytesBefore": self.log_size_before,
            "ownedPages": [], "preExistingTargets": [], "protectedConversationIds": [],
        }
        self.handle = None
        self.playwright_runtime = None
        self.browser = None
        self.raw_context = None
        self.owned_context = None
        self.logger = None
        self.runtime_run_id = self.run_id
        self.preexisting_guids: set[str] = set()
        self.protected_ids: set[str] = set(runtime.QUALIFICATION_PROTECTED_CONVERSATION_IDS)
        self.close_requests: list[str] = []
        self._previous_env: dict[str, str | None] = {}
        self._patched_submit_impl = None

    def start(self) -> None:
        (self.state_dir / "prompts").mkdir(parents=True, exist_ok=False)
        (self.state_dir / "results").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=False)
        self.handle = self.log_path.open("x", encoding="utf-8", newline="\n")
        self.handle.write("runtimeContext=QUALIFICATION realBrowser=true fakeExecutor=true productionStateMutation=false productionArchitectMutation=false\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        qualification._write_event(self.handle, "QUALIFICATION_GATE_BEGIN", {
            "runId": self.run_id, "gateId": self.gate_id, "endpoint": self.endpoint,
            "productionStateSha256": self.state_hash_before,
            "productionLogSha256": self.log_hash_before,
        })
        self._persist()

    def connect_and_inventory(self) -> None:
        env_keys = (
            "AFFOTECH_RUNTIME_CONTEXT", "AFFOTECH_QUALIFICATION_RUN_ID",
            "AFFOTECH_QUALIFICATION_RUN_ROOT", "AFFOTECH_QUALIFICATION_LOG_ROOT",
            "AFFOTECH_QUALIFICATION_LOG_PATH", "AFFOTECH_QUALIFICATION_OWNED_IDS",
            "AFFOTECH_QUALIFICATION_PROTECTED_IDS", "AFFOTECH_ORCHESTRATOR_STATE_DIR",
        )
        self._previous_env = {key: os.environ.get(key) for key in env_keys}
        os.environ.update({
            "AFFOTECH_RUNTIME_CONTEXT": "QUALIFICATION",
            "AFFOTECH_QUALIFICATION_RUN_ID": self.run_id,
            "AFFOTECH_QUALIFICATION_RUN_ROOT": str(self.root),
            "AFFOTECH_QUALIFICATION_LOG_ROOT": str(self.log_path.parent),
            "AFFOTECH_QUALIFICATION_LOG_PATH": str(self.log_path),
            "AFFOTECH_QUALIFICATION_OWNED_IDS": "",
            "AFFOTECH_QUALIFICATION_PROTECTED_IDS": ",".join(sorted(self.protected_ids)),
            "AFFOTECH_ORCHESTRATOR_STATE_DIR": str(self.state_dir),
        })
        from playwright.sync_api import sync_playwright
        self.playwright_runtime = sync_playwright().start()
        self.browser = self.playwright_runtime.chromium.connect_over_cdp(self.endpoint, timeout=10000)
        contexts = list(self.browser.contexts)
        if len(contexts) != 1:
            raise RuntimeError(f"QUALIFICATION_CDP_CONTEXT_AMBIGUOUS:{len(contexts)}")
        self.raw_context = contexts[0]
        inventory = qualification._target_inventory(self.browser)
        self.preexisting_guids = {row["pageGuid"] for row in inventory}
        self.protected_ids.update(row["conversationId"] for row in inventory if row.get("conversationId"))
        self.evidence["preExistingTargets"] = inventory
        self.evidence["protectedConversationIds"] = sorted(self.protected_ids)
        qualification._write_event(self.handle, "QUALIFICATION_CDP_INVENTORY_RECORDED", {
            "pageCount": len(inventory), "targets": inventory,
            "protectedIds": sorted(self.protected_ids), "preExistingPageGuids": sorted(self.preexisting_guids),
        })
        self.owned_context = qualification._OwnedContext(
            self.raw_context, self.close_requests,
            lambda name, fields: qualification._write_event(self.handle, name, fields),
            run_id=self.run_id, evidence=self.evidence, persist_evidence=self._persist,
        )
        self.owned_context.inventory_complete = True
        self.logger, self.runtime_run_id, path = runtime.initialize_runtime_logging(
            self.state_dir, self.run_id, "QUALIFICATION")
        if Path(path).resolve() != self.log_path.resolve():
            raise RuntimeError("QUALIFICATION_LOG_DESTINATION_MISMATCH")
        qualification._write_event(self.handle, "QUALIFICATION_BROWSER_GUARD_READY", {
            "preExistingPagesUntouchable": True, "realBrowser": True,
        })
        self._persist()

    def allow_owned_ids(self, ids: set[str]) -> None:
        self.protected_ids.update(self.evidence.get("protectedConversationIds", []))
        self.protected_ids.difference_update(ids)
        os.environ["AFFOTECH_QUALIFICATION_PROTECTED_IDS"] = ",".join(sorted(self.protected_ids))
        os.environ["AFFOTECH_QUALIFICATION_OWNED_IDS"] = ",".join(sorted(ids))

    def evidence_view(self, prior: dict[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
        view = dict(prior)
        view["protectedConversationIds"] = sorted(self.protected_ids - allowed_ids)
        return view

    def adopt(self, role_evidence: dict[str, Any], role: str, allowed_ids: set[str]):
        view = self.evidence_view(role_evidence, allowed_ids)
        raw = qualification.find_owned_qualification_page(self.browser, view, role)
        row = next(row for row in role_evidence["ownedPages"] if row.get("role") == role)
        wrapper = self.owned_context.adopt_page(raw, row)
        return wrapper

    def _persist(self) -> None:
        _write_json(self.evidence_path, self.evidence)

    def finish(self, result: str, **fields: Any) -> dict[str, Any]:
        state_hash_after, state_bytes_after = qualification._sha(self.state_path)
        log_hash_after, log_bytes_after = qualification._sha(self.production_log_path)
        self.evidence.update(fields)
        if self.gate_id == "g3-fresh-bootstrap":
            self.evidence.update(_bootstrap_timing_metrics(self.log_path))
        self.evidence.update({
            "result": result, "terminalStatus": "QUALIFICATION_COMPLETE" if result == "PASS" else "QUALIFICATION_FAILED",
            "productionStateSha256After": state_hash_after, "productionStateBytesAfter": state_bytes_after,
            "productionLogSha256After": log_hash_after, "productionLogBytesAfter": log_bytes_after,
            "productionStateUnchanged": state_hash_after == self.state_hash_before,
            "productionLogUnchanged": log_hash_after == self.log_hash_before,
            "productionStateMutation": state_hash_after != self.state_hash_before,
            "productionArchitectMutation": log_hash_after != self.log_hash_before,
            "unintendedMutationCount": 0,
        })
        if result == "PASS":
            qualification._write_event(self.handle, "QUALIFICATION_GATE_PASS", {"runId": self.run_id, "gateId": self.gate_id})
        self._persist()
        return self.evidence

    def fail(self, error: BaseException, result: str = "BLOCKED") -> dict[str, Any]:
        self.evidence["terminalFailure"] = {
            "errorClass": type(error).__name__, "reason": str(error)[:500],
            "ownedPageCount": len(self.evidence.get("ownedPages", [])),
        }
        qualification._write_event(self.handle, "QUALIFICATION_GATE_BLOCKED", {
            "runId": self.run_id, "gateId": self.gate_id,
            "errorClass": type(error).__name__, "reason": str(error)[:300],
            "ownedPageCount": len(self.evidence.get("ownedPages", [])),
        })
        return self.finish(result)

    def close(self) -> None:
        if self.logger is not None:
            for handler in list(self.logger.handlers):
                self.logger.removeHandler(handler)
                handler.close()
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self.playwright_runtime is not None:
            try:
                self.playwright_runtime.stop()
            except Exception:
                pass
        if self._patched_submit_impl is not None:
            runtime.ArchitectPlaywright.submit_result_bounded = self._patched_submit_impl
        if self.handle is not None:
            self.handle.close()


def _synthetic_inputs(session: _GateSession) -> dict[str, Any]:
    task_id = "QUAL-TASK-" + uuid.uuid4().hex[:12].upper()
    tx_id = "QUAL-TX-" + uuid.uuid4().hex
    worktree = session.root / "synthetic-worktree"
    worktree.mkdir()
    staged = ("Synthetic qualification only. No business, customer, AFFOTECH, or production work.\n"
              "This exact text is used only to test staged-prompt identity preservation.\n")
    prompt_path = session.state_dir / "prompts" / f"{task_id}.txt"
    prompt_path.write_text(staged, encoding="utf-8", newline="")
    prompt_hash = _sha_bytes(prompt_path.read_bytes())
    handover_body = ("Synthetic disposable qualification handover.\n"
                     "Already-completed synthetic decision: classification=ACCEPTED; action=EXECUTE.\n"
                     f"The complete synthetic next prompt is exactly:\n{staged}"
                     "For the fresh session's formatting-only envelope request, preserve that decision and prompt exactly.\n")
    expected = runtime.make_handover_envelope(tx_id, task_id, handover_body)
    request = ("This is a disposable browser qualification, not business work. Return exactly the following "
               "synthetic handover envelope as your entire response, with no Markdown fences or extra text:\n\n" + expected)
    qualification._persist_initial_qualification_inputs(
        session.root, session.evidence, task_id, tx_id, staged, expected, request, session._persist)
    return {"taskId": task_id, "transactionId": tx_id, "worktreePath": str(worktree),
            "stagedPromptPath": str(prompt_path), "stagedPromptSha256": prompt_hash,
            "expectedHandoverPath": str(session.root / "inputs" / "expected-handover.txt"),
            "expectedHandoverSha256": _sha_bytes(expected.encode()), "oldRequest": request}


def _install_submit_recorder(session: _GateSession, values: dict[str, Any], old_request: str = ""):
    original = runtime.ArchitectPlaywright.submit_result_bounded
    recorder = qualification._QualificationInputSubmitter(
        original, session.root, session.evidence, session._persist,
        values["taskId"], values["transactionId"], old_request)
    session._patched_submit_impl = original
    runtime.ArchitectPlaywright.submit_result_bounded = recorder
    return recorder


def _prerequisite(paths: list[str], index: int, expected_gate: str, repository: Path):
    if len(paths) <= index:
        raise RuntimeError("QUALIFICATION_PREREQUISITE_REQUIRED:" + expected_gate)
    return _load_evidence(paths[index], repository, expected_gate)


def _load_recorded_prerequisite(owner: dict[str, Any], owner_path: Path,
                                expected_gate: str, repository: Path):
    """Resolve one nested prerequisite only through its persisted provenance record."""
    references = owner.get("prerequisiteEvidence")
    if not isinstance(references, list):
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_INVALID:" + expected_gate)
    matches = [row for row in references if isinstance(row, dict) and row.get("gateId") == expected_gate]
    if len(matches) != 1:
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_INVALID:" + expected_gate)
    reference = matches[0]
    path_value, expected_hash = reference.get("path"), reference.get("sha256")
    if not isinstance(path_value, str) or not path_value or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_INVALID:" + expected_gate)
    # Keep any existing convenience fields bound to the same original reference.
    alias_prefix = expected_gate.split("-", 1)[0]
    alias_path, alias_hash = owner.get(alias_prefix + "EvidencePath"), owner.get(alias_prefix + "EvidenceSha256")
    if alias_path is not None and str(Path(str(alias_path)).resolve()) != str(Path(path_value).resolve()):
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_MISMATCH:" + expected_gate)
    if alias_hash is not None and alias_hash != expected_hash:
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_MISMATCH:" + expected_gate)
    evidence, evidence_path, digest = _load_evidence(path_value, repository, expected_gate)
    if digest != expected_hash:
        raise RuntimeError("QUALIFICATION_PREREQUISITE_HASH_MISMATCH:" + expected_gate)
    # The owner file itself must be the immediate gate evidence that supplied
    # this reference; this also guards accidental use of a sibling run record.
    if owner_path.name != "qualification-evidence.json" or not _inside(owner_path, repository / ".agent-work" / "orchestrator" / "qualification"):
        raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_INVALID:" + expected_gate)
    return evidence, evidence_path, digest


def _resolve_g4_provenance(g3: dict[str, Any], g3_path: Path, repository: Path):
    """Validate G3's G2 provenance and each artifact against its owning run."""
    g2, g2_path, g2_hash = _load_recorded_prerequisite(g3, g3_path, "g2-old-handover", repository)
    for key in ("taskId", "transactionId", "oldConversationId", "handoverResponsePath", "handoverResponseSha256"):
        if g3.get(key) != g2.get(key):
            raise RuntimeError("QUALIFICATION_PREREQUISITE_PROVENANCE_MISMATCH:g2-old-handover:" + key)
    bootstrap = _read_evidence_artifact(g3, g3_path, "bootstrapPath", "bootstrapSha256")
    handover = _read_evidence_artifact(g2, g2_path, "handoverResponsePath", "handoverResponseSha256")
    return g2, g2_path, g2_hash, bootstrap, handover


def _create_watcher(session: _GateSession, values: dict[str, Any], old_id: str,
                    *, fresh_id: str | None = None, prompt_path: str | None = None):
    watcher = runtime.LocalFirstOrchestrator(str(session.repository), session.state_dir)
    watcher.runtime_logger = session.logger
    watcher.runtime_run_id = session.runtime_run_id
    task_id = values["taskId"]
    tx_id = values["transactionId"]
    prompt_path = prompt_path or values["stagedPromptPath"]
    worktree = values.get("worktreePath") or str(session.root / "synthetic-worktree")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": values.get("completeTaskId", "QUAL-COMPLETE-" + uuid.uuid4().hex[:8].upper()),
        "nextTaskId": task_id, "lastCompletedTaskId": None, "nextPromptPath": prompt_path,
        "taskWorktrees": {task_id: {"taskId": task_id, "worktreePath": worktree}},
        "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": False,
        "handoverRequested": True, "handoverReady": False,
        "rolloverTransactionId": tx_id, "rolloverTransactionTaskId": task_id,
        "rolloverAttemptedForTaskId": task_id, "rolloverMaintenanceState": "DEFERRED",
        "rolloverRecoveryState": "RECOVERING", "rolloverRecoveryEpoch": 1,
        "rolloverAutomaticRecoveryEpochCount": 1, "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverFreshPageCreated": fresh_id is not None,
        "rolloverFreshCandidateConversationId": fresh_id,
        "rolloverFreshCandidateState": "ACK_PENDING" if fresh_id else None,
        "rolloverFreshBootstrapPayloadHash": values.get("bootstrapSha256"),
        "pending_handover": None, "rolloverHandoverResponseIdentity": None,
        "postDiscussionEnvelopeRequired": True, "postDiscussionResumeEpoch": 1,
        "postDiscussionProtocolTaskId": task_id, "postDiscussionProtocolTransactionId": tx_id,
        "postDiscussionProtocolRolloverCommittedTransactionId": None,
        "postDiscussionEnvelopeRepairAttempted": False, "postDiscussionEnvelopeRepairAwaiting": False,
        "postDiscussionEnvelopeRepairTaskId": task_id, "postDiscussionEnvelopeRepairEpoch": 1,
        "postDiscussionProtocolBaseline": {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()},
        "architectConversationId": old_id, "executorSessionId": "QUALIFICATION-FAKE-SESSION",
        "executorSessionMode": "QUALIFICATION_FAKE", "executorProcessState": "STOPPED",
        "executorLaunchState": "NOT_LAUNCHED", "executorActiveWriter": False,
        "governedExecutorActiveWriter": False, "discussionPauseActive": False,
    })
    watcher.state["lastCompletedTaskId"] = watcher.state["taskId"]
    watcher.save()
    watcher.session_rollover = runtime.ArchitectSessionRollover(watcher)
    return watcher


def _wait_ready(watcher: Any, old_bridge: Any, fresh_page: Any) -> tuple[str, int]:
    fresh_id = runtime.architect_conversation_id_from_url(str(fresh_page.url))
    bridge = runtime.ArchitectPlaywright(fresh_page)
    bridge.runtime_logger = watcher.runtime_logger
    bridge.runtime_run_id = watcher.runtime_run_id
    bridge.runtime_watcher = watcher
    bridge.runtime_conversation_id = fresh_id
    deadline = time.monotonic() + 30.0
    stable = 0
    source = "NONE"
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        if bridge.generation_visible():
            time.sleep(0.25)
            continue
        ready, source = watcher.session_rollover._fresh_page_ready_visible(fresh_page)
        stable = stable + 1 if ready else 0
        if stable >= 2:
            return source, polls
        if not ready and source == "SEMANTIC_HISTORY":
            entries = bridge._assistant_entries()
            latest = entries[-1].get("text", "") if entries and isinstance(entries[-1], dict) else ""
            if latest.strip():
                raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ACK_INVALID")
        time.sleep(0.25)
    raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ACK_TIMEOUT")


def _result_without_browser(session: _GateSession, result: str, error: BaseException,
                           prerequisite_refs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    if prerequisite_refs:
        session.evidence["prerequisiteEvidence"] = prerequisite_refs
    session.evidence["terminalFailure"] = {"errorClass": type(error).__name__, "reason": str(error)[:500]}
    return session.finish(result)


def _run_one(gate_id: str, repository: Path, endpoint: str,
             prerequisite_paths: list[str]) -> dict[str, Any]:
    session = _GateSession(repository, endpoint, gate_id)
    session.start()
    prerequisite_refs: list[dict[str, str]] = []
    try:
        if gate_id == "g1-cdp-attach":
            session.connect_and_inventory()
            return session.finish("PASS", browserInventoryReached=True,
                                 pageInventoryReached=True, pageMutationCount=0)

        needed = GATE_PREREQUISITES[gate_id]
        loaded = []
        for index, expected in enumerate(needed):
            evidence, path, digest = _prerequisite(prerequisite_paths, index, expected, session.repository)
            loaded.append(evidence)
            prerequisite_refs.append({"gateId": expected, "path": str(path), "sha256": digest})
        session.evidence["prerequisiteEvidence"] = prerequisite_refs
        g4_provenance = None
        if gate_id == "g4-authority-switch":
            g4_provenance = _resolve_g4_provenance(
                loaded[0], Path(prerequisite_refs[0]["path"]), session.repository)
        session.connect_and_inventory()

        if gate_id == "g2-old-handover":
            values = _synthetic_inputs(session)
            values["worktreePath"] = str(session.root / "synthetic-worktree")
            recorder = _install_submit_recorder(session, values, values["oldRequest"])
            old_page = session.owned_context.new_page()
            old_page.goto("https://chatgpt.com/")
            old_bridge = runtime.ArchitectPlaywright(old_page)
            old_bridge.runtime_logger = session.logger
            old_bridge.runtime_run_id = session.runtime_run_id
            old_bridge.runtime_conversation_id = "QUALIFICATION_PENDING_OLD"
            old_bridge.runtime_expected_handover_transaction_id = values["transactionId"]
            old_bridge.runtime_expected_handover_task_id = values["taskId"]
            baseline = old_bridge.assistant_baseline()
            old_bridge.submit_result_bounded(values["oldRequest"], timeout=45.0)
            observed = old_bridge.wait_for_new_response(baseline, poll_interval=0.5)
            if not isinstance(observed, dict) or observed.get("state") != "COMPLETED":
                raise RuntimeError("QUALIFICATION_OLD_SYNTHETIC_HANDOVER_NOT_COMPLETED")
            handover = observed.get("text")
            parsed = runtime.parse_handover_envelope(handover) if isinstance(handover, str) else None
            if (not isinstance(parsed, dict)
                    or parsed.get("transactionId") != values["transactionId"]
                    or parsed.get("taskId") != values["taskId"]):
                raise RuntimeError("QUALIFICATION_OLD_SYNTHETIC_HANDOVER_INVALID")
            old_id = runtime.architect_conversation_id_from_url(str(old_page.url))
            if old_id in session.protected_ids:
                raise RuntimeError("QUALIFICATION_PROTECTED_CONVERSATION_BLOCKED:old-created")
            handover_path = session.root / "handover-response.txt"
            handover_path.write_text(handover, encoding="utf-8", newline="")
            metadata = session.evidence.get("inputs", {}).get("OLD_ARCHITECT_REQUEST", {})
            if metadata.get("sendAttemptCount") != 1:
                raise RuntimeError("QUALIFICATION_OLD_HANDOVER_SEND_COUNT_INVALID")
            fields = {**values, "oldConversationId": old_id,
                      "handoverResponsePath": str(handover_path),
                      "handoverResponseSha256": _sha_bytes(handover.encode()),
                      "handoverValidated": True, "handoverSendCount": 1,
                      "oldArchitectRetireCount": 0}
            return session.finish("PASS", **fields)

        if gate_id == "g3-fresh-bootstrap":
            g2 = loaded[0]
            handover_path = Path(g2["handoverResponsePath"]).resolve()
            g2_evidence_path = Path(prerequisite_refs[0]["path"])
            _read_evidence_artifact(g2, g2_evidence_path, "stagedPromptPath", "stagedPromptSha256")
            _read_evidence_artifact(g2, g2_evidence_path, "expectedHandoverPath", "expectedHandoverSha256")
            handover = _read_evidence_artifact(
                g2, Path(prerequisite_refs[0]["path"]), "handoverResponsePath", "handoverResponseSha256").decode("utf-8")
            parsed_handover = runtime.parse_handover_envelope(handover)
            if (not isinstance(parsed_handover, dict)
                    or parsed_handover.get("transactionId") != g2.get("transactionId")
                    or parsed_handover.get("taskId") != g2.get("taskId")):
                raise RuntimeError("QUALIFICATION_HANDOVER_EVIDENCE_INVALID")
            old_id = str(g2["oldConversationId"])
            session.allow_owned_ids({old_id})
            old_page = session.adopt(g2, "OLD_ARCHITECT", {old_id})
            values = {key: g2[key] for key in (
                "taskId", "transactionId", "worktreePath", "stagedPromptPath",
                "stagedPromptSha256", "expectedHandoverPath", "expectedHandoverSha256",
            )}
            values["completeTaskId"] = "QUAL-COMPLETE-" + uuid.uuid4().hex[:8].upper()
            values["worktreePath"] = g2["worktreePath"]
            values["oldRequest"] = ""
            watcher = _create_watcher(session, values, old_id)
            old_bridge = runtime.ArchitectPlaywright(old_page)
            old_bridge.runtime_logger = session.logger
            old_bridge.runtime_run_id = session.runtime_run_id
            old_bridge.runtime_watcher = watcher
            old_bridge.runtime_conversation_id = old_id
            old_bridge.runtime_expected_handover_transaction_id = values["transactionId"]
            old_bridge.runtime_expected_handover_task_id = values["taskId"]
            recorder = _install_submit_recorder(session, values)
            bootstrap = runtime.fresh_architect_bootstrap_payload(handover)
            recorder.bootstrap_payload = bootstrap
            original_unsent = runtime.ArchitectPlaywright.reconcile_unsent_submission
            submit_wrapper, ack_state = qualification._inject_post_send_ack_ambiguity(
                recorder, handover,
                lambda event, fields: qualification._write_event(session.handle, event, fields))
            runtime.ArchitectPlaywright.submit_result_bounded = submit_wrapper
            runtime.ArchitectPlaywright.reconcile_unsent_submission = lambda bridge, payload, *args, **kwargs: "AMBIGUOUS"
            try:
                fresh_page = old_bridge.open_fresh_with_handover(handover)
            finally:
                runtime.ArchitectPlaywright.submit_result_bounded = recorder
                runtime.ArchitectPlaywright.reconcile_unsent_submission = original_unsent
            session.evidence["ackAmbiguityExercised"] = bool(ack_state["done"])
            if not ack_state["done"]:
                raise RuntimeError("QUALIFICATION_ACK_AMBIGUITY_INJECTION_NOT_REACHED")
            meta = session.evidence.get("inputs", {}).get("FRESH_ARCHITECT_BOOTSTRAP", {})
            if meta.get("sendAttemptCount") != 1:
                raise RuntimeError("QUALIFICATION_FRESH_BOOTSTRAP_SEND_COUNT_INVALID")
            fresh_id = runtime.architect_conversation_id_from_url(str(fresh_page.url))
            if fresh_id in session.protected_ids or fresh_id == old_id:
                raise RuntimeError("QUALIFICATION_FRESH_CONVERSATION_ID_INVALID")
            source, polls = _wait_ready(watcher, old_bridge, fresh_page)
            if getattr(old_bridge, "alternateSendAttempted", False):
                raise RuntimeError("QUALIFICATION_FRESH_BOOTSTRAP_RESEND_DETECTED")
            bootstrap_path = session.root / "fresh-bootstrap.txt"
            bootstrap_path.write_text(bootstrap, encoding="utf-8", newline="")
            fields = {
                **values, "oldConversationId": old_id, "freshConversationId": fresh_id,
                "g2EvidencePath": prerequisite_refs[0]["path"],
                "g2EvidenceSha256": prerequisite_refs[0]["sha256"],
                "handoverResponsePath": str(handover_path),
                "handoverResponseSha256": g2["handoverResponseSha256"],
                "bootstrapPath": str(bootstrap_path), "bootstrapSha256": _sha_bytes(bootstrap.encode()),
                "freshBootstrapSendCount": 1, "freshBootstrapResendCount": 0,
                "readyObserved": True, "readyObserver": source, "readyPollCount": polls,
                "authorityCommitted": False, "freshTabCount": 1,
                "preAuthorityWatcherState": watcher.state,
                "borrowedPageEvidence": [{"gateId": "g2-old-handover", "role": "OLD_ARCHITECT"}],
            }
            return session.finish("PASS", **fields)

        if gate_id == "g4-authority-switch":
            g3 = loaded[0]
            assert g4_provenance is not None
            g2, g2_path, g2_hash, _bootstrap, handover_bytes = g4_provenance
            old_id, fresh_id = str(g3["oldConversationId"]), str(g3["freshConversationId"])
            allowed = {old_id, fresh_id}
            session.allow_owned_ids(allowed)
            old_page = session.adopt(g2, "OLD_ARCHITECT", allowed)
            fresh_page = session.adopt(g3, "FRESH_ARCHITECT", allowed)
            values = {key: g3[key] for key in (
                "taskId", "transactionId", "worktreePath", "stagedPromptPath",
                "stagedPromptSha256", "expectedHandoverPath", "expectedHandoverSha256",
                "completeTaskId", "bootstrapSha256",
            )}
            values["oldRequest"] = ""
            watcher = _create_watcher(session, values, old_id, fresh_id=fresh_id)
            old_bridge = runtime.ArchitectPlaywright(old_page)
            old_bridge.runtime_logger = session.logger
            old_bridge.runtime_run_id = session.runtime_run_id
            old_bridge.runtime_watcher = watcher
            old_bridge.runtime_conversation_id = old_id
            old_bridge._fresh_candidate_page = fresh_page
            handover = handover_bytes.decode("utf-8")
            watcher.session_rollover.persist_validated_handover(handover)
            proven = watcher.session_rollover._fresh_candidate_proven(
                fresh_page, runtime.fresh_architect_bootstrap_payload(handover), fresh_id)
            if not proven:
                raise RuntimeError("QUALIFICATION_G3_FRESH_READY_PROOF_INVALID")
            original_submit = runtime.ArchitectPlaywright.submit_result_bounded
            def block_any_resend(_bridge, _payload, *_args, **_kwargs):
                raise RuntimeError("QUALIFICATION_UNEXPECTED_SEND_BLOCKED")
            runtime.ArchitectPlaywright.submit_result_bounded = block_any_resend
            session._patched_submit_impl = original_submit
            try:
                committed = watcher.session_rollover.complete_from_response(old_bridge, handover)
            finally:
                runtime.ArchitectPlaywright.submit_result_bounded = original_submit
                session._patched_submit_impl = None
            if not committed or watcher.state.get("architectConversationId") != fresh_id:
                raise RuntimeError("QUALIFICATION_AUTHORITY_COMMIT_NOT_PROVEN")
            if not any(item.startswith("DEFERRED:") for item in session.close_requests):
                raise RuntimeError("QUALIFICATION_OLD_AUTHORITY_RETIREMENT_NOT_REQUESTED")
            fields = {
                **values, "oldConversationId": old_id, "freshConversationId": fresh_id,
                "g3EvidencePath": prerequisite_refs[0]["path"],
                "g3EvidenceSha256": prerequisite_refs[0]["sha256"],
                "g2EvidencePath": str(g2_path), "g2EvidenceSha256": g2_hash,
                "authorityCommitCount": 1, "oldArchitectRetireCount": 1,
                "oldArchitectRetirementDeferred": True,
                "freshBootstrapSendCount": g3["freshBootstrapSendCount"],
                "freshBootstrapResendCount": 0,
                "postAuthorityWatcherState": watcher.state,
                "borrowedPageEvidence": [
                    {"gateId": "g2-old-handover", "role": "OLD_ARCHITECT"},
                    {"gateId": "g3-fresh-bootstrap", "role": "FRESH_ARCHITECT"},
                ],
            }
            return session.finish("PASS", **fields)

        if gate_id == "g5-post-discussion-envelope":
            g4 = loaded[0]
            g3, g3_path, g3_hash = _load_recorded_prerequisite(
                g4, Path(prerequisite_refs[0]["path"]), "g3-fresh-bootstrap", session.repository)
            g2, g2_path, _ = _load_recorded_prerequisite(g3, g3_path, "g2-old-handover", session.repository)
            if (g2.get("taskId") != g4.get("taskId")
                    or g2.get("transactionId") != g4.get("transactionId")
                    or g2.get("stagedPromptSha256") != g4.get("stagedPromptSha256")):
                raise RuntimeError("QUALIFICATION_G5_STAGED_PROMPT_IDENTITY_MISMATCH")
            local_prompt_path, prompt_bytes, prompt_provenance = _materialize_g5_staged_prompt(
                session, g2, g2_path, str(g2.get("taskId") or ""))
            fresh_id = str(g4["freshConversationId"])
            session.allow_owned_ids({fresh_id})
            fresh_page = session.adopt(g3, "FRESH_ARCHITECT", {fresh_id})
            values = {key: g4[key] for key in (
                "taskId", "transactionId", "worktreePath", "stagedPromptPath",
                "stagedPromptSha256", "expectedHandoverPath", "expectedHandoverSha256",
                "completeTaskId", "bootstrapSha256",
            )}
            # Keep the authoritative staged-prompt identity while moving only
            # its qualification-local path into G5's isolated runtime.
            values["stagedPromptPath"] = str(local_prompt_path)
            values["oldRequest"] = ""
            watcher = _create_watcher(session, values, str(g4["oldConversationId"]), fresh_id=fresh_id)
            _restore_g5_watcher_state(watcher, g4["postAuthorityWatcherState"], local_prompt_path)
            session.evidence.update({**prompt_provenance, "stagedPromptPath": str(local_prompt_path),
                                     "stagedPromptSha256": values["stagedPromptSha256"]})
            session._persist()
            fresh_bridge = runtime.ArchitectPlaywright(fresh_page)
            fresh_bridge.runtime_logger = session.logger
            fresh_bridge.runtime_run_id = session.runtime_run_id
            fresh_bridge.runtime_watcher = watcher
            fresh_bridge.runtime_conversation_id = fresh_id
            recorder = _install_submit_recorder(session, values)
            recorder.stage_hint = "POST_DISCUSSION_REPAIR_REQUEST"
            _verify_g5_local_staged_prompt(
                local_prompt_path, session.state_dir, values["taskId"], values["stagedPromptSha256"])
            if not watcher.request_post_discussion_envelope_repair(fresh_bridge):
                raise RuntimeError("QUALIFICATION_PROTOCOL_REPAIR_REQUEST_FAILED")
            repair_meta = session.evidence.get("inputs", {}).get("POST_DISCUSSION_REPAIR_REQUEST", {})
            if repair_meta.get("sendAttemptCount") != 1:
                raise RuntimeError("QUALIFICATION_REPAIR_REQUEST_SEND_COUNT_INVALID")
            baseline = watcher.state.get("postDiscussionProtocolBaseline")
            observed = fresh_bridge.wait_for_new_response(baseline, poll_interval=0.5)
            if not isinstance(observed, dict) or observed.get("state") != "COMPLETED":
                raise RuntimeError("QUALIFICATION_SYNTHETIC_ENVELOPE_NOT_COMPLETED")
            response = observed.get("text", "")
            response_path = session.root / "post-discussion-response.txt"
            response_path.write_text(response, encoding="utf-8", newline="")
            disposition = watcher.reconcile_post_discussion_response(fresh_bridge, response, completed=True)
            if disposition != "EXECUTE" or watcher.state.get("postDiscussionEnvelopeRequired") is not False:
                raise RuntimeError("QUALIFICATION_SYNTHETIC_ENVELOPE_NOT_ACCEPTED:" + str(disposition))
            prompt_bytes = _verify_g5_local_staged_prompt(
                local_prompt_path, session.state_dir, values["taskId"], values["stagedPromptSha256"])
            fields = {
                **values, **prompt_provenance, "g4EvidencePath": prerequisite_refs[0]["path"],
                "g4EvidenceSha256": prerequisite_refs[0]["sha256"],
                "g3EvidencePath": str(g3_path), "g3EvidenceSha256": g3_hash,
                "freshConversationId": fresh_id, "repairRequestCount": 1,
                "postDiscussionResponseObserved": True,
                "postDiscussionResponsePath": str(response_path),
                "postDiscussionResponseSha256": _sha_bytes(response.encode()),
                "orchestratorResultValid": True, "completeStagedPromptRecovered": True,
                "protocolRecovered": True, "stagedPromptSha256Verified": _sha_bytes(prompt_bytes),
                "postRecoveryWatcherState": watcher.state,
                "borrowedPageEvidence": [{"gateId": "g3-fresh-bootstrap", "role": "FRESH_ARCHITECT"}],
            }
            return session.finish("PASS", **fields)

        if gate_id == "g6-executor-dispatch":
            g5 = loaded[0]
            g4, g4_path, g4_hash = _load_recorded_prerequisite(
                g5, Path(prerequisite_refs[0]["path"]), "g4-authority-switch", session.repository)
            g3, g3_path, _ = _load_recorded_prerequisite(
                g4, g4_path, "g3-fresh-bootstrap", session.repository)
            g2, g2_path, _ = _load_recorded_prerequisite(g3, g3_path, "g2-old-handover", session.repository)
            _read_evidence_artifact(g2, g2_path, "stagedPromptPath", "stagedPromptSha256")
            fresh_id = str(g5["freshConversationId"])
            session.allow_owned_ids({fresh_id})
            fresh_page = session.adopt(g3, "FRESH_ARCHITECT", {fresh_id})
            values = {key: g5[key] for key in (
                "taskId", "transactionId", "worktreePath", "stagedPromptPath",
                "stagedPromptSha256", "completeTaskId",
            )}
            watcher = _create_watcher(session, values, str(g4["oldConversationId"]), fresh_id=fresh_id)
            watcher.state = dict(g5["postRecoveryWatcherState"])
            watcher.save()
            if watcher.state.get("postDiscussionEnvelopeRequired") is not False:
                raise RuntimeError("QUALIFICATION_PROTOCOL_NOT_RECOVERED")
            prompt_bytes = Path(values["stagedPromptPath"]).read_bytes()
            if _sha_bytes(prompt_bytes) != values["stagedPromptSha256"]:
                raise RuntimeError("QUALIFICATION_STAGED_PROMPT_HASH_MISMATCH")
            spy = runtime._QualificationExecutorLaunchSpy(watcher, session.logger, session.runtime_run_id)
            dispatched = runtime.run_next_prompt_ready_once(
                watcher, spy, endpoint, watcher.discussion_pause_active, session.logger, session.runtime_run_id)
            if dispatched is None or spy.count != 1:
                raise RuntimeError("QUALIFICATION_EXECUTOR_SPY_NOT_REACHED")
            fields = {
                **values, "g5EvidencePath": prerequisite_refs[0]["path"],
                "g5EvidenceSha256": prerequisite_refs[0]["sha256"],
                "g4EvidencePath": str(g4_path), "g4EvidenceSha256": g4_hash,
                "freshConversationId": fresh_id, "protocolRecovered": True,
                "executorLaunchSpyCount": spy.count, "realExecutorLaunched": False,
                "stagedPromptSha256Verified": _sha_bytes(prompt_bytes),
                "borrowedPageEvidence": [{"gateId": "g3-fresh-bootstrap", "role": "FRESH_ARCHITECT"}],
            }
            return session.finish("PASS", **fields)

        raise RuntimeError("QUALIFICATION_GATE_UNSUPPORTED:" + gate_id)
    except Exception as error:
        result = "INCONCLUSIVE" if str(error).startswith("QUALIFICATION_PREREQUISITE_") else "BLOCKED"
        return session.fail(error, result)
    finally:
        session.close()


def run_gate(gate: str, repository: str | os.PathLike[str], endpoint: str,
             prerequisite_paths: list[str] | None = None) -> dict[str, Any]:
    gate_id = GATE_NAMES.get(gate.lower())
    if gate_id is None:
        raise RuntimeError("QUALIFICATION_GATE_INVALID:" + gate)
    repository_path = Path(repository).resolve()
    prerequisite_paths = prerequisite_paths or []
    if gate_id == "g7-full-chain":
        refs = []
        try:
            for index, expected in enumerate(GATE_ORDER):
                _, path, digest = _prerequisite(prerequisite_paths, index, expected, repository_path)
                refs.append({"gateId": expected, "path": str(path), "sha256": digest})
        except Exception as error:
            session = _GateSession(repository_path, endpoint, gate_id)
            session.start()
            try:
                return _result_without_browser(session, "INCONCLUSIVE", error, refs)
            finally:
                session.close()
        run_id = "qualification-g7-full-" + uuid.uuid4().hex
        return qualification.run(repository_path, endpoint, gate_id=gate_id, run_id=run_id,
                                 prerequisite_evidence={"gates": refs})
    return _run_one(gate_id, repository_path, endpoint, prerequisite_paths)


def dispatch_selected_gate(gate: str, handlers: dict[str, Callable[[], Any]]) -> Any:
    """Testable single-gate dispatch: never evaluates later gate handlers."""
    gate_id = GATE_NAMES.get(gate.lower())
    if gate_id is None or gate_id not in handlers:
        raise RuntimeError("QUALIFICATION_GATE_INVALID:" + gate)
    return handlers[gate_id]()
