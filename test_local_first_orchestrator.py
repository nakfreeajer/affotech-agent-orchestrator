import json
import inspect
import subprocess
import time
from pathlib import Path

import pytest

from local_orchestrator_watcher import (ArchitectPlaywright, LocalFirstOrchestrator,
                                        LocalWatcher,
                                        ResultSubmissionError, atomic_write,
                                        parse_orchestrator_result, resolve_executor_worktree)
import local_orchestrator_watcher as watcher_module


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
    watcher.state.update({"state": "ARCHITECT_RUNNING", "formatRecoveryCount": 1})
    bridge = RecoveryBridge()
    watcher.request_format_recovery(bridge)
    assert bridge.messages == []
    assert watcher.state["state"] == "HUMAN_REQUIRED"


def test_format_recovery_response_uses_strict_same_task_parser():
    parsed = parse_orchestrator_result(envelope("task-1", prompt="next"), "task-1")
    assert parsed == {"classification": "ACCEPTED", "action": "EXECUTE", "taskId": "task-1", "prompt": "next"}


def test_atomic_write_and_github_free_evidence(tmp_path):
    path = tmp_path / "work" / "state.json"
    atomic_write(path, b"{}")
    assert json.loads(path.read_text()) == {}
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", action="STOP"))
    assert "relay" not in json.dumps(watcher.state).lower()


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


def test_current_composer_receives_exact_text_and_is_confirmed():
    page = FakeComposerPage()
    result = "line 1\nline 2\n<ORCHESTRATOR_RESULT>"
    ArchitectPlaywright(page).submit_result_bounded(result, timeout=1)
    assert page.sent == [result]


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
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", prompt="next"))
    launches = []

    class Process:
        pid = 4321

    watcher.launch_next(lambda prompt, path: (launches.append(prompt) or Process()))
    assert launches == ["next"]
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
        [[{"id": "a", "text": "malformed final"}]],
        [False],
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


def test_architect_executor_prompt_resolves_explicit_worktree(tmp_path):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    prompt = f"ROLE\nExecutor\nWORKTREE\n{worktree}\nGOAL\nPreserve"
    assert resolve_executor_worktree(prompt, tmp_path) == str(worktree)
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", prompt=prompt))
    assert watcher.state["targetProject"] == str(worktree)
    assert watcher.state["targetRepo"] == str(worktree)
    assert watcher.state["targetWorktree"] == str(worktree)


def test_missing_or_invalid_executor_worktree_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="EXECUTOR_WORKTREE_MISSING"):
        resolve_executor_worktree("ROLE\nExecutor")
    with pytest.raises(RuntimeError, match="EXECUTOR_WORKTREE_INVALID"):
        resolve_executor_worktree("WORKTREE\nC:\\does-not-exist", tmp_path)


def test_launch_next_uses_resolved_worktree_and_records_owned_pid(tmp_path):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    watcher = ready(tmp_path)
    watcher.state["state"] = "ARCHITECT_RUNNING"
    watcher.accept_architect_response(envelope("task-1", prompt=f"WORKTREE\n{worktree}\nnext"))

    class Process:
        pid = 9876

    observed = []
    watcher.launch_next(lambda prompt, path: (observed.append((prompt, path, watcher.state["targetWorktree"])) or Process()))
    assert observed[0][2] == str(worktree)
    assert watcher.state["codexPid"] == 9876
    assert watcher.state["targetWorktree"] == str(worktree)


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
