import json
import subprocess
from pathlib import Path

import crash_recovery_bootstrap as bootstrap


def state_for(tmp_path, workflow, **extra):
    state_dir = tmp_path / ".agent-work" / "orchestrator"
    state_dir.mkdir(parents=True, exist_ok=True)
    prompt = state_dir / "prompts" / "000087.txt"
    prompt.parent.mkdir(exist_ok=True)
    prompt.write_text("prompt", encoding="utf-8")
    state = {
        "state": workflow, "taskId": "000087", "nextTaskId": "000087",
        "lastCompletedTaskId": "000086", "nextPromptPath": str(prompt),
        "executorSessionId": "session-087", "architectConversationId": "arch-087",
        "rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False,
        "executorResultPath": str(state_dir / "results" / "000087.txt"),
    }
    state.update(extra)
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return state_dir, state


def discovery(tmp_path, workflow, records=None, codex_home=None, **extra):
    state_dir, state = state_for(tmp_path, workflow, **extra)
    repo = Path(__file__).resolve().parent
    return bootstrap.discover(repo, state_dir, records or [], codex_home=codex_home), state


def test_idle_and_next_prompt_ready_are_safe_normal_start(tmp_path):
    d, _ = discovery(tmp_path / "idle", "IDLE")
    assert bootstrap.classify_workflow(d) == "SAFE_NORMAL_START"
    d, _ = discovery(tmp_path / "ready", "NEXT_PROMPT_READY")
    assert bootstrap.classify_workflow(d) == "SAFE_NORMAL_START"


def test_result_ready_is_safe_result_recovery(tmp_path):
    d, state = discovery(tmp_path, "RESULT_READY")
    result = Path(state["executorResultPath"])
    result.parent.mkdir()
    result.write_text("valid result", encoding="utf-8")
    d, _ = discovery(tmp_path, "RESULT_READY")
    assert bootstrap.classify_workflow(d) == "SAFE_RESULT_RECOVERY"


def test_architect_running_cases_are_conservative(tmp_path):
    d, _ = discovery(tmp_path / "generating", "ARCHITECT_RUNNING")
    assert bootstrap.classify_workflow(d, {"generationVisible": True}) == "WAIT_EXISTING_ARCHITECT"
    assert bootstrap.classify_workflow(d, {"generationVisible": False, "newCompletedResponse": True}) == "CONSUME_EXISTING_ARCHITECT_RESPONSE"
    assert bootstrap.classify_workflow(d, {"generationVisible": False, "newCompletedResponse": False}) == "ARCHITECT_RECOVERY_INCONCLUSIVE"


def test_executor_live_dead_with_result_and_dead_without_result(tmp_path):
    d, state = discovery(tmp_path / "live", "EXECUTOR_RUNNING", records=[{"ProcessId": 44, "CommandLine": "codex session-087"}], codexPid=44)
    assert bootstrap.classify_workflow(d) == "WAIT_EXISTING_EXECUTOR"
    d, state = discovery(tmp_path / "result", "EXECUTOR_RUNNING", codexPid=45)
    result = Path(state["executorResultPath"]); result.parent.mkdir(); result.write_text("done", encoding="utf-8")
    d, _ = discovery(tmp_path / "result", "EXECUTOR_RUNNING", codexPid=45)
    assert bootstrap.classify_workflow(d) == "RECOVER_EXISTING_EXECUTOR_RESULT"
    d, _ = discovery(tmp_path / "dead", "EXECUTOR_RUNNING", codexPid=46)
    assert bootstrap.classify_workflow(d) == "EXECUTOR_INTERRUPTED_NO_RESULT"


def test_human_required_never_automatically_retries(tmp_path):
    d, _ = discovery(tmp_path, "HUMAN_REQUIRED", humanRequiredReason="EXECUTOR_EXITED_WITHOUT_RESULT", executorLaunchState="POSTLAUNCH_NO_RESULT")
    assert bootstrap.classify_workflow(d) == "HUMAN_REQUIRED_NO_AUTOMATIC_ACTION"


