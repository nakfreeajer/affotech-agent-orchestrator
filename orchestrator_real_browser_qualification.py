"""One-run disposable Brave/CDP qualification using the production rollover path.

This harness never opens the production state as workflow authority. It connects
to the configured browser only after recording a read-only target inventory,
then exposes solely pages created by this run to the real rollover coordinator.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import local_orchestrator_watcher as runtime


def _qualification_page_marker(run_id: str, role: str, nonce: str) -> str:
    if role not in {"OLD_ARCHITECT", "FRESH_ARCHITECT"} or not run_id or not nonce:
        raise RuntimeError("QUALIFICATION_PAGE_MARKER_INPUT_INVALID")
    if any(":" in part for part in (run_id, role, nonce)):
        raise RuntimeError("QUALIFICATION_PAGE_MARKER_INPUT_INVALID")
    return f"AFFOTECH_QUALIFICATION:{run_id}:{role}:{nonce}"


def _cdp_target_id(context: Any, page: Any) -> str:
    """Read Chrome's stable target identity through a short-lived CDP session."""
    session = None
    try:
        session = context.new_cdp_session(page)
        info = session.send("Target.getTargetInfo")
        target_info = info.get("targetInfo") if isinstance(info, dict) else None
        target_id = target_info.get("targetId") if isinstance(target_info, dict) else None
        if not isinstance(target_id, str) or not target_id:
            raise RuntimeError("QUALIFICATION_CDP_TARGET_ID_UNAVAILABLE")
        return target_id
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError("QUALIFICATION_CDP_TARGET_ID_UNAVAILABLE") from error
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


def find_owned_qualification_page(browser: Any, evidence: dict[str, Any], role: str) -> Any:
    """Find exactly one page by persisted marker, with CDP target ID cross-check."""
    run_id = evidence.get("runId")
    rows = evidence.get("ownedPages")
    if not isinstance(run_id, str) or not run_id or not isinstance(rows, list):
        raise RuntimeError("QUALIFICATION_PAGE_EVIDENCE_INVALID")
    if role not in {"OLD_ARCHITECT", "FRESH_ARCHITECT"}:
        raise RuntimeError("QUALIFICATION_PAGE_ROLE_INVALID")
    role_rows = [row for row in rows if isinstance(row, dict) and row.get("role") == role]
    if len(role_rows) != 1:
        raise RuntimeError("QUALIFICATION_PAGE_EVIDENCE_ROLE_AMBIGUOUS")
    row = role_rows[0]
    nonce = row.get("nonce")
    if not isinstance(nonce, str) or not nonce or row.get("runId") != run_id:
        raise RuntimeError("QUALIFICATION_PAGE_EVIDENCE_IDENTITY_INVALID")
    marker = _qualification_page_marker(run_id, role, nonce)
    if row.get("marker") != marker or row.get("markerVerified") is not True:
        raise RuntimeError("QUALIFICATION_PAGE_EVIDENCE_MARKER_INVALID")
    target_id = row.get("cdpTargetId")
    if target_id is not None and (not isinstance(target_id, str) or not target_id):
        raise RuntimeError("QUALIFICATION_PAGE_EVIDENCE_TARGET_ID_INVALID")

    protected = set(runtime.QUALIFICATION_PROTECTED_CONVERSATION_IDS)
    protected.update(str(value) for value in evidence.get("protectedConversationIds", []) if value)
    marker_matches = []
    try:
        contexts = list(browser.contexts)
    except Exception as error:
        raise RuntimeError("QUALIFICATION_PAGE_INVENTORY_FAILED") from error
    for context in contexts:
        for page in list(context.pages):
            url = str(getattr(page, "url", "") or "")
            try:
                conversation_id = runtime.architect_conversation_id_from_url(url)
            except (RuntimeError, StopIteration, TypeError):
                conversation_id = None
            # Never evaluate or return a protected production page.
            if conversation_id in protected:
                if row.get("conversationId") == conversation_id:
                    raise RuntimeError("QUALIFICATION_PROTECTED_PAGE_IDENTITY_CONFLICT")
                continue
            try:
                page_marker = page.evaluate("() => window.name")
            except Exception as error:
                raise RuntimeError("QUALIFICATION_PAGE_MARKER_READ_FAILED") from error
            if page_marker == marker:
                marker_matches.append((page, context, conversation_id))
    if len(marker_matches) > 1:
        raise RuntimeError("QUALIFICATION_OWNED_PAGE_AMBIGUOUS")
    if not marker_matches:
        raise RuntimeError("QUALIFICATION_OWNED_PAGE_NOT_FOUND")
    page, context, conversation_id = marker_matches[0]
    expected_conversation = row.get("conversationId")
    if expected_conversation and conversation_id and expected_conversation != conversation_id:
        raise RuntimeError("QUALIFICATION_PAGE_CONVERSATION_ID_MISMATCH")
    if target_id is not None:
        try:
            observed_target_id = _cdp_target_id(context, page)
        except RuntimeError:
            raise
        if observed_target_id != target_id:
            raise RuntimeError("QUALIFICATION_PAGE_TARGET_ID_MISMATCH")
    return page


