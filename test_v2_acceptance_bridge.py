import hashlib
import json

import pytest

from local_orchestrator_watcher import LocalFirstOrchestrator
from prompt_source_artifacts import ingest_verified_prompt_source


TASK = "000200"
PROMPT = b"Synthetic V2 execution prompt\nExact bytes."
DIGEST = hashlib.sha256(PROMPT).hexdigest()
SOURCE_ID = f"prompt-source-{DIGEST}"


def envelope(*, documentation="NOT_REQUIRED", source_id=SOURCE_ID, digest=DIGEST,
             length=len(PROMPT), classification="ACCEPTED"):
    return ("<ORCHESTRATOR_RESULT>\nschemaVersion=2\n"
            f"classification={classification}\naction=EXECUTE\ntaskId={TASK}\n"
            f"documentation={documentation}\npromptTransport=ARTIFACT_V1\n"
            f"promptSourceArtifactId={source_id}\npromptSha256={digest}\n"
            f"promptByteLength={length}\n</ORCHESTRATOR_RESULT>")


def setup_watcher(tmp_path, *, source=True, state_extra=None):
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"
    repo.mkdir()
    state_dir.mkdir()
    state = {"state": "RESULT_READY", "taskId": TASK, "taskSequence": 200,
             "documentationClosurePending": False, "consumedArchitectResponses": {}}
    state.update(state_extra or {})
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    watcher = LocalFirstOrchestrator(str(repo), state_dir)

    def isolated_worktree(task_id, prompt, context_task_id=None):
        target = state_dir / "worktrees" / task_id
        target.mkdir(parents=True, exist_ok=True)
        watcher.state.setdefault("taskWorktrees", {})[task_id] = {
            "taskId": task_id, "project": "qualification", "worktreePath": str(target),
            "sourceTaskId": context_task_id,
        }
        watcher.state.update({"targetProject": str(target), "targetRepo": str(target), "targetWorktree": str(target)})
        watcher.save()
        return target

    watcher._owned_task_worktree = isolated_worktree
    if source:
        ingest_verified_prompt_source(repo, expected_prompt_source_artifact_id=SOURCE_ID,
                                      expected_sha256=DIGEST, expected_byte_length=len(PROMPT), prompt_bytes=PROMPT)
    return watcher, repo, state_dir


def test_v2_execute_authorizes_only_after_exact_source_binding_and_consumption(tmp_path):
    watcher, repo, state_dir = setup_watcher(tmp_path)
    result = watcher.accept_architect_response(envelope())
    assert result["action"] == "EXECUTE"
    state = watcher.state
    assert state["state"] == "NEXT_PROMPT_READY"
    assert state["nextTaskId"] == "000201"
    assert state["architectProtocolVersion"] == 2
    assert state["promptTransport"] == "ARTIFACT_V1"
    assert state["promptSourceArtifactId"] == SOURCE_ID
    assert state["promptSourceSha256"] == DIGEST
    assert state["promptSourceByteLength"] == len(PROMPT)
    assert state["promptTaskId"] == "000201"
    assert state["promptTransactionId"].startswith("v2-accept-")
    assert state["promptState"] == "STAGED"
    assert state["v2AcceptancePreparation"]["state"] == "AUTHORIZED"
    assert state["architectResultFingerprint"] in state["consumedArchitectResponses"]
    assert (state_dir / "prompts" / "000201.txt").read_bytes() == PROMPT
    bound = json.loads((repo / ".agent-work" / "prompts" / "000201" / f"{DIGEST}.json").read_text(encoding="utf-8"))
    assert bound["source"] == "ARCHITECT_V2_VERIFIED_SOURCE"
    assert bound["transactionId"] == state["promptTransactionId"]
    assert not any(key in state for key in ("executorPromptArtifactId", "executorLaunchClaimTaskId"))


def test_v2_exact_retry_is_duplicate_and_does_not_allocate_again(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path)
    control = envelope()
    watcher.accept_architect_response(control)
    tx = watcher.state["promptTransactionId"]
    artifact = watcher.state["promptArtifactId"]
    assert watcher.accept_architect_response(control)["action"] == "DUPLICATE"
    assert watcher.state["nextTaskId"] == "000201"
    assert watcher.state["promptTransactionId"] == tx
    assert watcher.state["promptArtifactId"] == artifact


