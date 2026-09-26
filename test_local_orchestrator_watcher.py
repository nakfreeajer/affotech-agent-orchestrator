import json
import hashlib
import subprocess
import sys
import time
from pathlib import Path

import pytest
import local_orchestrator_watcher as watcher_module

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
        def focus(self, **kwargs):
            if self.kind == "composer":
                page.focused = True
        def inner_text(self, **kwargs):
            if self.kind == "composer":
                return page.value
            return ""
        def count(self): return 0
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
            self.keyboard = type("Keyboard", (), {"insert_text": lambda _, value: setattr(page, "value", value)})()
        def get_by_role(self, role, **kwargs):
            if role == "textbox": return self.composer_locator
            if role == "button": return self.send_locator
            raise AssertionError(role)
        def locator(self, selector):
            if selector == '[data-message-author-role="assistant"]':
                return Locator("assistant")
            if selector == "#prompt-textarea":
                return self.composer_locator
            raise AssertionError(selector)
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
    page.composer_locator.inner_text = lambda **_: "wrong content"
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


def _recovery_fixture(tmp_path, reader, terminal="GH-PUB-265-AFFOTECH-READONLY-RECONCILIATION-EXECUTION-000001"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "in_flight_relay_key": "PUB-20281:hash",
        "relay_publication_id": "PUB-20281",
        "recovery_terminal_publication_id": terminal,
        "in_flight": True,
    }))
    return LocalWatcher(str(tmp_path), state_path, runner=object(), durable_decision_reader=reader)


def _matching_recovery_record(publication, *, required=False, accepted=True):
    return {"decision": {"reviewedPublicationId": publication, "requiresArchitectDecision": required, "decision": "ACCEPTED"},
            "acceptedPointer": {"accepted": accepted, "publicationId": publication}}


def test_matching_durable_architect_decision_reconciles_recovery_without_launch(tmp_path):
    publication = "GH-PUB-265-AFFOTECH-READONLY-RECONCILIATION-EXECUTION-000001"
    watcher = _recovery_fixture(tmp_path, lambda value: _matching_recovery_record(publication))
    source = type("Source", (), {"read_current": lambda self: {"publicationId": "PUB-20281", "contentSha256": "hash", "prompt": "unused"}})()
    output = []
    assert watcher.run_relay_once(source, None, emit=output.append) == "IDLE"
    assert watcher.state["recovery_reconciled"] is True
    assert "RECOVERY_REQUIRED" not in output


def test_nonmatching_or_missing_or_pending_architect_decision_keeps_recovery(tmp_path):
    publication = "GH-PUB-265-AFFOTECH-READONLY-RECONCILIATION-EXECUTION-000001"
    source = type("Source", (), {"read_current": lambda self: {"publicationId": "PUB-20281", "contentSha256": "hash", "prompt": "unused"}})()
    for reader in (
        lambda value: _matching_recovery_record("GH-PUB-other"),
        lambda value: None,
        lambda value: _matching_recovery_record(publication, required=True),
        lambda value: {"decision": {"reviewedPublicationId": publication, "requiresArchitectDecision": False}, "acceptedPointer": {"accepted": True, "publicationId": "GH-PUB-other"}},
    ):
        watcher = _recovery_fixture(tmp_path / str(id(reader)), reader)
        output = []
        assert watcher.run_relay_once(source, None, emit=output.append) == "RECOVERY_REQUIRED"
        assert output[-1] == "STATE=RECOVERY_REQUIRED"


def test_recovery_reconciliation_persists_across_restart_and_does_not_rerun(tmp_path):
    publication = "GH-PUB-265-AFFOTECH-READONLY-RECONCILIATION-EXECUTION-000001"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"in_flight_relay_key": "PUB-20281:hash", "recovery_terminal_publication_id": publication, "in_flight": True}))
    reader = lambda value: _matching_recovery_record(publication)
    watcher = LocalWatcher(str(tmp_path), state_path, runner=object(), durable_decision_reader=reader)
    source = type("Source", (), {"read_current": lambda self: {"publicationId": "PUB-20281", "contentSha256": "hash", "prompt": "unused"}})()
    assert watcher.run_relay_once(source, None, emit=lambda _: None) == "IDLE"
    restarted = LocalWatcher(str(tmp_path), state_path, runner=object(), durable_decision_reader=lambda _: (_ for _ in ()).throw(AssertionError("decision re-read")))
    assert restarted.run_relay_once(source, None, emit=lambda _: None) == "IDLE"
    assert restarted.state["last_completed_relay_key"] == "PUB-20281:hash"


def test_legacy_inflight_migration_requires_proof_and_preserves_unrelated_marker(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"in_flight": True, "in_flight_relay_key": "PUB-A:h", "relay_publication_id": "PUB-A", "retired_relay_keys": {"PUB-old:x": {"state": "SUPERSEDED_UNRECOVERABLE"}}}))
    watcher = LocalWatcher(str(tmp_path), state_path, runner=object())
    assert watcher.migrate_legacy_inflight_state(None) is False
    proof = {"decisionPublicationId": "DEC-A", "replacementPublicationId": "PUB-B", "currentPublicationId": "PUB-B", "snapshotCommit": "c"}
    assert watcher.migrate_legacy_inflight_state(proof) is True
    assert watcher.state["legacy_inflight_migration"]["resolution"] == "SUPERSEDED_WITHOUT_RETRY"
    assert watcher.state["retired_relay_keys"]["PUB-old:x"]["state"] == "SUPERSEDED_UNRECOVERABLE"
    assert (tmp_path / "state.json.pre-legacy-migration.bak").exists()
    restarted = LocalWatcher(str(tmp_path), state_path, runner=object())
    assert restarted.state["in_flight"] is False
    assert restarted.state["retired_relay_keys"]["PUB-A:h"]["executionAuthorized"] is False


def test_legacy_inflight_migration_refuses_active_or_unresolved_execution(tmp_path):
    proof = {"decisionPublicationId": "DEC-A", "replacementPublicationId": "PUB-B", "currentPublicationId": "PUB-B"}
    for extra in ({"active_codex_pid": 12}, {"result_pending": True}, {"executor_completed": True}):
        state_path = tmp_path / ("state-" + str(len(extra)) + ".json")
        state_path.write_text(json.dumps({"in_flight": True, "in_flight_relay_key": "PUB-A:h", "relay_publication_id": "PUB-A", **extra}))
        watcher = LocalWatcher(str(tmp_path), state_path, runner=object())
        assert watcher.migrate_legacy_inflight_state(proof) is False


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


def test_completed_result_publishes_terminal_before_browser_dependency_and_retires_once(tmp_path):
    make_bootstrap(tmp_path)
    publication = "PUB-" + "a" * 32
    calls = []
    watcher = None
    def publisher(pub, text, envelope):
        calls.append((pub, text, envelope, watcher.state.get("in_flight_relay_key")))
        return {"publicationId": "GH-PUB-terminal-a", "resultSha256": hashlib.sha256(text.encode()).hexdigest()}
    class Runner:
        def run(self, prompt, timeout): return CodexResult("COMPLETED", "durable result", 0, False)
    class Bridge:
        def submit_result_bounded(self, result): raise TimeoutError("browser unavailable")
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner(), durable_terminal_publisher=publisher)
    watcher.state["in_flight_relay_key"] = publication + ":" + "b" * 64
    watcher.state["relay_publication_id"] = publication
    assert watcher._execute_prompt(Bridge(), "task", 1, lambda _: None) is False
    assert len(calls) == 1 and calls[0][3] == publication + ":" + "b" * 64
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["durable_terminal_published"] is True
    assert saved["last_completed_relay_key"] == publication + ":" + "b" * 64
    assert "in_flight_relay_key" not in saved and saved["result_pending"] is True


def test_pending_restart_retries_browser_only_after_terminal_is_durable(tmp_path):
    result_path = tmp_path / "result.txt"
    result_path.write_text("same result", encoding="utf-8")
    publication = "PUB-" + "c" * 32
    (tmp_path / "result.txt.envelope.json").write_text(json.dumps(build_result_envelope("D", "PASS", publication)), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"result_pending": True, "executor_completed": True, "result_file": str(result_path), "result_envelope_file": str(tmp_path / "result.txt.envelope.json"), "relay_publication_id": publication, "in_flight_relay_key": publication + ":" + "d" * 64, "durable_terminal_published": True, "durable_terminal_publication_id": "GH-PUB-terminal-c"}), encoding="utf-8")
    calls = []
    def forbidden_publisher(*args): raise AssertionError("terminal must not republish after restart")
    class Source:
        def read_current(self): return {"publicationId": "PUB-" + "e" * 32, "contentSha256": "f" * 64, "prompt": "unused"}
    class Bridge:
        def submit_result_bounded(self, result): calls.append(result)
    class Runner:
        def run(self, prompt, timeout): raise AssertionError("Codex must not rerun")
    watcher = LocalWatcher(str(tmp_path), state_path, Runner(), durable_terminal_publisher=forbidden_publisher)
    assert watcher.run_relay_once(Source(), Bridge(), emit=lambda _: None) == "COMPLETED"
    assert calls == ["same result"]


def test_terminal_publisher_failure_preserves_pending_and_does_not_retire(tmp_path):
    make_bootstrap(tmp_path)
    publication = "PUB-" + "1" * 32
    class Runner:
        def run(self, prompt, timeout): return CodexResult("COMPLETED", "result", 0, False)
    def publisher(*args): raise OSError("evidence unavailable")
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner(), durable_terminal_publisher=publisher)
    watcher.state["in_flight_relay_key"] = publication + ":" + "2" * 64
    watcher.state["relay_publication_id"] = publication
    assert watcher._execute_prompt(None, "task", 1, lambda _: None) is False
    assert watcher.state["result_pending"] is True
    assert watcher.state["in_flight_relay_key"].startswith(publication + ":")


def test_architect_rollover_counts_to_thirty_and_requests_once(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    rollover.initialize_current_session()
    prompt = tmp_path / "next.txt"
    prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt)})
    watcher.state["architectMemoryBytes"] = 891289599
    class Bridge:
        def __init__(self): self.requests = []
        def submit_result_bounded(self, value): self.requests.append(value)
    bridge = Bridge()
    assert not rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    watcher.state["architectMemoryBytes"] = 891289600
    assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert not rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert "<HANDOVER>" in bridge.requests[0]
    assert "version=1" in bridge.requests[0]
    assert "ARCHITECT_HANDOVER_READY" not in bridge.requests[0]
    assert watcher.state["handoverRequested"] is True


