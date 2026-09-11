import json
import hashlib
import inspect
import subprocess
import time
from pathlib import Path

import pytest

from local_orchestrator_watcher import (ArchitectPlaywright, LocalFirstOrchestrator,
                                        LocalWatcher,
                                        ResultSubmissionError, atomic_write,
                                        parse_orchestrator_result, resolve_executor_worktree,
                                        run_executor_state_once, visible_executor_launcher,
                                        WatcherInstanceLock)
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


def test_ambiguous_result_delivery_retries_once_when_exact_payload_absent(tmp_path):
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
    restarted.deliver_result(AbsentBridge())
    assert len(sends) == 1


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


def test_resident_recovery_retries_ambiguous_delivery_only_when_absent(tmp_path):
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
    assert watcher.state["state"] == "ARCHITECT_RUNNING"
    assert len(sends) == 2


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

    monkeypatch.setattr(watcher_module, "resolve_executor_worktree", fail_once)
    with pytest.raises(RuntimeError, match="PRELAUNCH_TEST_FAILURE"):
        watcher.consume_idle_architect_response(response, lambda *_: calls.append("launched"))
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    assert calls == ["failed"]
    assert fingerprint not in watcher.state.get("consumedArchitectResponses", {})

    monkeypatch.setattr(watcher_module, "resolve_executor_worktree", resolve_executor_worktree)
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    launches = []
    process = type("Process", (), {"pid": 4101})()
    assert restarted.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1]) == "EXECUTE"
    assert launches == [1]
    assert restarted.state["consumedArchitectResponses"][fingerprint]["state"] == "LAUNCHED"


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
    assert watcher.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1]) == "EXECUTE"
    assert watcher.state["prelaunchRecoveryState"] == "PRELAUNCH_INCOMPLETE"
    assert launches == [1]
    assert watcher.state["consumedArchitectResponses"][fingerprint]["state"] == "LAUNCHED"

    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    assert restarted.consume_idle_architect_response(response, lambda *_: launches.append(2)) == "DUPLICATE"
    assert launches == [1]
    assert restarted.state["lastCompletedTaskId"] == "PUB-aa3b4121887c4047b3c056bcccaa6a96"


def test_fresh_execute_is_consumed_only_after_child_launch(tmp_path):
    worktree = tmp_path / "managed-worktree"
    worktree.mkdir()
    response = envelope("normal-task", prompt=f"WORKTREE\n{worktree}\nnext")
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    launches = []
    process = type("Process", (), {"pid": 4103})()
    assert watcher.consume_idle_architect_response(response, lambda *_: (launches.append(1), process)[1]) == "EXECUTE"
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
    assert launches == [1]
    assert watcher.state["consumedArchitectResponses"][fingerprint] == {
        "taskId": "normal-task", "classification": "ACCEPTED", "action": "EXECUTE", "state": "LAUNCHED"
    }


