import json
import hashlib
import ast
import inspect
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from local_orchestrator_watcher import (ArchitectPlaywright, LocalFirstOrchestrator,
                                        LocalWatcher,
                                        ResultSubmissionError, atomic_write,
                                        parse_orchestrator_result, resolve_executor_worktree,
                                        run_executor_state_once, visible_executor_launcher,
                                        WatcherInstanceLock, handle_architect_value_error,
                                        run_human_required_startup_once, DiscussionHotkeyController,
                                        RemoteDiscussionControlMonitor,
                                        architect_process_tree_memory_bytes,
                                        resident_human_decision_response,
                                        dispatch_next_prompt_once)
import local_orchestrator_watcher as watcher_module


def envelope(task, action="EXECUTE", prompt="next task", documentation=None):
    body = prompt if action == "EXECUTE" else ""
    documentation_line = f"documentation={documentation or 'NOT_REQUIRED'}\n"
    return (f"explanation\n<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\n"
            f"action={action}\ntaskId={task}\n{documentation_line}promptBegin\n{body}\n"
            "promptEnd\n</ORCHESTRATOR_RESULT>")


def canonical_handover(watcher, body="complete handover", task_id=None, transaction_id=None):
    """Create production-protocol evidence for a test rollover response."""
    transaction_id = transaction_id or watcher.state.get("rolloverTransactionId") or "test-rollover-transaction"
    task_id = task_id or watcher.state.get("rolloverTransactionTaskId") or watcher.state.get("nextTaskId") or "test-task"
    watcher.state.setdefault("rolloverTransactionId", transaction_id)
    watcher.state.setdefault("rolloverTransactionTaskId", task_id)
    watcher.state["rolloverHandoverProtocolVersion"] = 1
    return watcher_module.make_handover_envelope(transaction_id, task_id, body)


def ready(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / ".agent-work" / "orchestrator")
    result = tmp_path / "report.txt"
    result.write_text("executor report", encoding="utf-8")
    watcher.state.update({"state": "RESULT_READY", "taskId": "task-1", "executorResultPath": str(result)})
    watcher.save()
    return watcher


def configured_git_project(tmp_path):
    bare = tmp_path / "sample-project.git"
    base = tmp_path / "base"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(base)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(base), "config", "user.name", "Orchestrator Test"], check=True)
    (base / "README.md").write_text("authority\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(base), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "commit", "-m", "authority"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "branch", "-M", "hybrid-v2"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "push", "-u", "origin", "hybrid-v2"], check=True, capture_output=True)
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    return base, {"version": 1, "projects": {"sample-project": {
        "repository": "sample-project.git", "baseRepo": str(base), "branch": "hybrid-v2", "remote": "origin"
    }}}, head


def configured_project_prompt(base, head, task_id="owned-task"):
    return (f"repository=sample-project.git\nbranch=hybrid-v2\n"
            f"currentBranchHeadAtArchitectDecision={head}\n"
            f"bounded task in {task_id}")


def write_project_config(watcher, config):
    watcher.state_dir.mkdir(parents=True, exist_ok=True)
    (watcher.state_dir / "project-config.json").write_text(json.dumps(config), encoding="utf-8")


def runtime_event_capture():
    records = []
    logger = logging.getLogger(f"orchestrator-test-{id(records)}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger.addHandler(Capture())
    return logger, records


def test_normal_runtime_decision_instrumentation_is_structured_and_non_sensitive(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=False)
    logger, records = runtime_event_capture()
    watcher.runtime_logger = logger
    watcher.runtime_run_id = "synthetic-run"
    watcher.state.update({"taskId": "000079", "nextTaskId": "000080", "nextPromptPath": str(prompt), "architectConversationId": "ARCH-OLD"})

    before = dict(watcher.state)
    watcher.session_rollover.sample_memory(lambda: watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES - 1)
    watcher_module.log_main_loop_decision(watcher, "NEXT_PROMPT_READY_DISPATCH", "synthetic_observation", True, False)
    watcher.session_rollover.request_if_due(
        type("Bridge", (), {})(), True, False, safe_boundary_state="NEXT_PROMPT_READY"
    )
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: False)
    watcher.state["rolloverDue"] = True
    watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False, logger, "synthetic-run")

    events = {record.event for record in records}
    assert {"MAIN_LOOP_DECISION", "ARCHITECT_MEMORY_SAMPLE_TICK", "ARCHITECT_MEMORY_SAMPLE_RESULT",
            "ARCHITECT_MEMORY_THRESHOLD_DECISION", "ROLLOVER_TRIGGER_DECISION",
            "ROLLOVER_EXECUTION_GATE", "CODEX_DISPATCH_GATE"}.issubset(events)
    assert watcher.state["taskId"] == before["taskId"]
    assert watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES == 891289600
    rendered = "\n".join(record.getMessage() for record in records)
    assert "synthetic prompt body" not in rendered
    assert "synthetic result body" not in rendered
    assert "ARCHITECT_HANDOVER_READY" not in rendered
    assert "token=" not in rendered.lower()


def test_result_and_handover_instrumentation_logs_decisions_without_bodies(tmp_path):
    watcher = ready(tmp_path)
    logger, records = runtime_event_capture()
    watcher.runtime_logger = logger
    watcher.runtime_run_id = "synthetic-run"

    class ResultBridge:
        def generation_visible(self): return False
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def user_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def submit_result_bounded(self, _payload): raise ResultSubmissionError("SYNTHETIC_SEND_FAILURE")

    with pytest.raises(ResultSubmissionError):
        watcher.deliver_result(ResultBridge())

    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
        "nextPromptPath": str(tmp_path / "next.txt"), "rolloverDue": True,
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
        "architectConversationId": "ARCH-OLD",
    })
    Path(watcher.state["nextPromptPath"]).write_text("synthetic staged task", encoding="utf-8")

    class HandoverBridge:
        def submit_result_bounded(self, _payload): pass

    assert watcher.session_rollover.request_if_due(HandoverBridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY") is True
    events = {record.event for record in records}
    assert {"RESULT_DELIVERY_DECISION", "RESULT_DELIVERY_SEND_STAGE", "RESULT_DELIVERY_ACK_DECISION",
            "HANDOVER_SEND_DECISION", "HANDOVER_SEND_STAGE"}.issubset(events)
    rendered = "\n".join(record.getMessage() for record in records)
    assert "executor report" not in rendered
    assert "synthetic staged task" not in rendered


@pytest.mark.parametrize("workflow_state", ["NEXT_PROMPT_READY", "RESULT_READY", "IDLE"])
def test_paused_workflow_still_samples_architect_memory_without_actions(tmp_path, monkeypatch, workflow_state):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({
        "state": workflow_state,
        "taskId": "000079",
        "nextTaskId": "000080",
        "discussionPauseActive": True,
        "rolloverDue": False,
        "architectConversationId": "ARCH-OLD",
    })
    watcher.save()
    calls = {"attach": 0, "close": 0, "send": 0, "navigate": 0, "new_page": 0}

    class ReadOnlyBridge:
        def current_session_memory_bytes(self):
            return 700 * 1024 * 1024
        def close(self):
            calls["close"] += 1
        def submit_result_bounded(self, _payload):
            calls["send"] += 1
        def goto(self, _url):
            calls["navigate"] += 1

    def attach(_endpoint, _conversation_id):
        calls["attach"] += 1
        return ReadOnlyBridge()

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(attach))
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is True
    assert watcher.state["architectMemoryBytes"] == 700 * 1024 * 1024
    assert watcher.state["rolloverDue"] is False
    assert calls == {"attach": 1, "close": 0, "send": 0, "navigate": 0, "new_page": 0}
    assert watcher.state["state"] == workflow_state
    assert watcher.state["discussionPauseActive"] is True


def test_paused_memory_crossing_sets_due_without_workflow_action(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
        "discussionPauseActive": True, "rolloverDue": False, "architectConversationId": "ARCH-OLD",
    })
    watcher.save()
    memory = {"bytes": 700 * 1024 * 1024}
    calls = {"attach": 0, "close": 0, "send": 0, "navigate": 0, "new_page": 0}

    class ReadOnlyBridge:
        def current_session_memory_bytes(self):
            return memory["bytes"]
        def close(self):
            calls["close"] += 1
        def submit_result_bounded(self, _payload):
            calls["send"] += 1
        def goto(self, _url):
            calls["navigate"] += 1

    def attach(_endpoint, _conversation_id):
        calls["attach"] += 1
        return ReadOnlyBridge()

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(attach))
    for value in (700, 1500, 1500):
        watcher.session_rollover._next_memory_sample_at = 0
        memory["bytes"] = value * 1024 * 1024
        assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is True

    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["taskId"] == "000079"
    assert watcher.state["nextTaskId"] == "000080"
    assert watcher.state["discussionPauseActive"] is True
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"
    assert calls == {"attach": 3, "close": 0, "send": 0, "navigate": 0, "new_page": 0}


def test_paused_memory_cooldown_and_reader_failure_are_workflow_neutral(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
                          "discussionPauseActive": True, "rolloverDue": False})
    watcher.save()
    calls = {"attach": 0, "close": 0}

    class FailingBridge:
        def current_session_memory_bytes(self):
            raise RuntimeError("SYNTHETIC_MEMORY_FAILURE")
        def close(self):
            calls["close"] += 1

    def attach(_endpoint, _conversation_id):
        calls["attach"] += 1
        return FailingBridge()

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(attach))
    before = {key: watcher.state.get(key) for key in ("state", "taskId", "nextTaskId", "discussionPauseActive", "rolloverDue")}
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is True
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is False
    after = {key: watcher.state.get(key) for key in before}
    assert after == before
    assert watcher.state.get("humanRequiredReason") is None
    assert calls == {"attach": 1, "close": 0}


def test_passive_probe_uses_real_read_only_disconnect_lifecycle(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
                          "discussionPauseActive": True, "rolloverDue": False,
                          "architectConversationId": "ARCH-OLD"})
    watcher.save()
    counts = {"browser_close": 0, "page_close": 0, "context_close": 0, "runtime_stop": 0}

    class Browser:
        def close(self):
            counts["browser_close"] += 1
            raise AssertionError("passive probe must not close Browser")

    class Context:
        def close(self):
            counts["context_close"] += 1
            raise AssertionError("passive probe must not close context")

    class Page:
        context = Context()
        url = "https://chatgpt.com/c/ARCH-OLD"
        def close(self):
            counts["page_close"] += 1
            raise AssertionError("passive probe must not close page")

    class Runtime:
        def stop(self):
            counts["runtime_stop"] += 1

    bridge = ArchitectPlaywright(Page())
    bridge._browser = Browser()
    bridge._runtime = Runtime()
    bridge.current_session_memory_bytes = lambda: 700 * 1024 * 1024

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is True
    assert counts == {"browser_close": 0, "page_close": 0, "context_close": 0, "runtime_stop": 1}
    assert bridge._browser is None and bridge._runtime is None


def test_passive_probe_keeps_existing_bridge_and_normal_close_semantics(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "RESULT_READY", "taskId": "000079", "discussionPauseActive": True})
    watcher.save()
    counts = {"browser_close": 0, "runtime_stop": 0}

    class Browser:
        def close(self): counts["browser_close"] += 1

    class Runtime:
        def stop(self): counts["runtime_stop"] += 1

    existing = ArchitectPlaywright(object())
    existing._browser = Browser()
    existing._runtime = Runtime()
    existing.current_session_memory_bytes = lambda: 700 * 1024 * 1024
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint", existing_bridge=existing) is True
    assert counts == {"browser_close": 0, "runtime_stop": 0}
    assert existing._browser is not None and existing._runtime is not None

    normal = ArchitectPlaywright(object())
    normal._browser = Browser()
    normal._runtime = Runtime()
    normal.close()
    assert counts == {"browser_close": 1, "runtime_stop": 1}


def test_passive_probe_cleanup_failure_is_workflow_neutral(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
                          "discussionPauseActive": True, "rolloverDue": False})
    watcher.save()

    class Runtime:
        def stop(self): raise RuntimeError("SYNTHETIC_DISCONNECT_FAILURE")

    bridge = ArchitectPlaywright(object())
    bridge._browser = type("Browser", (), {"close": lambda _self: (_ for _ in ()).throw(AssertionError("browser close"))})()
    bridge._runtime = Runtime()
    bridge.current_session_memory_bytes = lambda: 700 * 1024 * 1024
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["taskId"] == "000079"
    assert watcher.state.get("humanRequiredReason") is None


def test_remote_control_canonicalization_does_not_publish_memory_reader(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    identity = "11111111-1111-1111-1111-111111111111"
    main_reader = lambda: 700 * 1024 * 1024
    watcher.architect_memory_reader = main_reader
    watcher.architect_memory_reader_thread_id = threading.get_ident()
    bridge = type("RemoteBridge", (), {
        "page": _AckPage("https://chatgpt.com/c/" + identity),
        "current_session_memory_bytes": lambda self: 1500 * 1024 * 1024,
    })()
    assert watcher_module.canonicalize_attached_architect_conversation(
        watcher, bridge, identity, bind_memory=False
    ) == identity
    assert watcher.architect_memory_reader is main_reader
    assert watcher.architect_memory_reader_thread_id == threading.get_ident()


def test_main_thread_canonicalization_binds_reader_owner(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    identity = "22222222-2222-2222-2222-222222222222"
    bridge = type("MainBridge", (), {
        "page": _AckPage("https://chatgpt.com/c/" + identity),
        "current_session_memory_bytes": lambda self: 700 * 1024 * 1024,
    })()
    assert watcher_module.canonicalize_attached_architect_conversation(watcher, bridge, identity) == identity
    assert watcher.architect_memory_reader is not None
    assert watcher.architect_memory_reader_thread_id == threading.get_ident()


def test_cross_thread_memory_reader_is_rejected_and_logged(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    logger, records = runtime_event_capture()
    watcher.runtime_logger = logger
    owner_calls = {"count": 0}

    def owner_reader():
        owner_calls["count"] += 1
        return 700 * 1024 * 1024

    owner_thread_id = {}
    def bind_on_owner_thread():
        watcher.bind_architect_session_memory(type("OwnerBridge", (), {"current_session_memory_bytes": owner_reader})(), "ARCH-OLD")
        owner_thread_id["id"] = threading.get_ident()

    owner_thread = threading.Thread(target=bind_on_owner_thread, name="synthetic-playwright-owner")
    owner_thread.start()
    owner_thread.join()
    assert owner_thread_id["id"] != threading.get_ident()
    assert watcher.session_rollover.sample_memory() is None
    assert owner_calls["count"] == 0
    assert any(record.event == "ARCHITECT_MEMORY_SAMPLE_SKIPPED" and
               "skipReason=MEMORY_READER_THREAD_MISMATCH" in record.getMessage()
               for record in records)


def test_cross_thread_reader_falls_back_to_main_thread_passive_probe(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
                          "discussionPauseActive": True, "rolloverDue": False,
                          "architectConversationId": "ARCH-OLD"})
    watcher.save()
    owner_calls = {"count": 0}
    main_calls = {"count": 0}

    def owner_reader():
        owner_calls["count"] += 1
        return 700 * 1024 * 1024

    def bind_on_owner_thread():
        watcher.bind_architect_session_memory(type("OwnerBridge", (), {"current_session_memory_bytes": owner_reader})(), "ARCH-OLD")

    owner_thread = threading.Thread(target=bind_on_owner_thread, name="synthetic-playwright-owner")
    owner_thread.start()
    owner_thread.join()

    class MainBridge:
        def current_session_memory_bytes(self):
            main_calls["count"] += 1
            return 1500 * 1024 * 1024
        def close(self): pass

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: MainBridge()))
    assert watcher_module.passive_architect_memory_sample_for_pause(watcher, "synthetic-endpoint") is True
    assert owner_calls["count"] == 0
    assert main_calls["count"] == 1
    assert watcher.state["architectMemoryBytes"] == 1500 * 1024 * 1024
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"


def recovery_fixture(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    task_id = "recovery-safety-task"
    prompt = configured_project_prompt(base, head, task_id)
    response = envelope(task_id, prompt=prompt)
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    worktree = watcher._owned_task_worktree(task_id, prompt)
    prompt_path = watcher.prompts_dir / f"{task_id}.txt"
    atomic_write(prompt_path, prompt.encode("utf-8"))
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "taskId": task_id, "nextTaskId": task_id,
        "nextPromptPath": str(prompt_path), "targetWorktree": str(worktree),
        "codexPid": None, "executorResultPath": str(tmp_path / "missing-result"),
        "lastCompletedTaskId": "completed-task", "architectResultFingerprint": fingerprint,
        "consumedArchitectResponses": {fingerprint: {"taskId": task_id, "action": "EXECUTE", "state": "RECEIVED"}},
    })
    watcher.save()
    return watcher, base, worktree


def postlaunch_recovery_fixture(tmp_path):
    watcher, base, worktree = recovery_fixture(tmp_path)
    watcher.state.update({"executorLaunchState": "POSTLAUNCH_NO_RESULT", "automaticRetryAuthorized": False})
    watcher.save()
    return watcher, base, worktree


def test_alive_recovery_never_contacts_architect(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": __import__("os").getpid()})
    watcher.save()
    assert watcher.reconcile_executor() == "EXECUTOR_RUNNING"


def test_exit_report_is_ready_and_missing_report_crashes_without_rerun(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    report = tmp_path / "report.txt"
    report.write_text("done", encoding="utf-8")
    watcher.state["taskId"] = "task-1"
    assert watcher.mark_executor_exit(0, report) == "RESULT_READY"
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "task-2"})
    assert watcher.mark_executor_exit(0, tmp_path / "missing") == "EXECUTOR_CRASHED"


def test_restart_completed_result_does_not_launch_duplicate(tmp_path):
    watcher = ready(tmp_path)
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.state["state"] == "RESULT_READY"


def test_send_failure_keeps_same_result_ready(tmp_path):
    watcher = ready(tmp_path)
    class Bridge:
        def submit_result_bounded(self, value):
            raise RuntimeError("offline")
    with pytest.raises(RuntimeError):
        watcher.deliver_result(Bridge())
    assert LocalFirstOrchestrator(str(tmp_path), watcher.state_dir).state["state"] == "RESULT_READY"


def test_final_envelope_persists_exact_prompt_and_duplicate_is_ignored(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    prompt = configured_project_prompt(base, head).replace("bounded task in owned-task", "line 1\r\nline 2")
    response = envelope("recovery-safety-task", prompt=prompt)
    decision = watcher.accept_architect_response(response)
    assert decision["action"] == "EXECUTE"
    assert "line 1\nline 2" in Path(watcher.state["nextPromptPath"]).read_text(encoding="utf-8")
    assert watcher.accept_architect_response(response)["action"] == "DUPLICATE"


def test_architect_execute_stages_next_sequential_task_without_launching(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "000021", "taskSequence": 21})
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    response = envelope("000021", prompt=configured_project_prompt(base, head, "next-000022"))
    launch_calls = []
    decision = watcher.accept_architect_response(response)
    assert decision["action"] == "EXECUTE"
    assert watcher.state["nextTaskId"] == "000022"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert launch_calls == []
    watcher.launch_next(lambda *_: launch_calls.append(1) or type("Process", (), {"pid": 22022})())
    assert launch_calls == [1]


def test_prelaunch_recovery_only_restores_next_prompt_ready(tmp_path, monkeypatch):
    base, config, head = configured_git_project(tmp_path)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    task_id = "recovery-only"
    prompt = configured_project_prompt(base, head, task_id)
    worktree = watcher._owned_task_worktree(task_id, prompt)
    prompt_path = watcher.prompts_dir / f"{task_id}.txt"
    atomic_write(prompt_path, prompt.encode("utf-8"))
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": task_id, "nextTaskId": task_id,
                          "nextPromptPath": str(prompt_path), "targetWorktree": str(worktree),
                          "executorResultPath": str(tmp_path / "missing-result"), "codexPid": None,
                          "lastCompletedTaskId": "completed"})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    calls = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: calls.append(1)) is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert calls == []


def test_exhausted_result_transport_cleanup_accepts_null_bridge(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"architectSendState": "PENDING", "architectTransportRecoveryCount": 2})
    watcher.save()
    assert watcher.deliver_result_with_recovery(lambda: None, max_attempts=1, initial_bridge=None) is None
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"


@pytest.mark.parametrize("action,state", [("HUMAN_REQUIRED", "HUMAN_REQUIRED"), ("STOP", "IDLE")])
def test_terminal_actions_launch_nothing(tmp_path, action, state):
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", action=action))
    assert watcher.state["state"] == state
    assert watcher.launch_next(lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) is None


def test_malformed_or_wrong_task_envelope_launches_nothing(tmp_path):
    with pytest.raises(ValueError):
        parse_orchestrator_result("ordinary response", "task-1")
    with pytest.raises(ValueError):
        parse_orchestrator_result(envelope("other"), "task-1")


class RecoveryBridge:
    def __init__(self):
        self.messages = []

    def submit_result_bounded(self, value):
        self.messages.append(value)

    def assistant_baseline(self):
        return {"count": 2, "text_hash": "baseline", "entries": []}


def test_invalid_architect_envelope_gets_one_same_task_format_recovery(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    bridge = RecoveryBridge()
    watcher.request_format_recovery(bridge)
    assert len(bridge.messages) == 1
    assert "taskId=task-1" in bridge.messages[0]
    assert bridge.messages[0] != "executor report"
    assert watcher.state["formatRecoveryCount"] == 1
    assert watcher.state["state"] == "ARCHITECT_RUNNING"


def test_format_recovery_is_not_repeated_and_falls_back_to_human(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": "task-1"})
    bridge = RecoveryBridge()
    watcher.request_format_recovery(bridge)
    assert bridge.messages == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"


def test_format_recovery_is_scoped_to_current_task(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "task-B", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": "task-A", "formatRecoveryExhausted": True})
    bridge = RecoveryBridge()
    watcher.request_format_recovery(bridge)
    assert len(bridge.messages) == 1
    assert watcher.state["architectFormatRecoveryTaskId"] == "task-B"
    assert watcher.state["formatRecoveryCount"] == 1
    assert watcher.state["formatRecoveryExhausted"] is False


def test_successful_executor_result_clears_stale_failure_state(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    report = tmp_path / "report.txt"
    report.write_text("completed", encoding="utf-8")
    watcher.state.update({"taskId": "task-C", "executorFailureClass": "POSTLAUNCH_NO_RESULT", "executorProcessState": "EXITED_WITHOUT_RESULT", "executorCrash": {"old": True}})
    assert watcher.mark_executor_exit(0, report) == "RESULT_READY"
    assert watcher.state["lastCompletedTaskId"] == "task-C"
    assert watcher.state["executorProcessState"] == "COMPLETED_WITH_RESULT"
    assert watcher.state["executorFailureClass"] is None
    assert watcher.state["executorCrash"] is None


def test_three_task_resident_lifecycle_does_not_leak_recovery_or_rerun(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    bridge = RecoveryBridge()
    for task_id in ("task-A", "task-B", "task-C"):
        report = tmp_path / f"{task_id}.txt"
        report.write_text(f"{task_id} complete", encoding="utf-8")
        watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": task_id, "codexPid": None})
        assert watcher.mark_executor_exit(0, report) == "RESULT_READY"
        assert watcher.state["lastCompletedTaskId"] == task_id
        assert watcher.state["executorProcessState"] == "COMPLETED_WITH_RESULT"
        if task_id == "task-B":
            watcher.state["state"] = "ARCHITECT_RUNNING"
            watcher.request_format_recovery(bridge)
            assert watcher.state["architectFormatRecoveryTaskId"] == task_id
            assert watcher.accept_architect_response(envelope(task_id, action="STOP"))["action"] == "STOP"
        elif task_id == "task-C":
            assert watcher.state["formatRecoveryCount"] == 0
            assert watcher.state["formatRecoveryExhausted"] is False
    assert len(bridge.messages) == 1


def test_main_loop_value_error_normalizes_cross_task_recovery_first(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "task-B", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": "task-A", "formatRecoveryExhausted": True})
    bridge = RecoveryBridge()
    assert handle_architect_value_error(watcher, bridge) is True
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state["architectFormatRecoveryTaskId"] == "task-B"
    assert watcher.state["formatRecoveryCount"] == 1
    assert watcher.state["formatRecoveryExhausted"] is False


def test_stale_format_human_required_reenters_architect_without_executor_launch(tmp_path, monkeypatch):
    watcher = ready(tmp_path)
    task_id = "task-000016"
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "taskId": task_id, "architectSendState": "CONFIRMED",
        "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": "old-task",
        "formatRecoveryExhausted": True, "humanRequiredReason": "",
        "codexPid": None,
    })
    watcher.save()
    assert run_human_required_startup_once(watcher, lambda *_: (_ for _ in ()).throw(AssertionError("stale recovery must not launch executor"))) == "ARCHITECT_RUNNING"
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state["formatRecoveryCount"] == 0
    assert watcher.state["architectFormatRecoveryTaskId"] is None
    assert watcher.state["executorResultPath"]


def test_human_required_restart_rechecks_fresh_postlaunch_authorization(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    reason = "EXECUTOR_EXITED_WITHOUT_RESULT"
    watcher.state.update({
        "humanRequiredReason": reason,
        "humanRequiredRecoveryAttemptedReason": reason,
        "executorLaunchState": "POSTLAUNCH_NO_RESULT",
    })
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", watcher.state["taskId"])
    assert watcher_module.human_required_startup_recovery_due(
        watcher, reason, reason
    ) is True


def test_human_required_restart_without_authorization_remains_passive(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    reason = "EXECUTOR_EXITED_WITHOUT_RESULT"
    watcher.state.update({
        "humanRequiredReason": reason,
        "humanRequiredRecoveryAttemptedReason": reason,
        "executorLaunchState": "POSTLAUNCH_NO_RESULT",
    })
    monkeypatch.delenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", raising=False)
    assert watcher_module.human_required_startup_recovery_due(
        watcher, reason, reason
    ) is False


def test_human_required_restart_wrong_task_authorization_fails_closed(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    reason = "EXECUTOR_EXITED_WITHOUT_RESULT"
    watcher.state.update({
        "humanRequiredReason": reason,
        "humanRequiredRecoveryAttemptedReason": reason,
        "executorLaunchState": "POSTLAUNCH_NO_RESULT",
    })
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", "wrong-task")
    assert watcher_module.human_required_startup_recovery_due(
        watcher, reason, reason
    ) is False


def test_human_required_restart_same_task_consumed_authorization_reaches_one_shot_gate(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    reason = "EXECUTOR_EXITED_WITHOUT_RESULT"
    watcher.state.update({
        "humanRequiredReason": reason,
        "humanRequiredRecoveryAttemptedReason": reason,
        "executorLaunchState": "POSTLAUNCH_NO_RESULT",
        "humanRecoveryAuthorizationConsumed": True,
        "humanRecoveryAuthorizedTaskId": task_id,
        "codexPid": 4402,
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    assert watcher_module.human_required_startup_recovery_due(
        watcher, reason, reason
    ) is True
    launches = []
    assert run_human_required_startup_once(watcher, lambda *_: launches.append(1)) == "STOP"
    assert launches == []
    assert watcher.state["humanRecoveryAuthorizationError"] == "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED"


def test_000016_startup_shape_consumes_existing_response_once(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "taskId": "000016", "architectSendState": "CONFIRMED",
        "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96",
        "formatRecoveryExhausted": True, "humanRequiredReason": "", "codexPid": None,
    })
    Path(watcher.state["executorResultPath"]).parent.mkdir(parents=True, exist_ok=True)
    Path(watcher.state["executorResultPath"]).write_text("000016 completed", encoding="utf-8")
    watcher.save()
    assert run_human_required_startup_once(watcher, lambda *_: (_ for _ in ()).throw(AssertionError("stale recovery must not launch executor"))) == "ARCHITECT_RUNNING"
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    response = envelope("000016", prompt=configured_project_prompt(base, head, "next-000016"))
    launches = []
    process = type("Process", (), {"pid": 17016})()
    assert watcher.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1]) == "EXECUTE"
    assert launches == []
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    watcher.launch_next(lambda *_: (launches.append(1), process)[1])
    assert launches == [1]
    assert watcher.state["state"] == "EXECUTOR_RUNNING"


def test_format_recovery_response_uses_strict_same_task_parser():
    parsed = parse_orchestrator_result(envelope("task-1", prompt="next"), "task-1")
    assert parsed == {"classification": "ACCEPTED", "action": "EXECUTE", "taskId": "task-1", "prompt": "next", "documentation": "NOT_REQUIRED"}


def test_atomic_write_and_github_free_evidence(tmp_path):
    path = tmp_path / "work" / "state.json"
    atomic_write(path, b"{}")
    assert json.loads(path.read_text()) == {}
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", action="STOP"))
    assert "relay" not in json.dumps(watcher.state).lower()


def test_atomic_state_write_flushes_and_preserves_complete_previous_state_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "work" / "state.json"
    atomic_write(path, b'{"state":"PREVIOUS","transaction":"tx-1"}\n')
    fsync_calls = []
    original_fsync = watcher_module.os.fsync
    monkeypatch.setattr(watcher_module.os, "fsync", lambda descriptor: (fsync_calls.append(descriptor), original_fsync(descriptor))[1])
    original_replace = watcher_module.os.replace
    def fail_replace(*_args):
        raise OSError("simulated power loss before replacement")
    monkeypatch.setattr(watcher_module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write(path, b'{"state":"NEW","transaction":"tx-2"}\n')
    assert fsync_calls
    assert json.loads(path.read_text(encoding="utf-8")) == {"state": "PREVIOUS", "transaction": "tx-1"}
    monkeypatch.setattr(watcher_module.os, "replace", original_replace)
    assert json.loads(path.read_text(encoding="utf-8")) == {"state": "PREVIOUS", "transaction": "tx-1"}


def test_stale_state_temp_does_not_override_canonical_state(tmp_path):
    state_dir = tmp_path / "work"
    state_dir.mkdir(parents=True)
    canonical = state_dir / "state.json"
    canonical.write_text(json.dumps({"state": "NEXT_PROMPT_READY", "nextTaskId": "000080", "rolloverTransactionId": "tx-canonical"}), encoding="utf-8")
    (state_dir / f".state.json.{os.getpid()}.tmp").write_text(json.dumps({"state": "RESULT_READY", "nextTaskId": "000081", "rolloverTransactionId": "tx-stale"}), encoding="utf-8")
    watcher = LocalFirstOrchestrator(str(tmp_path), state_dir)
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000080"
    assert watcher.state["rolloverTransactionId"] == "tx-canonical"


def test_restart_recovers_delivered_pending_transaction_without_resend(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    transaction = "crash-delivered-000080"
    watcher.state.update({
        "taskId": "000079", "lastCompletedTaskId": "000079", "nextTaskId": "000080",
        "nextPromptPath": str(prompt), "rolloverDue": True, "rolloverPending": True,
        "rolloverInProgress": True, "handoverRequested": True, "handoverReady": False,
        "rolloverHandoverSendState": "PENDING", "rolloverMaintenanceState": "IN_PROGRESS",
        "rolloverTransactionId": transaction, "rolloverTransactionGeneration": 2,
        "rolloverTransactionTaskId": "000080", "rolloverAttemptedForTaskId": "000080",
        "architectConversationId": "OLD", "executorProcessState": "COMPLETED_WITH_RESULT",
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    sends = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def exact_user_message_payload_observed(self, payload):
            return (f"transactionId={transaction}" in payload
                    and "taskId=000080" in payload
                    and watcher_module.HANDOVER_OPEN in payload
                    and watcher_module.HANDOVER_CLOSE in payload)
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def generation_visible(self): return False
        def _assistant_entries(self): return []
        def submit_result_bounded(self, _payload): sends.append(1)
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    request_calls = []
    monkeypatch.setattr(restarted.session_rollover, "request_if_due", lambda *_args, **_kwargs: request_calls.append(1) or (_ for _ in ()).throw(AssertionError("restart must not resend")))
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert restarted.state["rolloverTransactionId"] == transaction
    assert restarted.state["rolloverTransactionGeneration"] == 2
    assert restarted.state["rolloverHandoverSendState"] == "ACKNOWLEDGED"
    assert restarted.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert sends == [] and request_calls == []


def test_restart_with_cdp_unavailable_preserves_transaction_and_fails_closed(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    transaction = "cdp-unavailable-000080"
    watcher.state.update({
        "taskId": "000079", "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": True,
        "handoverRequested": True, "rolloverHandoverSendState": "ACKNOWLEDGED",
        "rolloverMaintenanceState": "RECONCILE_PENDING", "rolloverTransactionId": transaction,
        "rolloverTransactionGeneration": 2, "rolloverTransactionTaskId": "000080",
        "rolloverAttemptedForTaskId": "000080", "architectConversationId": "OLD",
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: (_ for _ in ()).throw(RuntimeError("CDP_UNAVAILABLE"))))
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert restarted.state["rolloverTransactionId"] == transaction
    assert restarted.state["rolloverTransactionGeneration"] == 2
    assert restarted.state["nextTaskId"] == "000080"
    assert restarted.state.get("rolloverRetiredTransactionId") is None


def test_three_fresh_restarts_keep_unresolved_transaction_identity_stable(tmp_path):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    transaction = "stable-restart-000080"
    watcher.state.update({
        "taskId": "000079", "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": True,
        "handoverRequested": True, "rolloverHandoverSendState": "ACKNOWLEDGED",
        "rolloverMaintenanceState": "RECONCILE_PENDING", "rolloverTransactionId": transaction,
        "rolloverTransactionGeneration": 2, "rolloverTransactionTaskId": "000080",
        "rolloverAttemptedForTaskId": "000080", "architectConversationId": "OLD",
    })
    watcher.save()
    for _ in range(3):
        fresh = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
        assert fresh.state["rolloverTransactionId"] == transaction
        assert fresh.state["rolloverTransactionGeneration"] == 2
        assert fresh.state["rolloverTransactionTaskId"] == "000080"
        assert fresh.state["nextTaskId"] == "000080"


class FakeComposer:
    def __init__(self, page):
        self.page = page
        self.last = self

    def is_visible(self, **_):
        return True

    def is_editable(self, **_):
        return True

    def scroll_into_view_if_needed(self, **_):
        return None

    def focus(self, **_):
        if not self.page.focus:
            raise TimeoutError("composer focus blocked")

    def press(self, key, **_):
        if key == "ControlOrMeta+A":
            self.page.content = ""
        elif key == "Enter":
            self.page.submit()

    def inner_text(self, **_):
        return self.page.content


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        assert key == "ControlOrMeta+A"
        self.page.content = ""

    def insert_text(self, value):
        self.page.content = value


class FakeButton:
    def __init__(self, page):
        self.page = page
        self.last = self

    def is_visible(self, **_):
        return True

    def is_enabled(self, **_):
        return self.page.logical_enabled

    def click(self, **_):
        if not self.page.click_actionable:
            raise TimeoutError("send button actionability blocked")
        self.page.submit()

    def scroll_into_view_if_needed(self, **_):
        return None


class FakeStopButton:
    last = None

    def __init__(self):
        self.last = self

    def count(self):
        return 0

    def is_visible(self, **_):
        return False


class FakeComposerPage:
    def __init__(self, *, available=True, focus=True, transition=True, logical_enabled=True, click_actionable=True):
        self.available = available
        self.focus = focus
        self.transition = transition
        self.logical_enabled = logical_enabled
        self.click_actionable = click_actionable
        self.content = ""
        self.sent = []
        self.native_submissions = 0
        self.keyboard = FakeKeyboard(self)

    def get_by_role(self, role, **_):
        if role == "textbox":
            if not self.available:
                return FakeUnavailable()
            return FakeComposer(self)
        if role == "button" and "stop" in str(_.get("name", "")).lower():
            return FakeStopButton()
        return FakeButton(self)

    def locator(self, selector):
        class Count:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 0

        return Count(self)

    def submit(self):
        self.native_submissions += 1
        if self.transition:
            self.sent.append(self.content)
            self.content = ""


class FakeUnavailable:
    def __init__(self):
        self.last = self

    def is_visible(self, **_):
        raise RuntimeError("missing composer")

    def is_editable(self, **_):
        raise RuntimeError("missing composer")


class ReplacingComposerPage(FakeComposerPage):
    def __init__(self):
        super().__init__()
        self.composer_calls = 0

    def get_by_role(self, role, **kwargs):
        if role == "textbox":
            self.composer_calls += 1
            if self.composer_calls == 1:
                stale = FakeComposer(self)
                stale.focus = lambda **_: (_ for _ in ()).throw(TimeoutError("detached"))
                return stale
        return super().get_by_role(role, **kwargs)


def test_current_composer_receives_exact_text_and_is_confirmed():
    page = FakeComposerPage()
    result = "line 1\nline 2\n<ORCHESTRATOR_RESULT>"
    ArchitectPlaywright(page).submit_result_bounded(result, timeout=1)
    assert page.sent == [result]


def test_transient_population_timeout_retries_with_fresh_composer_and_sends_once():
    class RetryPage(FakeComposerPage):
        def __init__(self):
            super().__init__()
            self.composer_calls = 0
            self.composers = []

        def get_by_role(self, role, **kwargs):
            if role == "textbox":
                self.composer_calls += 1
                composer = FakeComposer(self)
                if self.composer_calls == 2:
                    composer.focus = lambda **_: (_ for _ in ()).throw(TimeoutError("transient focus"))
                self.composers.append(composer)
                return composer
            return super().get_by_role(role, **kwargs)

    page = RetryPage()
    bridge = ArchitectPlaywright(page)
    result = "fresh handover payload"
    bridge.submit_result_bounded(result, timeout=1)
    assert page.sent == [result]
    assert page.composer_calls >= 5
    assert len({id(composer) for composer in page.composers}) == len(page.composers)
    assert bridge.sendActionAttempted is True


def test_real_playwright_timeout_is_transient_and_reacquires_composer():
    class PlaywrightRetryPage(FakeComposerPage):
        def __init__(self):
            super().__init__()
            self.composer_calls = 0
            self.composers = []
            self.bridge = None

        def get_by_role(self, role, **kwargs):
            if role == "textbox":
                self.composer_calls += 1
                composer = FakeComposer(self)
                if self.composer_calls == 2:
                    def fail(**_):
                        assert self.bridge.sendActionAttempted is False
                        raise PlaywrightTimeoutError("transient focus")
                    composer.focus = fail
                self.composers.append(composer)
                return composer
            return super().get_by_role(role, **kwargs)

    page = PlaywrightRetryPage()
    bridge = ArchitectPlaywright(page)
    page.bridge = bridge
    bridge.submit_result_bounded("playwright timeout payload", timeout=1)
    assert page.sent == ["playwright timeout payload"]
    assert page.composer_calls >= 5
    assert len({id(composer) for composer in page.composers}) == len(page.composers)


def test_non_timeout_population_exception_fails_closed_as_input_rejected():
    class RejectingPage(FakeComposerPage):
        def get_by_role(self, role, **kwargs):
            if role == "textbox":
                composer = FakeComposer(self)
                composer.focus = lambda **_: (_ for _ in ()).throw(ValueError("not transient"))
                return composer
            return super().get_by_role(role, **kwargs)

    with pytest.raises(ResultSubmissionError) as error:
        ArchitectPlaywright(RejectingPage()).submit_result_bounded("rejected payload", timeout=1)
    assert error.value.code == "ARCHITECT_COMPOSER_INPUT_REJECTED"


def test_population_timeout_does_not_mark_send_attempted_before_retry_succeeds():
    class ObservingPage(FakeComposerPage):
        def __init__(self):
            super().__init__()
            self.bridge = None
            self.calls = 0

        def get_by_role(self, role, **kwargs):
            if role == "textbox":
                self.calls += 1
                composer = FakeComposer(self)
                if self.calls == 2:
                    def fail(**_):
                        assert self.bridge.sendActionAttempted is False
                        raise TimeoutError("transient focus")
                    composer.focus = fail
                return composer
            return super().get_by_role(role, **kwargs)

    page = ObservingPage()
    bridge = ArchitectPlaywright(page)
    page.bridge = bridge
    bridge.submit_result_bounded("safe payload", timeout=1)
    assert page.sent == ["safe payload"]


def test_partial_population_is_cleared_before_retry_without_duplicate_text():
    class PartialKeyboard(FakeKeyboard):
        def __init__(self, page):
            super().__init__(page)
            self.calls = 0

        def insert_text(self, value):
            self.calls += 1
            if self.calls == 1:
                self.page.content = value[:7]
                raise TimeoutError("input interrupted")
            super().insert_text(value)

    page = FakeComposerPage()
    page.keyboard = PartialKeyboard(page)
    ArchitectPlaywright(page).submit_result_bounded("handover payload", timeout=1)
    assert page.sent == ["handover payload"]


def test_repeated_population_timeout_preserves_bounded_failure_code():
    page = FakeComposerPage()
    page.focus = False
    with pytest.raises(ResultSubmissionError) as error:
        ArchitectPlaywright(page).submit_result_bounded("never populated", timeout=0.1)
    assert error.value.code == "ARCHITECT_COMPOSER_POPULATE_OPERATION_TIMEOUT"


def test_hidden_prompt_textarea_does_not_override_visible_semantic_composer():
    page = FakeComposerPage()

    class Hidden:
        last = None
        def count(self): return 1
        def is_visible(self, **_): return False
        def is_editable(self, **_): return False

    page.locator = lambda _selector: Hidden()
    result = "semantic live payload"
    ArchitectPlaywright(page).submit_result_bounded(result, timeout=1)
    assert page.sent == [result]


class _InputVerificationRerenderPage(FakeComposerPage):
    def __init__(self):
        super().__init__()
        self.verification_reads = 0

    def get_by_role(self, role, **kwargs):
        if role == "textbox":
            page = self
            class RerenderedComposer(FakeComposer):
                def inner_text(self, **_):
                    page.verification_reads += 1
                    if page.verification_reads == 2:
                        raise TimeoutError("DOM rerender")
                    return super().inner_text(**_)
            return RerenderedComposer(self)
        return super().get_by_role(role, **kwargs)


def test_composer_rerender_between_input_and_verification_reacquires_current_editor():
    page = _InputVerificationRerenderPage()
    result = "rerendered payload"
    ArchitectPlaywright(page).submit_result_bounded(result, timeout=1)
    assert page.sent == [result]


def test_unsent_known_payload_is_cleared_after_pre_send_input_failure():
    page = FakeComposerPage()
    original_inner_text = FakeComposer.inner_text
    reads = {"count": 0}

    def failing_once(composer, **kwargs):
        reads["count"] += 1
        if reads["count"] == 1:
            raise TimeoutError("acceptance unavailable")
        return original_inner_text(composer, **kwargs)

    FakeComposer.inner_text = failing_once
    try:
        with pytest.raises(ResultSubmissionError) as error:
            ArchitectPlaywright(page).submit_result_bounded("known unsent payload", timeout=1)
        assert error.value.code == "ARCHITECT_COMPOSER_INPUT_ACCEPTANCE_TIMEOUT"
        assert page.content == ""
        assert page.sent == []
    finally:
        FakeComposer.inner_text = original_inner_text


def test_stale_composer_is_reacquired_before_focus_and_send():
    page = ReplacingComposerPage()
    result = "reacquired payload"
    ArchitectPlaywright(page).submit_result_bounded(result, timeout=1)
    assert page.sent == [result]
    assert page.composer_calls >= 3


def test_result_delivery_pre_send_failure_is_persisted_and_restart_retries_without_executor(tmp_path):
    watcher = ready(tmp_path)
    payload = Path(watcher.state["executorResultPath"]).read_bytes()
    class FailedBridge:
        def assistant_baseline(self): return {"count": 2, "entries": []}
        def submit_result_bounded(self, _message):
            raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", "TimeoutError")
    with pytest.raises(ResultSubmissionError):
        watcher.deliver_result(FailedBridge())
        assert watcher.state["state"] == "RESULT_READY"
    assert watcher.state["architectDeliveryFailureClass"] == "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"
    assert Path(watcher.state["executorResultPath"]).read_bytes() == payload
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    sends = []
    class HealthyBridge:
        def assistant_baseline(self): return {"count": 3, "entries": []}
        def submit_result_bounded(self, message): sends.append(message)
        def latest_user_message(self): return sends[-1] if sends else None
    restarted.deliver_result(HealthyBridge())
    assert len(sends) == 1
    assert not restarted.state.get("executorLaunchCount", 0)


def test_ambiguous_result_delivery_does_not_duplicate_on_restart(tmp_path):
    watcher = ready(tmp_path)
    sent = []
    class AmbiguousBridge:
        def assistant_baseline(self): return {"count": 4, "entries": ["baseline"]}
        def submit_result_bounded(self, message):
            sent.append(message)
            raise ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT")
    with pytest.raises(ResultSubmissionError):
        watcher.deliver_result(AmbiguousBridge())
    payload = sent[0]
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    class PresentBridge:
        def latest_user_message(self): return payload
        def assistant_baseline(self): return {"count": 9, "entries": ["new"]}
        def submit_result_bounded(self, _message): raise AssertionError("duplicate delivery")
    restarted.deliver_result(PresentBridge())
    assert restarted.state["state"] == "ARCHITECT_RUNNING"
    assert sent == [payload]


def test_ambiguous_result_delivery_stays_blocked_without_positive_non_delivery(tmp_path):
    watcher = ready(tmp_path)
    class AmbiguousBridge:
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def submit_result_bounded(self, _message): raise ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT")
    with pytest.raises(ResultSubmissionError):
        watcher.deliver_result(AmbiguousBridge())
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    sends = []
    class AbsentBridge:
        def latest_user_message(self): return "different payload"
        def assistant_baseline(self): return {"count": 2, "entries": []}
        def submit_result_bounded(self, message): sends.append(message)
    with pytest.raises(ResultSubmissionError):
        restarted.deliver_result(AbsentBridge())
    assert len(sends) == 0
    assert restarted.state["state"] == "RESULT_READY"


def test_confirmed_identical_result_delivery_is_terminally_idempotent(tmp_path):
    watcher = ready(tmp_path)
    sent = []
    class Bridge:
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def submit_result_bounded(self, message): sent.append(message)
        def latest_user_message(self): return sent[-1] if sent else None
    watcher.deliver_result(Bridge())
    watcher.state["state"] = "RESULT_READY"
    watcher.save()
    class DuplicateBridge:
        def latest_user_message(self): return sent[0]
        def submit_result_bounded(self, _message): raise AssertionError("confirmed result resent")
    watcher.deliver_result(DuplicateBridge())
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert len(sent) == 1


def test_localfirst_has_governed_memory_rollover_at_safe_boundary(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000002.txt"
    prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt), "architectResponseCount": 30})
    watcher.session_rollover.sample_memory(lambda: watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES)
    messages = []
    class Bridge:
        def submit_result_bounded(self, message): messages.append(message)
    assert watcher.session_rollover.request_if_due(Bridge(), True, False, architect_generating=False, safe_boundary_state="NEXT_PROMPT_READY") is True
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["handoverRequested"] is True
    assert len(messages) == 1


class _RolloverPage:
    def __init__(self, urls):
        self.urls = iter(urls)
        self.closed = False

    @property
    def url(self):
        return next(self.urls)

    def close(self):
        self.closed = True

    def evaluate(self, script):
        if "stop-button" in script:
            return False
        return [{"id": "ack", "text": "ARCHITECT_SESSION_READY"}]


class _AckPage(_RolloverPage):
    def __init__(self, url, entries=None, generating=False):
        super().__init__([url])
        self._url = url
        self.entries = entries or []
        self.generating = generating

    @property
    def url(self):
        return self._url

    def evaluate(self, script):
        if "stop-button" in script:
            return self.generating
        return self.entries


class _ChangingAckPage:
    def __init__(self, urls, entries=None, generating=False):
        self.urls = iter(urls)
        self.entries = entries or [{"id": "ack", "text": "ARCHITECT_SESSION_READY"}]
        self.generating = generating
        self.closed = False

    @property
    def url(self):
        return next(self.urls)

    def evaluate(self, script):
        if "stop-button" in script:
            return self.generating
        return self.entries

    def close(self):
        self.closed = True


def test_architect_conversation_identity_equivalence_is_exact():
    identity = "7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    assert watcher_module.architect_conversation_ids_equivalent("WEB:" + identity, identity)
    assert watcher_module.architect_conversation_ids_equivalent(identity, "WEB:" + identity)
    assert not watcher_module.architect_conversation_ids_equivalent("WEB:" + identity, "different-uuid")


def _fake_attach_runtime(pages):
    class Browser:
        contexts = [type("Context", (), {"pages": pages})()]

    class Chromium:
        def connect_over_cdp(self, _endpoint, timeout): return Browser()

    class Runtime:
        chromium = Chromium()
        def stop(self): self.stopped = True

    class Factory:
        def start(self): return Runtime()
    return Factory()


def test_attach_resolves_both_compatible_identity_representations(monkeypatch):
    from playwright import sync_api
    identity = "7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    page = _AckPage("https://chatgpt.com/c/" + identity)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime([page]))
    assert ArchitectPlaywright.attach("http://127.0.0.1:9333", "WEB:" + identity).page is page
    reverse = _AckPage("https://chatgpt.com/c/WEB:" + identity)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime([reverse]))
    assert ArchitectPlaywright.attach("http://127.0.0.1:9333", identity).page is reverse


def test_attach_rejects_unrelated_conversation_page(monkeypatch):
    from playwright import sync_api
    page = _AckPage("https://chatgpt.com/c/11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime([page]))
    with pytest.raises(RuntimeError, match="ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"):
        ArchitectPlaywright.attach("http://127.0.0.1:9333", "WEB:22222222-2222-2222-2222-222222222222")


def _legacy_identity_fixture(tmp_path, state="RESULT_READY"):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "000040.txt"
    result.write_text("captured task 000040 result", encoding="utf-8")
    provisional = "WEB:7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    watcher.state.update({
        "state": state, "architectConversationId": provisional, "taskId": "000040",
        "lastCompletedTaskId": "000040", "executorResultPath": str(result),
        "codexPid": None, "rolloverPending": False, "handoverRequested": False,
        "architectSendState": "FAILED", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_PRE_SEND_FAILURE",
    })
    watcher.save()
    return watcher, provisional, result


def test_legacy_provisional_identity_recovers_one_handover_page_and_preserves_result(tmp_path, monkeypatch):
    from playwright import sync_api
    watcher, provisional, result = _legacy_identity_fixture(tmp_path)
    canonical = "6aa6d480-f628-83ec-a617-51fbea5a592a"
    page = _AckPage("https://chatgpt.com/c/" + canonical, [{"id": "ack", "text": "handover\nARCHITECT_HANDOVER_READY"}])
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime([page]))
    bridge = watcher_module.attach_legacy_provisional_architect("http://127.0.0.1:9333", provisional, watcher)
    assert bridge.page is page
    assert watcher.state["architectConversationId"] == canonical
    assert watcher.state["taskId"] == watcher.state["lastCompletedTaskId"] == "000040"
    assert watcher.state["executorResultPath"] == str(result)
    assert watcher.state["architectDeliveryFailureClass"] == "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"
    bridge.close()


def test_legacy_provisional_identity_caller_adopts_persisted_id_before_canonicalization(tmp_path, monkeypatch):
    from playwright import sync_api
    watcher, provisional, result = _legacy_identity_fixture(tmp_path)
    watcher.state.update({"rolloverPending": True, "rolloverTrigger": "MEMORY_THRESHOLD"})
    watcher.save()
    canonical = "6aa6d480-f628-83ec-a617-51fbea5a592a"
    page = _AckPage("https://chatgpt.com/c/" + canonical, [{"id": "ack", "text": "handover\nARCHITECT_HANDOVER_READY"}])
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime([page]))
    attach_calls = []

    def unavailable_attach(_endpoint, _conversation_id):
        attach_calls.append(_conversation_id)
        raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")

    monkeypatch.setattr(ArchitectPlaywright, "attach", staticmethod(unavailable_attach))
    conversation_id = watcher.state["architectConversationId"]
    bridge = None
    legacy_recovery_used = False
    try:
        try:
            bridge = ArchitectPlaywright.attach("http://127.0.0.1:9333", conversation_id)
        except Exception as error:
            if str(error) == "ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND":
                bridge = watcher_module.attach_legacy_provisional_architect("http://127.0.0.1:9333", conversation_id, watcher)
                legacy_recovery_used = True
            else:
                raise
        if legacy_recovery_used:
            conversation_id = watcher.state["architectConversationId"]
        conversation_id = watcher_module.canonicalize_attached_architect_conversation(watcher, bridge, conversation_id)
        assert conversation_id == canonical
        assert bridge.page is page
        assert attach_calls == [provisional]
        assert watcher.state["taskId"] == watcher.state["lastCompletedTaskId"] == "000040"
        assert watcher.state["executorResultPath"] == str(result)
        assert watcher.state["architectSendState"] == "FAILED"
        assert watcher.state["architectDeliveryFailureClass"] == "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"
        assert watcher.state["rolloverPending"] is False
        assert "rolloverTrigger" not in watcher.state
    finally:
        if bridge is not None:
            bridge.close()


@pytest.mark.parametrize("pages", [[], [
    _AckPage("https://chatgpt.com/c/11111111-1111-1111-1111-111111111111", [{"id": "a", "text": "ARCHITECT_HANDOVER_READY"}]),
    _AckPage("https://chatgpt.com/c/22222222-2222-2222-2222-222222222222", [{"id": "b", "text": "ARCHITECT_HANDOVER_READY"}]),
]])
def test_legacy_provisional_identity_requires_exactly_one_candidate(tmp_path, monkeypatch, pages):
    from playwright import sync_api
    watcher, provisional, _result = _legacy_identity_fixture(tmp_path)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime(pages))
    with pytest.raises(RuntimeError, match="ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"):
        watcher_module.attach_legacy_provisional_architect("http://127.0.0.1:9333", provisional, watcher)
    assert watcher.state["architectConversationId"] == provisional


def test_legacy_provisional_identity_rejects_new_chat_non_web_idle_and_active_executor(tmp_path, monkeypatch):
    from playwright import sync_api
    watcher, provisional, _result = _legacy_identity_fixture(tmp_path)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: (_ for _ in ()).throw(AssertionError("fallback must not attach")))
    watcher.state["architectConversationId"] = "canonical-id"
    assert not watcher_module.legacy_provisional_architect_recovery_allowed(watcher, "canonical-id")
    watcher.state["architectConversationId"] = provisional
    watcher.state["state"] = "IDLE"
    assert not watcher_module.legacy_provisional_architect_recovery_allowed(watcher, provisional)
    watcher.state["state"] = "RESULT_READY"
    watcher.state["codexPid"] = 1234
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    assert not watcher_module.legacy_provisional_architect_recovery_allowed(watcher, provisional)
    watcher.state["codexPid"] = None
    watcher.state["handoverRequested"] = True
    assert not watcher_module.legacy_provisional_architect_recovery_allowed(watcher, provisional)


def test_legacy_provisional_identity_rejects_blank_or_unmarked_page(tmp_path, monkeypatch):
    from playwright import sync_api
    watcher, provisional, _result = _legacy_identity_fixture(tmp_path)
    page = _AckPage("https://chatgpt.com/", [{"id": "a", "text": "ARCHITECT_HANDOVER_READY"}])
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _fake_attach_runtime([page]))
    with pytest.raises(RuntimeError, match="ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"):
        watcher_module.attach_legacy_provisional_architect("http://127.0.0.1:9333", provisional, watcher)


def test_rollover_persists_final_canonical_url_identity(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    provisional = "7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _ChangingAckPage(["https://chatgpt.com/c/WEB:" + provisional, "https://chatgpt.com/c/" + provisional])
    assert watcher.session_rollover.complete_from_response(_rollover_bridge(old_page, new_page), canonical_handover(watcher)) is True
    assert watcher.state["architectConversationId"] == provisional


def test_rollover_final_identity_failure_closes_page_before_commit(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    provisional = "7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _ChangingAckPage(["https://chatgpt.com/c/WEB:" + provisional, "https://chatgpt.com/"])
    assert watcher.session_rollover.complete_from_response(_rollover_bridge(old_page, new_page), canonical_handover(watcher)) is False
    assert new_page.closed is True
    assert old_page.closed is False
    assert watcher.state["architectConversationId"] == "OLD"


def test_attached_identity_self_heals_once_without_changing_task_state(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    identity = "7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    result = tmp_path / "000040.txt"
    result.write_text("result", encoding="utf-8")
    watcher.state.update({"architectConversationId": "WEB:" + identity, "state": "RESULT_READY", "taskId": "000040", "executorResultPath": str(result)})
    watcher.save()
    saves = []
    original_save = watcher.save
    monkeypatch.setattr(watcher, "save", lambda: (saves.append(1), original_save())[1])
    attached = type("Attached", (), {"page": _AckPage("https://chatgpt.com/c/" + identity)})()
    assert watcher_module.canonicalize_attached_architect_conversation(watcher, attached, "WEB:" + identity) == identity
    assert watcher.state["architectConversationId"] == identity
    assert watcher.state["state"] == "RESULT_READY"
    assert watcher.state["taskId"] == "000040"
    assert len(saves) == 1


def test_remote_control_startup_accepts_compatible_identity_and_self_heals(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    identity = "7e8916ac-bd6b-4186-8e40-4df52b5192c1"
    watcher.state["architectConversationId"] = "WEB:" + identity
    watcher.save()
    class Bridge:
        page = _AckPage("https://chatgpt.com/c/" + identity)
        def control_user_messages(self): return []
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: Bridge())
    monitor.establish_startup_baseline()
    assert watcher.state["architectConversationId"] == identity


def _rollover_bridge(old_page, new_page):
    class Bridge:
        page = old_page
        def open_fresh_with_handover(self, _response):
            return new_page
    return Bridge()


def test_new_conversation_url_alone_does_not_authorize_result_delivery(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _AckPage("https://chatgpt.com/c/NEW", entries=[])
    ticks = iter(range(100))
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    assert watcher.session_rollover.complete_from_response(_rollover_bridge(old_page, new_page), canonical_handover(watcher)) is False
    assert new_page.closed is True
    assert old_page.closed is False
    assert watcher.state["architectConversationId"] == "OLD"


def test_new_architect_handover_ack_must_finish_before_delivery_ready(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _AckPage("https://chatgpt.com/c/NEW", entries=[{"id": "ack", "text": "ARCHITECT_SESSION_READY"}])
    assert watcher.session_rollover.complete_from_response(_rollover_bridge(old_page, new_page), canonical_handover(watcher)) is True
    assert watcher.state["architectConversationId"] == "NEW"
    assert old_page.closed is True


def test_generation_visible_new_architect_blocks_handover_ack_until_timeout(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _AckPage("https://chatgpt.com/c/NEW", entries=[{"id": "ack", "text": "ARCHITECT_SESSION_READY"}], generating=True)
    ticks = iter(range(1000))
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    assert watcher.session_rollover.complete_from_response(_rollover_bridge(old_page, new_page), canonical_handover(watcher)) is False
    assert new_page.closed is True
    assert old_page.closed is False
    assert watcher.state["architectConversationId"] == "OLD"


def test_invalid_new_architect_ack_fails_closed_before_authority_commit(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _AckPage("https://chatgpt.com/c/NEW", entries=[{"id": "ack", "text": "not the handover acknowledgement"}])
    assert watcher.session_rollover.complete_from_response(_rollover_bridge(old_page, new_page), canonical_handover(watcher)) is False
    assert new_page.closed is True
    assert old_page.closed is False
    assert watcher.state["architectConversationId"] == "OLD"


def test_generation_visible_new_architect_blocks_result_delivery(tmp_path):
    watcher = ready(tmp_path)
    sends = []
    class Bridge:
        def generation_visible(self): return True
        def submit_result_bounded(self, payload): sends.append(payload)
    assert watcher.deliver_result(Bridge()) == watcher_module.RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING
    assert sends == []
    assert watcher.state["state"] == "RESULT_READY"


@pytest.mark.parametrize("workflow_state,reason", [
    ("HUMAN_REQUIRED", "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"),
    ("RESULT_READY", None),
    ("ARCHITECT_RUNNING", None),
    ("NEXT_PROMPT_READY", None),
])
def test_memory_due_records_maintenance_without_preempting_workflow_state(tmp_path, workflow_state, reason):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": workflow_state, "taskId": "task-1", "humanRequiredReason": reason})
    before = dict(watcher.state)
    watcher.session_rollover.sample_memory(lambda: watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES)
    assert watcher.state.get("rolloverDue") is True
    assert watcher.state.get("rolloverTrigger") == "MEMORY_THRESHOLD"
    assert watcher.state["state"] == workflow_state
    assert watcher.state.get("humanRequiredReason") == reason
    assert watcher.state.get("rolloverPending", False) is False
    assert {key: watcher.state.get(key) for key in ("taskId", "state", "humanRequiredReason")} == {key: before.get(key) for key in ("taskId", "state", "humanRequiredReason")}


def test_memory_due_failed_rollover_blocks_next_prompt_dispatch(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000002.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000001", "lastCompletedTaskId": "000001",
                          "nextTaskId": "000002", "nextPromptPath": str(prompt), "executorProcessState": "COMPLETED_WITH_RESULT"})
    watcher.session_rollover.sample_memory(lambda: watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES * 2)
    assert watcher.state.get("rolloverDue") is True
    maintenance_calls = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: maintenance_calls.append(1) or False)
    class Process:
        pid = 7001
    launches = []
    process = watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: launches.append(1) or Process(), "endpoint", lambda: False
    )
    assert process is None
    assert watcher.state["rolloverPending"] is True
    process = watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: launches.append(1) or Process(), "endpoint", lambda: False
    )
    assert maintenance_calls == [1]
    assert launches == []


def test_completed_architect_response_samples_before_next_prompt_dispatch(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state["state"] = "ARCHITECT_RUNNING"
    samples = iter([watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES - 1,
                    watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES])
    class Bridge:
        def current_session_memory_bytes(self): return next(samples)
    assert watcher_module.sample_completed_architect_response_memory(watcher, Bridge()) is None
    assert watcher.state.get("rolloverDue") is not True
    assert watcher_module.sample_completed_architect_response_memory(watcher, Bridge()) == "MEMORY_THRESHOLD"
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    prompt = tmp_path / "next.txt"
    prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000001", "lastCompletedTaskId": "000001",
                          "nextTaskId": "000002", "nextPromptPath": str(prompt),
                          "executorProcessState": "COMPLETED_WITH_RESULT"})
    order = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: order.append("rollover") or False)
    class Process:
        pid = 7002
    assert watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: order.append("launch") or Process(), "endpoint", lambda: False
    ) is None
    assert watcher.state["rolloverPending"] is True
    assert watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: order.append("launch") or Process(), "endpoint", lambda: False
    ) is None
    assert order == ["rollover"]


def test_legacy_rollover_cutout_recovers_only_with_valid_newer_staged_task(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "000001-result.txt"
    prompt = tmp_path / "000002.txt"
    result.write_text("completed result", encoding="utf-8")
    prompt.write_bytes(b"staged next prompt")
    watcher.state.update({
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "taskId": "000001",
        "lastCompletedTaskId": "000001",
        "nextTaskId": "000002",
        "nextPromptPath": str(prompt),
        "executorResultPath": str(result),
        "executorProcessState": "COMPLETED_WITH_RESULT",
        "rolloverDue": True,
    })
    before = prompt.read_bytes()
    assert watcher.recover_legacy_rollover_cutout() is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state.get("humanRequiredReason") is None
    assert watcher.state["taskId"] == "000001"
    assert watcher.state["nextTaskId"] == "000002"
    assert prompt.read_bytes() == before


def test_legacy_rollover_cutout_retires_memory_authority_and_dispatches_without_maintenance(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "000053-result.txt"
    prompt = tmp_path / "000054.txt"
    result.write_text("completed result", encoding="utf-8")
    prompt.write_bytes(b"staged task 000054")
    prompt_before = prompt.read_bytes()
    watcher.state.update({
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "taskId": "000053",
        "lastCompletedTaskId": "000053",
        "nextTaskId": "000054",
        "nextPromptPath": str(prompt),
        "executorResultPath": str(result),
        "executorProcessState": "COMPLETED_WITH_RESULT",
        "architectResponseCount": 12,
        "rolloverDue": True,
        "rolloverTrigger": "MEMORY_THRESHOLD",
        "rolloverPending": True,
        "rolloverInProgress": True,
        "handoverRequested": True,
        "handoverReady": True,
        "pending_handover": "stale handover",
        "rolloverAttemptedForTaskId": "000053",
        "rolloverTransactionId": "old-transaction",
        "rolloverTransactionTaskId": "000053",
        "rolloverHandoverSendState": "ACKNOWLEDGED",
        "rolloverHandoverResponseIdentity": "old-response",
        "rolloverHandoverRecoveryDisposition": "RETRYABLE",
        "rolloverFreshCandidateConversationId": "old-candidate",
        "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS",
        "rolloverFreshCandidateDiscoveryState": "STALE",
        "rolloverFreshBootstrapPayloadHash": "old-bootstrap",
        "rolloverFreshPageCreated": True,
        "rolloverRecoveryState": "CUTOUT",
        "rolloverRecoveryAttemptCount": 2,
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverLastFailureReason": "old failure",
        "rolloverDeferredForTaskId": "000053",
        "rolloverAutoAttemptCount": 2,
    })
    watcher.save()

    assert watcher.recover_legacy_rollover_cutout() is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state.get("humanRequiredReason") is None
    assert watcher.state["rolloverRecoveryState"] == "PENDING"
    assert watcher.state.get("rolloverDue") is False
    assert watcher.state.get("rolloverPending") is False
    assert watcher.state.get("rolloverTrigger") is None
    for key in (
        "handoverRequested", "handoverReady", "pending_handover",
        "rolloverAttemptedForTaskId", "rolloverTransactionId",
        "rolloverTransactionTaskId", "rolloverHandoverSendState",
        "rolloverHandoverResponseIdentity", "rolloverFreshCandidateConversationId",
        "rolloverFreshCandidateState", "rolloverFreshCandidateDiscoveryState",
        "rolloverFreshBootstrapPayloadHash", "rolloverFreshPageCreated",
        "rolloverMaintenanceState", "rolloverLastFailureReason",
        "rolloverDeferredForTaskId", "rolloverAutoAttemptCount",
    ):
        assert watcher.state.get(key) in (None, False)

    maintenance_calls = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: maintenance_calls.append(1) or False)
    launches = []

    class Process:
        pid = 54054

    process = watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: launches.append(1) or Process(), "endpoint", lambda: False
    )
    assert process is not None
    assert maintenance_calls == []
    assert launches == [1]
    assert watcher.state["lastCompletedTaskId"] == "000053"
    assert watcher.state["taskId"] == "000054"
    assert prompt.read_bytes() == prompt_before


def test_legacy_rollover_cutout_rederives_memory_authority(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "000053-result.txt"
    prompt = tmp_path / "000054.txt"
    result.write_text("completed result", encoding="utf-8")
    prompt.write_text("staged task 000054", encoding="utf-8")
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "taskId": "000053", "lastCompletedTaskId": "000053", "nextTaskId": "000054",
        "nextPromptPath": str(prompt), "executorResultPath": str(result),
        "executorProcessState": "COMPLETED_WITH_RESULT", "architectResponseCount": 30,
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
        "rolloverDue": True, "rolloverTrigger": "MEMORY_THRESHOLD", "rolloverPending": True,
        "pending_handover": "stale handover", "rolloverTransactionId": "old-transaction",
    })
    assert watcher.recover_legacy_rollover_cutout() is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"
    assert watcher.state["rolloverPending"] is False
    watcher.state.update({
        "rolloverPending": True,
        "rolloverMaintenanceState": "IN_PROGRESS",
        "rolloverRecoveryState": "PENDING",
    })

    maintenance_calls = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: maintenance_calls.append(1) or False)
    launches = []

    class Process:
        pid = 54054

    process = watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: launches.append(1) or Process(), "endpoint", lambda: False
    )
    assert process is None
    assert maintenance_calls == [1]
    assert launches == []
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"


def test_legacy_rollover_cutout_recovery_fails_closed_for_incomplete_proof(tmp_path):
    cases = [
        {"executorResultPath": "missing-result"},
        {"nextPromptPath": "missing-prompt"},
        {"nextTaskId": "000053"},
        {"architectDecisionAuthorityInvalid": True},
    ]
    for index, overrides in enumerate(cases):
        watcher = LocalFirstOrchestrator(str(tmp_path / str(index)), tmp_path / str(index) / "work")
        result = tmp_path / str(index) / "000053-result.txt"
        prompt = tmp_path / str(index) / "000054.txt"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text("completed result", encoding="utf-8")
        prompt.write_text("staged task 000054", encoding="utf-8")
        watcher.state.update({
            "state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
            "taskId": "000053", "lastCompletedTaskId": "000053", "nextTaskId": "000054",
            "nextPromptPath": str(prompt), "executorResultPath": str(result),
            "executorProcessState": "COMPLETED_WITH_RESULT",
        })
        watcher.state.update(overrides)
        assert watcher.recover_legacy_rollover_cutout() is False
        assert watcher.state["state"] == "HUMAN_REQUIRED"


def test_legacy_rollover_cutout_validates_canonical_next_task_and_dispatches_once(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "000054-result.txt"
    prompt = tmp_path / "000055.txt"
    result.write_text("completed result", encoding="utf-8")
    prompt.write_bytes(b"canonical staged task 000055")
    prompt_before = prompt.read_bytes()
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "taskId": "000054", "lastCompletedTaskId": "000054", "nextTaskId": "000055",
        "taskSequence": 54, "nextPromptPath": str(prompt), "executorResultPath": str(result),
        "executorProcessState": "COMPLETED_WITH_RESULT", "architectResponseCount": 12,
        "rolloverDue": True, "rolloverTrigger": "MEMORY_THRESHOLD", "rolloverPending": True,
        "pending_handover": "stale handover", "rolloverTransactionId": "old-transaction",
    })

    assert watcher.recover_legacy_rollover_cutout() is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000055"
    assert watcher.state["rolloverDue"] is False
    assert watcher.state.get("rolloverTrigger") is None

    maintenance_calls = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: maintenance_calls.append(1) or False)
    launches = []

    class Process:
        pid = 55055

    process = watcher_module.dispatch_next_prompt_once(
        watcher, lambda *_args: launches.append(1) or Process(), "endpoint", lambda: False
    )
    assert process is not None
    assert maintenance_calls == []
    assert launches == [1]
    assert watcher.state["lastCompletedTaskId"] == "000054"
    assert watcher.state["taskId"] == "000055"
    assert prompt.read_bytes() == prompt_before


@pytest.mark.parametrize("next_task_id,task_sequence,expected", [
    ("000054", 54, False),
    ("000053", 54, False),
    ("000056", 54, False),
    ("000056", 55, True),
    ("000055", "not-a-number", False),
])
def test_legacy_rollover_cutout_rejects_incompatible_canonical_task_sequence(tmp_path, next_task_id, task_sequence, expected):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "000054-result.txt"
    prompt = tmp_path / f"{next_task_id}.txt"
    result.write_text("completed result", encoding="utf-8")
    prompt.write_text("staged prompt", encoding="utf-8")
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "taskId": "000054", "lastCompletedTaskId": "000054", "nextTaskId": next_task_id,
        "taskSequence": task_sequence, "nextPromptPath": str(prompt), "executorResultPath": str(result),
        "executorProcessState": "COMPLETED_WITH_RESULT",
    })
    assert watcher.recover_legacy_rollover_cutout() is expected
    assert watcher.state["state"] == ("NEXT_PROMPT_READY" if expected else "HUMAN_REQUIRED")


def test_deferred_rollover_can_attempt_once_at_live_executor_boundary(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "task-1", "codexPid": 1234,
                          "architectConversationId": "current", "rolloverDue": True, "rolloverTrigger": "MEMORY_THRESHOLD"})
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    sent = []
    class Bridge:
        def submit_result_bounded(self, message): sent.append(message)
    assert watcher.session_rollover.request_if_due(Bridge(), True, True) is False
    assert sent == []


def test_failed_rollover_preserves_executor_workflow_and_due_state(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "task-1", "codexPid": 1234,
                          "rolloverDue": True, "rolloverTrigger": "MEMORY_THRESHOLD"})
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    class Bridge:
        page = _AckPage("https://chatgpt.com/c/OLD")
        def submit_result_bounded(self, _response): pass
        def open_fresh_with_handover(self, _response): raise RuntimeError("fresh page unavailable")
    assert watcher.session_rollover.request_if_due(Bridge(), True, True) is False
    assert watcher.state["state"] == "EXECUTOR_RUNNING"
    assert watcher.state.get("humanRequiredReason") is None
    assert watcher.state["rolloverDue"] is True
    assert watcher.state.get("rolloverInProgress", False) is False


def test_preempted_rollover_failure_restores_result_recovery_without_executor(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-1", "lastCompletedTaskId": "task-1",
                          "executorResultPath": watcher.state["executorResultPath"],
                          "architectDeliveryPayloadHash": payload_hash,
                          "architectSendState": "AMBIGUOUS",
                          "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS",
                          "humanRequiredReason": "ARCHITECT_HANDOVER_RESPONSE_INVALID",
                          "rolloverPending": True, "rolloverDue": True, "rolloverInProgress": False,
                          "handoverRequested": True, "nextPromptPath": None, "codexPid": None})
    watcher.save()
    assert watcher.recover_preempted_rollover_failure() is True
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"
    assert watcher.state["handoverRequested"] is False
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["architectDeliveryPayloadHash"] == payload_hash
    assert Path(watcher.state["executorResultPath"]).read_text(encoding="utf-8") == "executor report"


def test_preempted_rollover_confirmed_delivery_restores_architect_without_resend(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-1", "lastCompletedTaskId": "task-1",
                          "architectDeliveryPayloadHash": payload_hash, "architectSendState": "CONFIRMED",
                          "humanRequiredReason": "ARCHITECT_HANDOVER_RESPONSE_INVALID",
                          "rolloverPending": True, "rolloverDue": True, "rolloverInProgress": False,
                          "handoverRequested": True, "pending_handover": "old handover", "nextPromptPath": None,
                          "codexPid": None})
    watcher.save()
    class Bridge:
        def submit_result_bounded(self, _message): raise AssertionError("confirmed result must not be resent")
    bridge = Bridge()
    assert watcher.recover_preempted_rollover_failure() is True
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state.get("humanRequiredReason") is None
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert watcher.state["architectDeliveryPayloadHash"] == payload_hash
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverPending"] is False
    assert watcher.state["handoverRequested"] is False
    assert watcher.state["rolloverInProgress"] is False
    assert "pending_handover" not in watcher.state
    assert not hasattr(bridge, "sent")


def test_preempted_rollover_confirmed_recovery_allows_normal_execute_envelope(tmp_path):
    watcher, base, _worktree = recovery_fixture(tmp_path)
    result = Path(watcher.state["executorResultPath"])
    result.write_text("completed result", encoding="utf-8")
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "recovery-safety-task", "lastCompletedTaskId": "recovery-safety-task",
                          "executorResultPath": str(result), "architectDeliveryPayloadHash": payload_hash,
                          "architectSendState": "CONFIRMED", "humanRequiredReason": "ARCHITECT_HANDOVER_RESPONSE_INVALID",
                          "rolloverDue": True, "rolloverPending": True, "handoverRequested": True, "nextPromptPath": None,
                          "codexPid": None})
    watcher.save()
    assert watcher.recover_preempted_rollover_failure() is True
    decision = watcher.accept_architect_response(envelope("recovery-safety-task", prompt=configured_project_prompt(base, subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip(), "next-task")))
    assert decision["action"] == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000001"


def test_fact_based_confirmed_human_recovery_preserves_result_and_stages_once(tmp_path, monkeypatch):
    watcher, base, _worktree = recovery_fixture(tmp_path)
    result = Path(watcher.state["executorResultPath"])
    result.write_text("completed result", encoding="utf-8")
    _payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED",
                          "taskId": "recovery-safety-task", "lastCompletedTaskId": "recovery-safety-task",
                          "nextTaskId": "recovery-safety-task", "nextPromptPath": None,
                          "architectDeliveryPayloadHash": payload_hash, "architectSendState": "CONFIRMED",
                          "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": True,
                          "handoverRequested": True, "pending_handover": "stale", "codexPid": 7348})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    assert watcher.recover_completed_confirmed_workflow() is True
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state.get("humanRequiredReason") is None
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert watcher.state["architectDeliveryPayloadHash"] == payload_hash
    assert watcher.state["executorResultPath"] == str(result)
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverPending"] is False
    assert "pending_handover" not in watcher.state
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    decision = watcher.accept_architect_response(envelope("recovery-safety-task", prompt=configured_project_prompt(base, head, "next-task")))
    assert decision["action"] == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000001"


def test_main_maintenance_service_requires_live_executor_boundary(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "task-1", "codexPid": 1234,
                          "architectConversationId": "current", "rolloverDue": True, "rolloverTrigger": "MEMORY_THRESHOLD"})
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    closed = []
    class Bridge:
        page = _AckPage("https://chatgpt.com/c/current")
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def wait_for_new_response(self, *_args, **_kwargs): return {"state": "COMPLETED", "text": "handover"}
        def close(self): closed.append(True)
    monkeypatch.setattr(ArchitectPlaywright, "attach", staticmethod(lambda *_args: Bridge()))
    monkeypatch.setattr(watcher.session_rollover, "request_if_due", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rollover must not run while executor is active")))
    monkeypatch.setattr(watcher, "process_pending_handover_response", lambda *_args: True)
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False) is False
    assert closed == []


def test_ambiguous_handover_reconciles_same_invocation_without_terminal_defer(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000080.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000079", "lastCompletedTaskId": "000079",
        "nextTaskId": "000080", "nextPromptPath": str(prompt), "rolloverDue": True,
        "rolloverTrigger": "MEMORY_THRESHOLD", "rolloverPending": True,
        "architectConversationId": "OLD", "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
    })
    watcher.save()
    clock = [100.0]
    monkeypatch.setattr(watcher_module.time, "time", lambda: clock[0])
    sends = []
    ready = [False]

    handover_prefix = "AFFOTECH ARCHITECT SESSION HANDOVER"

    class Page:
        def __init__(self, url, users=None, assistants=None):
            self.url = url
            self.users = users or []
            self.assistants = assistants or []
            self.context = None
            self.closed = False

        def evaluate(self, script):
            if "stop-button" in script:
                return False
            if 'data-message-author-role="user"' in script:
                return self.users
            if 'data-message-author-role="assistant"' in script:
                return self.assistants
            return []

        def close(self):
            self.closed = True

    old_page = Page("https://chatgpt.com/c/OLD")
    fresh_page = Page("https://chatgpt.com/c/FRESH")
    context = type("Context", (), {"pages": [old_page, fresh_page]})()
    old_page.context = fresh_page.context = context

    class Bridge:
        page = old_page

        def assistant_baseline(self):
            return {"count": 0, "text_hash": "baseline", "entries": []}

        def generation_visible(self):
            return False

        def submit_result_bounded(self, payload):
            sends.append(payload)
            raise ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT")

        def _assistant_entries(self):
            if not ready[0]:
                return []
            transaction_id = watcher.state["rolloverTransactionId"]
            return [{"id": "handover", "text": watcher_module.make_handover_envelope(transaction_id, "000080", handover_prefix)}]

        def close(self):
            pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)

    # The first send action is attempted once, then its acknowledgement times
    # out.  The same service invocation must reconcile read-only rather than
    # recording terminal DEFERRED state.
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert len(sends) == 1
    assert watcher.state["rolloverHandoverSendState"] == "AMBIGUOUS"
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["rolloverInProgress"] is True
    assert watcher.state["handoverRequested"] is True
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert "rolloverDeferredForTaskId" not in watcher.state
    assert watcher.state["rolloverHandoverRecoveryDisposition"] == "RETRYABLE"

    # A later bounded read on the same watcher finds the matching response;
    # no second submit is permitted and no operator restart is involved.
    ready[0] = True
    transaction_id = watcher.state["rolloverTransactionId"]
    handover = watcher_module.make_handover_envelope(transaction_id, "000080", handover_prefix)
    fresh_page.users = [watcher_module.fresh_architect_bootstrap_payload(handover)]
    fresh_page.assistants = [{"id": "ready", "text": "ARCHITECT_SESSION_READY"}]
    clock[0] = 106.0
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is True
    assert len(sends) == 1
    assert watcher.state["architectConversationId"] == "FRESH"
    assert watcher.state["rolloverDue"] is False
    assert watcher.state["rolloverPending"] is False
    assert watcher.state["rolloverInProgress"] is False
    assert watcher.state["handoverRequested"] is False
    assert watcher.state.get("humanRequiredReason") is None
    assert getattr(watcher, "_operator_restart_rollover_recovery_available", False) is False


def test_deferred_rollover_gets_fresh_transaction_and_rejects_stale_handover(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt_a = tmp_path / "000041.txt"
    prompt_b = tmp_path / "000042.txt"
    prompt_a.write_text("task A", encoding="utf-8")
    prompt_b.write_text("task B", encoding="utf-8")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000040", "lastCompletedTaskId": "000040",
        "nextTaskId": "000041", "nextPromptPath": str(prompt_a), "rolloverDue": True,
        "architectResponseCount": 30, "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES, "architectConversationId": "OLD",
    })
    watcher.save()

    class FailedSend:
        def submit_result_bounded(self, _payload):
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE")

    assert watcher.session_rollover.request_if_due(FailedSend(), True, False, safe_boundary_state="NEXT_PROMPT_READY") is False
    transaction_a = watcher.state["rolloverTransactionId"]
    launches = []
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: launches.append(1) or type("Process", (), {"pid": 4100})())
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    assert launches == []
    assert watcher.state.get("humanRequiredReason") == "ROLLOVER_EVALUATOR_RECOVERY_EPOCH_BUDGET_INVALID"

    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000041", "lastCompletedTaskId": "000041",
        "nextTaskId": "000042", "nextPromptPath": str(prompt_b), "rolloverDue": True,
        "humanRequiredReason": None,
        "rolloverPending": True, "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": "000041", "rolloverHandoverSendState": "UNSENT",
        "handoverRequested": False, "architectResponseCount": 30,
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
    })
    watcher.save()
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    fresh_page = _AckPage("https://chatgpt.com/c/NEW", entries=[{"id": "ready", "text": "ARCHITECT_SESSION_READY"}])
    opened = []

    class FreshBridge:
        page = old_page
        def submit_result_bounded(self, _payload):
            pass
        def open_fresh_with_handover(self, _handover):
            opened.append(1)
            return fresh_page

    bridge = FreshBridge()
    assert watcher.session_rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY") is True
    transaction_b = watcher.state["rolloverTransactionId"]
    assert transaction_b != transaction_a
    stale = f"old handover\nRollover transaction ID: {transaction_a}\nARCHITECT_HANDOVER_READY"
    assert watcher.session_rollover.complete_from_response(bridge, stale) is False
    assert opened == []
    assert watcher.state["architectConversationId"] == "OLD"
    matching = watcher_module.make_handover_envelope(transaction_b, "000042", "new handover")
    assert watcher.session_rollover.complete_from_response(bridge, matching) is True
    assert opened == [1]
    assert watcher.state["architectConversationId"] == "NEW"
    assert watcher.state.get("humanRequiredReason") is None


def test_acknowledged_handover_recovery_reconstructs_and_launches_staged_next_task(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000054.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    handover = canonical_handover(watcher, "captured handover", task_id="000054")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000053", "lastCompletedTaskId": "000053",
                          "nextTaskId": "000054", "nextPromptPath": str(prompt), "rolloverDue": True,
                          "rolloverPending": True, "rolloverInProgress": False, "rolloverAttemptedForTaskId": "000053",
                          "handoverRequested": False, "handoverReady": False, "rolloverHandoverSendState": "ACKNOWLEDGED",
                          "rolloverHandoverResponseIdentity": None, "architectConversationId": "OLD",
                          "executorProcessState": "COMPLETED_WITH_RESULT"})
    watcher.save()

    class Page:
        def __init__(self, url, assistants): self.url, self.assistants, self.closed, self.context = url, assistants, False, None
        def evaluate(self, script):
            if "stop-button" in script: return False
            if 'data-message-author-role="assistant"' in script:
                assert "cloneNode(true)" in script
                assert "button,[role=\"button\"]" in script
                return self.assistants
            return []
        def close(self): self.closed = True

    old = Page("https://chatgpt.com/c/OLD", [{"id": "handover", "text": handover}])
    fresh = Page("https://chatgpt.com/c/NEW", [{"id": "ready", "text": "ARCHITECT_SESSION_READY"}])
    old.context = type("Context", (), {"pages": [old]})()
    opened, sends, launches = [], [], []

    class Bridge:
        page = old
        def _assistant_entries(self): return ArchitectPlaywright(old)._assistant_entries()
        def assistant_baseline(self): return {"count": 1, "text_hash": "old"}
        def generation_visible(self): return False
        def open_fresh_with_handover(self, _handover): opened.append(1); return fresh
        def wait_for_new_response(self, *_args, **_kwargs): raise AssertionError("recovered handover must not be resent or waited for")
        def submit_result_bounded(self, _message): sends.append(1)
        def close(self): pass

    bridge = Bridge()
    original_complete = watcher.session_rollover.complete_from_response
    def capture_before_fresh_mutation(current_bridge, response):
        assert watcher.state["pending_handover"] == handover
        assert watcher.state["rolloverHandoverResponseIdentity"] == hashlib.sha256(handover.encode()).hexdigest()
        assert watcher.state["rolloverFreshBootstrapPayloadHash"] == hashlib.sha256(watcher_module.fresh_architect_bootstrap_payload(handover).encode()).hexdigest()
        return original_complete(current_bridge, response)
    monkeypatch.setattr(watcher.session_rollover, "complete_from_response", capture_before_fresh_mutation)
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _watcher, _bridge, requested: requested)
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is True
    assert watcher.state["architectConversationId"] == "NEW"
    assert watcher.state["rolloverDue"] is False
    assert watcher.state["rolloverPending"] is False
    assert opened == [1] and sends == []
    assert old.closed is True and fresh.closed is False
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: launches.append(1) or type("Process", (), {"pid": 5400})())
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False) is not None
    assert launches == [1]
    assert watcher.state["nextTaskId"] == "000054"


def test_acknowledged_handover_without_proof_gets_explicit_retryable_disposition(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000053", "lastCompletedTaskId": "000053",
                          "nextTaskId": "000054", "rolloverDue": True, "rolloverPending": True,
                          "rolloverAttemptedForTaskId": "000053", "handoverRequested": False,
                          "rolloverHandoverSendState": "ACKNOWLEDGED"})
    class Page:
        url = "https://chatgpt.com/c/OLD"
        context = None
        def evaluate(self, _script): return []
    page = Page(); page.context = type("Context", (), {"pages": [page]})()
    bridge = ArchitectPlaywright(page)
    assert watcher.session_rollover.reconcile_pending_handover(bridge) is False
    assert watcher.state["rolloverHandoverRecoveryDisposition"] == "RETRYABLE"
    assert watcher.state["rolloverHandoverRecoveryReason"] == "ARCHITECT_HANDOVER_NOT_FOUND"
    assert watcher.state["rolloverDue"] is True


def _next_prompt_ready_fixture(tmp_path, *, due=False):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "next.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000040", "nextTaskId": "000041",
                          "nextPromptPath": str(prompt), "targetWorktree": str(tmp_path / "worktree"),
                          "rolloverDue": due, "rolloverPending": due, "architectConversationId": "current"})
    watcher.save()
    return watcher, prompt


def test_next_prompt_ready_below_threshold_launches_without_rollover(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path)
    events = []
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: events.append("launch") or type("Process", (), {"pid": 41})())
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: events.append("rollover") or False)
    process = watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False)
    assert process is not None
    assert events == ["launch"]
    assert prompt.read_text(encoding="utf-8") == "next bounded task"


def test_next_prompt_ready_due_rollover_precedes_single_launch_and_preserves_staged_task(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    original = {key: watcher.state[key] for key in ("taskId", "nextTaskId", "nextPromptPath", "targetWorktree")}
    events = []
    def complete_rollover(*_args, **_kwargs):
        events.append("rollover")
        watcher.state.update({"rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False,
                              "handoverRequested": False, "architectConversationId": "FRESH"})
        return True
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", complete_rollover)
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: events.append("launch") or type("Process", (), {"pid": 41})())
    process = watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False)
    assert process is not None
    assert events == ["rollover", "launch"]
    assert {key: watcher.state[key] for key in original} == original
    assert prompt.read_text(encoding="utf-8") == "next bounded task"


def test_next_prompt_ready_failed_rollover_does_not_launch_or_duplicate(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    original = {key: watcher.state[key] for key in ("taskId", "nextTaskId", "nextPromptPath", "targetWorktree")}
    launches = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: launches.append(1))
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    assert launches == []
    assert watcher.state["rolloverDue"] is True
    assert {key: watcher.state[key] for key in original} == original
    assert prompt.read_text(encoding="utf-8") == "next bounded task"


def test_next_prompt_ready_deferred_rollover_enters_resident_passive_wait(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    service_calls = []
    launches = []
    sleeps = []

    def failed_rollover(*_args, **_kwargs):
        service_calls.append(1)
        watcher._operator_restart_rollover_recovery_available = False
        watcher.state.update({
            "rolloverMaintenanceState": "DEFERRED",
            "rolloverDeferredForTaskId": watcher.state["nextTaskId"],
            "rolloverDue": True,
            "rolloverAutomaticRecoveryEpochCount": 1,
            "rolloverAutomaticRecoveryMaxEpochs": 3,
            "rolloverAutomaticRecoveryNextEligibleAt": 100,
            "rolloverRecoveryState": "DEFERRED",
        })
        return False

    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", failed_rollover)
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: launches.append(1))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda interval: sleeps.append(interval))
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher, "_load_state", lambda: watcher.state)

    for _ in range(3):
        assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None

    assert service_calls == [1]
    assert launches == []
    assert len(sleeps) == 2
    assert all(interval == 2.0 for interval in sleeps)
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000041"
    assert watcher.state["nextPromptPath"] == str(prompt)
    assert watcher.state["rolloverDue"] is True
    assert watcher.state.get("humanRequiredReason") not in {"ARCHITECT_ROLLOVER_SAFETY_CUTOUT", "ROLLOVER_MAINTENANCE_DEFERRED"}


def _live_reconciliation_backoff_state(watcher, prompt, *, now=100.0):
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000079", "lastCompletedTaskId": "000079",
        "nextTaskId": "000080", "nextPromptPath": str(prompt), "rolloverDue": True,
        "rolloverTrigger": "MEMORY_THRESHOLD", "rolloverPending": True,
        "rolloverInProgress": True, "handoverRequested": True,
        "rolloverHandoverSendState": "AMBIGUOUS", "rolloverMaintenanceState": "RECONCILE_PENDING",
        "rolloverRecoveryState": "RECOVERING", "rolloverRecoveryAttemptCount": 1,
        "rolloverRecoveryStartedAt": now - 3.0, "rolloverRecoveryRetryAfter": now + 5.0,
        "rolloverHandoverRecoveryDisposition": "RETRYABLE",
        "rolloverAttemptedForTaskId": "000080",
        "rolloverTransactionId": "TEST-TX-000080", "rolloverTransactionTaskId": "000080",
        "architectConversationId": "OLD",
    })
    watcher.save()


def test_reconciliation_backoff_is_wait_not_exhaustion_or_busy_loop(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    _live_reconciliation_backoff_state(watcher, prompt)
    now = [100.0]
    monkeypatch.setattr(watcher_module.time, "time", lambda: now[0])
    events = []
    monkeypatch.setattr(watcher_module, "runtime_log", lambda *args, **kwargs: events.append((args[2], kwargs)))
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: (_ for _ in ()).throw(AssertionError("backoff must not attach"))))

    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    names = [name for name, _fields in events]
    assert "HANDOVER_RECONCILIATION_WAIT" in names
    assert "HANDOVER_RECONCILIATION_EXHAUSTED" not in names
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert "rolloverDeferredForTaskId" not in watcher.state

    sleeps = []
    monkeypatch.setattr(watcher_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(watcher, "_load_state", lambda: watcher.state)
    service_calls = []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: service_calls.append(1) or False)
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    assert service_calls == []
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 2.0
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["rolloverInProgress"] is True
    assert watcher.state["nextTaskId"] == "000080"


def test_reconciliation_retry_deadline_allows_next_attempt(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    _live_reconciliation_backoff_state(watcher, prompt)
    now = [106.0]
    monkeypatch.setattr(watcher_module.time, "time", lambda: now[0])
    calls = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def _assistant_entries(self):
            calls.append("reconcile")
            return []
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert calls == ["reconcile"]
    assert watcher.state["rolloverRecoveryAttemptCount"] == 2
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert watcher.state["rolloverHandoverSendState"] == "AMBIGUOUS"


@pytest.mark.parametrize("exhaustion", ["attempts", "window"])
def test_reconciliation_true_exhaustion_defers_once(tmp_path, monkeypatch, exhaustion):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    _live_reconciliation_backoff_state(watcher, prompt)
    if exhaustion == "attempts":
        watcher.state["rolloverRecoveryAttemptCount"] = watcher_module.ROLLOVER_RECOVERY_MAX_ATTEMPTS
    else:
        watcher.state["rolloverRecoveryStartedAt"] = 0.0
        watcher.state["rolloverRecoveryRetryAfter"] = 0.0
    watcher.save()
    now = [100.0 if exhaustion == "attempts" else watcher_module.ROLLOVER_RECOVERY_WINDOW_SECONDS + 100.0]
    if exhaustion == "window":
        watcher.state["rolloverRecoveryStartedAt"] = now[0] - watcher_module.ROLLOVER_RECOVERY_WINDOW_SECONDS - 1.0
        watcher.save()
    monkeypatch.setattr(watcher_module.time, "time", lambda: now[0])
    events = []
    monkeypatch.setattr(watcher_module, "runtime_log", lambda *args, **kwargs: events.append((args[2], kwargs)))
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: (_ for _ in ()).throw(AssertionError("exhaustion must not attach"))))

    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert watcher.state["rolloverMaintenanceState"] == "DEFERRED"
    assert watcher.state["rolloverDeferredForTaskId"] == "000080"
    exhausted = [fields for name, fields in events if name == "HANDOVER_RECONCILIATION_EXHAUSTED"]
    assert len(exhausted) == 1
    assert exhausted[0]["recoveryDisposition"] == watcher_module.ROLLOVER_RECOVERY_EXHAUSTED

    # The terminal state is handled by the existing passive wait and cannot
    # re-enter bounded recovery or emit another exhaustion event.
    monkeypatch.setattr(watcher, "_load_state", lambda: watcher.state)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    exhausted = [name for name, _fields in events if name == "HANDOVER_RECONCILIATION_EXHAUSTED"]
    assert len(exhausted) == 1


def test_deferred_rollover_self_rearms_after_cooldown_without_restart(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    transaction = "aa0000000000000000000090"
    watcher.state.update({
        "taskId": "000090", "lastCompletedTaskId": "000090", "nextTaskId": "000091",
        "nextPromptPath": str(prompt), "rolloverDue": True, "rolloverPending": True,
        "rolloverInProgress": False, "handoverRequested": True,
        "rolloverTransactionId": transaction, "rolloverTransactionTaskId": "000091",
        "rolloverAttemptedForTaskId": "000091", "rolloverHandoverSendState": "AMBIGUOUS",
        "rolloverMaintenanceState": "DEFERRED", "rolloverRecoveryState": "DEFERRED",
        "rolloverRecoveryTerminalReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "rolloverDeferredForTaskId": "000091", "rolloverAutomaticRecoveryEpochCount": 1,
        "rolloverAutomaticRecoveryMaxEpochs": 3, "rolloverAutomaticRecoveryNextEligibleAt": 105.0,
        "architectConversationId": "OLD", "executorProcessState": "EXITED",
    })
    watcher.save()
    monkeypatch.setattr(watcher_module.time, "time", lambda: 100.0)
    assert watcher_module.deferred_rollover_passive_wait_required(watcher) is True
    assert watcher.session_rollover._automatic_recovery_epoch_eligible("000091") is False
    monkeypatch.setattr(watcher_module.time, "time", lambda: 106.0)
    assert watcher.session_rollover._automatic_recovery_epoch_eligible("000091") is True
    assert watcher.session_rollover._begin_automatic_recovery_epoch("000091") is True
    assert watcher.state["rolloverAutomaticRecoveryEpochCount"] == 2
    assert watcher.state["rolloverRecoveryAttemptCount"] == 0
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["rolloverMaintenanceState"] == "IN_PROGRESS"
    assert watcher._operator_restart_rollover_recovery_available is False


def test_deferred_rollover_reconciles_existing_response_before_epoch_retry(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    transaction = "aa0000000000000000000091"
    response = canonical_handover(watcher, "handover", transaction_id=transaction, task_id="000091")
    watcher.state.update({
        "taskId": "000090", "lastCompletedTaskId": "000090", "nextTaskId": "000091",
        "nextPromptPath": str(prompt), "rolloverDue": True, "rolloverPending": True,
        "rolloverInProgress": False, "handoverRequested": True,
        "rolloverTransactionId": transaction, "rolloverTransactionTaskId": "000091",
        "rolloverAttemptedForTaskId": "000091", "rolloverHandoverSendState": "AMBIGUOUS",
        "rolloverMaintenanceState": "DEFERRED", "rolloverRecoveryState": "DEFERRED",
        "rolloverRecoveryTerminalReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "rolloverDeferredForTaskId": "000091", "rolloverAutomaticRecoveryEpochCount": 1,
        "rolloverAutomaticRecoveryMaxEpochs": 3, "rolloverAutomaticRecoveryNextEligibleAt": 1.0,
        "architectConversationId": "OLD", "executorProcessState": "EXITED",
    })
    watcher.save()
    events = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def exact_user_message_payload_observed(self, _payload): return False
        def generation_visible(self): return False
        def _assistant_entries(self): return [{"id": "ready", "text": response}]
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.time, "time", lambda: 10.0)
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    def consume(_bridge, _response):
        events.append("consume")
        watcher.state.update({"rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False, "handoverRequested": False, "architectConversationId": "FRESH"})
        return True
    monkeypatch.setattr(watcher, "process_pending_handover_response", consume)
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is True
    assert events == ["consume"]
    assert watcher.state["rolloverAutomaticRecoveryEpochCount"] == 1
    assert watcher.state["rolloverDue"] is False
    assert watcher.state["nextTaskId"] == "000091"


def test_automatic_rollover_epochs_exhaust_into_human_required(tmp_path, monkeypatch):
    watcher, _prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "taskId": "000090", "nextTaskId": "000091", "rolloverDue": True,
        "rolloverPending": True, "rolloverMaintenanceState": "DEFERRED",
        "rolloverRecoveryState": "DEFERRED", "rolloverDeferredForTaskId": "000091",
        "rolloverAutomaticRecoveryEpochCount": 3, "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverTransactionId": "aa0000000000000000000092", "rolloverTransactionTaskId": "000091",
    })
    watcher.save()
    assert watcher.session_rollover._begin_automatic_recovery_epoch("000091") is False
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_ROLLOVER_AUTOMATIC_RECOVERY_EXHAUSTED"
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["rolloverPending"] is True


def test_operator_restart_deferred_rollover_gets_one_attempt_then_successful_dispatch(tmp_path, monkeypatch):
    watcher, _prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": watcher.state["nextTaskId"],
        "rolloverAutomaticRecoveryEpochCount": 1,
        "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverAutomaticRecoveryNextEligibleAt": 0,
        "rolloverRecoveryState": "DEFERRED",
    })
    watcher._operator_restart_rollover_recovery_available = True
    events = []

    def successful_rollover(*_args, **_kwargs):
        events.append("rollover")
        watcher.state.update({
            "rolloverDue": False,
            "rolloverPending": False,
            "rolloverInProgress": False,
            "handoverRequested": False,
            "architectConversationId": "FRESH",
        })
        return True

    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", successful_rollover)
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: events.append("launch") or type("Process", (), {"pid": 4101})())
    process = watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: None, "endpoint", lambda: False)

    assert process is not None
    assert events == ["rollover", "launch"]


def test_operator_restart_failed_rollover_returns_to_passive_wait(tmp_path, monkeypatch):
    watcher, _prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": watcher.state["nextTaskId"],
        "rolloverAutomaticRecoveryEpochCount": 1,
        "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverAutomaticRecoveryNextEligibleAt": 0,
        "rolloverRecoveryState": "DEFERRED",
    })
    watcher._operator_restart_rollover_recovery_available = True
    service_calls = []
    sleeps = []

    def failed_rollover(*_args, **_kwargs):
        service_calls.append(1)
        watcher._operator_restart_rollover_recovery_available = False
        watcher.state.update({
            "rolloverMaintenanceState": "DEFERRED",
            "rolloverDeferredForTaskId": watcher.state["nextTaskId"],
            "rolloverDue": True,
            "rolloverAutomaticRecoveryEpochCount": 1,
            "rolloverAutomaticRecoveryMaxEpochs": 3,
            "rolloverAutomaticRecoveryNextEligibleAt": 100,
            "rolloverRecoveryState": "DEFERRED",
        })
        return False

    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", failed_rollover)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda interval: sleeps.append(interval))
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher, "_load_state", lambda: watcher.state)

    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    assert service_calls == [1]
    assert sleeps == [2.0]


def test_operator_restart_unsent_recovery_reaches_real_request_once(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "taskId": "000070",
        "lastCompletedTaskId": "000070",
        "nextTaskId": "000071",
        "nextPromptPath": str(prompt),
        "rolloverPending": True,
        "rolloverTrigger": "MEMORY_THRESHOLD",
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": "000071",
        "rolloverAttemptedForTaskId": "000071",
        "rolloverHandoverSendState": "UNSENT",
        "handoverRequested": False,
        "architectConversationId": "OLD",
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert restarted._operator_restart_rollover_recovery_available is True
    assert "architectMemoryBytes" not in restarted.state
    assert "architectMemoryMiB" not in restarted.state
    assert restarted.state["rolloverDue"] is True
    assert restarted.state["rolloverTrigger"] == "MEMORY_THRESHOLD"
    events = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def _assistant_entries(self):
            events.append("reconcile")
            return []
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def submit_result_bounded(self, _payload): events.append("send")
        def wait_for_new_response(self, *_args, **_kwargs): return {"state": "TIMEOUT", "text": ""}
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert events == ["reconcile", "send"]
    assert restarted.state["rolloverAttemptedForTaskId"] == "000071"
    assert restarted.state["rolloverHandoverSendState"] in {"ACKNOWLEDGED", "AMBIGUOUS"}


def test_operator_restart_retires_stale_prior_boundary_and_recovers_current_boundary_once(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    old_transaction = "old-transaction-000070"
    watcher.state.update({
        "taskId": "000070",
        "lastCompletedTaskId": "000070",
        "nextTaskId": "000071",
        "nextPromptPath": str(prompt),
        "rolloverDue": True,
        "rolloverTrigger": "MEMORY_THRESHOLD",
        "rolloverPending": True,
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": "000070",
        "rolloverTransactionTaskId": "000070",
        "rolloverAttemptedForTaskId": "000070",
        "rolloverTransactionId": old_transaction,
        "rolloverHandoverSendState": "UNSENT",
        "handoverRequested": False,
        "architectConversationId": "OLD",
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert "architectMemoryBytes" not in restarted.state
    assert "architectMemoryMiB" not in restarted.state
    events = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def submit_result_bounded(self, _payload): events.append("send")
        def wait_for_new_response(self, *_args, **_kwargs): return {"state": "TIMEOUT", "text": ""}
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert events == ["send"]
    assert restarted.state["rolloverTransactionId"] != old_transaction
    assert restarted.state["rolloverTransactionTaskId"] == "000071"
    assert restarted.state["rolloverAttemptedForTaskId"] == "000071"


def _terminal_same_task_rollover_fixture(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000080.txt"
    prompt.write_text("task 000080", encoding="utf-8")
    retired = "8084c3b80c13ebccb27d52c4"
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000079", "lastCompletedTaskId": "000079",
        "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "rolloverDue": True, "rolloverTrigger": "MEMORY_THRESHOLD", "rolloverPending": True,
        "rolloverInProgress": False, "handoverRequested": True,
        "rolloverHandoverSendState": "AMBIGUOUS", "rolloverMaintenanceState": "DEFERRED",
        "rolloverRecoveryState": "DEFERRED",
        "rolloverRecoveryTerminalReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "rolloverTransactionId": retired, "rolloverTransactionTaskId": "000080",
        "rolloverAttemptedForTaskId": "000080", "architectConversationId": "OLD",
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
        "executorProcessState": "COMPLETED_WITH_RESULT",
    })
    watcher.save()
    return watcher, prompt, retired


def test_transaction_lifecycle_authority_classifies_current_and_stale_evidence():
    prepared = {
        "state": "NEXT_PROMPT_READY", "nextTaskId": "000201",
        "rolloverDue": True, "rolloverPending": True,
        "rolloverTransactionId": "prepared-201", "rolloverTransactionTaskId": "000201",
        "rolloverTransactionGeneration": 1, "rolloverHandoverSendState": "UNSENT",
        "handoverRequested": False,
    }
    assert watcher_module.rollover_transaction_lifecycle_action(prepared, "000201") == "REUSE_PREPARED_UNSENT"

    live = dict(prepared, rolloverHandoverSendState="AMBIGUOUS", handoverRequested=True)
    assert watcher_module.rollover_transaction_lifecycle_action(live, "000201") == "RETAIN_CURRENT_TRANSACTION"
    assert watcher_module.rollover_transaction_lifecycle_action(live, "000202") == "RETIRE_STALE_TRANSACTION"

    terminal = dict(live, rolloverInProgress=False, rolloverMaintenanceState="DEFERRED",
                    rolloverRecoveryState="DEFERRED",
                    rolloverRecoveryTerminalReason="ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
                    rolloverAttemptedForTaskId="000201")
    assert watcher_module.rollover_transaction_lifecycle_action(terminal, "000201", False) == "RETAIN_TERMINAL"
    assert watcher_module.rollover_transaction_lifecycle_action(terminal, "000201", True) == "TERMINAL_RECOVERY_CANDIDATE"


def test_transaction_lifecycle_authority_is_non_mutating_and_identity_stable():
    state = {
        "state": "NEXT_PROMPT_READY", "nextTaskId": "000301", "rolloverDue": True,
        "rolloverPending": True, "rolloverTransactionId": "stable-301",
        "rolloverTransactionTaskId": "000301", "rolloverHandoverSendState": "ACKNOWLEDGED",
        "handoverRequested": True,
    }
    before = dict(state)
    first = watcher_module.rollover_transaction_lifecycle_action(state, "000301")
    second = watcher_module.rollover_transaction_lifecycle_action(state, "000301")
    assert first == second == "RETAIN_CURRENT_TRANSACTION"
    assert state == before


def test_action_error_after_payload_delivery_reconciles_without_resend(tmp_path):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "taskId": "000079", "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
        "architectConversationId": "OLD",
    })
    logger, records = runtime_event_capture()
    watcher.runtime_logger = logger
    watcher.runtime_run_id = "delivered-payload-test"
    watcher.save()
    calls = []

    class DeliveredAfterActionError:
        sendActionAttempted = True
        sendActionAcknowledged = False
        last_send_method = "playwright.click"
        initialSendActionReturned = False

        def submit_result_bounded(self, _payload):
            calls.append("send")
            raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", "TimeoutError")

        def exact_user_message_payload_observed(self, payload):
            calls.append(("observe", payload))
            return True

    bridge = DeliveredAfterActionError()
    assert watcher.session_rollover.request_if_due(
        bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY"
    ) is False
    assert calls[0] == "send"
    assert calls[1][0] == "observe"
    assert calls.count("send") == 1
    assert watcher.state["rolloverHandoverSendState"] == "ACKNOWLEDGED"
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert watcher.state["rolloverInProgress"] is True
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["handoverRequested"] is True
    assert watcher.state["rolloverTransactionId"]
    assert watcher.state["rolloverTransactionGeneration"] == 1
    assert any(record.event == "ARCHITECT_HANDOVER_DELIVERY_RECONCILED" for record in records)
    rendered = "\n".join(record.getMessage() for record in records)
    assert "errorCode=ARCHITECT_SEND_ACTION_FAILED" in rendered
    assert "lastSendMethod=playwright.click" in rendered


def test_action_error_without_payload_remains_ambiguous_without_resend(tmp_path):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "taskId": "000079", "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
    })
    calls = []

    class NotDelivered:
        sendActionAttempted = True
        last_send_method = "playwright.click"
        def submit_result_bounded(self, _payload):
            calls.append("send")
            raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", "TimeoutError")
        def exact_user_message_payload_observed(self, _payload):
            calls.append("observe")
            return False

    assert watcher.session_rollover.request_if_due(
        NotDelivered(), True, False, safe_boundary_state="NEXT_PROMPT_READY"
    ) is False
    assert calls == ["send", "observe"]
    assert watcher.state["rolloverHandoverSendState"] == "AMBIGUOUS"
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert watcher.state["rolloverTransactionId"]


def test_acknowledged_reconcile_pending_is_live_and_never_enters_new_send(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "taskId": "000079", "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "rolloverPending": True, "rolloverInProgress": True,
        "handoverRequested": True, "rolloverHandoverSendState": "ACKNOWLEDGED",
        "rolloverMaintenanceState": "RECONCILE_PENDING", "rolloverAttemptedForTaskId": "000080",
        "rolloverTransactionId": "ack-live-000080", "architectConversationId": "OLD",
    })
    assert watcher.session_rollover.handover_reconciliation_pending() is True

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def generation_visible(self): return False
        def _assistant_entries(self): return []
        def submit_result_bounded(self, _payload): raise AssertionError("must not resend")
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(
        watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY"
    ) is False
    assert watcher.state["rolloverHandoverSendState"] == "ACKNOWLEDGED"
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"


def test_terminal_delivered_transaction_is_revived_without_generation_three(tmp_path, monkeypatch):
    watcher, prompt, _retired = _terminal_same_task_rollover_fixture(tmp_path)
    transaction = "2b324cd32cefc00ef1790c36"
    generation = 2
    watcher.state.update({
        "rolloverTransactionId": transaction,
        "rolloverTransactionGeneration": generation,
        "rolloverHandoverSendState": "AMBIGUOUS",
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert restarted._operator_restart_rollover_recovery_available is True
    calls = {"attach": 0, "send": 0, "request": 0}

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def exact_user_message_payload_observed(self, payload):
            assert f"transactionId={transaction}" in payload
            return True
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def generation_visible(self): return False
        def _assistant_entries(self): return []
        def submit_result_bounded(self, _payload): calls["send"] += 1
        def close(self): pass

    bridge = Bridge()
    def attach(*_args):
        calls["attach"] += 1
        return bridge
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(attach))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    original_request = restarted.session_rollover.request_if_due
    def forbidden_request(*args, **kwargs):
        calls["request"] += 1
        return original_request(*args, **kwargs)
    monkeypatch.setattr(restarted.session_rollover, "request_if_due", forbidden_request)

    assert watcher_module.service_deferred_rollover_once(
        restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY"
    ) is False
    assert calls["attach"] == 1
    assert calls["send"] == 0
    assert calls["request"] == 0
    assert restarted.state["rolloverTransactionId"] == transaction
    assert restarted.state["rolloverTransactionGeneration"] == generation
    assert restarted.state["rolloverTransactionTaskId"] == "000080"
    assert restarted.state["rolloverAttemptedForTaskId"] == "000080"
    assert restarted.state["rolloverHandoverSendState"] == "ACKNOWLEDGED"
    assert restarted.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert restarted.state["rolloverInProgress"] is True
    assert restarted.state["rolloverRecoveryState"] == "RECOVERING"
    assert "rolloverRecoveryTerminalReason" not in restarted.state
    assert restarted.state.get("rolloverRetiredTransactionId") is None


def test_terminal_same_task_transaction_retires_once_and_prepares_fresh_id(tmp_path):
    watcher, _prompt, retired = _terminal_same_task_rollover_fixture(tmp_path)
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert restarted._operator_restart_rollover_recovery_available is True
    assert restarted.session_rollover._retire_terminal_same_task_transaction("000080", True) is True
    assert restarted.session_rollover._retire_terminal_same_task_transaction("000080", True) is False
    assert restarted.state["rolloverRetiredTransactionId"] == retired
    assert restarted.state["rolloverHandoverSendState"] == "UNSENT"

    submitted = []

    class Bridge:
        def submit_result_bounded(self, _payload):
            persisted = json.loads(restarted.state_path.read_text(encoding="utf-8"))
            submitted.append((persisted.get("rolloverTransactionId"), persisted.get("rolloverTransactionGeneration")))

    assert restarted.session_rollover.request_if_due(
        Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY",
        allow_same_task_unsent_recovery=True,
    ) is True
    replacement = restarted.state["rolloverTransactionId"]
    assert replacement != retired
    assert restarted.state["rolloverTransactionGeneration"] == 1
    assert submitted == [(replacement, 1)]
    persisted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert persisted.state["rolloverTransactionId"] == replacement
    assert persisted.state["rolloverTransactionGeneration"] == 1


def test_live_transaction_identity_is_immutable_across_restart(tmp_path):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    transaction = "live-transaction-000080"
    watcher.state.update({
        "nextTaskId": "000080", "nextPromptPath": str(prompt),
        "rolloverTransactionId": transaction, "rolloverTransactionTaskId": "000080",
        "rolloverAttemptedForTaskId": "000080", "rolloverInProgress": True,
        "rolloverHandoverSendState": "AMBIGUOUS", "rolloverMaintenanceState": "RECONCILE_PENDING",
    })
    watcher.save()
    assert watcher_module.rollover_transaction_id(watcher.state, "000080") == transaction
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert watcher_module.rollover_transaction_id(restarted.state, "000080") == transaction


def test_terminal_replacement_requires_exact_cutout_and_operator_authority(tmp_path):
    watcher, _prompt, _retired = _terminal_same_task_rollover_fixture(tmp_path)
    for updates in (
        {"rolloverRecoveryTerminalReason": "OTHER"},
        {"rolloverRecoveryTerminalReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT", "nextTaskId": "000081"},
    ):
        candidate = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
        candidate.state.update(watcher.state)
        candidate.state.update(updates)
        assert candidate.session_rollover._retire_terminal_same_task_transaction("000080", True) is False
    assert watcher.session_rollover._retire_terminal_same_task_transaction("000080", False) is False


def test_retired_transaction_token_cannot_satisfy_replacement(tmp_path):
    watcher, _prompt, retired = _terminal_same_task_rollover_fixture(tmp_path)
    assert watcher.session_rollover._retire_terminal_same_task_transaction("000080", True) is True

    class Bridge:
        def submit_result_bounded(self, _payload):
            return None

    assert watcher.session_rollover.request_if_due(
        Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY",
        allow_same_task_unsent_recovery=True,
    ) is True
    replacement = watcher.state["rolloverTransactionId"]
    assert replacement != retired
    assert watcher_module.handover_transaction_matches(
        f"ARCHITECT_HANDOVER_READY\nRollover transaction ID: {retired}", replacement
    ) is False
    assert watcher_module.handover_transaction_matches(
        f"Rollover transaction ID: {replacement}\nARCHITECT_HANDOVER_READY", replacement
    ) is True


def test_deferred_ambiguous_transaction_is_not_replaced_by_restart(tmp_path, monkeypatch):
    watcher, _prompt, retired = _terminal_same_task_rollover_fixture(tmp_path)
    first = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    sent = []

    class FailedBridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def submit_result_bounded(self, _payload):
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE")
        def close(self): pass

    failed_bridge = FailedBridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: failed_bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(first, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    prepared_id = first.state["rolloverTransactionId"]
    assert prepared_id == retired
    assert "rolloverTransactionGeneration" not in first.state
    assert first.state["rolloverHandoverSendState"] == "AMBIGUOUS"

    second = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert second._operator_restart_rollover_recovery_available is True

    class RetryBridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def submit_result_bounded(self, payload):
            persisted = json.loads(second.state_path.read_text(encoding="utf-8"))
            sent.append((payload, persisted.get("rolloverTransactionId"), persisted.get("rolloverTransactionGeneration")))
        def wait_for_new_response(self, *_args, **_kwargs): return {"state": "TIMEOUT", "text": ""}
        def close(self): pass

    retry_bridge = RetryBridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: retry_bridge))
    assert watcher_module.service_deferred_rollover_once(second, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert len(sent) == 0
    assert second.state["rolloverTransactionId"] == prepared_id
    assert "rolloverRecoveryRetryAfter" in second.state


def test_prepared_unsent_without_valid_generation_gets_new_identity(tmp_path):
    watcher, _prompt, retired = _terminal_same_task_rollover_fixture(tmp_path)
    watcher.state.update({
        "rolloverTransactionId": "prepared-without-generation",
        "rolloverTransactionTaskId": "000080",
        "rolloverHandoverSendState": "UNSENT",
        "handoverRequested": False,
    })

    class Bridge:
        def submit_result_bounded(self, _payload): return None

    assert watcher.session_rollover.request_if_due(
        Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY",
        allow_same_task_unsent_recovery=True,
    ) is True
    assert watcher.state["rolloverTransactionId"] != "prepared-without-generation"
    assert watcher.state["rolloverTransactionId"] != retired
    assert watcher.state["rolloverTransactionGeneration"] == 1


@pytest.mark.parametrize(
    ("rollover_due", "rollover_trigger", "deferred_task"),
    [
        (False, "MEMORY_THRESHOLD", "000071"),
        (True, None, "000071"),
        (True, "RESPONSE_COUNT_FALLBACK", "000071"),
        (True, "MEMORY_THRESHOLD", "000070"),
    ],
)
def test_operator_restart_requires_current_durable_memory_trigger_and_boundary(
    tmp_path, monkeypatch, rollover_due, rollover_trigger, deferred_task
):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=rollover_due)
    watcher.state.update({
        "taskId": "000070",
        "lastCompletedTaskId": "000070",
        "nextTaskId": "000071",
        "nextPromptPath": str(prompt),
        "rolloverPending": True,
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": deferred_task,
        "rolloverAttemptedForTaskId": "000071",
        "rolloverHandoverSendState": "UNSENT",
        "handoverRequested": False,
        "rolloverTrigger": rollover_trigger,
        "architectConversationId": "OLD",
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    events = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def _assistant_entries(self):
            events.append("reconcile")
            return []
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def submit_result_bounded(self, _payload): events.append("send")
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert "send" not in events


@pytest.mark.parametrize("send_state", ["AMBIGUOUS", "ACKNOWLEDGED"])
def test_operator_restart_existing_delivery_reconciles_before_any_resend(tmp_path, monkeypatch, send_state):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({
        "taskId": "000070",
        "lastCompletedTaskId": "000070",
        "nextTaskId": "000071",
        "nextPromptPath": str(prompt),
        "rolloverPending": True,
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverDeferredForTaskId": "000071",
        "rolloverAttemptedForTaskId": "000071",
        "rolloverHandoverSendState": send_state,
        "handoverRequested": True,
        "architectConversationId": "OLD",
        "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES,
    })
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    events = []

    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def _assistant_entries(self):
            events.append("reconcile")
            return []
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def submit_result_bounded(self, _payload): events.append("send")
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert events == ["reconcile"]


def test_operator_restart_allows_one_bounded_recovery_after_deferred_rollover(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "next.txt"
    prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000069", "nextTaskId": "000070",
                          "nextPromptPath": str(prompt), "rolloverDue": True, "rolloverPending": True,
                          "rolloverMaintenanceState": "DEFERRED", "rolloverDeferredForTaskId": "000070",
                          "rolloverRecoveryState": "DEFERRED", "rolloverRecoveryAttemptCount": 1,
                          "rolloverHandoverSendState": "UNSENT", "architectConversationId": "OLD"})
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert restarted._operator_restart_rollover_recovery_available is True
    calls = []
    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD"})()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def wait_for_new_response(self, *_args, **_kwargs): return {"state": "COMPLETED", "text": "handover"}
        def close(self): pass
    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    monkeypatch.setattr(restarted.session_rollover, "request_if_due", lambda *_args, **_kwargs: calls.append("request") or True)
    monkeypatch.setattr(restarted, "process_pending_handover_response", lambda *_args: True)
    assert watcher_module.service_deferred_rollover_once(restarted, "endpoint", lambda: False, "NEXT_PROMPT_READY") is True
    assert calls == ["request"]
    assert restarted._operator_restart_rollover_recovery_available is False


def test_completed_executor_success_retires_active_pid_but_preserves_history(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "completed.txt"
    result.write_text("completed", encoding="utf-8")
    watcher.state.update({"taskId": "000052", "codexPid": 14872, "active_codex_pid": 14872,
                          "nextTaskId": "000053"})
    watcher._record_executor_success("000052", result, 0)
    assert watcher.state["executorProcessState"] == "COMPLETED_WITH_RESULT"
    assert "codexPid" not in watcher.state
    assert "active_codex_pid" not in watcher.state
    assert watcher.state["lastExecutorPid"] == 14872
    assert watcher.state["lastCompletedTaskId"] == "000052"


def test_next_prompt_ready_stale_completed_pid_does_not_block_rollover_or_launch(tmp_path, monkeypatch):
    watcher, prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({"lastCompletedTaskId": "000052", "taskId": "000052", "nextTaskId": "000053",
                          "codexPid": 14872, "executorProcessState": "COMPLETED_WITH_RESULT"})
    events = []
    def complete_rollover(*_args, **_kwargs):
        events.append("rollover")
        watcher.state.update({"rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False,
                              "handoverRequested": False, "architectConversationId": "FRESH"})
        return True
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", complete_rollover)
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: events.append("launch") or type("Process", (), {"pid": 5300})())
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: (_ for _ in ()).throw(AssertionError("stale PID must not be queried"))))
    process = watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False)
    assert process is not None
    assert events == ["rollover", "launch"]
    assert watcher.state.get("codexPid") is None
    assert watcher.state["lastExecutorPid"] == 14872
    assert prompt.read_text(encoding="utf-8") == "next bounded task"


def test_next_prompt_ready_stale_completed_pid_allows_real_rollover_service(tmp_path, monkeypatch):
    watcher, _prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({"lastCompletedTaskId": "000052", "taskId": "000052", "nextTaskId": "000053",
                          "codexPid": 14872, "executorProcessState": "COMPLETED_WITH_RESULT"})
    calls = []

    class Page:
        url = "https://chatgpt.com/c/current"

    class Bridge:
        page = Page()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def generation_visible(self): return False
        def wait_for_new_response(self, *_args, **_kwargs): return {"state": "COMPLETED", "text": "handover"}
        def close(self): pass

    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: (_ for _ in ()).throw(AssertionError("retired PID must not be queried"))))
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: Bridge()))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _watcher, _bridge, requested: requested)
    monkeypatch.setattr(watcher.session_rollover, "request_if_due", lambda *_args, **kwargs: calls.append(kwargs["executor_running"]) or True)
    monkeypatch.setattr(watcher, "process_pending_handover_response", lambda *_args: True)
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is True
    assert calls == [False]
    assert watcher.state.get("codexPid") is None


def test_next_prompt_ready_live_current_executor_still_blocks_dispatch(tmp_path, monkeypatch):
    watcher, _prompt = _next_prompt_ready_fixture(tmp_path, due=True)
    watcher.state.update({"taskId": "000053", "lastCompletedTaskId": "000052", "nextTaskId": "000054",
                          "codexPid": 5300, "executorProcessState": "RUNNING"})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda pid: pid == 5300))
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: (_ for _ in ()).throw(AssertionError("must remain blocked before attach"))))
    launches = []
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: launches.append(1))
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False) is None
    assert launches == []
    assert watcher.state["codexPid"] == 5300


def test_result_delivery_deferral_resumes_same_bridge_after_generation(tmp_path, monkeypatch):
    watcher = ready(tmp_path)
    sends = []
    class Bridge:
        def __init__(self):
            self.generating = iter([True, True, False, False])
            self.wait_called = False
        def generation_visible(self): return next(self.generating)
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def user_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def submit_result_bounded(self, payload): sends.append(payload)
        def latest_user_message(self): return sends[-1] if sends else None
        def wait_for_new_response(self, *_args, **_kwargs):
            self.wait_called = True
            raise AssertionError("decision wait is not part of delivery")
        def close(self): pass
    bridge = Bridge()
    watcher.state["architectTransportRecoveryCount"] = 7
    watcher.save()
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    result = watcher.deliver_result_with_recovery(lambda: (_ for _ in ()).throw(AssertionError("must retain bridge")), initial_bridge=bridge)
    payload, _payload_hash = watcher._result_delivery_payload()
    assert result is bridge
    assert sends == [watcher._result_delivery_wire_payload(payload, _payload_hash)]
    assert bridge.wait_called is False
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert watcher.state["architectTransportRecoveryCount"] == 0


def test_exact_unsent_payload_is_replaced_and_sent_once(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    page = FakeComposerPage()
    page.content = payload
    page_bridge = ArchitectPlaywright(page)
    watcher.state.update({"architectDeliveryPayloadHash": payload_hash, "architectSendState": "FAILED",
                          "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"})
    watcher.save()
    class Bridge:
        def generation_visible(self): return False
        def assistant_baseline(self): return {"count": 1, "text_hash": "baseline"}
        def _live_composer(self): return page_bridge._live_composer()
        def submit_result_bounded(self, value): return page_bridge.submit_result_bounded(value, timeout=1)
        def latest_user_message(self): return page.sent[-1] if page.sent else None
    watcher.deliver_result(Bridge())
    assert page.sent == [payload]
    assert watcher.state["architectSendState"] == "CONFIRMED"


def test_recovery_bridge_closes_on_success_false_and_exception():
    class Bridge:
        def __init__(self): self.close_count = 0
        def close(self): self.close_count += 1
    successful = Bridge()
    assert watcher_module.run_owned_recovery_bridge(successful, lambda _bridge: True) is True
    assert successful.close_count == 1
    false_result = Bridge()
    assert watcher_module.run_owned_recovery_bridge(false_result, lambda _bridge: False) is False
    assert false_result.close_count == 1
    raised = Bridge()
    with pytest.raises(RuntimeError, match="recovery failed"):
        watcher_module.run_owned_recovery_bridge(raised, lambda _bridge: (_ for _ in ()).throw(RuntimeError("recovery failed")))
    assert raised.close_count == 1


def test_same_payload_pre_send_failure_is_retryable_and_confirms_delivery(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"architectDeliveryPayloadHash": payload_hash, "architectSendState": "FAILED",
                          "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"})
    watcher.save()
    sends = []
    class Bridge:
        def generation_visible(self): return False
        def assistant_baseline(self): return {"count": 1, "text_hash": "baseline"}
        def submit_result_bounded(self, value): sends.append(value)
        def latest_user_message(self): return sends[-1] if sends else None
    watcher.deliver_result(Bridge())
    assert sends == [payload]
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert watcher.state["state"] == "ARCHITECT_RUNNING"


def test_ambiguous_delivery_failure_does_not_retry_without_evidence(tmp_path):
    watcher = ready(tmp_path)
    _payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"architectDeliveryPayloadHash": payload_hash, "architectSendState": "FAILED",
                          "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
    watcher.save()
    sends = []
    class Bridge:
        def generation_visible(self): return False
        def latest_user_message(self): return None
        def submit_result_bounded(self, value): sends.append(value)
    with pytest.raises(ResultSubmissionError) as error:
        watcher.deliver_result(Bridge())
    assert error.value.code == "ARCHITECT_DELIVERY_AMBIGUOUS"
    assert sends == []


def test_rollover_waits_for_new_conversation_url_before_switching(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "handoverReady": False,
                          "architectConversationId": "OLD", "pending_handover": "private handover"})
    watcher.save()
    old_page = _RolloverPage(iter(()))
    new_page = _RolloverPage(["https://chatgpt.com/", "https://chatgpt.com/", "https://chatgpt.com/c/NEW_ID", "https://chatgpt.com/c/NEW_ID"])
    class Bridge:
        page = old_page
        def open_fresh_with_handover(self, handover):
            assert handover.endswith(watcher_module.HANDOVER_CLOSE)
            return new_page
    ticks = iter(range(100))
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    assert watcher.session_rollover.complete_from_response(Bridge(), canonical_handover(watcher, "private handover")) is True
    assert watcher.state["architectConversationId"] == "NEW_ID"
    assert not watcher.state.get("pending_handover")
    assert new_page.closed is False
    assert old_page.closed is True


def test_fresh_session_ready_is_strict_and_handover_marker_is_not_fresh_ack():
    assert watcher_module.architect_session_ready("ARCHITECT_SESSION_READY")
    assert watcher_module.architect_session_ready("  ARCHITECT_SESSION_READY  ")
    assert not watcher_module.architect_session_ready("ARCHITECT_HANDOVER_READY")
    assert not watcher_module.architect_session_ready("discussion\nARCHITECT_SESSION_READY")


def test_fresh_bootstrap_adds_ready_instruction_without_changing_handover_body(tmp_path, monkeypatch):
    sent = []
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")

    class Context:
        def __init__(self, page): self.page = page
        def new_page(self): return self.page

    class Page:
        url = "https://chatgpt.com/"
        def __init__(self): self.closed = False; self.context = Context(self); self.users = []
        def goto(self, _url): pass
        def close(self): self.closed = True

    old = Page(); fresh = Page(); old.context = Context(fresh)
    def submit(_self, payload):
        sent.append(payload)
        _self.page.users.append(payload)
    monkeypatch.setattr(ArchitectPlaywright, "submit_result_bounded", submit)
    monkeypatch.setattr(ArchitectPlaywright, "exact_user_message_payload_observed", lambda _self, payload: payload in _self.page.users)
    bridge = ArchitectPlaywright(old)
    handover = canonical_handover(watcher, "complete old handover")
    assert bridge.open_fresh_with_handover(handover) is fresh
    assert sent[0].startswith(handover)
    assert sent[0].endswith("ARCHITECT_SESSION_READY")


@pytest.mark.parametrize("weak_signal", ["composer_empty", "generation_visible", "assistant_started"])
def test_fresh_weak_acknowledgement_requires_exact_user_message(tmp_path, monkeypatch, weak_signal):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True,
                          "architectConversationId": "OLD", "taskId": "000049"})
    watcher.save()
    handover = canonical_handover(watcher, "complete old handover")
    calls = []

    class Locator:
        def __init__(self, page): self.page = page
        def count(self): return self.page.assistant_count

    class Composer:
        def __init__(self, page): self.page = page
        def inner_text(self, **_): return self.page.composer_text

    class Stop:
        def __init__(self, page): self.page = page
        def count(self): return 1 if self.page.generation else 0
        def is_visible(self, **_): return self.page.generation

    class Page:
        url = "https://chatgpt.com/"
        def __init__(self):
            self.closed = False
            self.context = None
            self.composer_text = ""
            self.generation = False
            self.assistant_count = 0
        def goto(self, _url): pass
        def close(self): self.closed = True
        def get_by_role(self, role, **_):
            if role == "textbox": return Composer(self)
            return Stop(self)
        def locator(self, _selector): return Locator(self)
        def evaluate(self, script):
            if 'data-message-author-role="user"' in script: return []
            if 'data-testid="stop-button"' in script: return self.generation
            return []

    old, fresh = Page(), Page()
    class Context:
        def __init__(self): self.pages = [old]
        def new_page(self): self.pages.append(fresh); fresh.context = self; return fresh
    context = Context(); old.context = context

    def submit(_self, _payload):
        calls.append("initial")
        _self.initialSendMethod = "playwright.click"
        _self.initialSendActionReturned = True
        if weak_signal == "generation_visible":
            _self.page.generation = True
            assert _self.generation_visible() is True
        elif weak_signal == "assistant_started":
            _self.page.assistant_count = 1
            assert _self.assistant_count() == 1
        else:
            assert _self.page.composer_text == ""
    monkeypatch.setattr(ArchitectPlaywright, "submit_result_bounded", submit)

    assert watcher.session_rollover.complete_from_response(ArchitectPlaywright(old), handover) is False
    assert calls == ["initial"]
    assert len(context.pages) == 2
    assert watcher.state["state"] == "IDLE"
    assert watcher.state.get("humanRequiredReason") is None


def test_fresh_initial_send_waits_for_delayed_exact_user_message_without_fallback(tmp_path, monkeypatch):
    sent = []
    observations = []

    class Page:
        url = "https://chatgpt.com/"
        def __init__(self): self.closed = False; self.context = None
        def goto(self, _url): pass
        def close(self): self.closed = True

    old, fresh = Page(), Page()
    class Context:
        def __init__(self): self.pages = [old]
        def new_page(self): self.pages.append(fresh); fresh.context = self; return fresh
    context = Context(); old.context = context

    def submit(_self, payload):
        sent.append(payload)
        _self.initialSendMethod = "playwright.click"
        _self.initialSendActionReturned = True

    def exact(_self, _payload):
        observations.append(1)
        return len(observations) >= 3

    monkeypatch.setattr(ArchitectPlaywright, "submit_result_bounded", submit)
    monkeypatch.setattr(ArchitectPlaywright, "exact_user_message_payload_observed", exact)
    monkeypatch.setattr(ArchitectPlaywright, "reconcile_unsent_submission", lambda *_args: pytest.fail("same-page fallback was not expected"))
    bridge = ArchitectPlaywright(old)
    assert bridge.open_fresh_with_handover("handover\nARCHITECT_HANDOVER_READY") is fresh
    assert len(context.pages) == 2
    assert len(sent) == 1
    assert len(observations) == 3


def test_fresh_click_noop_uses_one_same_page_enter_and_launches_staged_task_once(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000050.txt"
    prompt.write_text("staged next task", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000049", "lastCompletedTaskId": "000049",
                          "nextTaskId": "000050", "nextPromptPath": str(prompt), "rolloverDue": True,
                          "rolloverPending": True, "handoverRequested": True, "architectConversationId": "OLD"})
    watcher.save()
    handover = canonical_handover(watcher, "complete old handover")
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)

    class Composer:
        def __init__(self, page): self.page, self.last = page, self
        def is_visible(self, **_): return True
        def is_editable(self, **_): return True
        def focus(self, **_): return None
        def inner_text(self, **_): return self.page.content
        def press(self, key, **_):
            if key == "ControlOrMeta+A": self.page.content = ""
            elif key == "Backspace": self.page.content = ""
            elif key == "Enter": self.page.submit()

    class Button:
        last = None
        def __init__(self, page): self.page, self.last = page, self
        def is_visible(self, **_): return True
        def is_enabled(self, **_): return True
        def click(self, **_): return None  # production defect: successful no-op

    class Count:
        def __init__(self, page, selector): self.page, self.selector = page, selector
        def count(self): return len(self.page.assistants) if "assistant" in self.selector else 0

    class Page:
        def __init__(self, url):
            self.url, self.closed, self.context = url, False, None
            self.content, self.users, self.assistants = "", [], []
            page = self
            self.keyboard = type("Keyboard", (), {"insert_text": lambda _self, value: setattr(page, "content", value)})()
        def goto(self, _url): self.url = "https://chatgpt.com/"
        def close(self): self.closed = True
        def submit(self):
            self.users.append(self.content)
            self.content = ""
            self.url = "https://chatgpt.com/c/FRESH"
            self.assistants.append({"id": "ready", "text": "ARCHITECT_SESSION_READY"})
        def get_by_role(self, role, **kwargs):
            if role == "textbox": return Composer(self)
            if role == "button" and "stop" in str(kwargs.get("name", "")).lower(): return type("Stop", (), {"count": lambda _self: 0, "is_visible": lambda _self, **_: False})()
            return Button(self)
        def locator(self, selector): return Count(self, selector)
        def evaluate(self, script):
            if 'data-message-author-role="user"' in script: return list(self.users)
            if 'data-message-author-role="assistant"' in script: return list(self.assistants)
            if "stop-button" in script: return False
            return False

    old = Page("https://chatgpt.com/c/OLD")
    fresh = Page("https://chatgpt.com/")
    class Context:
        def __init__(self): self.pages = [old]
        def new_page(self): self.pages.append(fresh); fresh.context = self; return fresh
    context = Context()
    old.context = context
    ticks = iter([index * 0.01 for index in range(10000)])
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    reconcile_results = []
    original_reconcile = ArchitectPlaywright.reconcile_unsent_submission
    def record_reconcile(self, payload):
        result = original_reconcile(self, payload)
        reconcile_results.append(result)
        return result
    monkeypatch.setattr(ArchitectPlaywright, "reconcile_unsent_submission", record_reconcile)
    bridge = ArchitectPlaywright(old)
    assert watcher.session_rollover.complete_from_response(bridge, handover) is True, reconcile_results
    assert len(context.pages) == 2
    assert fresh.users == [bootstrap]
    assert watcher.state["architectConversationId"] == "FRESH"
    assert old.closed is True and fresh.closed is False
    assert watcher.state["nextTaskId"] == "000050"
    launches = []
    watcher.launch_next = lambda _launch: launches.append(1) or type("Process", (), {"pid": 5050})()
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False) is not None
    assert launches == [1]


def test_fresh_button_and_enter_noop_fail_closed_without_second_tab(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000049", "lastCompletedTaskId": "000049",
                          "nextTaskId": "000050", "rolloverDue": True, "rolloverPending": True,
                          "handoverRequested": True, "architectConversationId": "OLD"})
    watcher.save()
    handover = canonical_handover(watcher, "complete old handover")

    class Page:
        def __init__(self, url):
            self.url, self.closed, self.context, self.content = url, False, None, ""
            page = self
            self.keyboard = type("Keyboard", (), {"insert_text": lambda _self, value: setattr(page, "content", value)})()
        def goto(self, _url): self.url = "https://chatgpt.com/"
        def close(self): self.closed = True
        def get_by_role(self, role, **kwargs):
            if role == "textbox":
                page = self
                return type("Composer", (), {"__init__": lambda _s: setattr(_s, "last", _s), "is_visible": lambda _s, **_: True, "is_editable": lambda _s, **_: True,
                    "focus": lambda _s, **_: None, "inner_text": lambda _s, **_: page.content,
                    "press": lambda _s, key, **_: (setattr(page, "content", "") if key in {"ControlOrMeta+A", "Backspace"} else None)})()
            if role == "button" and "stop" in str(kwargs.get("name", "")).lower():
                return type("Stop", (), {"count": lambda _s: 0, "is_visible": lambda _s, **_: False})()
            return type("Send", (), {"__init__": lambda _s: setattr(_s, "last", _s), "is_visible": lambda _s, **_: True, "is_enabled": lambda _s, **_: True,
                "click": lambda _s, **_: None})()
        def locator(self, _selector): return type("Count", (), {"count": lambda _s: 0})()
        def evaluate(self, script):
            if "stop-button" in script: return False
            if 'data-message-author-role="user"' in script: return []
            if 'data-message-author-role="assistant"' in script: return []
            return False

    old, fresh = Page("https://chatgpt.com/c/OLD"), Page("https://chatgpt.com/")
    class Context:
        def __init__(self): self.pages = [old]
        def new_page(self): self.pages.append(fresh); fresh.context = self; return fresh
    context = Context(); old.context = context
    ticks = iter([float(index) for index in range(1000)])
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    assert watcher.session_rollover.complete_from_response(ArchitectPlaywright(old), handover) is False
    assert len(context.pages) == 2
    assert fresh.closed is False
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state.get("humanRequiredReason") is None


def test_unidentified_prior_fresh_page_never_creates_replacement_tab_after_restart(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000049", "lastCompletedTaskId": "000049",
                          "nextTaskId": "000050", "rolloverDue": True, "rolloverPending": True,
                          "handoverRequested": True, "rolloverFreshPageCreated": True,
                          "architectConversationId": "OLD"})
    watcher.save()
    calls = []
    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/OLD", "context": type("Context", (), {"pages": []})()})()
        def open_fresh_with_handover(self, _handover): calls.append(1); return None
    assert watcher.session_rollover.complete_from_response(Bridge(), canonical_handover(watcher, "handover")) is False
    assert calls == []
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state.get("humanRequiredReason") is None


def test_ambiguous_fresh_submission_persists_candidate_without_closing_it(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True,
                          "architectConversationId": "OLD", "taskId": "000049"})
    watcher.save()

    class Context:
        def __init__(self, fresh): self.fresh = fresh
        def new_page(self): return self.fresh

    class Page:
        def __init__(self, url): self.url = url; self.closed = False; self.context = None
        def goto(self, _url): pass
        def close(self): self.closed = True

    old = Page("https://chatgpt.com/c/OLD")
    fresh = Page("https://chatgpt.com/c/CANDIDATE")
    old.context = Context(fresh)
    monkeypatch.setattr(ArchitectPlaywright, "submit_result_bounded", lambda _self, _payload: (_ for _ in ()).throw(ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT")))
    bridge = ArchitectPlaywright(old)
    assert watcher.session_rollover.complete_from_response(bridge, canonical_handover(watcher, "private handover")) is False
    assert fresh.closed is False
    assert old.closed is False
    assert watcher.state["rolloverFreshCandidateConversationId"] == "CANDIDATE"
    assert watcher.state["rolloverFreshCandidateState"] == "SUBMISSION_AMBIGUOUS"
    assert watcher.state["handoverRequested"] is True


def test_ambiguous_fresh_candidate_is_reused_before_new_tab_creation(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True,
                          "architectConversationId": "OLD", "taskId": "000049",
                          "nextTaskId": "000050", "rolloverDue": True})
    watcher.save()
    old_page = _AckPage("https://chatgpt.com/c/OLD")
    new_page = _AckPage("https://chatgpt.com/c/NEW", entries=[{"id": "ack", "text": "ARCHITECT_SESSION_READY"}])
    context = type("Context", (), {"pages": [old_page, new_page]})()
    old_page.context = context
    new_page.context = context
    calls = []

    class Bridge:
        page = old_page
        _fresh_candidate_page = None
        _fresh_candidate_submission_ambiguous = False
        def open_fresh_with_handover(self, _response):
            calls.append(1)
            raise AssertionError("existing candidate must be reused")

    bridge = Bridge()
    watcher.state.update({"rolloverFreshCandidateConversationId": "NEW",
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS",
                          "pending_handover": canonical_handover(watcher, "complete old handover", task_id="000050")})
    watcher.save()
    assert watcher.session_rollover.complete_from_response(bridge, watcher.state["pending_handover"]) is True
    assert calls == []
    assert watcher.state["architectConversationId"] == "NEW"
    assert old_page.closed is True
    assert new_page.closed is False
    assert watcher.state["nextTaskId"] == "000050"


def test_fresh_candidate_reacquisition_uses_bootstrap_and_ready_proof(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "complete old handover")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "handoverRequested": True, "rolloverPending": True,
                          "rolloverDue": True, "rolloverAttemptedForTaskId": "000049", "architectConversationId": "OLD",
                          "taskId": "000049", "nextTaskId": "000050", "pending_handover": handover,
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS"})
    watcher.save()
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)

    class Page:
        def __init__(self, url, users, assistants):
            self.url, self.users, self.assistants, self.closed = url, users, assistants, False
            self.context = None
        def evaluate(self, script):
            if 'data-message-author-role="user"' in script: return self.users
            if 'data-message-author-role="assistant"' in script: return self.assistants
            if "stop-button" in script: return False
            return False
        def close(self): self.closed = True

    old = Page("https://chatgpt.com/c/OLD", [], [])
    valid = Page("https://chatgpt.com/c/VALID", [bootstrap], [{"id": "ready", "text": "ARCHITECT_SESSION_READY"}])
    unrelated = Page("https://chatgpt.com/c/OTHER", ["unrelated"], [{"id": "x", "text": "discussion"}])
    context = type("Context", (), {"pages": [old, valid, unrelated]})()
    old.context = valid.context = unrelated.context = context
    bridge = ArchitectPlaywright(old)
    assert watcher.session_rollover._existing_fresh_candidate_page(bridge, handover) is valid
    assert watcher.state["rolloverFreshBootstrapPayloadHash"] == hashlib.sha256(bootstrap.encode()).hexdigest()


def test_stale_persisted_candidate_id_is_corrected_by_content_proof_and_reused(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "complete old handover")
    old_id = "6aac1369-7e48-83ec-b2bc-73797a88e6f5"
    stale_id = "WEB:204cb572-6571-4b81-9bbd-25f5cedcb073"
    fresh_id = "6aac98c9-571c-83ec-b11a-4bb8bc7744d1"
    prompt = tmp_path / "next.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000052", "lastCompletedTaskId": "000052",
                          "nextTaskId": "000053", "nextPromptPath": str(prompt), "targetWorktree": str(tmp_path / "worktree"),
                          "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": True,
                          "rolloverAttemptedForTaskId": "000052", "handoverRequested": True, "handoverReady": False,
                          "pending_handover": handover, "rolloverHandoverSendState": "AMBIGUOUS",
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS",
                          "rolloverFreshCandidateConversationId": stale_id, "architectConversationId": old_id})
    watcher.save()
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)

    class Page:
        def __init__(self, url, users, assistants):
            self.url, self.users, self.assistants, self.closed, self.context = url, users, assistants, False, None
        def evaluate(self, script):
            if "stop-button" in script: return False
            if 'data-message-author-role="user"' in script: return self.users
            if 'data-message-author-role="assistant"' in script: return self.assistants
            return False
        def close(self): self.closed = True

    old = Page(f"https://chatgpt.com/c/{old_id}", [], [{"id": "handover", "text": handover}])
    fresh = Page(f"https://chatgpt.com/c/{fresh_id}", [bootstrap], [{"id": "ready", "text": "ARCHITECT_SESSION_READY"}])
    unrelated = Page("https://chatgpt.com/c/unrelated", ["ordinary chat"], [{"text": "discussion"}])
    context = type("Context", (), {"pages": [old, fresh, unrelated]})()
    old.context = fresh.context = unrelated.context = context
    opened, sends, launches = [], [], []

    class Bridge:
        page = old
        _fresh_candidate_submission_ambiguous = True
        def _assistant_entries(self): return [{"id": "handover", "text": handover}]
        def open_fresh_with_handover(self, _handover): opened.append(1); raise AssertionError("must reuse proven candidate")

    bridge = Bridge()
    monkeypatch.setattr(watcher, "process_pending_handover_response", lambda current_bridge, response: watcher.session_rollover.complete_from_response(current_bridge, response))
    assert watcher.session_rollover._existing_fresh_candidate_page(bridge, handover) is fresh
    assert watcher.state["rolloverFreshCandidateConversationId"] == fresh_id
    assert watcher.session_rollover.reconcile_pending_handover(bridge) is True
    assert watcher.state["architectConversationId"] == fresh_id
    assert watcher.state["rolloverDue"] is False
    assert watcher.state["rolloverPending"] is False
    assert opened == [] and sends == []
    assert old.closed is True and fresh.closed is False
    assert watcher.state["nextTaskId"] == "000053"
    monkeypatch.setattr(watcher, "launch_next", lambda _launch: launches.append(1) or type("Process", (), {"pid": 5300})())
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: None, "endpoint", lambda: False) is not None
    assert launches == [1]


def test_stale_persisted_candidate_without_proof_fails_closed_with_diagnostic(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "handover")
    watcher.state.update({"rolloverDue": True, "rolloverPending": True, "handoverRequested": True,
                          "architectConversationId": "OLD", "rolloverFreshCandidateConversationId": "STALE",
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS"})
    watcher.save()

    class Page:
        url = "https://chatgpt.com/c/OTHER"
        context = None
        def evaluate(self, _script): return []
    page = Page()
    page.context = type("Context", (), {"pages": [page]})()
    bridge = ArchitectPlaywright(page)
    assert watcher.session_rollover._existing_fresh_candidate_page(bridge, handover) is None
    assert watcher.state["rolloverFreshCandidateDiscoveryState"] == "NOT_FOUND"


def test_fresh_candidate_requires_exact_bootstrap_and_ready(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "handover")
    watcher.state.update({"rolloverPending": True, "rolloverDue": True, "handoverRequested": True,
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS", "architectConversationId": "OLD"})
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)

    class Page:
        def __init__(self, url, users, assistants): self.url, self.users, self.assistants, self.context = url, users, assistants, None
        def evaluate(self, script):
            if 'data-message-author-role="user"' in script: return self.users
            if 'data-message-author-role="assistant"' in script: return self.assistants
            return False

    old = Page("https://chatgpt.com/c/OLD", [], [])
    wrong = Page("https://chatgpt.com/c/WRONG", [bootstrap + " extra"], [{"text": "ARCHITECT_SESSION_READY"}])
    no_ready = Page("https://chatgpt.com/c/NO_READY", [bootstrap], [{"text": "ordinary prose"}])
    context = type("Context", (), {"pages": [old, wrong, no_ready]})()
    for page in (old, wrong, no_ready): page.context = context
    assert watcher.session_rollover._existing_fresh_candidate_page(ArchitectPlaywright(old), handover) is None


def test_ready_unidentified_candidate_is_reused_and_committed_without_new_tab(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "complete old handover")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "handoverRequested": True, "rolloverPending": True,
                          "rolloverDue": True, "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS",
                          "architectConversationId": "OLD", "taskId": "000049", "nextTaskId": "000050",
                          "pending_handover": handover})
    watcher.save()
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)

    class Page:
        def __init__(self, url, users, assistants):
            self.url, self.users, self.assistants, self.closed, self.context = url, users, assistants, False, None
        def evaluate(self, script):
            if "stop-button" in script: return False
            if 'data-message-author-role="user"' in script: return self.users
            if 'data-message-author-role="assistant"' in script: return self.assistants
            return False
        def close(self): self.closed = True

    old = Page("https://chatgpt.com/c/OLD", [], [])
    fresh = Page("https://chatgpt.com/c/FRESH", [bootstrap], [{"id": "ready", "text": "ARCHITECT_SESSION_READY"}])
    context = type("Context", (), {"pages": [old, fresh]})()
    old.context = fresh.context = context
    opened = []
    class Bridge:
        page = old
        _fresh_candidate_submission_ambiguous = True
        def open_fresh_with_handover(self, _handover):
            opened.append(1)
            raise AssertionError("proven candidate must be reused")

    bridge = Bridge()
    assert watcher.session_rollover.complete_from_response(bridge, handover) is True
    assert opened == []
    assert watcher.state["architectConversationId"] == "FRESH"
    assert old.closed is True
    assert fresh.closed is False
    assert watcher.state["nextTaskId"] == "000050"


def test_multiple_proven_fresh_candidates_fail_closed(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "handover")
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)
    watcher.state.update({"rolloverPending": True, "rolloverDue": True, "handoverRequested": True,
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS", "architectConversationId": "OLD"})

    class Page:
        def __init__(self, url): self.url, self.context = url, None
        def evaluate(self, script):
            if 'data-message-author-role="user"' in script: return [bootstrap]
            if 'data-message-author-role="assistant"' in script: return [{"text": "ARCHITECT_SESSION_READY"}]
            return False

    old = Page("https://chatgpt.com/c/OLD")
    first, second = Page("https://chatgpt.com/c/FIRST"), Page("https://chatgpt.com/c/SECOND")
    context = type("Context", (), {"pages": [old, first, second]})()
    for page in (old, first, second): page.context = context
    assert watcher.session_rollover._existing_fresh_candidate_page(ArchitectPlaywright(old), handover) is None
    assert watcher.state["rolloverFreshCandidateDiscoveryState"] == "AMBIGUOUS"


def test_lost_handover_authority_is_reconstructed_before_candidate_discovery(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    handover = canonical_handover(watcher, "complete old handover")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "rolloverDue": True, "rolloverPending": True,
                          "rolloverAttemptedForTaskId": "000049", "handoverRequested": False,
                          "taskId": "000049", "nextTaskId": "000050", "architectConversationId": "OLD",
                          "rolloverFreshCandidateState": "SUBMISSION_AMBIGUOUS"})
    watcher.save()
    bootstrap = watcher_module.fresh_architect_bootstrap_payload(handover)

    class Page:
        def __init__(self, url, users, assistants): self.url, self.users, self.assistants, self.closed, self.context = url, users, assistants, False, None
        def evaluate(self, script):
            if "stop-button" in script: return False
            if 'data-message-author-role="user"' in script: return self.users
            if 'data-message-author-role="assistant"' in script: return self.assistants
            return False
        def close(self): self.closed = True

    old = Page("https://chatgpt.com/c/OLD", [], [{"id": "handover", "text": handover}])
    fresh = Page("https://chatgpt.com/c/FRESH", [bootstrap], [{"id": "ready", "text": "ARCHITECT_SESSION_READY"}])
    context = type("Context", (), {"pages": [old, fresh]})()
    old.context = fresh.context = context
    opened, sends = [], []
    class Bridge:
        page = old
        _fresh_candidate_submission_ambiguous = True
        def _assistant_entries(self): return [{"id": "handover", "text": handover}]
        def open_fresh_with_handover(self, _handover): opened.append(1); raise AssertionError("must reuse fresh candidate")
        def submit_result_bounded(self, message): sends.append(message)

    bridge = Bridge()
    assert watcher.session_rollover.reconcile_pending_handover(bridge) is True
    assert watcher.state["handoverRequested"] is False
    assert watcher.state["architectConversationId"] == "FRESH"
    assert opened == [] and sends == []
    assert old.closed is True and fresh.closed is False
    assert watcher.state["nextTaskId"] == "000050"


def test_rollover_timeout_closes_only_fresh_page_and_preserves_authority(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "ARCHITECT_RUNNING", "handoverRequested": True, "rolloverPending": True,
                          "handoverReady": False, "architectConversationId": "OLD", "pending_handover": "private handover"})
    watcher.save()
    old_page = _RolloverPage(iter(()))
    new_page = _RolloverPage(["https://chatgpt.com/"] * 20)
    class Bridge:
        page = old_page
        def open_fresh_with_handover(self, _handover): return new_page
    ticks = iter(range(100))
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    assert watcher.session_rollover.complete_from_response(Bridge(), canonical_handover(watcher, "private handover")) is False
    assert new_page.closed is True
    assert old_page.closed is False
    assert watcher.state["architectConversationId"] == "OLD"
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["handoverRequested"] is True
    assert watcher.state["pending_handover"].endswith(watcher_module.HANDOVER_CLOSE)
    assert watcher.state["state"] == "ARCHITECT_RUNNING"


def test_fresh_handover_submission_failure_closes_fresh_page(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD",
                          "pending_handover": "private handover"})
    watcher.save()
    old_page = FakeComposerPage()
    fresh_page = FakeComposerPage(available=False)
    fresh_page.goto = lambda _url: None
    fresh_page.close = lambda: setattr(fresh_page, "closed", True)
    fresh_page.closed = False
    old_page.context = type("Context", (), {"new_page": lambda _self: fresh_page})()
    bridge = ArchitectPlaywright(old_page)
    assert watcher.session_rollover.complete_from_response(bridge, canonical_handover(watcher, "private handover")) is False
    assert fresh_page.closed is True
    assert bridge.page is old_page
    assert watcher.state["architectConversationId"] == "OLD"
    assert watcher.state["pending_handover"].endswith(watcher_module.HANDOVER_CLOSE)


def test_rollover_failure_logs_phase_class_and_bounded_flattened_message(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    logger, run_id, log_path = watcher_module.initialize_runtime_logging(watcher.state_dir)
    watcher.runtime_logger, watcher.runtime_run_id = logger, run_id
    class Bridge:
        page = _RolloverPage(iter(()))
        def open_fresh_with_handover(self, _response):
            raise RuntimeError("some detailed rollover failure\nwith a second line" + "x" * 700)
    events = []
    assert watcher.session_rollover.complete_from_response(Bridge(), canonical_handover(watcher, "handover"), emit=events.append) is False
    for handler in logger.handlers:
        handler.flush()
    log = Path(log_path).read_text(encoding="utf-8")
    assert "event=ARCHITECT_SESSION_ROLLOVER_FAILED" in log
    assert "error=ARCHITECT_SESSION_ROLLOVER_FAILED" in log
    assert "errorClass=RuntimeError" in log
    assert "phase=OPEN_FRESH_WITH_HANDOVER" in log
    assert "errorMessage=some detailed rollover failure with a second linex" in log
    error_message = log.split("errorMessage=", 1)[1].split(" phase=", 1)[0]
    assert len(error_message) == 500
    assert "\n" not in error_message and "\r" not in error_message
    assert any(event.startswith("ARCHITECT_SESSION_ROLLOVER_FAILED phase=OPEN_FRESH_WITH_HANDOVER") for event in events)


def test_rollover_timeout_preserves_stable_error_code(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "architectConversationId": "OLD"})
    watcher.save()
    class Bridge:
        page = _RolloverPage(iter(()))
        def open_fresh_with_handover(self, _response):
            return _RolloverPage(["https://chatgpt.com/"] * 20)
    ticks = iter(range(100))
    monkeypatch.setattr(watcher_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    events = []
    assert watcher.session_rollover.complete_from_response(Bridge(), canonical_handover(watcher, "handover"), emit=events.append) is False
    assert any(event.startswith("ARCHITECT_NEW_CONVERSATION_ID_TIMEOUT ") for event in events)


def test_architect_handover_ready_accepts_plain_and_escaped_terminal_markers():
    assert watcher_module.architect_handover_ready("handover\nARCHITECT_HANDOVER_READY")
    assert watcher_module.architect_handover_ready(r"handover\nARCHITECT\_HANDOVER\_READY")
    assert watcher_module.architect_handover_ready("handover\nARCHITECT_HANDOVER_READY\nARCHITECT_RESPONSE_COMPLETE")
    assert watcher_module.architect_handover_ready("handover\nARCHITECT\\_HANDOVER\\_READY\nARCHITECT_RESPONSE_COMPLETE")


def test_architect_handover_ready_requires_terminal_marker():
    assert not watcher_module.architect_handover_ready("ARCHITECT_HANDOVER_READY\nmore content")
    assert not watcher_module.architect_handover_ready("body ARCHITECT\\_HANDOVER\\_READY more")


def test_pending_handover_invalid_response_never_enters_format_recovery(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"handoverRequested": True, "rolloverPending": True, "rolloverTrigger": "MEMORY_THRESHOLD",
                          "taskId": "000040", "lastCompletedTaskId": "000040"})
    watcher.save()
    monkeypatch.setattr(watcher.session_rollover, "complete_from_response", lambda *_: False)
    monkeypatch.setattr(watcher, "request_format_recovery", lambda *_: (_ for _ in ()).throw(AssertionError("format recovery forbidden")))
    assert watcher.process_pending_handover_response(object(), "not a handover") is False
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_HANDOVER_RESPONSE_INVALID"
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["handoverRequested"] is True
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"


def test_incident_recovery_uses_newest_valid_handover_and_ignores_stop(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "task-000040.txt"
    result.write_text("captured task 000040 result", encoding="utf-8")
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED",
                          "handoverRequested": True, "rolloverPending": True, "rolloverTrigger": "MEMORY_THRESHOLD",
                          "taskId": "000040", "lastCompletedTaskId": "000040", "executorResultPath": str(result)})
    watcher.save()
    escaped = r"complete handover\nARCHITECT\_HANDOVER\_READY"
    contaminated_stop = "<ORCHESTRATOR_RESULT>\naction=STOP\ntaskId=000040\n</ORCHESTRATOR_RESULT>"
    class Bridge:
        def _assistant_entries(self):
            return [{"id": "handover", "text": escaped}, {"id": "stop", "text": contaminated_stop}]
    complete_calls, deliveries = [], []
    def complete(_bridge, response):
        complete_calls.append(response)
        watcher.state.update({"handoverRequested": False, "rolloverPending": False})
        return True
    monkeypatch.setattr(watcher.session_rollover, "complete_from_response", complete)
    monkeypatch.setattr(watcher, "deliver_result", lambda bridge: deliveries.append(bridge))
    assert watcher.recover_pending_rollover_handover(Bridge()) is True
    assert complete_calls == [escaped]
    assert len(deliveries) == 1
    assert watcher.state["taskId"] == "000040"
    assert watcher.state["lastCompletedTaskId"] == "000040"
    assert Path(watcher.state["executorResultPath"]).read_text(encoding="utf-8") == "captured task 000040 result"
    assert watcher.state["state"] == "RESULT_READY"


def test_incident_recovery_without_valid_handover_stays_human_required(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "task-000040.txt"
    result.write_text("captured task 000040 result", encoding="utf-8")
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED",
                          "handoverRequested": True, "rolloverPending": True, "taskId": "000040",
                          "lastCompletedTaskId": "000040", "executorResultPath": str(result)})
    watcher.save()
    class Bridge:
        def _assistant_entries(self):
            return [{"id": "stop", "text": "<ORCHESTRATOR_RESULT> action=STOP taskId=000040 </ORCHESTRATOR_RESULT>"}]
    called = []
    monkeypatch.setattr(watcher.session_rollover, "complete_from_response", lambda *_: called.append(1))
    monkeypatch.setattr(watcher, "launch_next", lambda *_: (_ for _ in ()).throw(AssertionError("executor forbidden")))
    assert watcher.recover_pending_rollover_handover(Bridge()) is False
    assert called == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED"
    assert watcher.state["taskId"] == "000040"


def test_memory_telemetry_is_throttled_deduplicated_and_recovers(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    logger, run_id, log_path = watcher_module.initialize_runtime_logging(watcher.state_dir)
    watcher.runtime_logger, watcher.runtime_run_id = logger, run_id
    mib = 1024 * 1024
    samples = iter([100 * mib, 110 * mib,
                    RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_TIMEOUT"),
                    RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_TIMEOUT"),
                    115 * mib, 180 * mib, 1025 * mib])
    for _ in range(7):
        watcher.session_rollover.sample_memory(lambda: next(samples))
    for handler in logger.handlers:
        handler.flush()
    log = Path(log_path).read_text(encoding="utf-8")
    assert log.count("event=ARCHITECT_MEMORY_SAMPLE ") == 4
    assert "memoryMiB=100" in log and "memoryMiB=115" in log and "memoryMiB=180" in log and "memoryMiB=1025" in log
    assert log.count("event=ARCHITECT_MEMORY_THRESHOLD_TELEMETRY ") == 1
    assert log.count("event=ARCHITECT_MEMORY_SAMPLE_FAILED ") == 1


def test_localfirst_does_not_bind_browser_tree_as_session_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHITECT_BROWSER_ROOT_PID", "4242")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert watcher.state["architectMemoryOwnership"] == "UNBOUND"
    assert watcher.architect_memory_reader is None


def test_attached_architect_page_binds_session_memory(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    class Bridge:
        page = type("Page", (), {"url": "https://chatgpt.com/c/11111111-1111-1111-1111-111111111111"})()
        def current_session_memory_bytes(self): return 123
    assert watcher.bind_architect_session_memory(Bridge(), "11111111-1111-1111-1111-111111111111")
    assert watcher.state["architectMemoryOwnership"] == "SESSION_SCOPED"
    assert watcher.state["architectMemoryOwnershipSource"] == "ARCHITECT_PAGE_RENDERER"
    assert watcher.state["architectMemorySessionId"] == "11111111-1111-1111-1111-111111111111"
    assert watcher.architect_memory_reader is not None


def test_obsolete_process_owner_binding_is_rejected(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    with pytest.raises(RuntimeError, match="ARCHITECT_SESSION_MEMORY_REQUIRES_ATTACHED_PAGE"):
        watcher.bind_architect_memory_owner("http://127.0.0.1:9333", lambda _: None)


def test_attached_page_memory_reader_drives_threshold_rollover(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    class Bridge:
        def current_session_memory_bytes(self): return watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES
    bridge = Bridge()
    watcher.bind_architect_session_memory(bridge, "11111111-1111-1111-1111-111111111111")
    assert watcher.session_rollover.sample_memory() == "MEMORY_THRESHOLD"


def test_response_count_does_not_drive_threshold_rollover(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state["architectResponseCount"] = 30
    watcher.architect_memory_reader = lambda: watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES - 1
    assert watcher.session_rollover.sample_memory() is None
    assert watcher.state.get("rolloverDue") is not True


def test_process_tree_memory_json_boundary_includes_descendants_only(monkeypatch):
    captured = []
    monkeypatch.setattr(watcher_module.os, "name", "nt")
    monkeypatch.setattr(watcher_module.subprocess, "check_output", lambda command, **kwargs: captured.append(command[-1]) or json.dumps([
        {"pid": 100, "parentPid": 1, "workingSet": 10},
        {"pid": 101, "parentPid": 100, "workingSet": 20},
        {"pid": 102, "parentPid": 101, "workingSet": 30},
        {"pid": 900, "parentPid": 1, "workingSet": 9000},
    ]))
    assert architect_process_tree_memory_bytes(100) == 60
    assert "`t" not in captured[0]
    assert "ConvertTo-Json" in captured[0]


def test_process_tree_memory_sampler_fails_closed_on_bad_snapshots():
    for rows, error in [
        ([], "ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED"),
        ([{"pid": 2, "parentPid": 1}], "ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED"),
        ([{"pid": 2, "parentPid": 1, "workingSet": 1}], "ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE"),
        ([{"pid": 2, "parentPid": 1, "workingSet": 1}, "bad"], "ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED"),
    ]:
        with pytest.raises(RuntimeError, match=error):
            architect_process_tree_memory_bytes(100, rows)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell qualification")
def test_live_windows_current_process_memory_sampler_is_positive():
    assert architect_process_tree_memory_bytes(os.getpid()) > 0


def test_resident_recovery_reconciles_ambiguous_delivery_without_restart(tmp_path):
    watcher = ready(tmp_path)
    sent = []
    class FirstBridge:
        last_send_method = "playwright.click"
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def submit_result_bounded(self, message):
            sent.append(message)
            raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED")
        def close(self): pass
    first = FirstBridge()
    class ReattachedBridge:
        def latest_user_message(self): return sent[0]
        def assistant_baseline(self): return {"count": 2, "entries": []}
        def submit_result_bounded(self, _message): raise AssertionError("duplicate delivery")
        def close(self): pass
    attachments = []
    bridge = watcher.deliver_result_with_recovery(lambda: attachments.append(1) or ReattachedBridge(), initial_bridge=first)
    assert bridge is not None
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert sent and attachments == [1]


def test_resident_recovery_exhausts_ambiguous_delivery_without_proof(tmp_path):
    watcher = ready(tmp_path)
    sends = []
    class FirstBridge:
        last_send_method = "playwright.click"
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def submit_result_bounded(self, message):
            sends.append(message)
            raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED")
        def close(self): pass
    class ReattachedBridge:
        def latest_user_message(self): return "different delivery"
        def assistant_baseline(self): return {"count": 2, "entries": []}
        def submit_result_bounded(self, message): sends.append(message)
        def close(self): pass
    watcher.deliver_result_with_recovery(lambda: ReattachedBridge(), initial_bridge=FirstBridge())
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert len(sends) == 1


def test_resident_recovery_exhaustion_preserves_result_and_stops_safely(tmp_path):
    watcher = ready(tmp_path)
    payload = Path(watcher.state["executorResultPath"]).read_bytes()
    class FailedBridge:
        last_send_method = "playwright.click"
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def submit_result_bounded(self, _message): raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED")
        def latest_user_message(self): return None
        def close(self): pass
    assert watcher.deliver_result_with_recovery(lambda: FailedBridge(), max_attempts=3, initial_bridge=FailedBridge()) is None
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"
    assert Path(watcher.state["executorResultPath"]).read_bytes() == payload
    assert watcher.state["architectTransportRecoveryCount"] == 3


def test_normal_playwright_click_sends_exact_text():
    page = FakeComposerPage()
    ArchitectPlaywright(page).submit_result_bounded("click result", timeout=1)
    assert page.sent == ["click result"]


def test_actionability_timeout_uses_playwright_enter_fallback():
    page = FakeComposerPage(click_actionable=False)
    ArchitectPlaywright(page).submit_result_bounded("fallback result", timeout=1)
    assert page.sent == ["fallback result"]


def test_disabled_send_is_never_forced():
    page = FakeComposerPage(logical_enabled=False)
    with pytest.raises(ResultSubmissionError) as error:
        ArchitectPlaywright(page).submit_result_bounded("blocked", timeout=1)
    assert error.value.code == "ARCHITECT_SEND_CONTROL_DISABLED"
    assert page.native_submissions == 0


def test_unavailable_composer_fails_without_executor_rerun():
    page = FakeComposerPage(available=False)
    with pytest.raises(ResultSubmissionError) as error:
        ArchitectPlaywright(page).submit_result_bounded("same result", timeout=0.1)
    assert error.value.code == "ARCHITECT_COMPOSER_UNAVAILABLE"
    assert page.sent == []


def test_population_timeout_preserves_result_ready_payload_for_identical_retry(tmp_path):
    watcher = ready(tmp_path)
    payload = Path(watcher.state["executorResultPath"]).read_text(encoding="utf-8")
    page = FakeComposerPage(focus=False)
    with pytest.raises(ResultSubmissionError):
        ArchitectPlaywright(page).submit_result_bounded(payload, timeout=1)
    assert LocalFirstOrchestrator(str(tmp_path), watcher.state_dir).state["state"] == "RESULT_READY"
    assert Path(watcher.state["executorResultPath"]).read_text(encoding="utf-8") == payload
    retry = FakeComposerPage()
    ArchitectPlaywright(retry).submit_result_bounded(payload, timeout=1)
    assert retry.sent == [payload]


def test_native_submit_without_transition_preserves_payload(tmp_path):
    watcher = ready(tmp_path)
    payload = Path(watcher.state["executorResultPath"]).read_text(encoding="utf-8")
    page = FakeComposerPage(transition=False)
    with pytest.raises(ResultSubmissionError) as error:
        ArchitectPlaywright(page).submit_result_bounded(payload, timeout=1)
    assert error.value.code == "ARCHITECT_SUBMISSION_ACK_TIMEOUT"
    assert page.native_submissions == 1
    assert LocalFirstOrchestrator(str(tmp_path), watcher.state_dir).state["state"] == "RESULT_READY"
    assert Path(watcher.state["executorResultPath"]).read_text(encoding="utf-8") == payload


def test_next_prompt_persisted_and_launches_exactly_one_codex_child(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    watcher.accept_architect_response(envelope("recovery-safety-task", prompt=configured_project_prompt(base, head)))
    launches = []

    class Process:
        pid = 4321

    watcher.launch_next(lambda prompt, path: (launches.append(prompt) or Process()))
    assert len(launches) == 1
    assert watcher.launch_next(lambda *_: (_ for _ in ()).throw(AssertionError("duplicate launch"))) is None


class FakeResponseBridge(ArchitectPlaywright):
    def __init__(self, entries, generation):
        self.entries = iter(entries)
        self.generation = iter(generation)
        self.polls = 0

    def _assistant_entries(self):
        self.polls += 1
        return next(self.entries)

    def generation_visible(self):
        return next(self.generation)


def test_architect_generation_has_no_wall_clock_failure_authority(monkeypatch):
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    bridge = FakeResponseBridge(
        [[{"id": "a", "text": "draft"}], [{"id": "a", "text": "final " + watcher_module.COMPLETE}]],
        [True, False],
    )
    observed = bridge.wait_for_new_response({"count": 0, "text_hash": "", "entries": []}, poll_interval=999999)
    assert observed["state"] == "COMPLETED"
    assert bridge.polls == 2
    assert "timeout" not in inspect.signature(ArchitectPlaywright.wait_for_new_response).parameters


def test_architect_response_requires_stopped_stable_final_envelope(monkeypatch):
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    response = envelope("task-1", prompt="next") + "\nARCHITECT_RESPONSE_COMPLETE"
    bridge = FakeResponseBridge(
        [[{"id": "a", "text": "draft"}], [{"id": "a", "text": response}]],
        [True, False],
    )
    observed = bridge.wait_for_new_response({"count": 0, "text_hash": "", "entries": []}, poll_interval=1)
    assert observed["state"] == "COMPLETED"
    assert parse_orchestrator_result(observed["text"], "task-1")["action"] == "EXECUTE"


def test_partial_architect_response_is_not_authoritative_until_final(monkeypatch):
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    response = envelope("task-1", prompt="next") + "\nARCHITECT_RESPONSE_COMPLETE"
    bridge = FakeResponseBridge(
        [[{"id": "a", "text": "partial one"}], [{"id": "a", "text": "partial two"}], [{"id": "a", "text": response}]],
        [False, False, False],
    )
    observed = bridge.wait_for_new_response({"count": 0, "text_hash": "", "entries": []}, poll_interval=1)
    assert observed["state"] == "COMPLETED"
    assert parse_orchestrator_result(observed["text"], "task-1")["action"] == "EXECUTE"


def test_format_recovery_transport_failure_is_contained(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "task-1"})
    class BrokenBridge:
        def submit_result_bounded(self, _message): raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED")
    assert handle_architect_value_error(watcher, BrokenBridge()) is False
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED"


def test_restart_architect_running_uses_persisted_baseline_without_resend(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "architectBaseline": {"count": 1, "text_hash": "h", "entries": []}})
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.state["state"] == "ARCHITECT_RUNNING"
    assert "executorResultPath" in restarted.state


def test_stopped_malformed_architect_response_is_safe_without_resend(monkeypatch):
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    bridge = FakeResponseBridge(
        [[{"id": "a", "text": "malformed final"}], [{"id": "a", "text": "malformed final"}]],
        [False, False],
    )
    observed = bridge.wait_for_new_response({"count": 0, "text_hash": "", "entries": []}, poll_interval=1)
    assert observed["state"] == "BLOCKED"
    with pytest.raises(ValueError):
        parse_orchestrator_result(observed["text"], "task-1")


def test_executor_running_poll_is_resident_and_advances_on_exit(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    report = tmp_path / "report.txt"
    report.write_text("completed", encoding="utf-8")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 1234, "taskId": "task-1", "executorResultPath": str(report)})
    alive = iter([True, True, False])
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: next(alive)))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    assert watcher.wait_for_executor(poll_interval=999999) == "RESULT_READY"


def test_resident_executor_poll_has_no_wall_clock_timeout():
    assert "timeout" not in inspect.signature(LocalFirstOrchestrator.wait_for_executor).parameters


def test_idle_remains_resident_and_detects_later_local_work(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.save()
    polls = []

    def poll(_delay):
        polls.append(1)
        if len(polls) == 2:
            atomic_write(watcher.state_path, json.dumps({"state": "NEXT_PROMPT_READY", "nextPromptPath": "future.txt"}).encode())

    monkeypatch.setattr(watcher_module.time, "sleep", poll)
    assert watcher.wait_for_idle(poll_interval=999999) == "NEXT_PROMPT_READY"
    assert len(polls) == 2
    assert watcher.state.get("codexLaunchCount", 0) == 0
    assert watcher.state.get("architectContactCount", 0) == 0


def test_idle_does_not_resurrect_superseded_task(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "IDLE", "taskId": None, "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96", "supersededTaskIds": ["000001"]})
    watcher.save()

    def stop(_delay):
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher_module.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        watcher.wait_for_idle()
    assert watcher.state["state"] == "IDLE"
    assert watcher.state["supersededTaskIds"] == ["000001"]


def test_main_handles_ctrl_c_from_idle_cleanly(monkeypatch, capsys):
    class IdleWatcher:
        def __init__(self, *_args, **_kwargs):
            self.state = {"state": "IDLE"}

        def intake_inbox(self, _launcher):
            raise KeyboardInterrupt

    monkeypatch.setattr(watcher_module, "LocalFirstOrchestrator", IdleWatcher)
    monkeypatch.setattr(watcher_module.DiscussionHotkeyController, "start", lambda *_args, **_kwargs: True)
    watcher_module.main()
    assert "STATE=STOPPED" in capsys.readouterr().out


def test_architect_executor_prompt_resolves_explicit_worktree(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    prompt = configured_project_prompt(base, head)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("recovery-safety-task", prompt=prompt))
    owned = Path(watcher.state["targetWorktree"])
    assert owned.is_dir()
    assert owned != base
    assert owned == Path(watcher.state["taskWorktrees"][watcher.state["nextTaskId"]]["worktreePath"])


def test_missing_or_invalid_executor_worktree_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="EXECUTOR_WORKTREE_MISSING"):
        resolve_executor_worktree("ROLE\nExecutor")
    with pytest.raises(RuntimeError, match="EXECUTOR_WORKTREE_INVALID"):
        resolve_executor_worktree("WORKTREE\nC:\\does-not-exist", tmp_path)


def test_report_worktree_label_is_not_machine_routing_authority():
    prompt = "RETURN\nReport:\nsourceBase\nworktree\nfilesChanged\nmatchingRouteOrBoundary"
    with pytest.raises(RuntimeError, match="EXECUTOR_WORKTREE_MISSING") as error:
        resolve_executor_worktree(prompt)
    assert "filesChanged" not in str(error.value)


def test_absolute_machine_worktree_path_resolves_with_following_prompt_text(tmp_path):
    worktree = tmp_path / "valid-worktree"
    worktree.mkdir()
    prompt = f"WORKTREE\n{worktree}\nGOAL\ncontinue"
    assert resolve_executor_worktree(prompt) == str(worktree)


def test_prelaunch_failure_does_not_terminally_consume_architect_execute(tmp_path, monkeypatch):
    worktree = tmp_path / "valid-worktree"
    worktree.mkdir()
    response = envelope("prelaunch-task", prompt=f"WORKTREE\n{worktree}\nnext")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    calls = []

    def fail_once(_prompt, _fallback):
        calls.append("failed")
        raise RuntimeError("PRELAUNCH_TEST_FAILURE")

    monkeypatch.setattr(watcher, "_owned_task_worktree", lambda *args, **kwargs: fail_once(None, None))
    with pytest.raises(RuntimeError, match="PRELAUNCH_TEST_FAILURE"):
        watcher.consume_idle_architect_response(response, lambda *_: calls.append("launched"))
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    assert calls == ["failed"]
    assert fingerprint not in watcher.state.get("consumedArchitectResponses", {})

    monkeypatch.setattr(watcher, "_owned_task_worktree", LocalFirstOrchestrator._owned_task_worktree.__get__(watcher))
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    launches = []
    process = type("Process", (), {"pid": 4101})()
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        restarted.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1])
    assert launches == []


def test_current_prelaunch_incomplete_execute_recovers_same_response_once(tmp_path):
    task_id = "PUB-7c6f3f3c8b9b46f88b8e2c3d91d7a5e2"
    worktree = tmp_path / "managed-worktree"
    worktree.mkdir()
    response = envelope(task_id, prompt=f"WORKTREE\n{worktree}\nnext bounded task")
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({
        "state": "IDLE",
        "taskId": task_id,
        "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96",
        "codexPid": None,
        "executorResultPath": None,
        "consumedArchitectResponses": {
            fingerprint: {"taskId": task_id, "action": "EXECUTE", "state": "RECEIVED"}
        },
    })
    watcher.save()
    launches = []
    process = type("Process", (), {"pid": 4102})()
    assert watcher.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1]) == "DUPLICATE"
    assert "prelaunchRecoveryState" not in watcher.state
    assert launches == []

    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.consume_idle_architect_response(response, lambda *_: launches.append(2)) == "DUPLICATE"
    assert launches == []
    assert restarted.state["lastCompletedTaskId"] == "PUB-aa3b4121887c4047b3c056bcccaa6a96"


def test_fresh_execute_is_consumed_only_after_child_launch(tmp_path):
    worktree = tmp_path / "managed-worktree"
    worktree.mkdir()
    response = envelope("normal-task", prompt=f"WORKTREE\n{worktree}\nnext")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    launches = []
    process = type("Process", (), {"pid": 4103})()
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        watcher.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1])
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    assert launches == []
    assert fingerprint not in watcher.state.get("consumedArchitectResponses", {})


def test_project_execute_creates_owned_worktree_and_child_uses_it(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    watcher = LocalFirstOrchestrator(str(tmp_path / "orchestrator"), tmp_path / "orchestrator" / "state")
    write_project_config(watcher, config)
    task_id = "owned-task"
    response = envelope(task_id, prompt=configured_project_prompt(base, head, task_id))
    observed = []
    process = type("Process", (), {"pid": 4201})()
    assert watcher.consume_idle_architect_response(response, lambda prompt, result: (observed.append(watcher.state["targetWorktree"]), process)[1]) == "EXECUTE"
    watcher.launch_next(lambda prompt, result: (observed.append(watcher.state["targetWorktree"]), process)[1])
    owned = Path(watcher.state["targetWorktree"])
    assert owned.is_dir() and owned != base and owned != watcher.project_dir
    assert observed == [str(owned)]
    assert watcher.state["taskWorktrees"]["000001"]["worktreePath"] == str(owned)
    assert watcher.state["taskWorktrees"]["000001"]["baseCommit"] == head
    assert len(subprocess.check_output(["git", "-C", str(base), "worktree", "list"], text=True).splitlines()) == 2


def test_owned_worktree_restart_recovery_and_duplicate_are_singleton(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    response = envelope("restart-task", prompt=configured_project_prompt(base, head, "restart-task"))
    process = type("Process", (), {"pid": 4202})()
    launches = []
    watcher.consume_idle_architect_response(response, lambda *_: (launches.append(watcher.state["targetWorktree"]), process)[1])
    watcher.launch_next(lambda *_: (launches.append(watcher.state["targetWorktree"]), process)[1])
    owned = launches[0]
    restarted = LocalFirstOrchestrator(str(root), root / "state")
    assert restarted.state["taskWorktrees"]["000001"]["worktreePath"] == owned
    assert restarted.consume_idle_architect_response(response, lambda *_: launches.append("duplicate")) == "DUPLICATE"
    assert launches == [owned]
    assert len(subprocess.check_output(["git", "-C", str(base), "worktree", "list"], text=True).splitlines()) == 2


def test_prelaunch_incomplete_project_task_reuses_one_owned_worktree(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    task_id = "PUB-7c6f3f3c8b9b46f88b8e2c3d91d7a5e2"
    response = envelope(task_id, prompt=configured_project_prompt(base, head, task_id))
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    watcher.state.update({"state": "IDLE", "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96", "codexPid": None, "executorResultPath": None, "consumedArchitectResponses": {fingerprint: {"taskId": task_id, "action": "EXECUTE", "state": "RECEIVED"}}})
    watcher.save()
    launches = []
    process = type("Process", (), {"pid": 4203})()
    assert watcher.consume_idle_architect_response(response, lambda *_: (launches.append(watcher.state["targetWorktree"]), process)[1]) == "DUPLICATE"
    assert len(launches) == 0
    assert "prelaunchRecoveryState" not in watcher.state
    assert len(subprocess.check_output(["git", "-C", str(base), "worktree", "list"], text=True).splitlines()) == 1


def test_source_authority_advancement_fails_closed(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    watcher = LocalFirstOrchestrator(str(tmp_path / "orchestrator"), tmp_path / "orchestrator" / "state")
    write_project_config(watcher, config)
    response = envelope("authority-task", prompt=configured_project_prompt(base, "0" * 40, "authority-task"))
    with pytest.raises(RuntimeError, match="EXECUTOR_SOURCE_AUTHORITY_ADVANCED"):
        watcher.consume_idle_architect_response(response, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch")))
    assert not (watcher.state_dir / "worktrees" / "authority-task").exists()


def test_explicit_architect_branch_ignores_incidental_base_checkout_branch(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    subprocess.run(["git", "-C", str(base), "checkout", "-b", "some-other-local-branch"], check=True, capture_output=True)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    response = envelope("explicit-branch-task", prompt=configured_project_prompt(base, head, "explicit-branch-task"))
    process = type("Process", (), {"pid": 4301})()
    assert watcher.consume_idle_architect_response(response, lambda *_: process) == "EXECUTE"
    watcher.launch_next(lambda *_: process)
    assert subprocess.check_output(["git", "-C", str(base), "branch", "--show-current"], text=True).strip() == "some-other-local-branch"
    assert watcher.state["taskWorktrees"]["000001"]["branch"] == "hybrid-v2"


def test_legacy_project_config_branch_is_migrated_without_blocking_explicit_branch(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    config["projects"]["sample-project"]["branch"] = "some-other-local-branch"
    config["projects"]["sample-project"].pop("defaultBranch", None)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    response = envelope("legacy-config-task", prompt=configured_project_prompt(base, head, "legacy-config-task"))
    process = type("Process", (), {"pid": 4302})()
    assert watcher.consume_idle_architect_response(response, lambda *_: process) == "EXECUTE"
    migrated = json.loads((watcher.state_dir / "project-config.json").read_text(encoding="utf-8"))
    spec = migrated["projects"]["sample-project"]
    assert "branch" not in spec and spec["defaultBranch"] == "some-other-local-branch"
    assert watcher.state["taskWorktrees"]["000001"]["branch"] == "hybrid-v2"


def test_requested_remote_branch_must_exist(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    watcher = LocalFirstOrchestrator(str(tmp_path / "orchestrator"), tmp_path / "orchestrator" / "state")
    write_project_config(watcher, config)
    prompt = "repository=sample-project.git\nbranch=does-not-exist\nbounded task"
    with pytest.raises(RuntimeError, match="EXECUTOR_SOURCE_FETCH_FAILED"):
        watcher.consume_idle_architect_response(envelope("missing-branch-task", prompt=prompt), lambda *_: (_ for _ in ()).throw(AssertionError("must not launch")))
    assert not (watcher.state_dir / "worktrees" / "missing-branch-task").exists()


def test_worktree_path_owned_by_another_task_is_not_reused(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    conflict = watcher.state_dir / "worktrees" / "conflict-task"
    conflict.mkdir(parents=True)
    response = envelope("conflict-task", prompt=configured_project_prompt(base, head, "conflict-task"))
    assert watcher.consume_idle_architect_response(response, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"


def test_visible_launcher_uses_fresh_task_execution_and_owned_cwd(tmp_path, monkeypatch):
    owned = tmp_path / "owned-worktree"
    owned.mkdir()
    watcher = LocalFirstOrchestrator(str(tmp_path / "orchestrator"), tmp_path / "orchestrator" / "state")
    watcher.state.update({"targetWorktree": str(owned), "taskId": "fresh-task"})
    observed = {}

    class Stdin:
        def write(self, value): observed["prompt"] = value
        def close(self): pass

    class Child:
        pid = 4401
        stdin = Stdin()

    class FreshRunner:
        launcher = ["codex"]
        executable = "codex"
        session_id = watcher_module.AFFOTECH_EXECUTOR_SESSION_ID
        def __init__(self, *_args, **_kwargs): pass
        def assemble_prompt(self, prompt): return prompt

    def fake_popen(command, **kwargs):
        observed.update({"command": command, "cwd": kwargs["cwd"]})
        return Child()

    monkeypatch.setattr(watcher_module, "CodexRunner", FreshRunner)
    monkeypatch.setattr(watcher_module.subprocess, "Popen", fake_popen)
    result_path = tmp_path / "result.txt"
    child = visible_executor_launcher(str(tmp_path), watcher)("unchanged prompt", result_path)
    assert child.pid == 4401
    assert observed["command"][0:4] == ["codex", "exec", "resume", watcher_module.AFFOTECH_EXECUTOR_SESSION_ID]
    assert "--ephemeral" not in observed["command"]
    assert observed["cwd"] == str(owned)
    assert observed["prompt"] == b"unchanged prompt"
    assert Path(watcher.state["stderrLogPath"]).is_file()


def test_known_persistent_executor_session_passes_read_only_preflight():
    assert watcher_module.verify_executor_session(watcher_module.AFFOTECH_EXECUTOR_SESSION_ID)


def test_missing_executor_session_fails_before_resume_or_child_creation(tmp_path, monkeypatch):
    owned = tmp_path / "owned-worktree"
    owned.mkdir()
    watcher = LocalFirstOrchestrator(str(tmp_path / "orchestrator"), tmp_path / "orchestrator" / "state")
    watcher.state["targetWorktree"] = str(owned)
    watcher.state["executorSessionId"] = watcher_module.AFFOTECH_EXECUTOR_SESSION_ID
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: (_ for _ in ()).throw(RuntimeError("EXECUTOR_SESSION_NOT_FOUND")))
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create child")))
    with pytest.raises(RuntimeError, match="EXECUTOR_SESSION_NOT_FOUND"):
        visible_executor_launcher(str(tmp_path), watcher)("prompt", tmp_path / "result.txt")


def test_configured_executor_session_identity_mismatch_fails_closed(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.state["targetWorktree"] = str(tmp_path)
    watcher.state["executorSessionId"] = "different-session"
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create child")))
    with pytest.raises(RuntimeError, match="EXECUTOR_SESSION_IDENTITY_MISMATCH"):
        visible_executor_launcher(str(tmp_path), watcher)("prompt", tmp_path / "result.txt")


def test_dead_child_without_result_persists_crash_diagnostics(tmp_path, monkeypatch, capsys):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    result_path = tmp_path / "missing-result.txt"
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "crashed-task", "codexPid": 4402, "targetWorktree": str(tmp_path / "owned"), "executorResultPath": str(result_path)})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    assert watcher.reconcile_executor() == "EXECUTOR_CRASHED"
    assert watcher.state["executorProcessState"] == "EXITED_WITHOUT_RESULT"
    assert watcher.state["executorCrash"] == {"taskId": "crashed-task", "pid": 4402, "targetWorktree": str(tmp_path / "owned"), "resultPath": str(result_path), "resultExists": False, "exitCode": None, "stderrLogPath": None, "stderrSummary": ""}
    assert "EXITED_WITHOUT_RESULT" in capsys.readouterr().out


def test_postlaunch_no_result_never_automatically_relaunches(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "postlaunch-task", "codexPid": 4404, "executorLaunchState": "LAUNCHED", "prelaunchRecoveryState": "RECOVERED_AND_LAUNCHED", "executorResultPath": str(tmp_path / "missing-result"), "targetWorktree": str(tmp_path / "owned")})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["executorLaunchState"] == "POSTLAUNCH_NO_RESULT"
    assert watcher.state["automaticRetryAuthorized"] is False
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.recover_prelaunch_incomplete(lambda *_: launches.append(2)) is None
    assert launches == []


def test_human_postlaunch_retry_requires_exact_authorization(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    monkeypatch.delenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", raising=False)
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is None
    assert launches == []
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", "different-task")
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(2)) is None
    assert watcher.state["humanRecoveryAuthorizationError"] == "HUMAN_RECOVERY_TASK_MISMATCH"
    assert launches == []


def test_exact_human_postlaunch_retry_is_consumed_and_launches_once(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    process = type("Process", (), {"pid": 4406})()
    assert watcher.authorize_postlaunch_retry(lambda *_: (launches.append(1), process)[1]) is True
    assert launches == []
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == task_id
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["automaticRetryAuthorized"] is False


def test_old_task_consumed_authorization_allows_current_task_once(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state.update({
        "humanRecoveryAuthorizationConsumed": True,
        "humanRecoveryAuthorizedTaskId": "OLD-TASK",
        "humanRecoveryAuthorizationError": "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED",
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: True)
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is True
    assert launches == []
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == task_id
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == task_id
    assert "humanRecoveryAuthorizationError" not in watcher.state


def test_postlaunch_retry_inherits_persisted_project_context_without_prompt_metadata(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.prompts_dir.joinpath(f"{task_id}.txt").write_text("MILESTONE\npostlaunch retry without project metadata\n", encoding="utf-8")
    watcher.state.update({
        "nextPromptPath": str(watcher.prompts_dir / f"{task_id}.txt"),
        "humanRecoveryAuthorizationConsumed": True,
        "humanRecoveryAuthorizedTaskId": "OLD-TASK",
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: True)
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is True
    assert launches == []
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == task_id
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == task_id


def test_postlaunch_retry_without_project_context_fails_closed_before_git(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state["taskWorktrees"] = {}
    watcher.state.update({
        "humanRecoveryAuthorizationConsumed": True,
        "humanRecoveryAuthorizedTaskId": "OLD-TASK",
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: True)
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is None
    assert launches == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == "OLD-TASK"
    assert watcher.state["humanRecoveryAuthorizationError"] == "PRELAUNCH_WORKTREE_NOT_OWNED"


def test_same_task_consumed_authorization_remains_one_shot(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state.update({
        "humanRecoveryAuthorizationConsumed": True,
        "humanRecoveryAuthorizedTaskId": task_id,
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is None
    assert launches == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRecoveryAuthorizationError"] == "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED"


def test_old_task_consumed_authorization_current_validation_failure_preserves_evidence(tmp_path, monkeypatch):
    watcher, _base, _worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state.update({
        "humanRecoveryAuthorizationConsumed": True,
        "humanRecoveryAuthorizedTaskId": "OLD-TASK",
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: (_ for _ in ()).throw(RuntimeError("EXECUTOR_SESSION_NOT_FOUND")))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is None
    assert launches == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == "OLD-TASK"
    assert watcher.state["humanRecoveryAuthorizationError"] == "EXECUTOR_SESSION_NOT_FOUND"


def test_task_scoped_authorization_allows_sequential_tasks_once_each(tmp_path, monkeypatch):
    watcher, base, _worktree = postlaunch_recovery_fixture(tmp_path)
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: True)
    launches = []
    task_a = watcher.state["taskId"]
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_a)
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append("a")) is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == task_a

    task_b = "TASK-B"
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "HEAD"], text=True).strip()
    prompt_b = configured_project_prompt(base, head, task_b)
    prompt_b_path = watcher.prompts_dir / f"{task_b}.txt"
    prompt_b_path.write_text(prompt_b, encoding="utf-8")
    worktree_b = watcher._owned_task_worktree(task_b, prompt_b)
    watcher.state.update({
        "state": "HUMAN_REQUIRED",
        "taskId": task_b,
        "nextTaskId": task_b,
        "executorLaunchState": "POSTLAUNCH_NO_RESULT",
        "nextPromptPath": str(prompt_b_path),
    })
    watcher.save()
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_b)
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append("b")) is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == "TASK-B"
    assert launches == []

    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append("b-again")) is None
    assert launches == []


def test_failed_human_postlaunch_retry_cannot_reuse_authorization(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    def fail_launch(*_args):
        launches.append(1)
        raise RuntimeError("writer conflict")
    assert watcher.authorize_postlaunch_retry(fail_launch) is True
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert launches == []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(2)) is None
    assert launches == []


def test_human_postlaunch_retry_session_preflight_is_required(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    monkeypatch.setattr(watcher_module, "verify_executor_session", lambda _sid: (_ for _ in ()).throw(RuntimeError("EXECUTOR_SESSION_NOT_FOUND")))
    launches = []
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(1)) is None
    assert watcher.state["humanRecoveryAuthorizationError"] == "EXECUTOR_SESSION_NOT_FOUND"
    assert launches == []
    assert watcher.state.get("humanRecoveryAuthorizationConsumed") is not True


def test_active_writer_failure_is_classified_without_retry(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    stderr_path = watcher.state_dir / "executor-logs" / "task-attempt-1.stderr.txt"
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("thread 019f842e already has an active writer\n", encoding="utf-8")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "writer-task", "codexPid": 4405, "executorResultPath": str(tmp_path / "missing-result"), "stderrLogPath": str(stderr_path), "targetWorktree": str(tmp_path / "owned")})
    watcher.save()
    class Process:
        def poll(self): return 23
    watcher._active_process = Process()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    assert watcher.reconcile_executor() == "EXECUTOR_CRASHED"
    assert watcher.state["executorFailureClass"] == "EXECUTOR_SESSION_ACTIVE_WRITER"
    assert watcher.state["executorExitCode"] == 23
    assert watcher.state["executorCrash"]["stderrLogPath"] == str(stderr_path)
    assert watcher.state["automaticRetryAuthorized"] is False


def test_prelaunch_recovery_reuses_owned_worktree_without_new_architect_decision(tmp_path, monkeypatch):
    base, config, head = configured_git_project(tmp_path)
    root = tmp_path / "orchestrator"
    watcher = LocalFirstOrchestrator(str(root), root / "state")
    write_project_config(watcher, config)
    task_id = "recovery-task"
    response = envelope(task_id, prompt=configured_project_prompt(base, head, task_id))
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    worktree = watcher._owned_task_worktree(task_id, configured_project_prompt(base, head, task_id))
    prompt_path = watcher.prompts_dir / f"{task_id}.txt"
    atomic_write(prompt_path, configured_project_prompt(base, head, task_id).encode("utf-8"))
    watcher.state.update({
        "state": "HUMAN_REQUIRED", "taskId": task_id, "nextTaskId": task_id,
        "nextPromptPath": str(prompt_path), "targetWorktree": str(worktree),
        "codexPid": None, "executorResultPath": str(tmp_path / "missing-result"),
        "lastCompletedTaskId": "completed-task", "architectResultFingerprint": fingerprint,
        "consumedArchitectResponses": {fingerprint: {"taskId": task_id, "action": "EXECUTE", "state": "RECEIVED"}},
    })
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    process = type("Process", (), {"pid": 4403})()
    assert watcher.recover_prelaunch_incomplete(lambda *_: (launches.append(watcher.state["targetWorktree"]), process)[1]) is True
    assert launches == []
    assert len(subprocess.check_output(["git", "-C", str(base), "worktree", "list"], text=True).splitlines()) == 2


def test_dirty_tracked_worktree_blocks_recovery_without_cleanup(tmp_path, monkeypatch):
    watcher, base, worktree = recovery_fixture(tmp_path)
    (worktree / "README.md").write_text("partial work\n", encoding="utf-8")
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["prelaunchRecoveryError"] == "RECOVERY_WORKTREE_DIRTY"
    assert watcher.state["state"] == "HUMAN_REQUIRED" and launches == []
    assert (worktree / "README.md").read_text(encoding="utf-8") == "partial work\n"


def test_untracked_worktree_file_blocks_recovery_without_cleanup(tmp_path, monkeypatch):
    watcher, base, worktree = recovery_fixture(tmp_path)
    marker = worktree / "partial-untracked.txt"
    marker.write_text("partial\n", encoding="utf-8")
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["prelaunchRecoveryError"] == "RECOVERY_WORKTREE_DIRTY"
    assert marker.exists() and launches == []


def test_changed_worktree_head_blocks_recovery(tmp_path, monkeypatch):
    watcher, base, worktree = recovery_fixture(tmp_path)
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Orchestrator Test"], check=True)
    (worktree / "README.md").write_text("committed partial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-m", "partial"], check=True, capture_output=True)
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["prelaunchRecoveryError"] == "RECOVERY_WORKTREE_HEAD_CHANGED"
    assert launches == []


def test_remote_advance_blocks_recovery(tmp_path, monkeypatch):
    watcher, base, worktree = recovery_fixture(tmp_path)
    (base / "README.md").write_text("remote advanced\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(base), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "commit", "-m", "advance"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(base), "push", "origin", "hybrid-v2"], check=True, capture_output=True)
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["prelaunchRecoveryError"] == "RECOVERY_SOURCE_ADVANCED"
    assert launches == []


def test_existing_nonempty_result_blocks_recovery(tmp_path, monkeypatch):
    watcher, base, worktree = recovery_fixture(tmp_path)
    result_path = Path(watcher.state["executorResultPath"])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("terminal result\n", encoding="utf-8")
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["prelaunchRecoveryError"] == "RECOVERY_RESULT_ALREADY_EXISTS"
    assert result_path.read_text(encoding="utf-8") == "terminal result\n" and launches == []


def test_live_owned_child_blocks_recovery(tmp_path, monkeypatch):
    watcher, base, worktree = recovery_fixture(tmp_path)
    watcher.state["codexPid"] = 4405
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    launches = []
    assert watcher.recover_prelaunch_incomplete(lambda *_: launches.append(1)) is None
    assert watcher.state["prelaunchRecoveryError"] == "RECOVERY_CHILD_STILL_ALIVE"
    assert launches == []


def test_production_state_cycle_dead_postlaunch_without_authorization_stops_safely(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 4410})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.delenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", raising=False)
    launches = []
    assert run_executor_state_once(watcher, lambda *_: launches.append(1)) == "STOP"
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["executorLaunchState"] == "POSTLAUNCH_NO_RESULT"
    assert watcher.state["automaticRetryAuthorized"] is False
    assert launches == []


def test_production_state_cycle_consumes_authorization_same_invocation(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 4411})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    process = type("Process", (), {"pid": 4412})()
    assert run_executor_state_once(watcher, lambda *_: (launches.append(1), process)[1]) == "NEXT_PROMPT_READY"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert launches == []


def test_authorized_child_failure_is_postlaunch_active_writer_without_retry(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 4413})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    process = type("Process", (), {"pid": 4414, "poll": lambda self: 17})()
    launches = []
    def launch(_prompt, _result):
        launches.append(1)
        log_path = watcher.state_dir / "executor-logs" / "retry.stderr.txt"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("thread 019f842e-98bc-7672-a619-51441d91be00 already has an active writer", encoding="utf-8")
        watcher.state["stderrLogPath"] = str(log_path)
        return process
    assert run_executor_state_once(watcher, launch) == "NEXT_PROMPT_READY"
    watcher.launch_next(launch)
    assert run_executor_state_once(watcher, launch) == "STOP"
    assert watcher.state["executorFailureClass"] == "EXECUTOR_SESSION_ACTIVE_WRITER"
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["automaticRetryAuthorized"] is False
    assert launches == [1]


def test_authorized_child_success_reaches_result_and_architect(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 4415})
    watcher.save()
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    process = type("Process", (), {"pid": 4416, "poll": lambda self: 0})()
    launches = []
    child_cwds = []
    def launch(_prompt, result):
        launches.append(1)
        child_cwds.append(watcher.state["targetWorktree"])
        Path(result).parent.mkdir(parents=True, exist_ok=True)
        Path(result).write_text("completed report", encoding="utf-8")
        return process
    assert run_executor_state_once(watcher, launch) == "NEXT_PROMPT_READY"
    watcher.launch_next(launch)
    assert run_executor_state_once(watcher, launch) == "RESULT_READY"
    assert launches == [1]
    messages = []
    class Bridge:
        def submit_result_bounded(self, message): messages.append(message)
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def latest_user_message(self): return messages[-1] if messages else None
    watcher.deliver_result(Bridge())
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert messages
    next_prompt = configured_project_prompt(base, head, "next-bounded-task")
    assert watcher.accept_architect_response(envelope(task_id, prompt=next_prompt))["action"] == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    next_worktree = Path(watcher.state["targetWorktree"])
    assert next_worktree.is_dir()
    assert watcher.launch_next(launch) is process
    assert run_executor_state_once(watcher, launch) == "RESULT_READY"
    assert len(launches) == 2
    assert Path(child_cwds[1]) == next_worktree
    assert next_worktree != base
    assert next_worktree != watcher.project_dir
    second_messages = []
    class SecondBridge:
        def submit_result_bounded(self, message): second_messages.append(message)
        def assistant_baseline(self): return {"count": 2, "entries": []}
        def latest_user_message(self): return second_messages[-1] if second_messages else None
    watcher.deliver_result(SecondBridge())
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert second_messages
    assert watcher.accept_architect_response(envelope(watcher.state["taskId"], action="STOP"))["action"] == "STOP"
    assert watcher.state["state"] == "IDLE"


def test_post_result_execute_inherits_project_context_without_routing_lines(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskSequence": 4})
    prompt = "Review current project state and perform the next bounded milestone."
    response = envelope(task_id, prompt=prompt)
    assert watcher.accept_architect_response(response)["action"] == "EXECUTE"
    next_id = watcher.state["nextTaskId"]
    owned = Path(watcher.state["targetWorktree"])
    assert next_id == "000005"
    assert owned.is_dir() and owned != base and owned != watcher.project_dir
    assert Path(watcher.state["nextPromptPath"]).read_text(encoding="utf-8") == prompt
    assert watcher.state["taskWorktrees"][next_id]["branch"] == "hybrid-v2"
    assert watcher.state["taskWorktrees"][next_id]["baseCommit"] == subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()


def test_post_result_execute_without_project_context_fails_before_prompt_commit(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "completed-without-context", "taskSequence": 1})
    response = envelope("completed-without-context", prompt="next bounded action")
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        watcher.accept_architect_response(response)
    assert not (watcher.prompts_dir / "000002.txt").exists()
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state.get("architectResultFingerprint") in (None, "")


def test_watcher_instance_lock_is_single_owner_and_releases(tmp_path):
    state_dir = tmp_path / "state"
    first = WatcherInstanceLock(state_dir)
    second = WatcherInstanceLock(state_dir)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="ORCHESTRATOR_ALREADY_RUNNING"):
            second.acquire()
        assert not (state_dir / "state.json").exists()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_launch_next_uses_owned_worktree_and_records_owned_pid(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    watcher.accept_architect_response(envelope("recovery-safety-task", prompt=configured_project_prompt(base, head)))

    class Process:
        pid = 9876

    observed = []
    watcher.launch_next(lambda prompt, path: (observed.append((prompt, path, watcher.state["targetWorktree"])) or Process()))
    owned = watcher.state["targetWorktree"]
    assert observed[0][2] == owned
    assert watcher.state["codexPid"] == 9876
    assert watcher.state["targetWorktree"] == owned


def test_dead_pid_with_no_result_is_crashed_and_never_relaunched(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 1111, "taskId": "task-1", "executorResultPath": str(tmp_path / "missing")})
    calls = []
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda pid: calls.append(pid) or False))
    assert watcher.reconcile_executor() == "EXECUTOR_CRASHED"
    assert watcher.state["state"] != "EXECUTOR_RUNNING"
    assert calls == [1111]
    assert watcher.launch_next(lambda *_: (_ for _ in ()).throw(AssertionError("must not relaunch"))) is None


def test_restart_reconciliation_persists_pending_worktree_before_pid_decision(tmp_path, monkeypatch):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    prompt = tmp_path / "next.txt"
    prompt.write_text(f"WORKTREE\n{worktree}\nnext", encoding="utf-8")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "codexPid": 2222, "taskId": "000001", "nextPromptPath": str(prompt), "executorResultPath": str(tmp_path / "missing"), "targetProject": str(tmp_path)})
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    assert watcher.reconcile_executor() == "EXECUTOR_CRASHED"
    assert watcher.state["targetProject"] == str(worktree)
    assert watcher.state["targetWorktree"] == str(worktree)


def inbox_watcher(tmp_path, prompt):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.inbox_dir.mkdir(parents=True)
    source = watcher.inbox_dir / "000002.txt"
    source.write_text(prompt, encoding="utf-8")
    return watcher, source


def test_idle_valid_inbox_launches_once_and_persists_target(tmp_path):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    watcher, source = inbox_watcher(tmp_path, f"PROJECT\naffotech-system-v2-hybrid\nWORKTREE\n{worktree}\nGOAL\nnext")

    class Process:
        pid = 3001

    launches = []
    assert watcher.intake_inbox(lambda prompt, result: (launches.append((prompt, result)), Process())[1])
    assert len(launches) == 0
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    watcher.launch_next(lambda prompt, result: (launches.append((prompt, result)), Process())[1])
    assert len(launches) == 1
    assert watcher.state["targetWorktree"] == str(worktree)
    assert Path(watcher.state["nextPromptPath"]).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert watcher.state["consumedInboxItems"]["000002"]["status"] == "LAUNCH_AUTHORIZED"


def test_consumed_inbox_item_cannot_relaunch_after_restart_or_poll(tmp_path):
    watcher, _ = inbox_watcher(tmp_path, "next")

    class Process:
        pid = 3002

    launches = []
    watcher.intake_inbox(lambda *_: (launches.append(1) or Process()))
    watcher.launch_next(lambda *_: (launches.append(1) or Process()))
    assert len(launches) == 1
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.intake_inbox(lambda *_: (launches.append(1) or Process())) is False
    assert len(launches) == 1


def test_malformed_or_missing_target_inbox_fails_closed(tmp_path):
    for prompt in ("", "WORKTREE\nC:\\missing-affotech-target"):
        watcher, _ = inbox_watcher(tmp_path / str(len(prompt)), prompt)
        launches = []
        assert watcher.intake_inbox(lambda *_: launches.append(1))
        assert watcher.state["state"] == "HUMAN_REQUIRED"
        assert launches == []


def test_non_idle_state_does_not_consume_inbox_work(tmp_path):
    watcher, source = inbox_watcher(tmp_path, "next")
    watcher.state["state"] = "EXECUTOR_RUNNING"
    watcher.save()
    assert watcher.intake_inbox(lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) is False
    assert "000002" not in watcher.state.get("consumedInboxItems", {})
    assert source.exists()


class IdleArchitectBridge:
    def __init__(self, response):
        self.response = response
        self.messages = []

    def generation_visible(self):
        return False

    def _assistant_entries(self):
        return [{"id": "architect-1", "text": self.response}]

    def assistant_baseline(self):
        return {"count": 1, "text_hash": "architect-baseline", "entries": self._assistant_entries()}

    def submit_result_bounded(self, message):
        self.messages.append(message)


def test_idle_consumes_latest_architect_execute_without_inbox(tmp_path):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    response = envelope("architect-task-1", prompt=f"WORKTREE\n{worktree}\nnext")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    bridge = IdleArchitectBridge(response)

    class Process:
        pid = 4001

    launches = []
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        watcher.inspect_idle_architect(bridge, lambda prompt, result: (launches.append((prompt, result)), Process())[1])
    assert not watcher.inbox_dir.exists()
    assert watcher.state["state"] == "IDLE"
    assert launches == []


def test_consumed_architect_response_cannot_relaunch_after_restart_or_poll(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    bridge = IdleArchitectBridge(envelope("architect-task-1", prompt="next"))
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        watcher.inspect_idle_architect(bridge, lambda *_: type("Process", (), {"pid": 4002})())
    watcher.state["state"] = "IDLE"
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    launches = []
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        restarted.inspect_idle_architect(bridge, lambda *_: launches.append(1))
    assert launches == []


def test_valid_architect_stop_or_human_required_launches_nothing(tmp_path):
    for action, expected in (("STOP", "STOP"), ("HUMAN_REQUIRED", "HUMAN_REQUIRED")):
        watcher = LocalFirstOrchestrator(str(tmp_path / action), tmp_path / action / "work")
        bridge = IdleArchitectBridge(envelope(f"architect-{action}", action=action))
        launches = []
        assert watcher.inspect_idle_architect(bridge, lambda *_: launches.append(1)) == expected
        assert launches == []


def test_malformed_idle_architect_response_bootstraps_once_without_result_replay(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    bridge = IdleArchitectBridge("ordinary Architect prose")
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert len(bridge.messages) == 1
    assert "Executor report" not in bridge.messages[0]
    for forbidden in ("AFFOTECH", "OCR", "Procurement", "5C", "5D"):
        assert forbidden.lower() not in bridge.messages[0].lower()
    assert "Return STOP only if there is genuinely no further currently authorized project work." in bridge.messages[0]
    watcher.request_architect_bootstrap(bridge)
    assert watcher.state["state"] == "IDLE"
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert len(bridge.messages) == 1


@pytest.mark.parametrize("response", [
    "<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\naction=EXECUTE\npromptBegin\nnext\npromptEnd\n</ORCHESTRATOR_RESULT>",
    "<ORCHESTRATOR_RESULT>\nclassification=INVALID\naction=EXECUTE\ntaskId=000025\npromptBegin\nnext\npromptEnd\n</ORCHESTRATOR_RESULT>",
    "<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\naction=INVALID\ntaskId=000025\npromptBegin\nnext\npromptEnd\n</ORCHESTRATOR_RESULT>",
    "<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\naction=EXECUTE\ntaskId=000025\npromptBegin\nnext\n</ORCHESTRATOR_RESULT>",
    envelope("000024", prompt="next"),
])
def test_idle_intended_invalid_envelope_fails_closed_without_bootstrap(tmp_path, response):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "IDLE", "taskId": "000025", "lastCompletedTaskId": "000025"})
    bridge = IdleArchitectBridge(response)
    calls = []
    assert watcher.inspect_idle_architect(bridge, lambda *_: calls.append(1)) == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_ENVELOPE_INVALID"
    assert bridge.messages == []
    assert calls == []
    assert watcher.state["taskId"] == "000025"
    assert watcher.state["lastCompletedTaskId"] == "000025"


def test_idle_intended_envelope_marker_is_not_triggered_by_ordinary_prose(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert watcher._architect_response_attempts_authority("mentioning orchestrator in discussion") is False


def test_ambiguous_idle_bootstrap_is_reconciled_without_duplicate_send(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "IDLE", "continuationSourceFingerprint": "source-1"})
    sent = []

    class SendingBridge:
        sendActionAttempted = True
        def submit_result_bounded(self, message):
            sent.append(message)
            raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", "TimeoutError")

    with pytest.raises(ResultSubmissionError):
        watcher.request_architect_bootstrap(SendingBridge())
    assert watcher.state["architectSendState"] == "AMBIGUOUS"
    payload = watcher.state["architectBootstrapPayload"]

    class ReattachedBridge:
        def exact_user_message_payload_observed(self, candidate): return candidate == payload
        def generation_visible(self): return False
        def assistant_baseline(self): return {"count": 3, "entries": []}
        def submit_result_bounded(self, _message): raise AssertionError("duplicate bootstrap")

    assert watcher.inspect_idle_architect(ReattachedBridge(), lambda *_: None) == "ARCHITECT_RUNNING"
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert len(sent) == 1


def test_ambiguous_idle_bootstrap_waits_while_architect_generates(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "IDLE", "architectBootstrapDeliveryState": "AMBIGUOUS",
                          "architectBootstrapPayload": "bootstrap", "architectBootstrapPayloadHash": hashlib.sha256(b"bootstrap").hexdigest()})

    class Bridge:
        def exact_user_message_payload_observed(self, _payload): return False
        def generation_visible(self): return True
        def assistant_baseline(self): return {"count": 1, "entries": []}

    assert watcher.inspect_idle_architect(Bridge(), lambda *_: None) == "IDLE"
    assert watcher.state["state"] == "IDLE"


def test_ambiguous_idle_bootstrap_can_clear_fence_for_one_bounded_retry(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "IDLE", "architectBootstrapDeliveryState": "AMBIGUOUS",
                          "architectBootstrapPayload": "bootstrap", "architectBootstrapPayloadHash": hashlib.sha256(b"bootstrap").hexdigest(),
                          "architectBootstrapRetryAfter": 0, "architectBootstrapObservationAttempts": 0,
                          "lastContinuationSourceFingerprint": "source-1"})

    class Bridge:
        def exact_user_message_payload_observed(self, _payload): return False
        def generation_visible(self): return False

    monkeypatch.setattr(watcher_module.time, "time", lambda: 10.0)
    assert watcher._reconcile_architect_bootstrap(Bridge()) == "RETRY"
    assert watcher.state["architectBootstrapDeliveryState"] == "UNSENT"
    assert "lastContinuationSourceFingerprint" not in watcher.state


def test_bootstrap_execute_response_uses_same_architect_task_once(tmp_path):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "ARCHITECT_RUNNING", "architectBootstrapAwaiting": True, "taskId": None})
    response = envelope("architect-task-2", prompt=f"WORKTREE\n{worktree}\nnext")
    launches = []
    with pytest.raises(RuntimeError, match="EXECUTOR_PROJECT_CONTEXT_MISSING"):
        watcher.consume_idle_architect_response(response, lambda prompt, result: (launches.append(prompt), type("Process", (), {"pid": 4003})())[1])
    assert launches == []


def test_main_reuses_idle_playwright_bridge_until_state_changes(monkeypatch, tmp_path):
    calls = {"attach": 0, "close": 0, "inspect": 0}

    class Bridge:
        def close(self):
            calls["close"] += 1

    bridge = Bridge()

    class Watcher:
        def __init__(self, *_args):
            self.state = {"state": "IDLE"}

        def _load_state(self): return self.state
        def save(self): pass
        def intake_inbox(self, _launch): return False

        def inspect_idle_architect(self, _bridge, _launch):
            calls["inspect"] += 1
            if calls["inspect"] == 3:
                raise KeyboardInterrupt
            return "IDLE"

    def attach(_endpoint, _conversation_id):
        calls["attach"] += 1
        return bridge

    monkeypatch.setattr(watcher_module, "LocalFirstOrchestrator", Watcher)
    monkeypatch.setattr(watcher_module.DiscussionHotkeyController, "start", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(attach))
    monkeypatch.setenv("ORCHESTRATOR_POLL_INTERVAL", "0")
    monkeypatch.setattr(watcher_module, "visible_executor_launcher", lambda *_: None)
    watcher_module.main()
    assert calls == {"attach": 1, "close": 1, "inspect": 3}


def test_real_playwright_boundary_idle_reads_are_passive(tmp_path):
    response = "unchanged completed Architect response"

    class Page:
        def __init__(self):
            self.operations = []
            self.response = response

        def evaluate(self, script):
            self.operations.append(script)
            if "data-message-author-role=\"assistant\"" in script:
                return [{"id": "architect-1", "text": self.response}]
            if "button,[role=\"button\"]" in script:
                return False
            raise AssertionError("unexpected non-passive browser operation")

    page = Page()
    bridge = ArchitectPlaywright(page)
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state["lastContinuationSourceFingerprint"] = __import__("hashlib").sha256(response.encode()).hexdigest()
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert len(page.operations) == 4
    assert all("bring_to_front" not in operation for operation in page.operations)


def test_stale_thinking_placeholder_does_not_block_substantive_continuation(tmp_path):
    substantive = "latest completed Architect response without envelope"

    class Page:
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return [
                    {"id": "architect-response-1", "text": substantive},
                    {"id": "request-placeholder-1", "text": "Thinking"},
                ]
            return False

    bridge = ArchitectPlaywright(Page())
    sent = []
    bridge.submit_result_bounded = lambda message: sent.append(message)
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert len(sent) == 1
    assert watcher.state["continuationSourceFingerprint"] == hashlib.sha256(substantive.encode()).hexdigest()
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert len(sent) == 1


def test_production_state_fixture_reaches_new_continuation_once(tmp_path):
    substantive = "So there is no Curator handoff remaining. We can proceed directly to the bounded 5D Executor milestone."

    class Page:
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return [
                    {"id": "architect-response-new", "text": substantive},
                    {"id": "request-placeholder-request-new-40", "text": "Thinking"},
                ]
            return False

    bridge = ArchitectPlaywright(Page())
    sent = []
    bridge.submit_result_bounded = lambda message: sent.append(message)
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({
        "architectBootstrapAwaiting": False,
        "architectBootstrapCount": 1,
        "architectContactCount": 0,
        "architectSendState": "CONFIRMED",
        "architectResultFingerprint": "old-stop-fingerprint",
        "consumedArchitectResponses": {"old-stop-fingerprint": {"action": "STOP", "classification": "ACCEPTED", "taskId": "NONE"}},
        "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96",
        "taskId": None,
        "state": "IDLE",
    })
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert len(sent) == 1
    assert watcher.state["architectContactCount"] == 1
    assert watcher.state["continuationSourceFingerprint"] == hashlib.sha256(substantive.encode()).hexdigest()
    assert watcher.state["architectBootstrapCount"] == 2
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert len(sent) == 1


def test_consumed_response_recovers_newest_virtualized_architect_window(tmp_path):
    old = envelope("old-stop", action="STOP")
    new = "newest completed Architect response without an envelope"

    class Bridge:
        def __init__(self):
            self.restores = 0
            self.snapshots = 0
            self.sent = []
            self.live = False
        def generation_visible(self): return False
        def _assistant_entries(self):
            self.snapshots += 1
            return [{"id": "old", "text": old}] if not self.live else [{"id": "new", "text": new}]
        def restore_live_bottom(self):
            self.restores += 1
            self.live = True
            return {"before": 500, "after": 1000}
        def submit_result_bounded(self, message): self.sent.append(message)

    bridge = Bridge()
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    old_fp = hashlib.sha256(old.encode()).hexdigest()
    watcher.state.update({
        "state": "IDLE", "taskId": "old-stop", "lastCompletedTaskId": "old-stop",
        "architectDeliveryTaskId": "old-stop", "architectSendState": "CONFIRMED",
        "executorResultPath": str(tmp_path / "completed.txt"),
        "consumedArchitectResponses": {old_fp: {"action": "STOP", "classification": "ACCEPTED", "taskId": "old-stop"}},
        "architectResultFingerprint": old_fp,
    })
    Path(watcher.state["executorResultPath"]).write_text("completed", encoding="utf-8")
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert bridge.restores == 0 and bridge.snapshots == 1 and len(bridge.sent) == 1
    assert watcher.state["continuationSourceFingerprint"] == old_fp
    assert watcher.state["architectContactCount"] == 1
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert bridge.restores == 0 and len(bridge.sent) == 1


def test_consumed_response_at_bottom_restores_once_and_restore_failure_is_safe(tmp_path):
    old = envelope("old-stop", action="STOP")
    old_fp = hashlib.sha256(old.encode()).hexdigest()

    class Bridge:
        def __init__(self, fail=False): self.restores = 0; self.fail = fail
        def generation_visible(self): return False
        def _assistant_entries(self): return [{"id": "old", "text": old}]
        def restore_live_bottom(self):
            self.restores += 1
            if self.fail: raise RuntimeError("restore failed")
            return {"before": 0, "after": 0}

    for fail in (False, True):
        bridge = Bridge(fail)
        watcher = LocalFirstOrchestrator(str(tmp_path / str(fail)), tmp_path / ("work-" + str(fail)))
        watcher.state.update({"state": "IDLE", "consumedArchitectResponses": {old_fp: {"action": "STOP"}}})
        watcher.save()
        assert watcher.inspect_idle_architect(bridge, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) == "IDLE"
        watcher.state["state"] = "IDLE"
        watcher.save()
        assert watcher.inspect_idle_architect(bridge, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) == "IDLE"
        assert bridge.restores == 0 and watcher.state["state"] == "IDLE"


def test_live_generation_does_not_trigger_virtualized_bottom_recovery(tmp_path):
    class Bridge:
        restores = 0
        def generation_visible(self): return True
        def assistant_baseline(self): return {"count": 1, "entries": []}
        def restore_live_bottom(self): self.restores += 1

    bridge = Bridge()
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert bridge.restores == 0


def test_stuck_production_state_uses_baseline_only_for_reconfirmation(tmp_path):
    old = envelope("old-stop", action="STOP")
    new = "So there is no Curator handoff remaining. We can proceed directly to the bounded 5D Executor milestone."
    old_fp = hashlib.sha256(old.encode()).hexdigest()

    class Bridge:
        def __init__(self): self.restores = 0; self.sent = []
        def generation_visible(self): return False
        def _assistant_entries(self): return [{"id": "old", "text": old}]
        def restore_live_bottom(self): self.restores += 1
        def submit_result_bounded(self, message): self.sent.append(message)

    bridge = Bridge()
    launches = []
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({
        "state": "IDLE", "taskId": "000021", "architectContactCount": 0,
        "architectBootstrapAwaiting": False, "architectBootstrapCount": 1,
        "architectSendState": "CONFIRMED", "architectDeliveryTaskId": "000021",
        "executorResultPath": str(tmp_path / "completed.txt"),
        "architectResultFingerprint": old_fp, "architectLiveBottomFingerprint": old_fp,
        "lastCompletedTaskId": "000021",
        "consumedArchitectResponses": {old_fp: {"action": "STOP", "classification": "ACCEPTED", "taskId": "000021"}},
        "architectBaseline": {"entries": [{"id": "old", "text": old}, {"id": "new", "text": new}]},
    })
    Path(watcher.state["executorResultPath"]).write_text("completed", encoding="utf-8")
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *args: launches.append(args)) == "ARCHITECT_RUNNING"
    assert bridge.restores == 0 and len(bridge.sent) == 1 and launches == []
    assert watcher.state["continuationSourceFingerprint"] == old_fp
    assert watcher.state["architectContactCount"] == 1
    assert watcher.state["architectLiveBottomFingerprint"] == old_fp
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *args: launches.append(args)) == "IDLE"
    assert watcher.state["state"] == "IDLE"
    assert len(bridge.sent) == 1


def test_historical_baseline_execute_is_never_launched(tmp_path):
    old = envelope("old-stop", action="STOP")
    historical_execute = envelope("historical-execute", prompt=f"WORKTREE\n{tmp_path}\nold task")
    old_fp = hashlib.sha256(old.encode()).hexdigest()

    class Bridge:
        def __init__(self): self.sent = []
        def generation_visible(self): return False
        def _assistant_entries(self): return [{"id": "old", "text": old}]
        def submit_result_bounded(self, message): self.sent.append(message)

    bridge = Bridge()
    launches = []
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({
        "state": "IDLE",
        "consumedArchitectResponses": {old_fp: {"action": "STOP"}},
        "architectResultFingerprint": old_fp,
        "architectBaseline": {"entries": [{"id": "historical", "text": historical_execute}]},
    })
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *args: launches.append(args)) == "IDLE"
    assert len(bridge.sent) == 0 and launches == []


def test_runtime_logging_is_durable_rotating_contextual_and_private(tmp_path):
    logger, run_id, log_path = watcher_module.initialize_runtime_logging(tmp_path)
    state = {"state": "RESULT_READY", "taskId": "task-1"}
    watcher_module.runtime_log(logger, run_id, "WATCHER_STARTED", state)
    watcher_module.runtime_log(logger, run_id, "STATE_TRANSITION", state, **{"from": "IDLE", "to": "RESULT_READY", "reason": "result", "hash": "abc"})
    logger.handlers[0].flush()
    text = Path(log_path).read_text(encoding="utf-8")
    assert Path(log_path) == tmp_path / "logs" / "orchestrator.log"
    assert run_id in text and "WATCHER_STARTED" in text and "from=IDLE" in text
    assert "private prompt body" not in text
    handler = logger.handlers[0]
    assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 5


def test_runtime_logging_initialization_fails_closed(tmp_path, monkeypatch):
    def fail_handler(*_args, **_kwargs):
        raise OSError("logging unavailable")
    monkeypatch.setattr(watcher_module.logging.handlers, "RotatingFileHandler", fail_handler)
    with pytest.raises(OSError, match="logging unavailable"):
        watcher_module.initialize_runtime_logging(tmp_path)


def _idle_result_review_fixture(tmp_path, origin=None):
    response = envelope("000021", action="STOP")
    fingerprint = hashlib.sha256(response.encode()).hexdigest()
    result = tmp_path / "results" / "000021.txt"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text("completed report", encoding="utf-8")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    record = {"taskId": "000021", "action": "STOP", "classification": "ACCEPTED"}
    if origin:
        record["origin"] = origin
    watcher.state.update({
        "state": "IDLE", "taskId": "000021", "lastCompletedTaskId": "000021",
        "architectDeliveryTaskId": "000021", "architectSendState": "CONFIRMED",
        "executorResultPath": str(result), "consumedArchitectResponses": {fingerprint: record},
    })
    watcher.save()
    return watcher, response, fingerprint


def test_idle_consumed_result_review_stop_bootstraps_once(tmp_path):
    watcher, response, fingerprint = _idle_result_review_fixture(tmp_path, "RESULT_REVIEW")
    bridge = IdleArchitectBridge(response)
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert len(bridge.messages) == 1
    assert watcher.state["continuationSourceFingerprint"] == fingerprint
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert len(bridge.messages) == 1


def test_idle_bootstrap_stop_does_not_recurse(tmp_path):
    watcher, response, _ = _idle_result_review_fixture(tmp_path, "BOOTSTRAP")
    bridge = IdleArchitectBridge(response)
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert bridge.messages == []


def test_canonical_handover_envelope_is_strict_and_exact():
    envelope = watcher_module.make_handover_envelope("abc123", "000103", "complete body")
    assert watcher_module.parse_handover_envelope(envelope) == {
        "version": 1, "transactionId": "abc123", "taskId": "000103", "body": "complete body"
    }
    assert watcher_module.parse_handover_envelope(envelope + "\nprose") is None
    assert watcher_module.parse_handover_envelope(envelope.replace("version=1", "version=2")) is None
    assert watcher_module.parse_handover_envelope(envelope.replace("</HANDOVER>", "</HANDOVER></HANDOVER>")) is None
    assert watcher_module.parse_handover_envelope("prose " + envelope) is None


def test_new_rollover_requires_canonical_handover_after_request(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000103.txt"
    prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000102", "nextTaskId": "000103",
                          "nextPromptPath": str(prompt), "rolloverDue": True, "architectMemoryBytes": watcher_module.ARCHITECT_MEMORY_THRESHOLD_BYTES})
    watcher.save()
    class Bridge:
        def submit_result_bounded(self, _payload): pass
    assert watcher.session_rollover.request_if_due(Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY") is True
    tx = watcher.state["rolloverTransactionId"]
    assert watcher.session_rollover._handover_response_valid(
        f"body\nRollover transaction ID: {tx}\nARCHITECT_HANDOVER_READY", tx
    ) is False
    assert watcher.session_rollover._handover_response_valid(
        watcher_module.make_handover_envelope(tx, "000103", "body"), tx
    ) is True


def test_legacy_current_inflight_handover_is_compatibility_only(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"rolloverTransactionId": watcher_module.LEGACY_COMPAT_TRANSACTION_ID,
                          "rolloverTransactionTaskId": watcher_module.LEGACY_COMPAT_TASK_ID})
    response = ("legacy body\nRollover transaction ID: " + watcher_module.LEGACY_COMPAT_TRANSACTION_ID
                + "\n000103\nARCHITECT_HANDOVER_READY")
    assert watcher.session_rollover._handover_response_valid(response) is True
    watcher.state["rolloverTransactionId"] = "new-transaction"
    assert watcher.session_rollover._handover_response_valid(response) is False


def test_canonical_handover_matches_transaction_and_task_exactly(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"rolloverTransactionId": "tx-1", "rolloverTransactionTaskId": "000103",
                          "rolloverHandoverProtocolVersion": 1})
    valid = watcher_module.make_handover_envelope("tx-1", "000103", "body")
    assert watcher.session_rollover._handover_response_valid(valid) is True
    assert watcher.session_rollover._handover_response_valid(
        watcher_module.make_handover_envelope("tx-2", "000103", "body")) is False
    assert watcher.session_rollover._handover_response_valid(
        watcher_module.make_handover_envelope("tx-1", "000104", "body")) is False


def test_current_legacy_transaction_reconciles_without_resend(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "nextTaskId": "000103",
                          "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": True,
                          "handoverRequested": True, "rolloverAttemptedForTaskId": "000103",
                          "rolloverTransactionId": watcher_module.LEGACY_COMPAT_TRANSACTION_ID,
                          "rolloverTransactionTaskId": watcher_module.LEGACY_COMPAT_TASK_ID,
                          "rolloverHandoverSendState": "AMBIGUOUS", "rolloverMaintenanceState": "RECONCILE_PENDING",
                          "architectConversationId": "OLD"})
    response = ("legacy body\nRollover transaction ID: " + watcher_module.LEGACY_COMPAT_TRANSACTION_ID
                + "\n000103\nARCHITECT_HANDOVER_READY")
    sends = []
    class Bridge:
        def _assistant_entries(self): return [{"id": "existing", "text": response}]
        def submit_result_bounded(self, payload): sends.append(payload)
    monkeypatch.setattr(watcher, "process_pending_handover_response", lambda _bridge, _response: True)
    assert watcher.session_rollover.reconcile_pending_handover(Bridge()) is True
    assert sends == []


def test_deferred_wait_never_uses_zero_delay_and_does_not_inflate_budget(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "nextTaskId": "000103", "rolloverDue": True,
                          "rolloverMaintenanceState": "DEFERRED", "rolloverDeferredForTaskId": "000103",
                          "rolloverAutomaticRecoveryEpochCount": 1, "rolloverAutomaticRecoveryMaxEpochs": 3,
                          "rolloverAutomaticRecoveryNextEligibleAt": 0})
    watcher.save()
    waits = []
    monkeypatch.setattr(watcher_module.time, "sleep", waits.append)
    # An expired cooldown is no longer a passive wait decision; the canonical
    # evaluator makes it eligible for recovery service instead.
    assert watcher_module.passive_deferred_rollover_wait(watcher, poll_interval=0) is False
    assert waits == []
    assert watcher.state["rolloverAutomaticRecoveryEpochCount"] == 1


def test_windows_log_rotation_sharing_violation_is_deferred(tmp_path, monkeypatch, capsys):
    logger, run_id, _ = watcher_module.initialize_runtime_logging(tmp_path)
    handler = logger.handlers[0]
    calls = []
    def denied(_self):
        calls.append(1)
        raise PermissionError(32, "sharing violation")
    monkeypatch.setattr(watcher_module.logging.handlers.RotatingFileHandler, "doRollover", denied)
    handler.maxBytes = 1
    watcher_module.runtime_log(logger, run_id, "FIRST", {"state": "NEXT_PROMPT_READY", "taskId": "000103"}, payload="x")
    watcher_module.runtime_log(logger, run_id, "SECOND", {"state": "NEXT_PROMPT_READY", "taskId": "000103"}, payload="x")
    handler.flush()
    assert calls == [1]
    assert getattr(handler, "rotation_deferred") is True
    assert "FIRST" in Path(handler.baseFilename).read_text(encoding="utf-8") or "SECOND" in Path(handler.baseFilename).read_text(encoding="utf-8")
    assert "LOG_ROTATION_DEFERRED" in capsys.readouterr().err


def test_idle_legacy_consumed_result_review_migrates_and_bootstraps_once(tmp_path):
    watcher, response, fingerprint = _idle_result_review_fixture(tmp_path)
    bridge = IdleArchitectBridge(response)
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert bridge.messages and watcher.state["consumedArchitectResponses"][fingerprint]["origin"] == "RESULT_REVIEW"
    assert watcher.state["architectContactCount"] == 1


def test_idle_legacy_consumed_response_without_completed_context_stays_quiet(tmp_path):
    watcher, response, fingerprint = _idle_result_review_fixture(tmp_path)
    watcher.state.update({"lastCompletedTaskId": "different", "architectDeliveryTaskId": "different"})
    watcher.save()
    bridge = IdleArchitectBridge(response)
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert bridge.messages == []
    assert "origin" not in watcher.state["consumedArchitectResponses"][fingerprint]


def _logging_test_watcher(tmp_path):
    logger, run_id, path = watcher_module.initialize_runtime_logging(tmp_path)
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.runtime_logger = logger
    watcher.runtime_run_id = run_id
    return watcher, logger, Path(path)


def test_runtime_logging_executor_lifecycle_is_single_and_contextual(tmp_path):
    watcher, logger, path = _logging_test_watcher(tmp_path)
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "targetWorktree": str(tmp_path)})
    watcher.save()
    watcher.mark_executor_started("task-1", 1234, tmp_path / "result.txt")
    (tmp_path / "result.txt").write_text("terminal", encoding="utf-8")
    watcher.mark_executor_exit(7, tmp_path / "result.txt")
    logger.handlers[0].flush()
    text = path.read_text(encoding="utf-8")
    assert text.count("event=CODEX_STARTED") == 1
    assert text.count("event=EXECUTOR_RESULT_FOUND") >= 1
    assert "pid=1234" in text and "exitCode=7" in text


def test_runtime_logging_delivery_ambiguity_and_exhaustion_are_explicit(tmp_path):
    watcher, logger, path = _logging_test_watcher(tmp_path)
    result = tmp_path / "result.txt"
    result.write_text("private report body", encoding="utf-8")
    watcher.state.update({"state": "RESULT_READY", "taskId": "task-2", "executorResultPath": str(result)})
    watcher.save()

    class AmbiguousBridge:
        sendActionAttempted = True
        sendActionAcknowledged = False
        def assistant_baseline(self): return {"count": 0, "entries": []}
        def submit_result_bounded(self, _payload): raise RuntimeError("ACK timeout")
        def latest_user_message(self): return None

    with pytest.raises(RuntimeError):
        watcher.deliver_result(AmbiguousBridge())
    logger.handlers[0].flush()
    text = path.read_text(encoding="utf-8")
    assert "event=RESULT_DELIVERY_AMBIGUOUS" in text
    assert "private report body" not in text


def test_runtime_logging_human_required_reasons_and_format_exhaustion(tmp_path):
    watcher, logger, path = _logging_test_watcher(tmp_path)
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-3", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": "task-3"})
    watcher.save()
    watcher.request_format_recovery(type("Bridge", (), {})())
    logger.handlers[0].flush()
    text = path.read_text(encoding="utf-8")
    assert "event=HUMAN_REQUIRED" in text and "reason=FORMAT_RECOVERY_EXHAUSTED" in text


def test_runtime_logging_attach_failure_uses_actual_failure_event(tmp_path):
    watcher, logger, path = _logging_test_watcher(tmp_path)
    error = ValueError("replacement unavailable")
    watcher_module.runtime_log(logger, watcher.runtime_run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId="new")
    watcher_module.runtime_log(logger, watcher.runtime_run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId="new")
    logger.handlers[0].flush()
    text = path.read_text(encoding="utf-8")
    assert "event=ARCHITECT_ATTACH_FAILED" in text and "errorClass=ValueError" in text
    assert "replacement unavailable" in text


class _DeliveryEvidenceBridge:
    def __init__(self, *, user=None, assistant=None, generating=False, latest=None, user_messages=None):
        self._user = user
        self._assistant = assistant
        self._generating = generating
        self._latest = latest
        self._user_messages = user_messages

    def user_baseline(self):
        return self._user

    def assistant_baseline(self):
        return self._assistant

    def generation_visible(self):
        return self._generating

    def latest_user_message(self):
        return self._latest

    def user_message_texts(self):
        return list(self._user_messages or [])


def _baseline(count, text_hash):
    return {"count": count, "text_hash": text_hash}


def test_delivery_missing_user_baseline_does_not_prove_advancement(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["architectDeliveryBaseline"] = _baseline(2, "a" * 64)
    payload, _ = watcher._result_delivery_payload()
    bridge = _DeliveryEvidenceBridge(user=_baseline(3, "b" * 64), assistant=_baseline(2, "a" * 64))
    assert watcher._delivery_evidence_advanced(bridge, payload) is False


def test_delivery_missing_assistant_baseline_does_not_prove_advancement(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["architectDeliveryUserBaseline"] = _baseline(2, "a" * 64)
    payload, _ = watcher._result_delivery_payload()
    bridge = _DeliveryEvidenceBridge(user=_baseline(2, "a" * 64), assistant=_baseline(3, "b" * 64))
    assert watcher._delivery_evidence_advanced(bridge, payload) is False


def test_delivery_missing_baselines_without_independent_evidence_fails_closed(tmp_path):
    watcher = ready(tmp_path)
    payload, _ = watcher._result_delivery_payload()
    bridge = _DeliveryEvidenceBridge(user=_baseline(3, "b" * 64), assistant=_baseline(3, "c" * 64), latest="different")
    assert watcher._delivery_evidence_advanced(bridge, payload) is False


def test_strict_result_proof_rejects_advanced_assistant_user_and_generation_evidence(tmp_path):
    watcher = ready(tmp_path)
    payload, _payload_hash = watcher._result_delivery_payload()
    assert watcher._exact_result_payload_observed(_DeliveryEvidenceBridge(assistant=_baseline(3, "b" * 64)), payload) is False
    assert watcher._exact_result_payload_observed(_DeliveryEvidenceBridge(user=_baseline(3, "b" * 64)), payload) is False
    assert watcher._exact_result_payload_observed(_DeliveryEvidenceBridge(generating=True), payload) is False
    assert watcher._exact_result_payload_observed(_DeliveryEvidenceBridge(user_messages=["unrelated handover response"]), payload) is False


def test_strict_result_proof_inspects_all_user_messages_and_normalizes_controls(tmp_path):
    watcher = ready(tmp_path)
    payload, _payload_hash = watcher._result_delivery_payload()
    later_recovery = "Your previous response for task task-1 was received successfully. Return only the machine-readable envelope."
    bridge = _DeliveryEvidenceBridge(user_messages=[payload, later_recovery])
    assert watcher._exact_result_payload_observed(bridge, payload) is True


def test_architect_user_message_reader_excludes_button_controls():
    class Page:
        def __init__(self): self.script = ""
        def evaluate(self, script):
            self.script = script
            return ["complete result"]
    page = Page()
    assert ArchitectPlaywright(page).user_message_texts() == ["complete result"]
    assert "cloneNode" in page.script and "button,[role=\"button\"]" in page.script


def test_legacy_delivery_token_proof_accepts_markdown_rendering_but_requires_order(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    rendered = "**" + payload.replace("\n", "  \n") + "**"
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(user_messages=[rendered]), payload, payload_hash)
    tokens = watcher._delivery_tokens(payload)
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(user_messages=[" ".join(tokens[:-1])]), payload, payload_hash) is False
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(user_messages=[" ".join(reversed(tokens))]), payload, payload_hash) is False


def test_receipt_marker_is_exact_authority_and_legacy_proof_is_not_used_for_marked_state(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state["architectDeliveryProofVersion"] = watcher_module.ARCHITECT_DELIVERY_PROOF_VERSION
    marker = f"ORCHESTRATOR_DELIVERY_SHA256={payload_hash}"
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(user_messages=[marker]), payload, payload_hash)
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(user_messages=[payload]), payload, payload_hash) is False
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(assistant=marker), payload, payload_hash) is False
    assert watcher._result_payload_proof(_DeliveryEvidenceBridge(user_messages=[f"ORCHESTRATOR_DELIVERY_SHA256={'0' * 64}"]), payload, payload_hash) is False


def test_fresh_delivery_wire_payload_uses_base_hash_once(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    sent = []
    class Bridge:
        def assistant_baseline(self): return _baseline(0, "a" * 64)
        def user_baseline(self): return _baseline(0, "b" * 64)
        def submit_result_bounded(self, message): sent.append(message)
        def user_message_texts(self): return list(sent)
    watcher.deliver_result(Bridge())
    expected = watcher._result_delivery_wire_payload(payload, payload_hash)
    assert sent == [expected]
    assert sent[0].count(f"ORCHESTRATOR_DELIVERY_SHA256={payload_hash}") == 1
    assert watcher.state["architectDeliveryPayloadHash"] == payload_hash
    assert watcher.state["architectDeliveryProofVersion"] == watcher_module.ARCHITECT_DELIVERY_PROOF_VERSION


def test_sender_success_without_exact_result_proof_is_ambiguous(tmp_path, monkeypatch):
    watcher = ready(tmp_path)
    sent = []
    class Bridge:
        def assistant_baseline(self): return _baseline(0, "a" * 64)
        def user_baseline(self): return _baseline(0, "b" * 64)
        def submit_result_bounded(self, message): sent.append(message)
        def latest_user_message(self): return "unrelated message"
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(watcher_module.time, "monotonic", iter([0, 3]).__next__)
    with pytest.raises(ResultSubmissionError, match="ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED"):
        watcher.deliver_result(Bridge())
    assert len(sent) == 1
    assert watcher.state["architectSendState"] == "AMBIGUOUS"
    assert watcher.state["state"] == "RESULT_READY"


def test_false_result_reconciliation_recovery_restores_only_proven_corruption(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    invalid = "invalid-stop-fingerprint"
    format_request = "Your previous response for task task-1 was received successfully but did not contain a valid ORCHESTRATOR_RESULT envelope. Return only the machine-readable envelope."
    watcher.state.update({
        "state": "IDLE", "lastCompletedTaskId": "task-1", "architectDeliveryPayloadHash": payload_hash,
        "architectSendState": "CONFIRMED", "architectResultFingerprint": invalid,
        "consumedArchitectResponses": {invalid: {"taskId": "task-1", "action": "STOP"}},
        "nextPromptPath": None, "nextTaskId": "task-1", "documentationClosureFingerprint": invalid,
        "documentationClosureCompletedTaskId": "task-1",
    })
    watcher.save()
    bridge = _DeliveryEvidenceBridge(user_messages=[format_request])
    assert watcher.recover_false_result_reconciliation(bridge) is True
    assert watcher.state["state"] == "RESULT_READY"
    assert watcher.state["architectSendState"] == "FAILED"
    assert watcher.state["architectDeliveryFailureClass"] == "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"
    assert watcher.state["architectDeliveryPayloadHash"] == payload_hash
    assert watcher.state["taskId"] == watcher.state["lastCompletedTaskId"] == "task-1"
    assert watcher.state["architectResultFingerprint"] is None
    assert invalid not in watcher.state["consumedArchitectResponses"]
    assert watcher.state["documentationClosureFingerprint"] is None
    assert watcher.state["documentationClosureCompletedTaskId"] is None


@pytest.mark.parametrize("field_update", [{"nextTaskId": "task-2"}, {"nextPromptPath": "staged-prompt.txt"}])
def test_false_result_reconciliation_rejects_future_staged_work(tmp_path, field_update):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    invalid = "invalid-stop"
    watcher.state.update({"state": "IDLE", "lastCompletedTaskId": "task-1", "architectDeliveryPayloadHash": payload_hash,
                          "architectSendState": "CONFIRMED", "architectResultFingerprint": invalid,
                          "consumedArchitectResponses": {invalid: {"taskId": "task-1", "action": "STOP"}},
                          "nextTaskId": None, "nextPromptPath": None})
    watcher.state.update(field_update)
    watcher.save()
    request = "Your previous response for task task-1 was received successfully. Return only the machine-readable envelope."
    assert watcher.recover_false_result_reconciliation(_DeliveryEvidenceBridge(user_messages=[request])) is False
    assert watcher.state["state"] == "IDLE"


def test_false_result_reconciliation_preserves_legitimate_stop_when_payload_exists(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    invalid = "legitimate-stop"
    request = "Your previous response for task task-1 was received successfully. Return only the machine-readable envelope."
    watcher.state.update({"state": "IDLE", "lastCompletedTaskId": "task-1", "architectDeliveryPayloadHash": payload_hash,
                          "architectSendState": "CONFIRMED", "architectResultFingerprint": invalid,
                          "consumedArchitectResponses": {invalid: {"taskId": "task-1", "action": "STOP"}}})
    watcher.save()
    assert watcher.recover_false_result_reconciliation(_DeliveryEvidenceBridge(user_messages=[payload, request])) is False
    assert watcher.state["state"] == "IDLE"
    assert watcher.state["architectResultFingerprint"] == invalid


def test_false_result_reconciliation_rejects_unrelated_idle_stop(tmp_path):
    watcher = ready(tmp_path)
    _payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"state": "IDLE", "lastCompletedTaskId": "other", "architectDeliveryPayloadHash": payload_hash,
                          "architectSendState": "CONFIRMED", "architectResultFingerprint": "stop",
                          "consumedArchitectResponses": {"stop": {"taskId": "different", "action": "STOP"}}})
    watcher.save()
    assert watcher.recover_false_result_reconciliation(_DeliveryEvidenceBridge(user_messages=["Return only the machine-readable envelope."])) is False


def test_legacy_task_000023_assistant_baseline_alone_cannot_reconcile(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["taskId"] = "000023"
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED",
        "architectDeliveryPayloadHash": payload_hash,
        "architectDeliveryBaseline": _baseline(2, "a" * 64),
        "architectDeliveryUserBaseline": None,
    })
    bridge = _DeliveryEvidenceBridge(assistant=_baseline(3, "b" * 64))
    assert watcher.reconcile_exhausted_result_delivery(bridge) is False
    assert watcher.state["state"] == "HUMAN_REQUIRED"


def test_legacy_task_000023_unchanged_assistant_baseline_remains_human_required(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["taskId"] = "000023"
    _, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED",
        "architectDeliveryPayloadHash": payload_hash,
        "architectDeliveryBaseline": _baseline(2, "a" * 64),
        "architectDeliveryUserBaseline": None,
    })
    assert watcher.reconcile_exhausted_result_delivery(_DeliveryEvidenceBridge(assistant=_baseline(2, "a" * 64))) is False
    assert watcher.state["state"] == "HUMAN_REQUIRED"


def test_generation_and_exact_user_match_remain_independent_delivery_evidence(tmp_path):
    watcher = ready(tmp_path)
    payload, _ = watcher._result_delivery_payload()
    assert watcher._delivery_evidence_advanced(_DeliveryEvidenceBridge(generating=True), payload) is True
    assert watcher._delivery_evidence_advanced(_DeliveryEvidenceBridge(latest=payload), payload) is True


def test_ambiguous_post_send_recovery_performs_no_second_send(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"architectDeliveryPayloadHash": payload_hash, "architectSendState": "AMBIGUOUS", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS", "architectDeliveryBaseline": _baseline(2, "a" * 64)})
    sends = []
    class Bridge(_DeliveryEvidenceBridge):
        def submit_result_bounded(self, message): sends.append(message)
    with pytest.raises(ResultSubmissionError):
        watcher.deliver_result(Bridge(assistant=_baseline(2, "a" * 64), latest="different"))
    assert sends == []


class _RichEditor:
    def __init__(self, text, clear=True):
        self.text = text
        self.clear = clear
        self.actions = []

    def inner_text(self, **_): return self.text
    def focus(self, **_): self.actions.append("focus")
    def press(self, key, **_):
        self.actions.append(key)
        if key == "ControlOrMeta+A" and self.clear:
            self.text = ""


def test_stale_composer_uses_rich_editor_clear_and_verifies_empty(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    editor = _RichEditor(payload)
    bridge = type("Bridge", (), {"_live_composer": lambda self: editor})()
    assert watcher.clear_confirmed_stale_composer(bridge, payload, payload_hash) is True
    assert editor.actions == ["focus", "ControlOrMeta+A", "Backspace"]
    assert editor.text == ""


def test_stale_composer_cleanup_does_not_clear_unrelated_or_unverified_text(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    unrelated = _RichEditor("user draft")
    bridge = type("Bridge", (), {"_live_composer": lambda self: unrelated})()
    assert watcher.clear_confirmed_stale_composer(bridge, payload, payload_hash) is False
    assert unrelated.actions == [] and unrelated.text == "user draft"
    failed = _RichEditor(payload, clear=False)
    bridge = type("Bridge", (), {"_live_composer": lambda self: failed})()
    assert watcher.clear_confirmed_stale_composer(bridge, payload, payload_hash) is False
    assert failed.text == payload


def test_confirmed_delivery_ignores_stale_composer_acquisition_failure(tmp_path):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"architectDeliveryPayloadHash": payload_hash, "architectSendState": "FAILED",
                          "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"})
    watcher.save()

    class Bridge:
        def generation_visible(self): return False
        def user_message_texts(self): return [payload]
        def assistant_baseline(self): return {"count": 1, "text_hash": "baseline"}
        def _live_composer(self): raise PlaywrightTimeoutError("composer lookup timed out")
        def submit_result_bounded(self, _message): raise AssertionError("confirmed result must not resend")

    bridge = Bridge()
    assert watcher.deliver_result_with_recovery(lambda: (_ for _ in ()).throw(AssertionError("must retain bridge")), initial_bridge=bridge) is bridge
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert watcher.state["architectSendState"] == "CONFIRMED"
    assert watcher.state["architectTransportRecoveryCount"] == 0


@pytest.mark.parametrize("failure", ["inner_text", "focus", "press"])
def test_confirmed_stale_composer_cleanup_operation_is_best_effort(tmp_path, failure):
    watcher = ready(tmp_path)
    payload, payload_hash = watcher._result_delivery_payload()

    class Composer:
        def inner_text(self, **_):
            if failure == "inner_text": raise PlaywrightTimeoutError("read timed out")
            return payload
        def focus(self, **_):
            if failure == "focus": raise PlaywrightTimeoutError("focus timed out")
        def press(self, *_args, **_kwargs):
            if failure == "press": raise PlaywrightTimeoutError("clear timed out")

    class Bridge:
        def _live_composer(self): return Composer()

    assert watcher.clear_confirmed_stale_composer(Bridge(), payload, payload_hash) is False
    assert watcher.state.get("state") == "RESULT_READY"


def test_architect_stop_clears_stale_transport_reason(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["humanRequiredReason"] = "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"
    watcher.accept_architect_response(envelope("task-1", action="STOP"))
    assert watcher.state["state"] == "IDLE"
    assert watcher.state["humanRequiredReason"] is None


def test_architect_human_required_replaces_stale_transport_reason(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["humanRequiredReason"] = "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"
    watcher.accept_architect_response(envelope("task-1", action="HUMAN_REQUIRED"))
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_DECISION_HUMAN_REQUIRED"
    assert watcher.reconcile_exhausted_result_delivery(_DeliveryEvidenceBridge(generating=True)) is False


def test_human_decision_wait_accepts_discussion_without_format_recovery(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", action="HUMAN_REQUIRED", documentation="COMPLETE"))
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    baseline = {"count": 3, "text_hash": "discussion"}
    assert watcher_module.resident_human_decision_response(watcher, "Rony's decision is being discussed...", baseline) == "DISCUSSION"
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_DECISION_HUMAN_REQUIRED"
    assert watcher.state["architectBaseline"] == baseline


def test_human_decision_wait_repeated_human_and_later_execute_stage_normally(tmp_path):
    watcher, base, _worktree = recovery_fixture(tmp_path)
    result = Path(watcher.state["executorResultPath"])
    result.write_text("completed", encoding="utf-8")
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "recovery-safety-task",
                          "lastCompletedTaskId": "recovery-safety-task"})
    first = envelope("recovery-safety-task", action="HUMAN_REQUIRED", documentation="COMPLETE")
    assert watcher.accept_architect_response(first)["action"] == "HUMAN_REQUIRED"
    assert watcher_module.resident_human_decision_response(watcher, first, {"count": 1, "text_hash": "a"}) == "DUPLICATE"
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    execute = envelope("recovery-safety-task", prompt=configured_project_prompt(base, head, "next bounded work"), documentation="COMPLETE")
    assert watcher_module.resident_human_decision_response(watcher, execute, {"count": 2, "text_hash": "b"}) == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000001"


def test_run_executor_state_keeps_architect_human_decision_resident(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED"})
    assert run_executor_state_once(watcher, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) == "HUMAN_REQUIRED"


def test_unhandled_human_required_waits_reloads_and_preserves_workflow(tmp_path, monkeypatch):
    watcher = ready(tmp_path)
    prompt = tmp_path / "staged.txt"
    prompt.write_text("staged", encoding="utf-8")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-1", "lastCompletedTaskId": "task-0",
                          "nextTaskId": "task-1", "nextPromptPath": str(prompt),
                          "humanRequiredReason": "EXECUTOR_EXITED_WITHOUT_RESULT"})
    watcher.save()
    slept = []
    monkeypatch.setattr(watcher_module.time, "sleep", lambda value: slept.append(value))
    assert watcher_module.passive_human_required_wait(watcher, 0.25) == "HUMAN_REQUIRED"
    assert slept == [0.25]
    assert watcher.state["taskId"] == "task-1"
    assert watcher.state["nextTaskId"] == "task-1"
    assert watcher.state["nextPromptPath"] == str(prompt)
    assert watcher.state["humanRequiredReason"] == "EXECUTOR_EXITED_WITHOUT_RESULT"


def test_result_transport_exhausted_remains_resident_without_executor_rerun(tmp_path, monkeypatch):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED",
                          "taskId": "task-1", "lastCompletedTaskId": "task-1"})
    watcher.save()
    launches = []
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _value: None)
    assert watcher_module.passive_human_required_wait(watcher, 0) == "HUMAN_REQUIRED"
    assert launches == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"


def test_architect_execute_clears_stale_transport_reason(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state.update({"taskId": "000021", "taskSequence": 21, "state": "ARCHITECT_RUNNING", "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"})
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    watcher.accept_architect_response(envelope("000021", prompt=configured_project_prompt(base, head, "next-000022")))
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["humanRequiredReason"] is None
    assert watcher.state["nextTaskId"] == "000022"


def test_exhausted_delivery_reconciliation_clears_stale_reason(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["taskId"] = "000023"
    payload, payload_hash = watcher._result_delivery_payload()
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED", "architectDeliveryPayloadHash": payload_hash, "architectDeliveryBaseline": _baseline(1, "a" * 64)})
    assert watcher.reconcile_exhausted_result_delivery(_DeliveryEvidenceBridge(assistant=_baseline(2, "b" * 64), user_messages=[payload])) is True
    assert watcher.state["humanRequiredReason"] is None


def test_executor_success_clears_stale_human_reason(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "result.txt"
    result.write_text("completed", encoding="utf-8")
    watcher.state.update({"taskId": "000025", "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED"})
    watcher._record_executor_success("000025", result, 0)
    assert watcher.state["state"] == "RESULT_READY"
    assert watcher.state["humanRequiredReason"] is None


def test_documentation_field_is_optional_and_defaults_not_required():
    parsed = parse_orchestrator_result(envelope("task-doc"), "task-doc")
    assert parsed["documentation"] == "NOT_REQUIRED"
    assert parse_orchestrator_result(envelope("task-doc", documentation="COMPLETE"), "task-doc")["documentation"] == "COMPLETE"


def test_documentation_required_stages_one_normal_sequential_task(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state.update({"taskId": "000021", "taskSequence": 21, "state": "ARCHITECT_RUNNING"})
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    response = envelope("000021", prompt=configured_project_prompt(base, head, "documentation closure"), documentation="REQUIRED")
    watcher.accept_architect_response(response)
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000022"
    assert watcher.state["documentationClosurePending"] is True
    assert watcher.state["documentationClosureSourceTaskId"] == "000021"
    assert watcher.state["documentationClosureTaskId"] == "000022"
    assert Path(watcher.state["nextPromptPath"]).read_text(encoding="utf-8") == configured_project_prompt(base, head, "documentation closure")


def test_documentation_pending_survives_restart_and_blocks_bypass(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000021", "nextTaskId": "000022", "documentationClosurePending": True})
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.state["documentationClosurePending"] is True
    with pytest.raises(ValueError, match="DOCUMENTATION_CLOSURE_REQUIRED"):
        restarted.accept_architect_response(envelope("000021", action="STOP"))
    assert restarted.state["state"] == "HUMAN_REQUIRED"


def test_complete_documentation_clears_pending_and_honors_stop(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"taskId": "000022", "taskSequence": 22, "state": "ARCHITECT_RUNNING", "documentationClosurePending": True})
    watcher.accept_architect_response(envelope("000022", action="STOP", documentation="COMPLETE"))
    assert watcher.state["state"] == "IDLE"
    assert watcher.state["documentationClosurePending"] is False
    assert watcher.state["documentationClosureCompletedTaskId"] == "000022"


def test_complete_documentation_can_be_architect_performed_without_extra_child(tmp_path):
    watcher = ready(tmp_path)
    watcher.state.update({"taskId": "000025", "state": "ARCHITECT_RUNNING", "documentationClosurePending": True})
    watcher.accept_architect_response(envelope("000025", action="HUMAN_REQUIRED", documentation="COMPLETE"))
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["documentationClosurePending"] is False
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_DECISION_HUMAN_REQUIRED"


def test_documentation_complete_execute_stages_next_ordinary_task_once(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    watcher.state.update({"taskId": "000022", "taskSequence": 22, "state": "ARCHITECT_RUNNING", "documentationClosurePending": True})
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    watcher.accept_architect_response(envelope("000022", prompt=configured_project_prompt(base, head, "ordinary next work"), documentation="COMPLETE"))
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000023"
    assert watcher.state["documentationClosurePending"] is False


def test_discussion_pause_dispatch_is_durable_and_survives_restart(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "000025", "lastCompletedTaskId": "000024", "codexPid": 1234})
    watcher.save()
    controller = DiscussionHotkeyController(watcher, emit=lambda _message: None)
    assert controller.dispatch("F9") is True
    assert watcher.discussion_pause_active() is True
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.discussion_pause_active() is True
    assert restarted.state["taskId"] == "000025"
    assert controller.dispatch("F10") is True
    assert watcher.discussion_pause_active() is False


def test_discussion_pause_allows_executor_observation_but_blocks_new_actions(tmp_path, monkeypatch):
    watcher = ready(tmp_path)
    watcher.state.update({"discussionPauseActive": True, "state": "RESULT_READY", "taskId": "000025"})
    sends = []
    class Bridge:
        def submit_result_bounded(self, message): sends.append(message)
    watcher.deliver_result(Bridge())
    assert sends == [] and watcher.state["state"] == "RESULT_READY"
    watcher.state.update({"state": "NEXT_PROMPT_READY", "nextPromptPath": str(tmp_path / "missing-prompt"), "nextTaskId": "000026"})
    launches = []
    assert watcher.launch_next(lambda *_: launches.append(1)) is None
    assert launches == []
    watcher.state.update({"state": "IDLE"})
    assert watcher.request_architect_bootstrap(Bridge()) is False
    assert watcher.intake_inbox(lambda *_: launches.append(1)) is False


def test_discussion_pause_does_not_abort_executor_completion(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    result = tmp_path / "result.txt"
    result.write_text("completed", encoding="utf-8")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "000025", "codexPid": 4321, "executorResultPath": str(result), "discussionPauseActive": True})
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    assert run_executor_state_once(watcher, lambda *_: (_ for _ in ()).throw(AssertionError("must not relaunch"))) == "RESULT_READY"
    assert watcher.state["state"] == "RESULT_READY"
    assert watcher.state["discussionPauseActive"] is True


def test_discussion_resume_restores_normal_result_delivery_eligibility(tmp_path):
    watcher = ready(tmp_path)
    watcher.state["discussionPauseActive"] = True
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    sent = []
    class Bridge:
        def assistant_baseline(self): return {"count": 1, "text_hash": "a" * 64}
        def user_baseline(self): return {"count": 1, "text_hash": "b" * 64}
        def submit_result_bounded(self, message): sent.append(message)
        def latest_user_message(self): return sent[-1] if sent else None
    watcher.deliver_result(Bridge())
    assert len(sent) == 1


def test_discussion_resume_does_not_change_human_required(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "discussionPauseActive": True})
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_DECISION_HUMAN_REQUIRED"


class _PostDiscussionBridge:
    def __init__(self, entries):
        self.entries = list(entries)
        self.sent = []
        self.generating = False

    def generation_visible(self):
        return self.generating

    def assistant_baseline(self):
        snapshot = json.dumps(self.entries, separators=(",", ":"), ensure_ascii=False)
        return {"count": len(self.entries), "text_hash": hashlib.sha256(snapshot.encode()).hexdigest(), "entries": list(self.entries)}

    def latest_assistant_entry(self):
        return self.entries[-1] if self.entries else None

    def wait_for_new_response(self, baseline, poll_interval=0.5):
        if self.generating:
            return {"state": "RUNNING", "text": ""}
        if len(self.entries) > int(baseline.get("count", 0)):
            return {"state": "COMPLETED", "text": str(self.entries[-1].get("text") or "")}
        return {"state": "WAIT", "text": ""}

    def submit_result_bounded(self, message):
        self.sent.append(message)

    def close(self):
        return None


def _discussion_bridge_response(text, identifier):
    return {"id": identifier, "text": text}


def test_post_discussion_resume_restores_machine_protocol_and_accepts_valid_envelope(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-discuss", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 1, "text_hash": "before"}})
    watcher.save()
    bridge = _PostDiscussionBridge([_discussion_bridge_response("discussion decision", "old")])
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert watcher.state["postDiscussionResumeEpoch"] == 1
    response = envelope("task-discuss", action="STOP")
    bridge.entries.append(_discussion_bridge_response(response, "decision"))
    assert resident_human_decision_response(watcher, response, bridge=bridge) == "STOP"
    assert watcher.state["state"] == "IDLE"
    assert watcher.state["postDiscussionEnvelopeRequired"] is False
    assert bridge.sent == []


def test_post_discussion_response_completed_before_f10_is_consumed_after_resume(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-before-resume", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 1, "text_hash": "before"}})
    bridge = _PostDiscussionBridge([
        _discussion_bridge_response("initial human-required envelope", "initial"),
        _discussion_bridge_response(envelope("task-before-resume", action="STOP"), "final-before-f10"),
    ])
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    response = bridge.entries[-1]["text"]
    assert watcher.reconcile_post_discussion_response(bridge, response) == "STOP"
    assert watcher.state["state"] == "IDLE"
    assert bridge.sent == []


def test_post_discussion_conversation_while_paused_never_repairs(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-discuss", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED"})
    bridge = _PostDiscussionBridge([])
    watcher._write_discussion_pause_marker(True)
    assert resident_human_decision_response(watcher, "ordinary conversational discussion", bridge=bridge) == "DISCUSSION"
    assert bridge.sent == []
    assert not watcher.state.get("postDiscussionEnvelopeRequired")


def test_post_discussion_missing_envelope_repairs_once_and_accepts_correction(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-discuss", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 1, "text_hash": "before"}})
    watcher.save()
    bridge = _PostDiscussionBridge([_discussion_bridge_response("discussion", "old")])
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    missing = "Proceeding with the already decided bounded task.\nWORKTREE\nC:\\worktree"
    bridge.entries.append(_discussion_bridge_response(missing, "decision"))
    assert watcher.reconcile_post_discussion_response(bridge, missing) == "REPAIR_REQUESTED"
    assert len(bridge.sent) == 1
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    corrected = envelope("task-discuss", action="STOP")
    bridge.entries.append(_discussion_bridge_response(corrected, "repair"))
    assert watcher.reconcile_post_discussion_response(bridge, corrected) == "STOP"
    assert len(bridge.sent) == 1
    assert watcher.state["state"] == "IDLE"


def _staged_prompt_missing_envelope_fixture(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = watcher.prompts_dir / "000103.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt_text = "INVOICE.PAYMENT.POSTSAVE.EXACT.RECEIPT.UX.HANDOFF.1A\nexact staged task\n"
    prompt.write_text(prompt_text, encoding="utf-8")
    worktree = tmp_path / "task-000103-worktree"
    worktree.mkdir()
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000102", "lastCompletedTaskId": "000102",
        "nextTaskId": "000103", "nextPromptPath": str(prompt),
        "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": False,
        "handoverRequested": True, "handoverReady": False,
        "rolloverHandoverSendState": "AMBIGUOUS", "rolloverMaintenanceState": "DEFERRED",
        "rolloverRecoveryState": "DEFERRED", "rolloverRecoveryAttemptCount": 2,
        "rolloverAutomaticRecoveryEpochCount": 1, "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverTransactionId": "7fdbd798659f42295a18dd2d",
        "rolloverTransactionTaskId": "000103", "rolloverAttemptedForTaskId": "000103",
        "rolloverRecoveryTerminalReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        "discussionPauseActive": True, "architectDiscussionBaseline": {"count": 1, "text_hash": "before"},
        "taskWorktrees": {"000103": {"taskId": "000103", "worktreePath": str(worktree)}},
    })
    watcher.save()
    watcher._write_discussion_pause_marker(True)
    return watcher, prompt, prompt_text


@pytest.mark.parametrize("kind", ["valid", "partial", "different"], ids=["valid-final", "partial", "stale-different"])
def test_legacy_reemission_observation_artifact_preserves_exact_payload(tmp_path, kind):
    watcher, _prompt, _prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    watcher.state["rolloverHandoverProtocolVersion"] = None
    response = (
        "Authoritative legacy handover for task 000103\nRollover transaction ID: 7fdbd798659f42295a18dd2d\n"
        "ARCHITECT_HANDOVER_READY"
    )
    if kind == "partial":
        response = "partial legacy handover \N{SNOWMAN}\n"
    elif kind == "different":
        response = "stale response for unrelated transaction\nARCHITECT_HANDOVER_READY"
    rollover = watcher_module.ArchitectSessionRollover(watcher)

    class Bridge:
        last_wait_diagnostics = {
            "baselineAssistantCount": 4,
            "candidateLatestAssistantIdentity": "assistant-5",
            "completionReason": "HANDOVER_READY_MARKER" if kind == "valid" else "STABLE_TWO_POLL_NO_COMPLETION_MARKER",
        }

    metadata = rollover._capture_legacy_handover_reemission_observation(
        Bridge(), {"state": "COMPLETED" if kind != "partial" else "BLOCKED", "text": response},
        watcher_module.LEGACY_COMPAT_TRANSACTION_ID, "000103", 2,
    )
    artifact = Path(metadata["artifactPath"])
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    exact_bytes = response.encode("utf-8")
    assert metadata["artifactWritten"] is True
    assert saved["observedText"] == response
    assert saved["textUtf8Bytes"] == len(exact_bytes)
    assert saved["textLengthChars"] == len(response)
    assert saved["textSha256"] == hashlib.sha256(exact_bytes).hexdigest()
    assert saved["textPrefix"] == response[:200]
    assert saved["textSuffix"] == response[-500:]
    assert saved["waiterDiagnostics"]["candidateLatestAssistantIdentity"] == "assistant-5"
    assert saved["architectHandoverReady"] is (kind != "partial")
    assert saved["transactionMatches"] is (kind == "valid")
    assert saved["handoverResponseValid"] is (kind == "valid")
    assert saved["rejectionReason"] == (None if kind == "valid" else "OBSERVED_STATE_BLOCKED" if kind == "partial" else "TRANSACTION_MISMATCH")
    assert artifact.parent == watcher.state_dir / "logs" / "diagnostic" / "legacy-handover-reemission"


def test_invalid_legacy_reemission_artifact_is_durable_before_rejection_save(tmp_path, monkeypatch):
    watcher, _prompt, _prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    watcher.state.update({"discussionPauseActive": False, "rolloverRecoveryEpoch": 2,
                          "rolloverHandoverProtocolVersion": None})
    watcher.save()
    rollover = watcher_module.ArchitectSessionRollover(watcher)
    returned = "different response without the authorized transaction"
    artifact_paths = []
    original_save = watcher.save

    def check_artifact_before_rejection_save(*args, **kwargs):
        if watcher.state.get("rolloverLegacyHandoverReemissionState") == "INVALID_RESPONSE":
            artifact_paths.extend((watcher.state_dir / "logs" / "diagnostic" / "legacy-handover-reemission").glob("*.json"))
            assert artifact_paths, "waiter payload must be durable before invalid-response state is saved"
            saved = json.loads(artifact_paths[0].read_text(encoding="utf-8"))
            assert saved["observedText"] == returned
            assert saved["textSha256"] == hashlib.sha256(returned.encode("utf-8")).hexdigest()
        return original_save(*args, **kwargs)

    monkeypatch.setattr(watcher, "save", check_artifact_before_rejection_save)

    class Bridge:
        def assistant_baseline(self): return {"count": 1, "text_hash": "baseline", "entries": []}
        def submit_result_bounded(self, _message): pass
        def wait_for_new_response(self, _baseline, poll_interval=0.5):
            assert poll_interval == 0.5
            return {"state": "COMPLETED", "text": returned}

    status, response = rollover._request_legacy_handover_reemission_once(Bridge(), "000103")
    assert status == "INVALID" and response is None
    assert artifact_paths


def test_missing_envelope_exact_staged_prompt_repairs_without_regeneration(tmp_path):
    watcher, prompt, prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    bridge = _PostDiscussionBridge([_discussion_bridge_response("old", "old")])
    watcher.request_discussion_resume()
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert watcher.state["postDiscussionProtocolTaskId"] == "000103"
    missing = "Proceeding with the already prepared exact Executor task."
    bridge.entries.append(_discussion_bridge_response(missing, "missing-envelope"))
    assert watcher.reconcile_post_discussion_response(bridge, missing) == "REPAIR_REQUESTED"
    corrected = envelope("000103", prompt=prompt_text)
    bridge.entries.append(_discussion_bridge_response(corrected, "repair"))
    before_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
    assert watcher.reconcile_post_discussion_response(bridge, corrected) == "EXECUTE"
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert watcher.state["nextTaskId"] == "000103"
    assert hashlib.sha256(prompt.read_bytes()).hexdigest() == before_hash
    assert bridge.sent and len(bridge.sent) == 1


@pytest.mark.parametrize("mutation", ["task", "transaction", "path", "missing"])
def test_missing_envelope_staged_prompt_identity_mismatch_fails_closed(tmp_path, mutation):
    watcher, prompt, prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    bridge = _PostDiscussionBridge([_discussion_bridge_response("old", "old")])
    watcher.request_discussion_resume()
    missing = "Already prepared prompt, but no machine envelope."
    bridge.entries.append(_discussion_bridge_response(missing, "missing-envelope"))
    assert watcher.reconcile_post_discussion_response(bridge, missing) == "REPAIR_REQUESTED"
    if mutation == "task":
        corrected = envelope("000104", prompt=prompt_text)
        bridge.entries.append(_discussion_bridge_response(corrected, "wrong-task"))
    elif mutation == "transaction":
        watcher.state["rolloverTransactionId"] = "different-transaction"
        corrected = envelope("000103", prompt=prompt_text)
        bridge.entries.append(_discussion_bridge_response(corrected, "wrong-transaction"))
    elif mutation == "path":
        watcher.state["nextPromptPath"] = str(tmp_path / "other.txt")
        corrected = envelope("000103", prompt=prompt_text)
        bridge.entries.append(_discussion_bridge_response(corrected, "wrong-path"))
    else:
        prompt.unlink()
        corrected = envelope("000103", prompt=prompt_text)
        bridge.entries.append(_discussion_bridge_response(corrected, "missing-prompt"))
    assert watcher.reconcile_post_discussion_response(bridge, corrected) == "FAILED"
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED"


def test_missing_envelope_reproduction_at_accepted_head_was_not_required_path(tmp_path):
    watcher, _prompt, _prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    watcher.state["discussionPauseActive"] = False
    bridge = _PostDiscussionBridge([])
    assert watcher.reconcile_post_discussion_response(bridge, "complete prose without envelope") == "NOT_REQUIRED"
    assert bridge.sent == []


def test_public_next_prompt_path_gates_rollover_until_exact_staged_envelope_is_repaired(tmp_path, monkeypatch):
    watcher, prompt, prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    watcher.state.update({"rolloverTransactionId": "canonical-new-transaction", "rolloverHandoverProtocolVersion": 1})
    watcher.save()
    bridge = _PostDiscussionBridge([_discussion_bridge_response("old", "old")])
    watcher.state["architectDiscussionBaseline"] = bridge.assistant_baseline()
    watcher.save()
    watcher.request_discussion_resume()
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert watcher.state["postDiscussionProtocolTaskId"] == "000103"
    before = prompt.read_bytes()
    rollover_calls = []
    launches = []
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: rollover_calls.append("rollover") or False)

    # No newer response: the protocol waits and rollover remains untouched.
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: launches.append(1), "endpoint", lambda: False) is None
    assert rollover_calls == [] and launches == []
    assert watcher.state["rolloverRecoveryAttemptCount"] == 2

    # A completed response without an envelope requests exactly one repair.
    bridge.entries.append(_discussion_bridge_response("already prepared task, missing envelope", "missing"))
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: launches.append(1), "endpoint", lambda: False) is None
    assert len(bridge.sent) == 1
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert rollover_calls == [] and launches == []

    # The exact corrected envelope clears only the protocol gate and preserves bytes.
    corrected = envelope("000103", prompt=prompt_text)
    bridge.entries.append(_discussion_bridge_response(corrected, "corrected"))
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: launches.append(1), "endpoint", lambda: False) is None
    assert watcher.state["postDiscussionEnvelopeRequired"] is False
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert prompt.read_bytes() == before
    assert rollover_calls == [] and launches == []

    # Only the following public iteration may reach ordinary rollover service.
    monkeypatch.setattr(
        watcher_module,
        "_evaluate_public_rollover_boundary",
        lambda *_args, **_kwargs: watcher_module.RolloverDecision(watcher_module.RolloverAction.NO_ROLLOVER, "TEST"),
    )
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: launches.append(1), "endpoint", lambda: False) is None
    assert rollover_calls == ["rollover"]
    assert launches == []


@pytest.mark.parametrize("scenario", ["one-call", "multi-iteration", "invisible-history"], ids=["one-call", "multi-iteration", "invisible-history"])
def test_preserved_legacy_transaction_completes_end_to_end_through_public_path(tmp_path, monkeypatch, scenario):
    watcher, prompt, prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    prompt_bytes_before = prompt.read_bytes()
    prompt_hash_before = hashlib.sha256(prompt_bytes_before).hexdigest()
    tx = watcher_module.LEGACY_COMPAT_TRANSACTION_ID
    session_id = watcher_module.AFFOTECH_EXECUTOR_SESSION_ID
    legacy_handover = (
        "Authoritative AFFOTECH state for already-staged task 000103.\n"
        f"Rollover transaction ID: {tx}\nARCHITECT_HANDOVER_READY"
    )
    watcher.state.update({
        "discussionPauseActive": True,
        "executorSessionId": session_id,
        "executorSessionMode": "PERSISTENT",
        "executorProcessState": "COMPLETED_WITH_RESULT",
        "rolloverDeferredForTaskId": "000103",
        "rolloverAutomaticRecoveryNextEligibleAt": 1.0,
        "rolloverRecoveryRetryAfter": 1.0,
        "rolloverHandoverProtocolVersion": None,
        "architectConversationId": "OLD-ARCHITECT",
        "postDiscussionEnvelopeRequired": False,
        "postDiscussionEnvelopeRepairAttempted": False,
    })
    # Reproduce the live incident timing: the old response is not visible at
    # the first reconciliation probe and becomes visible only between public
    # NEXT_PROMPT_READY iterations.
    old_entries = [_discussion_bridge_response("old conversation baseline", "baseline")]
    if scenario == "one-call":
        old_entries.append(_discussion_bridge_response(legacy_handover, "preserved-legacy-handover"))
    watcher.state["architectDiscussionBaseline"] = {
        "count": 1,
        "text_hash": hashlib.sha256(json.dumps(old_entries[:1], separators=(",", ":")).encode()).hexdigest(),
    }
    watcher.save()
    watcher._write_discussion_pause_marker(True)

    events = []
    ordered_trace = []

    class Page:
        def __init__(self, conversation_id, assistants, event_name=None):
            self.url = f"https://chatgpt.com/c/{conversation_id}"
            self.assistants = assistants
            self.closed = False
            self.context = None
            self.event_name = event_name
            self.ready_event_emitted = False

        def evaluate(self, script):
            if "stop-button" in script:
                return False
            if 'data-message-author-role="assistant"' in script:
                if (self.url.endswith("NEW-ARCHITECT") and not self.ready_event_emitted
                        and self.assistants and self.assistants[-1].get("text") == "ARCHITECT_SESSION_READY"):
                    self.ready_event_emitted = True
                    events.append("fresh_ready_observed")
                    ordered_trace.append((iteration[0], "fresh_ready"))
                return self.assistants
            if 'data-message-author-role="user"' in script:
                return []
            return []

        def close(self):
            self.closed = True
            events.append("old_architect_closed")
            ordered_trace.append((iteration[0], "old_architect_closed"))

    old_page = Page("OLD-ARCHITECT", old_entries)
    fresh_page = Page("NEW-ARCHITECT", [_discussion_bridge_response("ARCHITECT_SESSION_READY", "ready")])
    old_page.context = type("Context", (), {"pages": [old_page]})()
    fresh_page.context = type("Context", (), {"pages": [fresh_page]})()

    class OldBridge:
        def __init__(self):
            self.page = old_page
            self.sent = []
            self.recovery_requests = []

        def _assistant_entries(self):
            return old_page.assistants

        def assistant_baseline(self):
            return {"count": len(old_page.assistants), "text_hash": hashlib.sha256(json.dumps(old_page.assistants, separators=(",", ":")).encode()).hexdigest()}

        def generation_visible(self):
            return False

        def exact_user_message_payload_observed(self, _payload):
            return True

        def open_fresh_with_handover(self, handover):
            assert watcher.state.get("pending_handover") == handover
            assert watcher.state.get("rolloverHandoverResponseIdentity") == hashlib.sha256(handover.encode("utf-8")).hexdigest()
            ordered_trace.append((iteration[0], "handover_persisted_before_fresh_creation"))
            events.append("fresh_created")
            events.append("fresh_bootstrap_sent")
            ordered_trace.extend([(iteration[0], "fresh_architect_created"), (iteration[0], "fresh_bootstrap_sent")])
            self.sent.append(handover)
            return fresh_page

        def submit_result_bounded(self, message):
            assert message.startswith("ARCHITECT HANDOVER TRANSPORT RECOVERY")
            assert tx in message and "000103" in message
            self.recovery_requests.append(message)
            ordered_trace.append((iteration[0], "legacy_handover_reemission_requested"))

        def wait_for_new_response(self, _baseline, poll_interval=0.5):
            assert self.recovery_requests, "must not fabricate a handover without the bounded recovery request"
            assert poll_interval > 0
            if scenario == "multi-iteration":
                ordered_trace.append((iteration[0], "reemission_response_not_yet_visible"))
                raise TimeoutError("synthetic delayed re-emission response")
            ordered_trace.append((iteration[0], "legacy_handover_reemitted"))
            return {"state": "COMPLETED", "text": legacy_handover}

        def close(self):
            return None

    old_bridge = OldBridge()

    class FreshProtocolBridge(_PostDiscussionBridge):
        def __init__(self):
            super().__init__([_discussion_bridge_response("ARCHITECT_SESSION_READY", "ready")])

    fresh_protocol_bridge = FreshProtocolBridge()
    monkeypatch.setattr(
        watcher_module.ArchitectPlaywright,
        "attach",
        staticmethod(lambda _endpoint, conversation: old_bridge if conversation == "OLD-ARCHITECT" else fresh_protocol_bridge),
    )
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _watcher, _bridge, requested: requested)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher_module.time, "time", lambda: 100.0)

    original_process_handover = watcher.process_pending_handover_response

    def observe_handover(bridge, response):
        assert response == legacy_handover
        events.append("legacy_handover_validated")
        ordered_trace.append((iteration[0], "legacy_handover_validated"))
        return original_process_handover(bridge, response)

    monkeypatch.setattr(watcher, "process_pending_handover_response", observe_handover)
    original_save = watcher.save
    committed = []

    def observe_save(*args, **kwargs):
        result = original_save(*args, **kwargs)
        if (watcher.state.get("architectConversationId") == "NEW-ARCHITECT"
                and watcher.state.get("rolloverDue") is False
                and not committed):
            committed.append(True)
            events.append("authority_committed")
            ordered_trace.append((iteration[0], "authority_committed"))
        return result

    monkeypatch.setattr(watcher, "save", observe_save)

    resume_bridge = _PostDiscussionBridge([_discussion_bridge_response("old discussion", "discussion")])
    watcher.state["architectDiscussionBaseline"] = resume_bridge.assistant_baseline()
    watcher.save()
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert watcher.state["postDiscussionProtocolTaskId"] == "000103"
    assert watcher.state["discussionPauseActive"] is False
    assert watcher.discussion_pause_active() is False
    assert watcher_module._legacy_handover_must_precede_post_discussion_gate(watcher) is True
    public_decision = watcher_module._evaluate_public_rollover_boundary(watcher, watcher.discussion_pause_active)
    assert public_decision.action is watcher_module.RolloverAction.START_RECOVERY_EPOCH, public_decision

    launches = []

    def launch_executor(_prompt, _result_path):
        launches.append("000103")
        events.append("executor_launch")
        return type("Process", (), {"pid": 91003})()

    protocol_gate_targets = []
    original_protocol_gate = watcher_module.service_post_discussion_protocol_gate_once

    def observe_protocol_gate(*args, **kwargs):
        protocol_gate_targets.append(watcher.state.get("architectConversationId"))
        return original_protocol_gate(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "service_post_discussion_protocol_gate_once", observe_protocol_gate)

    service_trace = []
    iteration = [1]
    original_service = watcher_module.service_deferred_rollover_once

    def observe_service(*args, **kwargs):
        result = original_service(*args, **kwargs)
        service_trace.append((iteration[0], result, watcher.state.get("rolloverInProgress"), watcher.state.get("rolloverMaintenanceState")))
        ordered_trace.append((iteration[0], f"rollover_service_{str(bool(result)).lower()}"))
        return result

    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", observe_service)

    # This is the public NEXT_PROMPT_READY path. It enters the real dispatcher,
    # recovery service, handover parser, fresh-session readiness proof and commit.
    first_iteration_result = watcher_module.run_next_prompt_ready_once(
        watcher, launch_executor, "endpoint", watcher.discussion_pause_active,
    )
    ordered_trace.insert(0, (1, "public_iteration_started"))
    assert first_iteration_result is None
    if scenario == "multi-iteration":
        assert watcher.state["rolloverInProgress"] is True
        assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
        assert watcher.state["rolloverRecoveryState"] == "RECOVERING"
        assert watcher_module._legacy_handover_must_precede_post_discussion_gate(watcher) is True
        assert launches == []
        assert len(old_bridge.recovery_requests) == 1
        old_entries.append(_discussion_bridge_response(legacy_handover, "preserved-legacy-handover"))
        # Let iteration one's bounded reconciliation deadline expire, then
        # call the same public path for iteration two.
        iteration[0] = 2
        ordered_trace.append((2, "public_iteration_started"))
        monkeypatch.setattr(watcher_module.time, "time", lambda: 2_000_000_000.0)
        iteration2_pre_state = {key: watcher.state.get(key) for key in (
            "postDiscussionEnvelopeRequired", "rolloverDue", "rolloverPending", "rolloverInProgress",
            "handoverRequested", "rolloverHandoverSendState", "rolloverMaintenanceState",
            "rolloverRecoveryState", "rolloverRecoveryRetryAfter", "rolloverTransactionId",
            "rolloverTransactionTaskId", "nextTaskId", "postDiscussionProtocolTaskId",
        )}
        iteration2_legacy_precedence = watcher_module._legacy_handover_must_precede_post_discussion_gate(watcher)
        iteration2_decision = watcher_module._evaluate_public_rollover_boundary(watcher, watcher.discussion_pause_active)
        run_result = watcher_module.run_next_prompt_ready_once(watcher, launch_executor, "endpoint", watcher.discussion_pause_active)
        assert service_trace[:2] == [(1, False, True, "RECONCILE_PENDING"), (2, True, False, None)], (run_result, iteration2_pre_state, iteration2_legacy_precedence, iteration2_decision)
        assert ordered_trace == [
            (1, "public_iteration_started"),
            (1, "legacy_handover_reemission_requested"),
            (1, "reemission_response_not_yet_visible"),
            (1, "rollover_service_false"),
            (2, "public_iteration_started"),
            (2, "legacy_handover_validated"),
            (2, "handover_persisted_before_fresh_creation"),
            (2, "fresh_architect_created"),
            (2, "fresh_bootstrap_sent"),
            (2, "fresh_ready"),
            (2, "authority_committed"),
            (2, "old_architect_closed"),
            (2, "rollover_service_true"),
        ]
    elif scenario == "invisible-history":
        assert legacy_handover not in [entry.get("text") for entry in old_bridge._assistant_entries()]
        assert service_trace == [(1, True, False, None)]
        assert len(old_bridge.recovery_requests) == 1
        artifacts = list((watcher.state_dir / "logs" / "diagnostic" / "legacy-handover-reemission").glob("*.json"))
        assert len(artifacts) == 1
        observed_artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
        assert observed_artifact["observedText"] == legacy_handover
        assert observed_artifact["textSha256"] == hashlib.sha256(legacy_handover.encode("utf-8")).hexdigest()
        assert observed_artifact["handoverResponseValid"] is True
        assert ordered_trace == [
            (1, "public_iteration_started"),
            (1, "legacy_handover_reemission_requested"),
            (1, "legacy_handover_reemitted"),
            (1, "legacy_handover_validated"),
            (1, "handover_persisted_before_fresh_creation"),
            (1, "fresh_architect_created"),
            (1, "fresh_bootstrap_sent"),
            (1, "fresh_ready"),
            (1, "authority_committed"),
            (1, "old_architect_closed"),
            (1, "rollover_service_true"),
        ]
    else:
        run_result = first_iteration_result
        assert service_trace == [(1, True, False, None)]
        assert ordered_trace == [
            (1, "public_iteration_started"),
            (1, "legacy_handover_validated"),
            (1, "handover_persisted_before_fresh_creation"),
            (1, "fresh_architect_created"),
            (1, "fresh_bootstrap_sent"),
            (1, "fresh_ready"),
            (1, "authority_committed"),
            (1, "old_architect_closed"),
            (1, "rollover_service_true"),
        ]

    assert events[:6] == [
        "legacy_handover_validated", "fresh_created", "fresh_bootstrap_sent",
        "fresh_ready_observed", "authority_committed", "old_architect_closed",
    ], (run_result, events, protocol_gate_targets, service_trace, {key: watcher.state.get(key) for key in ("rolloverDue", "rolloverPending", "rolloverInProgress", "handoverRequested", "rolloverMaintenanceState", "rolloverRecoveryState", "rolloverRecoveryAttemptCount", "rolloverRecoveryRetryAfter", "humanRequiredReason", "rolloverHandoverRecoveryDisposition", "rolloverHandoverRecoveryReason", "rolloverRecoveryTerminalReason", "rolloverAutomaticRecoveryEpochCount", "rolloverDeferredForTaskId", "postDiscussionEnvelopeRepairAttempted", "postDiscussionProtocolFailure")}, public_decision)
    assert watcher.state["architectConversationId"] == "NEW-ARCHITECT"
    assert watcher.state["rolloverDue"] is False
    assert watcher.state["rolloverPending"] is False
    assert watcher.state["rolloverInProgress"] is False
    assert watcher.state["handoverRequested"] is False
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert watcher.state["postDiscussionProtocolRolloverCommittedTransactionId"] == tx
    assert watcher.state["postDiscussionEnvelopeRepairAttempted"] is True
    assert watcher.state["postDiscussionEnvelopeRepairAwaiting"] is True
    assert len(old_bridge.sent) == 1  # bootstrap only; no resend to old Architect
    assert "Your previous completed response" not in old_bridge.sent[0]
    assert len(fresh_protocol_bridge.sent) == 1
    assert fresh_protocol_bridge.sent[0].startswith("Your previous completed response")
    assert launches == []
    assert events.count("legacy_handover_validated") == 1
    assert events.count("fresh_created") == 1
    assert events.count("fresh_bootstrap_sent") == 1
    assert events.count("fresh_ready_observed") == 1
    assert events.count("authority_committed") == 1
    assert events.count("old_architect_closed") == 1
    assert protocol_gate_targets == ["NEW-ARCHITECT"]

    # The fresh Architect wraps exactly the immutable staged prompt; no content
    # is regenerated or altered by protocol recovery.
    repaired = envelope("000103", prompt=prompt_text)
    fresh_protocol_bridge.entries.append(_discussion_bridge_response(repaired, "repaired-envelope"))
    assert watcher_module.service_post_discussion_protocol_gate_once(
        watcher, "endpoint", watcher.discussion_pause_active,
    ) == "EXECUTE"
    assert watcher.state["postDiscussionEnvelopeRequired"] is False
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    assert prompt.read_bytes() == prompt_bytes_before
    assert hashlib.sha256(prompt.read_bytes()).hexdigest() == prompt_hash_before
    events.append("exact_envelope_accepted")

    process = watcher_module.dispatch_next_prompt_once(
        watcher, launch_executor, "endpoint", watcher.discussion_pause_active,
    )
    assert process is not None
    assert launches == ["000103"]
    assert events[-2:] == ["exact_envelope_accepted", "executor_launch"]
    assert watcher.state["executorSessionId"] == session_id
    assert watcher.state["executorSessionMode"] == "PERSISTENT"
    assert old_page.closed is True
    assert prompt.read_bytes() == prompt_bytes_before


def test_persisted_legacy_handover_survives_restart_without_old_browser_history(tmp_path, monkeypatch):
    watcher, prompt, prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    transaction_id = watcher_module.LEGACY_COMPAT_TRANSACTION_ID
    handover = (
        "Authoritative AFFOTECH state for already-staged task 000103.\n"
        f"Rollover transaction ID: {transaction_id}\nARCHITECT_HANDOVER_READY"
    )
    watcher.state.update({
        "architectConversationId": "OLD-ARCHITECT",
        "executorSessionId": watcher_module.AFFOTECH_EXECUTOR_SESSION_ID,
        "executorSessionMode": "PERSISTENT",
        "discussionPauseActive": True,
        "rolloverInProgress": True,
        "rolloverMaintenanceState": "RECONCILE_PENDING",
        "rolloverRecoveryState": "RECOVERING",
        "rolloverHandoverRecoveryDisposition": "RETRYABLE",
        "rolloverRecoveryRetryAfter": 1.0,
        "rolloverAutomaticRecoveryNextEligibleAt": 1.0,
        "postDiscussionEnvelopeRequired": True,
        "postDiscussionProtocolTaskId": "000103",
        "postDiscussionProtocolTransactionId": transaction_id,
    })
    watcher.save()
    watcher.request_discussion_resume()
    staged_before = prompt.read_bytes()
    digest = hashlib.sha256(handover.encode("utf-8")).hexdigest()

    # Model the crash boundary: exact validated response bytes are durably
    # written, then the process stops before any fresh page is created.
    assert watcher.session_rollover.persist_validated_handover(handover)
    durable_state = json.loads(watcher.state_path.read_text(encoding="utf-8"))
    assert durable_state["pending_handover"] == handover
    assert durable_state["rolloverHandoverResponseIdentity"] == digest
    assert "rolloverFreshPageCreated" not in durable_state

    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.state["pending_handover"] == handover
    assert restarted.session_rollover.persisted_validated_handover() == handover
    events = []

    class Page:
        def __init__(self, conversation_id, assistant_text=""):
            self.url = f"https://chatgpt.com/c/{conversation_id}"
            self.assistant_text = assistant_text
            self.closed = False
            self.context = None

        def evaluate(self, script):
            if "stop-button" in script:
                return False
            if 'data-message-author-role="assistant"' in script:
                return [_discussion_bridge_response(self.assistant_text, "ready")] if self.assistant_text else []
            return []

        def close(self):
            self.closed = True
            events.append("old_closed")

    old_page = Page("OLD-ARCHITECT")
    fresh_page = Page("NEW-ARCHITECT", "ARCHITECT_SESSION_READY")
    old_page.context = type("Context", (), {"pages": [old_page]})()
    fresh_page.context = type("Context", (), {"pages": [fresh_page]})()

    class OldBridge:
        page = old_page

        def _assistant_entries(self):
            raise AssertionError("restart recovery must not require old browser history")

        def open_fresh_with_handover(self, exact_handover):
            assert exact_handover == handover
            assert restarted.state["pending_handover"] == handover
            events.append("fresh_created_bootstrap_sent")
            return fresh_page

        def close(self):
            pass

    class FreshProtocolBridge(_PostDiscussionBridge):
        def __init__(self):
            super().__init__([_discussion_bridge_response("ARCHITECT_SESSION_READY", "ready")])

    old_bridge = OldBridge()
    fresh_protocol_bridge = FreshProtocolBridge()
    monkeypatch.setattr(
        watcher_module.ArchitectPlaywright,
        "attach",
        staticmethod(lambda _endpoint, conversation: old_bridge if conversation == "OLD-ARCHITECT" else fresh_protocol_bridge),
    )
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _watcher, _bridge, requested: requested)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher_module.time, "time", lambda: 2_000_000_000.0)

    launches = []
    restart_decision = watcher_module._evaluate_public_rollover_boundary(restarted, restarted.discussion_pause_active)
    process = watcher_module.run_next_prompt_ready_once(
        restarted,
        lambda _body, _path: (launches.append("000103") or type("Process", (), {"pid": 91003})()),
        "endpoint",
        restarted.discussion_pause_active,
    )
    assert process is None and restarted.state["architectConversationId"] == "NEW-ARCHITECT", (
        restart_decision,
        {key: restarted.state.get(key) for key in (
            "postDiscussionEnvelopeRequired", "postDiscussionProtocolRolloverCommittedTransactionId",
            "rolloverDue", "rolloverPending", "rolloverInProgress", "handoverRequested",
            "rolloverRecoveryState", "rolloverMaintenanceState", "architectConversationId",
        )},
    )
    assert restarted.state["architectConversationId"] == "NEW-ARCHITECT"
    assert restarted.state["rolloverDue"] is False
    assert events == ["fresh_created_bootstrap_sent", "old_closed"]
    assert restarted.state["postDiscussionEnvelopeRequired"] is True
    repaired = envelope("000103", prompt=prompt_text)
    fresh_protocol_bridge.entries.append(_discussion_bridge_response(repaired, "repair"))
    assert watcher_module.service_post_discussion_protocol_gate_once(
        restarted, "endpoint", restarted.discussion_pause_active,
    ) == "EXECUTE"
    assert len(fresh_protocol_bridge.sent) == 1
    assert watcher_module.dispatch_next_prompt_once(
        restarted,
        lambda _body, _path: (launches.append("000103") or type("Process", (), {"pid": 91003})()),
        "endpoint",
        restarted.discussion_pause_active,
    ) is not None
    assert launches == ["000103"]
    assert prompt.read_bytes() == staged_before
    assert hashlib.sha256(prompt.read_bytes()).digest() == hashlib.sha256(staged_before).digest()
    assert restarted.state["executorSessionId"] == watcher_module.AFFOTECH_EXECUTOR_SESSION_ID


def test_f9_pause_acknowledgement_is_durable_and_only_f10_resumes(tmp_path, monkeypatch, capsys):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = watcher.prompts_dir / "000301.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("bounded synthetic prompt\n", encoding="utf-8")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000300", "lastCompletedTaskId": "000300",
        "nextTaskId": "000301", "nextPromptPath": str(prompt), "rolloverDue": True,
        "rolloverPending": True, "rolloverInProgress": False, "discussionPauseActive": False,
    })
    watcher.save()
    marker_writes = []
    original_marker_write = watcher._write_discussion_pause_marker

    def observe_marker(active):
        marker_writes.append(active)
        return original_marker_write(active)

    monkeypatch.setattr(watcher, "_write_discussion_pause_marker", observe_marker)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    service_calls, launches = [], []
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_a, **_kw: service_calls.append(1))

    watcher.request_discussion_pause()
    first_ack = capsys.readouterr().out
    assert "ORCHESTRATOR PAUSED BY HUMAN" in first_ack
    assert watcher.discussion_pause_active() is True
    assert watcher.state["discussionPauseActive"] is True
    assert json.loads(watcher.state_path.read_text(encoding="utf-8"))["discussionPauseActive"] is True

    watcher_module.run_next_prompt_ready_once(
        watcher, lambda *_args: launches.append(1), "unused", watcher.discussion_pause_active,
    )
    assert service_calls == []
    assert launches == []
    assert watcher.state["rolloverDue"] is True

    watcher.request_discussion_pause()
    repeated_ack = capsys.readouterr().out
    assert "ORCHESTRATOR ALREADY PAUSED" in repeated_ack
    assert marker_writes == [True]

    watcher.request_discussion_resume()
    resume_ack = capsys.readouterr().out
    assert "ORCHESTRATOR RESUMED BY HUMAN" in resume_ack
    assert watcher.discussion_pause_active() is False
    assert watcher.state["discussionPauseActive"] is False
    assert json.loads(watcher.state_path.read_text(encoding="utf-8"))["discussionPauseActive"] is False

    watcher.request_discussion_resume()
    repeated_resume_ack = capsys.readouterr().out
    assert "ORCHESTRATOR ALREADY RESUMED" in repeated_resume_ack
    assert marker_writes == [True, False]
    assert service_calls == [] and launches == []

    source = Path(watcher_module.__file__).read_text(encoding="utf-8")
    restore_start = source.index("def _restore_machine_protocol_after_discussion")
    pause_start = source.index("def request_discussion_pause", restore_start)
    restore_source = source[restore_start:pause_start]
    assert restore_source.count("_write_discussion_pause_marker(False)") == 2
    assert source.count("_write_discussion_pause_marker(False)") == 2


def test_public_protocol_gate_waits_while_architect_generates(tmp_path, monkeypatch):
    watcher, _prompt, _prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    watcher.state.update({"rolloverTransactionId": "canonical-generating-transaction", "rolloverHandoverProtocolVersion": 1})
    watcher.save()
    bridge = _PostDiscussionBridge([_discussion_bridge_response("old", "old")])
    watcher.state["architectDiscussionBaseline"] = bridge.assistant_baseline()
    watcher.save()
    watcher.request_discussion_resume()
    bridge.generating = True
    rollover_calls = []
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: rollover_calls.append(1) or False)
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: (_ for _ in ()).throw(AssertionError("must not launch")), "endpoint", lambda: False) is None
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert bridge.sent == []
    assert rollover_calls == []


def test_public_protocol_gate_fails_closed_on_staged_prompt_mismatch(tmp_path, monkeypatch):
    watcher, _prompt, _prompt_text = _staged_prompt_missing_envelope_fixture(tmp_path)
    bridge = _PostDiscussionBridge([_discussion_bridge_response("old", "old")])
    watcher.state["architectDiscussionBaseline"] = bridge.assistant_baseline()
    watcher.save()
    watcher.request_discussion_resume()
    watcher.state["nextPromptPath"] = str(tmp_path / "wrong-prompt.txt")
    watcher.save()
    rollover_calls = []
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher_module, "service_deferred_rollover_once", lambda *_args, **_kwargs: rollover_calls.append(1) or False)
    assert watcher_module.run_next_prompt_ready_once(watcher, lambda *_args: (_ for _ in ()).throw(AssertionError("must not launch")), "endpoint", lambda: False) is None
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["postDiscussionProtocolFailure"] == "ARCHITECT_STAGED_PROMPT_EVIDENCE_INVALID"
    assert bridge.sent == []
    assert rollover_calls == []


def test_architect_running_main_path_prioritizes_post_discussion_protocol_over_handover():
    source = inspect.getsource(watcher_module.main)
    protocol_branch = source.index('if watcher.state.get("postDiscussionEnvelopeRequired"):')
    handover_branch = source.index('if watcher.state.get("handoverRequested") and watcher.state.get("rolloverInProgress")')
    assert protocol_branch < handover_branch


def test_post_discussion_repair_failure_is_durable_and_prevents_second_repair(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-discuss", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 1, "text_hash": "before"}})
    watcher.save()
    bridge = _PostDiscussionBridge([_discussion_bridge_response("discussion", "old")])
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    missing = "A completed response without the required machine envelope."
    bridge.entries.append(_discussion_bridge_response(missing, "decision"))
    assert watcher.reconcile_post_discussion_response(bridge, missing) == "REPAIR_REQUESTED"
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    bridge.entries.append(_discussion_bridge_response("still conversational", "repair"))
    assert restarted.reconcile_post_discussion_response(bridge, "still conversational") == "FAILED"
    assert len(bridge.sent) == 1
    assert restarted.state["state"] == "HUMAN_REQUIRED"
    assert restarted.state["humanRequiredReason"] == "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED"


def test_remote_resume_restores_protocol_epoch_and_f9_f10_cycles_reset_repair_for_new_epoch(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "task-cycle", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 0, "text_hash": "before"}})
    watcher._write_discussion_pause_marker(True)
    watcher.request_remote_discussion_resume()
    assert watcher.state["postDiscussionResumeEpoch"] == 1
    watcher.state["postDiscussionEnvelopeRepairAttempted"] = True
    watcher._write_discussion_pause_marker(True)
    watcher.request_discussion_resume()
    assert watcher.state["postDiscussionResumeEpoch"] == 2
    assert watcher.state["postDiscussionEnvelopeRepairAttempted"] is False


def test_post_discussion_wrong_task_and_strict_envelope_guards(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    wrong = envelope("other-task")
    with pytest.raises(ValueError, match="ARCHITECT_ENVELOPE_INVALID"):
        parse_orchestrator_result(wrong, "current-task")
    missing_documentation = wrong.replace("documentation=NOT_REQUIRED\n", "")
    with pytest.raises(ValueError, match="ARCHITECT_ENVELOPE_INVALID"):
        parse_orchestrator_result(missing_documentation.replace("taskId=other-task", "taskId=current-task"), "current-task")
    multiple = wrong + "\n" + wrong
    with pytest.raises(ValueError, match="ARCHITECT_ENVELOPE_INVALID"):
        parse_orchestrator_result(multiple, "other-task")
    incomplete = envelope("current-task", prompt="")
    with pytest.raises(ValueError, match="ARCHITECT_ENVELOPE_PROMPT_REQUIRED"):
        parse_orchestrator_result(incomplete, "current-task")


def test_architect_bootstrap_carries_permanent_post_discussion_contract():
    bootstrap = watcher_module.fresh_architect_bootstrap_payload("handover")
    assert "While human discussion is active" in bootstrap
    assert "F10 / ORCH:RESUME" in bootstrap


def test_concurrent_save_calls_use_serialized_unique_atomic_writes(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    barrier = threading.Barrier(3)
    errors = []

    def save_worker(index):
        try:
            watcher.state[f"concurrent-{index}"] = index
            barrier.wait()
            watcher.save()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=save_worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert errors == []
    persisted = json.loads(watcher.state_path.read_text(encoding="utf-8"))
    assert persisted["concurrent-0"] == 0
    assert persisted["concurrent-1"] == 1


def test_f10_after_next_prompt_ready_only_clears_pause_and_dispatches_once(tmp_path):
    watcher, base, _ = recovery_fixture(tmp_path)
    head = subprocess.check_output(["git", "-C", str(base), "rev-parse", "refs/remotes/origin/hybrid-v2"], text=True).strip()
    prompt = configured_project_prompt(base, head, "next-000100")
    watcher.state.update({"state": "ARCHITECT_RUNNING", "taskId": "000099", "lastCompletedTaskId": "000099", "nextTaskId": "000100", "architectConversationId": "ARCH", "rolloverDue": False, "rolloverPending": False})
    watcher._write_discussion_pause_marker(True)
    response = envelope("000099", prompt=prompt)
    watcher.accept_architect_response(response)
    assert watcher.state["state"] == "NEXT_PROMPT_READY"
    controller = DiscussionHotkeyController(watcher, emit=lambda _message: None)
    assert controller.dispatch("F10") is True
    assert not watcher.discussion_pause_active()
    assert watcher.state.get("postDiscussionResumeEpoch") is None
    launches = []
    process = type("Process", (), {"pid": 12345})()
    assert dispatch_next_prompt_once(watcher, lambda *_args: launches.append(1) or process, "endpoint", lambda: False) is process
    assert len(launches) == 1


def test_resume_for_unresolved_human_discussion_arms_once_and_repeated_f10_is_idempotent(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "000099", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 1, "text_hash": "before"}})
    watcher._write_discussion_pause_marker(True)
    controller = DiscussionHotkeyController(watcher, emit=lambda _message: None)
    assert controller.dispatch("F10") is True
    assert watcher.state["postDiscussionResumeEpoch"] == 1
    assert watcher.state["postDiscussionEnvelopeRequired"] is True
    assert controller.dispatch("F10") is True
    assert watcher.state["postDiscussionResumeEpoch"] == 1
    assert controller.dispatch("F9") is True
    assert controller.dispatch("F10") is True
    assert watcher.state["postDiscussionResumeEpoch"] == 2


@pytest.mark.parametrize("workflow_state", ["EXECUTOR_RUNNING", "RESULT_READY", "IDLE"])
def test_f10_does_not_arm_unrelated_machine_protocol_epoch(tmp_path, workflow_state):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": workflow_state, "taskId": "000099", "humanRequiredReason": None})
    watcher._write_discussion_pause_marker(True)
    assert DiscussionHotkeyController(watcher, emit=lambda _message: None).dispatch("F10") is True
    assert not watcher.discussion_pause_active()
    assert watcher.state.get("postDiscussionResumeEpoch") is None
    assert not watcher.state.get("postDiscussionEnvelopeRequired")


def test_resume_persistence_failure_leaves_discussion_paused_and_hotkey_alive(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "000099", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED"})
    watcher._write_discussion_pause_marker(True)
    original_save = watcher.save
    calls = []

    def failing_save():
        calls.append(1)
        raise PermissionError("simulated state persistence failure")

    monkeypatch.setattr(watcher, "save", failing_save)
    controller = DiscussionHotkeyController(watcher, emit=lambda message: calls.append(message))
    assert controller.dispatch("F10") is False
    assert watcher.discussion_pause_active() is True
    assert any(isinstance(item, str) and "DISCUSSION_CONTROL_DISPATCH_FAILED" in item for item in calls)
    monkeypatch.setattr(watcher, "save", original_save)
    assert controller.dispatch("F10") is True
    assert not watcher.discussion_pause_active()


@pytest.mark.parametrize("resume_method", ["request_discussion_resume", "request_remote_discussion_resume"])
def test_concurrent_save_and_resume_are_serialized_and_keep_state_valid(tmp_path, resume_method):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "HUMAN_REQUIRED", "taskId": "000099", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED", "architectDiscussionBaseline": {"count": 1, "text_hash": "before"}})
    watcher._write_discussion_pause_marker(True)
    barrier = threading.Barrier(3)
    errors = []

    def competing_save():
        try:
            barrier.wait()
            watcher.state["competingSave"] = True
            watcher.save()
        except Exception as error:
            errors.append(error)

    def resume():
        try:
            barrier.wait()
            getattr(watcher, resume_method)()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=competing_save), threading.Thread(target=resume)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert errors == []
    persisted = json.loads(watcher.state_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "HUMAN_REQUIRED"
    assert persisted["taskId"] == "000099"
    assert persisted.get("postDiscussionResumeEpoch") == 1
    assert watcher.discussion_pause_active() is False


def test_discussion_hotkey_unknown_key_is_ignored(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    controller = DiscussionHotkeyController(watcher, emit=lambda _message: None)
    assert controller.dispatch("F8") is False
    assert not watcher.state.get("discussionPauseActive")


class _RemoteControlBridge:
    def __init__(self, messages):
        self.messages = messages
        self.closed = False

    def control_user_messages(self):
        return list(self.messages)

    def close(self):
        self.closed = True


def test_remote_monitor_incremental_reads_skip_unchanged_history(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")

    class Bridge:
        def __init__(self):
            self.messages = [{"id": f"history-{index}", "text": "ordinary"} for index in range(1000)]
            self.reads = []

        def control_user_message_count(self):
            return len(self.messages)

        def control_user_messages(self, start=0):
            self.reads.append(start)
            return list(self.messages[start:])

        def close(self):
            pass

    bridge = Bridge()
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: bridge, emit=lambda _message: None)
    monitor.establish_startup_baseline(bridge)
    assert bridge.reads == [0]
    assert monitor.poll_once() == 0
    assert bridge.reads == [0]
    bridge.messages.append({"id": "pause-new", "text": "ORCH:PAUSE"})
    assert monitor.poll_once() == 1
    assert bridge.reads == [0, 1000]
    assert monitor.poll_once() == 0
    assert bridge.reads == [0, 1000]


def test_remote_exact_user_commands_share_durable_pause_authority(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    messages = [{"id": "old", "text": "ORCH:PAUSE"}]
    bridge = _RemoteControlBridge(messages)
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: bridge, emit=lambda _message: None)
    monitor.establish_startup_baseline(bridge)
    messages.append({"id": "pause-1", "text": "ORCH:PAUSE"})
    assert monitor.poll_once() == 1
    assert watcher.discussion_pause_active() is True
    messages.append({"id": "pause-1", "text": "ORCH:PAUSE"})
    assert monitor.poll_once() == 0
    messages.append({"id": "resume-1", "text": "ORCH:RESUME"})
    assert monitor.poll_once() == 1
    assert watcher.discussion_pause_active() is False
    assert watcher.state.get("taskId") is None


def test_remote_commands_require_exact_user_message_and_ignore_assistant(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    messages = [{"id": "base", "text": "ordinary discussion"}]
    bridge = _RemoteControlBridge(messages)
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: bridge, emit=lambda _message: None)
    monitor.establish_startup_baseline(bridge)
    messages.extend([
        {"id": "fuzzy", "text": "please pause"},
        {"id": "space", "text": "ORCH: PAUSE"},
        {"id": "assistant", "author": "assistant", "text": "ORCH:PAUSE"},
        {"id": "unknown", "text": "ORCH:STOP"},
    ])
    assert monitor.poll_once() == 0
    assert watcher.discussion_pause_active() is False


def test_remote_monitor_diagnostic_trace_wraps_existing_user_read(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DIAGNOSTIC_TRACE", "1")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    tracer = watcher_module.DiagnosticTracer(tmp_path, "remote-trace")
    watcher.diagnostic_trace = tracer
    bridge = _RemoteControlBridge([{"id": "base", "text": "ordinary discussion"}])
    bridge._diagnostic_connection_id = "PW-CONN-REMOTE"
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: bridge, emit=lambda _message: None)
    monitor.establish_startup_baseline(bridge)
    assert monitor.poll_once() == 0
    tracer.shutdown(watcher.state)
    records = [json.loads(line) for line in (tmp_path / "logs" / "diagnostic" / "remote-trace" / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    operations = [record["operation"] for record in records]
    assert "REMOTE_CONTROL_POLL_BEGIN" in operations
    assert "REMOTE_CONTROL_USER_READ_BEGIN" in operations
    assert "REMOTE_CONTROL_USER_READ_END" in operations
    assert "REMOTE_CONTROL_POLL_END" in operations
    read_end = next(record for record in records if record["operation"] == "REMOTE_CONTROL_USER_READ_END")
    assert read_end["connectionId"] == "PW-CONN-REMOTE"
    assert read_end["userMessageCount"] == 1


def test_remote_command_identity_is_consumed_once_and_restart_baseline_ignores_old_command(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    messages = [{"id": "base", "text": "hello"}]
    bridge = _RemoteControlBridge(messages)
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: bridge, emit=lambda _message: None)
    monitor.establish_startup_baseline(bridge)
    messages.append({"id": "pause-1", "text": "ORCH:PAUSE"})
    assert monitor.poll_once() == 1
    assert monitor.poll_once() == 0
    restarted = RemoteDiscussionControlMonitor(watcher, lambda: bridge, emit=lambda _message: None)
    restarted.establish_startup_baseline(bridge)
    assert restarted.poll_once() == 0
    assert watcher.discussion_pause_active() is True


def test_remote_monitor_failure_isolated_and_rollover_target_follows_state(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state["architectConversationId"] = "OLD"
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: (_ for _ in ()).throw(RuntimeError("attach failed")), emit=lambda _message: None)
    assert monitor.start() is False
    assert watcher.state.get("taskId") is None
    bridge = _RemoteControlBridge([])
    monitor.establish_startup_baseline(bridge)
    watcher.state["architectConversationId"] = "NEW"
    assert monitor._conversation_id == "OLD"
    monitor.stop()


def test_remote_monitor_playwright_bridge_is_created_used_and_closed_on_worker_thread(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    caller = threading.get_ident()
    factory_threads, use_threads, close_threads = [], [], []

    class Bridge:
        def control_user_messages(self):
            use_threads.append(threading.get_ident())
            return []
        def close(self):
            close_threads.append(threading.get_ident())

    def factory():
        factory_threads.append(threading.get_ident())
        return Bridge()

    monitor = RemoteDiscussionControlMonitor(watcher, factory, emit=lambda _message: None)
    assert monitor.start() is True
    monitor.stop()
    assert factory_threads and factory_threads[0] != caller
    assert set(factory_threads + use_threads + close_threads) == {factory_threads[0]}


def test_remote_monitor_delayed_startup_uses_attachment_budget(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    caller = threading.get_ident()
    factory_threads = []

    class Bridge:
        def control_user_messages(self): return []
        def close(self): pass

    def factory():
        factory_threads.append(threading.get_ident())
        time.sleep(2.1)
        return Bridge()

    monitor = RemoteDiscussionControlMonitor(watcher, factory, emit=lambda _message: None, startup_timeout=3.0)
    assert monitor.start() is True
    monitor.stop()
    assert factory_threads and factory_threads[0] != caller


def test_remote_monitor_startup_timeout_terminates_worker(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")

    class Bridge:
        def control_user_messages(self): return []
        def close(self): pass

    def factory():
        time.sleep(0.2)
        return Bridge()

    monitor = RemoteDiscussionControlMonitor(watcher, factory, emit=lambda _message: None, startup_timeout=0.05, shutdown_timeout=1.0)
    assert monitor.start() is False
    assert monitor.active is False
    assert monitor._thread is not None and not monitor._thread.is_alive()


def test_remote_monitor_startup_failure_isolated_from_workflow_state(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "000025", "codexPid": 4321})
    before = dict(watcher.state)
    monitor = RemoteDiscussionControlMonitor(watcher, lambda: (_ for _ in ()).throw(RuntimeError("no bridge")), emit=lambda _message: None)
    assert monitor.start() is False
    assert watcher.state == before


def test_remote_commands_persist_canonical_pause_state_from_worker_path(tmp_path, monkeypatch):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    original_save = watcher.save
    saves = []

    def observe_save():
        saves.append(dict(watcher.state))
        return original_save()

    monkeypatch.setattr(watcher, "save", observe_save)
    watcher.request_remote_discussion_pause()
    assert watcher.discussion_pause_active() is True
    assert watcher.state["discussionPauseActive"] is True
    watcher.request_remote_discussion_resume()
    assert watcher.discussion_pause_active() is False
    assert watcher.state["discussionPauseActive"] is False
    assert json.loads(watcher.state_path.read_text(encoding="utf-8"))["discussionPauseActive"] is False
    assert len(saves) == 2


def test_explicit_pause_marker_overrides_legacy_state_flag(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state["discussionPauseActive"] = False
    watcher._write_discussion_pause_marker(True)
    assert watcher.discussion_pause_active() is True
    watcher.state["discussionPauseActive"] = True
    watcher._write_discussion_pause_marker(False)
    assert watcher.discussion_pause_active() is False


def test_remote_monitor_rollover_reattach_stays_on_worker_thread(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state["architectConversationId"] = "OLD"
    caller = threading.get_ident()
    factory_threads, close_threads = [], []
    bridges = []

    class Bridge:
        def control_user_messages(self): return []
        def close(self): close_threads.append(threading.get_ident())

    def factory():
        factory_threads.append(threading.get_ident())
        bridge = Bridge()
        bridges.append(bridge)
        return bridge

    monitor = RemoteDiscussionControlMonitor(watcher, factory, emit=lambda _message: None)
    assert monitor.start() is True
    watcher.state["architectConversationId"] = "NEW"
    deadline = time.monotonic() + 3.0
    while len(factory_threads) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    monitor.stop()
    assert len(factory_threads) == 2
    assert factory_threads[0] != caller
    assert set(factory_threads + close_threads) == {factory_threads[0]}


def test_result_review_instruction_advertises_documentation_disposition(tmp_path):
    watcher = ready(tmp_path)
    messages = []
    class Bridge:
        def assistant_baseline(self): return {"count": 0, "text_hash": "a" * 64}
        def user_baseline(self): return {"count": 0, "text_hash": "b" * 64}
        def submit_result_bounded(self, message): messages.append(message)
        def latest_user_message(self): return messages[-1] if messages else None
    watcher.deliver_result(Bridge())
    message = messages[0]
    assert "documentation=NOT_REQUIRED|REQUIRED|COMPLETE" in message
    assert "REQUIRED" in message and "COMPLETE" in message
    assert "Do not use NOT_REQUIRED merely" in message


def test_idle_bootstrap_instruction_advertises_documentation_disposition(tmp_path):
    watcher = ready(tmp_path)
    messages = []
    class Bridge:
        def submit_result_bounded(self, message): messages.append(message)
    watcher.state.update({"continuationSourceFingerprint": "source-hash"})
    assert watcher.request_architect_bootstrap(Bridge()) is True
    message = messages[0]
    assert "documentation=NOT_REQUIRED|REQUIRED|COMPLETE" in message
    assert "milestone/release documentation closure" in message
    assert "Do not choose NOT_REQUIRED merely" in message
    envelope_start = message.index("<ORCHESTRATOR_RESULT>")
    template = message[envelope_start:]
    assert template.index("taskId=<task id>") < template.index("documentation=NOT_REQUIRED|REQUIRED|COMPLETE") < template.index("promptBegin <complete Executor prompt only when action=EXECUTE>")


def test_format_recovery_instruction_preserves_documentation_disposition(tmp_path):
    watcher = ready(tmp_path)
    messages = []
    class Bridge:
        def submit_result_bounded(self, message): messages.append(message)
        def assistant_baseline(self): return {"count": 1, "text_hash": "a" * 64}
    watcher.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED"})
    watcher.state.update({"taskId": "task-1", "documentationClosurePending": True})
    watcher.request_format_recovery(Bridge())
    message = messages[0]
    assert "documentation=NOT_REQUIRED|REQUIRED|COMPLETE" in message
    assert "Preserve the documentation disposition" in message
    assert "do not downgrade a pending documentation closure to NOT_REQUIRED" in message


def test_all_production_documentation_schema_prompts_advertise_all_values(tmp_path):
    watcher = ready(tmp_path)
    captured = []
    class Bridge:
        def assistant_baseline(self): return {"count": 0, "text_hash": "a" * 64}
        def user_baseline(self): return {"count": 0, "text_hash": "b" * 64}
        def submit_result_bounded(self, message): captured.append(message)
        def latest_user_message(self): return captured[-1] if captured else None
        def user_message_texts(self): return list(captured)
    watcher.deliver_result(Bridge())
    watcher.state.update({"state": "IDLE", "continuationSourceFingerprint": "next-source"})
    watcher.request_architect_bootstrap(Bridge())
    watcher.state.update({"taskId": "task-1", "formatRecoveryCount": 0})
    watcher.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED"})
    watcher.request_format_recovery(Bridge())
    assert len(captured) == 3
    for message in captured:
        assert "documentation=NOT_REQUIRED|REQUIRED|COMPLETE" in message