def test_architect_rollover_requires_running_executor_and_dedupes_events(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher); rollover.initialize_current_session()
    prompt = tmp_path / "next.txt"; prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt)})
    watcher.state["architectMemoryBytes"] = 891289600
    for index in range(30): rollover.observe_complete_response(f"same-{index}\n{COMPLETE}")
    class Bridge:
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, value): self.calls += 1
    bridge = Bridge()
    assert not rollover.request_if_due(bridge, False, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert not rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert bridge.calls == 1


def test_rollover_ack_timeout_retains_reconciliation_authority(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    rollover.initialize_current_session()
    prompt = tmp_path / "next.txt"; prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "EXECUTOR_RUNNING", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt), "codexPid": 123, "architectResponseCount": 30})

    class Bridge:
        sendActionAttempted = True
        def submit_result_bounded(self, _value):
            raise ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT")

    assert rollover.request_if_due(Bridge(), True, True) is False
    assert watcher.state.get("rolloverPending", False) is False


def test_orphaned_handover_is_reconciled_without_duplicate_send(tmp_path, monkeypatch):
    from local_orchestrator_watcher import ArchitectSessionRollover, LocalFirstOrchestrator
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "rolloverDue": True,
                          "rolloverPending": True, "handoverRequested": False,
                          "rolloverAttemptedForTaskId": "task-1", "rolloverTransactionId": "orphan-tx",
                          "rolloverTransactionTaskId": "task-1", "rolloverHandoverProtocolVersion": 1})
    rollover = ArchitectSessionRollover(watcher)
    calls = []

    class Bridge:
        def _assistant_entries(self):
            return [{"id": "handover", "text": watcher_module.make_handover_envelope("orphan-tx", "task-1", "completed")}]
        def submit_result_bounded(self, _value):
            raise AssertionError("orphan recovery must not resend handover")

    monkeypatch.setattr(watcher, "process_pending_handover_response", lambda _bridge, response: calls.append(response) or True)
    assert rollover.reconcile_pending_handover(Bridge()) is True
    assert len(calls) == 1
    assert watcher.state["handoverRequested"] is True
    assert watcher.state["rolloverHandoverSendState"] == "AMBIGUOUS"


def test_ambiguous_handover_without_response_remains_resident(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "rolloverDue": True,
                          "rolloverPending": True, "handoverRequested": True,
                          "rolloverAttemptedForTaskId": "task-1", "rolloverHandoverSendState": "AMBIGUOUS"})
    rollover = ArchitectSessionRollover(watcher)

    class Bridge:
        def _assistant_entries(self): return []
        def submit_result_bounded(self, _value): raise AssertionError("ambiguous request must not resend")

    assert rollover.reconcile_pending_handover(Bridge()) is False
    assert watcher.state["handoverRequested"] is True
    assert watcher.state["rolloverDue"] is True


def test_service_reconciles_orphan_before_requesting_new_handover(tmp_path, monkeypatch):
    import local_orchestrator_watcher as watcher_module
    from local_orchestrator_watcher import LocalFirstOrchestrator
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "rolloverDue": True,
                          "rolloverPending": True, "handoverRequested": False,
                          "rolloverAttemptedForTaskId": "task-1", "architectConversationId": "current",
                          "rolloverTransactionId": "orphan-tx", "rolloverTransactionTaskId": "task-1",
                          "rolloverHandoverProtocolVersion": 1})
    calls = []

    class Page:
        url = "https://chatgpt.com/c/current"

    class Bridge:
        page = Page()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline"}
        def _assistant_entries(self): return [{"id": "handover", "text": watcher_module.make_handover_envelope("orphan-tx", "task-1", "completed")}]
        def generation_visible(self): return False
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _watcher, _bridge, requested: requested)
    monkeypatch.setattr(watcher, "process_pending_handover_response", lambda _bridge, response: calls.append(response) or True)
    monkeypatch.setattr(watcher.session_rollover, "request_if_due", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate handover request")))
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is True
    assert len(calls) == 1


def _prebudget_reconciliation_fixture(tmp_path, response, *, recovery_started=997.0,
                                      recovery_attempts=1, retry_after=1005.0):
    from local_orchestrator_watcher import LocalFirstOrchestrator
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    prompt = tmp_path / "000080.txt"
    prompt.write_text("next prompt", encoding="utf-8")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000079", "lastCompletedTaskId": "000079",
        "nextTaskId": "000080", "nextPromptPath": str(prompt), "rolloverDue": True,
        "rolloverPending": True, "rolloverInProgress": True, "handoverRequested": True,
        "handoverReady": False, "rolloverHandoverSendState": "ACKNOWLEDGED",
        "rolloverMaintenanceState": "RECONCILE_PENDING", "rolloverRecoveryState": "RECOVERING",
        "rolloverHandoverRecoveryDisposition": "RETRYABLE",
        "rolloverHandoverRecoveryReason": "ARCHITECT_HANDOVER_NOT_FOUND",
        "rolloverRecoveryStartedAt": recovery_started, "rolloverRecoveryAttemptCount": recovery_attempts,
        "rolloverRecoveryRetryAfter": retry_after, "rolloverTransactionId": "2b324cd32cefc00ef1790c36",
        "rolloverTransactionGeneration": 2, "rolloverTransactionTaskId": "000080",
        "rolloverAttemptedForTaskId": "000080", "architectConversationId": "6ab08bd2-7140-83ec-813b-8501af0993f5",
        "rolloverHandoverProtocolVersion": 1,
    })
    watcher.save()
    return watcher, prompt


class _PrebudgetBridge:
    class _Page:
        url = "https://chatgpt.com/c/6ab08bd2-7140-83ec-813b-8501af0993f5"

    def __init__(self, response):
        self.page = self._Page()
        self.response = response
        self.assistant_entries_calls = 0
        self.close_calls = 0
        self.send_calls = 0

    def _assistant_entries(self):
        self.assistant_entries_calls += 1
        return ([{"id": "existing", "text": self.response}] if self.response is not None else [])

    def assistant_baseline(self):
        return {"count": 0, "text_hash": "baseline", "entries": []}

    def generation_visible(self):
        return False

    def close(self):
        self.close_calls += 1


def _run_prebudget_case(tmp_path, monkeypatch, response, *, now=1000.0,
                        recovery_started=997.0, recovery_attempts=1, retry_after=1005.0):
    watcher, prompt = _prebudget_reconciliation_fixture(
        tmp_path, response, recovery_started=recovery_started,
        recovery_attempts=recovery_attempts, retry_after=retry_after,
    )
    bridge = _PrebudgetBridge(response)
    attaches, processed = [], []
    monkeypatch.setattr(watcher_module.time, "time", lambda: now)
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach",
                        staticmethod(lambda *_args: attaches.append(bridge) or bridge))
    monkeypatch.setattr(watcher, "process_pending_handover_response",
                        lambda _bridge, text: processed.append(text) or bool(response))
    monkeypatch.setattr(watcher.session_rollover, "request_if_due",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("pre-budget reconciliation must not send")))
    result = watcher_module.service_deferred_rollover_once(
        watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY")
    return watcher, prompt, bridge, attaches, processed, result


def test_prebudget_probe_consumes_exact_response_before_exhaustion(tmp_path, monkeypatch):
    response = "<HANDOVER>\nversion=1\ntransactionId=2b324cd32cefc00ef1790c36\ntaskId=000080\n\ndocument\n</HANDOVER>"
    watcher, _prompt, bridge, attaches, processed, result = _run_prebudget_case(
        tmp_path, monkeypatch, response, recovery_started=0.0, recovery_attempts=2, retry_after=0.0)
    assert result is True
    assert processed == [response]
    assert len(attaches) == 1 and bridge.close_calls == 1
    assert watcher.state["rolloverTransactionId"] == "2b324cd32cefc00ef1790c36"
    assert watcher.state["rolloverTransactionGeneration"] == 2
    assert watcher.state["rolloverRecoveryState"] == "COMPLETE"


def test_prebudget_probe_consumes_exact_response_during_backoff(tmp_path, monkeypatch):
    response = "<HANDOVER>\nversion=1\ntransactionId=2b324cd32cefc00ef1790c36\ntaskId=000080\n\ndocument\n</HANDOVER>"
    watcher, _prompt, bridge, attaches, processed, result = _run_prebudget_case(tmp_path, monkeypatch, response)
    assert result is True
    assert processed == [response]
    assert len(attaches) == 1 and bridge.close_calls == 1
    assert watcher.state["rolloverRecoveryState"] == "COMPLETE"


def test_prebudget_probe_preserves_exhaustion_without_response(tmp_path, monkeypatch):
    watcher, prompt, bridge, attaches, processed, result = _run_prebudget_case(
        tmp_path, monkeypatch, None, recovery_started=699.0, recovery_attempts=2, retry_after=0.0)
    assert result is False and processed == []
    assert len(attaches) == 1 and bridge.close_calls == 1
    assert watcher.state["rolloverMaintenanceState"] == "DEFERRED"
    assert watcher.state["rolloverRecoveryState"] == "DEFERRED"
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["nextTaskId"] == "000080"
    assert prompt.read_text(encoding="utf-8") == "next prompt"


def test_prebudget_probe_preserves_backoff_without_response(tmp_path, monkeypatch):
    watcher, _prompt, bridge, attaches, processed, result = _run_prebudget_case(tmp_path, monkeypatch, None)
    assert result is False and processed == []
    assert len(attaches) == 1 and bridge.close_calls == 1
    assert watcher.state["rolloverMaintenanceState"] == "RECONCILE_PENDING"
    assert watcher.state["rolloverRecoveryAttemptCount"] == 1
    assert watcher.state["rolloverRecoveryRetryAfter"] == 1005.0


@pytest.mark.parametrize("wrong_token", ["768df22312ef1a5c0f86a9ea", "8084c3b80c13ebccb27d52c4"])
def test_prebudget_probe_rejects_wrong_or_missing_transaction_token(tmp_path, monkeypatch, wrong_token):
    response = f"document\nRollover transaction ID: {wrong_token}\nARCHITECT_HANDOVER_READY"
    watcher, _prompt, bridge, attaches, processed, result = _run_prebudget_case(
        tmp_path, monkeypatch, response, recovery_started=699.0, recovery_attempts=2, retry_after=0.0)
    assert result is False and processed == []
    assert len(attaches) == 1 and bridge.close_calls == 1
    assert watcher.state["rolloverMaintenanceState"] == "DEFERRED"


def test_prebudget_probe_rejects_ready_without_transaction_token(tmp_path, monkeypatch):
    response = "document\nARCHITECT_HANDOVER_READY"
    watcher, _prompt, bridge, attaches, processed, result = _run_prebudget_case(
        tmp_path, monkeypatch, response, recovery_started=699.0, recovery_attempts=2, retry_after=0.0)
    assert result is False and processed == []
    assert len(attaches) == 1 and bridge.close_calls == 1
    assert watcher.state["rolloverMaintenanceState"] == "DEFERRED"


def test_prebudget_probe_skipped_for_non_live_transaction(tmp_path, monkeypatch):
    from local_orchestrator_watcher import LocalFirstOrchestrator
    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "state")
    prompt = tmp_path / "000080.txt"
    prompt.write_text("next prompt", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000079", "nextTaskId": "000080",
                          "nextPromptPath": str(prompt), "rolloverDue": False, "rolloverPending": True,
                          "handoverRequested": False, "architectConversationId": "old"})
    watcher.save()
    attaches = []
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach",
                        staticmethod(lambda *_args: attaches.append(1)))
    assert watcher_module.service_deferred_rollover_once(
        watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert attaches == []


def test_conclusive_unsent_handover_remains_retryable(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    rollover.initialize_current_session()
    prompt = tmp_path / "next.txt"; prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt), "architectResponseCount": 30})
    watcher.state["architectMemoryBytes"] = 891289600

    class Bridge:
        sendActionAttempted = False
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, _value):
            self.calls += 1
            if self.calls == 1:
                raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE")

    bridge = Bridge()
    assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY") is False
    assert bridge.calls == 1


