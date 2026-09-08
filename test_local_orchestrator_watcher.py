import json
import hashlib
import subprocess
import sys
from pathlib import Path

from local_orchestrator_watcher import (
    BEGIN, COMPLETE, END, HANDOVER_BEGIN, HANDOVER_END, READY, CodexResult,
    CodexRunner, LocalWatcher, LoopGuard, RelayAuthorityError, RelayPromptSource,
    ResultSubmissionError,
    architect_response_finished,
    choose_conversation_scroll_candidate, extract_executor_prompt,
    extract_executor_prompt_envelope, extract_handover, build_result_envelope,
    read_result_envelope, reconcile_executor_result, result_submission_key,
    verify_child_project_binding,
)


def _submission_page(*, composer=True, editable=True, send=True, enabled=True, click_error=None, acknowledge=True):
    class Locator:
        def __init__(self, kind): self.kind = kind
        @property
        def last(self): return self
        def is_visible(self, **kwargs):
            if self.kind == "composer": return composer
            if self.kind == "send": return send
            return False
        def is_editable(self, **kwargs): return editable
        def fill(self, value, **kwargs):
            if not composer: raise RuntimeError("missing composer")
            page.value = value
        def is_enabled(self, **kwargs): return enabled
        def click(self, **kwargs):
            if self.kind == "composer":
                page.focused = True
                return
            if click_error: raise click_error
            page.sent = True
            page.value = "" if acknowledge else page.value
        def press(self, key, **kwargs):
            if self.kind == "composer" and key == "ControlOrMeta+A":
                page.value = ""
    class Page:
        def __init__(self):
            self.value = ""
            self.sent = False
            self.focused = False
            self.composer_locator = Locator("composer")
            self.send_locator = Locator("send")
            self.keyboard = type("Keyboard", (), {"insert_text": lambda _, value: setattr(self, "value", value)})()
        def get_by_role(self, role, **kwargs):
            if role == "textbox": return self.composer_locator
            if role == "button": return self.send_locator
            raise AssertionError(role)
        def evaluate(self, script):
            if "return null" in script: return self.value
            if "trim() === ''" in script: return not self.value
            raise AssertionError("unexpected evaluation")
    page = Page()
    return page


def test_result_submission_uses_explicit_stages_and_acknowledges_without_live_browser():
    from local_orchestrator_watcher import ArchitectPlaywright
    page = _submission_page()
    ArchitectPlaywright(page).submit_result_bounded("captured result", timeout=1)
    assert page.sent is True


def test_result_submission_classifies_post_populate_failures():
    from local_orchestrator_watcher import ArchitectPlaywright
    cases = [
        (_submission_page(send=False), "ARCHITECT_SEND_CONTROL_UNAVAILABLE"),
        (_submission_page(enabled=False), "ARCHITECT_SEND_CONTROL_DISABLED"),
        (_submission_page(click_error=RuntimeError("click")), "ARCHITECT_SEND_ACTION_FAILED"),
        (_submission_page(acknowledge=False), "ARCHITECT_SUBMISSION_ACK_TIMEOUT"),
    ]
    for page, code in cases:
        try:
            ArchitectPlaywright(page).submit_result_bounded("captured result", timeout=0.2)
        except ResultSubmissionError as error:
            assert error.code == code
        else:
            raise AssertionError(code)


def test_result_submission_distinguishes_composer_input_rejection():
    from local_orchestrator_watcher import ArchitectPlaywright
    page = _submission_page()
    original = page.evaluate
    page.evaluate = lambda script: "wrong content" if "return null" in script else original(script)
    try:
        ArchitectPlaywright(page).submit_result_bounded("captured result", timeout=1)
    except ResultSubmissionError as error:
        assert error.code == "ARCHITECT_COMPOSER_INPUT_REJECTED"
    else:
        raise AssertionError("input rejection was not classified")


def test_result_submission_accepts_normalized_multiline_markdown_content():
    from local_orchestrator_watcher import ArchitectPlaywright
    page = _submission_page()
    original_fill = page.get_by_role("textbox").fill
    def normalized_fill(value, **kwargs):
        original_fill(value.replace("\n", " "), **kwargs)
    page.get_by_role("textbox").fill = normalized_fill
    ArchitectPlaywright(page).submit_result_bounded("line one\n\n```js\nline two\n```", timeout=1)
    assert page.sent is True


def test_result_submission_keyboard_path_does_not_call_blocking_fill():
    from local_orchestrator_watcher import ArchitectPlaywright
    page = _submission_page()
    page.composer_locator.fill = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("fill must not be used"))
    ArchitectPlaywright(page).submit_result_bounded("keyboard path", timeout=1)
    assert page.sent is True and page.focused is True


def make_bootstrap(tmp_path):
    (tmp_path / "AFFOTECH_EXECUTOR_BOOTSTRAP.md").write_text("AFFOTECH EXECUTOR TEST BOOTSTRAP\n", encoding="utf-8")


def test_prompt_extraction_requires_exact_completion_marker():
    response = f"intro\n{BEGIN}\nDo the bounded work\n{END}\n{COMPLETE}"
    assert extract_executor_prompt(response) == "Do the bounded work"
    assert extract_executor_prompt(response.replace(COMPLETE, "")) is None
    assert extract_executor_prompt(f"{COMPLETE}\nno block") is None
    duplicate = f"{BEGIN}\nfirst\n{END}\n{BEGIN}\nsecond\n{END}\n{COMPLETE}"
    assert extract_executor_prompt(duplicate) is None


def test_relay_pointer_and_manifest_are_verified_from_one_captured_ref():
    import hashlib
    publication = "PUB-" + "a" * 32
    manifest = {"protocolVersion": "1.0", "createdAt": "2026-01-01T00:00:00Z", "publicationId": publication,
                "recipientRole": "EXECUTOR", "status": "READY_FOR_EXECUTION", "executionTarget": "WINDOWS_LOCAL_CODEX",
                "prompt": "EXACT RELAY PROMPT", "senderRole": "ARCHITECT", "requiredInvariantSetId": "AFFOTECH-NR-001", "requiredInvariantContentSha256": "a" * 64}
    content_hash = hashlib.sha256(manifest["prompt"].encode()).hexdigest()
    manifest["contentSha256"] = content_hash
    class Source(RelayPromptSource):
        def __init__(self): super().__init__("unused", refresh=False); self.captured_ref = "ref"
        def _show_json(self, path):
            if path == RELAY_POINTER: return {"publicationId": publication, "contentSha256": content_hash}
            return manifest
    from local_orchestrator_watcher import RELAY_POINTER
    result = Source().read_current()
    assert result["prompt"] == "EXACT RELAY PROMPT"


