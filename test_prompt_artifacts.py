import hashlib
import json
from pathlib import Path

import pytest

import local_orchestrator_watcher as watcher_module
from local_orchestrator_watcher import LocalFirstOrchestrator, parse_orchestrator_result
import prompt_artifacts as artifacts


def _persist(tmp_path, prompt="Exact prompt café 😀\nkeep trailing newline\n", **kwargs):
    return artifacts.persist_verified_prompt_artifact(
        tmp_path, task_id=kwargs.pop("task_id", "000104"),
        transaction_id=kwargs.pop("transaction_id", "tx-104"),
        prompt=prompt, architect_conversation_id=kwargs.pop("architect_conversation_id", "arch-104"),
        **kwargs)


def _paths(identity):
    return Path(identity["promptArtifactPath"]), Path(identity["promptArtifactManifestPath"])


def _accepted_result(task_id="000050", action="EXECUTE", prompt="WORKTREE\nC:\\synthetic\nnext step café 😀\n"):
    body = prompt if action == "EXECUTE" else ""
    return ("<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\n"
            f"action={action}\ntaskId={task_id}\ndocumentation=NOT_REQUIRED\n"
            f"promptBegin\n{body}\npromptEnd\n</ORCHESTRATOR_RESULT>")


def _watcher(tmp_path, task_id="000050"):
    project = tmp_path / "project"
    project.mkdir()
    state_dir = project / ".agent-work" / "orchestrator"
    watcher = LocalFirstOrchestrator(str(project), state_dir)
    worktree = tmp_path / "owned-worktree"
    worktree.mkdir()
    watcher._owned_task_worktree = lambda *_args, **_kwargs: worktree
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": task_id,
                          "taskSequence": int(task_id) if task_id.isdigit() else 0,
                          "architectConversationId": "arch-test"})
    return watcher


def test_accepted_prompt_is_persisted_byte_for_byte_with_content_identity(tmp_path):
    prompt = "Line 1\r\nLine 2 café 😀\n"
    identity = _persist(tmp_path, prompt)
    prompt_path, manifest_path = _paths(identity)
    expected = prompt.encode("utf-8")
    assert prompt_path.read_bytes() == expected
    assert identity["promptSha256"] == hashlib.sha256(expected).hexdigest()
    assert identity["promptByteLength"] == len(expected)
    assert identity["promptByteLength"] != len(prompt)
    assert prompt_path.parent == tmp_path / ".agent-work" / "prompts" / "000104"
    assert prompt_path.stem == identity["promptSha256"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 1
    assert manifest["taskId"] == "000104"
    assert manifest["promptSha256"] == hashlib.sha256(expected).hexdigest()
    assert manifest["promptByteLength"] == len(expected)
    assert manifest["promptState"] == "STAGED"
    assert "Line 1" not in manifest_path.read_text(encoding="utf-8")


def test_verified_loader_returns_exact_persisted_bytes(tmp_path):
    prompt = "exact\n雪 and 🛰\n"
    identity = _persist(tmp_path, prompt)
    assert artifacts.load_verified_staged_prompt(tmp_path, identity) == prompt.encode("utf-8")


def test_one_character_mutation_fails_hash_verification(tmp_path):
    identity = _persist(tmp_path, "abcdef")
    path, _ = _paths(identity)
    path.write_bytes(b"abcdeg")
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_HASH_MISMATCH"):
        artifacts.load_verified_staged_prompt(tmp_path, identity)


def test_truncation_fails_length_verification(tmp_path):
    identity = _persist(tmp_path, "abcdef")
    path, _ = _paths(identity)
    path.write_bytes(b"abc")
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_LENGTH_MISMATCH"):
        artifacts.load_verified_staged_prompt(tmp_path, identity)


def test_manifest_task_mutation_fails_closed(tmp_path):
    identity = _persist(tmp_path)
    _, manifest_path = _paths(identity)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["taskId"] = "000105"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_TASK_MISMATCH"):
        artifacts.load_verified_staged_prompt(tmp_path, identity)


def test_transaction_mismatch_fails_closed(tmp_path):
    identity = _persist(tmp_path)
    wrong = dict(identity, promptTransactionId="tx-other")
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_TRANSACTION_MISMATCH"):
        artifacts.load_verified_staged_prompt(tmp_path, wrong)


def test_invalid_prompt_state_fails_closed(tmp_path):
    identity = _persist(tmp_path)
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_STATE_INVALID"):
        artifacts.load_verified_staged_prompt(tmp_path, dict(identity, promptState="VERIFIED"))


def test_missing_artifact_fails_closed(tmp_path):
    identity = _persist(tmp_path)
    path, _ = _paths(identity)
    path.unlink()
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_UNAVAILABLE"):
        artifacts.load_verified_staged_prompt(tmp_path, identity)


def test_exact_repeat_reuses_one_artifact_identity(tmp_path):
    first = _persist(tmp_path, "same bytes")
    second = _persist(tmp_path, "same bytes")
    assert first == second
    task_dir = tmp_path / ".agent-work" / "prompts" / "000104"
    assert sorted(path.suffix for path in task_dir.iterdir()) == [".json", ".md"]


def test_content_address_collision_with_different_bytes_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "_sha256", lambda _data: "a" * 64)
    _persist(tmp_path, "first")
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_HASH_MISMATCH"):
        _persist(tmp_path, "other")