def test_missing_source_is_retryable_pinned_and_does_not_consume_or_allocate(tmp_path):
    watcher, repo, _ = setup_watcher(tmp_path, source=False)
    control = envelope()
    result = watcher.accept_architect_response(control)
    assert result["reason"] == "V2_PROMPT_SOURCE_UNAVAILABLE"
    prep = watcher.state["v2AcceptancePreparation"]
    assert prep["state"] == "WAITING_FOR_SOURCE"
    assert prep["architectResultFingerprint"] not in watcher.state.get("consumedArchitectResponses", {})
    assert watcher.state.get("nextTaskId") is None
    ingest_verified_prompt_source(repo, expected_prompt_source_artifact_id=SOURCE_ID,
                                  expected_sha256=DIGEST, expected_byte_length=len(PROMPT), prompt_bytes=PROMPT)
    watcher = LocalFirstOrchestrator(str(repo), watcher.state_dir)
    watcher._owned_task_worktree = lambda task_id, prompt, context_task_id=None: watcher.state_dir / "worktrees" / task_id
    target = watcher.state_dir / "worktrees" / "000201"
    watcher._owned_task_worktree = lambda task_id, prompt, context_task_id=None: (target.mkdir(parents=True, exist_ok=True) or target)
    assert watcher.accept_architect_response(control)["action"] == "EXECUTE"
    assert watcher.state["nextTaskId"] == "000201"
    assert watcher.state["v2AcceptancePreparation"]["transactionId"].startswith("v2-accept-")


def test_invalid_source_manifest_fails_closed_without_consumption(tmp_path):
    watcher, repo, _ = setup_watcher(tmp_path)
    manifest = repo / ".agent-work" / "prompt-ingress" / f"{DIGEST}.json"
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["artifactState"] = "AUTHORIZED"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    result = watcher.accept_architect_response(envelope())
    assert result["reason"] == "V2_PROMPT_SOURCE_INVALID"
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state.get("architectResultFingerprint") is None
    assert watcher.state["v2AcceptancePreparation"]["state"] == "WAITING_FOR_SOURCE"


def test_rollover_gate_does_not_consume_or_modify_rollover_ownership(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path, state_extra={"rolloverPending": True,
                                                        "rolloverTransactionId": "legacy-tx",
                                                        "rolloverTransactionTaskId": "000103"})
    result = watcher.accept_architect_response(envelope())
    assert result["reason"] == "V2_ROLLOVER_NOT_QUALIFIED"
    assert watcher.state["rolloverTransactionId"] == "legacy-tx"
    assert watcher.state["rolloverTransactionTaskId"] == "000103"
    assert watcher.state.get("architectResultFingerprint") is None
    assert watcher.state["state"] == "HUMAN_REQUIRED"


def test_distinct_response_cannot_replace_existing_preparation(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path, source=False)
    first = envelope()
    watcher.accept_architect_response(first)
    original = dict(watcher.state["v2AcceptancePreparation"])
    second = envelope(documentation="COMPLETE")
    result = watcher.accept_architect_response(second)
    assert result["reason"] == "V2_PROMPT_CORRECTION_REQUIRED"
    assert watcher.state["v2AcceptancePreparation"] == original
    assert watcher.state.get("nextTaskId") is None


@pytest.mark.parametrize(("action", "expected_state"), [("HUMAN_REQUIRED", "HUMAN_REQUIRED"), ("STOP", "IDLE")])
def test_v2_terminal_actions_need_no_source_or_task_allocation(tmp_path, action, expected_state):
    watcher, _, _ = setup_watcher(tmp_path, source=False)
    control = ("<ORCHESTRATOR_RESULT>\nschemaVersion=2\nclassification=BLOCKED\n"
               f"action={action}\ntaskId={TASK}\ndocumentation=NOT_REQUIRED\n</ORCHESTRATOR_RESULT>")
    assert watcher.accept_architect_response(control)["action"] == action
    assert watcher.state["state"] == expected_state
    assert watcher.state["architectResultFingerprint"] == hashlib.sha256(control.encode()).hexdigest()
    assert "nextTaskId" not in watcher.state