def test_retry_current_wrong_and_consumed_authorizations(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text('{"id":"session-087"}\n', encoding="utf-8")
    d, state = discovery(tmp_path / "current", "HUMAN_REQUIRED", codex_home=codex_home, humanRequiredReason="EXECUTOR_EXITED_WITHOUT_RESULT", executorLaunchState="POSTLAUNCH_NO_RESULT", humanRecoveryAuthorizationConsumed=True, humanRecoveryAuthorizedTaskId="old")
    assert bootstrap.validate_retry(d, "000087")[0]
    assert bootstrap.validate_retry(d, "wrong")[1] == "HUMAN_RECOVERY_TASK_MISMATCH"
    state["humanRecoveryAuthorizedTaskId"] = "000087"
    Path(d["statePath"]).write_text(json.dumps(state), encoding="utf-8")
    d, _ = discovery(tmp_path / "current", "HUMAN_REQUIRED", codex_home=codex_home, humanRequiredReason="EXECUTOR_EXITED_WITHOUT_RESULT", executorLaunchState="POSTLAUNCH_NO_RESULT", humanRecoveryAuthorizationConsumed=True, humanRecoveryAuthorizedTaskId="000087")
    assert bootstrap.validate_retry(d, "000087")[1] == "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED"


def test_active_writer_only_blocks_matching_session(tmp_path):
    d, _ = discovery(tmp_path / "owned", "EXECUTOR_RUNNING", codexPid=99, records=[{"ProcessId": 99, "CommandLine": "codex session-087"}])
    assert d["activeWriterPresent"] is True
    assert bootstrap.classify_workflow(d) == "WAIT_EXISTING_EXECUTOR"
    d, _ = discovery(tmp_path / "conflict", "EXECUTOR_RUNNING", codexPid=99, records=[
        {"ProcessId": 99, "CommandLine": "codex unrelated-process"},
        {"ProcessId": 100, "CommandLine": "codex session-087"},
    ])
    assert bootstrap.classify_workflow(d) == "EXECUTOR_SESSION_ACTIVE_WRITER"
    d, _ = discovery(tmp_path / "other", "EXECUTOR_RUNNING", codexPid=100, records=[{"ProcessId": 100, "CommandLine": "codex other-session"}])
    assert d["activeWriterPresent"] is False


def test_watcher_detection_prevents_duplicate_start(tmp_path):
    d, _ = discovery(tmp_path, "NEXT_PROMPT_READY", records=[{"ProcessId": 123, "CommandLine": "python local_orchestrator_watcher.py"}])
    assert d["watcherRunning"] is True
    assert bootstrap.validate_retry(d, "000087")[1] == "WATCHER_ALREADY_RUNNING"


def test_dynamic_architect_identity_and_state_summary(tmp_path):
    d, state = discovery(tmp_path, "NEXT_PROMPT_READY")
    state["architectConversationId"] = "new-architect"
    Path(d["statePath"]).write_text(json.dumps(state), encoding="utf-8")
    d = bootstrap.discover(Path(__file__).resolve().parent, d["stateDir"])
    assert d["state"]["architectConversationId"] == "new-architect"


def test_cdp_health_is_read_only(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert bootstrap.architect_cdp_health("http://127.0.0.1:9333") == (True, "ARCHITECT_CDP_HEALTHY")


def test_architect_probe_uses_durable_dynamic_identity_and_closes_read_only(monkeypatch, tmp_path):
    import local_orchestrator_watcher as watcher_module

    closed = []
    class Bridge:
        def assistant_baseline(self): return {"count": 2, "text_hash": "new"}
        def generation_visible(self): return False
        def close(self): closed.append(True)

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda endpoint, conversation: Bridge()))
    state = {"state": "ARCHITECT_RUNNING", "architectBaseline": {"count": 1, "text_hash": "old"}}
    observed = bootstrap.probe_architect("http://127.0.0.1:9333", "durable-architect-id", state)
    assert observed["classification"] == "CONSUME_EXISTING_ARCHITECT_RESPONSE"
    assert closed == [True]


def test_unverified_architect_attachment_fails_closed(monkeypatch):
    import local_orchestrator_watcher as watcher_module
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_: (_ for _ in ()).throw(RuntimeError("wrong browser"))))
    try:
        bootstrap.probe_architect("http://127.0.0.1:9333", "durable-architect-id", {"state": "ARCHITECT_RUNNING"})
    except RuntimeError as error:
        assert str(error) == "wrong browser"
    else:
        raise AssertionError("unverified attachment must fail closed")


def test_status_classification_is_read_only(tmp_path, capsys):
    state_dir, _ = state_for(tmp_path, "NEXT_PROMPT_READY")
    before = (state_dir / "state.json").read_bytes()
    assert bootstrap.main(["--repository", str(Path(__file__).resolve().parent), "--state-dir", str(state_dir), "--classify"]) == 0
    assert (state_dir / "state.json").read_bytes() == before
    assert "SAFE_NORMAL_START" in capsys.readouterr().out


def test_bootstrap_watcher_is_visible_and_brave_launch_is_governed():
    script = Path(__file__).with_name("AFFOTECH-START.ps1").read_text(encoding="utf-8")
    assert "-WindowStyle Hidden" not in script
    assert "--remote-debugging-port=$ArchitectPort" in script
    assert "--user-data-dir=$ArchitectProfile" in script
    assert "local_orchestrator_watcher.py" in script