def test_malformed_result_does_not_create_prompt_artifact(tmp_path):
    watcher = _watcher(tmp_path)
    with pytest.raises(ValueError, match="ARCHITECT_ENVELOPE_INVALID"):
        watcher.accept_architect_response("not an orchestrator result")
    assert not (watcher.project_dir / ".agent-work" / "prompts").exists()


def test_non_execute_result_does_not_create_prompt_artifact(tmp_path):
    watcher = _watcher(tmp_path)
    decision = watcher.accept_architect_response(_accepted_result(action="STOP"))
    assert decision["action"] == "STOP"
    assert not (watcher.project_dir / ".agent-work" / "prompts").exists()


def test_artifact_failure_sets_human_required_and_blocks_dispatch(tmp_path, monkeypatch):
    watcher = _watcher(tmp_path)
    def fail(*_args, **_kwargs):
        raise RuntimeError("PROMPT_ARTIFACT_WRITE_FAILED")
    monkeypatch.setattr(watcher_module, "persist_verified_prompt_artifact", fail)
    with pytest.raises(RuntimeError, match="PROMPT_ARTIFACT_PERSISTENCE_FAILED"):
        watcher.accept_architect_response(_accepted_result())
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "PROMPT_ARTIFACT_PERSISTENCE_FAILED"
    assert watcher.state.get("nextPromptPath") is None
    assert watcher.launch_next(lambda *_args: pytest.fail("dispatch must not progress")) is None


def test_accepted_execute_records_additive_artifact_state_and_keeps_legacy_path(tmp_path):
    watcher = _watcher(tmp_path)
    decision = watcher.accept_architect_response(_accepted_result())
    assert decision["action"] == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextPromptPath"].endswith("000051.txt")
    assert watcher.state["promptArtifactVersion"] == 1
    assert watcher.state["promptTaskId"] == "000051"
    assert watcher.state["promptState"] == "STAGED"
    assert watcher.state["promptByteLength"] == len(decision["prompt"].encode("utf-8"))
    assert artifacts.load_verified_staged_prompt(watcher.project_dir, watcher.state) == decision["prompt"].encode("utf-8")


def test_verified_staged_prompt_envelope_persists_original_staged_bytes(tmp_path):
    watcher = _watcher(tmp_path)
    task_id = "000051"
    staged = "original staged\r\nbytes café\r\n"
    staged_path = watcher.prompts_dir / f"{task_id}.txt"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(staged.encode("utf-8"))
    watcher.state.update({"taskId": "000050", "nextTaskId": task_id,
                          "nextPromptPath": str(staged_path),
                          "postDiscussionProtocolTaskId": task_id,
                          "postDiscussionProtocolTransactionId": "tx-post",
                          "rolloverTransactionId": "tx-post"})
    watcher._post_discussion_staged_prompt_evidence_valid = lambda _task: True
    response = _accepted_result(task_id=task_id, prompt=staged.replace("\r\n", "\n").rstrip("\n"))
    decision = watcher._accept_exact_staged_prompt_envelope(response, task_id)
    assert decision["action"] == "EXECUTE"
    assert Path(watcher.state["promptArtifactPath"]).read_bytes() == staged.encode("utf-8")
    assert watcher.state["promptTransactionId"] == "tx-post"


def test_strict_parser_and_prompt_delimiters_remain_authoritative():
    response = _accepted_result(prompt="x")
    assert parse_orchestrator_result(response, "000050")["prompt"] == "x"
    assert "promptBegin" in response and "promptEnd" in response
    with pytest.raises(ValueError, match="ARCHITECT_ENVELOPE_INVALID"):
        parse_orchestrator_result(response.replace("promptEnd", "promptFinish"), "000050")


def test_qualification_gate_order_is_unchanged():
    import qualification_gate_runner
    assert qualification_gate_runner.GATE_ORDER == (
        "g1-cdp-attach", "g2-old-handover", "g3-fresh-bootstrap",
        "g4-authority-switch", "g5-post-discussion-envelope", "g6-executor-dispatch",
    )


def test_f9_f10_pause_resume_controls_keep_existing_semantics(tmp_path):
    watcher = _watcher(tmp_path)
    controls = watcher_module.DiscussionHotkeyController(watcher, emit=lambda _message: None)
    assert controls.dispatch("F9") is True
    assert watcher.discussion_pause_active() is True
    assert controls.dispatch("F10") is True
    assert watcher.discussion_pause_active() is False
    assert not (watcher.project_dir / ".agent-work" / "prompts").exists()