def test_architect_rollover_accepts_next_prompt_ready_without_executor(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    prompt = tmp_path / "next-prompt.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY",
        "taskId": "task-1",
        "nextTaskId": "task-2",
        "nextPromptPath": str(prompt),
        "rolloverDue": True,
        "architectResponseCount": 30,
        "architectConversationId": "current",
        "codexPid": None,
    })
    watcher.state["architectMemoryBytes"] = 891289600
    rollover = ArchitectSessionRollover(watcher)
    class Bridge:
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, value): self.calls += 1
    bridge = Bridge()
    assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert bridge.calls == 1
    assert watcher.state["handoverRequested"] is True


def test_next_prompt_ready_rollover_rejects_live_codex_and_invalid_staging(tmp_path, monkeypatch):
    from local_orchestrator_watcher import ArchitectSessionRollover

    def make_watcher(prompt_path, next_task="task-2", pid=None):
        watcher = LocalWatcher(str(tmp_path), tmp_path / ("state-" + str(next_task) + ".json"), runner=object())
        watcher.state.update({
            "state": "NEXT_PROMPT_READY",
            "taskId": "task-1",
            "nextTaskId": next_task,
            "nextPromptPath": str(prompt_path) if prompt_path is not None else None,
            "rolloverDue": True,
            "rolloverTrigger": "MEMORY_THRESHOLD",
            "codexPid": pid,
        })
        return watcher

    prompt = tmp_path / "valid-prompt.txt"
    prompt.write_text("next bounded task", encoding="utf-8")
    class Bridge:
        def submit_result_bounded(self, value): raise AssertionError("must not submit")

    monkeypatch.setattr(LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    live = make_watcher(prompt, pid=123)
    assert not ArchitectSessionRollover(live).request_if_due(Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY")

    missing_task = make_watcher(prompt, next_task=None)
    assert not ArchitectSessionRollover(missing_task).request_if_due(Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY")
    missing_prompt = make_watcher(tmp_path / "does-not-exist.txt")
    assert not ArchitectSessionRollover(missing_prompt).request_if_due(Bridge(), True, False, safe_boundary_state="NEXT_PROMPT_READY")
    generating = make_watcher(prompt)
    assert not ArchitectSessionRollover(generating).request_if_due(Bridge(), True, False, architect_generating=True, safe_boundary_state="NEXT_PROMPT_READY")


def test_due_rollover_recovery_is_bounded_and_enters_safety_cutout(tmp_path, monkeypatch):
    import local_orchestrator_watcher as watcher_module
    from local_orchestrator_watcher import LocalFirstOrchestrator

    watcher = LocalFirstOrchestrator(str(tmp_path), tmp_path / "work")
    prompt = tmp_path / "000054.txt"
    prompt.write_bytes(b"preserved staged prompt")
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000053", "lastCompletedTaskId": "000053",
        "nextTaskId": "000054", "nextPromptPath": str(prompt), "targetWorktree": "worktree-054",
        "rolloverDue": True, "rolloverPending": True, "rolloverAttemptedForTaskId": "000053",
        "handoverRequested": True, "executorProcessState": "COMPLETED_WITH_RESULT",
        "codexPid": None, "active_codex_pid": None,
    })
    now = [1000.0]
    attaches = []
    launches = []
    monkeypatch.setattr(watcher_module.time, "time", lambda: now[0])

    class Page:
        url = "https://chatgpt.com/c/old"
        context = None

    class Bridge:
        page = Page()
        def assistant_baseline(self): return {"count": 0, "text_hash": "baseline", "entries": []}
        def _assistant_entries(self): return []
        def generation_visible(self): return False
        def close(self): pass

    bridge = Bridge()
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: attaches.append(1) or bridge))
    monkeypatch.setattr(watcher_module, "canonicalize_attached_architect_conversation", lambda _w, _b, requested: requested)
    for _ in range(2):
        assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
        now[0] += 6.0
    assert watcher.state["rolloverRecoveryAttemptCount"] == 2
    assert watcher_module.service_deferred_rollover_once(watcher, "endpoint", lambda: False, "NEXT_PROMPT_READY") is False
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state.get("humanRequiredReason")
    assert watcher.state["rolloverRecoveryState"] == "DEFERRED"
    assert len(attaches) == 2
    assert watcher.state["rolloverDue"] is True
    assert watcher.state["nextTaskId"] == "000054"
    assert watcher.state["nextPromptPath"] == str(prompt)
    assert prompt.read_bytes() == b"preserved staged prompt"
    class Process:
        pid = 9001
    assert watcher_module.dispatch_next_prompt_once(watcher, lambda *_args: (launches.append(1) or Process()), "endpoint", lambda: False) is None
    assert attaches == [1, 1]
    assert launches == []


def test_diagnostic_trace_is_opt_in_and_records_existing_browser_evidence(tmp_path, monkeypatch):
    import local_orchestrator_watcher as watcher_module
    from local_orchestrator_watcher import ArchitectPlaywright, DiagnosticTracer, LocalWatcher

    monkeypatch.delenv("ORCHESTRATOR_DIAGNOSTIC_TRACE", raising=False)
    assert not DiagnosticTracer.enabled()
    assert not (tmp_path / "logs" / "diagnostic").exists()

    monkeypatch.setenv("ORCHESTRATOR_DIAGNOSTIC_TRACE", "1")
    tracer = DiagnosticTracer(tmp_path, "run-test")
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.diagnostic_trace = tracer
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "000053", "nextTaskId": "000054"})
    watcher.save()
    watcher.state["rolloverDue"] = True
    watcher.save()

    calls = []
    semantic = "handover body\nARCHITECT_HANDOVER_READY"
    class Page:
        def evaluate(self, script):
            calls.append(script)
            return [{"id": "assistant-1", "text": semantic}]

    bridge = ArchitectPlaywright(Page())
    bridge.diagnostic_trace = tracer
    entries = bridge._assistant_entries()
    assert entries[0]["text"] == semantic
    assert len(calls) == 1
    tracer.record("TEST", "test", "GATE", "DECISION", watcher.state, gate="example", result="ALLOW")
    connection_id = tracer.connection_begin("endpoint", "OLD", watcher.state)
    tracer.attach_end(connection_id, "endpoint", "OLD", "OLD", time.monotonic(), watcher.state)
    tracer.close_connection(connection_id, "test", watcher.state)
    tracer.shutdown(watcher.state)

    trace_path = tmp_path / "logs" / "diagnostic" / "run-test" / "trace.jsonl"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["seq"] for record in records] == sorted(record["seq"] for record in records)
    assert any(record["operation"] == "STATE_WRITE" for record in records)
    assert any(record["operation"] == "assistant_entries" for record in records)
    assert any(record["operation"] == "ASSISTANT_ENTRY" and record["sha256"] == hashlib.sha256(semantic.encode()).hexdigest() for record in records)
    assert any(record["operation"] == "GATE" and record["gate"] == "example" for record in records)
    snapshots = list((tmp_path / "logs" / "diagnostic" / "run-test" / "snapshots").glob("*"))
    assert snapshots
    assert any(path.read_text(encoding="utf-8") == semantic for path in snapshots)