def test_relay_reader_pins_all_authority_reads_to_remote_head_not_dirty_worktree(tmp_path):
    """A fetched remote snapshot wins over a stale local checkout."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    cache = tmp_path / "cache"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    def git(path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()
    git(seed, "config", "user.email", "test@example.invalid")
    git(seed, "config", "user.name", "Test")
    git(seed, "remote", "add", "origin", str(bare))
    publication = "PUB-" + "a" * 32
    prompt = "REMOTE SNAPSHOT → prompt\n"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    manifest = {"protocolVersion":"1.0", "publicationId":publication,
                "contentSha256":digest, "recipientRole":"EXECUTOR",
                "status":"READY_FOR_EXECUTION", "executionTarget":"WINDOWS_LOCAL_CODEX",
                "requiredInvariantSetId":"AFFOTECH-NR-001", "requiredInvariantContentSha256":"a" * 64,
                "prompt":prompt}
    pointer = {"publicationId":publication, "contentSha256":digest}
    current = seed / "relay" / "current"
    prompt_dir = seed / "relay" / "architect" / "prompts" / publication
    current.mkdir(parents=True)
    prompt_dir.mkdir(parents=True)
    (current / "LATEST_ARCHITECT_PROMPT.json").write_text(json.dumps(pointer), encoding="utf-8")
    (prompt_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (prompt_dir / "prompt.md").write_bytes(prompt.encode("utf-8"))
    git(seed, "add", "."); git(seed, "commit", "-m", "remote authority"); git(seed, "push", "origin", "main")
    subprocess.run(["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(cache)], check=True, capture_output=True)
    # Make the cache working tree disagree and create a newer remote snapshot.
    (cache / "relay" / "current" / "LATEST_ARCHITECT_PROMPT.json").write_text("{\"stale\":true}", encoding="utf-8")
    git(seed, "commit", "--allow-empty", "-m", "remote followup"); git(seed, "push", "origin", "main")
    source = RelayPromptSource(cache)
    ref = source.refresh()
    assert ref == git(cache, "rev-parse", "refs/remotes/origin/main")
    assert source.read_current()["publicationId"] == publication
    assert source.captured_ref == ref
    class Runner:
        def __init__(self): self.calls = []; self.relay_authority = None
        def run(self, prompt, timeout):
            self.calls.append(prompt)
            return CodexResult("COMPLETED", "fixture result", 0, False)
    class Bridge:
        def submit_result(self, result): pass
    runner = Runner()
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=runner)
    assert watcher.run_relay_once(source, Bridge(), emit=lambda _: None) == "COMPLETED"
    assert runner.calls == [prompt]
    assert runner.relay_authority["snapshotCommit"] == ref
    assert runner.relay_authority["publicationId"] == publication


def test_bootstrap_is_required_and_precedes_exact_task(tmp_path):
    runner = CodexRunner(str(tmp_path), sys.executable)
    try:
        runner.assemble_prompt("task")
    except RuntimeError as error:
        assert "BOOTSTRAP_UNAVAILABLE" in str(error)
    else:
        raise AssertionError("missing bootstrap was accepted")
    make_bootstrap(tmp_path)
    assembled = runner.assemble_prompt("EXACT TASK BYTES")
    assert assembled.startswith("AFFOTECH EXECUTOR TEST BOOTSTRAP")
    assert assembled.endswith("EXACT TASK BYTES")
    assert assembled.index("AFFOTECH EXECUTOR TEST BOOTSTRAP") < assembled.index("EXACT TASK BYTES")


def test_relay_validation_rejects_pointer_manifest_and_policy_mismatches():
    import hashlib
    from local_orchestrator_watcher import RELAY_POINTER
    publication = "PUB-" + "b" * 32
    def make(overrides=None):
        manifest = {"protocolVersion": "1.0", "createdAt": "x", "publicationId": publication,
                    "recipientRole": "EXECUTOR", "status": "READY_FOR_EXECUTION", "executionTarget": "WINDOWS_LOCAL_CODEX", "prompt": "p", "requiredInvariantSetId": "AFFOTECH-NR-001", "requiredInvariantContentSha256": "b" * 64}
        manifest.update(overrides or {})
        manifest["contentSha256"] = hashlib.sha256(manifest["prompt"].encode()).hexdigest()
        return manifest
    for overrides, pointer_hash in [({"publicationId": "PUB-" + "c" * 32}, None), ({"recipientRole": "CURATOR"}, "pointer"), ({"status": "DRAFT"}, None), ({"executionTarget": "OTHER"}, None), ({"prompt": ""}, None)]:
        manifest = make(overrides)
        class Source(RelayPromptSource):
            def __init__(self): super().__init__("unused", refresh=False); self.captured_ref = "ref"
            def _show_json(self, path):
                if path == RELAY_POINTER: return {"publicationId": publication, "contentSha256": pointer_hash or manifest["contentSha256"]}
                return manifest
        try:
            Source().read_current()
        except RelayAuthorityError:
            pass
        else:
            raise AssertionError("invalid relay publication was accepted")


def test_invalid_same_content_publication_does_not_suppress_corrected_identity(tmp_path):
    import hashlib
    from local_orchestrator_watcher import RELAY_POINTER, stable_json
    invalid_id = "PUB-" + "9" * 32
    corrected_id = "PUB-" + "f" * 32
    body = {"recipientRole": "EXECUTOR", "senderRole": "ARCHITECT", "status": "READY_FOR_EXECUTION", "executionTarget": "WINDOWS_LOCAL_CODEX", "prompt": "same bytes", "requiredInvariantSetId": "AFFOTECH-NR-001", "requiredInvariantContentSha256": "c" * 64}
    content_hash = hashlib.sha256(body["prompt"].encode()).hexdigest()
    invalid = {"protocolVersion": "1.0", "publicationId": invalid_id, **{k: v for k, v in body.items() if k not in {"requiredInvariantSetId", "requiredInvariantContentSha256"}}, "contentSha256": content_hash}
    corrected = {"protocolVersion": "1.0", "publicationId": corrected_id, **body, "contentSha256": content_hash}
    class Source(RelayPromptSource):
        def __init__(self, manifest): super().__init__("unused", refresh=False); self.captured_ref = "ref"; self.manifest = manifest
        def _show_json(self, path):
            if path == RELAY_POINTER: return {"publicationId": self.manifest["publicationId"], "contentSha256": content_hash}
            return self.manifest
    try:
        Source(invalid).read_current()
    except RelayAuthorityError as error:
        assert "INVARIANT" in str(error)
    else:
        raise AssertionError("invalid publication was accepted")
    class Runner:
        def __init__(self): self.calls = []
        def run(self, prompt, timeout): self.calls.append(prompt); return CodexResult("COMPLETED", "machine result", 0, False)
    class Bridge:
        def submit_result(self, result): pass
    runner = Runner(); watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner)
    assert watcher.run_relay_once(Source(corrected), Bridge(), emit=lambda _: None) == "COMPLETED"
    assert watcher.run_relay_once(Source(corrected), Bridge(), emit=lambda _: None) == "IDLE"
    assert runner.calls == ["same bytes"]


def test_pending_relay_publication_dispatches_once_and_repeated_key_stays_idle(tmp_path):
    publication = "PUB-" + "d" * 32
    class Source:
        def read_current(self): return {"publicationId": publication, "contentSha256": "e" * 64, "prompt": "EXACT RELAY PROMPT"}
    class Runner:
        def __init__(self): self.calls = []
        def run(self, prompt, timeout): self.calls.append(prompt); return CodexResult("COMPLETED", "ok", 0, False)
    class Bridge:
        def submit_result(self, result): pass
    runner = Runner(); watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner)
    output = []
    assert watcher.run_relay_once(Source(), Bridge(), emit=output.append) == "COMPLETED"
    assert watcher.run_relay_once(Source(), Bridge(), emit=output.append) == "IDLE"
    assert runner.calls == ["EXACT RELAY PROMPT"]
    assert any(line.startswith("RELAY_PROMPT_DETECTED") for line in output)


def test_relay_inflight_key_fails_closed_without_retry(tmp_path):
    class Source:
        def read_current(self): return {"publicationId": "PUB-" + "f" * 32, "contentSha256": "1" * 64, "prompt": "p"}
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state["in_flight_relay_key"] = "existing"
    output = []
    assert watcher.run_relay_once(Source(), None, emit=output.append) == "RECOVERY_REQUIRED"
    assert output[-1] == "STATE=RECOVERY_REQUIRED"


def test_relay_prompt_discovery_does_not_read_architect_dom(tmp_path):
    class Source:
        def read_current(self): return {"publicationId": "PUB-" + "1" * 32, "contentSha256": "2" * 64, "prompt": "relay prompt"}
    class ForbiddenArchitect:
        def __getattr__(self, name): raise AssertionError(f"Architect DOM read: {name}")
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    seen = []
    def intercepted(bridge, prompt, timeout, emit, **kwargs): seen.append((bridge, prompt)); return True
    watcher._execute_prompt = intercepted
    assert watcher.run_relay_once(Source(), ForbiddenArchitect(), emit=lambda _: None) == "COMPLETED"
    assert seen[0][1] == "relay prompt"


def test_completed_result_is_persisted_before_unavailable_composer_and_recovers(tmp_path):
    make_bootstrap(tmp_path)
    class Runner:
        def run(self, prompt, timeout): return CodexResult("COMPLETED", "captured result", 0, False)
    class Bridge:
        def submit_result_bounded(self, result): raise TimeoutError("composer unavailable")
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner())
    output = []
    assert watcher._execute_prompt(Bridge(), "task", 1, output.append) is False
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["executor_completed"] is True and saved["result_pending"] is True
    assert Path(saved["result_file"]).read_text() == "captured result"
    assert any(line.startswith("RESULT_SUBMISSION_DEFERRED reason=") for line in output)
    assert any("reason=TimeoutError:composer unavailable" in line for line in output)


def test_restart_recovers_pending_result_without_launching_codex(tmp_path):
    result_path = tmp_path / "captured-result.txt"
    result_path.write_text("preserved result", encoding="utf-8")
    (tmp_path / "captured-result.txt.envelope.json").write_text(json.dumps(build_result_envelope("D", "PASS", "PUB-" + "7" * 32)), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "cycle_count": 1,
        "in_flight": False,
        "executor_completed": True,
        "result_pending": True,
        "result_file": str(result_path),
        "relay_publication_id": "PUB-" + "7" * 32,
        "in_flight_relay_key": "PUB-" + "7" * 32 + ":" + "8" * 64,
    }), encoding="utf-8")
    class Source:
        def read_current(self): return {"publicationId": "PUB-" + "9" * 32, "contentSha256": "a" * 64, "prompt": "must not launch"}
    class Bridge:
        def __init__(self): self.results = []
        def submit_result_bounded(self, result): self.results.append(result)
    class Runner:
        def run(self, prompt, timeout): raise AssertionError("Codex must not be relaunched")
    bridge = Bridge()
    watcher = LocalWatcher(str(tmp_path), state_path, Runner())
    assert watcher.run_relay_once(Source(), bridge, emit=lambda _: None) == "COMPLETED"
    assert bridge.results == ["preserved result"]
    saved = json.loads(state_path.read_text())
    assert saved["result_pending"] is False
    assert saved["last_completed_relay_key"].startswith("PUB-" + "7" * 32)


def test_architect_rollover_counts_to_thirty_and_requests_once(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, STANDARD_HANDOVER_REQUEST
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    rollover.initialize_current_session()
    class Bridge:
        def __init__(self): self.requests = []
        def submit_result_bounded(self, value): self.requests.append(value)
    bridge = Bridge()
    for index in range(29):
        assert rollover.observe_complete_response(f"response {index}\n{COMPLETE}")
        assert not rollover.request_if_due(bridge, True, True)
    assert watcher.state["architectResponseCount"] == 29
    assert rollover.observe_complete_response(f"response 30\n{COMPLETE}")
    assert rollover.request_if_due(bridge, True, True)
    assert not rollover.request_if_due(bridge, True, True)
    assert bridge.requests == [STANDARD_HANDOVER_REQUEST]
    assert watcher.state["handoverRequested"] is True


def test_architect_rollover_requires_running_executor_and_dedupes_events(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher); rollover.initialize_current_session()
    for index in range(30): rollover.observe_complete_response(f"same-{index}\n{COMPLETE}")
    class Bridge:
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, value): self.calls += 1
    bridge = Bridge()
    assert not rollover.request_if_due(bridge, False, True)
    assert not rollover.request_if_due(bridge, True, False)
    assert rollover.request_if_due(bridge, True, True)
    assert not rollover.request_if_due(bridge, True, True)
    assert bridge.calls == 1


def test_architect_memory_tree_aggregates_only_owned_root_and_descendants():
    from local_orchestrator_watcher import architect_process_tree_memory_bytes
    rows = [
        {"pid": 10, "parentPid": 1, "workingSet": 100},
        {"pid": 11, "parentPid": 10, "workingSet": 200},
        {"pid": 12, "parentPid": 11, "workingSet": 300},
        {"pid": 99, "parentPid": 1, "workingSet": 9999},
    ]
    assert architect_process_tree_memory_bytes(10, rows) == 600


def test_memory_threshold_is_primary_and_creates_one_pending_rollover(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, ARCHITECT_MEMORY_THRESHOLD_BYTES
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    lines = []
    assert rollover.sample_memory(lambda: ARCHITECT_MEMORY_THRESHOLD_BYTES, lines.append) == "MEMORY_THRESHOLD"
    assert rollover.sample_memory(lambda: ARCHITECT_MEMORY_THRESHOLD_BYTES + 1, lines.append) == "MEMORY_THRESHOLD"
    assert watcher.state["rolloverPending"] is True
    assert watcher.state["rolloverTrigger"] == "MEMORY_THRESHOLD"
    assert lines == ["ROLLOVER_PENDING trigger=MEMORY_THRESHOLD"]


def test_rollover_waits_for_generation_and_requests_once_after_threshold(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, ARCHITECT_MEMORY_THRESHOLD_BYTES
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    rollover.sample_memory(lambda: ARCHITECT_MEMORY_THRESHOLD_BYTES, lambda _: None)
    class Bridge:
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, value): self.calls += 1
    bridge = Bridge()
    assert not rollover.request_if_due(bridge, True, True, lambda _: None, architect_generating=True)
    assert rollover.request_if_due(bridge, True, True, lambda _: None)
    assert not rollover.request_if_due(bridge, True, True, lambda _: None)
    assert bridge.calls == 1


def test_response_count_remains_fallback_when_memory_is_below_threshold(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    watcher.state["architectResponseCount"] = 29
    assert rollover.rollover_trigger(0, 29) is None
    assert rollover.rollover_trigger(0, 30) == "RESPONSE_COUNT_FALLBACK"
    assert rollover.rollover_trigger(1_073_741_824, 0) == "MEMORY_THRESHOLD"


def test_pending_rollover_blocks_new_relay_execution(tmp_path):
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state["rolloverPending"] = True
    class Source:
        def read_current(self):
            return {"publicationId": "PUB-" + "a" * 32, "contentSha256": "b" * 64, "prompt": "must not run"}
    assert watcher.run_relay_once(Source(), None, emit=lambda _: None) == "ROLLOVER_PENDING"


def test_documentation_requires_structured_architect_acceptance_and_dedupes(tmp_path):
    from local_orchestrator_watcher import DocumentationDoorbell, documentation_requirement
    record = {"classification": "ACCEPTED", "milestoneId": "M-1", "acceptedPublicationId": "PUB-1", "milestoneKind": "IMPLEMENTATION", "implementationCommit": "abc"}
    assert documentation_requirement(record) == (True, "ACCEPTED_IMPLEMENTATION")
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    class Bridge:
        def __init__(self): self.messages = []
        def submit_result_bounded(self, value): self.messages.append(value)
    bridge = Bridge(); doorbell = DocumentationDoorbell(watcher)
    assert doorbell.evaluate_and_trigger(record, bridge, lambda _: None) == "TRIGGER_SENT"
    assert doorbell.evaluate_and_trigger(record, bridge, lambda _: None) == "TRIGGER_SENT"
    assert len(bridge.messages) == 1
    assert "problemDetected" not in bridge.messages[0]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["docTriggerKey"] == "M-1:PUB-1"
    assert saved["documentationTriggerCount"] == 1


def test_documentation_trigger_defaults_and_overrides_are_fail_closed(tmp_path):
    from local_orchestrator_watcher import documentation_requirement
    base = {"classification": "ACCEPTED"}
    for kind in ("BUG_FIX", "REPAIR", "RECOVERY", "ARCHITECTURE_CHANGE", "GOVERNANCE_CHANGE", "INCIDENT_CLOSURE"):
        assert documentation_requirement({**base, "milestoneKind": kind})[0] is True
    assert documentation_requirement({**base, "milestoneKind": "DIAGNOSTIC"}) == (False, None)
    assert documentation_requirement({"classification": "BLOCKED", "milestoneKind": "REPAIR"}) == (False, None)
    assert documentation_requirement({**base, "milestoneKind": "IMPLEMENTATION", "documentationOnAcceptance": "NONE"}) == (False, None)
    assert documentation_requirement({**base, "documentationOnAcceptance": "REQUIRED"})[0] is True
    assert documentation_requirement({**base, "problemDetected": True, "problemResolved": True}) == (True, "ACCEPTED_DISCOVERED_AND_RESOLVED_PROBLEM")
    assert documentation_requirement({**base, "problemDetected": True, "problemResolved": False}) == (False, None)


def test_documentation_restart_does_not_duplicate_trigger(tmp_path):
    from local_orchestrator_watcher import DocumentationDoorbell
    record = {"classification": "ACCEPTED", "milestone": "M-2", "publicationId": "PUB-2", "milestoneKind": "REPAIR"}
    state_path = tmp_path / "state.json"
    first = LocalWatcher(str(tmp_path), state_path, runner=object())
    class Bridge:
        def __init__(self): self.messages = []
        def submit_result_bounded(self, value): self.messages.append(value)
    b1 = Bridge(); assert DocumentationDoorbell(first).evaluate_and_trigger(record, b1, lambda _: None) == "TRIGGER_SENT"
    second = LocalWatcher(str(tmp_path), state_path, runner=object())
    b2 = Bridge(); assert DocumentationDoorbell(second).evaluate_and_trigger(record, b2, lambda _: None) == "TRIGGER_SENT"
    assert b1.messages and not b2.messages


def test_architect_rollover_fail_closed_preserves_old_tab_on_handover_or_new_tab_failure(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher); rollover.initialize_current_session()
    watcher.state["architectResponseCount"] = 30
    class OldPage:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    class Bridge:
        def submit_result_bounded(self, value): pass
        def open_fresh_with_handover(self, value): raise RuntimeError("new tab failed")
    bridge = Bridge(); bridge.page = OldPage()
    rollover.request_if_due(bridge, True, True)
    response = "HANDOVER\nARCHITECT_HANDOVER_READY"
    assert not rollover.complete_from_response(bridge, response)
    assert bridge.page.closed is False
    assert watcher.state["handoverReady"] is True
    assert watcher.state["pending_handover"] == response


def test_successful_architect_rollover_switches_then_closes_old_tab_and_resets_count(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher); rollover.initialize_current_session()
    watcher.state["architectResponseCount"] = 30
    class Page:
        def __init__(self, url): self.url = url; self.closed = False
        def close(self): self.closed = True
    old = Page("old"); new = Page("new")
    class Bridge:
        page = old
        def submit_result_bounded(self, value): pass
        def open_fresh_with_handover(self, value): return new
    bridge = Bridge(); rollover.request_if_due(bridge, True, True)
    assert rollover.complete_from_response(bridge, "handover\nARCHITECT_HANDOVER_READY")
    assert bridge.page is new and old.closed
    assert watcher.state["architectResponseCount"] == 0
    assert watcher.state["handoverRequested"] is False


def test_completion_fallback_requires_two_stable_full_polls_and_no_generation():
    from local_orchestrator_watcher import ArchitectPlaywright
    prompt_response = f"answer\n{BEGIN}\ncomplete without marker\n{END}"

    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "old", "text": "old"}],
                [{"id": "new", "text": prompt_response}],
                [{"id": "new", "text": prompt_response}],
            ])
            self.generating = iter([False, False])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return next(self.generating)
        def locator(self, selector):
            raise AssertionError("fallback completion must remain atomic")

    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_baseline()
    assert bridge.wait_for_new_response(baseline, timeout=2)["state"] == "COMPLETED"
    assert extract_executor_prompt_envelope(prompt_response) == "complete without marker"


def test_completion_fallback_rejects_generation_visible_and_malformed_envelopes():
    assert extract_executor_prompt_envelope(f"{BEGIN}\none\n{END}\n{BEGIN}\ntwo\n{END}") is None
    assert extract_executor_prompt_envelope(f"{BEGIN}\nouter {BEGIN}\ninner\n{END}\n{END}") is None

    from local_orchestrator_watcher import ArchitectPlaywright
    response = f"answer\n{BEGIN}\nnot yet\n{END}"
    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "old", "text": "old"}],
                [{"id": "new", "text": response}],
                [{"id": "new", "text": response}],
            ])
            self.generating = iter([True, True])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return next(self.generating)
    assert ArchitectPlaywright(Page()).wait_for_new_response(
        ArchitectPlaywright(Page()).assistant_baseline(), timeout=0.01
    )["state"] == "NOT_YET"


def test_startup_recovery_accepts_existing_unmarked_stable_prompt_without_launching(tmp_path, monkeypatch):
    from local_orchestrator_watcher import ArchitectPlaywright
    monkeypatch.setattr("local_orchestrator_watcher.time.sleep", lambda _: None)
    response = f"existing\n{BEGIN}\nrecovered\n{END}"
    class Page:
        def __init__(self): self.calls = 0
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                self.calls += 1
                return [{"id": "pending", "text": response}]
            return False
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    assert watcher.startup_candidate(bridge, scan_history=False) == "recovered"


def test_old_unmarked_response_does_not_satisfy_new_submission():
    class Messages:
        def __init__(self): self.values = ["old response"]
        def count(self): return len(self.values)
        def nth(self, index): return type("Message", (), {"inner_text": lambda self: messages.values[index]})()
    messages = Messages()
    class Page:
        def __init__(self): self.submitted = False
        def locator(self, selector): return messages
        def get_by_role(self, role, **kwargs): return type("Role", (), {"count": lambda self: 0})()
    page = Page()
    # The old response is not complete; a new completed response is accepted only after identity changes.
    from local_orchestrator_watcher import ArchitectPlaywright
    bridge = ArchitectPlaywright(page)
    baseline = bridge.assistant_baseline()
    assert not architect_response_finished("old response")
    messages.values.append(f"new\n{COMPLETE}")
    assert bridge.wait_for_new_completed_response(baseline, 1).endswith(COMPLETE)


def test_confirmed_submission_not_yet_and_running_states_are_not_blocked():
    from local_orchestrator_watcher import ArchitectPlaywright
    class Messages:
        def __init__(self): self.values = ["old"]
        def count(self): return len(self.values)
        def nth(self, index): return type("Message", (), {"inner_text": lambda self: messages.values[index]})()
    messages = Messages()
    class Page:
        def __init__(self): self.running = False
        def locator(self, selector): return messages
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return [{"id": str(index), "text": value} for index, value in enumerate(messages.values)]
            return self.running
        def get_by_role(self, role, **kwargs): return type("Role", (), {"count": lambda self: 1 if page.running else 0})()
    page = Page(); bridge = ArchitectPlaywright(page); base = bridge.assistant_baseline()
    assert bridge.wait_for_new_response(base, 0.01)["state"] == "NOT_YET"
    messages.values.append("partial"); page.running = True
    assert bridge.wait_for_new_response(base, 0.01)["state"] == "NOT_YET"  # active generation remains a wait state
    assert bridge.last_state == "RUNNING"
    page.running = False; messages.values[-1] = f"done\n{COMPLETE}"
    assert bridge.wait_for_new_response(base, 1)["state"] == "COMPLETED"


def test_completion_and_loop_guard_are_deterministic():
    assert architect_response_finished(COMPLETE)
    assert not architect_response_finished(COMPLETE, generation_control_visible=True)
    guard = LoopGuard()
    assert guard.check("a  prompt") == "FORWARD"
    assert guard.check("a prompt") == "LOOP_SUSPECTED"
    guard.record_result("new result")
    assert guard.check("a prompt") == "FORWARD"


def test_cycle_counts_only_forwarded_prompts_and_handover_fail_safe(tmp_path):
    class FakeRunner:
        def run(self, prompt, timeout):
            return CodexResult("COMPLETED", "ok", 0, False)
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", FakeRunner())
    for i in range(30):
        watcher.forward(f"prompt {i}")
    assert watcher.handover_due()
    handover = f"{HANDOVER_BEGIN}\nstate\n{HANDOVER_END}\n{COMPLETE}"
    assert extract_handover(handover) == "state"
    assert watcher.accept_documentation_closure("DOCUMENTATION_SYNC_COMPLETE\nARCHITECT_RESPONSE_COMPLETE") is True
    assert watcher.rotate_after_ready("state", False) is False
    assert watcher.state["pending_handover"] == "state"
    assert watcher.rotate_after_ready("state", True) is True
    assert watcher.state["cycle_count"] == 0
    assert READY not in watcher.state


def test_codex_wrapper_sentinel_is_non_affotech_and_captures_output(tmp_path):
    make_bootstrap(tmp_path)
    runner = CodexRunner(str(tmp_path), sys.executable)
    # The runner contract is exercised without invoking AFFOTECH or a live browser.
    runner.executable = sys.executable
    result = runner.run("", timeout=5)
    assert result.state == "BLOCKED"
    assert result.exit_code != 0


def test_codex_wrapper_uses_unique_last_message_file_and_prefers_it(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    seen = {}
    class FakeProcess:
        pid = 4242
        returncode = 0
        def __init__(self, command, **kwargs):
            seen["command"] = command
            path = Path(command[command.index("-o") + 1])
            path.write_text("LAST_MESSAGE_SENTINEL\n")
        def poll(self): return None
        def communicate(self, **kwargs): return "stdout-not-final", "stderr-not-final"
        def kill(self): pass
        def wait(self): return self.returncode
    def fake_popen(command, **kwargs):
        return FakeProcess(command, **kwargs)
    monkeypatch.setattr(watcher_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(watcher_module, "discover_codex_launcher", lambda executable="codex": ["node.exe", "codex.js"])
    result = CodexRunner(str(tmp_path), "codex").run("FULL_PROMPT_ON_STDIN", timeout=5)
    assert result.state == "COMPLETED"
    assert result.output == "LAST_MESSAGE_SENTINEL"
    assert result.last_message_exists
    assert "-o" in seen["command"] and seen["command"][seen["command"].index("-") - 1] == result.last_message_path
    assert seen["command"][-1] == "-"


def test_visible_windows_console_preserves_real_command_prompt_and_result_contract(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    runner = CodexRunner(str(tmp_path))
    captured = {}
    def visible(prompt, timeout, command, result_path, child_cwd=None):
        captured.update({"prompt": prompt, "timeout": timeout, "command": command, "result_path": result_path})
        return CodexResult("COMPLETED", "VISIBLE_EXECUTOR_SENTINEL", 0, False, last_message_path=result_path, last_message_exists=True)
    monkeypatch.setattr(runner, "_use_visible_windows_console", lambda: True)
    monkeypatch.setattr(runner, "_run_visible_windows_console", visible)
    result = runner.run("VISIBLE_EXECUTOR_SENTINEL", timeout=17)
    assert captured["prompt"].startswith("AFFOTECH EXECUTOR TEST BOOTSTRAP")
    assert captured["prompt"].count("AFFOTECH EXECUTOR TEST BOOTSTRAP") == 1
    assert captured["prompt"].count("VISIBLE_EXECUTOR_SENTINEL") == 1
    assert captured["timeout"] == 17
    assert captured["command"][-1] == "-"
    assert captured["command"][captured["command"].index("-o") + 1] == captured["result_path"]
    assert result.exit_code == 0 and result.last_message_exists


def test_actual_visible_windows_path_writes_assembled_payload_without_nameerror(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    captured = {}
    class Stdin:
        def write(self, value): captured["payload"] = value
        def close(self): captured["closed"] = True
    class Host:
        stdin = Stdin()
        pid = 7331
    def fake_popen(command, **kwargs):
        captured["command"] = command
        status_path = command[2]
        Path(status_path).write_text(json.dumps({"phase": "FINISHED", "pid": 7331, "exitCode": 0}), encoding="utf-8")
        return Host()
    monkeypatch.setattr(watcher_module.subprocess, "Popen", fake_popen)
    last_message = tmp_path / "last-message.txt"
    last_message.write_text("terminal", encoding="utf-8")
    runner = CodexRunner(str(tmp_path), sys.executable)
    payload = runner.assemble_prompt("EXACT WINDOWS TASK")
    result = runner._run_visible_windows_console(payload, 1, [sys.executable, "codex.js", "-"], str(last_message))
    assert result.exit_code == 0
    assert captured["payload"].decode("utf-8") == payload
    assert captured["payload"].decode("utf-8").count("AFFOTECH EXECUTOR TEST BOOTSTRAP") == 1
    assert captured["payload"].decode("utf-8").count("EXACT WINDOWS TASK") == 1
    assert captured["closed"] is True


def test_actual_visible_windows_path_transports_unicode_utf8_exactly(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    captured = {}
    class Stdin:
        def write(self, value): captured["payload"] = value
        def close(self): pass
    class Host:
        stdin = Stdin()
        pid = 7332
    def fake_popen(command, **kwargs):
        Path(command[2]).write_text(json.dumps({"phase": "FINISHED", "pid": 7332, "exitCode": 0}), encoding="utf-8")
        assert kwargs["text"] is False
        return Host()
    monkeypatch.setattr(watcher_module.subprocess, "Popen", fake_popen)
    last_message = tmp_path / "last-message.txt"
    last_message.write_text("terminal", encoding="utf-8")
    runner = CodexRunner(str(tmp_path), sys.executable)
    payload = runner.assemble_prompt("ASCII\n→\n—\nline two")
    result = runner._run_visible_windows_console(payload, 1, [sys.executable, "codex.js", "-"], str(last_message))
    assert result.exit_code == 0
    assert captured["payload"] == payload.encode("utf-8")


def test_codex_child_is_explicitly_bound_to_target_project_and_not_watcher_cwd(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    captured = {}
    class Process:
        pid = 8127
        def poll(self): return None
        def communicate(self, **kwargs): captured["input"] = kwargs["input"]; return "", ""
        def wait(self): return 0
    def fake_popen(command, **kwargs):
        captured.update(command=command, cwd=kwargs["cwd"])
        return Process()
    monkeypatch.setattr(watcher_module.subprocess, "Popen", fake_popen)
    runner = CodexRunner(str(tmp_path), sys.executable, child_project_dir=str(tmp_path), child_identity_verifier=lambda cwd: {"childCwd": cwd, "repositoryIdentity": "synthetic"})
    runner.run("bound task", timeout=1)
    assert captured["cwd"] == str(tmp_path)
    assert captured["command"][captured["command"].index("-C") + 1] == str(tmp_path)
    assert captured["input"].endswith("bound task")


def test_wrong_child_project_fails_closed_before_process_or_prompt_delivery(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    runner = CodexRunner(str(tmp_path), sys.executable, child_project_dir=str(tmp_path), child_identity_verifier=lambda cwd: (_ for _ in ()).throw(RuntimeError("CODEX_CHILD_PROJECT_IDENTITY_MISMATCH")))
    import local_orchestrator_watcher as watcher_module
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("child must not start")))
    try:
        runner.run("must not be delivered", timeout=1)
    except RuntimeError as error:
        assert "IDENTITY_MISMATCH" in str(error)
    else:
        raise AssertionError("wrong project was accepted")


def test_child_project_identity_does_not_require_hybrid_v2_branch(tmp_path):
    from local_orchestrator_watcher import verify_child_project_binding
    (tmp_path / ".git").mkdir()
    calls = []
    import local_orchestrator_watcher as watcher_module
    original = watcher_module.subprocess.check_output
    def fake_check_output(args, **kwargs):
        calls.append(args)
        if "--show-toplevel" in args: return str(tmp_path)
        return "origin\thttps://github.com/nakfreeajer/affotech-system-v2-hybrid.git (fetch)\n"
    watcher_module.subprocess.check_output = fake_check_output
    try:
        identity = verify_child_project_binding(str(tmp_path))
    finally:
        watcher_module.subprocess.check_output = original
    assert identity["repositoryIdentity"] == "https://github.com/nakfreeajer/affotech-system-v2-hybrid.git"
    assert all("branch" not in call for call in calls)


def test_affotech_codex_runner_resumes_dedicated_session_from_bound_cwd(tmp_path, monkeypatch):
    import local_orchestrator_watcher as watcher_module
    bootstrap = tmp_path / "bootstrap.md"
    bootstrap.write_text("AFFOTECH COLD START\n", encoding="utf-8")
    calls = {}
    class Process:
        pid = 4321
        def communicate(self, input=None, timeout=None):
            calls["input"] = input
            return "", ""
        def poll(self): return 0
        def wait(self): return 0
    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["cwd"] = kwargs["cwd"]
        return Process()
    monkeypatch.setattr(watcher_module.subprocess, "Popen", fake_popen)
    runner = CodexRunner(
        str(tmp_path), sys.executable, bootstrap_path=bootstrap,
        child_project_dir=str(tmp_path),
        child_identity_verifier=lambda cwd: {"childCwd": cwd, "repositoryIdentity": "synthetic"},
        session_id="019f842e-98bc-7672-a619-51441d91be00",
    )
    result = runner.run("CURRENT IMMUTABLE TASK", timeout=1)
    assert result.exit_code == 0
    assert calls["cwd"] == str(tmp_path)
    assert calls["command"][0:3] == [sys.executable, "exec", "resume"]
    assert "019f842e-98bc-7672-a619-51441d91be00" in calls["command"]
    assert "--ephemeral" not in calls["command"]
    assert "AFFOTECH COLD START" in calls["input"]
    assert "CURRENT IMMUTABLE TASK" in calls["input"]


def test_non_affotech_runner_does_not_use_dedicated_affotech_session(tmp_path):
    runner = CodexRunner(str(tmp_path), executable=sys.executable)
    assert runner.session_id is None


def test_result_submission_key_is_publication_and_result_identity():
    assert result_submission_key("PUB-A", "result") != result_submission_key("PUB-B", "result")
    assert result_submission_key("PUB-A", "result") == result_submission_key("PUB-A", "result")


def test_completed_result_submission_is_not_repeated_after_restart(tmp_path):
    result_path = tmp_path / "result.txt"
    result_path.write_text("same result", encoding="utf-8")
    publication = "PUB-" + "a" * 32
    (tmp_path / "result.txt.envelope.json").write_text(json.dumps(build_result_envelope("D", "PASS", publication)), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"result_pending": True, "executor_completed": True, "result_file": str(result_path), "result_envelope_file": str(tmp_path / "result.txt.envelope.json"), "relay_publication_id": publication, "in_flight_relay_key": publication + ":" + "b" * 64, "last_submitted_result_key": result_submission_key(publication, "same result")}), encoding="utf-8")
    class Source:
        def read_current(self): return {"publicationId": publication, "contentSha256": "b" * 64, "prompt": "no launch"}
    class Bridge:
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, result): self.calls += 1
    bridge = Bridge()
    watcher = LocalWatcher(str(tmp_path), state_path, runner=object())
    assert watcher.run_relay_once(Source(), bridge, emit=lambda _: None) == "COMPLETED"
    assert bridge.calls == 0
    assert json.loads(state_path.read_text())["result_pending"] is False


def test_codex_child_completion_requires_waited_integer_exit_and_real_pid(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    class Process:
        pid = 8123
        returncode = None
        def poll(self): return None
        def communicate(self, **kwargs): self.returncode = 0; return "out", ""
        def wait(self): return self.returncode
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(watcher_module, "discover_codex_launcher", lambda executable="codex": [sys.executable])
    runner = CodexRunner(str(tmp_path), "codex")
    started = []
    runner.on_start = started.append
    result = runner.run("prompt", timeout=1)
    assert started == [8123]
    assert result.state == "STALLED"  # no last-message file means no completed result
    assert result.exit_code == 0


def test_codex_nonzero_exit_and_timeout_are_truthful(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    class Nonzero:
        pid = 8124
        returncode = None
        def poll(self): return None
        def communicate(self, **kwargs): self.returncode = 7; return "", "failure"
        def wait(self): return self.returncode
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *args, **kwargs: Nonzero())
    monkeypatch.setattr(watcher_module, "discover_codex_launcher", lambda executable="codex": [sys.executable])
    result = CodexRunner(str(tmp_path), "codex").run("prompt", timeout=1)
    assert result.state == "BLOCKED" and result.exit_code == 7

    class TimeoutProcess:
        pid = 8125
        returncode = None
        calls = 0
        def poll(self): return None
        def communicate(self, **kwargs):
            self.calls += 1
            if self.calls == 1: raise watcher_module.subprocess.TimeoutExpired("cmd", 1)
            self.returncode = -9
            return "", "killed"
        def kill(self): pass
        def wait(self): return self.returncode
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *args, **kwargs: TimeoutProcess())
    result = CodexRunner(str(tmp_path), "codex").run("prompt", timeout=1)
    assert result.state == "STALLED" and result.timed_out is True and result.exit_code == -9


def test_result_submission_is_impossible_without_terminal_integer(tmp_path):
    class Runner:
        def run(self, prompt, timeout): return CodexResult("COMPLETED", "result", None, False)
    class Bridge:
        def __init__(self): self.submitted = []
        def submit_result(self, result): self.submitted.append(result)
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner())
    bridge = Bridge(); output = []
    watcher._execute_prompt(bridge, "prompt", 1, output.append)
    assert "CODEX_STALLED timeout=false exit=unknown" in output
    assert bridge.submitted == []


def test_completed_result_emits_real_exit_and_submits_after_terminal_state(tmp_path):
    class Runner:
        on_start = None
        def run(self, prompt, timeout):
            self.on_start(8126)
            return CodexResult("COMPLETED", "result", 0, False)
    class Bridge:
        def __init__(self): self.submitted = []
        def submit_result(self, result): self.submitted.append(result)
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner())
    bridge = Bridge(); output = []
    watcher._execute_prompt(bridge, "prompt", 1, output.append)
    assert "CODEX_STARTED pid=8126" in output
    assert "CODEX_COMPLETED exit=0" in output
    assert output.index("CODEX_COMPLETED exit=0") < output.index("RESULT_SENT_TO_ARCHITECT")
    assert bridge.submitted == ["result"]
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["in_flight"] is False and "in_flight_prompt_hash" not in saved
    assert saved["last_prompt_hash"] and saved["last_result_hash"]


def test_production_length_response_and_end_only_change_are_detected():
    from local_orchestrator_watcher import ArchitectPlaywright
    prefix = "P" * 12000
    baseline_text = prefix + "baseline-end"
    completed = prefix + "changed-end\n" + BEGIN + "\nlong prompt\n" + END + "\n" + COMPLETE
    class Page:
        def __init__(self): self.snapshots = iter([
            [{"id": "same", "text": baseline_text}],
            [{"id": "same", "text": completed}],
        ])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return False
        def locator(self, selector): raise AssertionError("long-response polling must remain atomic")
    bridge = ArchitectPlaywright(Page())
    observed = bridge.wait_for_new_response(bridge.assistant_baseline(), timeout=1)
    assert observed["state"] == "COMPLETED"
    assert extract_executor_prompt(observed["text"]) == "long prompt"


def test_machine_result_envelope_reconciliation_is_deterministic(tmp_path):
    path = tmp_path / "result.envelope.json"
    path.write_text(json.dumps(build_result_envelope("DISPATCH-1", "PASS", "PUB-1")), encoding="utf-8")
    advanced = []
    assert reconcile_executor_result(path, "PUB-old", lambda publication: publication == "PUB-1", advanced.append) == "POINTER_RECONCILED"
    assert advanced == ["PUB-1"]
    assert read_result_envelope(path)["resultType"] == "EXECUTOR_RESULT"


def test_child_running_is_not_reported_as_no_new_report(tmp_path):
    path = tmp_path / "running.json"
    assert not path.exists()
    assert "NO_NEW_REPORT" not in {"RUNNING"}


def test_missing_and_invalid_machine_result_fail_closed(tmp_path):
    with __import__("pytest").raises(RuntimeError, match="CHILD_RESULT_MISSING"):
        read_result_envelope(tmp_path / "missing.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    with __import__("pytest").raises(RuntimeError, match="CHILD_RESULT_INVALID"):
        read_result_envelope(invalid)


def test_non_success_machine_status_is_preserved_without_prose_parsing(tmp_path):
    for status in ("BLOCKED", "FAILED", "STOP"):
        path = tmp_path / f"{status}.json"
        path.write_text(json.dumps(build_result_envelope("D", status)), encoding="utf-8")
        assert reconcile_executor_result(path) == status


def test_stale_pointer_without_reconciler_is_explicit(tmp_path):
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(build_result_envelope("D", "PASS", "PUB-new")), encoding="utf-8")
    assert reconcile_executor_result(path, "PUB-old") == "POINTER_STALE"


def test_stale_unrecoverable_relay_is_retired_without_retry_and_restart_preserves_it(tmp_path):
    publication = "PUB-" + "7" * 32
    digest = "1" * 64
    key = f"{publication}:{digest}"
    state_path = tmp_path / "state.json"
    watcher = LocalWatcher(str(tmp_path), state_path, runner=object())
    watcher.state.update({"relay_publication_id": publication, "relay_content_sha256": digest, "in_flight_relay_key": key})
    assert watcher.retire_unrecoverable_relay(publication, digest) is True
    assert watcher.state["relay_recovery_state"] == "SUPERSEDED_UNRECOVERABLE"
    assert watcher.state["retired_relay_keys"][key]["resultRecovered"] is False
    assert "in_flight_relay_key" not in watcher.state

    class Source:
        def read_current(self): return {"publicationId": publication, "contentSha256": digest, "prompt": "must not rerun"}
    class Runner:
        def run(self, prompt, timeout): raise AssertionError("retired publication must not rerun")
    restarted = LocalWatcher(str(tmp_path), state_path, Runner())
    assert restarted.run_relay_once(Source(), None, emit=lambda _: None) == "IDLE"


def test_newer_relay_publication_remains_observable_after_old_retirement(tmp_path):
    old = "PUB-" + "7" * 32
    old_digest = "1" * 64
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state["in_flight_relay_key"] = f"{old}:{old_digest}"
    watcher.retire_unrecoverable_relay(old, old_digest)
    new = "PUB-" + "8" * 32
    class Source:
        def read_current(self): return {"publicationId": new, "contentSha256": "2" * 64, "prompt": "new task"}
    class Runner:
        def run(self, prompt, timeout): return CodexResult("STALLED", "", -1, True)
    restarted = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner())
    assert restarted.run_relay_once(Source(), None, emit=lambda _: None) == "RESULT_PENDING"
    assert restarted.state.get("in_flight_relay_key", "").startswith(new + ":")


def test_long_generation_in_progress_is_not_forwarded_and_unchanged_stays_idle():
    from local_orchestrator_watcher import ArchitectPlaywright
    long_text = "L" * 14000
    class Page:
        def __init__(self, generation, changed=True):
            self.generation = iter(generation)
            updated = long_text + "update" if changed else long_text
            self.snapshots = iter([[{"id": "long", "text": long_text}], [{"id": "long", "text": updated}], [{"id": "long", "text": updated}]])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script: return next(self.snapshots)
            return next(self.generation)
        def locator(self, selector): raise AssertionError("long-response polling must remain atomic")
    bridge = ArchitectPlaywright(Page([True, False]))
    baseline = bridge.assistant_baseline()
    assert bridge.wait_for_new_response(baseline, timeout=1)["state"] == "BLOCKED"
    unchanged = ArchitectPlaywright(Page([False], changed=False))
    assert unchanged.wait_for_new_response(unchanged.assistant_baseline(), timeout=0.01)["state"] == "NOT_YET"


def test_one_long_prompt_is_one_forward_and_no_duplicate_startup_poll_launch(tmp_path):
    long_prompt = "X" * 13000
    class Runner:
        def __init__(self): self.calls = []
        def run(self, prompt, timeout): self.calls.append(prompt); return CodexResult("COMPLETED", "ok", 0, False)
    runner = Runner(); watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner)
    first = watcher.forward(long_prompt)
    assert first.state == "COMPLETED"
    class Bridge:
        def latest_completed_executor_prompt(self): return ("long response", long_prompt)
    assert watcher.startup_candidate(Bridge(), scan_history=False) is None
    assert runner.calls == [long_prompt]


def test_launcher_selection_resolves_installed_codex_shim():
    from local_orchestrator_watcher import discover_codex_launcher
    launcher = discover_codex_launcher()
    assert launcher and launcher[-1].lower().endswith("codex.js")


def test_startup_baselines_current_response_without_replaying_it(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright

    class Messages:
        values = [f"historical\n{BEGIN}\nold\n{END}\n{COMPLETE}"]
        def count(self): return len(self.values)
        def nth(self, index): return type("Message", (), {"inner_text": lambda self: messages.values[index]})()
    messages = Messages()

    class Page:
        def locator(self, selector): return messages
        def get_by_role(self, role, **kwargs): return type("Role", (), {"count": lambda self: 0})()

    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    baseline = bridge.assistant_baseline()
    observed, _ = watcher.observe_response(bridge, baseline, timeout=0.01)
    assert observed["state"] == "NOT_YET"


def test_startup_scan_selects_newest_completed_executor_block(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Message:
        def __init__(self, text): self.text = text
        def inner_text(self): return self.text
        def get_attribute(self, name): return None
    class Messages:
        def __init__(self, values): self.values = [Message(value) for value in values]
        def count(self): return len(self.values)
        def nth(self, index): return self.values[index]
    old = f"old\n{BEGIN}\nolder\n{END}\n{COMPLETE}"
    pending = f"pending\n{BEGIN}\nnewest prompt\n{END}\n{COMPLETE}"
    newer_without_block = f"newer commentary\n{COMPLETE}"
    messages = Messages([old, pending, newer_without_block])
    class Page:
        def locator(self, selector): return messages
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    assert watcher.startup_candidate(bridge) == "newest prompt"


def test_startup_history_scan_loads_virtualized_pending_prompt(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Message:
        def __init__(self, text): self.text = text
        def inner_text(self): return self.text
        def get_attribute(self, name): return None
    class Messages:
        def __init__(self): self.values = [Message(f"newer\n{COMPLETE}")]
        def count(self): return len(self.values)
        def nth(self, index): return self.values[index]
    messages = Messages()
    class Page:
        def locator(self, selector): return messages
        def evaluate(self, script):
            if "scrollHeight" in script:
                messages.values.insert(0, Message(f"older pending\n{BEGIN}\nvirtualized prompt\n{END}\n{COMPLETE}"))
                return True
            return [{"id": None, "text": message.text} for message in messages.values]
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    assert bridge.assistant_count() == 1
    assert watcher.startup_candidate(bridge, scan_history=False) is None
    assert watcher.startup_candidate(bridge, scan_history=True) == "virtualized prompt"


def test_history_scan_is_bounded_and_does_not_launch_codex(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Messages:
        def count(self): return 0
    class Page:
        def __init__(self): self.evaluations = 0
        def locator(self, selector): return Messages()
        def evaluate(self, script):
            self.evaluations += 1
            return True
    page = Page()
    bridge = ArchitectPlaywright(page)
    assert bridge.load_older_history(max_steps=3, delay=0) == 3
    assert page.evaluations == 3


def test_scroll_selector_rejects_tiny_nearest_ancestor_and_prefers_active_scroll_root():
    tiny = {"tag": "div", "classSummary": "flex min-h-0 grow flex-col text-sm", "scrollTop": 0, "scrollHeight": 28590, "clientHeight": 28554}
    root = {"tag": "div", "classSummary": "group/scroll-root relative flex", "scrollTop": 26699.2, "scrollHeight": 28682, "clientHeight": 782}
    selected = choose_conversation_scroll_candidate([tiny, root])
    assert selected == root


def test_history_step_reports_upward_movement_and_new_virtualized_ids():
    from local_orchestrator_watcher import ArchitectPlaywright
    class Message:
        def __init__(self, message_id): self.message_id = message_id
        def inner_text(self): return "not exposed"
        def get_attribute(self, name): return self.message_id if name == "data-message-id" else None
    class Messages:
        def __init__(self): self.values = [Message("new")]
        def count(self): return len(self.values)
        def nth(self, index): return self.values[index]
    class Page:
        def __init__(self): self.calls = 0
        def locator(self, selector): return MessagesProxy(self)
        def evaluate(self, script):
            self.calls += 1
            if "scrollHeight" not in script:
                return [{"id": "new", "text": "not exposed"}]
            return {"found": True, "classSummary": "group/scroll-root", "scrollHeight": 28682, "clientHeight": 782,
                    "scrollTopBefore": 26699.2, "scrollTopAfter": 26034.5,
                    "idsBefore": ["new"], "idsAfter": ["old", "new"]}
    class MessagesProxy:
        def __init__(self, page): self.page = page
        def count(self): return 1
        def nth(self, index): return Message("new")
    page = Page(); bridge = ArchitectPlaywright(page); output = []
    assert bridge.load_older_history(max_steps=1, delay=0, emit=output.append) == 1
    assert page.calls == 2
    assert any("overflow=27900" in item for item in output)
    assert any("before=26699.2 after=26034.5 assistants=1->1" in item for item in output)


def test_adaptive_history_scan_stops_at_top_no_progress_and_safety_cap():
    from local_orchestrator_watcher import ArchitectPlaywright
    class EmptyMessages:
        def count(self): return 0
    class Page:
        def __init__(self, results): self.results = iter(results)
        def locator(self, selector): return EmptyMessages()
        def evaluate(self, script):
            if "scrollHeight" not in script:
                return []
            return next(self.results)
    top = ArchitectPlaywright(Page([{"found": False}]))
    top.load_older_history(delay=0)
    assert top.last_history_stop_reason == "TOP_REACHED"
    no_progress = ArchitectPlaywright(Page([{"found": True, "scrollHeight": 10000, "clientHeight": 700,
        "scrollTopBefore": 1000, "scrollTopAfter": 1000, "idsBefore": [], "idsAfter": []}]))
    no_progress.load_older_history(delay=0)
    assert no_progress.last_history_stop_reason == "NO_PROGRESS"
    moving = [{"found": True, "scrollHeight": 10000, "clientHeight": 700,
        "scrollTopBefore": n, "scrollTopAfter": n - 500, "idsBefore": [], "idsAfter": []} for n in (5000, 4500, 4000)]
    safety = ArchitectPlaywright(Page(moving))
    safety.load_older_history(max_steps=3, delay=0)
    assert safety.last_history_stop_reason == "SAFETY_CAP"


def test_atomic_assistant_snapshot_survives_virtualized_node_replacement():
    from local_orchestrator_watcher import ArchitectPlaywright
    class Page:
        def __init__(self): self.calls = 0
        def evaluate(self, script):
            self.calls += 1
            assert "querySelectorAll" in script and "isConnected" in script
            return ([{"id": "a", "text": "old"}] if self.calls == 1 else
                    [{"id": "b", "text": "current"}])
        def locator(self, selector):
            raise AssertionError("individual assistant locator access is forbidden")
    bridge = ArchitectPlaywright(Page())
    assert bridge._assistant_entries() == [{"id": "a", "text": "old"}]
    assert bridge._assistant_entries() == [{"id": "b", "text": "current"}]


def test_virtualized_snapshot_race_continues_and_finds_later_prompt(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Page:
        def __init__(self): self.snapshot_calls = 0
        def evaluate(self, script):
            if "scrollHeight" in script:
                return {"found": True, "classSummary": "group/scroll-root", "scrollHeight": 10000, "clientHeight": 700,
                        "scrollTopBefore": 5000, "scrollTopAfter": 4500, "idsBefore": ["a"], "idsAfter": ["b"]}
            self.snapshot_calls += 1
            if self.snapshot_calls < 3:
                return [{"id": "a", "text": "mounted window"}]
            return [{"id": "b", "text": f"later\n{BEGIN}\nlate prompt\n{END}\n{COMPLETE}"}]
        def locator(self, selector):
            raise AssertionError("individual assistant locator access is forbidden")
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    assert watcher.startup_candidate(bridge, scan_history=True, emit=lambda _: None) == "late prompt"


def test_startup_restores_bottom_and_discovers_prompt_arriving_during_history_scan(tmp_path):
    class Bridge:
        def __init__(self): self.submitted = []
        def assistant_baseline(self): return {"count": 1, "text_hash": "x", "entries": [{"id": "bottom", "text": "latest"}]}
        def assistant_count(self): return 1
        def latest_completed_executor_prompt(self):
            return ("arrived\nblock", "arrived prompt") if self.restored else None
        def load_older_history(self, emit=None, stop_when=None): return 2
        def restore_live_bottom(self): self.restored = True; return {"before": 0, "after": 1200}
        def submit_result(self, result): self.submitted.append(result)
        restored = False
    class Runner:
        def __init__(self): self.calls = []
        def run(self, prompt, timeout):
            self.calls.append(prompt)
            return CodexResult("COMPLETED", "result", 0, False)
    runner = Runner(); bridge = Bridge(); watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=runner)
    calls = {"count": 0}
    def observe(bridge, baseline, timeout):
        calls["count"] += 1
        raise KeyboardInterrupt
    watcher.observe_response = observe
    output = []
    try:
        watcher.run_forever(bridge, sleep_seconds=0, response_timeout=0, emit=output.append)
    except KeyboardInterrupt:
        pass
    assert runner.calls == ["arrived prompt"]
    assert bridge.submitted == ["result"]
    assert "LIVE_BOTTOM_RESTORE before=0 after=1200" in output
    assert "LIVE_BOTTOM_READY assistants=1" in output


def test_steady_state_polling_uses_atomic_snapshot_and_waits_for_completion():
    from local_orchestrator_watcher import ArchitectPlaywright
    complete = f"response\n{BEGIN}\nsteady prompt\n{END}\n{COMPLETE}"
    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "a", "text": "baseline"}],
                [{"id": "b", "text": "partial"}],
                [{"id": "b", "text": complete}],
            ])
            self.generation = iter([True, False])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return next(self.generation)
        def locator(self, selector):
            raise AssertionError("steady-state assistant polling must not use Locator operations")
    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_baseline()
    observed = bridge.wait_for_new_response(baseline, timeout=1)
    assert observed["state"] == "COMPLETED"
    assert extract_executor_prompt(observed["text"]) == "steady prompt"


def test_unchanged_atomic_snapshot_remains_not_yet_without_locator_count():
    from local_orchestrator_watcher import ArchitectPlaywright
    class Page:
        def __init__(self): self.calls = 0
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                self.calls += 1
                return [{"id": "same", "text": "unchanged"}]
            return False
        def locator(self, selector):
            raise AssertionError("Locator.count must not be used by steady-state polling")
    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_baseline()
    assert bridge.wait_for_new_response(baseline, timeout=0.01)["state"] == "NOT_YET"


def test_steady_state_call_graph_has_no_locator_count_path():
    import inspect
    from local_orchestrator_watcher import ArchitectPlaywright
    generation_source = inspect.getsource(ArchitectPlaywright.generation_visible)
    wait_source = inspect.getsource(ArchitectPlaywright.wait_for_new_response)
    assert ".count(" not in generation_source
    assert ".locator(" not in generation_source
    assert ".count(" not in wait_source
    assert ".locator(" not in wait_source


def test_startup_scan_does_not_hide_pending_block_behind_newer_non_executor_response(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Message:
        def __init__(self, text): self.text = text
        def inner_text(self): return self.text
        def get_attribute(self, name): return None
    class Messages:
        def __init__(self, values): self.values = [Message(value) for value in values]
        def count(self): return len(self.values)
        def nth(self, index): return self.values[index]
    messages = Messages([f"ready\n{BEGIN}\npending\n{END}\n{COMPLETE}", f"later answer\n{COMPLETE}"])
    class Page:
        def locator(self, selector): return messages
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    assert watcher.startup_candidate(bridge) == "pending"


def test_startup_hash_guards_skip_forwarded_and_in_flight_prompt(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright, normalize_prompt
    class Message:
        def inner_text(self): return f"ready\n{BEGIN}\n guarded prompt \n{END}\n{COMPLETE}"
        def get_attribute(self, name): return None
    class Messages:
        def count(self): return 1
        def nth(self, index): return Message()
    class Page:
        def locator(self, selector): return Messages()
    bridge = ArchitectPlaywright(Page())
    prompt_hash = __import__("hashlib").sha256(normalize_prompt("guarded prompt").encode()).hexdigest()
    for state in ({"last_prompt_hash": prompt_hash}, {"in_flight_prompt_hash": prompt_hash}):
        watcher = LocalWatcher(str(tmp_path), tmp_path / (str(len(state)) + ".json"), runner=type("Runner", (), {})())
        watcher.state.update(state)
        assert watcher.startup_candidate(bridge) is None


def test_fresh_startup_prompt_is_one_launch_candidate_without_live_codex(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Message:
        def inner_text(self): return f"ready\n{BEGIN}\nfresh\n{END}\n{COMPLETE}"
        def get_attribute(self, name): return None
    class Messages:
        def count(self): return 1
        def nth(self, index): return Message()
    class Page:
        def locator(self, selector): return Messages()
    class FakeRunner:
        calls = []
        def run(self, prompt, timeout):
            self.calls.append(prompt)
            return CodexResult("COMPLETED", "result", 0, False)
    runner = FakeRunner()
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=runner)
    bridge = ArchitectPlaywright(Page())
    candidate = watcher.startup_candidate(bridge)
    assert candidate == "fresh"
    assert runner.calls == []
    result = watcher.forward(candidate)
    assert result.state == "COMPLETED"
    assert runner.calls == ["fresh"]
    assert watcher.startup_candidate(bridge) is None


def test_new_assistant_node_is_detected_even_when_it_is_not_last_matching_node(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright

    class Message:
        def __init__(self, value, message_id): self.value, self.message_id = value, message_id
        def inner_text(self): return self.value
        def get_attribute(self, name): return self.message_id if name == "data-message-id" else None
    class Messages:
        def __init__(self): self.values = [Message("first", "a"), Message("trailing", "b")]
        def count(self): return len(self.values)
        def nth(self, index): return self.values[index]
    messages = Messages()
    class Page:
        def locator(self, selector): return messages
        def get_by_role(self, role, **kwargs): return type("Role", (), {"count": lambda self: 0})()
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    baseline = bridge.assistant_baseline()
    messages.values.insert(1, Message(f"new\n{BEGIN}\nprompt\n{END}\n{COMPLETE}", "new"))
    observed, _ = watcher.observe_response(bridge, baseline, timeout=1)
    assert observed["state"] == "COMPLETED"
    assert observed["prompt"] == "prompt"


def test_idle_loop_observes_external_completed_response_and_forwards_once(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright

    class Messages:
        def __init__(self): self.values = ["old"]
        def count(self): return len(self.values)
        def nth(self, index): return type("Message", (), {"inner_text": lambda self: messages.values[index]})()
    messages = Messages()

    class Role:
        def count(self): return 0
    class Page:
        def __init__(self): self.result = None
        def locator(self, selector): return messages
        def get_by_role(self, role, **kwargs):
            if role == "button": return Role()
            return Role()
        def submit_result(self, value): self.result = value

    page = Page(); bridge = ArchitectPlaywright(page)
    class FakeRunner:
        def run(self, prompt, timeout):
            assert prompt == "new executor prompt"
            return CodexResult("COMPLETED", "result", 0, False)
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", FakeRunner())
    baseline = bridge.assistant_baseline()
    messages.values.append(f"new\n{BEGIN}\nnew executor prompt\n{END}\n{COMPLETE}")
    observed, next_baseline = watcher.observe_response(bridge, baseline, timeout=1)
    assert observed["state"] == "COMPLETED"
    assert observed["prompt"] == "new executor prompt"
    assert next_baseline["count"] == 2


def test_completed_response_without_executor_block_advances_baseline(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Messages:
        values = ["old"]
        def count(self): return len(self.values)
        def nth(self, index): return type("Message", (), {"inner_text": lambda self: messages.values[index]})()
    messages = Messages()
    class Page:
        def locator(self, selector): return messages
        def get_by_role(self, role, **kwargs): return type("Role", (), {"count": lambda self: 0})()
    bridge = ArchitectPlaywright(Page())
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    baseline = bridge.assistant_baseline()
    messages.values.append(f"ordinary response\n{COMPLETE}")
    observed, next_baseline = watcher.observe_response(bridge, baseline, timeout=1)
    assert observed["state"] == "COMPLETED"
    assert "prompt" not in observed
    assert next_baseline["count"] == 2


def test_persistent_entrypoint_uses_idle_loop_and_status_transitions(tmp_path, monkeypatch):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Bridge:
        def assistant_baseline(self): return {"count": 0, "text_hash": "0"}
        def latest_completed_executor_prompt(self): return None
        def assistant_count(self): return 0
        def load_older_history(self, emit=None, stop_when=None): return 0
        def restore_live_bottom(self): return {"before": 0, "after": 0}
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    calls = {"count": 0}
    def observe(bridge, baseline, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"state": "NOT_YET", "text": ""}, baseline
        raise KeyboardInterrupt
    monkeypatch.setattr(watcher, "observe_response", observe)
    output = []
    try:
        watcher.run_forever(Bridge(), sleep_seconds=0, response_timeout=0, emit=output.append)
    except KeyboardInterrupt:
        pass
    assert calls["count"] == 2
    assert output[:7] == ["ARCHITECT_CONNECTED", "STARTUP_SCAN_MOUNTED count=0", "STARTUP_HISTORY_SCAN", "STARTUP_HISTORY_EXHAUSTED reason=SAFETY_CAP steps=0", "LIVE_BOTTOM_RESTORE before=0 after=0", "LIVE_BOTTOM_READY assistants=0", "STATE=IDLE"]


def test_production_startup_wires_history_loader_and_reports_exhaustion(tmp_path, monkeypatch):
    class Bridge:
        def assistant_baseline(self): return {"count": 2, "text_hash": "0", "entries": [{"id": "a"}, {"id": "b"}]}
        def assistant_count(self): return 2
        def latest_completed_executor_prompt(self): return None
        def load_older_history(self, emit=None, stop_when=None):
            assert emit is not None
            emit("HISTORY_SCROLL_CONTAINER overflow=1000 scrollTop=500")
            emit("HISTORY_SCROLL_STEP before=1000 after=500 assistants=2->2")
            return 1
        def restore_live_bottom(self): return {"before": 500, "after": 1000}
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=type("Runner", (), {})())
    calls = {"observe": 0}
    def observe(bridge, baseline, timeout):
        calls["observe"] += 1
        raise KeyboardInterrupt
    monkeypatch.setattr(watcher, "observe_response", observe)
    output = []
    try:
        watcher.run_forever(Bridge(), sleep_seconds=0, response_timeout=0, emit=output.append)
    except KeyboardInterrupt:
        pass
    assert calls["observe"] == 1
    assert output == [
        "ARCHITECT_CONNECTED",
        "STARTUP_SCAN_MOUNTED count=2",
        "STARTUP_HISTORY_SCAN",
        "HISTORY_SCROLL_CONTAINER overflow=1000 scrollTop=500",
        "HISTORY_SCROLL_STEP before=1000 after=500 assistants=2->2",
        "STARTUP_HISTORY_EXHAUSTED reason=SAFETY_CAP steps=1",
        "LIVE_BOTTOM_RESTORE before=500 after=1000",
        "LIVE_BOTTOM_READY assistants=2",
        "STATE=IDLE",
    ]
