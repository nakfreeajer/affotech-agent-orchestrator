"""Synthetic-only operator qualification harness; never attaches to production CDP."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import local_orchestrator_watcher as runtime
import crash_recovery_bootstrap as bootstrap


def _synthetic_page(conversation_id: str, *, prompt: str = "", ready: bool = False):
    class Page:
        def __init__(self):
            self.url = f"https://chatgpt.com/c/{conversation_id}"
            self.closed = False
            self.context = None
            self._qualification_visible_user = prompt
            self._qualification_ready = ready

        def evaluate(self, script: str):
            if "stop-button" in script:
                return False
            if "fresh-user-dom-fallback" in script:
                return [self._qualification_visible_user] if self._qualification_visible_user else []
            if "fresh-assistant-ready-dom-fallback" in script:
                return ["ARCHITECT_SESSION_READY"] if self._qualification_ready else []
            if 'data-message-author-role="user"' in script or 'data-message-author-role="assistant"' in script:
                return []
            return False

        def close(self):
            self.closed = True

    return Page()


def _make_fixture(repository: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    root = repository / ".agent-work" / "orchestrator" / "qualification" / run_id
    state_root = root / "state"
    prompts = state_root / "prompts"
    worktree = root / "synthetic-worktree"
    prompts.mkdir(parents=True, exist_ok=False)
    worktree.mkdir(parents=True, exist_ok=False)
    task_id = "QUAL-TASK-" + uuid.uuid4().hex[:12].upper()
    transaction_id = "QUAL-TX-" + uuid.uuid4().hex
    old_id = str(uuid.uuid4())
    fresh_id = str(uuid.uuid4())
    prompt_path = prompts / f"{task_id}.txt"
    prompt = "Synthetic qualification prompt. No business task or production content.\n"
    prompt_path.write_text(prompt, encoding="utf-8", newline="")
    prompt_hash = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    handover = runtime.make_handover_envelope(transaction_id, task_id, "Synthetic qualification handover only.")
    bootstrap = runtime.fresh_architect_bootstrap_payload(handover)
    state = {
        "qualificationSyntheticFixture": True,
        "qualificationExpectedPromptSha256": prompt_hash,
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED",
        "humanRequiredRecoveryAttemptedReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED",
        "postDiscussionProtocolFailure": "ARCHITECT_STAGED_PROMPT_EVIDENCE_INVALID",
        "taskId": "QUAL-COMPLETE-" + uuid.uuid4().hex[:8],
        "lastCompletedTaskId": None,
        "nextTaskId": task_id,
        "nextPromptPath": str(prompt_path),
        "taskWorktrees": {task_id: {"taskId": task_id, "worktreePath": str(worktree)}},
        "rolloverDue": True,
        "rolloverPending": True,
        "rolloverInProgress": False,
        "handoverRequested": False,
        "handoverReady": False,
        "rolloverTransactionId": transaction_id,
        "rolloverTransactionTaskId": task_id,
        "rolloverAttemptedForTaskId": task_id,
        "rolloverRecoveryEpoch": 5,
        "rolloverAutomaticRecoveryEpochCount": 3,
        "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverRecoveryState": "RECOVERING",
        "rolloverFreshPageCreated": True,
        "rolloverFreshCandidateConversationId": fresh_id,
        "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS",
        "rolloverFreshBootstrapPayloadHash": hashlib.sha256(bootstrap.encode()).hexdigest(),
        "rolloverHandoverResponseIdentity": hashlib.sha256(handover.encode()).hexdigest(),
        "pending_handover": handover,
        "postDiscussionEnvelopeRequired": True,
        "postDiscussionResumeEpoch": 1,
        "postDiscussionProtocolTaskId": task_id,
        "postDiscussionProtocolTransactionId": transaction_id,
        "postDiscussionProtocolRolloverCommittedTransactionId": None,
        "postDiscussionEnvelopeRepairAttempted": False,
        "postDiscussionEnvelopeRepairAwaiting": False,
        "postDiscussionEnvelopeRepairTaskId": task_id,
        "postDiscussionEnvelopeRepairEpoch": 1,
        "postDiscussionProtocolBaseline": {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()},
        "architectDiscussionBaseline": {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()},
        "discussionPauseActive": False,
        "architectConversationId": old_id,
        "executorSessionId": "QUALIFICATION-FAKE-SESSION",
        "executorSessionMode": "QUALIFICATION_FAKE",
        "executorProcessState": "STOPPED",
        "executorLaunchState": "NOT_LAUNCHED",
        "executorActiveWriter": False,
        "governedExecutorActiveWriter": False,
    }
    state["lastCompletedTaskId"] = state["taskId"]
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    (root / "qualification.json").write_text(json.dumps({"runId": run_id, "syntheticOnly": True}), encoding="utf-8")
    return root, {"old": old_id, "fresh": fresh_id, "task": task_id, "transaction": transaction_id,
                  "promptHash": prompt_hash, "promptBytes": prompt_path.read_bytes(), "handover": handover,
                  "bootstrap": bootstrap}


def run(repository: str | os.PathLike[str]) -> dict[str, Any]:
    repository = Path(repository).resolve()
    run_id = "qualification-" + uuid.uuid4().hex
    root, evidence = _make_fixture(repository, run_id)
    state_root = root / "state"
    log_root = root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / "qualification.log"
    discovery = bootstrap.discover(repository, state_root, process_records=[], codex_home=root / "synthetic-codex-home")
    classification = bootstrap.classify_workflow(discovery)
    if classification != "HUMAN_REQUIRED_NO_AUTOMATIC_ACTION":
        raise RuntimeError("QUALIFICATION_BOOTSTRAP_CLASSIFICATION_UNEXPECTED:" + classification)
    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("runtimeContext=QUALIFICATION productionStateMutation=false realBrowser=false fakeExecutor=true\n")
        handle.write("QUALIFICATION_BOOTSTRAP_CLASSIFICATION classification=" + classification + "\n")
    old_page = _synthetic_page(evidence["old"])
    fresh_page = _synthetic_page(evidence["fresh"], prompt=evidence["bootstrap"], ready=True)

    class Context:
        pages = [old_page, fresh_page]
        def new_page(self):
            raise AssertionError("qualification must reuse the existing synthetic candidate")

    context = Context()
    old_page.context = fresh_page.context = context
    sends = []
    response_ready = {"value": False}
    staged = evidence["promptBytes"].decode("utf-8")
    repaired = ("<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\naction=EXECUTE\n"
                f"taskId={evidence['task']}\ndocumentation=NOT_REQUIRED\npromptBegin\n"
                f"{staged.rstrip()}\npromptEnd\n</ORCHESTRATOR_RESULT>")

    def attach(_endpoint: str, conversation_id: str | None = None):
        page = old_page if conversation_id == evidence["old"] else fresh_page if conversation_id == evidence["fresh"] else None
        if page is None:
            raise AssertionError("qualification attempted an unidentified/protected page attachment")
        runtime._qualification_assert_page_allowed(page, "qualification-test-attach")
        bridge = runtime.ArchitectPlaywright(page)
        bridge.generation_visible = lambda: False
        bridge._assistant_entries = lambda: []
        bridge.current_visible_user_message_texts = lambda: [page._qualification_visible_user] if page._qualification_visible_user else []
        bridge.current_visible_assistant_texts = lambda: ["ARCHITECT_SESSION_READY"] if page._qualification_ready else []
        def guarded_send(text: str, timeout: float = 30.0):
            runtime._qualification_assert_page_allowed(page, "qualification-test-send")
            sends.append(text)
        bridge.submit_result_bounded = guarded_send
        def assistant_baseline():
            if response_ready["value"]:
                return {"count": 1, "text_hash": hashlib.sha256(repaired.encode()).hexdigest()}
            return {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()}
        bridge.assistant_baseline = assistant_baseline
        def wait_response(baseline, poll_interval=0.5):
            response_ready["value"] = True
            return {"state": "COMPLETED", "text": repaired}
        bridge.wait_for_new_response = wait_response
        bridge.close = lambda: None
        return bridge

    # The startup harness and all browser-dependent methods use one in-process,
    # synthetic page/DOM adapter. The production watcher entry and gates remain real.
    previous = {key: os.environ.get(key) for key in (
        "AFFOTECH_RUNTIME_CONTEXT", "AFFOTECH_PROJECT_DIR", "AFFOTECH_ORCHESTRATOR_STATE_DIR",
        "AFFOTECH_QUALIFICATION_RUN_ID", "AFFOTECH_QUALIFICATION_RUN_ROOT",
        "AFFOTECH_QUALIFICATION_LOG_ROOT", "AFFOTECH_QUALIFICATION_LOG_PATH",
        "AFFOTECH_QUALIFICATION_OWNED_IDS", "ORCHESTRATOR_POLL_INTERVAL")}
    previous_attach = runtime.ArchitectPlaywright.attach
    original_launcher = runtime.visible_executor_launcher
    original_runner = runtime.CodexRunner
    result_path = root / "result.json"
    try:
        os.environ.update({
            "AFFOTECH_RUNTIME_CONTEXT": "QUALIFICATION", "AFFOTECH_PROJECT_DIR": str(repository),
            "AFFOTECH_ORCHESTRATOR_STATE_DIR": str(state_root), "AFFOTECH_QUALIFICATION_RUN_ID": run_id,
            "AFFOTECH_QUALIFICATION_RUN_ROOT": str(root), "AFFOTECH_QUALIFICATION_LOG_ROOT": str(log_root),
            "AFFOTECH_QUALIFICATION_LOG_PATH": str(log_path),
            "AFFOTECH_QUALIFICATION_OWNED_IDS": f"{evidence['old']},{evidence['fresh']}",
            "ORCHESTRATOR_POLL_INTERVAL": "0.01",
        })
        runtime.ArchitectPlaywright.attach = staticmethod(attach)
        runtime.visible_executor_launcher = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("real launcher must be unreachable"))
        runtime.CodexRunner = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("CodexRunner must be unreachable"))
        runtime.main()
    finally:
        runtime.ArchitectPlaywright.attach = previous_attach
        runtime.visible_executor_launcher = original_launcher
        runtime.CodexRunner = original_runner
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    final_state = json.loads((state_root / "state.json").read_text(encoding="utf-8"))
    phase_events = {
        "QUALIFICATION_OLD_SESSION_READY": {"conversationId": evidence["old"], "synthetic": True},
        "QUALIFICATION_FRESH_SESSION_CREATED": {"conversationId": evidence["fresh"], "reusedExistingCandidate": True, "newTabCount": 0},
        "QUALIFICATION_BOOTSTRAP_SENT": {"preexistingDelivery": True, "resendCount": 0},
        "QUALIFICATION_BOOTSTRAP_OBSERVED": {"exactVisiblePayload": True},
        "QUALIFICATION_READY_OBSERVED": {"ready": True},
        "QUALIFICATION_AUTHORITY_COMMITTED": {"committed": final_state.get("architectConversationId") == evidence["fresh"]},
        "QUALIFICATION_PROTOCOL_RECOVERED": {"cleared": final_state.get("postDiscussionEnvelopeRequired") is False},
        "QUALIFICATION_EXECUTOR_SPY_REACHED": {"count": final_state.get("qualificationExecutorLaunchSpyCount", 0)},
        "QUALIFICATION_COMPLETE": {"realBrowser": False, "fakeExecutor": True},
    }
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        for event, metadata in phase_events.items():
            handle.write(event + " " + json.dumps(metadata, sort_keys=True) + "\n")
    log_text = log_path.read_text(encoding="utf-8")
    result = {
        "runId": run_id, "runRoot": str(root), "logPath": str(log_path),
        "runtimeContext": "QUALIFICATION", "realBrowser": False, "productionStateMutation": False,
        "bootstrapClassification": classification,
        "syntheticStateOnly": True, "oldSessionReady": True, "freshSessionCreated": False,
        "freshCandidateReused": True, "freshTabCount": 1, "freshTabCreatedDuringRecovery": 0, "bootstrapSent": 0,
        "bootstrapResendCount": 0, "protocolRepairSendCount": len(sends),
        "authorityCommitCount": log_text.count("commitCompleted=True"),
        "oldArchitectRetireCount": int(old_page.closed),
        "authorityCommitted": final_state.get("architectConversationId") == evidence["fresh"],
        "protocolRecovered": final_state.get("postDiscussionEnvelopeRequired") is False,
        "executorLaunchSpyCount": final_state.get("qualificationExecutorLaunchSpyCount", 0),
        "promptUnchanged": (state_root / "prompts" / f"{evidence['task']}.txt").read_bytes() == evidence["promptBytes"],
        "qualificationLogIsolated": log_path == root / "logs" / "qualification.log",
        "requiredEventsPresent": all(name in log_text for name in ("QUALIFICATION_PREFLIGHT", "QUALIFICATION_BROWSER_GUARD_READY", "EXISTING_FRESH_CANDIDATE_RECOVERY_BEGIN", "QUALIFICATION_COMPLETE")),
        "state": final_state,
    }
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not (result["authorityCommitted"] and result["protocolRecovered"]
            and result["executorLaunchSpyCount"] == 1 and result["promptUnchanged"]
            and result["bootstrapResendCount"] == 0 and result["oldArchitectRetireCount"] == 1):
        raise RuntimeError("QUALIFICATION_SYNTHETIC_REPLAY_FAILED:" + json.dumps({key: result[key] for key in (
            "authorityCommitted", "protocolRecovered", "executorLaunchSpyCount", "promptUnchanged",
            "bootstrapResendCount", "oldArchitectRetireCount")}))
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--run":
        raise SystemExit("usage: orchestrator_qualification.py --run REPOSITORY")
    outcome = run(sys.argv[2])
    print(json.dumps({key: value for key, value in outcome.items() if key != "state"}, indent=2))