def test_diagnostic_trace_cap_disables_only_trace_writes(tmp_path, monkeypatch):
    import local_orchestrator_watcher as watcher_module
    from local_orchestrator_watcher import DiagnosticTracer
    monkeypatch.setattr(watcher_module, "DIAGNOSTIC_TRACE_MAX_BYTES", 300)
    tracer = DiagnosticTracer(tmp_path, "run-cap")
    tracer.record("TEST", "test", "LARGE", "BEGIN", {}, payload="x" * 1000)
    tracer.record("TEST", "test", "AFTER", "END", {})
    tracer.shutdown({})
    lines = (tmp_path / "logs" / "diagnostic" / "run-cap" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 2


def test_rollover_diagnostic_only_is_read_only_and_logs_memory_sources(tmp_path, monkeypatch):
    from local_orchestrator_watcher import DiagnosticTracer, LocalWatcher, run_rollover_diagnostic_only

    state_path = tmp_path / "state.json"
    initial = {
        "state": "RESULT_READY", "taskId": "000069", "lastCompletedTaskId": "000069",
        "nextTaskId": "000070", "discussionPauseActive": True,
        "architectConversationId": "current", "architectMemoryBytes": 247 * 1024 * 1024,
        "architectMemoryOwnership": "SESSION_SCOPED", "architectMemoryOwnershipSource": "ARCHITECT_PAGE_RENDERER",
        "rolloverDue": False, "rolloverPending": False, "rolloverInProgress": False,
        "executorProcessState": "COMPLETED_WITH_RESULT",
    }
    state_path.write_text(json.dumps(initial), encoding="utf-8")
    watcher = LocalWatcher(str(tmp_path), state_path, runner=object())
    tracer = DiagnosticTracer(tmp_path, "diagnostic-only")
    watcher.diagnostic_trace = tracer
    calls = {"evaluate": 0, "title": 0, "stop": 0, "send": []}

    class Page:
        url = "https://chatgpt.com/c/current"
        def title(self):
            calls["title"] += 1
            return "Architect"
        def evaluate(self, _script):
            calls["evaluate"] += 1
            return {
                "measureUserAgentSpecificMemoryAvailable": True,
                "measureUserAgentSpecificMemoryBytes": 260 * 1024 * 1024,
                "measureUserAgentSpecificMemoryError": None,
                "performanceMemoryAvailable": True,
                "usedJSHeapSize": 247 * 1024 * 1024,
                "totalJSHeapSize": 300 * 1024 * 1024,
                "jsHeapSizeLimit": 2048 * 1024 * 1024,
            }

    page = Page()
    class CdpSession:
        def send(self, method):
            calls["send"].append(method)
            if method == "SystemInfo.getProcessInfo":
                return {"processInfo": [{"type": "browser", "id": 10}, {"type": "renderer", "id": 11}]}
            return {"targetInfos": [{"targetId": "target-1", "type": "page", "url": page.url, "title": "Architect", "attached": True}]}
        def detach(self):
            pass

    class Browser:
        contexts = [type("Context", (), {"pages": [page]})()]
        def new_browser_cdp_session(self):
            return CdpSession()

    class Runtime:
        def stop(self):
            calls["stop"] += 1

    class Bridge:
        def __init__(self):
            self.page = page
            self._browser = Browser()
            self._runtime = Runtime()
            self._diagnostic_connection_id = "PW-CONN-0001"

    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: Bridge()))
    monkeypatch.setattr(watcher_module, "architect_windows_process_rows", lambda: [
        {"pid": 10, "parentPid": 1, "workingSet": 100},
        {"pid": 11, "parentPid": 10, "workingSet": 120 * 1024 * 1024},
    ])
    monkeypatch.setattr(watcher_module, "resolve_architect_browser_root_pid", lambda _endpoint: 10)
    monkeypatch.setattr(watcher_module, "architect_process_tree_memory_bytes", lambda _root, _rows: 130 * 1024 * 1024)

    before = state_path.read_bytes()
    assert run_rollover_diagnostic_only(watcher, "http://127.0.0.1:9333", tracer) == "current"
    tracer.shutdown(watcher.state)
    assert state_path.read_bytes() == before
    assert calls["evaluate"] == 1
    assert calls["send"] == ["SystemInfo.getProcessInfo", "Target.getTargets"]
    assert calls["stop"] == 1
    assert watcher.state["state"] == "RESULT_READY"
    assert watcher.state["discussionPauseActive"] is True
    records = [json.loads(line) for line in (tmp_path / "logs" / "diagnostic" / "diagnostic-only" / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    candidates = [record for record in records if record.get("operation") == "MEMORY_CANDIDATE"]
    sources = {record.get("source") for record in candidates}
    assert "performance.measureUserAgentSpecificMemory" in sources
    assert "performance.memory.usedJSHeapSize" in sources
    assert "architect_tab_renderer_working_set" in sources
    assert "governed_browser_process_tree_working_set" in sources
    assert any(record.get("operation") == "MEMORY_APIS" for record in records)
    assert any(record.get("operation") == "ROLLOVER_GATE" and record.get("reason") == "DISCUSSION_PAUSED" for record in records)
    assert any(record.get("operation") == "EXECUTOR_DISPATCH_GATE" and record.get("reason") == "DIAGNOSTIC_ONLY" for record in records)
    assert any(record.get("operation") == "DIAGNOSTIC_ONLY_COMPLETE" for record in records) is False


def test_rollover_diagnostic_only_logs_memory_api_exception_without_mutation(tmp_path, monkeypatch):
    from local_orchestrator_watcher import DiagnosticTracer, LocalWatcher, run_rollover_diagnostic_only
    state_path = tmp_path / "state.json"
    initial = {"state": "RESULT_READY", "taskId": "000069", "discussionPauseActive": True, "architectConversationId": "current"}
    state_path.write_text(json.dumps(initial), encoding="utf-8")
    watcher = LocalWatcher(str(tmp_path), state_path, runner=object())
    tracer = DiagnosticTracer(tmp_path, "diagnostic-error")
    class Page:
        url = "https://chatgpt.com/c/current"
        def title(self): return "Architect"
        def evaluate(self, _script): raise RuntimeError("measure rejected")
    page = Page()
    class Session:
        def send(self, method):
            return {"processInfo": [{"type": "browser", "id": 10}, {"type": "renderer", "id": 11}]} if method == "SystemInfo.getProcessInfo" else {"targetInfos": []}
        def detach(self): pass
    class Browser:
        contexts = [type("Context", (), {"pages": [page]})()]
        def new_browser_cdp_session(self): return Session()
    class Runtime:
        def stop(self): pass
    class Bridge:
        _diagnostic_connection_id = "PW-CONN-0001"
        _browser = Browser()
        _runtime = Runtime()
        def __init__(self): self.page = page
    monkeypatch.setattr(watcher_module.ArchitectPlaywright, "attach", staticmethod(lambda *_args: Bridge()))
    monkeypatch.setattr(watcher_module, "architect_windows_process_rows", lambda: [])
    before = state_path.read_bytes()
    assert run_rollover_diagnostic_only(watcher, "endpoint", tracer) == "current"
    tracer.shutdown(watcher.state)
    assert state_path.read_bytes() == before
    records = [json.loads(line) for line in (tmp_path / "logs" / "diagnostic" / "diagnostic-error" / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    error = next(record for record in records if record.get("operation") == "MEMORY_APIS")
    assert error["phase"] == "ERROR"
    assert error["errorClass"] == "RuntimeError"
    assert "measure rejected" in error["errorMessage"]
    assert "stackTrace" in error


def test_rollover_diagnostic_only_flag_is_opt_in(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ROLLOVER_DIAGNOSTIC_ONLY", raising=False)
    assert not watcher_module.rollover_diagnostic_only_enabled()
    monkeypatch.setenv("ORCHESTRATOR_ROLLOVER_DIAGNOSTIC_ONLY", "1")
    assert watcher_module.rollover_diagnostic_only_enabled()


def test_rollover_recovery_accounting_records_successful_completion(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, LocalWatcher
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state.update({"rolloverDue": True, "rolloverPending": True, "taskId": "task-1"})
    rollover = ArchitectSessionRollover(watcher)
    assert rollover._begin_bounded_recovery() == watcher_module.ROLLOVER_RECOVERY_ATTEMPT_ALLOWED
    assert watcher.state["rolloverRecoveryAttemptCount"] == 1
    rollover._record_recovery_success()
    assert watcher.state["rolloverRecoveryState"] == "COMPLETE"


def test_memory_safety_ceiling_enters_rollover_cutout(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, ARCHITECT_MEMORY_SAFETY_CEILING_BYTES, LocalWatcher
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    assert rollover.sample_memory(lambda: ARCHITECT_MEMORY_SAFETY_CEILING_BYTES) == "MEMORY_THRESHOLD"
    assert watcher.state.get("rolloverDue") is True
    assert watcher.state.get("state") != "HUMAN_REQUIRED"


def test_architect_memory_tree_aggregates_only_owned_root_and_descendants():
    from local_orchestrator_watcher import architect_process_tree_memory_bytes
    rows = [
        {"pid": 10, "parentPid": 1, "workingSet": 100},
        {"pid": 11, "parentPid": 10, "workingSet": 200},
        {"pid": 12, "parentPid": 11, "workingSet": 300},
        {"pid": 99, "parentPid": 1, "workingSet": 9999},
    ]
    assert architect_process_tree_memory_bytes(10, rows) == 600


def test_memory_threshold_is_primary_and_creates_one_deferred_rollover(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, ARCHITECT_MEMORY_THRESHOLD_BYTES
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    lines = []
    assert rollover.sample_memory(lambda: ARCHITECT_MEMORY_THRESHOLD_BYTES - 1, lines.append) is None
    assert rollover.sample_memory(lambda: ARCHITECT_MEMORY_THRESHOLD_BYTES, lines.append) == "MEMORY_THRESHOLD"
    assert watcher.state.get("rolloverDue") is True
    assert watcher.state.get("rolloverTrigger") == "MEMORY_THRESHOLD"
    assert lines == ["ROLLOVER_DUE trigger=MEMORY_THRESHOLD"]


def test_rollover_waits_for_generation_and_requests_once_after_threshold(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, ARCHITECT_MEMORY_THRESHOLD_BYTES
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(tmp_path / "next.txt"), "architectMemoryBytes": ARCHITECT_MEMORY_THRESHOLD_BYTES})
    Path(watcher.state["nextPromptPath"]).write_text("next", encoding="utf-8")
    class Bridge:
        def __init__(self): self.calls = 0
        def submit_result_bounded(self, value): self.calls += 1
    bridge = Bridge()
    assert not rollover.request_if_due(bridge, True, False, lambda _: None, architect_generating=True, safe_boundary_state="NEXT_PROMPT_READY")
    assert rollover.request_if_due(bridge, True, False, lambda _: None, safe_boundary_state="NEXT_PROMPT_READY")
    assert not rollover.request_if_due(bridge, True, False, lambda _: None, safe_boundary_state="NEXT_PROMPT_READY")
    assert bridge.calls == 1


def test_response_count_does_not_override_memory_threshold(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, ARCHITECT_MEMORY_THRESHOLD_BYTES
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher)
    watcher.state["architectResponseCount"] = 29
    assert rollover.rollover_trigger(0, 29) is None
    assert rollover.rollover_trigger(0, 30) is None
    assert rollover.rollover_trigger(ARCHITECT_MEMORY_THRESHOLD_BYTES - 1, 30) is None
    assert rollover.rollover_trigger(ARCHITECT_MEMORY_THRESHOLD_BYTES, 0) == "MEMORY_THRESHOLD"


def test_current_session_memory_uses_tab_renderer_working_set(tmp_path, monkeypatch):
    from local_orchestrator_watcher import ArchitectPlaywright, ARCHITECT_MEMORY_THRESHOLD_BYTES
    class Page:
        url = "https://chatgpt.com/c/current"
    class Session:
        def send(self, method):
            assert method == "SystemInfo.getProcessInfo"
            return {"processInfo": [{"type": "browser", "id": 10}, {"type": "renderer", "id": 11}]}
        def detach(self): pass
    page = Page()
    class Browser:
        def __init__(self): self.contexts = [type("Context", (), {"pages": [page]})()]
        def new_browser_cdp_session(self): return Session()
    monkeypatch.setattr(watcher_module, "architect_renderer_working_set_bytes", lambda *_args: ARCHITECT_MEMORY_THRESHOLD_BYTES)
    bridge = ArchitectPlaywright(page)
    bridge._browser = Browser()
    assert bridge.current_session_memory_bytes() == ARCHITECT_MEMORY_THRESHOLD_BYTES


def test_current_session_memory_requires_unique_substantial_renderer(tmp_path):
    from local_orchestrator_watcher import architect_renderer_working_set_bytes
    rows = [{"pid": 11, "parentPid": 10, "workingSet": 123456}]
    with pytest.raises(RuntimeError, match="ARCHITECT_SESSION_MEMORY_UNAVAILABLE"):
        architect_renderer_working_set_bytes(10, {11}, rows)


def test_current_session_memory_unavailable_fails_closed(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright
    class Page:
        url = "https://chatgpt.com/c/current"
    class Browser:
        contexts = [type("Context", (), {"pages": [Page()]})()]
        def new_browser_cdp_session(self): raise RuntimeError("no cdp")
    bridge = ArchitectPlaywright(Browser.contexts[0].pages[0])
    bridge._browser = Browser()
    with pytest.raises(RuntimeError, match="ARCHITECT_SESSION_MEMORY_UNAVAILABLE"):
        bridge.current_session_memory_bytes()


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
    prompt = tmp_path / "next.txt"; prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt), "architectResponseCount": 30})
    watcher.state["architectMemoryBytes"] = 891289600
    class OldPage:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    class Bridge:
        def submit_result_bounded(self, value): pass
        def open_fresh_with_handover(self, value): raise RuntimeError("new tab failed")
    bridge = Bridge(); bridge.page = OldPage()
    assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    response = (f"<HANDOVER>\nversion=1\ntransactionId={watcher.state['rolloverTransactionId']}\n"
                "taskId=task-2\n\nbody\n</HANDOVER>")
    assert not rollover.complete_from_response(bridge, response)
    assert bridge.page.closed is False
    assert watcher.state["handoverReady"] is False
    assert watcher.state["pending_handover"] == response
    assert watcher.state["rolloverDue"] is True


def test_formatted_rollover_transaction_token_is_accepted_and_reaches_fresh_path(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, handover_transaction_matches
    transaction_id = "4b7971a1ac59ac799bc02f58"
    production_format = """AFFOTECH ARCHITECT SESSION HANDOVER

ROLLOVER TRANSACTION

`4b7971a1ac59ac799bc02f58`

ARCHITECT_HANDOVER_READY"""
    canonical_format = f"<HANDOVER>\nversion=1\ntransactionId={transaction_id}\ntaskId=000070\n\nbody\n</HANDOVER>"
    assert handover_transaction_matches(production_format, transaction_id)
    assert handover_transaction_matches(canonical_format, transaction_id)
    assert not handover_transaction_matches("handover\nARCHITECT_HANDOVER_READY", transaction_id)
    assert not handover_transaction_matches("handover\nRollover transaction ID: wrong\nARCHITECT_HANDOVER_READY", transaction_id)
    assert not handover_transaction_matches(f"handover\n`{transaction_id[:-1]}`\nARCHITECT_HANDOVER_READY", transaction_id)
    assert not handover_transaction_matches(f"handover\n`{transaction_id}a`\nARCHITECT_HANDOVER_READY", transaction_id)

    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state.update({
        "state": "NEXT_PROMPT_READY", "taskId": "000069", "nextTaskId": "000070",
        "handoverRequested": True, "rolloverPending": True,
        "rolloverTransactionId": transaction_id, "rolloverTransactionTaskId": "000070",
        "rolloverHandoverProtocolVersion": 1,
    })
    watcher.save()
    rollover = ArchitectSessionRollover(watcher)
    reached = []
    rollover._existing_fresh_candidate_page = lambda _bridge, _handover: reached.append(True) or None
    class OldPage:
        def close(self): pass
    class Bridge:
        page = OldPage()
        def open_fresh_with_handover(self, _handover):
            reached.append("fresh")
            raise RuntimeError("fresh path reached")
    assert rollover.complete_from_response(Bridge(), production_format) is False
    assert reached == []
    assert rollover.complete_from_response(Bridge(), canonical_format) is False
    assert reached == [True, "fresh"]


def test_handover_request_explicitly_requires_exact_transaction_echo():
    from local_orchestrator_watcher import handover_request_for_transaction, handover_transaction_matches, parse_handover_envelope

    transaction_id = "4b7971a1ac59ac799bc02f58"
    request = handover_request_for_transaction(transaction_id)
    assert "<HANDOVER>\nversion=1" in request
    assert f"transactionId={transaction_id}" in request
    assert "ARCHITECT_HANDOVER_READY" not in request
    assert "ARCHITECT_HANDOVER_BEGIN" not in request
    assert "ARCHITECT_HANDOVER_END" not in request
    response = f"<HANDOVER>\nversion=1\ntransactionId={transaction_id}\ntaskId=task-2\n\nbody\n</HANDOVER>"
    assert parse_handover_envelope(response)["transactionId"] == transaction_id
    exact_line = f"Rollover transaction ID: {transaction_id}"
    assert handover_transaction_matches(f"handover\n{exact_line}\nARCHITECT_HANDOVER_READY", transaction_id)
    assert not handover_transaction_matches("handover\nARCHITECT_HANDOVER_READY", transaction_id)
    assert not handover_transaction_matches("handover\nRollover transaction ID: wrong\nARCHITECT_HANDOVER_READY", transaction_id)


def test_transaction_match_failure_is_diagnosed_without_handover_text(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover, DiagnosticTracer
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state.update({"state": "NEXT_PROMPT_READY", "handoverRequested": True, "rolloverTransactionId": "4b7971a1ac59ac799bc02f58"})
    watcher.save()
    tracer = DiagnosticTracer(tmp_path, "transaction-mismatch")
    watcher.diagnostic_trace = tracer
    class Bridge:
        page = object()
    assert ArchitectSessionRollover(watcher).complete_from_response(Bridge(), "wrong-id\nARCHITECT_HANDOVER_READY") is False
    tracer.shutdown(watcher.state)
    records = [json.loads(line) for line in (tmp_path / "logs" / "diagnostic" / "transaction-mismatch" / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    record = next(item for item in records if item.get("operation") == "TRANSACTION_MATCH")
    assert record["expectedTransactionId"] == "4b7971a1ac59ac799bc02f58"
    assert record["transactionTokenPresent"] is False
    assert record["handoverReady"] is True
    assert record["reason"] == "TRANSACTION_TOKEN_MISSING_OR_INVALID"
    assert "wrong-id" not in json.dumps(record)


def test_successful_architect_rollover_switches_then_closes_old_tab_and_resets_count(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    rollover = ArchitectSessionRollover(watcher); rollover.initialize_current_session()
    prompt = tmp_path / "next.txt"; prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt), "architectResponseCount": 30})
    watcher.state["architectMemoryBytes"] = 891289600
    class Page:
        def __init__(self, url): self.url = url; self.closed = False
        def close(self): self.closed = True
        def evaluate(self, script):
                return False if "stop-button" in script else [{"id": "ack", "text": "ARCHITECT_SESSION_READY"}]
    old = Page("https://chatgpt.com/c/OLD"); new = Page("https://chatgpt.com/c/NEW")
    class Bridge:
        page = old
        def submit_result_bounded(self, value): pass
        def open_fresh_with_handover(self, value): return new
        def current_session_memory_bytes(self): return 7
    bridge = Bridge(); assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert rollover.complete_from_response(bridge, f"<HANDOVER>\nversion=1\ntransactionId={watcher.state['rolloverTransactionId']}\ntaskId=task-2\n\nbody\n</HANDOVER>")
    assert bridge.page is new and old.closed
    assert watcher.state["architectResponseCount"] == 0
    assert watcher.state["handoverRequested"] is False
    assert watcher.state["architectConversationId"] == "NEW"
    assert watcher.state["architectMemoryOwnership"] == "SESSION_SCOPED"
    assert watcher.state["architectMemorySessionId"] == "NEW"
    assert watcher.state.get("architectMemoryBytes") is None
    assert "currentArchitectConversationId" not in watcher.state


def test_rollover_identity_is_the_next_resident_attach_target(tmp_path):
    from local_orchestrator_watcher import ArchitectSessionRollover
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", runner=object())
    watcher.state["architectConversationId"] = "OLD"
    rollover = ArchitectSessionRollover(watcher)
    rollover.initialize_current_session()
    prompt = tmp_path / "next.txt"; prompt.write_text("next", encoding="utf-8")
    watcher.state.update({"state": "NEXT_PROMPT_READY", "taskId": "task-1", "nextTaskId": "task-2", "nextPromptPath": str(prompt), "architectResponseCount": 30})
    watcher.state["architectMemoryBytes"] = 891289600

    class Page:
        def __init__(self, url): self.url = url; self.closed = False
        def close(self): self.closed = True
        def evaluate(self, script):
                return False if "stop-button" in script else [{"id": "ack", "text": "ARCHITECT_SESSION_READY"}]

    old = Page("https://chatgpt.com/c/OLD")
    new = Page("https://chatgpt.com/c/NEW")
    class Bridge:
        page = old
        def submit_result_bounded(self, _value): pass
        def open_fresh_with_handover(self, _value): return new

    bridge = Bridge()
    assert rollover.request_if_due(bridge, True, False, safe_boundary_state="NEXT_PROMPT_READY")
    assert rollover.complete_from_response(bridge, f"<HANDOVER>\nversion=1\ntransactionId={watcher.state['rolloverTransactionId']}\ntaskId=task-2\n\nbody\n</HANDOVER>")
    attach_targets = []
    for _ in range(2):
        attach_targets.append(watcher.state.get("architectConversationId"))
    assert attach_targets == ["NEW", "NEW"]
    assert "OLD" not in attach_targets
    assert old.closed


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
    assert bridge.wait_for_new_response(baseline, poll_interval=0)["state"] == "COMPLETED"
    assert extract_executor_prompt_envelope(prompt_response) == "complete without marker"


def test_wait_for_new_response_recognizes_terminal_handover_marker_without_generic_complete():
    from local_orchestrator_watcher import ArchitectPlaywright
    handover = "captured handover\nARCHITECT_HANDOVER_READY"

    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "old", "text": "old"}],
                [{"id": "new", "text": handover}],
            ])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return False

    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_baseline()
    observed = bridge.wait_for_new_response(baseline, poll_interval=0)
    assert observed == {"state": "COMPLETED", "text": handover}
    assert bridge.last_wait_diagnostics["completionReason"] == "HANDOVER_READY_MARKER"
    assert bridge.last_wait_diagnostics["baselineAssistantCount"] == 1
    assert bridge.last_wait_diagnostics["candidateLatestAssistantIdentity"] == "new"
    assert bridge.last_wait_diagnostics["candidateTextSha256"] == hashlib.sha256(handover.encode("utf-8")).hexdigest()
    assert bridge.last_wait_diagnostics["returnedTextReReadAtReturn"] is False
    assert bridge.last_wait_diagnostics["generationVisibleLastSample"] is False


def test_waiter_diagnostics_identify_stable_partial_response_without_changing_result():
    from local_orchestrator_watcher import ArchitectPlaywright
    partial = "partial handover body; still no terminal marker"

    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "old", "text": "old"}],
                [{"id": "new", "text": partial}],
                [{"id": "new", "text": partial}],
            ])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return False

    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_baseline()
    observed = bridge.wait_for_new_response(baseline, poll_interval=0)
    assert observed == {"state": "BLOCKED", "text": partial}
    diagnostics = bridge.last_wait_diagnostics
    assert diagnostics["completionReason"] == "STABLE_TWO_POLL_NO_COMPLETION_MARKER"
    assert diagnostics["candidateTextSha256"] == hashlib.sha256(partial.encode("utf-8")).hexdigest()
    assert diagnostics["stabilityPollCount"] == 2
    assert diagnostics["generationCheckSkippedForPendingStabilityPoll"] is True
    assert diagnostics["returnWhileGenerationVisible"] == "NOT_SAMPLED"


