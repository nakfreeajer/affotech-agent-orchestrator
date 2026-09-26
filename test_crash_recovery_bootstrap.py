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
    d, _ = discovery(tmp_path / "missing", "RESULT_READY")
    assert bootstrap.classify_workflow(d) == "BLOCK_RESULT_EVIDENCE_MISSING"
    d, state = discovery(tmp_path / "empty", "RESULT_READY")
    result = Path(state["executorResultPath"]); result.parent.mkdir(); result.write_text("", encoding="utf-8")
    d, _ = discovery(tmp_path / "empty", "RESULT_READY")
    assert bootstrap.classify_workflow(d) == "BLOCK_RESULT_EVIDENCE_MISSING"


def test_architect_running_cases_are_conservative(tmp_path):
    d, _ = discovery(tmp_path / "generating", "ARCHITECT_RUNNING")
    assert bootstrap.classify_workflow(d, {"generationVisible": True}) == "WAIT_EXISTING_ARCHITECT"
    assert bootstrap.classify_workflow(d, {"generationVisible": False, "newCompletedResponse": True, "durableBaselineAdvanced": True}) == "ARCHITECT_RECOVERY_INCONCLUSIVE"
    assert bootstrap.classify_workflow(d, {"generationVisible": False, "newCompletedResponse": False}) == "ARCHITECT_RECOVERY_INCONCLUSIVE"


def _baseline(count, marker):
    return {"count": count, "text_hash": marker * 64}


def test_architect_baseline_proof_requires_valid_durable_advancement():
    state = {"state": "ARCHITECT_RUNNING"}
    current = _baseline(2, "b")
    assert bootstrap.classify_architect_observation(state, {"generationVisible": False, "newCompletedResponse": True, "durableBaselineAdvanced": True}) == "ARCHITECT_RECOVERY_INCONCLUSIVE"
    for persisted in ({}, {"count": "1", "text_hash": "bad"}, _baseline(2, "b")):
        observation = {"generationVisible": False, "newCompletedResponse": bootstrap.architect_baseline_advanced(persisted, current), "durableBaselineAdvanced": bootstrap.architect_baseline_advanced(persisted, current)}
        assert bootstrap.classify_architect_observation(state, observation) == "ARCHITECT_RECOVERY_INCONCLUSIVE"
    advanced = {"generationVisible": False, "newCompletedResponse": True, "durableBaselineValid": True, "durableBaselineAdvanced": bootstrap.architect_baseline_advanced(_baseline(1, "a"), current)}
    assert bootstrap.classify_architect_observation(state, advanced) == "CONSUME_EXISTING_ARCHITECT_RESPONSE"
    assert bootstrap.classify_architect_observation(state, {"generationVisible": True}) == "WAIT_EXISTING_ARCHITECT"


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
    assert bootstrap.classify_workflow(d) == "EXECUTOR_PID_OWNERSHIP_INCONCLUSIVE"


def test_executor_session_ownership_distinguishes_reused_pid_and_other_session(tmp_path):
    d, _ = discovery(tmp_path / "reused", "EXECUTOR_RUNNING", codexPid=99, records=[
        {"ProcessId": 99, "CommandLine": "codex unrelated-session"},
    ])
    assert d["executorPidAlive"] is True
    assert d["executorPidOwned"] is False
    assert bootstrap.classify_workflow(d) == "EXECUTOR_PID_OWNERSHIP_INCONCLUSIVE"
    d, _ = discovery(tmp_path / "different", "EXECUTOR_RUNNING", codexPid=99, records=[
        {"ProcessId": 99, "CommandLine": "codex unrelated-session"},
        {"ProcessId": 100, "CommandLine": "codex session-087"},
    ])
    assert bootstrap.classify_workflow(d) == "EXECUTOR_SESSION_ACTIVE_WRITER"


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
        def assistant_baseline(self): return {"count": 2, "text_hash": "b" * 64}
        def generation_visible(self): return False
        def close(self): closed.append(True)

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda endpoint, conversation: Bridge()))
    state = {"state": "ARCHITECT_RUNNING", "architectBaseline": {"count": 1, "text_hash": "a" * 64}}
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
    assert "$reason" not in script
    assert 'ARCHITECT_CDP_ABSENT' in script
    assert 'ARCHITECT_CDP_UNVERIFIED' in script