class _OwnedPage:
    def __init__(self, raw: Any, context: "_OwnedContext", close_requests: list[str],
                 identity: dict[str, Any], persist_evidence):
        self._raw = raw
        self._owned_context = context
        self._close_requests = close_requests
        self.identity = identity
        self._persist_evidence = persist_evidence
        self._affotech_qualification_owned = True

    def update_location(self) -> None:
        url = str(self._raw.url or "")
        try:
            conversation_id = runtime.architect_conversation_id_from_url(url)
        except (RuntimeError, StopIteration, TypeError):
            conversation_id = None
        self.identity["currentUrl"] = url
        self.identity["conversationId"] = conversation_id
        self._persist_evidence()
        current_marker = self._raw.evaluate("() => window.name")
        if current_marker != self.identity["marker"]:
            # Chrome may clear window.name on cross-site navigation. Restore it
            # only on this qualification-created page, then verify readback.
            current_marker = self._raw.evaluate(
                "marker => { window.name = marker; return window.name; }", self.identity["marker"])
        if current_marker != self.identity["marker"]:
            raise RuntimeError("QUALIFICATION_PAGE_MARKER_READBACK_FAILED")
        self.identity["markerVerified"] = True
        try:
            self.identity["cdpTargetId"] = _cdp_target_id(self._owned_context._raw, self._raw)
        except RuntimeError as error:
            self.identity["cdpTargetId"] = None
            self.identity["cdpTargetIdCaptureError"] = str(error)
        self._persist_evidence()

    @property
    def context(self):
        return self._owned_context

    @property
    def url(self):
        return self._raw.url

    @property
    def closed(self):
        return self._raw.is_closed()

    def goto(self, url: str, **kwargs: Any):
        runtime._qualification_assert_page_allowed(self, "navigate")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
            raise RuntimeError("QUALIFICATION_NAVIGATION_ORIGIN_BLOCKED")
        result = self._raw.goto(url, wait_until="domcontentloaded", timeout=30000, **kwargs)
        self.update_location()
        return result

    def close(self):
        runtime._qualification_assert_page_allowed(self, "close")
        self._close_requests.append("DEFERRED:" + str(self.url))

    def close_after_evidence(self):
        runtime._qualification_assert_page_allowed(self, "close")
        if not self._raw.is_closed():
            self._raw.close(run_before_unload=False)

    def __getattr__(self, key: str):
        return getattr(self._raw, key)


class _OwnedContext:
    def __init__(self, raw: Any, close_requests: list[str], write_event, *, run_id: str,
                 evidence: dict[str, Any], persist_evidence):
        self._raw = raw
        self._close_requests = close_requests
        self._write_event = write_event
        self._run_id = run_id
        self._evidence = evidence
        self._persist_evidence = persist_evidence
        self._pages: list[_OwnedPage] = []
        self.inventory_complete = False

    @property
    def pages(self) -> list[_OwnedPage]:
        # Production coordinator is intentionally unable to inspect any
        # pre-existing (including protected) browser page.
        return [page for page in self._pages if not page.closed]

    def new_page(self, role: str | None = None) -> _OwnedPage:
        if not self.inventory_complete:
            raise RuntimeError("QUALIFICATION_CDP_INVENTORY_REQUIRED")
        if len(self._pages) >= 2:
            raise RuntimeError("QUALIFICATION_OWNED_PAGE_LIMIT_EXCEEDED")
        expected_role = "OLD_ARCHITECT" if not self._pages else "FRESH_ARCHITECT"
        role = role or expected_role
        if role != expected_role:
            raise RuntimeError("QUALIFICATION_PAGE_ROLE_ORDER_INVALID")
        raw = self._raw.new_page()
        nonce = uuid.uuid4().hex
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        marker = _qualification_page_marker(self._run_id, role, nonce)
        identity = {
            "runId": self._run_id, "role": role, "nonce": nonce,
            "creationTimestamp": created_at, "currentUrl": str(raw.url or ""),
            "conversationId": None, "marker": marker, "markerVerified": False,
            "cdpTargetId": None, "browserIdentityType": "CDP_TARGET_ID",
            "playwrightGuidDiagnosticOnly": str(getattr(getattr(raw, "_impl_obj", None), "_guid", "UNAVAILABLE")),
        }
        page = _OwnedPage(raw, self, self._close_requests, identity, self._persist_evidence)
        self._pages.append(page)
        self._evidence.setdefault("ownedPages", []).append(identity)
        self._persist_evidence()
        try:
            initial_id = runtime.architect_conversation_id_from_url(str(raw.url or ""))
        except (RuntimeError, StopIteration, TypeError):
            initial_id = None
        if initial_id in runtime.QUALIFICATION_PROTECTED_CONVERSATION_IDS:
            raise RuntimeError("QUALIFICATION_PROTECTED_PAGE_CREATION_BLOCKED")
        readback = raw.evaluate("marker => { window.name = marker; return window.name; }", marker)
        if readback != marker:
            raise RuntimeError("QUALIFICATION_PAGE_MARKER_READBACK_FAILED")
        identity["markerVerified"] = True
        self._persist_evidence()
        try:
            identity["cdpTargetId"] = _cdp_target_id(self._raw, raw)
        except RuntimeError as error:
            identity["cdpTargetId"] = None
            identity["cdpTargetIdCaptureError"] = str(error)
        page.update_location()
        self._write_event("QUALIFICATION_OWNED_PAGE_CREATED", {
            "runId": self._run_id, "role": role, "nonce": nonce,
            "creationTimestamp": created_at, "markerVerified": True,
            "pageGuid": str(getattr(getattr(raw, "_impl_obj", None), "_guid", "UNAVAILABLE")),
            "url": str(raw.url), "pageCount": len(self._pages),
        })
        return page