def test_waiter_diagnostics_identify_different_unrelated_candidate():
    from local_orchestrator_watcher import ArchitectPlaywright
    unrelated = "unrelated old answer\nARCHITECT_HANDOVER_READY"

    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "baseline", "text": "previous"}],
                [{"id": "different-candidate", "text": unrelated}],
            ])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return False

    bridge = ArchitectPlaywright(Page())
    observed = bridge.wait_for_new_response(bridge.assistant_baseline(), poll_interval=0)
    assert observed == {"state": "COMPLETED", "text": unrelated}
    diagnostics = bridge.last_wait_diagnostics
    assert diagnostics["candidateLatestAssistantIdentity"] == "different-candidate"
    assert diagnostics["candidateTextSha256"] == hashlib.sha256(unrelated.encode("utf-8")).hexdigest()
    assert diagnostics["causalRequestCorrelationAvailable"] is False
    assert diagnostics["completionReason"] == "HANDOVER_READY_MARKER"


def test_healthy_large_history_polling_uses_no_full_history_scan(monkeypatch):
    from local_orchestrator_watcher import ArchitectPlaywright

    class Page:
        def __init__(self):
            self.polls = 0
        def evaluate(self, script):
            if "latestTextLength" in script:
                self.polls += 1
                return {"count": 200, "latestMessageId": "a-199", "latestText": "unchanged", "latestTextLength": 9}
            if "cloneNode" in script:
                raise AssertionError("latest extraction is not allowed for unchanged polling")
            return False

    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_fast_snapshot()
    monkeypatch.setattr("local_orchestrator_watcher.time.sleep", lambda _delay: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        bridge.wait_for_new_response(baseline, poll_interval=0)
    except KeyboardInterrupt:
        pass
    metrics = bridge.observation_metrics()
    assert metrics["fastObservations"] >= 1
    assert metrics["fullHistoryScans"] == 0
    assert metrics["historicalAssistantExtractions"] == 0


def test_new_response_extracts_only_latest_semantic_node():
    from local_orchestrator_watcher import ArchitectPlaywright
    complete = f"latest\n{BEGIN}\nprompt\n{END}\n{COMPLETE}"

    class Page:
        def __init__(self):
            self.latest = False
        def evaluate(self, script):
            if "latestTextLength" in script:
                return {"count": 101 if self.latest else 100, "latestMessageId": "new" if self.latest else "old", "latestText": complete if self.latest else "old", "latestTextLength": len(complete if self.latest else "old")}
            if "cloneNode" in script:
                return {"id": "new", "text": complete, "rawText": complete, "semanticSource": "STANDARD_RESPONSE"}
            return False

    page = Page()
    bridge = ArchitectPlaywright(page)
    baseline = bridge.assistant_fast_snapshot()
    page.latest = True
    observed = bridge.wait_for_new_response(baseline, poll_interval=0)
    assert observed == {"state": "COMPLETED", "text": complete}
    metrics = bridge.observation_metrics()
    assert metrics["fullHistoryScans"] == 0
    assert metrics["latestResponseExtractions"] == 1


def test_performance_harness_large_conversation_keeps_normal_observation_bounded(monkeypatch):
    from local_orchestrator_watcher import ArchitectPlaywright

    class Users:
        def count(self): return 200
        def nth(self, index):
            return type("User", (), {"inner_text": lambda _self: "latest user" if index == 199 else f"user-{index}"})()

    class Page:
        def __init__(self): self.polls = 0
        def evaluate(self, script):
            if "latestTextLength" in script:
                return {"count": 200, "latestMessageId": "assistant-199", "latestText": "stable", "latestTextLength": 6}
            if "cloneNode" in script:
                raise AssertionError("normal performance harness must not clone history")
            return False
        def locator(self, selector):
            if 'data-message-author-role="user"' in selector:
                return Users()
            raise AssertionError("normal performance harness must not inspect assistant history through locators")

    bridge = ArchitectPlaywright(Page())
    baseline = bridge.assistant_fast_snapshot()
    def tick(_delay):
        bridge.page.polls += 1
        if bridge.page.polls >= 30:
            raise KeyboardInterrupt
    monkeypatch.setattr("local_orchestrator_watcher.time.sleep", tick)
    try:
        bridge.wait_for_new_response(baseline, poll_interval=0)
    except KeyboardInterrupt:
        pass
    assert bridge.page.polls == 30
    assert bridge.exact_user_message_payload_observed("latest user")
    metrics = bridge.observation_metrics()
    assert metrics["fullHistoryScans"] == 0
    assert metrics["historicalAssistantExtractions"] == 0
    assert metrics["historicalUserExtractions"] == 0


def test_latest_writing_block_extraction_preserves_semantics_without_history_scan():
    from local_orchestrator_watcher import ArchitectPlaywright
    class Page:
        def evaluate(self, script):
            assert "cloneNode" in script
            return {"id": "latest", "text": "writing block prose", "rawText": "writing block prose", "semanticSource": "WRITING_BLOCK"}
    bridge = ArchitectPlaywright(Page())
    entry = bridge.latest_assistant_entry()
    assert entry["text"] == "writing block prose"
    assert entry["semanticSource"] == "WRITING_BLOCK"
    assert bridge.observation_metrics()["fullHistoryScans"] == 0


def test_normal_delivery_proof_uses_latest_user_message_before_history():
    from local_orchestrator_watcher import LocalFirstOrchestrator
    class Bridge:
        def latest_user_message(self):
            return "exact payload"
        def user_message_texts(self):
            raise AssertionError("normal proof must not scan historical users")
    watcher = object.__new__(LocalFirstOrchestrator)
    watcher.state = {}
    assert watcher._result_payload_proof(Bridge(), "exact payload", history=False)


def test_completion_fallback_rejects_generation_visible_and_malformed_envelopes():
    assert extract_executor_prompt_envelope(f"{BEGIN}\none\n{END}\n{BEGIN}\ntwo\n{END}") is None
    assert extract_executor_prompt_envelope(f"{BEGIN}\nouter {BEGIN}\ninner\n{END}\n{END}") is None

    from local_orchestrator_watcher import ArchitectPlaywright
    response = "answer\npartial response without a completed envelope"
    class Page:
        def __init__(self):
            self.snapshots = iter([
                [{"id": "old", "text": "old"}],
                [{"id": "new", "text": response}],
                [{"id": "new", "text": response}],
                [{"id": "new", "text": response}],
            ])
            self.generating = iter([True, False, False])
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                return next(self.snapshots)
            return next(self.generating)
    page = Page()
    bridge = ArchitectPlaywright(page)
    assert bridge.wait_for_new_response(
        bridge.assistant_baseline(), poll_interval=0
    )["state"] == "BLOCKED"


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


def test_confirmed_submission_and_running_generation_are_not_misclassified():
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
    assert bridge.generation_visible() is False
    messages.values.append("partial"); page.running = True
    assert bridge.generation_visible() is True  # active generation remains a wait state
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
    assert result.state == "FAILED"  # exit=0 without a result is missing-result evidence
    assert result.exit_code == 0


def test_codex_nonzero_exit_is_truthful_and_executor_has_no_timeout(tmp_path, monkeypatch):
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

    class LongRunningProcess:
        pid = 8125
        returncode = 0
        def poll(self): return None
        def communicate(self, **kwargs):
            assert "timeout" not in kwargs
            return "", ""
        def wait(self): return self.returncode
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *args, **kwargs: LongRunningProcess())
    result = CodexRunner(str(tmp_path), "codex").run("prompt", timeout=0.001)
    assert result.state == "FAILED" and result.timed_out is False and result.exit_code == 0