def test_project_execute_creates_owned_worktree_and_child_uses_it(tmp_path):
    base, config, head = configured_git_project(tmp_path)
    watcher = LocalFirstOrchestrator(str(tmp_path / "orchestrator"), tmp_path / "orchestrator" / "state")
    write_project_config(watcher, config)
    task_id = "owned-task"
    response = envelope(task_id, prompt=configured_project_prompt(base, head, task_id))
    observed = []
    process = type("Process", (), {"pid": 4201})()
    assert watcher.consume_idle_architect_response(response, lambda prompt, result: (observed.append(watcher.state["targetWorktree"]), process)[1]) == "EXECUTE"
    owned = Path(watcher.state["targetWorktree"])
    assert owned.is_dir() and owned != base and owned != watcher.project_dir
    assert observed == [str(owned)]
    assert watcher.state["taskWorktrees"][task_id]["worktreePath"] == str(owned)
    assert watcher.state["taskWorktrees"][task_id]["baseCommit"] == head
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
    owned = launches[0]
    restarted = LocalFirstOrchestrator(str(root), root / "state")
    assert restarted.state["taskWorktrees"]["restart-task"]["worktreePath"] == owned
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
    assert watcher.consume_idle_architect_response(response, lambda *_: (launches.append(watcher.state["targetWorktree"]), process)[1]) == "EXECUTE"
    assert len(launches) == 1
    assert watcher.state["prelaunchRecoveryState"] == "PRELAUNCH_INCOMPLETE"
    assert len(subprocess.check_output(["git", "-C", str(base), "worktree", "list"], text=True).splitlines()) == 2


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
    assert subprocess.check_output(["git", "-C", str(base), "branch", "--show-current"], text=True).strip() == "some-other-local-branch"
    assert watcher.state["taskWorktrees"]["explicit-branch-task"]["branch"] == "hybrid-v2"


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
    assert watcher.state["taskWorktrees"]["legacy-config-task"]["branch"] == "hybrid-v2"


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
    with pytest.raises(RuntimeError, match="EXECUTOR_WORKTREE_OWNERSHIP_CONFLICT"):
        watcher.consume_idle_architect_response(response, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch")))


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
    assert watcher.authorize_postlaunch_retry(lambda *_: (launches.append(1), process)[1]) is process
    assert launches == [1]
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["humanRecoveryAuthorizedTaskId"] == task_id
    assert watcher.state["executorLaunchState"] == "LAUNCHED"
    assert watcher.state["automaticRetryAuthorized"] is False


def test_failed_human_postlaunch_retry_cannot_reuse_authorization(tmp_path, monkeypatch):
    watcher, base, worktree = postlaunch_recovery_fixture(tmp_path)
    task_id = watcher.state["taskId"]
    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: False))
    monkeypatch.setenv("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY", task_id)
    launches = []
    def fail_launch(*_args):
        launches.append(1)
        raise RuntimeError("writer conflict")
    assert watcher.authorize_postlaunch_retry(fail_launch) is None
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert launches == [1]
    assert watcher.authorize_postlaunch_retry(lambda *_: launches.append(2)) is None
    assert launches == [1]


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
    assert watcher.recover_prelaunch_incomplete(lambda *_: (launches.append(watcher.state["targetWorktree"]), process)[1]) is process
    assert launches == [str(worktree)]
    assert watcher.state["consumedArchitectResponses"][fingerprint]["state"] == "LAUNCHED"
    assert watcher.state["taskWorktrees"][task_id]["worktreePath"] == str(worktree)
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
    assert run_executor_state_once(watcher, lambda *_: (launches.append(1), process)[1]) == "EXECUTOR_RUNNING"
    assert watcher.state["state"] == "EXECUTOR_RUNNING"
    assert watcher.state["humanRecoveryAuthorizationConsumed"] is True
    assert launches == [1]


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
    assert run_executor_state_once(watcher, launch) == "EXECUTOR_RUNNING"
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
    assert run_executor_state_once(watcher, launch) == "EXECUTOR_RUNNING"
    assert run_executor_state_once(watcher, launch) == "RESULT_READY"
    assert launches == [1]
    messages = []
    class Bridge:
        def submit_result_bounded(self, message): messages.append(message)
        def assistant_baseline(self): return {"count": 1, "entries": []}
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
    assert len(launches) == 1
    assert watcher.state["state"] == "EXECUTOR_RUNNING"
    assert watcher.state["targetWorktree"] == str(worktree)
    assert Path(watcher.state["nextPromptPath"]).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert watcher.state["consumedInboxItems"]["000002"]["status"] == "LAUNCH_AUTHORIZED"


def test_consumed_inbox_item_cannot_relaunch_after_restart_or_poll(tmp_path):
    watcher, _ = inbox_watcher(tmp_path, "next")

    class Process:
        pid = 3002

    launches = []
    watcher.intake_inbox(lambda *_: (launches.append(1) or Process()))
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
    assert watcher.inspect_idle_architect(bridge, lambda prompt, result: (launches.append((prompt, result)), Process())[1]) == "EXECUTE"
    assert not watcher.inbox_dir.exists()
    assert watcher.state["state"] == "EXECUTOR_RUNNING"
    assert watcher.state["targetWorktree"] == str(worktree)
    assert len(launches) == 1