def test_pre_authorization_preparation_cannot_launch(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path, source=False)
    watcher.accept_architect_response(envelope())
    calls = []
    assert watcher.launch_next(lambda *args: calls.append(args)) is None
    assert calls == []


def test_attempted_malformed_v2_fails_closed_instead_of_v1(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path)
    malformed = envelope().replace("schemaVersion=2\n", "schemaVersion=3\n")
    with pytest.raises(ValueError, match="ORCHESTRATOR_RESULT_V2_INVALID"):
        watcher.accept_architect_response(malformed)
    no_schema = envelope().replace("schemaVersion=2\n", "")
    with pytest.raises(ValueError, match="ORCHESTRATOR_RESULT_V2_INVALID"):
        watcher.accept_architect_response(no_schema)


def test_distinct_prompt_after_authorization_never_replaces_authorized_identity(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path)
    watcher.accept_architect_response(envelope())
    artifact_id = watcher.state["promptArtifactId"]
    changed = envelope(documentation="COMPLETE")
    result = watcher.accept_architect_response(changed)
    assert result["reason"] == "V2_PROMPT_CORRECTION_REQUIRED"
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["promptArtifactId"] == artifact_id


def test_documentation_closure_is_committed_only_with_successful_authorization(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path, source=False,
                                  state_extra={"documentationClosurePending": True})
    result = watcher.accept_architect_response(envelope(documentation="COMPLETE"))
    assert result["reason"] == "V2_PROMPT_SOURCE_UNAVAILABLE"
    assert watcher.state["documentationClosurePending"] is True
    assert "documentationClosureCompletedTaskId" not in watcher.state
    assert watcher.state.get("architectResultFingerprint") is None


def test_documentation_required_matches_v1_closure_behavior(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path)
    result = watcher.accept_architect_response(envelope(documentation="REQUIRED"))
    assert result["action"] == "EXECUTE"
    assert watcher.state["documentationClosurePending"] is True
    assert watcher.state["documentationClosureSourceTaskId"] == TASK
    assert watcher.state["documentationClosureTaskId"] == "000201"


def test_pending_documentation_gate_rejects_without_consuming_v2_execute(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path, state_extra={"documentationClosurePending": True})
    result = watcher.accept_architect_response(envelope(documentation="NOT_REQUIRED"))
    assert result["reason"] == "DOCUMENTATION_CLOSURE_REQUIRED"
    assert watcher.state["documentationClosurePending"] is True
    assert watcher.state.get("architectResultFingerprint") is None
    assert watcher.state.get("consumedArchitectResponses", {}) == {}


def test_transaction_derivation_is_deterministic_and_orchestrator_owned(tmp_path):
    watcher, _, _ = setup_watcher(tmp_path)
    control = envelope()
    fingerprint = hashlib.sha256(control.encode("utf-8")).hexdigest()
    expected = "v2-accept-" + hashlib.sha256(
        (TASK + "\0" + "000201" + "\0" + fingerprint + "\0" + SOURCE_ID).encode("utf-8")
    ).hexdigest()
    watcher.accept_architect_response(control)
    assert watcher.state["promptTransactionId"] == expected
    assert watcher.state["v2AcceptancePreparation"]["transactionId"] == expected


def test_preparation_transaction_tampering_fails_closed(tmp_path):
    watcher, repo, _ = setup_watcher(tmp_path, source=False)
    watcher.accept_architect_response(envelope())
    ingest_verified_prompt_source(repo, expected_prompt_source_artifact_id=SOURCE_ID,
                                  expected_sha256=DIGEST, expected_byte_length=len(PROMPT), prompt_bytes=PROMPT)
    watcher.state["v2AcceptancePreparation"].update({
        "nextTaskId": "000201", "transactionId": "v2-accept-forged", "state": "PREPARING"
    })
    watcher.save()
    watcher = LocalFirstOrchestrator(str(watcher.project_dir), watcher.state_dir)
    result = watcher.accept_architect_response(envelope())
    assert result["reason"] == "V2_PROMPT_TRANSACTION_MISSING"