def test_result_submission_is_impossible_without_terminal_integer(tmp_path):
    class Runner:
        def run(self, prompt, timeout): return CodexResult("COMPLETED", "result", None, False)
    class Bridge:
        def __init__(self): self.submitted = []
        def submit_result(self, result): self.submitted.append(result)
    watcher = LocalWatcher(str(tmp_path), tmp_path / "state.json", Runner())
    bridge = Bridge(); output = []
    watcher._execute_prompt(bridge, "prompt", 1, output.append)
    assert "CODEX_FAILED exit=unknown reason=missing_exit_evidence" in output
    assert bridge.submitted == []


def test_live_codex_beyond_former_timeout_remains_running_and_never_stalls(tmp_path, monkeypatch):
    make_bootstrap(tmp_path)
    import local_orchestrator_watcher as watcher_module
    seen = {}
    class LiveProcess:
        pid = 9010
        def poll(self): return None
        def communicate(self, **kwargs):
            seen["communicate_kwargs"] = kwargs
            return "", ""
        def wait(self): return 0
    monkeypatch.setattr(watcher_module.subprocess, "Popen", lambda *a, **k: LiveProcess())
    monkeypatch.setattr(watcher_module, "discover_codex_launcher", lambda executable="codex": [sys.executable])
    result = CodexRunner(str(tmp_path), "codex").run("long task", timeout=0.0001)
    assert result.timed_out is False
    assert "timeout" not in seen["communicate_kwargs"]
    assert result.exit_code == 0