def _sha(path: Path) -> tuple[str | None, int | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    return hashlib.sha256(data).hexdigest(), len(data)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


_QUALIFICATION_INPUT_FILES = {
    "OLD_ARCHITECT_REQUEST": "old-architect-request.txt",
    "FRESH_ARCHITECT_BOOTSTRAP": "fresh-architect-bootstrap.txt",
    "POST_DISCUSSION_REPAIR_REQUEST": "post-discussion-repair-request.txt",
}


def _persist_qualification_input(root: Path, evidence: dict[str, Any], stage: str,
                                 payload: str, task_id: str, transaction_id: str | None,
                                 persist_evidence, *, count_send_attempt: bool = True) -> dict[str, Any]:
    filename = _QUALIFICATION_INPUT_FILES.get(stage)
    if filename is None or not isinstance(payload, str):
        raise RuntimeError("QUALIFICATION_INPUT_STAGE_INVALID")
    payload_bytes = payload.encode("utf-8")
    path = root / "inputs" / filename
    if path.exists() and path.read_bytes() != payload_bytes:
        raise RuntimeError("QUALIFICATION_INPUT_IDENTITY_CONFLICT:" + stage)
    if not path.exists():
        _atomic_write_bytes(path, payload_bytes)
    inputs = evidence.setdefault("inputs", {})
    prior = inputs.get(stage, {})
    metadata = {
        "stage": stage,
        "createdAt": prior.get("createdAt") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "payloadSha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payloadByteLength": len(payload_bytes),
        "payloadCharacterLength": len(payload),
        "taskId": task_id,
        "transactionId": transaction_id,
        "exactPayloadFilePath": str(path),
        "sendAttemptCount": int(prior.get("sendAttemptCount", 0)) + int(count_send_attempt),
    }
    inputs[stage] = metadata
    if stage == "OLD_ARCHITECT_REQUEST":
        evidence.update({
            "oldArchitectRequestSha256": metadata["payloadSha256"],
            "oldArchitectRequestPath": str(path),
        })
    persist_evidence()
    return metadata


def _persist_static_qualification_input(root: Path, evidence: dict[str, Any], name: str,
                                        payload: str, task_id: str, transaction_id: str,
                                        persist_evidence) -> dict[str, Any]:
    """Persist synthetic input bytes and their identity before browser work."""
    if not name or Path(name).name != name or not isinstance(payload, str):
        raise RuntimeError("QUALIFICATION_STATIC_INPUT_INVALID")
    payload_bytes = payload.encode("utf-8")
    path = root / "inputs" / name
    if path.exists() and path.read_bytes() != payload_bytes:
        raise RuntimeError("QUALIFICATION_INPUT_IDENTITY_CONFLICT:" + name)
    if not path.exists():
        _atomic_write_bytes(path, payload_bytes)
    metadata = {
        "stage": name,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "payloadSha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payloadByteLength": len(payload_bytes),
        "payloadCharacterLength": len(payload),
        "taskId": task_id,
        "transactionId": transaction_id,
        "exactPayloadFilePath": str(path),
    }
    evidence.setdefault("inputs", {})[name] = metadata
    persist_evidence()
    return metadata


def _persist_initial_qualification_inputs(root: Path, evidence: dict[str, Any], task_id: str,
                                         transaction_id: str, staged_prompt: str,
                                         expected_handover: str, old_request: str,
                                         persist_evidence) -> None:
    evidence.update({
        "taskId": task_id,
        "transactionId": transaction_id,
        "stagedPromptSha256": hashlib.sha256(staged_prompt.encode("utf-8")).hexdigest(),
        "expectedHandoverSha256": hashlib.sha256(expected_handover.encode("utf-8")).hexdigest(),
    })
    _persist_static_qualification_input(root, evidence, "staged-prompt.txt", staged_prompt,
                                         task_id, transaction_id, persist_evidence)
    _persist_static_qualification_input(root, evidence, "expected-handover.txt", expected_handover,
                                         task_id, transaction_id, persist_evidence)
    _persist_qualification_input(root, evidence, "OLD_ARCHITECT_REQUEST", old_request,
                                 task_id, transaction_id, persist_evidence, count_send_attempt=False)


class _QualificationInputSubmitter:
    """Persist exact synthetic payload/evidence before delegating each send."""
    def __init__(self, original_submit, root: Path, evidence: dict[str, Any], persist_evidence,
                 task_id: str, transaction_id: str, old_request: str):
        self.original_submit = original_submit
        self.root = root
        self.evidence = evidence
        self.persist_evidence = persist_evidence
        self.task_id = task_id
        self.transaction_id = transaction_id
        self.old_request = old_request
        self.bootstrap_payload: str | None = None
        self.stage_hint: str | None = None

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return lambda payload, timeout=30.0: self(instance, payload, timeout)

    def __call__(self, bridge: Any, payload: str, timeout: float = 30.0):
        if payload == self.old_request:
            stage = "OLD_ARCHITECT_REQUEST"
        elif self.bootstrap_payload is not None and payload == self.bootstrap_payload:
            stage = "FRESH_ARCHITECT_BOOTSTRAP"
        elif self.stage_hint == "POST_DISCUSSION_REPAIR_REQUEST":
            stage = self.stage_hint
        else:
            raise RuntimeError("QUALIFICATION_UNCLASSIFIED_BROWSER_SEND")
        metadata = _persist_qualification_input(
            self.root, self.evidence, stage, payload, self.task_id, self.transaction_id,
            self.persist_evidence)
        original_reader = bridge.read_live_composer_payload
        had_instance_reader = "read_live_composer_payload" in getattr(bridge, "__dict__", {})
        read_count = 0
        submit_started = time.monotonic()

        def recording_reader(composer=None):
            nonlocal read_count
            observed = original_reader(composer)
            read_count += 1
            observed_bytes = observed.encode("utf-8") if isinstance(observed, str) else b""
            passed = isinstance(observed, str) and runtime.composer_payload_matches(observed, payload)
            metadata.update({
                "composerExpectedSha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "composerObservedSha256": hashlib.sha256(observed_bytes).hexdigest() if isinstance(observed, str) else None,
                "composerExpectedLength": len(payload),
                "composerObservedLength": len(observed) if isinstance(observed, str) else None,
                "composerAcceptancePassed": passed,
                "composerReadAttempts": read_count,
            })
            if passed and "composerAcceptanceLatencyMs" not in metadata:
                metadata["composerAcceptanceLatencyMs"] = round((time.monotonic() - submit_started) * 1000)
            self.persist_evidence()
            return observed

        bridge.read_live_composer_payload = recording_reader
        try:
            return self.original_submit(bridge, payload, timeout)
        finally:
            for attr, field in (
                ("sendReadinessLatencyMs", "sendReadinessLatencyMs"),
                ("sendReadinessAttempts", "sendReadinessAttempts"),
                ("sendCandidateCount", "sendCandidateCount"),
                ("sendNodeReplacementObserved", "sendNodeReplacementObserved"),
                ("sendEnabledInitially", "sendEnabledInitially"),
                ("sendEnabledEventually", "sendEnabledEventually"),
                ("sendMethod", "sendMethod"),
            ):
                if hasattr(bridge, attr):
                    metadata[field] = getattr(bridge, attr)
            if metadata:
                self.persist_evidence()
            if had_instance_reader:
                bridge.read_live_composer_payload = original_reader
            else:
                try:
                    delattr(bridge, "read_live_composer_payload")
                except AttributeError:
                    pass


def _write_event(handle, event: str, fields: dict[str, Any]) -> None:
    safe = {key: value for key, value in fields.items() if key.lower() not in {"text", "payload", "prompt", "handover"}}
    handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} runtimeContext=QUALIFICATION event={event} "
                 f"{json.dumps(safe, sort_keys=True, default=str)}\n")
    handle.flush()
    os.fsync(handle.fileno())


