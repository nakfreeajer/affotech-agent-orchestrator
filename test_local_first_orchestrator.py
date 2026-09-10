import json
import subprocess
import time
from pathlib import Path

import pytest

from local_orchestrator_watcher import LocalFirstOrchestrator, atomic_write, parse_orchestrator_result


def envelope(task, action="EXECUTE", prompt="next task"):
    body = prompt if action == "EXECUTE" else ""
    return (f"explanation\n<ORCHESTRATOR_RESULT>\nclassification=ACCEPTED\n"
            f"action={action}\ntaskId={task}\npromptBegin\n{body}\n"
            "promptEnd\n</ORCHESTRATOR_RESULT>")


def ready(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / ".agent-work" / "orchestrator")
    result = tmp_path / "report.txt"
    result.write_text("executor report", encoding="utf-8")
    watcher.state.update({"state": "RESULT_READY", "taskId": "task-1", "executorResultPath": str(result)})
    watcher.save()
    return watcher


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
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    response = envelope("task-1", prompt="line 1\r\nline 2")
    decision = watcher.accept_architect_response(response)
    assert decision["action"] == "EXECUTE"
    assert Path(watcher.state["nextPromptPath"]).read_text(encoding="utf-8") == "line 1\nline 2"
    assert watcher.accept_architect_response(response)["action"] == "DUPLICATE"


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


def test_atomic_write_and_github_free_evidence(tmp_path):
    path = tmp_path / "work" / "state.json"
    atomic_write(path, b"{}")
    assert json.loads(path.read_text()) == {}
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", action="STOP"))
    assert "relay" not in json.dumps(watcher.state).lower()

