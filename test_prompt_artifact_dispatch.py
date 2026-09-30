import hashlib
import json
import logging
from pathlib import Path

import pytest

import local_orchestrator_watcher as watcher_module
from local_orchestrator_watcher import CodexRunner, LocalFirstOrchestrator
from prompt_artifacts import persist_verified_prompt_artifact


PROMPT = "SYNTHETIC_PRIVATE_PROMPT_BODY: exact task instructions\n"


class Process:
    pid = 43210


def prepared(tmp_path, prompt=PROMPT, transaction_id="tx-1"):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "orchestrator")
    identity = persist_verified_prompt_artifact(
        tmp_path, task_id="task-1", transaction_id=transaction_id, prompt=prompt,
    )
    worktree = tmp_path / "owned-worktree"
    worktree.mkdir()
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-1",
        "nextPromptPath": str(tmp_path / "legacy-staged.txt"), "discussionPauseActive": False,
        "taskWorktrees": {"task-1": {"taskId": "task-1", "worktreePath": str(worktree)}},
        **identity,
    })
    watcher.save()
    return watcher, identity


def dispatch(watcher):
    calls = []
    process = Process()
    result = watcher.launch_next(lambda message, path: (calls.append((message, path)), process)[1])
    return result, calls


def test_artifact_dispatch_sends_compact_exact_identity_descriptor(tmp_path):
    watcher, identity = prepared(tmp_path)
    process, calls = dispatch(watcher)
    assert process.pid == 43210 and len(calls) == 1
    descriptor, result_path = calls[0]
    assert PROMPT not in descriptor
    assert descriptor.startswith("<EXECUTOR_PROMPT_ARTIFACT>\n")
    envelope = json.loads(descriptor.splitlines()[1])
    assert envelope == {
        "version": 1, "taskId": "task-1", "transactionId": "tx-1",
        "artifactId": identity["promptArtifactId"], "artifactPath": identity["promptArtifactPath"],
        "artifactSha256": identity["promptSha256"], "artifactByteLength": len(PROMPT.encode()),
        "manifestPath": identity["promptArtifactManifestPath"],
    }
    assert result_path.name == "task-1.txt"
    assert watcher.state["executorPromptArtifactId"] == identity["promptArtifactId"]
    assert watcher.state["executorPromptArtifactSha256"] == identity["promptSha256"]
    assert watcher.state["executorPromptArtifactByteLength"] == len(PROMPT.encode())
    assert watcher.state["executorPromptArtifactPath"] == identity["promptArtifactPath"]
    assert watcher.state["executorPromptManifestPath"] == identity["promptArtifactManifestPath"]
    assert watcher.state["executorPromptDispatchTaskId"] == "task-1"
    assert watcher.state["executorPromptDispatchTransactionId"] == "tx-1"
    assert watcher.state["executorPromptDispatchAt"]


@pytest.mark.parametrize("corruption", [
    "missing", "hash", "length", "task", "transaction", "manifest", "state", "stale_identity",
])
def test_invalid_artifact_identity_blocks_dispatch_without_inline_fallback(tmp_path, corruption):
    watcher, identity = prepared(tmp_path)
    artifact = Path(identity["promptArtifactPath"])
    manifest = Path(identity["promptArtifactManifestPath"])
    if corruption == "missing":
        artifact.unlink()
    elif corruption == "hash":
        artifact.write_bytes(b"X" + artifact.read_bytes()[1:])
    elif corruption == "length":
        artifact.write_bytes(b"short")
    elif corruption == "task":
        data = json.loads(manifest.read_text())
        data["taskId"] = "task-elsewhere"
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif corruption == "transaction":
        watcher.state["promptTransactionId"] = "different-tx"
    elif corruption == "manifest":
        data = json.loads(manifest.read_text())
        data["promptArtifactId"] = "stale-id"
        manifest.write_text(json.dumps(data), encoding="utf-8")
    elif corruption == "state":
        watcher.state["promptState"] = "INVALID"
    elif corruption == "stale_identity":
        watcher.state["promptTaskId"] = "another-task"
    process, calls = dispatch(watcher)
    assert process is None and calls == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "PROMPT_ARTIFACT_DISPATCH_BLOCKED"
    assert watcher.state["promptArtifactDispatchFailure"]