def test_repository_identity_is_exactly_governed_remote_and_branch():
    assert bootstrap.repository_identity("https://github.com/nakfreeajer/affotech-agent-orchestrator.git") == bootstrap.EXPECTED_REPOSITORY_IDENTITY
    assert bootstrap.repository_identity("git@github.com:nakfreeajer/affotech-agent-orchestrator.git") == bootstrap.EXPECTED_REPOSITORY_IDENTITY
    assert bootstrap.repository_identity("https://github.com/other/repo.git") != bootstrap.EXPECTED_REPOSITORY_IDENTITY


def _rollover_retry_fixture(tmp_path, monkeypatch):
    state_dir = tmp_path / "orchestrator"
    prompt = state_dir / "prompts" / "000103.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_bytes(b"synthetic immutable task prompt\n")
    worktree = tmp_path / "worktree-000103"
    worktree.mkdir()
    session_id = bootstrap.LEGACY_DIAGNOSTIC_RETRY_EXECUTOR_SESSION_ID
    state = {
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": "LEGACY_HANDOVER_REEMISSION_INVALID",
        "taskId": "000102", "lastCompletedTaskId": "000102", "nextTaskId": "000103",
        "nextPromptPath": str(prompt),
        "discussionPauseActive": False,
        "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": False,
        "handoverRequested": True, "handoverReady": False,
        "rolloverHandoverProtocolVersion": None,
        "rolloverTransactionId": bootstrap.LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID,
        "rolloverTransactionTaskId": "000103",
        "rolloverRecoveryEpoch": 2,
        "rolloverAutomaticRecoveryEpochCount": 1,
        "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverLegacyHandoverReemissionTransactionId": bootstrap.LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID,
        "rolloverLegacyHandoverReemissionAttemptedEpoch": 2,
        "rolloverLegacyHandoverReemissionState": "INVALID_RESPONSE",
        "executorSessionId": session_id, "executorSessionMode": "PERSISTENT",
        "executorProcessState": "COMPLETED_WITH_RESULT",
        "taskWorktrees": {"000103": {"taskId": "000103", "worktreePath": str(worktree)}},
    }
    digest = __import__("hashlib").sha256(prompt.read_bytes()).hexdigest().upper()
    monkeypatch.setattr(bootstrap, "LEGACY_DIAGNOSTIC_RETRY_PROMPT_SHA256", digest)
    discovery_record = {
        "state": state, "stateDir": str(state_dir), "watcherRunning": False,
        "executorSessionExists": True, "activeWriterPresent": False,
        "executorPidAlive": False,
        "executorPidAliveByField": {"codexPid": False, "active_codex_pid": False},
    }
    return discovery_record, prompt


def test_exact_legacy_rollover_diagnostic_retry_preflight_and_fail_closed_cases(tmp_path, monkeypatch):
    d, prompt = _rollover_retry_fixture(tmp_path, monkeypatch)
    assert bootstrap.validate_rollover_diagnostic_retry(d) == (True, "SAFE_TO_AUTHORIZE_ONE_ROLLOVER_DIAGNOSTIC_RETRY")

    mutations = [
        ("humanRequiredReason", "OTHER", "HUMAN_REQUIRED_REASON_MISMATCH"),
        ("rolloverTransactionId", "wrong", "TRANSACTION_IDENTITY_MISMATCH"),
        ("rolloverTransactionTaskId", "000104", "TRANSACTION_IDENTITY_MISMATCH"),
        ("nextTaskId", "000104", "NEXT_TASK_MISMATCH"),
        ("executorSessionId", "other-session", "PERSISTENT_EXECUTOR_SESSION_MISMATCH"),
    ]
    for field, value, reason in mutations:
        d["state"][field] = value
        assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == reason
        d["state"][field] = {
            "humanRequiredReason": "LEGACY_HANDOVER_REEMISSION_INVALID",
            "rolloverTransactionId": bootstrap.LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID,
            "rolloverTransactionTaskId": "000103", "nextTaskId": "000103",
            "executorSessionId": bootstrap.LEGACY_DIAGNOSTIC_RETRY_EXECUTOR_SESSION_ID,
        }[field]

    d["state"]["discussionPauseActive"] = True
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "DISCUSSION_PAUSE_ACTIVE"
    d["state"]["discussionPauseActive"] = False
    d["activeWriterPresent"] = True
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "EXECUTOR_PROCESS_OR_WRITER_ACTIVE"
    d["activeWriterPresent"] = False
    d["watcherRunning"] = True
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "WATCHER_ALREADY_RUNNING"
    d["watcherRunning"] = False
    d["state"]["rolloverDiagnosticRetryAuthorizationConsumedTransactionId"] = bootstrap.LEGACY_DIAGNOSTIC_RETRY_TRANSACTION_ID
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "ROLLOVER_DIAGNOSTIC_AUTHORIZATION_ALREADY_CONSUMED"
    d["state"].pop("rolloverDiagnosticRetryAuthorizationConsumedTransactionId")
    d["state"]["rolloverFreshCandidateConversationId"] = "candidate-already-created"
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "ROLLOVER_ALREADY_ADVANCED"
    d["state"].pop("rolloverFreshCandidateConversationId")

    original_path = d["state"]["nextPromptPath"]
    d["state"]["nextPromptPath"] = str(prompt.parent / "wrong.txt")
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "STAGED_PROMPT_IDENTITY_MISMATCH"
    d["state"]["nextPromptPath"] = original_path
    d["executorSessionExists"] = False
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "PERSISTENT_EXECUTOR_SESSION_MISMATCH"
    d["executorSessionExists"] = True
    d["executorPidAliveByField"]["codexPid"] = True
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "EXECUTOR_PROCESS_OR_WRITER_ACTIVE"
    d["executorPidAliveByField"]["codexPid"] = False

    prompt.write_bytes(b"modified prompt")
    assert bootstrap.validate_rollover_diagnostic_retry(d)[1] == "STAGED_PROMPT_IDENTITY_MISMATCH"


