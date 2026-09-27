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


class _OwnedPage:
    def __init__(self, raw: Any, context: "_OwnedContext", close_requests: list[str]):
        self._raw = raw
        self._owned_context = context
        self._close_requests = close_requests
        self._affotech_qualification_owned = True

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
        return self._raw.goto(url, wait_until="domcontentloaded", timeout=30000, **kwargs)

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
    def __init__(self, raw: Any, close_requests: list[str], write_event):
        self._raw = raw
        self._close_requests = close_requests
        self._write_event = write_event
        self._pages: list[_OwnedPage] = []
        self.inventory_complete = False

    @property
    def pages(self) -> list[_OwnedPage]:
        # Production coordinator is intentionally unable to inspect any
        # pre-existing (including protected) browser page.
        return [page for page in self._pages if not page.closed]

    def new_page(self) -> _OwnedPage:
        if not self.inventory_complete:
            raise RuntimeError("QUALIFICATION_CDP_INVENTORY_REQUIRED")
        if len(self._pages) >= 2:
            raise RuntimeError("QUALIFICATION_OWNED_PAGE_LIMIT_EXCEEDED")
        raw = self._raw.new_page()
        page = _OwnedPage(raw, self, self._close_requests)
        self._pages.append(page)
        self._write_event("QUALIFICATION_OWNED_PAGE_CREATED", {
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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


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

    log_path.parent.mkdir(parents=True, exist_ok=False)
    handle = log_path.open("x", encoding="utf-8", newline="\n")
    handle.write("runtimeContext=QUALIFICATION realBrowser=true fakeExecutor=true "
                 "productionStateMutation=false productionArchitectMutation=false\n")
    handle.flush()
    os.fsync(handle.fileno())
    _write_event(handle, "QUALIFICATION_PREFLIGHT", {"runId": run_id, "endpoint": endpoint,
        "productionStateSha256": state_hash_before, "productionStateBytes": state_size_before,
        "productionLogSha256": production_log_hash_before, "productionLogBytes": production_log_size_before})

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

        def event(name: str, fields: dict[str, Any]):
            _write_event(handle, name, fields)

        owned_context = _OwnedContext(contexts[0], close_requests, event)
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
        evidence = {
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
        }
        _atomic_json(evidence_path, evidence)
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
        handle.close()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--run":
        raise SystemExit("usage: orchestrator_real_browser_qualification.py --run REPOSITORY CDP_ENDPOINT")
    print(json.dumps(run(sys.argv[2], sys.argv[3]), indent=2, sort_keys=True))