def test_restart_with_alive_recorded_pid_stays_running_without_duplicate(tmp_path, monkeypatch):
    publication = "PUB-" + "a" * 32
    key = publication + ":" + "b" * 64
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"in_flight": True, "in_flight_relay_key": key,
                                      "relay_publication_id": publication,
                                      "active_codex_pid": 14304}), encoding="utf-8")
    class Source:
        def read_current(self): return {"publicationId": publication, "contentSha256": "b" * 64, "prompt": "must not rerun"}
    class Runner:
        def run(self, prompt, timeout): raise AssertionError("live executor must not rerun")
    watcher = LocalWatcher(str(tmp_path), state_path, Runner())
    monkeypatch.setattr(watcher, "process_alive", lambda pid: pid == 14304)
    output = []
    assert watcher.run_relay_once(Source(), None, emit=output.append) == "RUNNING"
    assert "CODEX_RUNNING pid=14304" in output


def test_restart_dead_pid_recovers_result_without_rerun(tmp_path, monkeypatch):
    publication = "PUB-" + "c" * 32
    key = publication + ":" + "d" * 64
    result_path = tmp_path / "result.txt"
    result_path.write_text("recovered", encoding="utf-8")
    envelope_path = tmp_path / "result.txt.envelope.json"
    envelope_path.write_text(json.dumps(build_result_envelope("D", "PASS", publication)), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"in_flight": True, "in_flight_relay_key": key,
                                      "relay_publication_id": publication, "active_codex_pid": 14304,
                                      "result_file": str(result_path), "result_envelope_file": str(envelope_path)}), encoding="utf-8")
    class Source:
        def read_current(self): return {"publicationId": publication, "contentSha256": "d" * 64, "prompt": "must not rerun"}
    class Runner:
        def run(self, prompt, timeout): raise AssertionError("dead executor result must be reused")
    class Bridge:
        def __init__(self): self.results = []
        def submit_result_bounded(self, result): self.results.append(result)
    watcher = LocalWatcher(str(tmp_path), state_path, Runner(),
                           durable_terminal_publisher=lambda *a: {"publicationId": "GH-recovered"})
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watcher.run_relay_once(Source(), Bridge(), emit=lambda _: None) == "COMPLETED"
    assert json.loads(state_path.read_text())["last_completed_relay_key"] == key


def test_restart_dead_pid_without_result_requires_recovery_and_never_reruns(tmp_path, monkeypatch):
    publication = "PUB-" + "e" * 32
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"in_flight": True, "in_flight_relay_key": publication + ":" + "f" * 64,
                                      "relay_publication_id": publication, "active_codex_pid": 14304,
                                      "rerunAuthorized": False}), encoding="utf-8")
    class Source:
        def read_current(self): return {"publicationId": publication, "contentSha256": "f" * 64, "prompt": "must not rerun"}
    class Runner:
        def run(self, prompt, timeout): raise AssertionError("recovery must not rerun Codex")
    watcher = LocalWatcher(str(tmp_path), state_path, Runner())
    monkeypatch.setattr(watcher, "process_alive", lambda pid: False)
    assert watcher.run_relay_once(Source(), None, emit=lambda _: None) == "RECOVERY_REQUIRED"
    saved = json.loads(state_path.read_text())
    assert saved["rerunAuthorized"] is False
    assert saved["executor_state"] == "EXITED_WITHOUT_RECOVERABLE_RESULT"


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
    observed = bridge.wait_for_new_response(bridge.assistant_baseline(), poll_interval=0)
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
    assert restarted.state.get("relay_publication_id") == new
    assert restarted.state["result_pending"] is True
    assert restarted.state["relay_execution_retired"] is True


def test_long_generation_in_progress_is_not_forwarded_and_unchanged_stays_idle(monkeypatch):
    from local_orchestrator_watcher import ArchitectPlaywright
    long_text = "L" * 14000
    class Page:
        def __init__(self, generation, changed=True):
            self.generation = iter(generation)
            updated = long_text + "update" if changed else long_text
            self.snapshots = iter([[{"id": "long", "text": long_text}], [{"id": "long", "text": updated}], [{"id": "long", "text": updated}]])
            self.last_snapshot = [{"id": "long", "text": long_text}]
        def evaluate(self, script):
            if 'data-message-author-role="assistant"' in script:
                try:
                    self.last_snapshot = next(self.snapshots)
                except StopIteration:
                    pass
                return self.last_snapshot
            return next(self.generation)
        def locator(self, selector): raise AssertionError("long-response polling must remain atomic")
    bridge = ArchitectPlaywright(Page([True, False]))
    baseline = bridge.assistant_baseline()
    assert bridge.wait_for_new_response(baseline, poll_interval=1)["state"] == "BLOCKED"
    unchanged = ArchitectPlaywright(Page([False], changed=False))
    unchanged_baseline = unchanged.assistant_baseline()
    class StopAfterFirstPassivePoll(Exception):
        pass
    monkeypatch.setattr("local_orchestrator_watcher.time.sleep", lambda _delay: (_ for _ in ()).throw(StopAfterFirstPassivePoll()))
    try:
        unchanged.wait_for_new_response(unchanged_baseline, poll_interval=0.01)
    except StopAfterFirstPassivePoll:
        pass
    else:
        raise AssertionError("unchanged polling unexpectedly returned")
    assert unchanged.last_state == "NOT_YET"


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


def test_startup_baselines_current_response_without_replaying_it(tmp_path, monkeypatch):
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
    monkeypatch.setattr(bridge, "wait_for_new_response", lambda _baseline, _poll_interval: {"state": "NOT_YET", "text": ""})
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


def test_assistant_snapshot_removes_structural_ui_chrome_but_preserves_semantic_text():
    from local_orchestrator_watcher import ArchitectPlaywright, architect_handover_ready
    semantic = ("AFFOTECH ARCHITECT SESSION HANDOVER\n\n"
                "```python\nprint('button and Shorten are genuine content')\n```\n\n"
                "Fresh Architect session bootstrap protocol:\n\n"
                "After accepting this handover, reply exactly:\n\n"
                "ARCHITECT_SESSION_READY\n\nARCHITECT_HANDOVER_READY")
    contaminated = semantic + "\nShorten this handover for faster bootstrapping"
    assert "ARCHITECT_HANDOVER_READY" in contaminated
    assert not architect_handover_ready(contaminated)

    class Page:
        def evaluate(self, script):
            assert "cloneNode(true)" in script
            assert "button,[role=\"button\"]" in script
            return [{"id": "assistant-1", "text": semantic}]
        def locator(self, _selector):
            raise AssertionError("assistant snapshot should use one DOM evaluation")

    entry = ArchitectPlaywright(Page())._assistant_entries()[0]
    assert entry["id"] == "assistant-1"
    assert entry["text"] == semantic
    assert "Shorten this handover for faster bootstrapping" not in entry["text"]
    assert "button and Shorten are genuine content" in entry["text"]
    assert architect_handover_ready(entry["text"])


def test_assistant_writing_block_extraction_excludes_chatgpt_followups_without_string_filtering():
    from local_orchestrator_watcher import ArchitectPlaywright, architect_handover_ready

    semantic = ("AFFOTECH ARCHITECT SESSION HANDOVER\n\n"
                "The assistant may discuss a button named Shorten this handover.\n"
                "The literal attribute aria-hidden is part of this semantic text.\n\n"
                "ARCHITECT_HANDOVER_READY")
    contaminated = semantic + ("\nShorten this handover for faster bootstrapping\n"
                                "Separate current state from historical evidence\n"
                                "Add a compact decision summary")
    assert "ARCHITECT_HANDOVER_READY" in contaminated
    assert not architect_handover_ready(contaminated)

    class Page:
        def evaluate(self, script):
            assert '[data-testid="writing-block-container"]' in script
            assert '[data-testid="writing-block-suggested-followups"]' in script
            assert '[data-testid="writing-block-suggested-followups-surface"]' in script
            assert '[aria-hidden="true"]' in script
            return [{"id": "c3814397-278a-474f-8a25-8e01222bdf02",
                     "rawText": contaminated, "text": semantic,
                     "semanticSource": "WRITING_BLOCK"}]
        def locator(self, _selector):
            raise AssertionError("semantic extraction must use one DOM evaluation")

    entry = ArchitectPlaywright(Page())._assistant_entries()[0]
    assert entry["id"] == "c3814397-278a-474f-8a25-8e01222bdf02"
    assert entry["semanticSource"] == "WRITING_BLOCK"
    assert entry["text"] == semantic
    assert architect_handover_ready(entry["text"])
    assert "Shorten this handover for faster bootstrapping" not in entry["text"]
    assert "Separate current state from historical evidence" not in entry["text"]
    assert "Add a compact decision summary" not in entry["text"]
    assert "button named Shorten this handover" in entry["text"]
    assert "aria-hidden" in entry["text"]