def test_artifact_is_verified_immediately_before_single_dispatch(tmp_path, monkeypatch):
    watcher, _ = prepared(tmp_path)
    observed = []
    original = watcher_module.load_verified_staged_prompt

    def verify(root, state):
        observed.append("verified")
        return original(root, state)

    monkeypatch.setattr(watcher_module, "load_verified_staged_prompt", verify)
    process, calls = dispatch(watcher)
    assert process.pid == 43210 and observed == ["verified", "verified"] and len(calls) == 1


def test_dispatch_logs_identity_without_prompt_body(tmp_path, caplog):
    watcher, identity = prepared(tmp_path)
    logger = logging.getLogger("artifact-dispatch-test")
    watcher.runtime_logger = logger
    watcher.runtime_run_id = "dispatch-test"
    with caplog.at_level(logging.INFO, logger="artifact-dispatch-test"):
        dispatch(watcher)
    output = caplog.text
    events = {record.event for record in caplog.records}
    assert "PROMPT_ARTIFACT_DISPATCH_VERIFIED" in events
    assert "PROMPT_ARTIFACT_DESCRIPTOR_SENT" in events
    assert identity["promptSha256"] in output
    assert PROMPT not in output


def test_large_prompt_produces_small_prompt_independent_descriptor(tmp_path):
    large = "LARGE_SYNTHETIC_PROMPT\n" + ("x" * 60_000)
    watcher, identity = prepared(tmp_path, prompt=large)
    process, calls = dispatch(watcher)
    assert process.pid == 43210
    descriptor = calls[0][0]
    assert identity["promptByteLength"] >= 50_000
    assert len(descriptor.encode("utf-8")) < 2_000
    assert len(descriptor.encode("utf-8")) < identity["promptByteLength"]
    assert hashlib.sha256(large.encode("utf-8")).hexdigest() in descriptor
    assert large not in descriptor


def test_f9_pause_still_prevents_artifact_dispatch(tmp_path):
    watcher, _ = prepared(tmp_path)
    watcher.state["discussionPauseActive"] = True
    process, calls = dispatch(watcher)
    assert process is None and calls == []
    assert watcher.state.get("executorLaunchState") != "LAUNCH_CLAIMED"


def test_legacy_inline_mode_is_explicit_and_artifact_marker_never_falls_back(tmp_path, caplog):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "orchestrator")
    staged = tmp_path / "legacy.txt"
    staged.write_text(PROMPT, encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "legacy-task", "nextTaskId": "legacy-task",
                         "nextPromptPath": str(staged), "discussionPauseActive": False})
    watcher.runtime_logger = logging.getLogger("legacy-dispatch-test")
    with caplog.at_level(logging.INFO, logger="legacy-dispatch-test"):
        process, calls = dispatch(watcher)
    assert process.pid == 43210 and calls[0][0] == PROMPT
    assert "EXECUTOR_PROMPT_LEGACY_INLINE_DISPATCH" in {record.event for record in caplog.records}

    watcher, _ = prepared(tmp_path / "second")
    watcher.state["promptArtifactVersion"] = 1
    watcher.state["promptState"] = "CORRUPT"
    process, calls = dispatch(watcher)
    assert process is None and calls == []


def test_executor_bootstrap_contract_requires_independent_descriptor_verification(tmp_path):
    bootstrap = Path(__file__).parent / "AFFOTECH_EXECUTOR_BOOTSTRAP.md"
    runner = CodexRunner(str(tmp_path), bootstrap_path=bootstrap)
    assembled = runner.assemble_prompt("<EXECUTOR_PROMPT_ARTIFACT>\n{}\n</EXECUTOR_PROMPT_ARTIFACT>")
    assert "verify the supplied task ID, SHA256, byte length, and manifest identity" in assembled
    assert "PROMPT_ARTIFACT_INVALID" in assembled