def test_consumed_architect_response_cannot_relaunch_after_restart_or_poll(tmp_path):
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    bridge = IdleArchitectBridge(envelope("architect-task-1", prompt="next"))
    watcher.inspect_idle_architect(bridge, lambda *_: type("Process", (), {"pid": 4002})())
    watcher.state["state"] = "IDLE"
    watcher.save()
    restarted = LocalFirstOrchestrator(str(tmp_path), watcher.state_dir)
    launches = []
    assert restarted.inspect_idle_architect(bridge, lambda *_: launches.append(1)) == "DUPLICATE"
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


def test_bootstrap_execute_response_uses_same_architect_task_once(tmp_path):
    worktree = tmp_path / "affotech-worktree"
    worktree.mkdir()
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    watcher.state.update({"state": "ARCHITECT_RUNNING", "architectBootstrapAwaiting": True, "taskId": None})
    response = envelope("architect-task-2", prompt=f"WORKTREE\n{worktree}\nnext")
    launches = []
    assert watcher.consume_idle_architect_response(response, lambda prompt, result: (launches.append(prompt), type("Process", (), {"pid": 4003})())[1]) == "EXECUTE"
    assert watcher.state["taskId"] == "architect-task-2"
    assert len(launches) == 1


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
        "state": "IDLE",
        "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96",
        "consumedArchitectResponses": {old_fp: {"action": "STOP", "classification": "ACCEPTED", "taskId": "old-stop"}},
        "architectResultFingerprint": old_fp,
    })
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "ARCHITECT_RUNNING"
    assert bridge.restores == 1 and bridge.snapshots == 2 and len(bridge.sent) == 1
    assert watcher.state["continuationSourceFingerprint"] == hashlib.sha256(new.encode()).hexdigest()
    assert watcher.state["architectContactCount"] == 1
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *_: None) == "IDLE"
    assert bridge.restores == 1 and len(bridge.sent) == 1


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
        assert watcher.inspect_idle_architect(bridge, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) == "DUPLICATE"
        watcher.state["state"] = "IDLE"
        watcher.save()
        assert watcher.inspect_idle_architect(bridge, lambda *_: (_ for _ in ()).throw(AssertionError("must not launch"))) == "DUPLICATE"
        assert bridge.restores == 1 and watcher.state["state"] == "IDLE"


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
        "state": "IDLE",
        "architectContactCount": 0,
        "architectBootstrapAwaiting": False,
        "architectBootstrapCount": 1,
        "architectSendState": "CONFIRMED",
        "architectResultFingerprint": old_fp,
        "architectLiveBottomFingerprint": old_fp,
        "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96",
        "consumedArchitectResponses": {old_fp: {"action": "STOP", "classification": "ACCEPTED", "taskId": "old-stop"}},
        "architectBaseline": {"entries": [{"id": "old", "text": old}, {"id": "new", "text": new}]},
        "taskId": None,
    })
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *args: launches.append(args)) == "ARCHITECT_RUNNING"
    assert bridge.restores == 0 and len(bridge.sent) == 1 and launches == []
    assert watcher.state["continuationSourceFingerprint"] == hashlib.sha256(new.encode()).hexdigest()
    assert watcher.state["architectContactCount"] == 1
    assert "architectLiveBottomFingerprint" not in watcher.state
    watcher.state["state"] = "IDLE"
    watcher.save()
    assert watcher.inspect_idle_architect(bridge, lambda *args: launches.append(args)) == "DUPLICATE"
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
    assert watcher.inspect_idle_architect(bridge, lambda *args: launches.append(args)) == "ARCHITECT_RUNNING"
    assert len(bridge.sent) == 1 and launches == []