def _target_inventory(browser) -> list[dict[str, Any]]:
    targets = []
    for context_index, context in enumerate(browser.contexts):
        for page in context.pages:
            url = str(page.url or "")
            try:
                conversation_id = runtime.architect_conversation_id_from_url(url)
            except (RuntimeError, StopIteration, TypeError):
                conversation_id = None
            targets.append({"contextIndex": context_index, "pageGuid": str(getattr(getattr(page, "_impl_obj", None), "_guid", "UNAVAILABLE")),
                            "url": url, "conversationId": conversation_id})
    return targets


def run(repository: str | os.PathLike[str], endpoint: str) -> dict[str, Any]:
    repository = Path(repository).resolve()
    run_id = "qualification-real-" + uuid.uuid4().hex
    root = repository / ".agent-work" / "orchestrator" / "qualification" / run_id
    state_dir = root / "state"
    (state_dir / "prompts").mkdir(parents=True, exist_ok=False)
    (state_dir / "results").mkdir(parents=True, exist_ok=True)
    (state_dir / "logs").mkdir(parents=True, exist_ok=True)
    log_path = repository / ".agent-work" / "orchestrator" / "logs" / "qualification" / run_id / "qualification.log"
    evidence_path = root / "qualification-evidence.json"
    state_path = repository / ".agent-work" / "orchestrator" / "state.json"
    production_log = repository / ".agent-work" / "orchestrator" / "logs" / "orchestrator.log"
    state_hash_before, state_size_before = _sha(state_path)
    production_log_hash_before, production_log_size_before = _sha(production_log)
    if state_hash_before is None:
        raise RuntimeError("QUALIFICATION_PRODUCTION_STATE_INTEGRITY_BASELINE_UNAVAILABLE")

    evidence: dict[str, Any] = {
        "runId": run_id, "runtimeContext": "QUALIFICATION", "realBrowser": True,
        "fakeExecutor": True, "productionStateMutation": False,
        "productionArchitectMutation": False, "productionStateSha256Before": state_hash_before,
        "productionStateBytesBefore": state_size_before,
        "productionLogSha256Before": production_log_hash_before,
        "productionLogBytesBefore": production_log_size_before,
        "ownedPages": [], "preExistingTargets": [], "protectedConversationIds": [],
    }

    def persist_evidence() -> None:
        _atomic_json(evidence_path, evidence)

    log_path.parent.mkdir(parents=True, exist_ok=False)
    handle = log_path.open("x", encoding="utf-8", newline="\n")
    handle.write("runtimeContext=QUALIFICATION realBrowser=true fakeExecutor=true "
                 "productionStateMutation=false productionArchitectMutation=false\n")
    handle.flush()
    os.fsync(handle.fileno())
    _write_event(handle, "QUALIFICATION_PREFLIGHT", {"runId": run_id, "endpoint": endpoint,
        "productionStateSha256": state_hash_before, "productionStateBytes": state_size_before,
        "productionLogSha256": production_log_hash_before, "productionLogBytes": production_log_size_before})
    persist_evidence()

    previous = {key: os.environ.get(key) for key in (
        "AFFOTECH_RUNTIME_CONTEXT", "AFFOTECH_QUALIFICATION_RUN_ID", "AFFOTECH_QUALIFICATION_RUN_ROOT",
        "AFFOTECH_QUALIFICATION_LOG_ROOT", "AFFOTECH_QUALIFICATION_LOG_PATH",
        "AFFOTECH_QUALIFICATION_OWNED_IDS", "AFFOTECH_QUALIFICATION_PROTECTED_IDS",
        "AFFOTECH_ORCHESTRATOR_STATE_DIR")}
    playwright_runtime = None
    browser = None
    pages: list[_OwnedPage] = []
    close_requests: list[str] = []
    logger = None
    committed = False
    original_submit_impl = runtime.ArchitectPlaywright.submit_result_bounded
    input_submitter = None
    try:
        os.environ.update({
            "AFFOTECH_RUNTIME_CONTEXT": "QUALIFICATION",
            "AFFOTECH_QUALIFICATION_RUN_ID": run_id,
            "AFFOTECH_QUALIFICATION_RUN_ROOT": str(root),
            "AFFOTECH_QUALIFICATION_LOG_ROOT": str(log_path.parent),
            "AFFOTECH_QUALIFICATION_LOG_PATH": str(log_path),
            "AFFOTECH_QUALIFICATION_OWNED_IDS": "",
            "AFFOTECH_QUALIFICATION_PROTECTED_IDS": ",".join(sorted(runtime.QUALIFICATION_PROTECTED_CONVERSATION_IDS)),
            "AFFOTECH_ORCHESTRATOR_STATE_DIR": str(state_dir),
        })
        from playwright.sync_api import sync_playwright
        playwright_runtime = sync_playwright().start()
        browser = playwright_runtime.chromium.connect_over_cdp(endpoint, timeout=10000)
        contexts = list(browser.contexts)
        if len(contexts) != 1:
            raise RuntimeError(f"QUALIFICATION_CDP_CONTEXT_AMBIGUOUS:{len(contexts)}")
        before_targets = _target_inventory(browser)
        before_guids = {item["pageGuid"] for item in before_targets}
        protected = set(runtime.QUALIFICATION_PROTECTED_CONVERSATION_IDS)
        protected.update(item["conversationId"] for item in before_targets if item["conversationId"])
        os.environ["AFFOTECH_QUALIFICATION_PROTECTED_IDS"] = ",".join(sorted(protected))
        if not all(isinstance(item.get("url"), str) for item in before_targets):
            raise RuntimeError("QUALIFICATION_CDP_INVENTORY_INCOMPLETE")
        _write_event(handle, "QUALIFICATION_CDP_INVENTORY_RECORDED", {"pageCount": len(before_targets),
            "targets": before_targets, "protectedIds": sorted(protected), "preExistingPageGuids": sorted(before_guids)})
        evidence["preExistingTargets"] = before_targets
        evidence["protectedConversationIds"] = sorted(protected)
        persist_evidence()

        def event(name: str, fields: dict[str, Any]):
            _write_event(handle, name, fields)

        owned_context = _OwnedContext(contexts[0], close_requests, event, run_id=run_id,
                                      evidence=evidence, persist_evidence=persist_evidence)
        owned_context.inventory_complete = True
        event("QUALIFICATION_BROWSER_GUARD_READY", {"realBrowser": True, "preExistingPagesUntouchable": True})
        logger, runtime_run_id, runtime_log_path = runtime.initialize_runtime_logging(
            state_dir, run_id, "QUALIFICATION")
        if Path(runtime_log_path).resolve() != log_path.resolve():
            raise RuntimeError("QUALIFICATION_LOG_DESTINATION_MISMATCH")

        task_id = "QUAL-TASK-" + uuid.uuid4().hex[:12].upper()
        transaction_id = "QUAL-TX-" + uuid.uuid4().hex
        worktree = root / "synthetic-worktree"
        worktree.mkdir()
        staged_prompt = ("Synthetic qualification only. No business, customer, AFFOTECH, or production work.\n"
                         "This exact text is used only to test staged-prompt identity preservation.\n")
        prompt_path = state_dir / "prompts" / f"{task_id}.txt"
        prompt_path.write_text(staged_prompt, encoding="utf-8", newline="")
        prompt_hash = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
        handover_body = ("Synthetic disposable qualification handover.\n"
                         "Already-completed synthetic decision: classification=ACCEPTED; action=EXECUTE.\n"
                         f"The complete synthetic next prompt is exactly:\n{staged_prompt}"
                         "For the fresh session's formatting-only envelope request, preserve that decision and prompt exactly.\n")
        expected_handover = runtime.make_handover_envelope(transaction_id, task_id, handover_body)
        prompt_request = ("This is a disposable browser qualification, not business work. Return exactly the following "
                          "synthetic handover envelope as your entire response, with no Markdown fences or extra text:\n\n"
                          + expected_handover)

        _persist_initial_qualification_inputs(root, evidence, task_id, transaction_id,
                                              staged_prompt, expected_handover, prompt_request,
                                              persist_evidence)
        input_submitter = _QualificationInputSubmitter(
            original_submit_impl, root, evidence, persist_evidence, task_id, transaction_id, prompt_request)
        runtime.ArchitectPlaywright.submit_result_bounded = input_submitter

        old_page = owned_context.new_page()
        pages.append(old_page)
        old_page.goto("https://chatgpt.com/")
        try:
            old_initial_id = runtime.architect_conversation_id_from_url(old_page.url)
        except RuntimeError:
            old_initial_id = None
        if old_initial_id in protected:
            raise RuntimeError("QUALIFICATION_PROTECTED_CONVERSATION_BLOCKED:old-initial-redirect")
        old_bridge = runtime.ArchitectPlaywright(old_page)
        old_bridge.runtime_logger = logger
        old_bridge.runtime_run_id = runtime_run_id
        old_bridge.runtime_conversation_id = "QUALIFICATION_PENDING_OLD"
        old_baseline = old_bridge.assistant_baseline()
        old_bridge.submit_result_bounded(prompt_request, timeout=45.0)
        old_observed = old_bridge.wait_for_new_response(old_baseline, poll_interval=0.5)
        if not isinstance(old_observed, dict) or old_observed.get("state") != "COMPLETED":
            raise RuntimeError("QUALIFICATION_OLD_SYNTHETIC_HANDOVER_NOT_COMPLETED")
        handover = old_observed.get("text")
        if not isinstance(handover, str) or not runtime.parse_handover_envelope(handover):
            raise RuntimeError("QUALIFICATION_OLD_SYNTHETIC_HANDOVER_INVALID")
        old_id = runtime.architect_conversation_id_from_url(old_page.url)
        if old_id in protected:
            raise RuntimeError("QUALIFICATION_PROTECTED_CONVERSATION_BLOCKED:old-created")
        old_page.update_location()
        os.environ["AFFOTECH_QUALIFICATION_OWNED_IDS"] = old_id
        event("QUALIFICATION_OLD_SESSION_READY", {"conversationId": old_id,
            "handoverSha256": hashlib.sha256(handover.encode()).hexdigest(), "handoverValidated": True})

        # Recheck the entire CDP inventory before coordinator operations. Only
        # our page may have been added; no pre-existing page is exposed to it.
        after_old_targets = _target_inventory(browser)
        after_old_guids = {item["pageGuid"] for item in after_old_targets}
        own_guid = str(getattr(getattr(old_page._raw, "_impl_obj", None), "_guid", "UNAVAILABLE"))
        if not before_guids.issubset(after_old_guids) or own_guid == "UNAVAILABLE" or own_guid not in after_old_guids or len(after_old_guids) != len(before_guids) + 1:
            raise RuntimeError("QUALIFICATION_CDP_TARGET_SET_CHANGED_UNEXPECTEDLY")

        watcher = runtime.LocalFirstOrchestrator(str(repository), state_dir)
        watcher.runtime_logger = logger
        watcher.runtime_run_id = runtime_run_id
        watcher.state.update({
            "state": "NEXT_PROMPT_READY", "taskId": "QUAL-COMPLETE-" + uuid.uuid4().hex[:8].upper(),
            "nextTaskId": task_id, "lastCompletedTaskId": None, "nextPromptPath": str(prompt_path),
            "taskWorktrees": {task_id: {"taskId": task_id, "worktreePath": str(worktree)}},
            "rolloverDue": True, "rolloverPending": True, "rolloverInProgress": False,
            "handoverRequested": True, "handoverReady": False,
            "rolloverTransactionId": transaction_id, "rolloverTransactionTaskId": task_id,
            "rolloverAttemptedForTaskId": task_id, "rolloverMaintenanceState": "DEFERRED",
            "rolloverRecoveryState": "RECOVERING", "rolloverRecoveryEpoch": 1,
            "rolloverAutomaticRecoveryEpochCount": 1, "rolloverAutomaticRecoveryMaxEpochs": 3,
            "rolloverFreshPageCreated": False, "rolloverFreshCandidateConversationId": None,
            "pending_handover": None, "rolloverHandoverResponseIdentity": None,
            "postDiscussionEnvelopeRequired": True, "postDiscussionResumeEpoch": 1,
            "postDiscussionProtocolTaskId": task_id, "postDiscussionProtocolTransactionId": transaction_id,
            "postDiscussionProtocolRolloverCommittedTransactionId": None,
            "postDiscussionEnvelopeRepairAttempted": False, "postDiscussionEnvelopeRepairAwaiting": False,
            "postDiscussionEnvelopeRepairTaskId": task_id, "postDiscussionEnvelopeRepairEpoch": 1,
            "postDiscussionProtocolBaseline": {"count": 0, "text_hash": hashlib.sha256(b"").hexdigest()},
            "architectConversationId": old_id, "executorSessionId": "QUALIFICATION-FAKE-SESSION",
            "executorSessionMode": "QUALIFICATION_FAKE", "executorProcessState": "STOPPED",
            "executorLaunchState": "NOT_LAUNCHED", "executorActiveWriter": False,
            "governedExecutorActiveWriter": False, "discussionPauseActive": False,
        })
        watcher.state["lastCompletedTaskId"] = watcher.state["taskId"]
        watcher.save()
        old_bridge.runtime_watcher = watcher
        watcher.session_rollover = runtime.ArchitectSessionRollover(watcher)

        bootstrap_payload_hash = None
        forced_ack = {"done": False}
        bootstrap_payload = runtime.fresh_architect_bootstrap_payload(handover)
        input_submitter.bootstrap_payload = bootstrap_payload
        original_submit = runtime.ArchitectPlaywright.submit_result_bounded
        def submit_then_induce_ack_ambiguity(bridge, payload: str, timeout: float = 30.0):
            nonlocal bootstrap_payload_hash
            original_submit(bridge, payload, timeout)
            if payload.startswith(handover) and "Fresh Architect session bootstrap protocol:" in payload and not forced_ack["done"]:
                forced_ack["done"] = True
                bootstrap_payload_hash = hashlib.sha256(payload.encode()).hexdigest()
                event("QUALIFICATION_ACK_AMBIGUITY_INJECTED_AFTER_REAL_SEND", {"payloadSha256": bootstrap_payload_hash,
                    "sendActionCompleted": True, "observerOnly": True})
                raise runtime.ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT", "qualification observer-only injection")

        runtime.ArchitectPlaywright.submit_result_bounded = submit_then_induce_ack_ambiguity
        try:
            coordinator_ok = watcher.session_rollover.complete_from_response(old_bridge, handover)
        finally:
            runtime.ArchitectPlaywright.submit_result_bounded = original_submit
        if not coordinator_ok:
            raise RuntimeError("QUALIFICATION_FRESH_SESSION_COORDINATOR_FAILED")
        fresh_page = old_bridge.page
        if fresh_page is old_page or fresh_page not in owned_context.pages:
            raise RuntimeError("QUALIFICATION_FRESH_PAGE_OWNERSHIP_INVALID")
        pages.append(fresh_page)
        fresh_id = runtime.architect_conversation_id_from_url(fresh_page.url)
        if fresh_id in protected or fresh_id == old_id:
            raise RuntimeError("QUALIFICATION_FRESH_CONVERSATION_ID_INVALID")
        fresh_page.update_location()
        os.environ["AFFOTECH_QUALIFICATION_OWNED_IDS"] = old_id + "," + fresh_id
        event("QUALIFICATION_FRESH_SESSION_CREATED", {"conversationId": fresh_id, "freshTabCount": 1})
        event("QUALIFICATION_BOOTSTRAP_OBSERVED", {"payloadSha256": bootstrap_payload_hash,
            "userObserver": watcher.session_rollover._fresh_page_exact_bootstrap_visible(fresh_page,
                runtime.fresh_architect_bootstrap_payload(handover))[1]})
        ready, ready_source = watcher.session_rollover._fresh_page_ready_visible(fresh_page)
        event("QUALIFICATION_READY_OBSERVED", {"ready": ready, "observer": ready_source})
        if not ready:
            raise RuntimeError("QUALIFICATION_READY_NOT_PROVEN")
        if watcher.state.get("architectConversationId") != fresh_id:
            raise RuntimeError("QUALIFICATION_AUTHORITY_COMMIT_NOT_PROVEN")
        event("QUALIFICATION_AUTHORITY_COMMITTED", {"conversationId": fresh_id, "commitCount": 1,
            "oldArchitectRetirementRequested": old_page in [] or "DEFERRED:" + str(old_page.url) in close_requests})

        fresh_bridge = runtime.ArchitectPlaywright(fresh_page)
        fresh_bridge.runtime_logger = logger
        fresh_bridge.runtime_run_id = runtime_run_id
        fresh_bridge.runtime_watcher = watcher
        fresh_bridge.runtime_conversation_id = fresh_id
        input_submitter.stage_hint = "POST_DISCUSSION_REPAIR_REQUEST"
        if not watcher.request_post_discussion_envelope_repair(fresh_bridge):
            raise RuntimeError("QUALIFICATION_PROTOCOL_REPAIR_REQUEST_FAILED")
        repair_baseline = watcher.state.get("postDiscussionProtocolBaseline")
        observed_repair = fresh_bridge.wait_for_new_response(repair_baseline, poll_interval=0.5)
        if not isinstance(observed_repair, dict) or observed_repair.get("state") != "COMPLETED":
            raise RuntimeError("QUALIFICATION_SYNTHETIC_ENVELOPE_NOT_COMPLETED")
        disposition = watcher.reconcile_post_discussion_response(fresh_bridge, observed_repair.get("text", ""), completed=True)
        if disposition != "EXECUTE" or watcher.state.get("postDiscussionEnvelopeRequired") is not False:
            raise RuntimeError("QUALIFICATION_SYNTHETIC_ENVELOPE_NOT_ACCEPTED:" + str(disposition))
        event("QUALIFICATION_PROTOCOL_RECOVERED", {"disposition": disposition, "cleared": True,
            "promptSha256": prompt_hash})

        spy = runtime._QualificationExecutorLaunchSpy(watcher, logger, runtime_run_id)
        dispatched = runtime.run_next_prompt_ready_once(watcher, spy, endpoint, watcher.discussion_pause_active, logger, runtime_run_id)
        if dispatched is None or spy.count != 1:
            raise RuntimeError("QUALIFICATION_EXECUTOR_SPY_NOT_REACHED")
        if prompt_path.read_bytes() != staged_prompt.encode("utf-8"):
            raise RuntimeError("QUALIFICATION_STAGED_PROMPT_CHANGED")
        if len(pages) != 2 or spy.count != 1:
            raise RuntimeError("QUALIFICATION_DUPLICATE_SIDE_EFFECT")

        after_targets = _target_inventory(browser)
        after_state_hash, after_state_size = _sha(state_path)
        after_log_hash, after_log_size = _sha(production_log)
        if after_state_hash != state_hash_before or after_log_hash != production_log_hash_before:
            raise RuntimeError("QUALIFICATION_PRODUCTION_ARTIFACT_CHANGED")
        if {item["pageGuid"] for item in after_targets if item["pageGuid"] in before_guids} != before_guids:
            raise RuntimeError("QUALIFICATION_PREEXISTING_TARGET_LOST")
        evidence.update({
            "runId": run_id, "runtimeContext": "QUALIFICATION", "realBrowser": True, "fakeExecutor": True,
            "productionStateMutation": False, "productionArchitectMutation": False,
            "productionStateSha256Before": state_hash_before, "productionStateSha256After": after_state_hash,
            "productionLogSha256Before": production_log_hash_before, "productionLogSha256After": after_log_hash,
            "preExistingTargets": before_targets, "qualificationOldConversationId": old_id,
            "qualificationFreshConversationId": fresh_id, "taskId": task_id,
            "transactionId": transaction_id, "promptSha256": prompt_hash,
            "freshTabCount": 1, "freshBootstrapSendCount": 1, "freshBootstrapResendCount": 0,
            "ackAmbiguityExercised": forced_ack["done"], "authorityCommitCount": 1,
            "oldArchitectRetireCount": 1, "protocolRecovered": True,
            "executorLaunchSpyCount": spy.count, "closeRequestsDeferredUntilEvidence": close_requests,
            "workflowState": watcher.state,
            "terminalStatus": "QUALIFICATION_COMPLETE",
        })
        persist_evidence()
        _write_event(handle, "QUALIFICATION_EVIDENCE_PERSISTED", {"path": str(evidence_path),
            "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()})
        # Only now may this run close its two exact, durably-identified tabs.
        for page in pages:
            page.close_after_evidence()
        committed = True
        _write_event(handle, "QUALIFICATION_COMPLETE", {"oldConversationId": old_id,
            "freshConversationId": fresh_id, "executorLaunchSpyCount": spy.count})
        return evidence
    except Exception as error:
        evidence["terminalStatus"] = "QUALIFICATION_FAILED"
        evidence["terminalFailure"] = {"errorClass": type(error).__name__, "reason": str(error)[:300],
                                        "ownedPageCount": len(pages)}
        persist_evidence()
        _write_event(handle, "QUALIFICATION_FAILED_TABS_PRESERVED", {"errorClass": type(error).__name__,
            "reason": str(error)[:300], "ownedPageCount": len(pages), "evidencePath": str(evidence_path)})
        raise
    finally:
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if playwright_runtime is not None:
            try:
                playwright_runtime.stop()
            except Exception:
                pass
        runtime.ArchitectPlaywright.submit_result_bounded = original_submit_impl
        handle.close()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--run":
        raise SystemExit("usage: orchestrator_real_browser_qualification.py --run REPOSITORY CDP_ENDPOINT")
    print(json.dumps(run(sys.argv[2], sys.argv[3]), indent=2, sort_keys=True))
