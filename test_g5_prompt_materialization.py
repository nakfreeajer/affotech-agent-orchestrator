import hashlib
import inspect
import json

import pytest

import qualification_gate_runner as gates
import local_orchestrator_watcher as runtime


TASK = "QUAL-TASK-ABC123"
TX = "QUAL-TX-ABC123"


class _Session:
    def __init__(self, root):
        self.root = root
        self.state_dir = root / "state"
        (self.state_dir / "prompts").mkdir(parents=True)


def _source(tmp_path, data):
    evidence_root = tmp_path / "qualification" / "qualification-g2-old-handover-source"
    evidence_root.mkdir(parents=True)
    artifact = evidence_root / "staged-prompt.txt"
    artifact.write_bytes(data)
    evidence_path = evidence_root / "qualification-evidence.json"
    evidence = {
        "gateId": "g2-old-handover", "result": "PASS", "taskId": TASK,
        "transactionId": TX, "stagedPromptPath": str(artifact),
        "stagedPromptSha256": hashlib.sha256(data).hexdigest(),
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return evidence, evidence_path


def test_g5_materializes_exact_verified_g2_bytes_and_records_provenance(tmp_path):
    data = "α prompt\r\nwith exact bytes\n".encode("utf-8")
    g2, g2_path = _source(tmp_path, data)
    session = _Session(tmp_path / "g5")
    target, materialized, provenance = gates._materialize_g5_staged_prompt(session, g2, g2_path, TASK)
    assert target == session.state_dir / "prompts" / f"{TASK}.txt"
    assert materialized == target.read_bytes() == data
    assert hashlib.sha256(materialized).hexdigest() == g2["stagedPromptSha256"]
    assert provenance == {
        "stagedPromptSourceGateId": "g2-old-handover",
        "stagedPromptSourceEvidencePath": str(g2_path),
        "stagedPromptSourceSha256": g2["stagedPromptSha256"],
        "stagedPromptMaterializedPath": str(target),
        "stagedPromptMaterializedSha256": g2["stagedPromptSha256"],
    }


def test_g5_restored_g4_state_is_rebound_only_at_prompt_path(tmp_path):
    session = _Session(tmp_path / "g5")
    watcher = runtime.LocalFirstOrchestrator(str(tmp_path), session.state_dir)
    accepted = {"nextPromptPath": str(tmp_path / "g2" / "prompts" / f"{TASK}.txt"),
                "architectConversationId": "fresh", "transactionId": TX,
                "rolloverDue": False}
    local = session.state_dir / "prompts" / f"{TASK}.txt"
    gates._restore_g5_watcher_state(watcher, accepted, local)
    assert watcher.state["nextPromptPath"] == str(local)
    assert watcher.state["architectConversationId"] == "fresh"
    assert watcher.state["transactionId"] == TX
    assert watcher.state["rolloverDue"] is False


def test_g2_artifact_hash_mismatch_fails_before_local_materialization(tmp_path):
    data = b"verified prompt"
    g2, g2_path = _source(tmp_path, data)
    g2["stagedPromptSha256"] = "0" * 64
    session = _Session(tmp_path / "g5")
    with pytest.raises(RuntimeError, match="ARTIFACT_HASH_MISMATCH"):
        gates._materialize_g5_staged_prompt(session, g2, g2_path, TASK)
    assert not (session.state_dir / "prompts" / f"{TASK}.txt").exists()


def test_g2_task_mismatch_fails_before_materialization(tmp_path):
    data = b"verified prompt"
    g2, g2_path = _source(tmp_path, data)
    session = _Session(tmp_path / "g5")
    with pytest.raises(RuntimeError, match="TASK_MISMATCH"):
        gates._materialize_g5_staged_prompt(session, g2, g2_path, "QUAL-TASK-OTHER")
    assert not list((session.state_dir / "prompts").iterdir())


def test_g5_local_verification_rejects_mutation_and_noncanonical_task_path(tmp_path):
    data = b"exact staged prompt"
    local = tmp_path / "state" / "prompts" / f"{TASK}.txt"
    local.parent.mkdir(parents=True)
    local.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    assert gates._verify_g5_local_staged_prompt(local, tmp_path / "state", TASK, digest) == data
    wrong = local.parent / "other.txt"
    wrong.write_bytes(data)
    with pytest.raises(RuntimeError, match="LOCAL_PATH_INVALID"):
        gates._verify_g5_local_staged_prompt(wrong, tmp_path / "state", TASK, digest)
    local.write_bytes(b"mutated")
    with pytest.raises(RuntimeError, match="LOCAL_HASH_MISMATCH"):
        gates._verify_g5_local_staged_prompt(local, tmp_path / "state", TASK, digest)


def test_invalid_local_materialization_blocks_repair_submission(tmp_path):
    data = b"exact staged prompt"
    prompts = tmp_path / "state" / "prompts"
    prompts.mkdir(parents=True)
    local = prompts / f"{TASK}.txt"
    local.write_bytes(b"tampered")
    watcher = runtime.LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.state.update({
        "postDiscussionProtocolTaskId": TASK, "postDiscussionProtocolTransactionId": TX,
        "nextTaskId": TASK, "nextPromptPath": str(local),
        "postDiscussionEnvelopeRequired": True,
        "postDiscussionEnvelopeRepairAttempted": False,
        "postDiscussionEnvelopeRepairTaskId": TASK, "postDiscussionResumeEpoch": 1,
        "postDiscussionEnvelopeRepairEpoch": 1,
        "postDiscussionProtocolRolloverCommittedTransactionId": TX,
        "rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False,
        "handoverRequested": False, "architectConversationId": "fresh",
        "taskWorktrees": {TASK: {"taskId": TASK, "worktreePath": str(tmp_path)}},
    })

    class Bridge:
        send_count = 0
        def assistant_response_baseline(self):
            return {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()}
        def submit_result_bounded(self, message):
            self.send_count += 1

    bridge = Bridge()
    with pytest.raises(RuntimeError, match="LOCAL_HASH_MISMATCH"):
        gates._verify_g5_local_staged_prompt(local, tmp_path / "state", TASK,
                                             hashlib.sha256(data).hexdigest())
    assert bridge.send_count == 0
    source = inspect.getsource(gates._run_one)
    g5 = source[source.index('if gate_id == "g5-post-discussion-envelope":'):]
    assert g5.index("_verify_g5_local_staged_prompt(") < g5.index("request_post_discussion_envelope_repair(")


def test_g5_valid_local_materialization_allows_one_simulated_repair_request(tmp_path):
    data = b"exact staged prompt"
    prompts = tmp_path / "state" / "prompts"
    prompts.mkdir(parents=True)
    local = prompts / f"{TASK}.txt"
    local.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    assert gates._verify_g5_local_staged_prompt(local, tmp_path / "state", TASK, digest) == data
    watcher = runtime.LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.state.update({
        "postDiscussionProtocolTaskId": TASK, "postDiscussionProtocolTransactionId": TX,
        "nextTaskId": TASK, "nextPromptPath": str(local),
        "postDiscussionEnvelopeRequired": True,
        "postDiscussionEnvelopeRepairAttempted": False,
        "postDiscussionEnvelopeRepairTaskId": TASK, "postDiscussionResumeEpoch": 1,
        "postDiscussionEnvelopeRepairEpoch": 1,
        "postDiscussionProtocolRolloverCommittedTransactionId": TX,
        "rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False,
        "handoverRequested": False, "architectConversationId": "fresh",
        "taskWorktrees": {TASK: {"taskId": TASK, "worktreePath": str(tmp_path)}},
    })

    class Bridge:
        send_count = 0
        def assistant_response_baseline(self):
            return {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()}
        def submit_result_bounded(self, message):
            self.send_count += 1
            assert "exact staged prompt" in message

    bridge = Bridge()
    assert watcher.request_post_discussion_envelope_repair(bridge) is True
    assert bridge.send_count == 1


def test_runtime_staged_prompt_scope_guard_remains_canonical_and_strict():
    source = inspect.getsource(runtime.LocalFirstOrchestrator._post_discussion_staged_prompt_evidence_valid)
    assert 'expected = self.prompts_dir / f"{task_id}.txt"' in source
    assert "Path(str(prompt_value)).resolve() == expected.resolve()" in source