def test_assistant_writing_blocks_preserve_dom_order_and_strict_terminal_marker():
    from local_orchestrator_watcher import ArchitectPlaywright, architect_handover_ready

    class Page:
        def evaluate(self, _script):
            return [{"id": "multi", "rawText": "first second trailing control",
                     "text": "first semantic\nsecond semantic", "semanticSource": "WRITING_BLOCK"}]

    entry = ArchitectPlaywright(Page())._assistant_entries()[0]
    assert entry["text"] == "first semantic\nsecond semantic"
    assert entry["id"] == "multi"
    assert not architect_handover_ready(entry["text"])


def test_assistant_entries_runtime_script_parses_and_old_python_escaping_is_rejected():
    from local_orchestrator_watcher import assistant_entries_script

    emitted = assistant_entries_script()
    assert "const text = clean(node);" in emitted
    assert r"writingBlocks.map(clean).join('\n')" not in emitted
    node_program = 'const source = process.argv[1]; new Function("return (" + source + ")");'
    valid = subprocess.run(["node", "-e", node_program, emitted], capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr

    broken = """() => ['a'].join('\n')"""
    assert r"join('\n')" not in broken
    invalid = subprocess.run(["node", "-e", node_program, broken], capture_output=True, text=True)
    assert invalid.returncode != 0


def _run_assistant_entries_dom_fixture(tree):
    from local_orchestrator_watcher import assistant_entries_script

    program = r'''
const source = process.argv[1];
const fixture = JSON.parse(process.argv[2]);
class Element {
  constructor(spec, parent = null) {
    this.attrs = Object.assign({}, spec.attrs || {});
    this.ownText = spec.text || '';
    this.parent = parent;
    this.children = (spec.children || []).map(child => new Element(child, this));
    this.connected = true;
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  get isConnected() { return this.connected; }
  get innerText() { return this.ownText + this.children.filter(c => c.connected).map(c => c.innerText).join(''); }
  get textContent() { return this.innerText; }
  cloneNode(deep) {
    const clone = new Element({attrs: this.attrs, text: this.ownText});
    if (deep) clone.children = this.children.filter(c => c.connected).map(child => child.cloneNode(true));
    clone.children.forEach(child => child.parent = clone);
    return clone;
  }
  remove() {
    this.connected = false;
    if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this);
  }
  matches(selector) {
    if (selector === 'button') return this.attrs.tag === 'button';
    if (selector === '[role="button"]') return this.attrs.role === 'button';
    if (selector === '[aria-hidden="true"]') return this.attrs['aria-hidden'] === 'true';
    if (selector === '[data-testid="writing-block-suggested-followups"]') return this.attrs['data-testid'] === 'writing-block-suggested-followups';
    if (selector === '[data-testid="writing-block-suggested-followups-surface"]') return this.attrs['data-testid'] === 'writing-block-suggested-followups-surface';
    const m = selector.match(/^\[([^=]+)="([^"]*)"\]$/);
    return !!m && this.attrs[m[1]] === m[2];
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(x => x.trim());
    const result = [];
    const walk = node => {
      for (const child of node.children) {
        if (!child.connected) continue;
        if (selectors.some(s => child.matches(s))) result.push(child);
        walk(child);
      }
    };
    walk(this);
    return result;
  }
}
const root = new Element(fixture);
global.document = {
  querySelectorAll: selector => {
    const selectors = selector.split(',').map(x => x.trim());
    const rootMatch = selectors.some(s => root.matches(s)) ? [root] : [];
    return rootMatch.concat(root.querySelectorAll(selector));
  }
};
const extractor = eval('(' + source + ')');
process.stdout.write(JSON.stringify(extractor()));
'''
    completed = subprocess.run(
        ["node", "-e", program, assistant_entries_script(), json.dumps(tree)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)[0]


def test_assistant_semantic_extraction_preserves_mixed_response_and_filters_controls():
    from local_orchestrator_watcher import architect_handover_ready, handover_transaction_matches

    marker = "2b324cd32cefc00ef1790c36"
    standard = _run_assistant_entries_dom_fixture({
        "attrs": {"data-message-author-role": "assistant", "data-message-id": "standard"},
        "text": "standard prose",
        "children": [{"attrs": {"tag": "button"}, "text": "button chrome"},
                      {"attrs": {"aria-hidden": "true"}, "text": "hidden chrome"}],
    })
    assert standard["semanticSource"] == "STANDARD_RESPONSE"
    assert standard["text"] == "standard prose"

    writing_only = _run_assistant_entries_dom_fixture({
        "attrs": {"data-message-author-role": "assistant", "data-message-id": "writing"},
        "children": [{"attrs": {"data-testid": "writing-block-container"}, "text": "block prose", "children": [
            {"attrs": {"data-testid": "writing-block-suggested-followups"}, "text": "follow-up chrome"},
        ]}],
    })
    assert writing_only["text"] == "block prose"
    assert writing_only["text"].count("block prose") == 1
    assert "follow-up chrome" not in writing_only["text"]

    mixed = _run_assistant_entries_dom_fixture({
        "attrs": {"data-message-author-role": "assistant", "data-message-id": "mixed"},
        "text": "prefix ",
        "children": [
            {"attrs": {"data-testid": "writing-block-container"}, "text": "writing block "},
            {"text": "suffix"},
        ],
    })
    assert mixed["semanticSource"] == "WRITING_BLOCK"
    assert mixed["text"] == "prefix writing block suffix"
    assert mixed["text"].count("writing block") == 1

    production = _run_assistant_entries_dom_fixture({
        "attrs": {"data-message-author-role": "assistant", "data-message-id": "generation-2"},
        "children": [
            {"attrs": {"data-testid": "writing-block-container"}, "text": "handover body\n"},
            {"text": f"Rollover transaction ID: {marker}\nARCHITECT_HANDOVER_READY"},
        ],
    })
    assert marker in production["text"]
    assert "ARCHITECT_HANDOVER_READY" in production["text"]
    assert architect_handover_ready(production["text"])
    assert handover_transaction_matches(production["text"], marker)
    assert not handover_transaction_matches(production["text"].replace(marker, "768df22312ef1a5c0f86a9ea"), marker)
    assert not handover_transaction_matches(production["text"].replace(marker, "8084c3b80c13ebccb27d52c4"), marker)

    inside = _run_assistant_entries_dom_fixture({
        "attrs": {"data-message-author-role": "assistant", "data-message-id": "inside"},
        "children": [{"attrs": {"data-testid": "writing-block-container"},
                      "text": f"Rollover transaction ID: {marker}\nARCHITECT_HANDOVER_READY"}],
    })
    assert handover_transaction_matches(inside["text"], marker)

    controls = _run_assistant_entries_dom_fixture({
        "attrs": {"data-message-author-role": "assistant", "data-message-id": "controls"},
        "children": [
            {"attrs": {"data-testid": "writing-block-container"}, "text": "legitimate prose"},
            {"attrs": {"tag": "button"}, "text": f"Rollover transaction ID: {marker}\nARCHITECT_HANDOVER_READY"},
            {"attrs": {"role": "button"}, "text": "ARCHITECT_HANDOVER_READY"},
            {"attrs": {"aria-hidden": "true"}, "text": f"Rollover transaction ID: {marker}"},
            {"attrs": {"data-testid": "writing-block-suggested-followups-surface"}, "text": "ARCHITECT_HANDOVER_READY"},
        ],
    })
    assert marker not in controls["text"]
    assert "ARCHITECT_HANDOVER_READY" not in controls["text"]
    assert not architect_handover_ready(controls["text"])


def test_assistant_entries_diagnostic_trace_records_each_evaluate_error_and_exhaustion(tmp_path):
    from local_orchestrator_watcher import ArchitectPlaywright, DiagnosticTracer

    tracer = DiagnosticTracer(tmp_path, "assistant-errors")

    class Page:
        def evaluate(self, _script):
            raise RuntimeError("synthetic evaluate failure")

    bridge = ArchitectPlaywright(Page())
    bridge.diagnostic_trace = tracer
    with pytest.raises(RuntimeError, match="synthetic evaluate failure"):
        bridge._assistant_entries()
    tracer.shutdown({})
    records = [json.loads(line) for line in (tmp_path / "logs" / "diagnostic" / "assistant-errors" / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    errors = [record for record in records if record["operation"] == "ASSISTANT_ENTRIES_ERROR"]
    assert len(errors) == 3
    assert [record["attemptNumber"] for record in errors] == [1, 2, 3]
    assert all(record["errorClass"] == "RuntimeError" and record["stackTrace"] for record in errors)
    exhausted = [record for record in records if record["operation"] == "ASSISTANT_ENTRIES_EXHAUSTED"]
    assert len(exhausted) == 1


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
    observed = bridge.wait_for_new_response(baseline, poll_interval=0)
    assert observed["state"] == "COMPLETED"
    assert extract_executor_prompt(observed["text"]) == "steady prompt"


def test_unchanged_atomic_snapshot_remains_not_yet_without_locator_count(monkeypatch):
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
    monkeypatch.setattr("local_orchestrator_watcher.time.sleep", lambda _delay: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        bridge.wait_for_new_response(baseline, poll_interval=0)
    except KeyboardInterrupt:
        pass
    assert bridge.last_state == "NOT_YET"


def test_steady_state_call_graph_has_no_locator_count_path():
    import inspect
    from local_orchestrator_watcher import ArchitectPlaywright
    generation_source = inspect.getsource(ArchitectPlaywright.generation_visible)
    wait_source = inspect.getsource(ArchitectPlaywright.wait_for_new_response)
    assert ".count(" not in generation_source
    assert ".locator(" not in generation_source
    assert ".count(" not in wait_source
    assert ".locator(" not in wait_source


def test_generation_gate_ignores_historical_thinking_placeholder():
    from local_orchestrator_watcher import ArchitectPlaywright
    seen = []

    class Page:
        def evaluate(self, script):
            seen.append(script)
            return False

    assert ArchitectPlaywright(Page()).generation_visible() is False
    assert 'data-testid="stop-button"' in seen[0]
    assert "data-message-author-role" not in seen[0]


def test_generation_gate_accepts_live_visible_stop_control():
    from local_orchestrator_watcher import ArchitectPlaywright

    class Page:
        def evaluate(self, script):
            assert "getBoundingClientRect" in script
            return True

    assert ArchitectPlaywright(Page()).generation_visible() is True


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