def test_validated_durable_handover_blocks_unnecessary_diagnostic_retry(tmp_path, monkeypatch):
    d, _prompt = _rollover_retry_fixture(tmp_path, monkeypatch)
    d["state"]["pending_handover"] = (
        "Rollover transaction ID: 7fdbd798659f42295a18dd2d\n"
        "taskId=000103\nARCHITECT_HANDOVER_READY"
    )
    assert bootstrap.validate_rollover_diagnostic_retry(d) == (False, "VALIDATED_DURABLE_HANDOVER_ALREADY_EXISTS")
    d["state"].pop("pending_handover")
    d["state"]["rolloverHandoverResponseIdentity"] = "persisted-but-payload-missing"
    assert bootstrap.validate_rollover_diagnostic_retry(d) == (False, "DURABLE_HANDOVER_EVIDENCE_INCOMPLETE")


def test_rollover_retry_cli_status_is_read_only_and_wrapper_authority_is_separate(tmp_path, monkeypatch, capsys):
    d, _prompt = _rollover_retry_fixture(tmp_path, monkeypatch)
    state_dir = Path(d["stateDir"])
    state_dir.mkdir(exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(d["state"]), encoding="utf-8")
    before = (state_dir / "state.json").read_bytes()
    monkeypatch.setattr(bootstrap, "_default_process_records", lambda: [])
    monkeypatch.setattr(bootstrap, "_session_exists", lambda _session, _home=None: True)
    assert bootstrap.main([
        "--repository", str(Path(__file__).resolve().parent), "--state-dir", str(state_dir),
        "--validate-rollover-diagnostic-retry",
    ]) == 0
    assert (state_dir / "state.json").read_bytes() == before
    output = json.loads(capsys.readouterr().out)
    assert output["rolloverDiagnosticRetryEligible"] is True
    assert bootstrap.classify_workflow({"state": d["state"], "resultNonEmpty": False}) == "HUMAN_REQUIRED_NO_AUTOMATIC_ACTION"
    script = Path(__file__).with_name("AFFOTECH-START.ps1").read_text(encoding="utf-8")
    assert "AuthorizeRolloverDiagnosticRetry" in script
    assert "ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY" in script
    assert "ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY" in script
    assert script.index("if ($StatusOnly)") < script.index("ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY =")
    assert script.index('Read-Host "Type START') < script.index('ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY = "7fdbd798659f42295a18dd2d"')
    import inspect
    import local_orchestrator_watcher as watcher_module
    main_source = inspect.getsource(watcher_module.main)
    assert main_source.index("hotkeys.start") < main_source.index("consume_rollover_diagnostic_retry_authorization") < main_source.index("while True")
