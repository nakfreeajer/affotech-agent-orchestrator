"""Minimal local Architect ⇄ Codex watcher for the AFFOTECH workflow."""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from rollover_state_machine import RolloverAction, RolloverDecision, RolloverObservations, evaluate_rollover_state

COMPLETE = "ARCHITECT_RESPONSE_COMPLETE"
BEGIN = "EXECUTOR_PROMPT_BEGIN"
END = "EXECUTOR_PROMPT_END"
HANDOVER_BEGIN = "ARCHITECT_HANDOVER_BEGIN"
HANDOVER_END = "ARCHITECT_HANDOVER_END"
HANDOVER_READY = "ARCHITECT_HANDOVER_READY"
HANDOVER_OPEN = "<HANDOVER>"
HANDOVER_CLOSE = "</HANDOVER>"
HANDOVER_PROTOCOL_VERSION = 1
LEGACY_COMPAT_TRANSACTION_ID = "7fdbd798659f42295a18dd2d"
LEGACY_COMPAT_TASK_ID = "000103"
READY = "ARCHITECT_SESSION_READY"
DOCUMENTATION_SYNC = "DOCUMENTATION_SYNC_COMPLETE"
RELAY_REPOSITORY = "https://github.com/nakfreeajer/affotech-agent-relay.git"
RELAY_POINTER = "relay/current/LATEST_ARCHITECT_PROMPT.json"
RESULT_SCHEMA_VERSION = "1.0"
ARCHITECT_MEMORY_THRESHOLD_BYTES = 891_289_600
ARCHITECT_MEMORY_THRESHOLD_MIB = 850
ARCHITECT_MEMORY_SAFETY_CEILING_BYTES = ARCHITECT_MEMORY_THRESHOLD_BYTES * 2
ROLLOVER_RECOVERY_MAX_ATTEMPTS = 2
ROLLOVER_RECOVERY_WINDOW_SECONDS = 300.0
ROLLOVER_RECOVERY_RETRY_DELAY_SECONDS = 5.0
ROLLOVER_AUTOMATIC_RECOVERY_COOLDOWN_SECONDS = 5.0
ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS = 3
ROLLOVER_RECOVERY_ATTEMPT_ALLOWED = "ATTEMPT_ALLOWED"
ROLLOVER_RECOVERY_WAIT_BACKOFF = "WAIT_BACKOFF"
ROLLOVER_RECOVERY_EXHAUSTED = "EXHAUSTED"
_HANDOVER_RESPONSE_UNSET = object()
ROLLOVER_MEMORY_SAMPLE_COOLDOWN_SECONDS = 5.0
FRESH_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS = 2.0
FRESH_BOOTSTRAP_OBSERVATION_POLL_SECONDS = 0.1
AFFOTECH_EXECUTOR_SESSION_ID = "019f842e-98bc-7672-a619-51441d91be00"
VERIFIED_ARCHITECT_CONVERSATION_ID = "6a9d6645-eebc-83ec-8367-d193f1cb18e9"
ARCHITECT_CONVERSATION_URL_RE = re.compile(r"/c/([^/?#]+)")
RUNTIME_LOGGER_NAME = "affotech.orchestrator.runtime"
_ORIGINAL_ROTATING_FILE_HANDLER = logging.handlers.RotatingFileHandler
RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING = "DEFERRED_ARCHITECT_GENERATING"
ARCHITECT_DELIVERY_PROOF_VERSION = "SHA256_MARKER_V1"
DIAGNOSTIC_TRACE_ENV = "ORCHESTRATOR_DIAGNOSTIC_TRACE"
ROLLOVER_DIAGNOSTIC_ONLY_ENV = "ORCHESTRATOR_ROLLOVER_DIAGNOSTIC_ONLY"
DIAGNOSTIC_TRACE_MAX_BYTES = 100 * 1024 * 1024
DIAGNOSTIC_SNAPSHOT_MAX_BYTES = 50 * 1024 * 1024
_ACTIVE_DIAGNOSTIC_TRACE = None


class DiagnosticTracer:
    """Opt-in, bounded, privacy-conscious trace of already-executed work."""
    _sequence = 0
    _sequence_lock = threading.Lock()

    def __init__(self, state_dir: str | os.PathLike[str], run_id: str):
        self.root = Path(state_dir) / "logs" / "diagnostic" / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "snapshots").mkdir(exist_ok=True)
        self.path = self.root / "trace.jsonl"
        self._handle = self.path.open("a", encoding="utf-8")
        self._bytes = self.path.stat().st_size
        self._disabled = False
        self._lock = threading.Lock()
        self._connections = {}
        self._created = 0
        self._closed = 0

    @staticmethod
    def enabled() -> bool:
        return os.environ.get(DIAGNOSTIC_TRACE_ENV) == "1"

    def _state_fields(self, state: dict[str, Any] | None) -> dict[str, Any]:
        current = state or {}
        keys = (
            "state", "taskId", "lastCompletedTaskId", "nextTaskId", "rolloverDue", "rolloverPending",
            "rolloverInProgress", "rolloverAttemptedForTaskId", "rolloverRecoveryState",
            "rolloverRecoveryAttemptCount", "rolloverRecoveryStartedAt", "rolloverRecoveryLastAttemptAt",
            "rolloverRecoveryRetryAfter", "rolloverRecoveryTerminalReason", "rolloverTransactionId", "rolloverTransactionTaskId", "handoverRequested", "handoverReady",
            "rolloverHandoverSendState", "rolloverHandoverRecoveryDisposition", "rolloverHandoverRecoveryReason",
            "rolloverFreshCandidateConversationId", "rolloverFreshCandidateState", "rolloverFreshCandidateDiscoveryState",
            "rolloverFreshCandidateAttemptCount", "codexPid", "active_codex_pid", "lastExecutorPid",
            "executorProcessState", "architectConversationId", "architectMemoryBytes",
            "architectMemoryMiB", "architectMemoryOwnership", "architectMemoryOwnershipSource",
            "architectMemorySessionId", "rolloverTrigger", "rolloverMaintenanceState",
            "rolloverLastFailureReason", "rolloverDeferredForTaskId", "rolloverAutoAttemptCount",
            "rolloverRecoveryEpoch", "rolloverAutomaticRecoveryEpochCount",
            "rolloverAutomaticRecoveryNextEligibleAt", "rolloverAutomaticRecoveryMaxEpochs",
            "discussionPauseActive", "discussionPauseEpoch", "architectGenerating",
            "postDiscussionEnvelopeRequired", "postDiscussionResumeEpoch", "postDiscussionProtocolTaskId",
            "postDiscussionEnvelopeRepairAttempted", "postDiscussionEnvelopeRepairAwaiting",
            "postDiscussionEnvelopeRepairTaskId", "postDiscussionEnvelopeRepairEpoch", "postDiscussionProtocolFailure",
            "postDiscussionProtocolTransactionId",
            "postDiscussionResumePauseEpoch",
        )
        return {key: current.get(key) for key in keys}

    def record(self, component: str, function: str, operation: str, phase: str, state: dict[str, Any] | None = None, duration_ms: float | None = None, **fields: Any) -> None:
        if self._disabled:
            return
        with self._lock:
            with self._sequence_lock:
                type(self)._sequence += 1
                seq = type(self)._sequence
            record = {
                "seq": seq,
                "wallClockTimestamp": time.time(),
                "monotonicTimestamp": time.monotonic(),
                "runId": self.root.name,
                "processPid": os.getpid(),
                "threadId": threading.get_ident(),
                "threadName": threading.current_thread().name,
                "state": (state or {}).get("state", "UNKNOWN"),
                "taskId": (state or {}).get("taskId") or (state or {}).get("nextTaskId") or "NONE",
                "lastCompletedTaskId": (state or {}).get("lastCompletedTaskId"),
                "nextTaskId": (state or {}).get("nextTaskId"),
                "component": component,
                "function": function,
                "operation": operation,
                "phase": phase,
            }
            if duration_ms is not None:
                record["durationMs"] = round(float(duration_ms), 3)
            record.update({key: value for key, value in fields.items() if value is not None})
            record.setdefault("stateFields", self._state_fields(state))
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            encoded_bytes = len(encoded.encode("utf-8"))
            if self._bytes + encoded_bytes > DIAGNOSTIC_TRACE_MAX_BYTES:
                if not self._disabled:
                    self._disabled = True
                    cap = json.dumps({
                        "seq": seq, "wallClockTimestamp": time.time(), "monotonicTimestamp": time.monotonic(),
                        "runId": self.root.name, "processPid": os.getpid(), "threadId": threading.get_ident(),
                        "threadName": threading.current_thread().name, "state": record["state"], "taskId": record["taskId"],
                        "lastCompletedTaskId": record["lastCompletedTaskId"], "nextTaskId": record["nextTaskId"],
                        "component": "DIAGNOSTIC", "function": "DiagnosticTracer.record", "operation": "TRACE_LIMIT_REACHED",
                        "phase": "ERROR", "reason": "TRACE_SIZE_CAP"
                    }) + "\n"
                    self._handle.write(cap)
                    self._handle.flush()
                return
            self._handle.write(encoded)
            self._handle.flush()
            self._bytes += encoded_bytes

    def state_write(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        changed = {}
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                old, new = before.get(key), after.get(key)
                def summary(value):
                    if isinstance(value, str) and len(value) > 256:
                        return {"length": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest()}
                    return value
                changed[key] = [summary(old), summary(new)]
        if changed:
            self.record("STATE", "save", "STATE_WRITE", "END", after, changed=changed)

    def snapshot_text(self, label: str, text: str, raw_text: str | None = None) -> str | None:
        payload = str(text or "")
        raw_payload = str(raw_text if raw_text is not None else payload)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        directory = self.root / "snapshots"
        existing = sum(path.stat().st_size for path in directory.glob("*") if path.is_file())
        if existing + len(raw_payload.encode("utf-8")) + len(payload.encode("utf-8")) > DIAGNOSTIC_SNAPSHOT_MAX_BYTES:
            self.record("DIAGNOSTIC", "snapshot_text", "SNAPSHOT", "SKIP", {}, reason="SNAPSHOT_LIMIT_REACHED", sha256=digest, length=len(payload))
            return None
        raw_path = directory / f"{safe_label}-{digest[:12]}-rawText.txt"
        sanitized_path = directory / f"{safe_label}-{digest[:12]}-sanitizedText.txt"
        raw_path.write_text(raw_payload, encoding="utf-8")
        sanitized_path.write_text(payload, encoding="utf-8")
        return str(sanitized_path.relative_to(self.root))

    def connection_begin(self, endpoint: str, requested: str | None, state: dict[str, Any] | None = None) -> str:
        self._created += 1
        connection_id = f"PW-CONN-{self._created:04d}"
        self._connections[connection_id] = True
        self.record("PLAYWRIGHT", "ArchitectPlaywright.attach", "PLAYWRIGHT_ATTACH", "BEGIN", state, endpoint=endpoint, requestedConversationId=requested, connectionId=connection_id, playwrightConnectionsCreated=self._created, playwrightConnectionsClosed=self._closed, playwrightConnectionsCurrentlyOpen=len(self._connections))
        return connection_id

    def connection_end(self, connection_id: str | None, state: dict[str, Any] | None = None, error: Exception | None = None, endpoint: str | None = None, requested: str | None = None, actual: str | None = None, started: float | None = None) -> None:
        if connection_id and connection_id in self._connections:
            self._connections.pop(connection_id, None)
            self._closed += 1
        self.record("PLAYWRIGHT", "ArchitectPlaywright.attach", "PLAYWRIGHT_ATTACH" if error is None else "PLAYWRIGHT_ATTACH_ERROR", "ERROR" if error else "END", state, duration_ms=(time.monotonic() - started) * 1000 if started else None, endpoint=endpoint, requestedConversationId=requested, actualConversationId=actual, connectionId=connection_id, errorClass=type(error).__name__ if error else None, errorMessage=str(error)[:500] if error else None, stackTrace=traceback.format_exc() if error else None, playwrightConnectionsCreated=self._created, playwrightConnectionsClosed=self._closed, playwrightConnectionsCurrentlyOpen=len(self._connections))

    def attach_end(self, connection_id: str, endpoint: str, requested: str | None, actual: str | None, started: float, state: dict[str, Any] | None = None) -> None:
        self.record("PLAYWRIGHT", "ArchitectPlaywright.attach", "PLAYWRIGHT_ATTACH", "END", state, duration_ms=(time.monotonic() - started) * 1000, endpoint=endpoint, requestedConversationId=requested, actualConversationId=actual, connectionId=connection_id, playwrightConnectionsCreated=self._created, playwrightConnectionsClosed=self._closed, playwrightConnectionsCurrentlyOpen=len(self._connections))

    def close_connection(self, connection_id: str | None, reason: str, state: dict[str, Any] | None = None, started: float | None = None, error: Exception | None = None) -> None:
        self.record("PLAYWRIGHT", "ArchitectPlaywright.close", "PLAYWRIGHT_CLOSE", "BEGIN", state, connectionId=connection_id, reason=reason)
        self.connection_end(connection_id, state, error=error, started=started)

    def shutdown(self, state: dict[str, Any] | None = None) -> None:
        remaining = list(self._connections)
        self.record("DIAGNOSTIC", "shutdown", "SHUTDOWN_COMPLETE" if not remaining else "SHUTDOWN_INCOMPLETE", "END", state, activePlaywrightConnectionIds=remaining, playwrightConnectionsCurrentlyOpen=len(remaining))
        with self._lock:
            self._handle.close()


def diagnostic_trace_for(value: Any = None) -> DiagnosticTracer | None:
    return getattr(value, "diagnostic_trace", None) or _ACTIVE_DIAGNOSTIC_TRACE


def rollover_diagnostic_only_enabled() -> bool:
    return os.environ.get(ROLLOVER_DIAGNOSTIC_ONLY_ENV) == "1"


def _diagnostic_memory_candidate(tracer: DiagnosticTracer, state: dict[str, Any], source: str, value: Any) -> None:
    try:
        numeric = int(value)
        valid = numeric >= 0
    except (TypeError, ValueError, OverflowError):
        numeric = None
        valid = False
    tracer.record(
        "ROLLOVER_DIAGNOSTIC",
        "run_rollover_diagnostic_only",
        "MEMORY_CANDIDATE",
        "END",
        state,
        source=source,
        bytes=numeric,
        memoryMiB=round(numeric / (1024 * 1024), 2) if valid else None,
        thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
        thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB,
        aboveThreshold=bool(valid and numeric >= ARCHITECT_MEMORY_THRESHOLD_BYTES),
    )


def _disconnect_architect_bridge_read_only(
    bridge: Any,
    tracer: DiagnosticTracer | None = None,
    state: dict[str, Any] | None = None,
) -> Exception | None:
    """Disconnect an attached browser without Browser.close() or page mutation."""
    started = time.monotonic()
    connection_id = getattr(bridge, "_diagnostic_connection_id", None)
    error = None
    try:
        runtime = getattr(bridge, "_runtime", None)
        if runtime is not None:
            runtime.stop()
    except Exception as caught:
        error = caught
    finally:
        if tracer is not None:
            tracer.close_connection(
                connection_id,
                "diagnostic_only_disconnect",
                state=state,
                started=started,
                error=error,
            )
        bridge._diagnostic_connection_id = None
        bridge._runtime = None
        bridge._browser = None
    return error


def run_rollover_diagnostic_only(watcher: Any, endpoint: str, tracer: DiagnosticTracer) -> str:
    """Collect one read-only rollover snapshot and perform no workflow action."""
    state = dict(watcher.state)
    try:
        persisted = json.loads(Path(watcher.state_path).read_text(encoding="utf-8"))
        if isinstance(persisted, dict):
            state = persisted
    except (OSError, UnicodeError, json.JSONDecodeError):
        tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "STATE_READ_ERROR", "ERROR", state, errorClass="StateReadError", errorMessage="unable to reread canonical state")
    requested_id = state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
    tracer.record(
        "ROLLOVER_DIAGNOSTIC",
        "run_rollover_diagnostic_only",
        "SNAPSHOT_BEGIN",
        "BEGIN",
        state,
        endpoint=endpoint,
        requestedConversationId=requested_id,
        thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
        thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB,
        workflowActionCount=0,
    )
    bridge = None
    try:
        bridge = ArchitectPlaywright.attach(endpoint, requested_id)
        page = bridge.page
        actual_id = architect_conversation_id_from_url(str(getattr(page, "url", "")))
        title = None
        try:
            title = page.title()
        except Exception as error:
            tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "PAGE_TITLE_ERROR", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc())
        tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "ARCHITECT_IDENTITY", "END", state, endpoint=endpoint, requestedConversationId=requested_id, actualConversationId=actual_id, pageUrl=str(getattr(page, "url", "")), pageTitle=title, targetIdentity=getattr(page, "guid", None), connectionId=getattr(bridge, "_diagnostic_connection_id", None))

        browser = getattr(bridge, "_browser", None)
        pages = [candidate for context in browser.contexts for candidate in context.pages]
        inventory = []
        for index, candidate in enumerate(pages):
            inventory.append({
                "index": index,
                "url": str(getattr(candidate, "url", "")),
                "title": candidate.title() if candidate is page else None,
                "conversationId": architect_conversation_id_from_url(str(getattr(candidate, "url", ""))) if ARCHITECT_CONVERSATION_URL_RE.search(str(getattr(candidate, "url", ""))) else None,
                "isCurrent": candidate is page,
            })
        tracer.record("PLAYWRIGHT", "run_rollover_diagnostic_only", "TARGET_INVENTORY", "END", state, pageCount=len(pages), inventory=inventory, connectionId=getattr(bridge, "_diagnostic_connection_id", None))

        api_script = """async () => {
            const performanceObject = globalThis.performance;
            const result = {
                measureUserAgentSpecificMemoryAvailable: Boolean(performanceObject && typeof performanceObject.measureUserAgentSpecificMemory === 'function'),
                measureUserAgentSpecificMemoryBytes: null,
                measureUserAgentSpecificMemoryError: null,
                performanceMemoryAvailable: Boolean(performanceObject && performanceObject.memory),
                usedJSHeapSize: null,
                totalJSHeapSize: null,
                jsHeapSizeLimit: null
            };
            if (result.measureUserAgentSpecificMemoryAvailable) {
                try {
                    const sample = await performanceObject.measureUserAgentSpecificMemory();
                    result.measureUserAgentSpecificMemoryBytes = sample && sample.bytes;
                } catch (error) {
                    result.measureUserAgentSpecificMemoryError = String(error && (error.stack || error.message || error));
                }
            }
            try {
                const memory = performanceObject && performanceObject.memory;
                if (memory) {
                    result.usedJSHeapSize = memory.usedJSHeapSize;
                    result.totalJSHeapSize = memory.totalJSHeapSize;
                    result.jsHeapSizeLimit = memory.jsHeapSizeLimit;
                }
            } catch (error) {
                result.performanceMemoryError = String(error && (error.stack || error.message || error));
            }
            return result;
        }"""
        try:
            api_values = page.evaluate(api_script)
            tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "MEMORY_APIS", "END", state, **api_values)
            if api_values.get("measureUserAgentSpecificMemoryBytes") is not None:
                _diagnostic_memory_candidate(tracer, state, "performance.measureUserAgentSpecificMemory", api_values.get("measureUserAgentSpecificMemoryBytes"))
            _diagnostic_memory_candidate(tracer, state, "performance.memory.usedJSHeapSize", api_values.get("usedJSHeapSize"))
        except Exception as error:
            tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "MEMORY_APIS", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc())

        cdp_session = None
        process_info = []
        targets = []
        try:
            cdp_session = browser.new_browser_cdp_session()
            process_info = cdp_session.send("SystemInfo.getProcessInfo").get("processInfo", [])
            targets = cdp_session.send("Target.getTargets").get("targetInfos", [])
            tracer.record("PLAYWRIGHT", "run_rollover_diagnostic_only", "CDP_PROCESS_INFO", "END", state, processInfo=process_info, connectionId=getattr(bridge, "_diagnostic_connection_id", None))
            tracer.record("PLAYWRIGHT", "run_rollover_diagnostic_only", "CDP_TARGET_INVENTORY", "END", state, targets=[{key: item.get(key) for key in ("targetId", "type", "url", "title", "attached", "browserContextId")} for item in targets], connectionId=getattr(bridge, "_diagnostic_connection_id", None))
        except Exception as error:
            tracer.record("PLAYWRIGHT", "run_rollover_diagnostic_only", "CDP_READ_ERROR", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc(), connectionId=getattr(bridge, "_diagnostic_connection_id", None))
        finally:
            if cdp_session is not None:
                try:
                    cdp_session.detach()
                except Exception:
                    pass

        browser_ids = {int(item["id"]) for item in process_info if item.get("type") == "browser" and str(item.get("id", "")).isdigit()}
        renderer_ids = {int(item["id"]) for item in process_info if item.get("type") == "renderer" and str(item.get("id", "")).isdigit()}
        process_rows = []
        try:
            process_rows = architect_windows_process_rows()
            tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "OS_PROCESS_INVENTORY", "END", state, processes=process_rows, browserProcessIds=sorted(browser_ids), rendererProcessIds=sorted(renderer_ids))
            if len(browser_ids) == 1:
                browser_pid = next(iter(browser_ids))
                try:
                    _diagnostic_memory_candidate(tracer, state, "architect_tab_renderer_working_set", architect_renderer_working_set_bytes(browser_pid, renderer_ids, process_rows))
                except Exception as error:
                    tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "TAB_RENDERER_MEMORY", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error))
                try:
                    root_pid = resolve_architect_browser_root_pid(endpoint)
                    aggregate = architect_process_tree_memory_bytes(root_pid, process_rows)
                    _diagnostic_memory_candidate(tracer, state, "governed_browser_process_tree_working_set", aggregate)
                    included = [row for row in process_rows if row.get("pid") == root_pid or row.get("parentPid") == root_pid]
                    tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "OS_PROCESS_TREE", "END", state, rootPid=root_pid, includedProcesses=included, excludedProcesses=[row for row in process_rows if row not in included])
                except Exception as error:
                    tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "OS_PROCESS_TREE", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error), stackTrace=traceback.format_exc())
        except Exception as error:
            tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "OS_PROCESS_INVENTORY", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error), stackTrace=traceback.format_exc())

        for source, value in (("persisted.architectMemoryBytes", state.get("architectMemoryBytes")),):
            _diagnostic_memory_candidate(tracer, state, source, value)
        due = bool(state.get("rolloverDue"))
        paused = bool(state.get("discussionPauseActive"))
        reason = "DISCUSSION_PAUSED" if paused else ("ROLLOVER_DUE_FALSE" if not due else "NOT_NEXT_PROMPT_READY")
        active_pid = state.get("codexPid") or state.get("active_codex_pid")
        active_pid_alive = None
        if active_pid:
            try:
                active_pid_alive = LocalWatcher.process_alive(int(active_pid))
            except (TypeError, ValueError):
                active_pid_alive = False
        tracer.record("ROLLOVER", "run_rollover_diagnostic_only", "ROLLOVER_GATE", "DECISION", state, workflowState=state.get("state"), safeBoundaryState="NEXT_PROMPT_READY", memorySource=state.get("architectMemoryOwnershipSource"), memoryBytes=state.get("architectMemoryBytes"), memoryMiB=state.get("architectMemoryMiB"), thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES, thresholdSatisfied=bool((state.get("architectMemoryBytes") or 0) >= ARCHITECT_MEMORY_THRESHOLD_BYTES), rolloverDueBefore=due, rolloverTriggerBefore=state.get("rolloverTrigger"), discussionPaused=paused, architectGenerating=False, executorRunning=state.get("executorProcessState") == "RUNNING", activePid=active_pid, activePidAlive=active_pid_alive, nextTaskId=state.get("nextTaskId"), nextPromptPathPresent=bool(state.get("nextPromptPath")), handoverRequested=state.get("handoverRequested"), rolloverAttemptedForTaskId=state.get("rolloverAttemptedForTaskId"), rolloverMaintenanceState=state.get("rolloverMaintenanceState"), decision="SKIP", reason=reason)
        tracer.record("EXECUTOR", "run_rollover_diagnostic_only", "EXECUTOR_DISPATCH_GATE", "DECISION", state, stateValue=state.get("state"), nextTaskId=state.get("nextTaskId"), rolloverDue=state.get("rolloverDue"), rolloverTrigger=state.get("rolloverTrigger"), rolloverServiceCalled=False, rolloverServiceResult=None, discussionPaused=paused, decision="BLOCK", reason="DIAGNOSTIC_ONLY")
        tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "SNAPSHOT_END", "END", state, diagnosticOnly=True, workflowActionCount=0)
        return actual_id
    except Exception as error:
        tracer.record("ROLLOVER_DIAGNOSTIC", "run_rollover_diagnostic_only", "SNAPSHOT_ERROR", "ERROR", state, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc(), workflowActionCount=0)
        return "ERROR"
    finally:
        if bridge is not None:
            _disconnect_architect_bridge_read_only(bridge, tracer, state)


class WindowsSafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate normally, but defer only transient Windows sharing violations."""

    retry_interval_seconds = 5.0
    warning_interval_seconds = 30.0

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._rotation_retry_at = 0.0
        self._rotation_warning_at = 0.0
        self.rotation_deferred = False

    @staticmethod
    def _is_sharing_violation(error: BaseException) -> bool:
        if not isinstance(error, PermissionError):
            return False
        return (getattr(error, "winerror", None) == 32
                or getattr(error, "errno", None) == 32
                or bool(error.args and error.args[0] == 32))

    def _warn_deferred(self, error: BaseException) -> None:
        now = time.monotonic()
        if now < self._rotation_warning_at:
            return
        self._rotation_warning_at = now + self.warning_interval_seconds
        print(f"LOG_ROTATION_DEFERRED reason=WINDOWS_SHARING_VIOLATION error={type(error).__name__}", file=sys.stderr)

    def doRollover(self) -> None:
        now = time.monotonic()
        if now < self._rotation_retry_at:
            self.rotation_deferred = True
            return
        try:
            super().doRollover()
            self.rotation_deferred = False
            self._rotation_retry_at = 0.0
        except PermissionError as error:
            if not self._is_sharing_violation(error):
                raise
            self.rotation_deferred = True
            self._rotation_retry_at = now + self.retry_interval_seconds
            self._warn_deferred(error)


def initialize_runtime_logging(state_dir: str | os.PathLike[str], run_id: str | None = None) -> tuple[logging.Logger, str, str]:
    """Initialize the one durable, privacy-safe watcher log before workflow work."""
    state_path = Path(state_dir)
    log_path = state_path / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    logger = logging.getLogger(RUNTIME_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler_type = (WindowsSafeRotatingFileHandler
                   if logging.handlers.RotatingFileHandler is _ORIGINAL_ROTATING_FILE_HANDLER
                   else logging.handlers.RotatingFileHandler)
    handler = handler_type(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s level=%(levelname)s runId=%(runId)s state=%(state)s taskId=%(taskId)s event=%(event)s %(message)s"))
    logger.addHandler(handler)
    return logger, run_id, str(log_path)


def runtime_log(logger: logging.Logger | None, run_id: str | None, event: str, state: dict[str, Any] | None = None, level: int = logging.INFO, **fields: Any) -> None:
    if logger is None:
        return
    current = state or {}
    safe = {"runId": run_id or "UNKNOWN", "state": current.get("state", "UNKNOWN"), "taskId": current.get("taskId") or current.get("nextTaskId") or "NONE", "event": event}
    safe.update({key: str(value).replace("\n", " ") for key, value in fields.items() if value is not None})
    logger.log(level, " ".join(f"{key}={value}" for key, value in fields.items() if value is not None), extra=safe)


def log_main_loop_decision(watcher: Any, decision: str, reason: str, action_allowed: bool, action_blocked: bool) -> None:
    """Emit bounded normal-runtime evidence for a main-loop branch."""
    state = getattr(watcher, "state", {})
    signature = (decision, reason, state.get("state"), state.get("taskId"), state.get("nextTaskId"),
                 bool(state.get("discussionPauseActive")), bool(state.get("rolloverDue")),
                 bool(state.get("rolloverPending")), bool(state.get("rolloverInProgress")),
                 bool(state.get("handoverRequested")), state.get("architectConversationId"))
    if getattr(watcher, "_last_runtime_main_loop_decision", None) == signature:
        return
    watcher._last_runtime_main_loop_decision = signature
    runtime_log(
        getattr(watcher, "runtime_logger", None),
        getattr(watcher, "runtime_run_id", None),
        "MAIN_LOOP_DECISION",
        state,
        taskId=state.get("taskId"),
        nextTaskId=state.get("nextTaskId"),
        discussionPauseActive=bool(state.get("discussionPauseActive")),
        rolloverDue=bool(state.get("rolloverDue")),
        rolloverPending=bool(state.get("rolloverPending")),
        rolloverInProgress=bool(state.get("rolloverInProgress")),
        handoverRequested=bool(state.get("handoverRequested")),
        architectConversationId=state.get("architectConversationId"),
        branch=decision,
        decision=decision,
        reason=reason,
        actionAllowed=action_allowed,
        actionBlocked=action_blocked,
    )


def run_owned_recovery_bridge(bridge: Any, operation: Callable[[Any], Any]) -> Any:
    """Run one recovery operation and always disconnect its owned bridge."""
    try:
        return operation(bridge)
    finally:
        try:
            bridge.close()
        except Exception:
            pass
AFFOTECH_CHILD_PROJECT_DIR = r"C:\Users\nitro\affotech-system-v2-hybrid"
AFFOTECH_CHILD_REMOTE = "https://github.com/nakfreeajer/affotech-system-v2-hybrid.git"
DOCUMENTATION_KINDS = frozenset({"IMPLEMENTATION", "BUG_FIX", "REPAIR", "RECOVERY", "ARCHITECTURE_CHANGE", "GOVERNANCE_CHANGE", "INCIDENT_CLOSURE"})
DOCUMENTATION_REQUIRED = "REQUIRED"
DOCUMENTATION_NONE = "NONE"


class RelayAuthorityError(RuntimeError):
    pass


class ResultSubmissionError(RuntimeError):
    """Typed, fail-closed errors for the Architect result-submit pipeline."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        super().__init__(f"{code}{':' + detail if detail else ''}")


class WatcherInstanceLock:
    """Own one canonical watcher state directory for this process lifetime."""
    def __init__(self, state_dir: str | os.PathLike[str]):
        self.path = Path(state_dir) / "watcher-instance.lock"
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as error:
            handle.close()
            raise RuntimeError("ORCHESTRATOR_ALREADY_RUNNING") from error
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "WatcherInstanceLock":
        self.acquire()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()


def verify_executor_session(session_id: str) -> bool:
    """Verify a persistent Codex session from Codex's read-only local index."""
    authority_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    index_path = authority_root / "session_index.jsonl"
    try:
        records = index_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("EXECUTOR_SESSION_AUTHORITY_UNAVAILABLE") from error
    for line in records:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("id") == session_id:
            return True
    raise RuntimeError("EXECUTOR_SESSION_NOT_FOUND")


def documentation_requirement(accepted_record: dict[str, Any]) -> tuple[bool, str | None]:
    """Evaluate structured accepted-state fields only; prose is never inspected."""
    if accepted_record.get("classification") != "ACCEPTED" and accepted_record.get("accepted") is not True:
        return False, None
    override = accepted_record.get("documentationOnAcceptance")
    if override == DOCUMENTATION_NONE:
        return False, None
    if override == DOCUMENTATION_REQUIRED:
        return True, "EXPLICIT_DOCUMENTATION_REQUIRED"
    kind = accepted_record.get("milestoneKind")
    if kind in DOCUMENTATION_KINDS:
        if kind == "INCIDENT_CLOSURE":
            return True, "ACCEPTED_INCIDENT_CLOSURE"
        if kind == "REPAIR":
            return True, "ACCEPTED_REPAIR"
        if kind == "BUG_FIX":
            return True, "ACCEPTED_BUG_FIX"
        if kind == "RECOVERY":
            return True, "ACCEPTED_RECOVERY"
        return True, f"ACCEPTED_{kind}"
    if accepted_record.get("implementationChanged") is True or accepted_record.get("implementationCommit"):
        return True, "ACCEPTED_IMPLEMENTATION_CHANGE"
    if accepted_record.get("problemDetected") is True and accepted_record.get("problemResolved") is True:
        return True, "ACCEPTED_DISCOVERED_AND_RESOLVED_PROBLEM"
    return False, None


class DocumentationDoorbell:
    """Exactly-once Architect doorbell for structured accepted milestones."""
    def __init__(self, watcher: "LocalWatcher"):
        self.watcher = watcher

    def evaluate_and_trigger(self, accepted_record: dict[str, Any], bridge: Any, emit: Callable[[str], None] = print) -> str:
        required, reason = documentation_requirement(accepted_record)
        if not required:
            self.watcher.state["documentationStatus"] = "NOT_REQUIRED"
            self.watcher.save()
            return "NOT_REQUIRED"
        milestone_id = accepted_record.get("milestoneId") or accepted_record.get("milestone")
        publication_id = accepted_record.get("acceptedPublicationId") or accepted_record.get("publicationId")
        if not isinstance(milestone_id, str) or not isinstance(publication_id, str):
            raise RuntimeError("DOCUMENTATION_ACCEPTED_IDENTITY_MISSING")
        key = f"{milestone_id}:{publication_id}"
        if self.watcher.state.get("documentationStatus") in {"TRIGGER_SENT", "CURATOR_PENDING", "COMPLETE"} and self.watcher.state.get("docTriggerKey") == key:
            return self.watcher.state["documentationStatus"]
        message = "\n".join([
            "DOCUMENTATION_SYNC_REQUIRED",
            f"reason={reason}",
            f"milestone={milestone_id}",
            f"acceptedPublication={publication_id}",
            f"implementationCommit={accepted_record.get('implementationCommit') or 'NONE'}",
            f"problemId={accepted_record.get('problemId') or 'NONE'}",
            f"milestoneKind={accepted_record.get('milestoneKind') or 'NONE'}",
            "documentationStatus=PENDING",
            "Issue the bounded Documentation Curator instruction for this accepted milestone.",
            "Do not reopen accepted implementation.",
        ])
        send = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result", None)
        if send is None:
            raise RuntimeError("ARCHITECT_DOORBELL_UNAVAILABLE")
        self.watcher.state["documentationStatus"] = "PENDING"
        self.watcher.state["documentationReason"] = reason
        self.watcher.state["docTriggerKey"] = key
        self.watcher.save()
        send(message)
        self.watcher.state["documentationStatus"] = "TRIGGER_SENT"
        self.watcher.state["documentationTriggerCount"] = int(self.watcher.state.get("documentationTriggerCount", 0)) + 1
        self.watcher.save()
        emit("DOCUMENTATION_SYNC_REQUIRED")
        return "TRIGGER_SENT"


def architect_windows_process_rows() -> list[dict[str, int]]:
    if os.name != "nt":
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE")
    script = ("Get-CimInstance Win32_Process | ForEach-Object { "
              "$p=Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
              "if ($p) { [pscustomobject][ordered]@{ pid=[int]$_.ProcessId; "
              "parentPid=[int]$_.ParentProcessId; name=[string]$_.Name; workingSet=[int64]$p.WorkingSet64 } } "
              "} | ConvertTo-Json -Compress")
    try:
        raw = subprocess.check_output(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], text=True, encoding="utf-8", errors="strict")
        decoded = json.loads(raw)
        rows = decoded if isinstance(decoded, list) else [decoded]
    except (OSError, subprocess.CalledProcessError, ValueError, UnicodeError) as error:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED") from error
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED")
    return rows


def architect_renderer_working_set_bytes(browser_pid: int, renderer_pids: set[int], process_rows: list[dict[str, Any]] | None = None) -> int:
    """Return the unique substantial renderer working set for one governed tab."""
    if not isinstance(browser_pid, int) or browser_pid <= 0 or not renderer_pids:
        raise RuntimeError("ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
    rows = process_rows if process_rows is not None else architect_windows_process_rows()
    candidates = []
    for row in rows:
        try:
            pid = int(row["pid"])
            parent_pid = int(row["parentPid"])
            working_set = int(row["workingSet"])
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED") from error
        if pid in renderer_pids and parent_pid == browser_pid and working_set >= 64 * 1024 * 1024:
            candidates.append((pid, working_set))
    if len(candidates) != 1:
        raise RuntimeError("ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
    return candidates[0][1]


def architect_process_tree_memory_bytes(root_pid: int, process_rows: list[dict[str, Any]] | None = None) -> int:
    """Return working-set bytes for one explicitly governed Windows process tree.

    Ownership is established by the caller-provided root PID; process names are
    never used as an identity heuristic.  The optional rows argument makes the
    aggregation deterministic in tests.
    """
    if not isinstance(root_pid, int) or root_pid <= 0:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE")
    if process_rows is None:
        try:
            process_rows = architect_windows_process_rows()
        except RuntimeError:
            raise
    if not isinstance(process_rows, list) or not process_rows:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED")
    normalized_rows: list[dict[str, int]] = []
    seen_pids: set[int] = set()
    try:
        for row in process_rows:
            if not isinstance(row, dict):
                raise ValueError("invalid process row")
            pid = int(row["pid"])
            parent_pid = int(row["parentPid"])
            working_set = int(row["workingSet"])
            # Win32 enumeration includes the synthetic System Idle Process;
            # it cannot own a governed browser tree and has no usable PID.
            if pid == 0:
                continue
            if pid < 0 or parent_pid < 0 or working_set < 0 or pid in seen_pids:
                raise ValueError("invalid process row")
            seen_pids.add(pid)
            normalized_rows.append({"pid": pid, "parentPid": parent_pid, "workingSet": working_set})
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_FAILED") from error
    if root_pid not in seen_pids:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_INCONCLUSIVE")
    process_rows = normalized_rows
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for row in process_rows:
        by_parent.setdefault(int(row["parentPid"]), []).append(row)
    pids = {root_pid}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for row in by_parent.get(parent, []):
            pid = int(row["pid"])
            if pid not in pids:
                pids.add(pid)
                pending.append(pid)
    return sum(row["workingSet"] for row in process_rows if row["pid"] in pids)


def architect_conversation_id_from_url(url: str) -> str:
    """Extract the bare conversation identity from a ChatGPT conversation URL."""
    match = ARCHITECT_CONVERSATION_URL_RE.search(str(url or ""))
    if not match:
        raise RuntimeError("ARCHITECT_CONVERSATION_ID_UNAVAILABLE")
    return match.group(1)


ARCHITECT_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def canonical_architect_conversation_id(identity: str) -> str:
    """Remove only the supported WEB: presentation prefix from UUID IDs."""
    value = str(identity or "")
    suffix = value[4:] if value.startswith("WEB:") else value
    return suffix if ARCHITECT_UUID_RE.fullmatch(suffix) else value


def architect_conversation_ids_equivalent(left: str, right: str) -> bool:
    """Compare Architect IDs exactly, with only WEB:<uuid> compatibility."""
    return str(left or "") == str(right or "") or canonical_architect_conversation_id(left) == canonical_architect_conversation_id(right)


def canonicalize_attached_architect_conversation(
    watcher: Any,
    bridge: Any,
    requested_id: str | None,
    bind_memory: bool = True,
) -> str:
    """Validate an attached page and persist its actual URL representation."""
    bridge.runtime_logger = getattr(watcher, "runtime_logger", None)
    bridge.runtime_run_id = getattr(watcher, "runtime_run_id", None)
    bridge.runtime_watcher = watcher
    tracer = diagnostic_trace_for(watcher)
    started = time.monotonic()
    if tracer:
        tracer.record("ROLLOVER", "canonicalize_attached_architect_conversation", "FUNCTION", "BEGIN", getattr(watcher, "state", {}), requestedConversationId=requested_id)
    page = getattr(bridge, "page", None)
    if page is None:
        return requested_id or ""
    actual_id = architect_conversation_id_from_url(page.url)
    bridge.runtime_conversation_id = actual_id
    if requested_id and not architect_conversation_ids_equivalent(requested_id, actual_id):
        raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
    if requested_id and requested_id != actual_id:
        watcher.state["architectConversationId"] = actual_id
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_CONVERSATION_ID_CANONICALIZED", watcher.state, **{"from": requested_id, "to": actual_id})
    if bind_memory:
        bind_memory_reader = getattr(watcher, "bind_architect_session_memory", None)
        if callable(bind_memory_reader):
            bind_memory_reader(bridge, actual_id)
    if tracer:
        tracer.record("ROLLOVER", "canonicalize_attached_architect_conversation", "FUNCTION", "END", getattr(watcher, "state", {}), durationMs=(time.monotonic() - started) * 1000, requestedConversationId=requested_id, actualConversationId=actual_id, result=True)
    return actual_id


def legacy_provisional_architect_recovery_allowed(watcher: Any, requested_id: str | None) -> bool:
    """Permit only the narrowly bounded recovery for pre-final-URL state."""
    state = getattr(watcher, "state", {})
    task_id = str(state.get("taskId") or "")
    result_path = state.get("executorResultPath")
    result_exists = False
    if isinstance(result_path, str) and result_path.strip():
        try:
            result_exists = Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            result_exists = False
    active_pid = state.get("codexPid") or state.get("active_codex_pid")
    executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
    return bool(
        isinstance(requested_id, str)
        and requested_id.startswith("WEB:")
        and state.get("state") == "RESULT_READY"
        and task_id
        and task_id == str(state.get("lastCompletedTaskId") or "")
        and result_exists
        and not executor_active
        and state.get("handoverRequested") is False
    )


def attach_legacy_provisional_architect(endpoint: str, requested_id: str, watcher: Any) -> "ArchitectPlaywright":
    """Recover one stale WEB: identity only at the interrupted-result boundary."""
    if not legacy_provisional_architect_recovery_allowed(watcher, requested_id):
        raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
    from playwright.sync_api import sync_playwright

    runtime = sync_playwright().start()
    try:
        browser = runtime.chromium.connect_over_cdp(endpoint, timeout=10000)
        pages = [page for context in browser.contexts for page in context.pages]
        eligible = []
        for page in pages:
            url = str(getattr(page, "url", "") or "")
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc.lower() != "chatgpt.com" or not parsed.path.startswith("/c/"):
                continue
            try:
                actual_id = architect_conversation_id_from_url(url)
            except RuntimeError:
                continue
            eligible.append((page, actual_id))
        if len(eligible) != 1:
            raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
        page, actual_id = eligible[0]
        bridge = ArchitectPlaywright(page)
        try:
            entries = bridge._assistant_entries()
        except Exception:
            entries = None
        if entries is not None:
            latest = next((entry.get("text") for entry in reversed(entries) if isinstance(entry, dict) and isinstance(entry.get("text"), str) and entry.get("text")), None)
            if latest is None or not architect_handover_ready(latest):
                raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
        stale_rollover_pending = watcher.state.get("rolloverPending") is True
        watcher.state["architectConversationId"] = actual_id
        watcher.state["rolloverPending"] = False
        watcher.state.pop("rolloverTrigger", None)
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_PROVISIONAL_CONVERSATION_RECOVERED", watcher.state, **{"from": requested_id, "to": actual_id, "staleRolloverPendingCleared": stale_rollover_pending})
        bridge._runtime = runtime
        bridge._browser = browser
        return bridge
    except Exception:
        runtime.stop()
        raise


def architect_handover_ready(response: str) -> bool:
    """Recognize only a terminal Architect handover protocol marker."""
    candidate = str(response or "").rstrip()
    if candidate.endswith(COMPLETE):
        candidate = candidate[:-len(COMPLETE)].rstrip()
    return candidate.endswith(HANDOVER_READY) or candidate.endswith(r"ARCHITECT\_HANDOVER\_READY")


def architect_session_ready(response: str) -> bool:
    """Recognize only the terminal acknowledgement for a fresh session."""
    return str(response or "").strip() == READY


ARCHITECT_MACHINE_PROTOCOL_CONTRACT = (
    "While human discussion is active, Architect responses may be conversational. "
    "Once Rony resolves the decision and discussion resumes, any workflow-bearing "
    "response MUST end with exactly one valid ORCHESTRATOR_RESULT envelope. "
    "F10 / ORCH:RESUME restores mandatory machine-envelope protocol."
)


def fresh_architect_bootstrap_payload(handover: str) -> str:
    """Build the deterministic wire payload used by every fresh Architect tab."""
    return (f"{handover}\n\n"
            "Fresh Architect session bootstrap protocol:\n"
            f"{ARCHITECT_MACHINE_PROTOCOL_CONTRACT}\n"
            f"After accepting this handover, reply exactly:\n{READY}")


def resolve_architect_browser_root_pid(endpoint: str) -> int:
    """Resolve the unique Windows listener owner for the governed CDP endpoint."""
    parts = urlsplit(endpoint)
    port = parts.port
    if port is None or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
    if os.name != "nt":
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
    script = f"Get-NetTCPConnection -State Listen -LocalPort {port} | Select-Object -ExpandProperty OwningProcess"
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True, encoding="utf-8", errors="strict",
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED") from error
    owners = set()
    for line in raw.splitlines():
        value = line.strip()
        if value:
            try:
                pid = int(value)
            except ValueError as error:
                raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED") from error
            if pid > 0:
                owners.add(pid)
    if len(owners) != 1:
        raise RuntimeError("ARCHITECT_BROWSER_MEMORY_OWNERSHIP_UNRESOLVED")
    return next(iter(owners))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_result_envelope(dispatch_id: str, status: str, publication_id: str | None = None, evidence_commit: str | None = None) -> dict[str, Any]:
    if not isinstance(dispatch_id, str) or not dispatch_id or status not in {"PASS", "BLOCKED", "FAILED", "STOP"}:
        raise ValueError("CHILD_RESULT_INVALID")
    if publication_id is not None and not isinstance(publication_id, str):
        raise ValueError("CHILD_RESULT_INVALID")
    return {"schemaVersion": RESULT_SCHEMA_VERSION, "dispatchId": dispatch_id, "resultType": "EXECUTOR_RESULT", "status": status, "publicationId": publication_id, "evidenceCommit": evidence_commit}


def validate_result_envelope(envelope: Any) -> bool:
    return (isinstance(envelope, dict) and envelope.get("schemaVersion") == RESULT_SCHEMA_VERSION
            and envelope.get("resultType") == "EXECUTOR_RESULT"
            and isinstance(envelope.get("dispatchId"), str) and bool(envelope["dispatchId"])
            and envelope.get("status") in {"PASS", "BLOCKED", "FAILED", "STOP"}
            and (envelope.get("publicationId") is None or isinstance(envelope.get("publicationId"), str))
            and (envelope.get("evidenceCommit") is None or isinstance(envelope.get("evidenceCommit"), str)))


def read_result_envelope(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("CHILD_RESULT_MISSING" if isinstance(error, FileNotFoundError) else "CHILD_RESULT_INVALID") from error
    if not validate_result_envelope(envelope):
        raise RuntimeError("CHILD_RESULT_INVALID")
    return envelope


def read_matching_architect_decision(evidence_repo: str | os.PathLike[str], terminal_publication_id: str) -> dict[str, Any] | None:
    """Read the matching Architect decision and accepted pointer from one evidence ref."""
    repo = Path(evidence_repo)
    try:
        subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"], check=True, capture_output=True, text=True)
        ref = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"], text=True, encoding="utf-8").strip()
        names = subprocess.check_output(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref], text=True, encoding="utf-8").splitlines()
        decisions: list[dict[str, Any]] = []
        for name in names:
            if not name.startswith("evidence/architect-decisions/") or not name.endswith("/decision.json"):
                continue
            value = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{name}"]).decode("utf-8"))
            if isinstance(value, dict) and value.get("reviewedPublicationId") == terminal_publication_id:
                decisions.append(value)
        if len(decisions) != 1:
            return None
        pointer = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:evidence/current/LATEST_EXECUTOR_ACCEPTED.json"]).decode("utf-8"))
        if not isinstance(pointer, dict):
            return None
        return {"decision": decisions[0], "acceptedPointer": pointer, "snapshotCommit": ref}
    except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError):
        return None


def read_durable_consumed_relay_key(evidence_repo: str | os.PathLike[str], publication_id: str, content_sha256: str) -> dict[str, Any] | None:
    """Find execution-and-acceptance evidence for one relay publication."""
    repo = Path(evidence_repo)
    try:
        ref = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"], text=True, encoding="utf-8").strip()
        needle = f'"publicationId": "{publication_id}"'
        commits = subprocess.check_output(["git", "-C", str(repo), "log", ref, "--all", "--format=%H", "-S", needle, "--", "evidence/terminal"], text=True, encoding="utf-8").splitlines()
        names = []
        for commit in commits[:20]:
            names.extend(subprocess.check_output(["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit, "--", "evidence/terminal"], text=True, encoding="utf-8").splitlines())
        names = list(dict.fromkeys(names))
        terminal_ids = []
        for result in names:
            name = result.split(":", 1)[1] if ":" in result else result
            if not name.endswith("/terminal.json"):
                continue
            value = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{name}"]).decode("utf-8"))
            context = value.get("authorityContext") if isinstance(value, dict) else None
            execution = value.get("execution") if isinstance(value, dict) else None
            if ((isinstance(context, dict) and context.get("publicationId") == publication_id)
                    or (isinstance(value, dict) and value.get("executedPublicationId") == publication_id)) and isinstance(execution, dict) and execution.get("publicationExecutionCount") == 1:
                terminal_ids.append(Path(name).parent.name)
        if len(terminal_ids) != 1:
            return None
        decision_needle = f'"reviewedPublicationId": "{terminal_ids[0]}"'
        decision_commits = subprocess.check_output(["git", "-C", str(repo), "log", ref, "--all", "--format=%H", "-S", decision_needle, "--", "evidence/architect-decisions"], text=True, encoding="utf-8").splitlines()
        decision_names = []
        for commit in decision_commits[:20]:
            decision_names.extend(subprocess.check_output(["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", commit, "--", "evidence/architect-decisions"], text=True, encoding="utf-8").splitlines())
        for result in decision_names:
            name = result.split(":", 1)[1] if ":" in result else result
            if not name.endswith("/decision.json"):
                continue
            value = json.loads(subprocess.check_output(["git", "-C", str(repo), "show", f"{ref}:{name}"]).decode("utf-8"))
            if isinstance(value, dict) and value.get("reviewedPublicationId") == terminal_ids[0] and value.get("decision") == "ACCEPTED":
                return {"publicationId": publication_id, "contentSha256": content_sha256, "terminalPublicationId": terminal_ids[0], "decisionPublicationId": Path(name).parent.name}
        return None
    except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError):
        return None


def reconcile_executor_result(envelope_path: str | os.PathLike[str], current_terminal_publication: str | None = None, publication_exists: Callable[[str], bool] | None = None, advance_pointer: Callable[[str], None] | None = None) -> str:
    """Reconcile machine evidence without inspecting Codex prose."""
    envelope = read_result_envelope(envelope_path)
    publication_id = envelope.get("publicationId")
    if publication_id:
        if publication_exists is not None and not publication_exists(publication_id):
            return "CHILD_PUBLICATION_MISSING"
        if current_terminal_publication != publication_id:
            if advance_pointer is None:
                return "POINTER_STALE"
            advance_pointer(publication_id)
            return "POINTER_RECONCILED"
    return envelope["status"]


class RelayPromptSource:
    """Read one immutable relay publication from a captured Git ref."""
    def __init__(self, cache_dir: str | os.PathLike[str], remote: str = RELAY_REPOSITORY, refresh: bool = True):
        self.cache_dir = Path(cache_dir)
        self.remote = remote
        self.refresh_enabled = refresh
        self.captured_ref: str | None = None

    def _git(self, *args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(self.cache_dir), *args], text=True, encoding="utf-8").strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise RelayAuthorityError(f"RELAY_GIT_UNAVAILABLE:{error}") from error

    def refresh(self) -> str:
        if not (self.cache_dir / ".git").exists():
            self.cache_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", self.remote, str(self.cache_dir)], check=True, capture_output=True, text=True)
            except (OSError, subprocess.CalledProcessError) as error:
                raise RelayAuthorityError(f"RELAY_CLONE_FAILED:{error}") from error
        try:
            subprocess.run(["git", "-C", str(self.cache_dir), "fetch", "--quiet", "origin", "main"], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RelayAuthorityError(f"RELAY_FETCH_FAILED:{error}") from error
        # Pin every authority read to the remote-tracking ref resolved after
        # fetch.  FETCH_HEAD is mutable fetch bookkeeping and may be stale or
        # refer to a different fetch in a shared cache.
        self.captured_ref = self._git("rev-parse", "refs/remotes/origin/main")
        return self.captured_ref

    def _show_bytes(self, path: str) -> bytes:
        if not self.captured_ref:
            raise RelayAuthorityError("RELAY_REF_NOT_CAPTURED")
        try:
            return subprocess.check_output(["git", "-C", str(self.cache_dir), "show", f"{self.captured_ref}:{path}"])
        except (OSError, subprocess.CalledProcessError) as error:
            raise RelayAuthorityError(f"RELAY_OBJECT_INVALID:{path}") from error

    def _show_json(self, path: str) -> dict[str, Any]:
        try:
            value = json.loads(self._show_bytes(path).decode("utf-8"))
        except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError) as error:
            raise RelayAuthorityError(f"RELAY_OBJECT_INVALID:{path}") from error
        if not isinstance(value, dict):
            raise RelayAuthorityError(f"RELAY_OBJECT_NOT_OBJECT:{path}")
        return value

    def read_current(self) -> dict[str, Any]:
        if self.refresh_enabled:
            self.refresh()
        pointer = self._show_json(RELAY_POINTER)
        publication_id = pointer.get("publicationId")
        pointer_hash = pointer.get("contentSha256")
        if not isinstance(publication_id, str) or not isinstance(pointer_hash, str):
            raise RelayAuthorityError("RELAY_POINTER_INVALID")
        manifest_path = f"relay/architect/prompts/{publication_id}/manifest.json"
        manifest = self._show_json(manifest_path)
        if manifest.get("protocolVersion") != "1.0":
            raise RelayAuthorityError("RELAY_PROTOCOL_UNSUPPORTED")
        if manifest.get("publicationId") != publication_id:
            raise RelayAuthorityError("RELAY_PUBLICATION_ID_MISMATCH")
        if manifest.get("contentSha256") != pointer_hash:
            raise RelayAuthorityError("RELAY_POINTER_MANIFEST_HASH_MISMATCH")
        for key, expected in (("recipientRole", "EXECUTOR"), ("status", "READY_FOR_EXECUTION"), ("executionTarget", "WINDOWS_LOCAL_CODEX")):
            if manifest.get(key) != expected:
                raise RelayAuthorityError(f"RELAY_{key.upper()}_INVALID")
        if not isinstance(manifest.get("requiredInvariantSetId"), str) or not manifest["requiredInvariantSetId"]:
            raise RelayAuthorityError("RELAY_REQUIRED_INVARIANT_SET_INVALID")
        if not isinstance(manifest.get("requiredInvariantContentSha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["requiredInvariantContentSha256"]):
            raise RelayAuthorityError("RELAY_REQUIRED_INVARIANT_HASH_INVALID")
        prompt = manifest.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise RelayAuthorityError("RELAY_PROMPT_EMPTY")
        prompt_bytes = prompt.encode("utf-8")
        if hashlib.sha256(prompt_bytes).hexdigest() != pointer_hash:
            raise RelayAuthorityError("RELAY_CONTENT_HASH_MISMATCH")
        # Real relay publications carry prompt.md as the immutable prompt
        # artifact.  Compare its raw bytes to the decoded manifest prompt,
        # while allowing lightweight unit doubles without a Git object store.
        if (self.cache_dir / ".git").exists():
            try:
                published_prompt_bytes = self._show_bytes(manifest_path.rsplit("/", 1)[0] + "/prompt.md")
            except RelayAuthorityError as error:
                if "prompt.md" in str(error):
                    raise RelayAuthorityError("RELAY_PROMPT_ARTIFACT_MISSING") from error
                raise
            if published_prompt_bytes != prompt_bytes:
                raise RelayAuthorityError("RELAY_PROMPT_ARTIFACT_MISMATCH")
        return {"snapshotCommit": self.captured_ref, "publicationId": publication_id, "contentSha256": pointer_hash,
                "promptBytes": prompt_bytes, "prompt": prompt, "manifest": manifest}

    def legacy_supersession_proof(self, publication_id: str) -> dict[str, Any] | None:
        """Return safe, snapshot-pinned proof that a legacy prompt was superseded."""
        if not self.captured_ref:
            raise RelayAuthorityError("RELAY_REF_NOT_CAPTURED")
        try:
            current = self._show_json(RELAY_POINTER).get("publicationId")
            if current == publication_id:
                return None
            needle = publication_id
            names = subprocess.check_output(["git", "-C", str(self.cache_dir), "grep", "-l", "-F", "--", needle, self.captured_ref, "--", "relay/architect/decisions"], text=True, encoding="utf-8").splitlines()
            matches = []
            for result in names:
                name = result.split(":", 1)[1] if ":" in result else result
                if not name.endswith("/decision.json"):
                    continue
                value = json.loads(self._show_bytes(name).decode("utf-8"))
                if (value.get("decision") == "SUPERSEDED"
                        and (value.get("supersededPublicationId") == publication_id or value.get("parentPublicationId") == publication_id)):
                    decision_dir = Path(name).parent.as_posix()
                    manifest = self._show_json(decision_dir + "/manifest.json")
                    replacement = value.get("replacementPublicationId") or manifest.get("replacementPublicationId")
                    if isinstance(replacement, str) and replacement:
                        matches.append({"decisionPublicationId": Path(name).parent.name, "replacementPublicationId": replacement, "reason": value.get("reason"), "currentPublicationId": current, "snapshotCommit": self.captured_ref})
            return matches[0] if len(matches) == 1 else None
        except (OSError, subprocess.CalledProcessError, UnicodeError, json.JSONDecodeError):
            return None


def normalize_prompt(text: str) -> str:
    return " ".join(text.strip().split())


def relay_task_key(publication_id: str, content_sha256: str) -> str:
    if not isinstance(publication_id, str) or not isinstance(content_sha256, str):
        raise ValueError("RELAY_KEY_INVALID")
    return f"{publication_id}:{content_sha256}"


def result_submission_key(publication_id: str, result_text: str) -> str:
    if not isinstance(publication_id, str) or not publication_id or not isinstance(result_text, str):
        raise ValueError("RESULT_SUBMISSION_KEY_INVALID")
    return f"{publication_id}:{hashlib.sha256(result_text.encode('utf-8')).hexdigest()}"


def publish_durable_executor_terminal(evidence_repo: str | os.PathLike[str], relay_publication_id: str, result_text: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Publish one immutable terminal and advance the governed terminal pointer."""
    root = Path(evidence_repo).resolve()
    result_sha = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
    publication_id = f"GH-PUB-{hashlib.sha256((relay_publication_id + ':' + result_sha).encode()).hexdigest()[:32]}-EXECUTOR-TERMINAL"
    terminal_dir = root / "evidence" / "terminal" / "executor" / publication_id
    terminal_dir.mkdir(parents=True, exist_ok=True)
    terminal = {"schemaVersion": "1.0", "recordType": "EXECUTOR_TERMINAL", "publicationId": publication_id, "executedRelayPublicationId": relay_publication_id, "resultSha256": result_sha, "status": envelope["status"], "machineResultEnvelope": envelope}
    receipt = {"schemaVersion": "1.0", "recordType": "EXECUTOR_RECEIPT", "publicationId": publication_id, "executedRelayPublicationId": relay_publication_id, "resultSha256": result_sha, "terminalDurable": True}
    files = {"terminal.json": stable_json(terminal).encode("utf-8"), "report.md": result_text.encode("utf-8"), "receipt.json": stable_json(receipt).encode("utf-8")}
    for name, data in files.items():
        path = terminal_dir / name
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise RuntimeError("DURABLE_TERMINAL_IMMUTABLE_COLLISION")
        if path.read_bytes() != data:
            raise RuntimeError("DURABLE_TERMINAL_READBACK_FAILED")
    pointer = {"schemaVersion": "1.0", "pointerKind": "LATEST_EXECUTOR_TERMINAL", "publicationId": publication_id, "terminalPath": f"evidence/terminal/executor/{publication_id}/terminal.json", "reportPath": f"evidence/terminal/executor/{publication_id}/report.md", "receiptPath": f"evidence/terminal/executor/{publication_id}/receipt.json", "status": envelope["status"], "executedRelayPublicationId": relay_publication_id, "resultSha256": result_sha}
    current_dir = root / "evidence" / "current"
    current_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = current_dir / "LATEST_EXECUTOR_TERMINAL.json"
    pointer_data = stable_json(pointer).encode("utf-8")
    if pointer_path.exists() and pointer_path.read_bytes() != pointer_data:
        temp = pointer_path.with_name(pointer_path.name + ".pending")
        try:
            with temp.open("xb") as handle:
                handle.write(pointer_data)
            os.replace(temp, pointer_path)
        finally:
            temp.unlink(missing_ok=True)
    elif not pointer_path.exists():
        temp = pointer_path.with_name(pointer_path.name + ".pending")
        try:
            with temp.open("xb") as handle:
                handle.write(pointer_data)
            os.replace(temp, pointer_path)
        finally:
            temp.unlink(missing_ok=True)
    if pointer_path.read_bytes() != pointer_data:
        raise RuntimeError("DURABLE_TERMINAL_POINTER_READBACK_FAILED")
    return {"publicationId": publication_id, "pointerPath": str(pointer_path), "resultSha256": result_sha}


def verify_child_project_binding(child_cwd: str | os.PathLike[str], expected_remote: str = AFFOTECH_CHILD_REMOTE) -> dict[str, Any]:
    """Read-only project identity gate for the AFFOTECH child boundary."""
    path = Path(child_cwd).resolve()
    if not path.is_dir() or not (path / ".git").exists():
        raise RuntimeError("CODEX_CHILD_PROJECT_NOT_A_GIT_REPOSITORY")
    try:
        root = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True, encoding="utf-8", errors="strict").strip()
        remotes = subprocess.check_output(["git", "-C", str(path), "remote", "-v"], text=True, encoding="utf-8", errors="strict")
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise RuntimeError("CODEX_CHILD_PROJECT_IDENTITY_UNAVAILABLE") from error
    if Path(root).resolve() != path or expected_remote not in remotes:
        raise RuntimeError("CODEX_CHILD_PROJECT_IDENTITY_MISMATCH")
    return {"childCwd": str(path), "repositoryIdentity": expected_remote, "sandbox": "read-only", "writableBoundary": str(path)}


def choose_conversation_scroll_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose a real conversation viewport, rejecting incidental overflow."""
    eligible = [
        item for item in candidates
        if item.get("scrollHeight", 0) - item.get("clientHeight", 0) >= max(200, item.get("clientHeight", 0) * 0.25)
        and item.get("clientHeight", 0) >= 400
    ]
    if not eligible:
        return None
    def score(item: dict[str, Any]) -> tuple[int, int, int, int]:
        class_summary = str(item.get("classSummary", ""))
        root_signal = 1 if "scroll-root" in class_summary else 0
        active_signal = 1 if item.get("scrollTop", 0) > 0 else 0
        scroll_range = int(item.get("scrollHeight", 0) - item.get("clientHeight", 0))
        return (root_signal, active_signal, min(scroll_range, 100000), int(item.get("clientHeight", 0)))
    return max(eligible, key=score)


def extract_executor_prompt(response: str) -> str | None:
    if not response.rstrip().endswith(COMPLETE):
        return None
    return extract_executor_prompt_envelope(response)


def extract_executor_prompt_envelope(response: str) -> str | None:
    """Extract exactly one structurally valid Executor envelope, marker optional."""
    if response.count(BEGIN) != 1 or response.count(END) != 1:
        return None
    begin = response.find(BEGIN)
    end = response.find(END)
    if begin < 0 or end < begin:
        return None
    match = re.search(rf"{re.escape(BEGIN)}\s*\n?(.*?){re.escape(END)}", response[begin:], re.DOTALL)
    return match.group(1).strip() if match else None


def architect_response_finished(response: str, generation_control_visible: bool = False) -> bool:
    return not generation_control_visible and response.rstrip().endswith(COMPLETE)


def extract_handover(response: str) -> str | None:
    if not response.rstrip().endswith(COMPLETE):
        return None
    match = re.search(rf"{re.escape(HANDOVER_BEGIN)}\s*\n(.*?){re.escape(HANDOVER_END)}", response, re.DOTALL)
    return match.group(1).strip() if match else None


def make_handover_envelope(transaction_id: str, task_id: str, body: str) -> str:
    """Build the sole authoritative wire format for new rollover handovers."""
    return (f"{HANDOVER_OPEN}\n"
            f"version={HANDOVER_PROTOCOL_VERSION}\n"
            f"transactionId={str(transaction_id).strip()}\n"
            f"taskId={str(task_id).strip()}\n\n"
            f"{str(body)}\n"
            f"{HANDOVER_CLOSE}")


def parse_handover_envelope(response: str) -> dict[str, Any] | None:
    """Parse a complete canonical handover, rejecting ambiguous surrounding text."""
    text = str(response or "")
    candidate = text.strip()
    if (not candidate.startswith(HANDOVER_OPEN) or not candidate.endswith(HANDOVER_CLOSE)
            or candidate.count(HANDOVER_OPEN) != 1 or candidate.count(HANDOVER_CLOSE) != 1):
        return None
    inner = candidate[len(HANDOVER_OPEN):-len(HANDOVER_CLOSE)].strip("\r\n")
    lines = inner.splitlines()
    if len(lines) < 5:
        return None
    if lines[0] != "version=1" or not lines[1].startswith("transactionId=") or not lines[2].startswith("taskId="):
        return None
    transaction_id = lines[1][len("transactionId="):].strip()
    task_id = lines[2][len("taskId="):].strip()
    if not transaction_id or not task_id or lines[1].count("=") != 1 or lines[2].count("=") != 1:
        return None
    body_lines = lines[3:]
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)
    if not body.strip():
        return None
    return {"version": HANDOVER_PROTOCOL_VERSION, "transactionId": transaction_id, "taskId": task_id, "body": body}


def _legacy_handover_compatibility_allowed(state: dict[str, Any]) -> bool:
    """Allow only the explicitly identified pre-upgrade in-flight transaction."""
    return (state.get("rolloverHandoverProtocolVersion") is None
            and str(state.get("rolloverTransactionId") or "") == LEGACY_COMPAT_TRANSACTION_ID
            and str(state.get("rolloverTransactionTaskId") or "") == LEGACY_COMPAT_TASK_ID)


STANDARD_HANDOVER_REQUEST = """ARCHITECT SESSION ROLLOVER

This Architect conversation has reached the configured response limit.

Prepare a complete handover body for a fresh ChatGPT Architect conversation.
Preserve the authoritative working state, current milestone, verified results,
unresolved blockers, non-regression rules, evidence pointers, and exact next
Architect action. Do not perform new project work or issue another Executor
milestone.

Return exactly one handover envelope and nothing outside it:

<HANDOVER>
version=1
transactionId=<exact transaction id>
taskId=<exact rollover task id>

<complete handover body>
</HANDOVER>"""


def rollover_transaction_id(state: dict[str, Any], task_id: str | None = None) -> str:
    """Return the immutable identity for a live transaction or a new identity."""
    current_task = str(task_id or state.get("nextTaskId") or state.get("taskId") or "")
    existing = str(state.get("rolloverTransactionId") or "").strip()
    owner = str(state.get("rolloverTransactionTaskId") or "").strip()
    attempted = str(state.get("rolloverAttemptedForTaskId") or "").strip()
    live_send_state = state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
    live_transaction = state.get("rolloverInProgress") is True or (
        state.get("rolloverMaintenanceState") in {"IN_PROGRESS", "RECONCILE_PENDING"}
        and live_send_state
    )
    if existing and live_transaction and (
            (owner and owner == current_task) or (not owner and attempted == current_task)):
        return existing
    try:
        generation = int(state.get("rolloverTransactionGeneration", 0) or 0)
    except (TypeError, ValueError):
        generation = 0
    basis = "|".join(("ROLLOVER_TRANSACTION_V3", current_task, str(generation), existing,
                       str(state.get("architectConversationId") or ""),
                       str(state.get("architectResponseCount") or ""),
                       str(state.get("rolloverTrigger") or "")))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def rollover_transaction_lifecycle_action(
    state: dict[str, Any],
    task_id: str,
    operator_restart_available: bool = False,
) -> str:
    """Classify one transaction boundary before a lifecycle mutation.

    This is deliberately protocol/lifecycle evidence only.  Scheduling remains
    owned by ``evaluate_rollover_state`` and its runtime adapter.
    """
    current = str(task_id or "").strip()
    transaction = str(state.get("rolloverTransactionId") or "").strip()
    owner = str(state.get("rolloverTransactionTaskId") or "").strip()
    attempted = str(state.get("rolloverAttemptedForTaskId") or "").strip()
    if not transaction:
        return "CREATE_TRANSACTION"
    if (owner and owner != current) or (not owner and attempted and attempted != current):
        return "RETIRE_STALE_TRANSACTION"
    terminal = (
        state.get("state") == "NEXT_PROMPT_READY"
        and str(state.get("nextTaskId") or "") == current
        and state.get("rolloverDue") is True
        and state.get("rolloverPending") is True
        and state.get("rolloverInProgress") is False
        and state.get("rolloverMaintenanceState") == "DEFERRED"
        and state.get("rolloverRecoveryState") == "DEFERRED"
        and state.get("rolloverRecoveryTerminalReason") == "ARCHITECT_ROLLOVER_SAFETY_CUTOUT"
        and owner == current
        and attempted == current
        and state.get("handoverRequested") is True
        and state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
    )
    if terminal:
        return "TERMINAL_RECOVERY_CANDIDATE" if operator_restart_available else "RETAIN_TERMINAL"
    prepared_unsent = (
        state.get("state") == "NEXT_PROMPT_READY"
        and str(state.get("nextTaskId") or "") == current
        and state.get("rolloverDue") is True
        and state.get("rolloverPending") is True
        and state.get("rolloverHandoverSendState") == "UNSENT"
        and state.get("handoverRequested") is False
        and state.get("rolloverTransactionGeneration") is not None
    )
    if prepared_unsent:
        return "REUSE_PREPARED_UNSENT"
    if owner == current or (not owner and attempted == current):
        return "RETAIN_CURRENT_TRANSACTION"
    return "RETAIN_TRANSACTION"


def handover_request_for_transaction(transaction_id: str, task_id: str | None = None) -> str:
    task = str(task_id or "").strip()
    return ("ARCHITECT SESSION ROLLOVER\n\n"
            "Return exactly:\n\n"
            f"{HANDOVER_OPEN}\nversion=1\ntransactionId={str(transaction_id).strip()}\n"
            f"taskId={task}\n\n<complete handover body>\n{HANDOVER_CLOSE}\n\n"
            "Nothing outside the envelope.")


def legacy_handover_reemission_request_for_transaction(transaction_id: str, task_id: str) -> str:
    """Recover transport for the one pre-upgrade transaction, without new work."""
    transaction = str(transaction_id or "").strip()
    task = str(task_id or "").strip()
    return (
        "ARCHITECT HANDOVER TRANSPORT RECOVERY\n\n"
        "This is transport recovery for the already-open rollover transaction below, not new Architect work. "
        "Do not re-evaluate the milestone, make new product decisions, create a different task, or replace the "
        "already-staged Executor prompt. Re-emit the complete handover for the same existing transaction and "
        "preserve the already-established authoritative state.\n\n"
        "For this pre-upgrade transaction only, return exactly the existing legacy handover response format:\n\n"
        "<complete handover body>\n"
        f"Rollover transaction ID: {transaction}\n"
        "ARCHITECT_HANDOVER_READY\n\n"
        f"The existing target task is {task}. Nothing outside the handover response."
    )


def handover_transaction_matches(response: str, transaction_id: str | None) -> bool:
    if not transaction_id:
        return True
    token = str(transaction_id).strip()
    if not token or re.fullmatch(r"[0-9A-Fa-f]+", token) is None:
        return False
    return re.search(rf"(?<![0-9A-Fa-f]){re.escape(token)}(?![0-9A-Fa-f])", str(response or "")) is not None


def trace_transaction_match_failure(watcher: Any, response: str, transaction_id: str | None, reason: str) -> None:
    tracer = diagnostic_trace_for(watcher)
    if not tracer:
        return
    token = str(transaction_id or "").strip()
    token_present = bool(token and re.search(rf"(?<![0-9A-Fa-f]){re.escape(token)}(?![0-9A-Fa-f])", str(response or "")))
    runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "HANDOVER_RECOVERY_DECISION", watcher.state,
                expectedTransactionId=token or None, transactionTokenPresent=token_present,
                handoverReady=architect_handover_ready(response), decision="REJECT", reason=reason)
    tracer.record(
        "HANDOVER",
        "handover_transaction_matches",
        "TRANSACTION_MATCH",
        "DECISION",
        watcher.state,
        expectedTransactionId=token or None,
        transactionTokenPresent=token_present,
        handoverReady=architect_handover_ready(response),
        decision="REJECT",
        reason=reason,
    )


class ArchitectSessionRollover:
    """Small crash-safe state machine with memory-first rollover policy."""
    def __init__(self, watcher: "LocalWatcher"):
        self.watcher = watcher
        self._last_logged_memory_bytes: int | None = None
        self._last_sampled_memory_bytes: int | None = None
        self._last_memory_error: str | None = None
        self._next_memory_sample_at = 0.0

    def initialize_current_session(self) -> None:
        self.watcher.architect_memory_reader = None
        self.watcher.architect_memory_reader_thread_id = None
        self.watcher.architect_memory_session_id = None
        self.watcher.state["architectResponseCount"] = 0
        self.watcher.state["handoverRequested"] = False
        self.watcher.state["handoverReady"] = False
        self.watcher.state["rolloverPending"] = False
        self.watcher.state.pop("rolloverTrigger", None)
        self.watcher.state.pop("architectMemoryBytes", None)
        self.watcher.state.pop("architectMemoryMiB", None)
        self.watcher.state.pop("architectMemorySessionId", None)
        self.watcher.save()

    def sample_memory(self, memory_reader: Callable[[], int] | None = None, emit: Callable[[str], None] = print) -> str | None:
        """Sample only the explicitly governed Architect process tree."""
        tracer = diagnostic_trace_for(self.watcher)
        started = time.monotonic()
        bound_reader = getattr(self.watcher, "architect_memory_reader", None)
        reader_owner_thread_id = getattr(self.watcher, "architect_memory_reader_thread_id", None)
        current_thread_id = threading.get_ident()
        reader_ownership_valid = not callable(bound_reader) or not reader_owner_thread_id or reader_owner_thread_id == current_thread_id
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_TICK", self.watcher.state,
                    taskId=self.watcher.state.get("taskId"), architectConversationId=self.watcher.state.get("architectConversationId"),
                    discussionPauseActive=bool(self.watcher.state.get("discussionPauseActive")), memoryReaderBound=bool(memory_reader or getattr(self.watcher, "architect_memory_reader", None)),
                    readerOwnerThreadId=reader_owner_thread_id, currentThreadId=current_thread_id, readerOwnershipValid=reader_ownership_valid,
                    sampleAttempted=True, rendererPid=self.watcher.state.get("architectMemoryRendererPid", "UNAVAILABLE_AT_LAYER"), thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
                    thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover.sample_memory", "MEMORY_SAMPLE_BEGIN", "BEGIN", self.watcher.state, threshold=ARCHITECT_MEMORY_THRESHOLD_BYTES, safetyCeiling=ARCHITECT_MEMORY_SAFETY_CEILING_BYTES)
        if self.watcher.state.get("humanRequiredReason") == "ARCHITECT_ROLLOVER_SAFETY_CUTOUT":
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_SKIPPED", self.watcher.state,
                        skipReason="SAFETY_CUTOUT", workflowMutation=False, rendererPid="UNAVAILABLE_AT_LAYER")
            if tracer:
                tracer.record("ROLLOVER", "ArchitectSessionRollover.sample_memory", "MEMORY_SAMPLE_SKIPPED", "SKIP", self.watcher.state, reason="SAFETY_CUTOUT", durationMs=(time.monotonic() - started) * 1000)
            return None
        if memory_reader is None and callable(bound_reader) and not reader_ownership_valid:
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_SKIPPED", self.watcher.state,
                        skipReason="MEMORY_READER_THREAD_MISMATCH", readerOwnerThreadId=reader_owner_thread_id,
                        currentThreadId=current_thread_id, workflowMutation=False)
            return None
        reader = memory_reader or bound_reader
        if reader is None:
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_SKIPPED", self.watcher.state,
                        skipReason="MEMORY_READER_UNBOUND", memoryReaderBound=False, sampleAttempted=False, workflowMutation=False, rendererPid="UNAVAILABLE_AT_LAYER")
            if getattr(self.watcher, "memory_ownership_required", False) and self.watcher.state.get("architectMemoryOwnershipWarningEmitted") is not True:
                self.watcher.state["architectMemoryOwnership"] = "UNCONFIGURED"
                self.watcher.state["architectMemoryError"] = "ARCHITECT_BROWSER_ROOT_PID_REQUIRED"
                self.watcher.state["architectMemoryOwnershipWarningEmitted"] = True
                self.watcher.save()
                emit("ARCHITECT_MEMORY_OWNERSHIP_REQUIRED")
            return None
        try:
            memory_bytes = reader()
            if not isinstance(memory_bytes, int) or memory_bytes < 0:
                raise RuntimeError("ARCHITECT_BROWSER_MEMORY_SAMPLE_INVALID")
        except Exception as error:
            candidate = str(error)
            reason = candidate if re.fullmatch(r"[A-Z0-9_]+", candidate) else f"ARCHITECT_MEMORY_SAMPLE_{type(error).__name__.upper()}"
            self.watcher.state["architectMemoryOwnership"] = "INCONCLUSIVE"
            self.watcher.state["architectMemoryError"] = reason
            self.watcher.save()
            if reason != self._last_memory_error:
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_FAILED", self.watcher.state, error=reason)
                self._last_memory_error = reason
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_RESULT", self.watcher.state,
                        sampleAttempted=True, memoryBytes=None, memoryMiB=None, thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
                        thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB, decision="UNAVAILABLE", workflowMutation=False,
                        rendererPid="UNAVAILABLE_AT_LAYER", error=reason)
            return None
        recovering_from_error = self._last_memory_error is not None
        self._last_memory_error = None
        self.watcher.state["architectMemoryBytes"] = memory_bytes
        self.watcher.state["architectMemoryMiB"] = round(memory_bytes / (1024 * 1024), 2)
        memory_mib = self.watcher.state["architectMemoryMiB"]
        if (self._last_logged_memory_bytes is None
                or recovering_from_error
                or abs(memory_bytes - self._last_logged_memory_bytes) >= 64 * 1024 * 1024):
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE", self.watcher.state, memoryMiB=memory_mib, thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB)
            self._last_logged_memory_bytes = memory_bytes
        previous_memory_bytes = self._last_sampled_memory_bytes
        self._last_sampled_memory_bytes = memory_bytes
        response_count = int(self.watcher.state.get("architectResponseCount", 0))
        trigger = self.rollover_trigger(memory_bytes, response_count)
        if memory_bytes >= ARCHITECT_MEMORY_THRESHOLD_BYTES and (previous_memory_bytes is None or previous_memory_bytes < ARCHITECT_MEMORY_THRESHOLD_BYTES):
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_THRESHOLD_TELEMETRY", self.watcher.state, memoryMiB=memory_mib, thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB)
        rollover_due_before = bool(self.watcher.state.get("rolloverDue"))
        trigger_before = self.watcher.state.get("rolloverTrigger")
        workflow_mutation = False
        if trigger and not self.watcher.state.get("rolloverDue"):
            self.watcher.state["rolloverDue"] = True
            self.watcher.state["rolloverTrigger"] = trigger
            self.watcher.save()
            workflow_mutation = True
            emit(f"ROLLOVER_DUE trigger={trigger}")
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_RESULT", self.watcher.state,
                    taskId=self.watcher.state.get("taskId"), architectConversationId=self.watcher.state.get("architectConversationId"),
                    memoryBytes=memory_bytes, memoryMiB=memory_mib, thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
                    thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB, rolloverDueBefore=rollover_due_before,
                    rolloverDueAfter=bool(self.watcher.state.get("rolloverDue")), rolloverTriggerBefore=trigger_before,
                    rolloverTriggerAfter=self.watcher.state.get("rolloverTrigger"), decision="TRIGGER_DUE" if trigger else "NO_TRIGGER",
                    workflowMutation=workflow_mutation, rendererPid=self.watcher.state.get("architectMemoryRendererPid", "UNAVAILABLE_AT_LAYER"))
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_THRESHOLD_DECISION", self.watcher.state,
                    memoryBytes=memory_bytes, memoryMiB=memory_mib, thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
                    thresholdMiB=ARCHITECT_MEMORY_THRESHOLD_MIB, decision="TRIGGER_DUE" if trigger else "BELOW_THRESHOLD",
                    rolloverDueAfter=bool(self.watcher.state.get("rolloverDue")), workflowMutation=workflow_mutation)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover.sample_memory", "MEMORY_SAMPLE_END", "END", self.watcher.state, durationMs=(time.monotonic() - started) * 1000, memoryBytes=memory_bytes, memoryMiB=round(memory_bytes / (1024 * 1024), 2), rolloverDueBefore=bool(previous_memory_bytes and previous_memory_bytes >= ARCHITECT_MEMORY_THRESHOLD_BYTES), rolloverDueAfter=bool(self.watcher.state.get("rolloverDue")), rootPid=os.environ.get("ARCHITECT_BROWSER_ROOT_PID"))
        return trigger

    def sample_memory_for_loop(self, memory_reader: Callable[[], int] | None = None) -> str | None:
        """Throttle main-loop telemetry without changing direct sampling semantics."""
        now = time.monotonic()
        if now < self._next_memory_sample_at:
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_MEMORY_SAMPLE_SKIPPED", self.watcher.state,
                        skipReason="COOLDOWN", sampleAttempted=False, workflowMutation=False, rendererPid="UNAVAILABLE_AT_LAYER")
            tracer = diagnostic_trace_for(self.watcher)
            if tracer:
                tracer.record("ROLLOVER", "ArchitectSessionRollover.sample_memory_for_loop", "MEMORY_SAMPLE_SKIPPED", "SKIP", self.watcher.state, reason="COOLDOWN", retryAfter=self._next_memory_sample_at)
            return None
        self._next_memory_sample_at = now + ROLLOVER_MEMORY_SAMPLE_COOLDOWN_SECONDS
        return self.sample_memory(memory_reader=memory_reader)

    def _begin_bounded_recovery(self) -> str:
        """Return ATTEMPT_ALLOWED, WAIT_BACKOFF, or EXHAUSTED."""
        state = self.watcher.state
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._begin_bounded_recovery", "FUNCTION", "BEGIN", state)
        if state.get("humanRequiredReason") == "ARCHITECT_ROLLOVER_SAFETY_CUTOUT":
            if tracer:
                tracer.record("ROLLOVER", "ArchitectSessionRollover._begin_bounded_recovery", "GATE", "DECISION", state, gate="safety_cutout", result="BLOCK", reason="terminal_cutout")
            return ROLLOVER_RECOVERY_EXHAUSTED
        now = time.time()
        started = float(state.get("rolloverRecoveryStartedAt", 0.0) or 0.0)
        if state.get("rolloverRecoveryState") == "COMPLETE":
            for key in ("rolloverRecoveryStartedAt", "rolloverRecoveryAttemptCount", "rolloverRecoveryLastAttemptAt", "rolloverRecoveryTerminalReason", "rolloverRecoveryRetryAfter"):
                state.pop(key, None)
            state.pop("rolloverRecoveryState", None)
            started = 0.0
        if not started:
            started = now
            state["rolloverRecoveryStartedAt"] = started
            state["rolloverRecoveryAttemptCount"] = 0
            state.pop("rolloverRecoveryTerminalReason", None)
            state["rolloverRecoveryState"] = "PENDING"
            state.pop("rolloverRecoveryRetryAfter", None)
            self.watcher.save()
        attempts = int(state.get("rolloverRecoveryAttemptCount", 0) or 0)
        if attempts >= ROLLOVER_RECOVERY_MAX_ATTEMPTS or now - started >= ROLLOVER_RECOVERY_WINDOW_SECONDS:
            self._enter_safety_cutout("ARCHITECT_ROLLOVER_SAFETY_CUTOUT")
            return ROLLOVER_RECOVERY_EXHAUSTED
        retry_after = float(state.get("rolloverRecoveryRetryAfter", 0.0) or 0.0)
        if now < retry_after:
            if tracer:
                tracer.record("ROLLOVER", "ArchitectSessionRollover._begin_bounded_recovery", "GATE", "DECISION", state, gate="rollover_retry_after", result=ROLLOVER_RECOVERY_WAIT_BACKOFF, now=now, retryAfter=retry_after)
            return ROLLOVER_RECOVERY_WAIT_BACKOFF
        state["rolloverRecoveryAttemptCount"] = attempts + 1
        state["rolloverRecoveryLastAttemptAt"] = now
        state["rolloverRecoveryRetryAfter"] = now + ROLLOVER_RECOVERY_RETRY_DELAY_SECONDS
        state["rolloverRecoveryState"] = "RECOVERING"
        self.watcher.save()
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._begin_bounded_recovery", "FUNCTION", "END", state, result=ROLLOVER_RECOVERY_ATTEMPT_ALLOWED, attemptCount=state.get("rolloverRecoveryAttemptCount"))
        return ROLLOVER_RECOVERY_ATTEMPT_ALLOWED

    def _record_recovery_failure(self) -> None:
        state = self.watcher.state
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._record_recovery_failure", "FUNCTION", "BEGIN", state)
        attempts = int(state.get("rolloverRecoveryAttemptCount", 0) or 0)
        started = float(state.get("rolloverRecoveryStartedAt", 0.0) or 0.0)
        state["rolloverRecoveryState"] = "DEFERRED"
        state["rolloverRecoveryTerminalReason"] = "ARCHITECT_ROLLOVER_MAINTENANCE_FAILED"
        state["rolloverMaintenanceState"] = "DEFERRED"
        state["rolloverLastFailureReason"] = state.get("rolloverHandoverRecoveryReason") or state.get("rolloverRecoveryTerminalReason")
        state["rolloverDeferredForTaskId"] = state.get("nextTaskId") or state.get("taskId")
        state["rolloverAutoAttemptCount"] = int(state.get("rolloverRecoveryAttemptCount", attempts) or attempts)
        state["rolloverRecoveryRetryAfter"] = time.time() + ROLLOVER_RECOVERY_RETRY_DELAY_SECONDS
        state.setdefault("rolloverAutomaticRecoveryEpochCount", 1)
        state.setdefault("rolloverAutomaticRecoveryMaxEpochs", ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS)
        state.setdefault("rolloverRecoveryEpoch", 1)
        state["rolloverAutomaticRecoveryNextEligibleAt"] = time.time() + ROLLOVER_AUTOMATIC_RECOVERY_COOLDOWN_SECONDS
        self.watcher.save()

    def _automatic_recovery_epoch_eligible(self, task_id: str) -> bool:
        """Compatibility adapter for the canonical epoch decision."""
        state = self.watcher.state
        if str(state.get("nextTaskId") or "") != str(task_id):
            return False
        decision = _evaluate_public_rollover_boundary(
            self.watcher,
            lambda: bool(state.get("discussionPauseActive")),
        )
        return decision.action is RolloverAction.START_RECOVERY_EPOCH

    def _begin_automatic_recovery_epoch(self, task_id: str, decision: RolloverDecision | None = None) -> bool:
        """Execute a canonical START_RECOVERY_EPOCH decision."""
        state = self.watcher.state
        if decision is None:
            decision = _evaluate_public_rollover_boundary(
                self.watcher,
                lambda: bool(state.get("discussionPauseActive")),
            )
        if decision.action is RolloverAction.HUMAN_REQUIRED:
            terminal_reason = (
                "ARCHITECT_ROLLOVER_AUTOMATIC_RECOVERY_EXHAUSTED"
                if decision.reason == "AUTOMATIC_RECOVERY_EXHAUSTED"
                else f"ROLLOVER_EVALUATOR_{decision.reason}"
            )
            state["state"] = "HUMAN_REQUIRED"
            state["humanRequiredReason"] = terminal_reason
            state["rolloverRecoveryTerminalReason"] = terminal_reason
            self.watcher.save()
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HUMAN_REQUIRED", state, reason=terminal_reason)
            return False
        if decision.action is not RolloverAction.START_RECOVERY_EPOCH:
            return False
        max_epochs = int(state.get("rolloverAutomaticRecoveryMaxEpochs", ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS) or 0)
        epochs = int(state.get("rolloverAutomaticRecoveryEpochCount", 0) or 0)
        now = time.time()
        state.update({
            "rolloverRecoveryEpoch": int(state.get("rolloverRecoveryEpoch", 0) or 0) + 1,
            "rolloverAutomaticRecoveryEpochCount": epochs + 1,
            "rolloverAutomaticRecoveryMaxEpochs": max_epochs,
            "rolloverRecoveryState": "PENDING",
            "rolloverMaintenanceState": "IN_PROGRESS",
            "rolloverRecoveryAttemptCount": 0,
            "rolloverRecoveryStartedAt": now,
            "rolloverRecoveryLastAttemptAt": None,
            "rolloverRecoveryRetryAfter": None,
            "rolloverAutomaticRecoveryNextEligibleAt": None,
            "rolloverDue": True,
            "rolloverPending": True,
        })
        state.pop("rolloverRecoveryTerminalReason", None)
        state.pop("rolloverDeferredForTaskId", None)
        state.pop("rolloverLastFailureReason", None)
        self.watcher.save()
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_EPOCH_STARTED", state, taskId=task_id, epoch=state["rolloverRecoveryEpoch"], epochCount=epochs + 1, maxEpochs=max_epochs)
        return True

    def _record_recovery_success(self) -> None:
        state = self.watcher.state
        state["rolloverRecoveryState"] = "COMPLETE"
        state.pop("rolloverRecoveryRetryAfter", None)
        state.pop("rolloverMaintenanceState", None)
        state.pop("rolloverLastFailureReason", None)
        state.pop("rolloverDeferredForTaskId", None)
        state.pop("rolloverAutoAttemptCount", None)
        self.watcher.save()
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._record_recovery_success", "FUNCTION", "END", state, result=True)

    def _enter_safety_cutout(self, reason: str) -> None:
        state = self.watcher.state
        state.update({
            "rolloverRecoveryState": "DEFERRED",
            "rolloverRecoveryTerminalReason": reason,
            "rolloverInProgress": False,
            "rolloverDue": True,
            "rolloverMaintenanceState": "DEFERRED",
            "rolloverLastFailureReason": reason,
            "rolloverDeferredForTaskId": state.get("nextTaskId") or state.get("taskId"),
        })
        state.pop("rolloverRecoveryRetryAfter", None)
        state["rolloverPending"] = True
        state.setdefault("rolloverAutomaticRecoveryMaxEpochs", ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS)
        state.setdefault("rolloverAutomaticRecoveryEpochCount", 1)
        state.setdefault("rolloverRecoveryEpoch", 1)
        state["rolloverAutomaticRecoveryNextEligibleAt"] = time.time() + ROLLOVER_AUTOMATIC_RECOVERY_COOLDOWN_SECONDS
        if int(state.get("rolloverAutomaticRecoveryEpochCount", 0) or 0) >= int(state.get("rolloverAutomaticRecoveryMaxEpochs", ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS) or 0):
            state["state"] = "HUMAN_REQUIRED"
            state["humanRequiredReason"] = "ARCHITECT_ROLLOVER_AUTOMATIC_RECOVERY_EXHAUSTED"
            state["rolloverRecoveryTerminalReason"] = "ARCHITECT_ROLLOVER_AUTOMATIC_RECOVERY_EXHAUSTED"
        self.watcher.save()
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_ROLLOVER_MAINTENANCE_DEFERRED", state, reason=reason)
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_DEFERRED", state, reason=reason, nextEligibleAt=state["rolloverAutomaticRecoveryNextEligibleAt"])
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._enter_safety_cutout", "FUNCTION", "END", state, reason=reason, result="DEFERRED")

    @staticmethod
    def rollover_trigger(memory_bytes: int, response_count: int = 0) -> str | None:
        del response_count
        if isinstance(memory_bytes, int) and memory_bytes >= ARCHITECT_MEMORY_THRESHOLD_BYTES:
            return "MEMORY_THRESHOLD"
        return None

    def observe_complete_response(self, response: str, response_id: str | None = None) -> bool:
        if self.watcher.state.get("handoverRequested") or not architect_response_finished(response):
            return False
        identity = response_id or hashlib.sha256(response.encode("utf-8")).hexdigest()
        if identity == self.watcher.state.get("lastArchitectResponseIdentity"):
            return False
        self.watcher.state["lastArchitectResponseIdentity"] = identity
        self.watcher.state["architectResponseCount"] = int(self.watcher.state.get("architectResponseCount", 0)) + 1
        self.watcher.save()
        return True

    def _retire_terminal_same_task_transaction(self, task_id: str, operator_restart_available: bool) -> bool:
        """Retire one exhausted same-boundary transaction for explicit restart recovery."""
        state = self.watcher.state
        if not self._terminal_same_task_transaction_eligible(task_id, operator_restart_available):
            return False
        transaction_id = str(state.get("rolloverTransactionId") or "").strip()
        for key in (
            "rolloverTransactionId", "rolloverTransactionTaskId", "rolloverAttemptedForTaskId",
            "pending_handover", "rolloverHandoverResponseIdentity", "rolloverFreshCandidateConversationId",
            "rolloverFreshCandidateState", "rolloverFreshCandidateDiscoveryState",
            "rolloverFreshCandidateAttemptCount", "rolloverFreshCandidateRetryAfter",
            "rolloverFreshBootstrapPayloadHash", "rolloverFreshPageCreated",
            "rolloverHandoverRecoveryDisposition", "rolloverHandoverRecoveryReason",
            "rolloverHandoverRecoveryRetryAfter", "rolloverRecoveryState",
            "rolloverRecoveryStartedAt", "rolloverRecoveryAttemptCount",
            "rolloverRecoveryLastAttemptAt", "rolloverRecoveryRetryAfter",
            "rolloverRecoveryTerminalReason", "rolloverMaintenanceState",
            "rolloverLastFailureReason", "rolloverDeferredForTaskId", "rolloverAutoAttemptCount",
        ):
            state.pop(key, None)
        state.update({
            "rolloverRetiredTransactionId": transaction_id,
            "rolloverRetiredTransactionTaskId": task_id,
            "rolloverRetiredTransactionReason": "ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
            "rolloverRetiredTransactionAt": time.time(),
            "handoverRequested": False,
            "handoverReady": False,
            "rolloverInProgress": False,
            "rolloverDue": True,
            "rolloverPending": True,
            "rolloverHandoverSendState": "UNSENT",
        })
        self.watcher.save()
        runtime_log(
            getattr(self.watcher, "runtime_logger", None),
            getattr(self.watcher, "runtime_run_id", None),
            "ROLLOVER_TERMINAL_TRANSACTION_RETIRED",
            state,
            retiredTransactionId=transaction_id,
            transactionTaskId=task_id,
            reason="ARCHITECT_ROLLOVER_SAFETY_CUTOUT",
        )
        return True

    def _terminal_same_task_transaction_eligible(self, task_id: str, operator_restart_available: bool) -> bool:
        return rollover_transaction_lifecycle_action(
            self.watcher.state, task_id, operator_restart_available
        ) == "TERMINAL_RECOVERY_CANDIDATE"

    def _revive_terminal_delivered_transaction(self, task_id: str) -> bool:
        """Revive one terminal transaction whose exact request is already delivered."""
        state = self.watcher.state
        transaction_id = str(state.get("rolloverTransactionId") or "").strip()
        if (
            not transaction_id
            or rollover_transaction_lifecycle_action(
                state, task_id, bool(getattr(self.watcher, "_operator_restart_rollover_recovery_available", False))
            ) != "TERMINAL_RECOVERY_CANDIDATE"
        ):
            return False
        state.update({
            "rolloverDue": True,
            "rolloverPending": True,
            "rolloverInProgress": True,
            "handoverRequested": True,
            "handoverReady": False,
            "rolloverHandoverSendState": "ACKNOWLEDGED",
            "rolloverMaintenanceState": "RECONCILE_PENDING",
            "rolloverRecoveryState": "PENDING",
            "rolloverHandoverRecoveryDisposition": "RECONCILE_PENDING",
            "rolloverHandoverRecoveryReason": "DELIVERED_PAYLOAD_RECONCILIATION",
        })
        for key in (
            "rolloverRecoveryStartedAt", "rolloverRecoveryLastAttemptAt",
            "rolloverRecoveryAttemptCount", "rolloverRecoveryRetryAfter",
            "rolloverRecoveryTerminalReason", "rolloverDeferredForTaskId",
            "rolloverLastFailureReason", "rolloverAutoAttemptCount",
            "rolloverHandoverRecoveryRetryAfter",
        ):
            state.pop(key, None)
        self.watcher.save()
        runtime_log(
            getattr(self.watcher, "runtime_logger", None),
            getattr(self.watcher, "runtime_run_id", None),
            "ROLLOVER_TERMINAL_DELIVERED_TRANSACTION_RECOVERED",
            state,
            transactionId=transaction_id,
            transactionTaskId=task_id,
            transactionGeneration=state.get("rolloverTransactionGeneration"),
            deliveryObserved=True,
            handoverResent=False,
        )
        return True

    def _retire_stale_transaction_for_task(self, task_id: str) -> bool:
        """Remove terminal evidence that belongs to an earlier task boundary."""
        state = self.watcher.state
        transaction_id = str(state.get("rolloverTransactionId") or "").strip()
        if (
            not task_id
            or not transaction_id
            or rollover_transaction_lifecycle_action(state, task_id) != "RETIRE_STALE_TRANSACTION"
        ):
            return False
        for key in (
            "rolloverTransactionId", "rolloverTransactionTaskId", "rolloverAttemptedForTaskId",
            "pending_handover", "rolloverHandoverResponseIdentity", "rolloverFreshCandidateConversationId",
            "rolloverFreshCandidateState", "rolloverFreshCandidateDiscoveryState",
            "rolloverFreshCandidateAttemptCount", "rolloverFreshCandidateRetryAfter",
            "rolloverFreshBootstrapPayloadHash", "rolloverFreshPageCreated",
            "rolloverHandoverRecoveryDisposition", "rolloverHandoverRecoveryReason",
            "rolloverHandoverRecoveryRetryAfter", "rolloverRecoveryState",
            "rolloverRecoveryStartedAt", "rolloverRecoveryAttemptCount",
            "rolloverRecoveryLastAttemptAt", "rolloverRecoveryRetryAfter",
            "rolloverRecoveryTerminalReason", "rolloverMaintenanceState",
            "rolloverLastFailureReason", "rolloverDeferredForTaskId", "rolloverAutoAttemptCount",
        ):
            state.pop(key, None)
        state.update({
            "handoverRequested": False,
            "handoverReady": False,
            "rolloverInProgress": False,
            "rolloverDue": True,
            "rolloverPending": True,
            "rolloverHandoverSendState": "UNSENT",
        })
        self.watcher.save()
        return True

    def request_if_due(self, bridge: "ArchitectPlaywright", latest_prompt_dispatched: bool, executor_running: bool, emit: Callable[[str], None] = print, architect_generating: bool = False, safe_boundary_state: str | None = None, allow_same_task_unsent_recovery: bool = False) -> bool:
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover.request_if_due", "FUNCTION", "BEGIN", self.watcher.state, latestPromptDispatched=latest_prompt_dispatched, executorRunning=executor_running, architectGenerating=architect_generating, safeBoundaryState=safe_boundary_state)
        workflow_state = self.watcher.state.get("state", "IDLE")
        boundary_state = safe_boundary_state or workflow_state
        if boundary_state != "NEXT_PROMPT_READY":
            _trace_rollover_gate(self.watcher, boundary_state, "SKIP", "NOT_NEXT_PROMPT_READY", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
            return False
        if boundary_state == "NEXT_PROMPT_READY":
            prompt_path = self.watcher.state.get("nextPromptPath")
            next_task = self.watcher.state.get("nextTaskId")
            if not next_task or not isinstance(prompt_path, str) or not Path(prompt_path).is_file() or self.watcher.state.get("handoverRequested"):
                _trace_rollover_gate(self.watcher, boundary_state, "SKIP", "NO_VALID_NEXT_PROMPT", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
                return False
        count = int(self.watcher.state.get("architectResponseCount", 0))
        trigger = self.rollover_trigger(self.watcher.state.get("architectMemoryBytes"), count)
        if not trigger:
            if (
                allow_same_task_unsent_recovery
                and self.watcher.state.get("rolloverDue") is True
                and self.watcher.state.get("rolloverTrigger") == "MEMORY_THRESHOLD"
            ):
                trigger = "MEMORY_THRESHOLD"
            else:
                _trace_rollover_gate(self.watcher, boundary_state, "NO_TRIGGER", "MEMORY_BELOW_THRESHOLD", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
                return False
        task_id = str(self.watcher.state.get("nextTaskId") or self.watcher.state.get("taskId") or "")
        lifecycle_action = rollover_transaction_lifecycle_action(self.watcher.state, task_id)
        if lifecycle_action == "RETIRE_STALE_TRANSACTION":
            self._retire_stale_transaction_for_task(task_id)
            lifecycle_action = rollover_transaction_lifecycle_action(self.watcher.state, task_id)
        if task_id and self.watcher.state.get("rolloverAttemptedForTaskId") == task_id and not allow_same_task_unsent_recovery:
            _trace_rollover_gate(self.watcher, boundary_state, "SKIP", "ALREADY_ATTEMPTED_FOR_TASK", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
            return False
        if architect_generating or not latest_prompt_dispatched or self.watcher.state.get("handoverRequested"):
            _trace_rollover_gate(self.watcher, boundary_state, "DEFER", "ARCHITECT_GENERATING" if architect_generating else "HANDOVER_ALREADY_REQUESTED", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
            return False
        if executor_running:
            _trace_rollover_gate(self.watcher, boundary_state, "DEFER", "EXECUTOR_RUNNING", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
            return False
        active_pid = self.watcher.state.get("codexPid") or self.watcher.state.get("active_codex_pid")
        if active_pid and self.watcher.state.get("executorProcessState") != "COMPLETED_WITH_RESULT":
            try:
                if LocalWatcher.process_alive(int(active_pid)):
                    _trace_rollover_gate(self.watcher, boundary_state, "DEFER", "EXECUTOR_RUNNING", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
                    return False
            except (TypeError, ValueError):
                _trace_rollover_gate(self.watcher, boundary_state, "SKIP", "ACTIVE_PID_INVALID", function="ArchitectSessionRollover.request_if_due", architectGenerating=architect_generating, executorRunning=executor_running)
                return False
        previous_transaction_id = str(self.watcher.state.get("rolloverTransactionId") or "").strip()
        retired_transaction_id = str(self.watcher.state.get("rolloverRetiredTransactionId") or "").strip()
        raw_generation = self.watcher.state.get("rolloverTransactionGeneration")
        try:
            persisted_generation = int(raw_generation)
        except (TypeError, ValueError):
            persisted_generation = 0
        prepared_unsent = (
            allow_same_task_unsent_recovery
            and lifecycle_action == "REUSE_PREPARED_UNSENT"
            and bool(previous_transaction_id)
            and not isinstance(raw_generation, bool)
            and persisted_generation > 0
            and previous_transaction_id != retired_transaction_id
        )
        if prepared_unsent:
            generation = persisted_generation
            transaction_id = previous_transaction_id
        else:
            generation = max(0, persisted_generation) + 1
            self.watcher.state["rolloverTransactionGeneration"] = generation
            transaction_id = rollover_transaction_id(self.watcher.state, task_id)
            while transaction_id in {previous_transaction_id, retired_transaction_id}:
                generation += 1
                self.watcher.state["rolloverTransactionGeneration"] = generation
                transaction_id = rollover_transaction_id(self.watcher.state, task_id)
        if previous_transaction_id and transaction_id != previous_transaction_id:
            # A deferred transaction is terminal authority.  Do not let its
            # response, candidate, or bootstrap evidence participate in the
            # next task boundary's transaction.
            for key in (
                "pending_handover", "rolloverHandoverResponseIdentity",
                "rolloverFreshCandidateConversationId", "rolloverFreshCandidateState",
                "rolloverFreshCandidateDiscoveryState", "rolloverFreshCandidateAttemptCount",
                "rolloverFreshCandidateRetryAfter", "rolloverFreshBootstrapPayloadHash",
                "rolloverFreshPageCreated", "rolloverHandoverRecoveryDisposition",
                "rolloverHandoverRecoveryReason", "rolloverHandoverRecoveryRetryAfter",
            ):
                self.watcher.state.pop(key, None)
            self.watcher.state["rolloverHandoverSendState"] = "UNSENT"
        if task_id:
            self.watcher.state["rolloverAttemptedForTaskId"] = task_id
        self.watcher.state["rolloverTransactionId"] = transaction_id
        self.watcher.state["rolloverTransactionTaskId"] = task_id
        self.watcher.state["rolloverDue"] = True
        self.watcher.state["rolloverInProgress"] = True
        self.watcher.state["rolloverPending"] = True
        self.watcher.state["rolloverTrigger"] = trigger
        self.watcher.state["handoverRequested"] = True
        self.watcher.state["handoverReady"] = False
        self.watcher.state["rolloverHandoverSendState"] = "PENDING"
        if self.watcher.__class__.__name__ == "LocalFirstOrchestrator":
            self.watcher.state["rolloverHandoverProtocolVersion"] = HANDOVER_PROTOCOL_VERSION
        self.watcher.state["rolloverMaintenanceState"] = "IN_PROGRESS"
        self.watcher.state.pop("rolloverLastFailureReason", None)
        self.watcher.state.pop("rolloverDeferredForTaskId", None)
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HANDOVER_SEND_DECISION", self.watcher.state,
                    transactionId=transaction_id, transactionTaskId=task_id, handoverSendState="PENDING",
                    sendActionAttempted=False, ackObserved=False, generationVisible=architect_generating,
                    classification="NEW_TRANSACTION", decision="SEND", reason="ROLLOVER_TRIGGER_AUTHORIZED")
        self.watcher.save()
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ROLLOVER_PENDING", self.watcher.state, trigger=trigger)
        try:
            if tracer:
                payload = handover_request_for_transaction(transaction_id, task_id)
                tracer.record("HANDOVER", "request_if_due", "HANDOVER_REQUEST_SEND_BEGIN", "BEGIN", self.watcher.state, payloadLength=len(payload), payloadSha256=hashlib.sha256(payload.encode()).hexdigest(), transactionId=transaction_id)
            else:
                payload = handover_request_for_transaction(transaction_id, task_id)
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HANDOVER_SEND_STAGE", self.watcher.state,
                        transactionId=transaction_id, transactionTaskId=task_id, stage="SEND_BEGIN", payloadLength=len(payload),
                        payloadSha256=hashlib.sha256(payload.encode()).hexdigest(), handoverSendState="PENDING")
            bridge.submit_result_bounded(payload)
            self.watcher.state["rolloverHandoverSendState"] = "ACKNOWLEDGED"
            self.watcher.save()
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HANDOVER_SEND_STAGE", self.watcher.state,
                        transactionId=transaction_id, transactionTaskId=task_id, stage="SEND_END", sendActionAttempted=True,
                        ackObserved=True, handoverSendState="ACKNOWLEDGED")
            emit("ARCHITECT_HANDOVER_REQUESTED")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_HANDOVER_REQUESTED", self.watcher.state)
            return True
        except Exception as error:
            if tracer:
                tracer.record("HANDOVER", "request_if_due", "HANDOVER_REQUEST_SEND_ERROR", "ERROR", self.watcher.state, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc())
            attempted = bool(getattr(bridge, "sendActionAttempted", False)) or bool(getattr(bridge, "last_send_method", None))
            ambiguous = attempted or (isinstance(error, ResultSubmissionError) and error.code == "ARCHITECT_SUBMISSION_ACK_TIMEOUT")
            exact_payload_observed = False
            if ambiguous:
                observer = getattr(bridge, "exact_user_message_payload_observed", None)
                if callable(observer):
                    try:
                        exact_payload_observed = bool(observer(payload))
                    except Exception:
                        exact_payload_observed = False
            error_code = getattr(error, "code", None) or type(error).__name__
            cause = getattr(error, "__cause__", None)
            error_message = str(error)[:500]
            cause_class = type(cause).__name__ if cause is not None else None
            cause_message = str(cause)[:500] if cause is not None else None
            send_acknowledged = bool(getattr(bridge, "sendActionAcknowledged", False))
            last_send_method = getattr(bridge, "last_send_method", None)
            initial_send_returned = getattr(bridge, "initialSendActionReturned", None)
            if ambiguous:
                self.watcher.state.update({
                    "rolloverInProgress": True,
                    "rolloverDue": True,
                    "rolloverPending": True,
                    "handoverRequested": True,
                    "handoverReady": False,
                    "rolloverHandoverSendState": "ACKNOWLEDGED" if exact_payload_observed else "AMBIGUOUS",
                    # An acknowledgement timeout means delivery is unknown,
                    # not failed.  Keep the transaction live so the caller
                    # can reconcile the existing Architect response without
                    # requiring a process restart or sending again.
                    "rolloverMaintenanceState": "RECONCILE_PENDING",
                    "rolloverHandoverRecoveryDisposition": "RECONCILE_PENDING",
                    "rolloverHandoverRecoveryReason": type(error).__name__,
                    "rolloverLastFailureReason": type(error).__name__,
                })
                self.watcher.state.pop("rolloverDeferredForTaskId", None)
                self.watcher.state.pop("rolloverHandoverRecoveryRetryAfter", None)
                if exact_payload_observed:
                    setattr(bridge, "sendActionAcknowledged", True)
                    runtime_log(
                        getattr(self.watcher, "runtime_logger", None),
                        getattr(self.watcher, "runtime_run_id", None),
                        "ARCHITECT_HANDOVER_DELIVERY_RECONCILED",
                        self.watcher.state,
                        transactionId=transaction_id,
                        transactionTaskId=task_id,
                        payloadSha256=hashlib.sha256(payload.encode()).hexdigest(),
                        deliveryObserved=True,
                        originalErrorCode=error_code,
                        originalErrorClass=type(error).__name__,
                        originalErrorMessage=error_message,
                        underlyingExceptionClass=cause_class,
                        underlyingExceptionMessage=cause_message,
                        sendActionAttempted=attempted,
                        sendActionAcknowledged=True,
                    )
                runtime_log(
                    getattr(self.watcher, "runtime_logger", None),
                    getattr(self.watcher, "runtime_run_id", None),
                    "ARCHITECT_HANDOVER_SEND_AMBIGUOUS",
                    self.watcher.state,
                    errorCode=error_code,
                    errorClass=type(error).__name__,
                    errorMessage=error_message,
                    causeClass=cause_class,
                    causeMessage=cause_message,
                    sendActionAttempted=attempted,
                    sendActionAcknowledged=send_acknowledged or exact_payload_observed,
                    lastSendMethod=last_send_method,
                    initialSendActionReturned=initial_send_returned,
                    exactPayloadObserved=exact_payload_observed,
                    transactionId=transaction_id,
                    transactionGeneration=self.watcher.state.get("rolloverTransactionGeneration"),
                )
            else:
                self.watcher.state.update({
                    "rolloverInProgress": False,
                    "handoverRequested": False,
                    "handoverReady": False,
                    "rolloverDue": True,
                    "rolloverPending": True,
                    "rolloverHandoverSendState": "UNSENT",
                    "rolloverMaintenanceState": "DEFERRED",
                    "rolloverLastFailureReason": type(error).__name__,
                    "rolloverDeferredForTaskId": task_id,
                })
                self.watcher.state.pop("rolloverAttemptedForTaskId", None)
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HANDOVER_SEND_DECISION", self.watcher.state,
                        transactionId=transaction_id, transactionTaskId=task_id, handoverSendState=self.watcher.state.get("rolloverHandoverSendState"),
                        sendActionAttempted=attempted, ackObserved=send_acknowledged or exact_payload_observed, classification="DELIVERED_AFTER_ERROR" if exact_payload_observed else ("AMBIGUOUS" if ambiguous else "UNSENT"),
                        decision="RECONCILE" if ambiguous else "DEFER", reason=type(error).__name__)
            self.watcher.save()
            emit("STATE=ROLLOVER_PENDING")
            return False

    def persisted_validated_handover(self) -> str | None:
        """Load exact validated handover bytes from durable state, if present."""
        state = self.watcher.state
        response = state.get("pending_handover")
        if not isinstance(response, str) or not response:
            return None
        transaction_id = str(state.get("rolloverTransactionId") or "")
        if not self._handover_response_valid(response, transaction_id):
            return None
        digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
        stored_digest = state.get("rolloverHandoverResponseIdentity")
        if stored_digest and stored_digest != digest:
            return None
        if stored_digest != digest:
            state["rolloverHandoverResponseIdentity"] = digest
            state["rolloverHandoverResponseSource"] = "DURABLE_RECOVERY"
            bootstrap = fresh_architect_bootstrap_payload(response)
            state["rolloverFreshBootstrapPayloadHash"] = hashlib.sha256(bootstrap.encode("utf-8")).hexdigest()
            self.watcher.save()
        return response

    def _legacy_handover_reemission_epoch(self) -> int | None:
        state = self.watcher.state
        for key in ("rolloverRecoveryEpoch", "rolloverAutomaticRecoveryEpochCount"):
            value = state.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return None

    def _request_legacy_handover_reemission_once(self, bridge: "ArchitectPlaywright", task_id: str) -> tuple[str, str | None]:
        """Re-emit only the exact in-flight legacy handover, once per durable epoch."""
        state = self.watcher.state
        transaction_id = str(state.get("rolloverTransactionId") or "")
        epoch = self._legacy_handover_reemission_epoch()
        allowed = bool(
            _legacy_handover_compatibility_allowed(state)
            and transaction_id
            and str(state.get("rolloverTransactionTaskId") or "") == task_id
            and str(state.get("nextTaskId") or "") == task_id
            and state.get("state") == "NEXT_PROMPT_READY"
            and state.get("rolloverDue") is True
            and state.get("rolloverPending") is True
            and state.get("handoverRequested") is True
            and state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
            and state.get("postDiscussionProtocolRolloverCommittedTransactionId") != transaction_id
            and state.get("discussionPauseActive") is not True
            and state.get("executorActiveWriter") is not True
            and state.get("governedExecutorActiveWriter") is not True
            and self.watcher._exact_staged_prompt_recovery_task() == task_id
            and epoch is not None
        )
        if not allowed:
            return "NOT_ELIGIBLE", None
        if (state.get("rolloverLegacyHandoverReemissionTransactionId") == transaction_id
                and state.get("rolloverLegacyHandoverReemissionAttemptedEpoch") == epoch):
            return "ALREADY_ATTEMPTED", None

        # Claim the one-shot before any browser submission so a restart or
        # ambiguous send cannot duplicate the request in this recovery epoch.
        state.update({
            "rolloverLegacyHandoverReemissionTransactionId": transaction_id,
            "rolloverLegacyHandoverReemissionAttemptedEpoch": epoch,
            "rolloverLegacyHandoverReemissionState": "REQUESTING",
        })
        self.watcher.save()
        request = legacy_handover_reemission_request_for_transaction(transaction_id, task_id)
        baseline_reader = getattr(bridge, "assistant_baseline", None)
        if not callable(baseline_reader):
            baseline_reader = getattr(bridge, "assistant_fast_snapshot", None)
        if not callable(baseline_reader):
            state["rolloverLegacyHandoverReemissionState"] = "FAILED"
            self.watcher.save()
            return "FAILED", None
        baseline = baseline_reader()
        runtime_log(
            getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None),
            "LEGACY_HANDOVER_REEMISSION_REQUESTED", state,
            transactionId=transaction_id, taskId=task_id, recoveryEpoch=epoch,
            requestSha256=hashlib.sha256(request.encode("utf-8")).hexdigest(),
            conversationId=state.get("architectConversationId"),
        )
        try:
            bridge.submit_result_bounded(request)
            state["rolloverLegacyHandoverReemissionState"] = "SENT"
            self.watcher.save()
            observed = bridge.wait_for_new_response(baseline, poll_interval=0.5)
        except Exception as error:
            state.update({
                "rolloverLegacyHandoverReemissionState": "AMBIGUOUS",
                "rolloverRecoveryState": "RECOVERING",
                "rolloverMaintenanceState": "RECONCILE_PENDING",
            })
            self.watcher.save()
            runtime_log(
                getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None),
                "LEGACY_HANDOVER_REEMISSION_WAIT", state,
                transactionId=transaction_id, taskId=task_id, recoveryEpoch=epoch,
                reason=type(error).__name__,
            )
            return "WAIT", None
        response = observed.get("text") if isinstance(observed, dict) and observed.get("state") == "COMPLETED" else None
        if not isinstance(response, str) or not self._handover_response_valid(response, transaction_id):
            state["rolloverLegacyHandoverReemissionState"] = "INVALID_RESPONSE"
            self.watcher.save()
            runtime_log(
                getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None),
                "LEGACY_HANDOVER_REEMISSION_REJECTED", state,
                transactionId=transaction_id, taskId=task_id, recoveryEpoch=epoch,
            )
            return "INVALID", None
        if not self.persist_validated_handover(response):
            return "CONFLICT", None
        state["rolloverLegacyHandoverReemissionState"] = "RESPONSE_PERSISTED"
        self.watcher.save()
        runtime_log(
            getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None),
            "LEGACY_HANDOVER_REEMISSION_PERSISTED", state,
            transactionId=transaction_id, taskId=task_id, recoveryEpoch=epoch,
            responseSha256=hashlib.sha256(response.encode("utf-8")).hexdigest(),
        )
        return "VALIDATED", response

    def persist_validated_handover(self, response: str) -> bool:
        """Durably bind exact validated response bytes and hash before fresh-page work."""
        state = self.watcher.state
        transaction_id = str(state.get("rolloverTransactionId") or "")
        if not self._handover_response_valid(response, transaction_id):
            return False
        digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
        prior = state.get("pending_handover")
        prior_digest = state.get("rolloverHandoverResponseIdentity")
        prior_is_valid = (
            isinstance(prior, str)
            and bool(prior)
            and self._handover_response_valid(prior, transaction_id)
        )
        if prior_digest and prior_digest != digest:
            return False
        if prior_is_valid and prior != response:
            return False
        bootstrap = fresh_architect_bootstrap_payload(response)
        state.update({
            "handoverReady": True,
            "pending_handover": response,
            "rolloverHandoverResponseIdentity": digest,
            "rolloverFreshBootstrapPayloadHash": hashlib.sha256(bootstrap.encode("utf-8")).hexdigest(),
            "rolloverRecoveryState": "RECOVERING",
        })
        self.watcher.save()
        runtime_log(
            getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None),
            "ROLLOVER_HANDOVER_DURABLE", state,
            transactionId=transaction_id, taskId=state.get("rolloverTransactionTaskId"), responseSha256=digest,
        )
        return True

    def _read_existing_handover_response(self, bridge: "ArchitectPlaywright", transaction_id: str | None = None) -> str | None:
        """Read an existing semantic handover response without waiting or sending."""
        entries = bridge._assistant_entries()
        return next(
            (
                entry.get("text")
                for entry in reversed(entries)
                if isinstance(entry, dict)
                and isinstance(entry.get("text"), str)
                and self._handover_response_valid(entry["text"], transaction_id)
            ),
            None,
        )

    def _handover_response_valid(self, response: str, transaction_id: str | None = None) -> bool:
        parsed = parse_handover_envelope(response)
        expected = str(transaction_id or self.watcher.state.get("rolloverTransactionId") or "").strip()
        expected_task = str(self.watcher.state.get("rolloverTransactionTaskId") or "").strip()
        if parsed is not None:
            return (parsed["transactionId"] == expected
                    and (not expected_task or parsed["taskId"] == expected_task))
        if not _legacy_handover_compatibility_allowed(self.watcher.state):
            return False
        return (architect_handover_ready(response)
                and handover_transaction_matches(response, expected)
                and (not expected_task or expected_task in response))

    def reconcile_pending_handover(self, bridge: "ArchitectPlaywright", existing_handover: Any = _HANDOVER_RESPONSE_UNSET) -> bool:
        """Consume an already-visible handover for an outstanding rollover."""
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover.reconcile_pending_handover", "FUNCTION", "BEGIN", self.watcher.state)
        if not self.watcher.state.get("rolloverDue") or not self.watcher.state.get("rolloverPending"):
            return False
        task_id = str(self.watcher.state.get("nextTaskId") or self.watcher.state.get("taskId") or "")
        attempted_task = str(self.watcher.state.get("rolloverAttemptedForTaskId") or "")
        legacy_task = str(self.watcher.state.get("taskId") or "")
        if not task_id or attempted_task not in {task_id, legacy_task}:
            return False
        retry_after = float(self.watcher.state.get("rolloverHandoverRecoveryRetryAfter", 0.0) or 0.0)
        if (self.watcher.state.get("rolloverHandoverRecoveryDisposition") == "RETRYABLE"
                and time.time() < retry_after):
            return False
        reconstructed_handover = False
        handover = self.watcher.state.get("pending_handover")
        if existing_handover is not _HANDOVER_RESPONSE_UNSET:
            if not isinstance(handover, str) or not self._handover_response_valid(handover):
                handover = existing_handover
        elif not isinstance(handover, str) or not self._handover_response_valid(handover):
            try:
                handover = self._read_existing_handover_response(bridge)
            except Exception:
                self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_HANDOVER_HISTORY_UNAVAILABLE")
                return False
            if tracer:
                tracer.record("HANDOVER", "reconcile_pending_handover", "HANDOVER_RESPONSE_CLASSIFICATION", "DECISION", self.watcher.state, found=bool(handover))
        if not isinstance(handover, str) or not self._handover_response_valid(handover):
            self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_HANDOVER_NOT_FOUND")
            return False
        transaction_id = self.watcher.state.get("rolloverTransactionId")
        if isinstance(handover, str) and not self._handover_response_valid(handover, transaction_id):
            trace_transaction_match_failure(self.watcher, handover, transaction_id, "TRANSACTION_TOKEN_MISSING_OR_INVALID")
            self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_HANDOVER_TRANSACTION_MISMATCH")
            return False
        if isinstance(handover, str) and self._handover_response_valid(handover) and not self.watcher.state.get("handoverRequested"):
            response_identity = hashlib.sha256(handover.encode("utf-8")).hexdigest()
            bootstrap = fresh_architect_bootstrap_payload(handover)
            self.watcher.state.update({
                "handoverRequested": True,
                "handoverReady": False,
                "pending_handover": handover,
                "rolloverHandoverResponseIdentity": response_identity,
                "rolloverFreshBootstrapPayloadHash": hashlib.sha256(bootstrap.encode("utf-8")).hexdigest(),
                "rolloverInProgress": True,
                "rolloverHandoverSendState": "AMBIGUOUS",
            })
            self.watcher.state.pop("rolloverHandoverRecoveryDisposition", None)
            self.watcher.state.pop("rolloverHandoverRecoveryReason", None)
            self.watcher.state.pop("rolloverHandoverRecoveryRetryAfter", None)
            self.watcher.save()
            reconstructed_handover = True
        candidate_state = self.watcher.state.get("rolloverFreshCandidateState")
        candidate_page = self._existing_fresh_candidate_page(bridge, handover)
        if self.watcher.state.get("rolloverFreshCandidateDiscoveryState") == "AMBIGUOUS":
            self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_FRESH_CANDIDATE_AMBIGUOUS")
            return False
        if candidate_state in {"SUBMISSION_AMBIGUOUS", "ACK_PENDING"} and candidate_page is None:
            self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_FRESH_CANDIDATE_NOT_FOUND")
            return False
        if candidate_page is not None and not self.watcher.state.get("rolloverFreshCandidateConversationId"):
            self._record_fresh_candidate(candidate_page, candidate_state or "ACK_PENDING")
        if candidate_state == "FAILED":
            attempts = int(self.watcher.state.get("rolloverFreshCandidateAttemptCount", 0))
            retry_after = float(self.watcher.state.get("rolloverFreshCandidateRetryAfter", 0.0) or 0.0)
            if attempts >= 2:
                self._mark_handover_recovery_disposition("HUMAN_REQUIRED", "ARCHITECT_FRESH_CANDIDATE_RETRY_EXHAUSTED")
                return False
            if time.time() < retry_after:
                self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_FRESH_CANDIDATE_RETRY_BACKOFF")
                return False
            self.watcher.state.pop("rolloverFreshCandidateConversationId", None)
            self.watcher.state.pop("rolloverFreshCandidateState", None)
            self.watcher.state.pop("rolloverFreshCandidateRetryAfter", None)
            self.watcher.state.pop("rolloverHandoverResponseIdentity", None)
            self.watcher.save()
            candidate_state = None
        if not handover:
            self._mark_handover_recovery_disposition("RETRYABLE", "ARCHITECT_HANDOVER_NOT_FOUND")
            return False
        response_identity = hashlib.sha256(handover.encode("utf-8")).hexdigest()
        if (self.watcher.state.get("rolloverHandoverResponseIdentity") == response_identity
                and candidate_state not in {"SUBMISSION_AMBIGUOUS", "ACK_PENDING"}
                and not reconstructed_handover):
            return False
        if not self.watcher.state.get("handoverRequested"):
            self.watcher.state.update({
                "handoverRequested": True,
                "handoverReady": False,
                "rolloverInProgress": True,
                "rolloverHandoverSendState": "AMBIGUOUS",
            })
            self.watcher.save()
        return self.watcher.process_pending_handover_response(bridge, handover)

    def handover_reconciliation_pending(self) -> bool:
        """Whether the current transaction is live and awaits read-only reconciliation."""
        state = self.watcher.state
        return bool(
            state.get("rolloverDue")
            and state.get("rolloverPending")
            and state.get("rolloverInProgress")
            and state.get("handoverRequested")
            and state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
            and state.get("rolloverMaintenanceState") in {"IN_PROGRESS", "RECONCILE_PENDING"}
            and state.get("rolloverHandoverRecoveryDisposition") not in {"HUMAN_REQUIRED", "EXHAUSTED"}
        )

    def _mark_handover_recovery_disposition(self, disposition: str, reason: str) -> None:
        previous = (self.watcher.state.get("rolloverHandoverRecoveryDisposition"),
                    self.watcher.state.get("rolloverHandoverRecoveryReason"))
        self.watcher.state["rolloverHandoverRecoveryDisposition"] = disposition
        self.watcher.state["rolloverHandoverRecoveryReason"] = reason
        if disposition == "RETRYABLE":
            # RETRYABLE is still a live transaction disposition.  It must not
            # be converted into terminal DEFERRED merely because one
            # read-only reconciliation did not yet find the response.
            if (self.watcher.state.get("rolloverPending")
                    and (self.watcher.state.get("rolloverInProgress")
                         or self.watcher.state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"})):
                self.watcher.state["rolloverMaintenanceState"] = "RECONCILE_PENDING"
                self.watcher.state.pop("rolloverDeferredForTaskId", None)
            self.watcher.state["rolloverHandoverRecoveryRetryAfter"] = time.time() + 5.0
        else:
            self.watcher.state.pop("rolloverHandoverRecoveryRetryAfter", None)
        self.watcher.save()
        if previous != (disposition, reason):
            event = "ARCHITECT_HANDOVER_RECOVERY_RETRYABLE" if disposition == "RETRYABLE" else "ARCHITECT_HANDOVER_RECOVERY_HUMAN_REQUIRED"
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), event, self.watcher.state, reason=reason)

    def _record_fresh_candidate(self, page: Any, status: str) -> str | None:
        try:
            url = getattr(page, "url", "")
            url = url() if callable(url) else url
            conversation_id = architect_conversation_id_from_url(url)
        except (RuntimeError, StopIteration, TypeError):
            return None
        self.watcher.state.update({
            "rolloverFreshCandidateConversationId": conversation_id,
            "rolloverFreshCandidateState": status,
        })
        self.watcher.save()
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_CANDIDATE_DECISION", self.watcher.state,
                    candidateConversationId=conversation_id, candidateState=status, decision="RECORD", reason="CANDIDATE_PAGE_PROVEN")
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._record_fresh_candidate", "FUNCTION", "END", self.watcher.state, conversationId=conversation_id, candidateState=status)
        return conversation_id

    def _existing_fresh_candidate_page(self, bridge: "ArchitectPlaywright", handover: str | None = None) -> Any | None:
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover._existing_fresh_candidate_page", "FUNCTION", "BEGIN", self.watcher.state, persistedCandidateId=self.watcher.state.get("rolloverFreshCandidateConversationId"))
        candidate_id = self.watcher.state.get("rolloverFreshCandidateConversationId")
        owned_page = getattr(bridge, "_fresh_candidate_page", None)
        if owned_page is not None and not getattr(owned_page, "closed", False):
            return owned_page
        stale_candidate_id = None
        context = getattr(getattr(bridge, "page", None), "context", None)
        pages = getattr(context, "pages", []) if context is not None else []
        if tracer:
            tracer.record("ROLLOVER", "_existing_fresh_candidate_page", "TARGET_INVENTORY", "BEGIN", self.watcher.state, pageCount=len(pages), inventory=[{"index": index, "url": getattr(page, "url", "")} for index, page in enumerate(pages)])
        matches = []
        old_id = None
        if context is not None:
            try:
                old_id = architect_conversation_id_from_url(getattr(bridge.page, "url", ""))
            except (RuntimeError, StopIteration):
                pass
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_CANDIDATE_SCAN", self.watcher.state,
                    oldConversationId=old_id, candidateConversationId=candidate_id, candidateCount=len(pages), decision="SCAN")
        if candidate_id:
            for page in pages:
                try:
                    actual_id = architect_conversation_id_from_url(getattr(page, "url", ""))
                except (RuntimeError, StopIteration, TypeError):
                    continue
                if architect_conversation_ids_equivalent(str(candidate_id), actual_id):
                    runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_CANDIDATE_DECISION", self.watcher.state,
                                oldConversationId=old_id, candidateConversationId=actual_id, candidateCount=1, decision="USE_PERSISTED", reason="PERSISTED_ID_MATCH")
                    return page
            stale_candidate_id = str(candidate_id)
        if (not self.watcher.state.get("rolloverDue") or not self.watcher.state.get("rolloverPending")
                or not self.watcher.state.get("handoverRequested")
                or not isinstance(handover, str) or not self._handover_response_valid(handover)):
            return None
        bootstrap = fresh_architect_bootstrap_payload(handover)
        bootstrap_hash = hashlib.sha256(bootstrap.encode("utf-8")).hexdigest()
        stored_hash = self.watcher.state.get("rolloverFreshBootstrapPayloadHash")
        if stored_hash and stored_hash != bootstrap_hash:
            if self.watcher.state.get("rolloverFreshCandidateDiscoveryState") != "BOOTSTRAP_MISMATCH":
                self.watcher.state["rolloverFreshCandidateDiscoveryState"] = "BOOTSTRAP_MISMATCH"
                self.watcher.save()
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_FRESH_CANDIDATE_BOOTSTRAP_MISMATCH", self.watcher.state)
            return None
        if not stored_hash:
            self.watcher.state["rolloverFreshBootstrapPayloadHash"] = bootstrap_hash
            self.watcher.save()
        proven = []
        for page in pages:
            try:
                actual_id = architect_conversation_id_from_url(getattr(page, "url", ""))
            except (RuntimeError, StopIteration, TypeError):
                continue
            if old_id and architect_conversation_ids_equivalent(old_id, actual_id):
                continue
            candidate_bridge = ArchitectPlaywright(page)
            try:
                user_messages = candidate_bridge.user_message_texts()
                entries = candidate_bridge._assistant_entries()
            except Exception:
                continue
            if not any(isinstance(text, str) and normalize_prompt(text) == normalize_prompt(bootstrap) for text in user_messages):
                continue
            completed = [entry.get("text", "") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("text"), str)]
            if not completed or not architect_session_ready(completed[-1]):
                continue
            proven.append(page)
        if len(proven) > 1:
            self.watcher.state["rolloverFreshCandidateDiscoveryState"] = "AMBIGUOUS"
            self.watcher.save()
            print(f"ARCHITECT_FRESH_CANDIDATE_AMBIGUOUS count={len(proven)}")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_FRESH_CANDIDATE_AMBIGUOUS", self.watcher.state, count=len(proven))
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_CANDIDATE_DECISION", self.watcher.state,
                        oldConversationId=old_id, candidateCount=len(proven), bootstrapHash=bootstrap_hash, decision="BLOCK", reason="MULTIPLE_PROVEN_CANDIDATES")
            return None
        if not proven:
            if self.watcher.state.get("rolloverFreshCandidateDiscoveryState") != "NOT_FOUND":
                self.watcher.state["rolloverFreshCandidateDiscoveryState"] = "NOT_FOUND"
                self.watcher.save()
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_FRESH_CANDIDATE_NOT_FOUND", self.watcher.state)
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_CANDIDATE_DECISION", self.watcher.state,
                            oldConversationId=old_id, candidateCount=0, bootstrapHash=bootstrap_hash, decision="BLOCK", reason="NO_PROVEN_CANDIDATE")
            return None
        if stale_candidate_id:
            actual_id = architect_conversation_id_from_url(getattr(proven[0], "url", ""))
            self.watcher.state.update({
                "rolloverFreshCandidateConversationId": actual_id,
                "rolloverFreshCandidateState": self.watcher.state.get("rolloverFreshCandidateState") or "ACK_PENDING",
                "rolloverFreshCandidateDiscoveryState": "IDENTITY_CORRECTED",
            })
            self.watcher.save()
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_FRESH_CANDIDATE_IDENTITY_CORRECTED", self.watcher.state, oldCandidateId=stale_candidate_id, newCandidateId=actual_id)
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_CANDIDATE_DECISION", self.watcher.state,
                        oldConversationId=old_id, candidateConversationId=actual_id, candidateCount=1, bootstrapHash=bootstrap_hash, decision="USE", reason="CONTENT_PROOF_IDENTITY_CORRECTED")
            return proven[0]
        self.watcher.state.pop("rolloverFreshCandidateDiscoveryState", None)
        self.watcher.save()
        return proven[0] if proven else None

    def complete_from_response(self, bridge: "ArchitectPlaywright", response: str, emit: Callable[[str], None] = print) -> bool:
        tracer = diagnostic_trace_for(self.watcher)
        if tracer:
            tracer.record("ROLLOVER", "ArchitectSessionRollover.complete_from_response", "HANDOVER_WAIT_COMPLETE", "BEGIN", self.watcher.state, responseLength=len(response), responseSha256=hashlib.sha256(response.encode()).hexdigest(), handoverReady=architect_handover_ready(response))
        if not self.watcher.state.get("handoverRequested"):
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HANDOVER_RECOVERY_DECISION", self.watcher.state,
                        transactionId=self.watcher.state.get("rolloverTransactionId"), handoverSendState=self.watcher.state.get("rolloverHandoverSendState"),
                        existingResponseFound=True, transactionMatched=False, decision="BLOCK", reason="HANDOVER_NOT_REQUESTED_OR_NOT_READY")
            return False
        transaction_id = self.watcher.state.get("rolloverTransactionId")
        parsed = parse_handover_envelope(response)
        if not self._handover_response_valid(response, transaction_id):
            trace_transaction_match_failure(self.watcher, response, transaction_id, "TRANSACTION_TOKEN_MISSING_OR_INVALID")
            return False
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "HANDOVER_RECOVERY_DECISION", self.watcher.state,
                    transactionId=transaction_id, transactionTaskId=self.watcher.state.get("rolloverTransactionTaskId"),
                    handoverSendState=self.watcher.state.get("rolloverHandoverSendState"), existingResponseFound=True,
                    transactionMatched=True, protocol="CANONICAL" if parsed is not None else "LEGACY_COMPATIBILITY",
                    decision="ACCEPT", reason="VALID_HANDOVER_RESPONSE")
        if not self.persist_validated_handover(response):
            return False
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_HANDOVER_READY", self.watcher.state)
        old_page = bridge.page
        new_page = None
        committed = False
        phase = "OPEN_FRESH_WITH_HANDOVER"
        workflow_snapshot = {
            key: self.watcher.state.get(key)
            for key in ("state", "taskId", "lastCompletedTaskId", "executorResultPath", "humanRequiredReason", "architectSendState", "architectDeliveryFailureClass")
        }
        self.watcher.state["rolloverInProgress"] = True
        try:
            new_page = self._existing_fresh_candidate_page(bridge, response)
            if new_page is None:
                if self.watcher.state.get("rolloverFreshPageCreated"):
                    raise RuntimeError("ARCHITECT_FRESH_PAGE_REPLACEMENT_FORBIDDEN")
                self.watcher.state["rolloverFreshPageCreated"] = True
                self.watcher.save()
                new_page = bridge.open_fresh_with_handover(response)
            else:
                if hasattr(bridge, "_fresh_candidate_submission_ambiguous"):
                    bridge._fresh_candidate_submission_ambiguous = False
                self.watcher.state["rolloverFreshCandidateState"] = "ACK_PENDING"
                self.watcher.save()
            phase = "WAIT_NEW_CONVERSATION_ID"
            deadline = time.monotonic() + 15.0
            conversation_id = None
            while time.monotonic() < deadline:
                current_url = getattr(new_page, "url", "")
                current_url = current_url() if callable(current_url) else current_url
                try:
                    conversation_id = architect_conversation_id_from_url(current_url)
                    self.watcher.state.update({"rolloverFreshCandidateConversationId": conversation_id, "rolloverFreshCandidateState": "ACK_PENDING"})
                    self.watcher.save()
                    break
                except RuntimeError:
                    if tracer:
                        tracer.record("ROLLOVER", "complete_from_response", "WAIT_BEGIN", "BEGIN", self.watcher.state, reason="new conversation URL", requestedSeconds=0.1)
                    time.sleep(0.1)
                    if tracer:
                        tracer.record("ROLLOVER", "complete_from_response", "WAIT_END", "END", self.watcher.state, reason="new conversation URL")
            if conversation_id is None:
                raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ID_TIMEOUT")
            phase = "WAIT_NEW_CONVERSATION_ACK"
            if tracer:
                tracer.record("ROLLOVER", "complete_from_response", "FRESH_READY_WAIT_BEGIN", "BEGIN", self.watcher.state, conversationId=conversation_id)
            ack_deadline = time.monotonic() + 30.0
            new_bridge = ArchitectPlaywright(new_page)
            new_bridge.runtime_logger = getattr(self.watcher, "runtime_logger", None)
            new_bridge.runtime_run_id = getattr(self.watcher, "runtime_run_id", None)
            new_bridge.runtime_watcher = self.watcher
            new_bridge.runtime_conversation_id = conversation_id
            while time.monotonic() < ack_deadline:
                if new_bridge.generation_visible():
                    if tracer:
                        tracer.record("ROLLOVER", "complete_from_response", "WAIT_BEGIN", "BEGIN", self.watcher.state, reason="fresh session generation", requestedSeconds=0.25)
                    time.sleep(0.25)
                    if tracer:
                        tracer.record("ROLLOVER", "complete_from_response", "WAIT_END", "END", self.watcher.state, reason="fresh session generation")
                    continue
                entries = new_bridge._assistant_entries()
                if entries:
                    latest = entries[-1].get("text", "") if isinstance(entries[-1], dict) else ""
                    if architect_session_ready(latest):
                        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "FRESH_ARCHITECT_ACK_DECISION", self.watcher.state,
                                    oldConversationId=self.watcher.state.get("architectConversationId"), candidateConversationId=conversation_id,
                                    sessionReadyObserved=True, candidateState="ACK_PENDING", decision="ACCEPT", reason="ARCHITECT_SESSION_READY")
                        if tracer:
                            tracer.record("ROLLOVER", "complete_from_response", "FRESH_READY_WAIT_COMPLETE", "END", self.watcher.state, conversationId=conversation_id, assistantSha256=hashlib.sha256(latest.encode()).hexdigest())
                        break
                    if latest.strip():
                        raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ACK_INVALID")
                time.sleep(0.25)
            else:
                raise RuntimeError("ARCHITECT_NEW_CONVERSATION_ACK_TIMEOUT")
            final_url = getattr(new_page, "url", "")
            final_url = final_url() if callable(final_url) else final_url
            conversation_id = architect_conversation_id_from_url(final_url)
            phase = "COMMIT_NEW_CONVERSATION_AUTHORITY"
            old_conversation_id = self.watcher.state.get("architectConversationId")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_AUTHORITY_COMMIT_DECISION", self.watcher.state,
                        oldConversationId=old_conversation_id, newConversationId=conversation_id,
                        sessionReadyProven=True, commitAllowed=True, commitCompleted=False, oldPageCloseAllowed=False, reason="FRESH_SESSION_READY")
            if tracer:
                tracer.record("ROLLOVER", "complete_from_response", "AUTHORITY_COMMIT_BEGIN", "BEGIN", self.watcher.state, conversationId=conversation_id)
            self.watcher.state["architectConversationId"] = conversation_id
            if (self.watcher.state.get("postDiscussionEnvelopeRequired") is True
                    and self.watcher.state.get("postDiscussionProtocolTransactionId") == transaction_id
                    and self.watcher.state.get("postDiscussionProtocolTaskId") == self.watcher.state.get("nextTaskId")):
                self.watcher.state["postDiscussionProtocolRolloverCommittedTransactionId"] = transaction_id
            self.watcher.state["architectMemorySessionId"] = conversation_id
            self.watcher.state.pop("architectMemoryBytes", None)
            self.watcher.state.pop("architectMemoryMiB", None)
            self.watcher.state.pop("currentArchitectConversationId", None)
            self.watcher.state["architectResponseCount"] = 0
            self.watcher.state["handoverRequested"] = False
            self.watcher.state["handoverReady"] = False
            self.watcher.state["rolloverPending"] = False
            self.watcher.state["rolloverDue"] = False
            self.watcher.state["rolloverInProgress"] = False
            self.watcher.state.pop("rolloverAttemptedForTaskId", None)
            self.watcher.state.pop("rolloverTrigger", None)
            self.watcher.state.pop("rolloverHandoverSendState", None)
            self.watcher.state.pop("rolloverHandoverResponseIdentity", None)
            self.watcher.state.pop("rolloverFreshCandidateConversationId", None)
            self.watcher.state.pop("rolloverFreshCandidateState", None)
            self.watcher.state.pop("rolloverFreshCandidateRetryAfter", None)
            self.watcher.state.pop("rolloverFreshCandidateAttemptCount", None)
            self.watcher.state.pop("rolloverFreshBootstrapPayloadHash", None)
            self.watcher.state.pop("rolloverFreshCandidateDiscoveryState", None)
            self.watcher.state.pop("rolloverFreshPageCreated", None)
            self.watcher.state.pop("rolloverLegacyHandoverReemissionTransactionId", None)
            self.watcher.state.pop("rolloverLegacyHandoverReemissionAttemptedEpoch", None)
            self.watcher.state.pop("rolloverLegacyHandoverReemissionState", None)
            self.watcher.state.pop("rolloverTransactionId", None)
            self.watcher.state.pop("rolloverTransactionTaskId", None)
            self.watcher.state.pop("rolloverHandoverRecoveryDisposition", None)
            self.watcher.state.pop("rolloverHandoverRecoveryReason", None)
            self.watcher.state.pop("rolloverHandoverRecoveryRetryAfter", None)
            self.watcher.state.pop("pending_handover", None)
            self.watcher.save()
            committed = True
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_AUTHORITY_COMMIT_DECISION", self.watcher.state,
                        oldConversationId=old_conversation_id, newConversationId=conversation_id, sessionReadyProven=True,
                        commitAllowed=True, commitCompleted=True, oldPageCloseAllowed=True, reason="DURABLE_AUTHORITY_COMMITTED")
            if tracer:
                tracer.record("ROLLOVER", "complete_from_response", "AUTHORITY_COMMIT_END", "END", self.watcher.state, conversationId=conversation_id)
            bridge.page = new_page
            bind_memory = getattr(self.watcher, "bind_architect_session_memory", None)
            if callable(bind_memory):
                bind_memory(bridge, conversation_id)
            if hasattr(old_page, "close"):
                if tracer:
                    tracer.record("PLAYWRIGHT", "complete_from_response", "OLD_ARCHITECT_CLOSE_BEGIN", "BEGIN", self.watcher.state, conversationId=conversation_id, mutation=True)
                try:
                    old_page.close()
                except Exception:
                    pass
                if tracer:
                    tracer.record("PLAYWRIGHT", "complete_from_response", "OLD_ARCHITECT_CLOSE_END", "END", self.watcher.state, conversationId=conversation_id, mutation=True)
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_NEW_CONVERSATION_CREATED", self.watcher.state, conversationId=conversation_id)
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_CONVERSATION_SWITCHED", self.watcher.state, conversationId=conversation_id)
            emit("ARCHITECT_SESSION_ROLLOVER_COMPLETE")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_SESSION_ROLLOVER_COMPLETE", self.watcher.state)
            return True
        except Exception as error:
            if new_page is None:
                new_page = getattr(bridge, "_fresh_candidate_page", None)
            ambiguous_submission = bool(getattr(bridge, "_fresh_candidate_submission_ambiguous", False)) or (
                isinstance(error, ResultSubmissionError) and error.code in {
                    "ARCHITECT_SUBMISSION_ACK_TIMEOUT",
                    "ARCHITECT_FRESH_BOOTSTRAP_SEND_AMBIGUOUS",
                    "ARCHITECT_FRESH_BOOTSTRAP_SEND_FAILED",
                }
            )
            if new_page is not None and not committed:
                self._record_fresh_candidate(new_page, "SUBMISSION_AMBIGUOUS" if ambiguous_submission else "FAILED")
            if not committed and not ambiguous_submission and new_page is not None and new_page is not old_page and hasattr(new_page, "close"):
                try:
                    new_page.close()
                except Exception:
                    pass
            for key, value in workflow_snapshot.items():
                if value is None:
                    self.watcher.state.pop(key, None)
                else:
                    self.watcher.state[key] = value
            self.watcher.state["rolloverInProgress"] = False
            self.watcher.state["rolloverDue"] = True
            self.watcher.state["rolloverPending"] = True
            self.watcher.state["handoverReady"] = False
            if ambiguous_submission and not self.watcher.state.get("rolloverFreshCandidateConversationId"):
                self.watcher.state["rolloverMaintenanceState"] = "DEFERRED"
            if str(error) == "ARCHITECT_FRESH_PAGE_REPLACEMENT_FORBIDDEN":
                self.watcher.state["rolloverMaintenanceState"] = "DEFERRED"
            if not ambiguous_submission:
                attempts = int(self.watcher.state.get("rolloverFreshCandidateAttemptCount", 0)) + 1
                self.watcher.state.update({
                    "rolloverFreshCandidateState": "FAILED",
                    "rolloverFreshCandidateAttemptCount": attempts,
                    "rolloverFreshCandidateRetryAfter": time.time() + 5.0,
                })
            self.watcher.save()
            candidate = str(error)
            code = candidate if re.fullmatch(r"[A-Z0-9_:]+", candidate) else "ARCHITECT_SESSION_ROLLOVER_FAILED"
            error_message = candidate.replace("\r", " ").replace("\n", " ")[:500]
            emit(f"{code} phase={phase} errorClass={type(error).__name__}")
            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "ARCHITECT_SESSION_ROLLOVER_FAILED", self.watcher.state, error=code, errorClass=type(error).__name__, errorMessage=error_message, phase=phase)
            emit("STATE=ROLLOVER_PENDING")
            return False


class LoopGuard:
    def __init__(self, state: dict[str, Any] | None = None):
        self.last_prompt_hash = (state or {}).get("last_prompt_hash")
        self.last_result_hash = (state or {}).get("last_result_hash")

    def check(self, prompt: str, milestone: str | None = None, blocked: bool = False) -> str:
        prompt_hash = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        if self.last_prompt_hash == prompt_hash and (blocked or self.last_result_hash is None):
            return "LOOP_SUSPECTED"
        self.last_prompt_hash = prompt_hash
        return "FORWARD"

    def record_result(self, result: str) -> None:
        self.last_result_hash = hashlib.sha256(result.encode()).hexdigest()


@dataclass
class CodexResult:
    state: str
    output: str
    exit_code: int | None
    timed_out: bool
    stdout: str = ""
    stderr: str = ""
    last_message_path: str | None = None
    last_message_exists: bool = False

    @property
    def command_form(self) -> str:
        return "codex exec --ephemeral --sandbox read-only -C <project> -o <unique-last-message-file> -"


class CodexRunner:
    def __init__(self, project_dir: str | os.PathLike[str], executable: str = "codex", bootstrap_path: str | os.PathLike[str] | None = None, child_project_dir: str | os.PathLike[str] | None = None, child_identity_verifier: Callable[[str], dict[str, Any]] | None = None, session_id: str | None = None):
        self.project_dir = str(project_dir)
        self.child_project_dir = str(child_project_dir) if child_project_dir else None
        self.child_identity_verifier = child_identity_verifier or verify_child_project_binding
        self.session_id = session_id
        self.executable = executable
        self.bootstrap_path = Path(bootstrap_path) if bootstrap_path else Path(self.project_dir) / "AFFOTECH_EXECUTOR_BOOTSTRAP.md"
        self.launcher = discover_codex_launcher(executable)
        self.running_observed = False
        self.last_pid: int | None = None
        self.on_start: Callable[[int], None] | None = None
        self.lifecycle_state = "CLOSED"
        self.active_child_pid: int | None = None
        self.relay_authority: dict[str, Any] | None = None

    def run(self, prompt: str, timeout: float = 300.0) -> CodexResult:
        """Run until the child exits; ``timeout`` is retained for API compatibility.

        Executor duration is deliberately not governed by a wall-clock deadline.
        Callers may use short timers for transport/polling, but only child exit
        evidence determines executor completion.
        """
        assembled_prompt = self.assemble_prompt(prompt, self.relay_authority)
        try:
            assembled_prompt_bytes = assembled_prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise RuntimeError("CODEX_PROMPT_UTF8_ENCODE_FAILED") from error
        handle, last_message_path = tempfile.mkstemp(prefix="codex-last-message-", suffix=".txt")
        os.close(handle)
        try:
            os.unlink(last_message_path)
        except FileNotFoundError:
            pass
        if self.session_id:
            verify_executor_session(self.session_id)
            args = ["exec", "resume", self.session_id, "-o", last_message_path, "-"]
        else:
            args = ["exec", "--ephemeral", "--sandbox", "read-only", "-C", self.project_dir, "-o", last_message_path, "-"]
        if os.name == "nt" and self.launcher[0].lower().endswith(".ps1"):
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.executable, *args]
        else:
            command = [*self.launcher, *args]
        child_cwd = self.child_project_dir or self.project_dir
        if self.child_project_dir:
            self.child_identity = self.child_identity_verifier(child_cwd)
            if "-C" in command:
                command[command.index("-C") + 1] = child_cwd
        if self.lifecycle_state != "CLOSED" or self.running_observed:
            raise RuntimeError("CODEX_CHILD_LIFECYCLE_NOT_CLOSED")
        self.lifecycle_state = "STARTING"
        if self._use_visible_windows_console():
            try:
                return self._run_visible_windows_console(assembled_prompt, timeout, command, last_message_path, child_cwd)
            finally:
                self.lifecycle_state = "CLOSED"
                self.running_observed = False
                self.active_child_pid = None
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict", cwd=child_cwd)
        self.last_pid = process.pid
        self.active_child_pid = process.pid
        if self.on_start:
            self.on_start(process.pid)
        self.running_observed = process.poll() is None
        stdout, stderr = process.communicate(input=assembled_prompt)
        returncode = process.wait()
        if not isinstance(returncode, int):
            raise RuntimeError("CODEX_RETURN_CODE_NOT_INTEGER")
        completed = type("Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
        self.lifecycle_state = "CLOSED"
        self.running_observed = False
        self.active_child_pid = None
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        last_message = Path(last_message_path).read_text(encoding="utf-8") if os.path.exists(last_message_path) else ""
        output = last_message.strip() or stdout.strip() or stderr.strip()
        if completed.returncode != 0:
            return CodexResult("BLOCKED", output, completed.returncode, False, stdout, stderr, last_message_path, bool(last_message))
        return CodexResult("COMPLETED" if last_message.strip() else "FAILED", output, completed.returncode, False, stdout, stderr, last_message_path, bool(last_message))

    def _use_visible_windows_console(self) -> bool:
        """Use the visible host only for an actual production subprocess call."""
        return (
            os.name == "nt"
            and self.executable == "codex"
            and os.environ.get("AFFOTECH_VISIBLE_EXECUTOR", "1") != "0"
            and getattr(subprocess.Popen, "__module__", "subprocess") == "subprocess"
        )

    def _run_visible_windows_console(self, assembled_prompt: str, timeout: float, command: list[str], last_message_path: str, child_cwd: str | None = None) -> CodexResult:
        """Run the real Codex child in a new visible console and poll its status."""
        try:
            assembled_prompt_bytes = assembled_prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise RuntimeError("CODEX_PROMPT_UTF8_ENCODE_FAILED") from error
        status_handle, status_path = tempfile.mkstemp(prefix="codex-visible-status-", suffix=".json")
        os.close(status_handle)
        Path(status_path).write_text("{}", encoding="utf-8")
        launcher_handle, launcher_path = tempfile.mkstemp(prefix="codex-visible-console-", suffix=".js")
        os.close(launcher_handle)
        launcher = r'''const fs = require("fs");
const { spawn } = require("child_process");
const statusPath = process.argv[2];
const command = process.argv.slice(3);
const write = (value) => fs.writeFileSync(statusPath, JSON.stringify(value));
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  console.log("==================================================");
  console.log("AFFOTECH AUTOMATED EXECUTOR");
  console.log("==================================================");
  console.log("Prompt received from Architect");
  console.log("Executor is running");
  console.log("Do not close this window while execution is active");
  console.log("==================================================");
  const child = spawn(command[0], command.slice(1), { stdio: ["pipe", "inherit", "inherit"] });
  write({ phase: "STARTED", pid: child.pid });
  child.stdin.end(input);
  child.on("close", (code) => {
    write({ phase: "FINISHED", pid: child.pid, exitCode: code });
    console.log("==================================================");
    console.log("EXECUTOR FINISHED");
    console.log("Exit code: " + code);
    console.log("Result returned to Architect");
    console.log("==================================================");
    console.log("Executor console closing after result capture.");
  });
});
'''
        with open(launcher_path, "w", encoding="utf-8", newline="\n") as launcher_file:
            launcher_file.write(launcher)
        host_command = [self.launcher[0], launcher_path, status_path, *command]
        try:
            host = subprocess.Popen(
                host_command,
                stdin=subprocess.PIPE,
                stdout=None,
                stderr=None,
                text=False,
                cwd=child_cwd or self.project_dir,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
            if host.stdin is None:
                raise RuntimeError("VISIBLE_EXECUTOR_STDIN_UNAVAILABLE")
            host.stdin.write(assembled_prompt_bytes)
            host.stdin.close()
            status: dict[str, Any] = {}
            while True:
                try:
                    candidate = json.loads(Path(status_path).read_text(encoding="utf-8"))
                    if isinstance(candidate, dict):
                        status = candidate
                except (OSError, json.JSONDecodeError):
                    pass
                if status.get("phase") == "STARTED" and isinstance(status.get("pid"), int):
                    self.last_pid = status["pid"]
                    if self.on_start:
                        self.on_start(status["pid"])
                        self.on_start = None
                if status.get("phase") == "FINISHED":
                    exit_code = status.get("exitCode")
                    if not isinstance(exit_code, int):
                        raise RuntimeError("CODEX_RETURN_CODE_NOT_INTEGER")
                    break
                time.sleep(0.05)
            stdout = ""
            stderr = ""
            exit_code = int(status["exitCode"])
        finally:
            try:
                os.unlink(launcher_path)
            except FileNotFoundError:
                pass
            try:
                os.unlink(status_path)
            except FileNotFoundError:
                pass
        last_message = Path(last_message_path).read_text(encoding="utf-8") if os.path.exists(last_message_path) else ""
        output = last_message.strip() or stderr.strip()
        if exit_code != 0:
            return CodexResult("BLOCKED", output, exit_code, False, stdout, stderr, last_message_path, bool(last_message))
        return CodexResult("COMPLETED" if last_message.strip() else "FAILED", output, exit_code, False, stdout, stderr, last_message_path, bool(last_message))

    def assemble_prompt(self, task_prompt: str, relay_authority: dict[str, Any] | None = None) -> str:
        try:
            bootstrap = self.bootstrap_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(f"AFFOTECH_EXECUTOR_BOOTSTRAP_UNAVAILABLE:{self.bootstrap_path}") from error
        if not bootstrap.strip():
            raise RuntimeError("AFFOTECH_EXECUTOR_BOOTSTRAP_EMPTY")
        context = ""
        if relay_authority is not None:
            snapshot = relay_authority.get("snapshotCommit")
            publication = relay_authority.get("publicationId")
            content_hash = relay_authority.get("contentSha256")
            if not all(isinstance(value, str) and value for value in (snapshot, publication, content_hash)):
                raise RuntimeError("RELAY_AUTHORITY_CONTEXT_INVALID")
            context = "\n\n".join((
                "ORCHESTRATOR_VALIDATED_RELAY_SNAPSHOT",
                f"snapshotCommit={snapshot}",
                f"publicationId={publication}",
                f"contentSha256={content_hash}",
                "Use this already-validated immutable snapshot for the current task; do not substitute another relay generation.",
            ))
        return bootstrap.rstrip("\r\n") + ("\n\n" + context if context else "") + "\n\n" + task_prompt


def discover_codex_launcher(executable: str = "codex") -> list[str]:
    if os.name != "nt" or executable != "codex":
        return [executable]
    cmd = shutil.which("codex.cmd") or shutil.which("codex")
    if not cmd:
        raise FileNotFoundError("CODEX_COMMAND_NOT_FOUND")
    text = Path(cmd).read_text(encoding="utf-8", errors="replace")
    match = re.search(r'node_modules[\\/]+@openai[\\/]codex[\\/]bin[\\/]codex\.js', text, re.I)
    if not match:
        raise RuntimeError("CODEX_SHIM_TARGET_NOT_RESOLVED")
    script = str(Path(cmd).parent / Path(match.group(0).replace("\\", "/")).as_posix())
    node = str(Path(cmd).parent / "node.exe")
    if not Path(node).exists():
        node = shutil.which("node") or "node"
    return [node, script]


def assistant_entries_script() -> str:
    """Return the exact read-only semantic assistant extraction script."""
    return r"""
            () => {
              const clean = (source) => {
                const clone = source.cloneNode(true);
                clone.querySelectorAll(
                  'button,[role="button"],[aria-hidden="true"],' +
                  '[data-testid="writing-block-suggested-followups"],' +
                  '[data-testid="writing-block-suggested-followups-surface"]'
                ).forEach(control => control.remove());
                return clone.innerText || clone.textContent || '';
              };
              return [...document.querySelectorAll('[data-message-author-role="assistant"]')]
                .filter((node) => node.isConnected)
                .map((node) => {
                  const writingBlocks = [...node.querySelectorAll('[data-testid="writing-block-container"]')]
                    .filter((block) => block.isConnected);
                  const semanticSource = writingBlocks.length ? 'WRITING_BLOCK' : 'STANDARD_RESPONSE';
                  // Writing blocks are part of the assistant response, not a
                  // replacement for the surrounding response text. Clean the
                  // complete node once so legitimate prefixes/suffixes remain
                  // available to response and handover matching.
                  const text = clean(node);
                  return {
                    id: node.getAttribute('data-message-id'),
                    rawText: node.innerText || node.textContent || '',
                    text,
                    semanticSource
                  };
                });
            }
            """


def assistant_fast_snapshot_script() -> str:
    """Read only the mounted assistant count and latest node signature."""
    return r"""
            () => {
              const nodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')]
                .filter((node) => node.isConnected);
              const latest = nodes.length ? nodes[nodes.length - 1] : null;
              const text = latest ? (latest.innerText || latest.textContent || '') : '';
              return {
                count: nodes.length,
                latestMessageId: latest ? latest.getAttribute('data-message-id') : null,
                latestText: text,
                latestTextLength: text.length
              };
            }
            """


def assistant_latest_entry_script() -> str:
    """Semantically extract one latest assistant node, never the transcript."""
    return r"""
            () => {
              const nodes = [...document.querySelectorAll('[data-message-author-role="assistant"]')]
                .filter((node) => node.isConnected);
              const node = nodes.length ? nodes[nodes.length - 1] : null;
              if (!node) return null;
              const clean = (source) => {
                const clone = source.cloneNode(true);
                clone.querySelectorAll(
                  'button,[role="button"],[aria-hidden="true"],' +
                  '[data-testid="writing-block-suggested-followups"],' +
                  '[data-testid="writing-block-suggested-followups-surface"]'
                ).forEach(control => control.remove());
                return clone.innerText || clone.textContent || '';
              };
              const writingBlocks = [...node.querySelectorAll('[data-testid="writing-block-container"]')]
                .filter((block) => block.isConnected);
              return {
                id: node.getAttribute('data-message-id'),
                rawText: node.innerText || node.textContent || '',
                text: clean(node),
                semanticSource: writingBlocks.length ? 'WRITING_BLOCK' : 'STANDARD_RESPONSE'
              };
            }
            """


class ArchitectPlaywright:
    """Semantic Playwright boundary; it never targets AFFOTECH pages."""
    def __init__(self, page: Any):
        self.page = page
        self.last_state = "NOT_YET"
        self.diagnostic_trace = diagnostic_trace_for()
        self._diagnostic_connection_id = None
        self.initialSendMethod = None
        self.initialSendActionReturned = False
        self.alternateSendAttempted = False
        self.alternateSendMethod = None
        self._fast_observation_count = 0
        self._latest_response_extraction_count = 0
        self._full_history_scan_count = 0
        self._historical_assistant_extraction_count = 0
        self._historical_user_extraction_count = 0
        self._observation_recovery_fallback_count = 0

    def _trace_operation(self, operation: str, phase: str, started: float | None = None, **fields: Any) -> None:
        tracer = getattr(self, "diagnostic_trace", None)
        if tracer:
            tracer.record("PLAYWRIGHT", "ArchitectPlaywright", operation, phase, {}, duration_ms=(time.monotonic() - started) * 1000 if started else None, connectionId=getattr(self, "_diagnostic_connection_id", None), **fields)

    def latest_response(self) -> str:
        return self.page.get_by_role("main").inner_text()

    def current_session_memory_bytes(self) -> int:
        """Return the OS working set of the renderer owned by the current tab."""
        started = time.monotonic()
        self._trace_operation("current_session_memory_bytes", "BEGIN", mutation=False)
        browser = getattr(self, "_browser", None)
        if browser is None:
            raise RuntimeError("ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
        pages = [page for context in browser.contexts for page in context.pages
                 if str(getattr(page, "url", "") or "").startswith("https://chatgpt.com/c/")]
        if len(pages) != 1 or pages[0] is not self.page:
            raise RuntimeError("ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
        session = None
        try:
            session = browser.new_browser_cdp_session()
            process_info = session.send("SystemInfo.getProcessInfo").get("processInfo", [])
            browser_ids = {int(row["id"]) for row in process_info if row.get("type") == "browser"}
            renderer_ids = {int(row["id"]) for row in process_info if row.get("type") == "renderer"}
            if len(browser_ids) != 1:
                raise RuntimeError("ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
            result = architect_renderer_working_set_bytes(next(iter(browser_ids)), renderer_ids)
        except (KeyError, TypeError, ValueError, RuntimeError):
            self._trace_operation("current_session_memory_bytes", "ERROR", started, mutation=False, error="ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
            raise RuntimeError("ARCHITECT_SESSION_MEMORY_UNAVAILABLE")
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
        self._trace_operation("current_session_memory_bytes", "END", started, mutation=False, memoryBytes=result)
        return result

    def _assistant_entries(self) -> list[dict[str, str | None]]:
        self._full_history_scan_count += 1
        self._historical_assistant_extraction_count += 1
        started = time.monotonic()
        self._trace_operation("assistant_entries", "BEGIN", selector='[data-message-author-role="assistant"]', mutation=False)
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is not None:
            script = assistant_entries_script()
            last_error = None
            for attempt_number in range(1, 4):
                try:
                    result = evaluate(script)
                    if not isinstance(result, list):
                        raise RuntimeError("ASSISTANT_SNAPSHOT_INVALID")
                    entries = []
                    for item in (result or []):
                        if not isinstance(item, dict):
                            continue
                        entry = {"id": item.get("id"), "text": item.get("text", "")}
                        if "rawText" in item:
                            entry["rawText"] = item.get("rawText", "")
                        if "semanticSource" in item:
                            entry["semanticSource"] = item.get("semanticSource", "STANDARD_RESPONSE")
                        entries.append(entry)
                    self._trace_operation("assistant_entries", "END", started, count=len(entries), mutation=False)
                    for entry in entries:
                        text = str(entry.get("text") or "")
                        raw_text = str(entry.get("rawText") or "")
                        if self.diagnostic_trace:
                            fields = {"messageId": entry.get("id"), "semanticSource": entry.get("semanticSource"),
                                      "rawTextLength": len(raw_text), "semanticTextLength": len(text),
                                      "rawSha256": hashlib.sha256(raw_text.encode()).hexdigest(),
                                      "semanticSha256": hashlib.sha256(text.encode()).hexdigest(),
                                      "sha256": hashlib.sha256(text.encode()).hexdigest(),
                                      "containsHandoverReady": HANDOVER_READY in text, "architectHandoverReady": architect_handover_ready(text)}
                            if architect_handover_ready(text):
                                fields["snapshotPath"] = self.diagnostic_trace.snapshot_text(f"assistant-{entry.get('id') or 'unknown'}", text, raw_text)
                                fields["rawSnapshotPath"] = fields["snapshotPath"].replace("-sanitizedText.txt", "-rawText.txt") if fields["snapshotPath"] else None
                                fields["semanticSnapshotPath"] = fields["snapshotPath"]
                            self.diagnostic_trace.record("HANDOVER", "_assistant_entries", "ASSISTANT_ENTRY", "END", {}, **fields)
                    return entries
                except Exception as error:  # transient DOM replacement; retry the whole snapshot
                    last_error = error
                    if self.diagnostic_trace:
                        self.diagnostic_trace.record(
                            "HANDOVER", "_assistant_entries", "ASSISTANT_ENTRIES_ERROR", "ERROR", {},
                            attemptNumber=attempt_number,
                            connectionId=getattr(self, "_diagnostic_connection_id", None),
                            errorClass=type(error).__name__, errorMessage=str(error),
                            stackTrace=traceback.format_exc(),
                        )
                    if attempt_number < 3:
                        time.sleep(0.05)
            if last_error:
                if self.diagnostic_trace:
                    self.diagnostic_trace.record(
                        "HANDOVER", "_assistant_entries", "ASSISTANT_ENTRIES_EXHAUSTED", "ERROR", {},
                        attemptNumber=3,
                        connectionId=getattr(self, "_diagnostic_connection_id", None),
                        errorClass=type(last_error).__name__, errorMessage=str(last_error),
                    )
                raise last_error
            raise RuntimeError("ASSISTANT_SNAPSHOT_INVALID")
        messages = self.page.locator('[data-message-author-role="assistant"]')
        entries = []
        for index in range(messages.count()):
            message = messages.nth(index)
            get_attribute = getattr(message, "get_attribute", lambda name: None)
            entries.append({"id": get_attribute("data-message-id"), "text": message.inner_text()})
        self._trace_operation("assistant_entries", "END", started, count=len(entries), mutation=False)
        return entries

    def assistant_fast_snapshot(self) -> dict[str, Any]:
        """Return a compact latest-only observation for healthy polling."""
        self._fast_observation_count = getattr(self, "_fast_observation_count", 0) + 1
        runtime_log(
            getattr(self, "runtime_logger", None),
            getattr(self, "runtime_run_id", None),
            "ARCHITECT_FAST_OBSERVATION",
            getattr(getattr(self, "runtime_watcher", None), "state", None),
            observationCount=self._fast_observation_count,
            fullHistoryScan=False,
        )
        if not hasattr(self, "page"):
            entries = self._assistant_entries()
            latest = entries[-1] if entries else {}
            text = str(latest.get("text") or "")
            return {
                "count": len(entries),
                "latestMessageId": latest.get("id"),
                "latestText": text,
                "latestTextHash": hashlib.sha256(text.encode()).hexdigest(),
                "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "latestTextLength": len(text),
                "_fixtureEntries": True,
            }
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is not None:
            result = evaluate(assistant_fast_snapshot_script())
            # Lightweight test doubles often return the old full-snapshot shape.
            # Normalize it without changing the production DOM path.
            if isinstance(result, list):
                entries = [item for item in result if isinstance(item, dict)]
                latest = entries[-1] if entries else {}
                text = str(latest.get("text") or "")
                text_hash = hashlib.sha256(text.encode()).hexdigest()
                return {
                    "count": len(entries),
                    "latestMessageId": latest.get("id"),
                    "latestText": text,
                    "latestTextHash": text_hash,
                    "text_hash": text_hash,
                    "latestTextLength": len(text),
                    "_fixtureEntries": True,
                }
            if not isinstance(result, dict):
                raise RuntimeError("ASSISTANT_FAST_SNAPSHOT_INVALID")
            text = str(result.get("latestText") or "")
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            return {
                "count": int(result.get("count") or 0),
                "latestMessageId": result.get("latestMessageId"),
                "latestTextHash": text_hash,
                "text_hash": text_hash,
                "latestTextLength": int(result.get("latestTextLength") or len(text)),
                "latestText": text,
            }
        messages = self.page.locator('[data-message-author-role="assistant"]')
        count = messages.count()
        if not count:
            empty_hash = hashlib.sha256(b"").hexdigest()
            return {"count": 0, "latestMessageId": None, "latestTextHash": empty_hash, "text_hash": empty_hash, "latestTextLength": 0, "latestText": ""}
        latest = messages.nth(count - 1)
        text = latest.inner_text()
        identity = getattr(latest, "get_attribute", lambda _name: None)("data-message-id")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return {"count": count, "latestMessageId": identity, "latestTextHash": text_hash, "text_hash": text_hash, "latestTextLength": len(text), "latestText": text}

    def latest_assistant_entry(self) -> dict[str, Any] | None:
        """Extract semantic text from only the latest mounted assistant node."""
        self._latest_response_extraction_count = getattr(self, "_latest_response_extraction_count", 0) + 1
        runtime_log(
            getattr(self, "runtime_logger", None),
            getattr(self, "runtime_run_id", None),
            "ARCHITECT_LATEST_RESPONSE_EXTRACTED",
            getattr(getattr(self, "runtime_watcher", None), "state", None),
            extractionCount=self._latest_response_extraction_count,
            fullHistoryScan=False,
        )
        if not hasattr(self, "page"):
            entries = self._assistant_entries()
            return entries[-1] if entries else None
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is not None:
            result = evaluate(assistant_latest_entry_script())
            if isinstance(result, list):
                result = result[-1] if result else None
            if result is None:
                return None
            if not isinstance(result, dict):
                raise RuntimeError("ASSISTANT_LATEST_ENTRY_INVALID")
            return {"id": result.get("id"), "text": result.get("text", ""), "rawText": result.get("rawText", ""), "semanticSource": result.get("semanticSource", "STANDARD_RESPONSE")}
        return None

    def observation_metrics(self) -> dict[str, int]:
        return {
            "fastObservations": getattr(self, "_fast_observation_count", 0),
            "latestResponseExtractions": getattr(self, "_latest_response_extraction_count", 0),
            "fullHistoryScans": getattr(self, "_full_history_scan_count", 0),
            "historicalAssistantExtractions": getattr(self, "_historical_assistant_extraction_count", 0),
            "historicalUserExtractions": getattr(self, "_historical_user_extraction_count", 0),
            "observationRecoveryFallbacks": getattr(self, "_observation_recovery_fallback_count", 0),
        }

    def assistant_baseline(self) -> dict[str, Any]:
        entries = self._assistant_entries()
        snapshot = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        return {"count": len(entries), "text_hash": hashlib.sha256(snapshot.encode()).hexdigest(), "entries": entries}

    def assistant_count(self) -> int:
        return self.page.locator('[data-message-author-role="assistant"]').count()

    def _assistant_ids(self) -> list[str]:
        return [entry["id"] for entry in self._assistant_entries() if entry.get("id")]

    def latest_completed_executor_prompt(self) -> tuple[str, str] | None:
        """Return the newest valid completed Executor block already in the DOM."""
        for entry in reversed(self._assistant_entries()):
            response = entry.get("text") or ""
            prompt = extract_executor_prompt(response)
            if prompt is not None:
                return response, prompt
        return None

    def latest_executor_prompt(self) -> tuple[str, str] | None:
        for entry in reversed(self._assistant_entries()):
            response = entry.get("text") or ""
            prompt = extract_executor_prompt_envelope(response)
            if prompt is not None:
                return response, prompt
        return None

    def load_older_history(self, max_steps: int = 64, max_seconds: float = 90.0, delay: float = 0.25, emit: Callable[[str], None] | None = None, stop_when: Callable[[], bool] | None = None) -> int:
        """Scroll likely conversation containers upward a bounded number of times."""
        self.last_history_stop_reason = "SAFETY_CAP"
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is None:
            self.last_history_stop_reason = "NO_PROGRESS"
            return 0
        loaded_steps = 0
        no_progress_steps = 0
        previous_key = None
        deadline = time.monotonic() + max_seconds
        script = """
        () => {
          const assistants = () => [...document.querySelectorAll('[data-message-author-role="assistant"]')];
          const ids = () => assistants().map((node) => node.getAttribute('data-message-id')).filter(Boolean);
          const seen = new Set(); const candidates = [];
          for (const node of assistants()) {
            let el = node.parentElement;
            while (el) { if (!seen.has(el)) { seen.add(el); candidates.push(el); } el = el.parentElement; }
          }
          const main = document.querySelector('main');
          if (main && !seen.has(main)) candidates.push(main);
          for (const el of document.querySelectorAll('[class*="scroll-root"]')) if (!seen.has(el)) candidates.push(el);
          const measured = candidates.map((el) => ({
            el, tag: el.tagName.toLowerCase(), classSummary: typeof el.className === 'string' ? el.className.slice(0,180) : '',
            scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight
          })).filter((item) => item.scrollHeight - item.clientHeight >= Math.max(200, item.clientHeight * 0.25) && item.clientHeight >= 400);
          const score = (item) => [item.classSummary.includes('scroll-root') ? 1 : 0, item.scrollTop > 0 ? 1 : 0,
            Math.min(item.scrollHeight - item.clientHeight, 100000), item.clientHeight];
          measured.sort((a, b) => { const sa = score(a), sb = score(b); for (let i=0;i<sa.length;i++) if (sa[i] !== sb[i]) return sb[i]-sa[i]; return 0; });
          const selected = measured[0];
          if (!selected) return {found:false, idsBefore:ids()};
          const before = selected.el.scrollTop; const idsBefore = ids();
          selected.el.scrollTop = Math.max(0, before - Math.max(500, selected.el.clientHeight * 0.85));
          return {found:true, tag:selected.tag, classSummary:selected.classSummary, scrollTopBefore:before,
            scrollTopAfter:selected.el.scrollTop, scrollHeight:selected.scrollHeight, clientHeight:selected.clientHeight,
            idsBefore, idsAfter:ids()};
        }
        """
        for _ in range(max_steps):
            if time.monotonic() >= deadline:
                self.last_history_stop_reason = "SAFETY_CAP"
                break
            result = evaluate(script)
            loaded_steps += 1
            if isinstance(result, dict):
                if not result.get("found"):
                    self.last_history_stop_reason = "TOP_REACHED"
                    break
                before = result.get("scrollTopBefore")
                after = result.get("scrollTopAfter")
                if delay:
                    time.sleep(delay)
                ids_after = self._assistant_ids()
                if emit:
                    emit(f"HISTORY_SCROLL_CONTAINER overflow={result.get('scrollHeight', 0) - result.get('clientHeight', 0)} scrollTop={after}")
                    emit(f"HISTORY_SCROLL_STEP step={loaded_steps} before={before} after={after} assistants={len(result.get('idsBefore', []))}->{len(ids_after)}")
                key = (round(float(after), 3), tuple(ids_after))
                if key == previous_key:
                    no_progress_steps += 1
                else:
                    no_progress_steps = 0
                previous_key = key
                if stop_when and stop_when():
                    self.last_history_stop_reason = "FOUND"
                    break
                if after >= before:
                    self.last_history_stop_reason = "TOP_REACHED" if float(after or 0) <= 1 else "NO_PROGRESS"
                    break
                if float(after or 0) <= 1:
                    self.last_history_stop_reason = "TOP_REACHED"
                    break
                if no_progress_steps >= 2:
                    self.last_history_stop_reason = "NO_PROGRESS"
                    break
            elif not result:
                self.last_history_stop_reason = "TOP_REACHED"
                break
            else:
                if stop_when and stop_when():
                    self.last_history_stop_reason = "FOUND"
                    break
                if delay:
                    time.sleep(delay)
        else:
            self.last_history_stop_reason = "SAFETY_CAP"
        return loaded_steps

    def restore_live_bottom(self, delay: float = 0.5) -> dict[str, Any]:
        """Return the conversation viewport to the latest virtualized window."""
        result = self.page.evaluate("""
        () => {
          const assistants = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
          const seen = new Set(), candidates = [];
          for (const node of assistants) { let el=node.parentElement; while(el){ if(!seen.has(el)){seen.add(el);candidates.push(el)} el=el.parentElement; } }
          const main=document.querySelector('main'); if(main&&!seen.has(main)) candidates.push(main);
          for(const el of document.querySelectorAll('[class*="scroll-root"]')) if(!seen.has(el)) candidates.push(el);
          const eligible=candidates.map(el=>({el,tag:el.tagName.toLowerCase(),classSummary:typeof el.className==='string'?el.className.slice(0,180):'',scrollTop:el.scrollTop,scrollHeight:el.scrollHeight,clientHeight:el.clientHeight}))
            .filter(x=>x.scrollHeight-x.clientHeight>=Math.max(200,x.clientHeight*.25)&&x.clientHeight>=400);
          eligible.sort((a,b)=>{const sa=[a.classSummary.includes('scroll-root')?1:0,a.scrollTop>0?1:0,Math.min(a.scrollHeight-a.clientHeight,100000),a.clientHeight],sb=[b.classSummary.includes('scroll-root')?1:0,b.scrollTop>0?1:0,Math.min(b.scrollHeight-b.clientHeight,100000),b.clientHeight];for(let i=0;i<sa.length;i++)if(sa[i]!=sb[i])return sb[i]-sa[i];return 0});
          const selected=eligible[0]; if(!selected)return {found:false,before:null,after:null};
          const before=selected.el.scrollTop; selected.el.scrollTop=selected.el.scrollHeight-selected.el.clientHeight;
          return {found:true,before,after:selected.el.scrollTop};
        }
        """)
        if delay:
            time.sleep(delay)
        return result if isinstance(result, dict) else {"found": False, "before": None, "after": None}

    def user_baseline(self) -> dict[str, Any]:
        messages = self.page.locator('[data-message-author-role="user"]')
        count = messages.count()
        text = messages.nth(count - 1).inner_text() if count else ""
        return {"count": count, "text_hash": hashlib.sha256(text.encode()).hexdigest()}

    def user_message_texts(self) -> list[str]:
        """Read all user messages without including expandable UI controls."""
        self._historical_user_extraction_count += 1
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is not None:
            script = """
            () => [...document.querySelectorAll('[data-message-author-role="user"]')].map(node => {
              const clone = node.cloneNode(true);
              clone.querySelectorAll('button,[role="button"]').forEach(control => control.remove());
              return clone.innerText || clone.textContent || '';
            })
            """
            result = evaluate(script)
            if isinstance(result, list):
                return [item for item in result if isinstance(item, str)]
        messages = self.page.locator('[data-message-author-role="user"]')
        return [messages.nth(index).inner_text() for index in range(messages.count())]

    def exact_user_message_payload_observed(self, payload: str, history: bool = False) -> bool:
        target = normalize_prompt(payload)
        latest = self.latest_user_message()
        if isinstance(latest, str) and normalize_prompt(latest) == target:
            return True
        if not history:
            return False
        return any(normalize_prompt(text) == target for text in self.user_message_texts())

    def observe_exact_user_message(self, payload: str, timeout: float = FRESH_BOOTSTRAP_OBSERVATION_TIMEOUT_SECONDS, poll_interval: float = FRESH_BOOTSTRAP_OBSERVATION_POLL_SECONDS) -> bool:
        """Wait briefly for the submitted user message to mount on this page."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.exact_user_message_payload_observed(payload):
                    return True
            except Exception:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll_interval, remaining))

    def reconcile_unsent_submission(self, payload: str, timeout: float = 1.0) -> str:
        """Reconcile one ambiguous send on this page without allocating a tab."""
        try:
            already_submitted = self.exact_user_message_payload_observed(payload, history=True)
        except Exception:
            already_submitted = False
        if already_submitted:
            self.sendActionAcknowledged = True
            return "SENT"
        try:
            composer = self._live_composer()
            observed = composer.inner_text(timeout=1000)
        except Exception:
            return "AMBIGUOUS"
        if not isinstance(observed, str) or normalize_prompt(observed) != normalize_prompt(payload):
            return "AMBIGUOUS"
        try:
            composer.focus(timeout=1000)
            self.sendActionAttempted = True
            composer.press("Enter", timeout=1000)
            self.last_send_method = "playwright.composer.press(Enter)"
            self.alternateSendAttempted = True
            self.alternateSendMethod = "playwright.composer.press(Enter)"
        except Exception:
            self.alternateSendAttempted = True
            self.alternateSendMethod = "playwright.composer.press(Enter)"
            return "SEND_FAILED"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.exact_user_message_payload_observed(payload, history=True):
                    self.sendActionAcknowledged = True
                    return "SENT"
            except Exception:
                pass
            time.sleep(0.1)
        try:
            composer = self._live_composer()
            still_present = composer.inner_text(timeout=1000)
        except Exception:
            return "AMBIGUOUS"
        return "SEND_FAILED" if isinstance(still_present, str) and normalize_prompt(still_present) == normalize_prompt(payload) else "AMBIGUOUS"

    def control_user_message_count(self) -> int:
        return self.page.locator('[data-message-author-role="user"]').count()

    def control_user_messages(self, start: int = 0) -> list[dict[str, str | None]]:
        """Read only user-message identities/text for the remote pause monitor."""
        messages = self.page.locator('[data-message-author-role="user"]')
        result = []
        for index in range(start, messages.count()):
            message = messages.nth(index)
            text = message.inner_text()
            identity = message.get_attribute("data-message-id") or message.get_attribute("id")
            result.append({"id": identity, "text": text})
        return result

    def latest_user_message(self) -> str | None:
        messages = self.page.locator('[data-message-author-role="user"]')
        count = messages.count()
        return messages.nth(count - 1).inner_text() if count else None

    def submit_user_and_confirm(self, message: str, timeout: float = 15.0) -> bool:
        baseline = self.user_baseline()
        self.submit_result(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = self.page.locator('[data-message-author-role="user"]')
            count = messages.count()
            text = messages.nth(count - 1).inner_text() if count else ""
            if count > baseline["count"] or hashlib.sha256(text.encode()).hexdigest() != baseline["text_hash"]:
                return True
            time.sleep(0.25)
        return False

    def wait_for_new_completed_response(self, baseline: dict[str, Any], poll_interval: float = 0.5) -> str:
        observed = self.wait_for_new_response(baseline, poll_interval)
        if observed["state"] != "COMPLETED":
            raise TimeoutError("ARCHITECT_NEW_RESPONSE_NOT_READY")
        return observed["text"]

    def wait_for_new_response(self, baseline: dict[str, Any], poll_interval: float = 0.5) -> dict[str, Any]:
        self._trace_operation("wait_for_new_response", "BEGIN", baselineCount=baseline.get("count"), pollInterval=poll_interval)
        stable_hash = None
        stable_polls = 0
        poll_count = 0
        self._observation_recovery_attempted = False
        self._history_recovery_attempted = False
        stability_poll_pending = False
        while True:
            poll_count += 1
            wait_conversation_id = getattr(self, "runtime_conversation_id", "UNAVAILABLE_AT_LAYER")
            wait_watcher = getattr(self, "runtime_watcher", None)
            wait_state = getattr(wait_watcher, "state", {})
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_WAIT_POLL", wait_state,
                        conversationId=wait_conversation_id,
                        pollCount=poll_count, pollIntervalSeconds=poll_interval, generationVisible="NOT_SAMPLED_AT_LOG_POINT",
                        waitReason="NEW_RESPONSE", completionState=getattr(self, "last_state", "NOT_SAMPLED_AT_LOG_POINT"))
            if not stability_poll_pending and self.generation_visible():
                stable_hash = None
                stable_polls = 0
                self.last_state = "RUNNING"
                time.sleep(poll_interval)
                continue
            stability_poll_pending = False
            current = self.assistant_fast_snapshot()
            baseline_entries = baseline.get("entries", [])
            baseline_latest = baseline_entries[-1] if baseline_entries else {}
            baseline_latest_id = baseline.get("latestMessageId", baseline_latest.get("id"))
            baseline_latest_hash = baseline.get("latestTextHash")
            if baseline_latest_hash is None and baseline_latest:
                baseline_latest_hash = hashlib.sha256(str(baseline_latest.get("text") or "").encode()).hexdigest()
            identity_changed = (
                current.get("count", 0) != baseline.get("count", 0)
                or current.get("latestMessageId") != baseline_latest_id
                or current.get("latestTextHash") != baseline_latest_hash
            )
            text = ""
            if identity_changed:
                if current.get("_fixtureEntries"):
                    text = str(current.get("latestText") or "")
                else:
                    latest = self.latest_assistant_entry()
                    text = str((latest or {}).get("text") or "")
                if current.get("count", 0) != baseline.get("count", 0) and current.get("latestMessageId") == baseline_latest_id:
                    if not getattr(self, "_history_recovery_attempted", False):
                        self._history_recovery_attempted = True
                        self._observation_recovery_fallback_count += 1
                        runtime_log(
                            getattr(self, "runtime_logger", None),
                            getattr(self, "runtime_run_id", None),
                            "ARCHITECT_FULL_HISTORY_SCAN",
                            getattr(getattr(self, "runtime_watcher", None), "state", None),
                            reason="AMBIGUOUS_LATEST_IDENTITY",
                        )
                        entries = self._assistant_entries()
                        baseline_pairs = {(entry.get("id"), entry.get("text")) for entry in baseline_entries}
                        changed_entries = [entry for entry in entries if (entry.get("id"), entry.get("text")) not in baseline_pairs]
                        if changed_entries:
                            text = str(changed_entries[-1].get("text") or "")
                if not text and current.get("count", 0):
                    if not getattr(self, "_observation_recovery_attempted", False):
                        self._observation_recovery_attempted = True
                        self._observation_recovery_fallback_count += 1
                        runtime_log(
                            getattr(self, "runtime_logger", None),
                            getattr(self, "runtime_run_id", None),
                            "ARCHITECT_OBSERVATION_RECOVERY_FALLBACK",
                            getattr(getattr(self, "runtime_watcher", None), "state", None),
                            reason="VIRTUALIZATION_RECOVERY",
                            fullHistoryScan=False,
                        )
                        try:
                            self.restore_live_bottom(delay=0)
                        except Exception:
                            pass
                        time.sleep(poll_interval)
                        continue
                    self.last_state = "BLOCKED"
                    return {"state": "BLOCKED", "text": ""}
            if identity_changed and text.strip():
                if text.rstrip().endswith(COMPLETE) or architect_handover_ready(text):
                    self.last_state = "COMPLETED"
                    result = {"state": "COMPLETED", "text": text}
                    self._trace_operation("wait_for_new_response", "END", resultState=result["state"])
                    return result
                current_hash = hashlib.sha256(text.encode()).hexdigest()
                if current_hash == stable_hash:
                    stable_polls += 1
                else:
                    stable_hash = current_hash
                    stable_polls = 1
                if stable_polls < 2:
                    self.last_state = "RUNNING"
                    stability_poll_pending = True
                    time.sleep(poll_interval)
                    continue
                if text.rstrip().endswith(COMPLETE) or architect_handover_ready(text) or extract_executor_prompt_envelope(text) is not None:
                    self.last_state = "COMPLETED"
                    result = {"state": "COMPLETED", "text": text}
                    self._trace_operation("wait_for_new_response", "END", resultState=result["state"])
                    return result
                self.last_state = "BLOCKED"
                result = {"state": "BLOCKED", "text": text}
                self._trace_operation("wait_for_new_response", "END", resultState=result["state"])
                return result
            stable_hash = None
            stable_polls = 0
            if identity_changed:
                self.last_state = "BLOCKED"
                return {"state": "BLOCKED", "text": text}
            self.last_state = "NOT_YET"
            time.sleep(poll_interval)

    def submit_and_wait(self, message: str, poll_interval: float = 0.5) -> str:
        baseline = self.assistant_fast_snapshot()
        if not self.submit_user_and_confirm(message):
            raise RuntimeError("ARCHITECT_SUBMISSION_NOT_CONFIRMED")
        return self.wait_for_new_completed_response(baseline, poll_interval)

    def submit_result(self, result: str) -> None:
        composer = self.page.get_by_role("textbox").last
        composer.fill(result)
        composer.press("Enter")

    def _live_composer(self) -> Any:
        """Resolve the current editor, never reusing a locator across rerenders."""
        def actionable(candidate: Any, require_count: bool = False) -> bool:
            try:
                count = getattr(candidate, "count", None)
                if require_count and callable(count) and count() < 1:
                    return False
                return bool(getattr(candidate, "is_visible", lambda **_: True)(timeout=1000)) and bool(
                    getattr(candidate, "is_editable", lambda **_: True)(timeout=1000)
                )
            except Exception:
                return False

        # The semantic textbox is the authoritative current composer.  The
        # id selector is only a compatibility fallback and must itself pass
        # actionability checks; DOM presence alone is not sufficient.
        try:
            semantic = self.page.get_by_role("textbox").last
            if actionable(semantic):
                return semantic
        except Exception:
            pass
        locator = getattr(self.page, "locator", None)
        if locator is not None:
            try:
                fallback_locator = locator("#prompt-textarea")
                fallback = getattr(fallback_locator, "last", fallback_locator)
                if actionable(fallback, require_count=True):
                    return fallback
            except Exception:
                pass
        raise ResultSubmissionError("ARCHITECT_COMPOSER_UNAVAILABLE")

    def clear_unsent_payload(self, payload: str) -> bool:
        """Clear only an exactly matching pre-send draft after safe failure."""
        try:
            composer = self._live_composer()
            observed = composer.inner_text(timeout=1000)
            if not isinstance(observed, str) or normalize_prompt(observed) != normalize_prompt(payload):
                return False
            composer.focus(timeout=1000)
            composer.press("ControlOrMeta+A", timeout=1000)
            composer.press("Backspace", timeout=1000)
            cleared = composer.inner_text(timeout=1000)
            return isinstance(cleared, str) and not cleared.strip()
        except Exception:
            return False

    def submit_result_bounded(self, result: str, timeout: float = 30.0) -> None:
        self._trace_operation("submit_result_bounded", "BEGIN", payloadLength=len(result), payloadSha256=hashlib.sha256(result.encode()).hexdigest(), mutation=True)
        """Submit a result with explicit, bounded stages and typed failures."""
        self.last_send_method = None
        self.sendActionAttempted = False
        self.sendActionAcknowledged = False
        self.last_unsent_payload_cleared = False
        deadline = time.monotonic() + timeout
        last_error = None
        populated = False
        try:
            self.restore_live_bottom(delay=0)
        except Exception:
            pass
        while time.monotonic() < deadline:
            try:
                composer = self._live_composer()
                visible = getattr(composer, "is_visible", lambda **_: True)(timeout=1000)
                editable = getattr(composer, "is_editable", lambda **_: True)(timeout=1000)
                if visible and editable:
                    break
            except Exception as error:
                last_error = error
            time.sleep(0.25)
        else:
            detail = type(last_error).__name__ if last_error else None
            raise ResultSubmissionError("ARCHITECT_COMPOSER_UNAVAILABLE", detail) from last_error

        # ChatGPT's current rich editor can remain actionability-blocked for
        # locator.fill even when it is visible/editable.  Use the native
        # keyboard route after explicit focus; this updates the same editor
        # state as user typing/pasting and handles multiline Markdown.
        while time.monotonic() < deadline:
            try:
                # Re-resolve on every attempt because the rich editor can be
                # replaced while the fresh page hydrates.
                composer = self._live_composer()
                visible = getattr(composer, "is_visible", lambda **_: True)(timeout=1000)
                editable = getattr(composer, "is_editable", lambda **_: True)(timeout=1000)
                if not visible or not editable:
                    raise TimeoutError("ARCHITECT_COMPOSER_NOT_ACTIONABLE")
                composer.focus(timeout=1000)
                composer.press("ControlOrMeta+A", timeout=1000)
                keyboard = getattr(self.page, "keyboard", None)
                if keyboard is None:
                    raise RuntimeError("KEYBOARD_INPUT_UNAVAILABLE")
                if self.diagnostic_trace:
                    self.diagnostic_trace.record("PLAYWRIGHT", "submit_result_bounded", "PLAYWRIGHT_VISIBLE_MUTATION", "BEGIN", {}, reason="populate composer", callingFunction="submit_result_bounded", connectionId=self._diagnostic_connection_id, mutation=True, payloadLength=len(result), payloadSha256=hashlib.sha256(result.encode()).hexdigest())
                keyboard.insert_text(result)
                populated = True
                break
            except Exception as error:
                if type(error).__name__ == "TimeoutError":
                    last_error = error
                    # A timed-out input may have left a partial unsent draft.
                    # A fresh composer reference and select-all/backspace keep
                    # the next attempt replacement-safe without sending.
                    try:
                        retry_composer = self._live_composer()
                        retry_composer.focus(timeout=1000)
                        retry_composer.press("ControlOrMeta+A", timeout=1000)
                        retry_composer.press("Backspace", timeout=1000)
                    except Exception:
                        pass
                    if time.monotonic() < deadline:
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                else:
                    code = "ARCHITECT_COMPOSER_INPUT_REJECTED"
                    if populated and not self.sendActionAttempted:
                        self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
                    raise ResultSubmissionError(code, type(error).__name__) from error
        else:
            detail = type(last_error).__name__ if last_error else None
            raise ResultSubmissionError("ARCHITECT_COMPOSER_POPULATE_OPERATION_TIMEOUT", detail) from last_error

        try:
            composer = self._live_composer()
            observed = composer.inner_text(timeout=1000)
        except Exception as error:
            code = "ARCHITECT_COMPOSER_INPUT_ACCEPTANCE_TIMEOUT" if type(error).__name__ == "TimeoutError" else "ARCHITECT_COMPOSER_INPUT_REJECTED"
            if populated and not self.sendActionAttempted:
                self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
            raise ResultSubmissionError(code, type(error).__name__) from error
        if not isinstance(observed, str) or not observed.strip():
            if populated and not self.sendActionAttempted:
                self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
            raise ResultSubmissionError("ARCHITECT_COMPOSER_INPUT_REJECTED")
        if normalize_prompt(observed) != normalize_prompt(result):
            if populated and not self.sendActionAttempted:
                self.last_unsent_payload_cleared = self.clear_unsent_payload(result)
            raise ResultSubmissionError("ARCHITECT_COMPOSER_INPUT_REJECTED", "CONTENT_MISMATCH")
        assistant_count_before = self.assistant_count()

        try:
            send = self.page.get_by_role("button", name=re.compile(r"^\s*send(?:\s+prompt)?\s*$", re.I)).last
            send_visible = getattr(send, "is_visible", lambda **_: True)(timeout=1000)
        except Exception as error:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE", type(error).__name__) from error
        if not send_visible:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE")
        try:
            enabled = getattr(send, "is_enabled", lambda **_: True)(timeout=1000)
        except Exception as error:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_UNAVAILABLE", type(error).__name__) from error
        if not enabled:
            raise ResultSubmissionError("ARCHITECT_SEND_CONTROL_DISABLED")
        try:
            self.sendActionAttempted = True
            if self.diagnostic_trace:
                self.diagnostic_trace.record("PLAYWRIGHT", "submit_result_bounded", "PLAYWRIGHT_VISIBLE_MUTATION", "BEGIN", {}, reason="send Architect payload", callingFunction="submit_result_bounded", connectionId=self._diagnostic_connection_id, mutation=True)
            send.click(timeout=1000)
            self.last_send_method = "playwright.click"
            self.initialSendMethod = self.last_send_method
            self.initialSendActionReturned = True
        except Exception as error:
            if type(error).__name__ != "TimeoutError":
                raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", type(error).__name__) from error
            # A visible, enabled button can still be actionability-blocked by
            # transient layout.  Enter is a Playwright-only fallback on the
            # already confirmed active composer; transition confirmation below
            # remains the delivery authority.
            try:
                self.sendActionAttempted = True
                composer = self._live_composer()
                composer.focus(timeout=1000)
                if self.diagnostic_trace:
                    self.diagnostic_trace.record("PLAYWRIGHT", "submit_result_bounded", "PLAYWRIGHT_VISIBLE_MUTATION", "BEGIN", {}, reason="send fallback Enter", callingFunction="submit_result_bounded", connectionId=self._diagnostic_connection_id, mutation=True)
                composer.press("Enter", timeout=1000)
                self.last_send_method = "playwright.composer.press(Enter)"
                self.initialSendMethod = self.last_send_method
                self.initialSendActionReturned = True
            except Exception as fallback_error:
                raise ResultSubmissionError("ARCHITECT_SEND_ACTION_FAILED", type(fallback_error).__name__) from fallback_error

        # A bounded acknowledgement is the first observable post-send state:
        # the live composer no longer contains the submitted result.  Do not
        # interpret a timeout as a composer-discovery failure.
        ack_deadline = min(deadline, time.monotonic() + 5.0)
        while time.monotonic() < ack_deadline:
            try:
                composer_empty = not self._live_composer().inner_text(timeout=1000).strip()
                stop = self.page.get_by_role("button", name=re.compile(r"stop(?: generating)?", re.I)).last
                generation_visible = stop.count() > 0 and stop.is_visible(timeout=1000)
                assistant_started = self.assistant_count() > assistant_count_before
                if composer_empty or generation_visible or assistant_started:
                    self.sendActionAcknowledged = True
                    self._trace_operation("submit_result_bounded", "END", result="ACKNOWLEDGED", sendActionAttempted=self.sendActionAttempted, mutation=True)
                    return
            except Exception as error:
                last_error = error
            time.sleep(0.1)
        detail = type(last_error).__name__ if last_error else None
        self._trace_operation("submit_result_bounded", "ERROR", errorClass="TimeoutError", errorMessage="ARCHITECT_SUBMISSION_ACK_TIMEOUT", mutation=True)
        raise ResultSubmissionError("ARCHITECT_SUBMISSION_ACK_TIMEOUT", detail) from last_error

    def generation_visible(self) -> bool:
        started = time.monotonic()
        self._trace_operation("generation_visible", "BEGIN", mutation=False)
        evaluate = getattr(self.page, "evaluate", None)
        if evaluate is None:
            self._trace_operation("generation_visible", "END", started, result=False, mutation=False)
            return False
        try:
            result = bool(evaluate("""
            () => [...document.querySelectorAll('[data-testid="stop-button"]')]
              .filter((button) => button.isConnected && !button.disabled)
              .some((button) => {
                const style = getComputedStyle(button);
                const rect = button.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
              })
            """))
            self._trace_operation("generation_visible", "END", started, result=result, mutation=False)
            return result
        except Exception as error:
            self._trace_operation("generation_visible", "ERROR", started, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc(), mutation=False)
            return False

    def close(self) -> None:
        started = time.monotonic()
        self._trace_operation("PLAYWRIGHT_CLOSE", "BEGIN", reason="bridge_close")
        runtime = getattr(self, "_runtime", None)
        browser = getattr(self, "_browser", None)
        error = None
        if browser is not None:
            try:
                browser.close()
            except Exception as caught:
                error = caught
        if runtime is not None:
            try:
                runtime.stop()
            except Exception as caught:
                error = error or caught
        if self.diagnostic_trace:
            self.diagnostic_trace.close_connection(self._diagnostic_connection_id, "bridge_close", error=error, started=started)
        self._diagnostic_connection_id = None
        self._runtime = None
        self._browser = None
        self._trace_operation("PLAYWRIGHT_CLOSE", "ERROR" if error else "END", started, reason="bridge_close", errorClass=type(error).__name__ if error else None, errorMessage=str(error)[:500] if error else None, stackTrace=traceback.format_exc() if error else None)

    def open_fresh_and_wait_ready(self, handover: str) -> bool:
        new_page = self.page.context.new_page()
        new_page.goto("https://chatgpt.com/")
        new_page.get_by_role("textbox").last.fill(f"{handover}\nReply exactly {READY}")
        new_page.get_by_role("textbox").last.press("Enter")
        ready = READY in new_page.get_by_role("main").inner_text()
        if ready:
            self.page = new_page
        return ready

    def open_fresh_with_handover(self, handover: str) -> Any:
        """Create one fresh tab and submit the handover with fresh-session framing."""
        started = time.monotonic()
        if self.diagnostic_trace:
            self.diagnostic_trace.record("ROLLOVER", "open_fresh_with_handover", "FRESH_SESSION_CREATE_BEGIN", "BEGIN", {}, pagesBefore=len(getattr(getattr(self.page, "context", None), "pages", [])))
        new_page = self.page.context.new_page()
        if self.diagnostic_trace:
            self.diagnostic_trace.record("PLAYWRIGHT", "open_fresh_with_handover", "PLAYWRIGHT_VISIBLE_MUTATION", "END", {}, reason="fresh Architect page", mutation=True, callingFunction="open_fresh_with_handover", pageCountAfter=len(getattr(getattr(self.page, "context", None), "pages", [])))
        self._fresh_candidate_page = new_page
        self._fresh_candidate_submission_ambiguous = False
        bootstrap = fresh_architect_bootstrap_payload(handover)
        fresh_bridge = None
        try:
            new_page.goto("https://chatgpt.com/")
            if self.diagnostic_trace:
                self.diagnostic_trace.record("PLAYWRIGHT", "open_fresh_with_handover", "FRESH_SESSION_CREATE_END", "END", {}, durationMs=(time.monotonic() - started) * 1000, newPageUrl=getattr(new_page, "url", ""))
            fresh_bridge = ArchitectPlaywright(new_page)
            fresh_bridge.diagnostic_trace = self.diagnostic_trace
            try:
                fresh_bridge.submit_result_bounded(bootstrap)
            except ResultSubmissionError as submission_error:
                if submission_error.code != "ARCHITECT_SUBMISSION_ACK_TIMEOUT":
                    raise
                try:
                    reconciliation = fresh_bridge.reconcile_unsent_submission(bootstrap)
                except Exception:
                    reconciliation = "AMBIGUOUS"
                if reconciliation != "SENT":
                    self._fresh_candidate_submission_ambiguous = True
                    raise ResultSubmissionError(
                        "ARCHITECT_FRESH_BOOTSTRAP_SEND_FAILED" if reconciliation == "SEND_FAILED" else "ARCHITECT_FRESH_BOOTSTRAP_SEND_AMBIGUOUS"
                    ) from submission_error
            else:
                exact_submitted = fresh_bridge.observe_exact_user_message(bootstrap)
                if not exact_submitted:
                    try:
                        reconciliation = fresh_bridge.reconcile_unsent_submission(bootstrap)
                    except Exception:
                        reconciliation = "AMBIGUOUS"
                    if reconciliation != "SENT":
                        self._fresh_candidate_submission_ambiguous = True
                        raise ResultSubmissionError(
                            "ARCHITECT_FRESH_BOOTSTRAP_SEND_FAILED" if reconciliation == "SEND_FAILED" else "ARCHITECT_FRESH_BOOTSTRAP_SEND_AMBIGUOUS"
                        )
            if self.diagnostic_trace:
                self.diagnostic_trace.record("ROLLOVER", "open_fresh_with_handover", "FRESH_BOOTSTRAP_SEND_END", "END", {}, payloadLength=len(bootstrap), payloadSha256=hashlib.sha256(bootstrap.encode()).hexdigest())
        except Exception as error:
            if self.diagnostic_trace:
                self.diagnostic_trace.record("ROLLOVER", "open_fresh_with_handover", "FRESH_SESSION_CREATE_ERROR", "ERROR", {}, errorClass=type(error).__name__, errorMessage=str(error)[:500], stackTrace=traceback.format_exc())
            attempted = bool(getattr(fresh_bridge, "sendActionAttempted", False)) or bool(getattr(fresh_bridge, "last_send_method", None))
            ambiguous = attempted or (isinstance(error, ResultSubmissionError) and error.code in {
                "ARCHITECT_SUBMISSION_ACK_TIMEOUT",
                "ARCHITECT_FRESH_BOOTSTRAP_SEND_AMBIGUOUS",
                "ARCHITECT_FRESH_BOOTSTRAP_SEND_FAILED",
            })
            if ambiguous:
                self._fresh_candidate_submission_ambiguous = True
                raise
            try:
                new_page.close()
            except Exception:
                pass
            raise
        self._fresh_candidate_page = None
        return new_page

    @staticmethod
    def attach(endpoint: str, conversation_id: str | None = None) -> "ArchitectPlaywright":
        from playwright.sync_api import sync_playwright
        tracer = diagnostic_trace_for()
        attach_started = time.monotonic()
        runtime = sync_playwright().start()
        connection_id = tracer.connection_begin(endpoint, conversation_id) if tracer else None
        try:
            browser = runtime.chromium.connect_over_cdp(endpoint, timeout=10000)
        except Exception as error:
            if tracer:
                tracer.connection_end(connection_id, error=error, endpoint=endpoint, requested=conversation_id, started=attach_started)
            runtime.stop()
            raise RuntimeError("ARCHITECT_CDP_WEBSOCKET_ATTACHMENT_TIMEOUT") from error
        pages = [p for context in browser.contexts for p in context.pages]
        if conversation_id:
            candidates = []
            for page in pages:
                try:
                    actual_id = architect_conversation_id_from_url(page.url)
                except RuntimeError:
                    continue
                if architect_conversation_ids_equivalent(conversation_id, actual_id):
                    candidates.append((page, actual_id))
            distinct_ids = {actual_id for _page, actual_id in candidates}
            if len(distinct_ids) > 1:
                if tracer:
                    tracer.connection_end(connection_id, error=RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"), endpoint=endpoint, requested=conversation_id, started=attach_started)
                runtime.stop()
                raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
            exact = [page for page, actual_id in candidates if actual_id == conversation_id]
            if exact:
                pages = exact
            elif len(candidates) == 1:
                pages = [candidates[0][0]]
            elif candidates:
                if tracer:
                    tracer.connection_end(connection_id, error=RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"), endpoint=endpoint, requested=conversation_id, started=attach_started)
                runtime.stop()
                raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND")
            else:
                pages = []
        if not pages:
            if tracer:
                tracer.connection_end(connection_id, error=RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND" if conversation_id else "ARCHITECT_PAGE_NOT_FOUND"), endpoint=endpoint, requested=conversation_id, started=attach_started)
            runtime.stop()
            raise RuntimeError("ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND") if conversation_id else RuntimeError("ARCHITECT_PAGE_NOT_FOUND")
        bridge = ArchitectPlaywright(pages[-1])
        bridge.diagnostic_trace = tracer
        bridge._diagnostic_connection_id = connection_id
        bridge._runtime = runtime
        bridge._browser = browser
        if tracer:
            try:
                actual = architect_conversation_id_from_url(getattr(bridge.page, "url", "")) if conversation_id else None
            except Exception:
                actual = None
            tracer.attach_end(connection_id, endpoint, conversation_id, actual, attach_started)
        return bridge


class LocalWatcher:
    def __init__(self, project_dir: str, state_path: str | os.PathLike[str] = "orchestrator-state.json", runner: CodexRunner | None = None, durable_decision_reader: Callable[[str], dict[str, Any] | None] | None = None, durable_terminal_publisher: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None):
        self.project_dir = project_dir
        self.state_path = Path(state_path)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {"cycle_count": 0, "in_flight": False}
        if "architectResponseCount" not in self.state:
            self.state["architectResponseCount"] = 0
        self.loop_guard = LoopGuard(self.state)
        self.architect_memory_reader = None
        self.architect_memory_reader_thread_id = None
        self.architect_memory_session_id = None
        self.state.pop("architectMemoryBytes", None)
        self.state.pop("architectMemoryMiB", None)
        self.state.pop("architectMemorySessionId", None)
        self.state["memoryThresholdBytes"] = ARCHITECT_MEMORY_THRESHOLD_BYTES
        self.runner = runner or CodexRunner(project_dir, child_project_dir=AFFOTECH_CHILD_PROJECT_DIR, session_id=AFFOTECH_EXECUTOR_SESSION_ID)
        if durable_decision_reader is not None:
            self.durable_decision_reader = durable_decision_reader
        else:
            evidence_repo = self.state.get("evidenceRepository") or str(Path(project_dir) / ".agent-work" / "evidence-repo")
            self.durable_decision_reader = lambda publication_id: read_matching_architect_decision(evidence_repo, publication_id)
        evidence_repo = self.state.get("evidenceRepository") or str(Path(project_dir) / ".agent-work" / "evidence-repo")
        self.durable_terminal_publisher = durable_terminal_publisher or (lambda publication_id, result_text, envelope: publish_durable_executor_terminal(evidence_repo, publication_id, result_text, envelope))
        self.session_rollover = ArchitectSessionRollover(self)
        self.documentation_doorbell = DocumentationDoorbell(self)

    def bind_architect_session_memory(self, bridge: Any, conversation_id: str | None = None) -> bool:
        reader = getattr(bridge, "current_session_memory_bytes", None)
        if not callable(reader):
            return False
        self.architect_memory_reader = reader
        self.architect_memory_reader_thread_id = threading.get_ident()
        self.architect_memory_session_id = conversation_id
        self.state["architectMemoryOwnership"] = "SESSION_SCOPED"
        self.state["architectMemoryOwnershipSource"] = "ARCHITECT_PAGE_RENDERER"
        if conversation_id:
            self.state["architectMemorySessionId"] = conversation_id
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_MEMORY_READER_BIND", self.state,
                    conversationId=conversation_id, ownerThreadId=self.architect_memory_reader_thread_id,
                    ownerThreadName=threading.current_thread().name, source="ARCHITECT_PAGE_RENDERER")
        return True

    def startup_candidate(self, bridge: ArchitectPlaywright, scan_history: bool = True, emit: Callable[[str], None] | None = None) -> str | None:
        """Find a fresh completed prompt without requiring a new response."""
        found = bridge.latest_completed_executor_prompt()
        if found is None:
            latest_prompt = getattr(bridge, "latest_executor_prompt", None)
            generation_visible = getattr(bridge, "generation_visible", lambda: False)
            fallback = latest_prompt() if latest_prompt is not None else None
            if fallback is not None and not generation_visible():
                first_hash = hashlib.sha256(fallback[0].encode()).hexdigest()
                time.sleep(0.5)
                second = latest_prompt() if latest_prompt is not None else None
                if second is not None and hashlib.sha256(second[0].encode()).hexdigest() == first_hash and not generation_visible():
                    found = second
        if found is None and scan_history:
            steps = bridge.load_older_history(emit=emit, stop_when=lambda: self._fresh_prompt(bridge) is not None)
            found = bridge.latest_completed_executor_prompt()
            if found is None and emit:
                emit(f"STARTUP_HISTORY_EXHAUSTED reason={getattr(bridge, 'last_history_stop_reason', 'SAFETY_CAP')} steps={steps}")
        if found is None:
            return None
        _, prompt = found
        prompt_hash = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        if prompt_hash == self.state.get("last_prompt_hash") or prompt_hash == self.state.get("in_flight_prompt_hash"):
            return None
        return prompt

    def _fresh_prompt(self, bridge: ArchitectPlaywright) -> str | None:
        found = bridge.latest_completed_executor_prompt()
        if found is None:
            return None
        prompt = found[1]
        prompt_hash = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        if prompt_hash == self.state.get("last_prompt_hash") or prompt_hash == self.state.get("in_flight_prompt_hash"):
            return None
        return prompt

    def save(self) -> None:
        tracer = diagnostic_trace_for(self)
        before = dict(getattr(self, "_diagnostic_last_saved_state", {})) if tracer else {}
        self.state.update({"last_prompt_hash": self.loop_guard.last_prompt_hash, "last_result_hash": self.loop_guard.last_result_hash})
        self.state_path.write_text(json.dumps(self.state, indent=2) + "\n")
        if tracer:
            tracer.state_write(before, self.state)
            self._diagnostic_last_saved_state = dict(self.state)

    @staticmethod
    def process_alive(pid: Any) -> bool:
        """Return whether an independently launched executor is still alive."""
        if not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            ERROR_ACCESS_DENIED = 5
            ERROR_INVALID_PARAMETER = 87
            ERROR_NOT_FOUND = 1168
            STILL_ACTIVE = 259

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error in (ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND):
                    return False
                if error == ERROR_ACCESS_DENIED:
                    return True
                # Any other uncertain result fails safe.
                return True
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (OSError, ProcessLookupError):
            return False
        return True

    def _record_executor_start(self, pid: int, emit: Callable[[str], None]) -> None:
        # Persist before any result handling so a watcher restart cannot launch
        # a second child for the same immutable relay task.
        self.state["active_codex_pid"] = pid
        self.state["codex_pid"] = pid
        self.state["executor_state"] = "RUNNING"
        self.state["in_flight"] = True
        self.save()
        emit(f"CODEX_STARTED pid={pid}")

    def _clear_executor_start(self) -> None:
        self.state.pop("active_codex_pid", None)
        self.state.pop("codex_pid", None)
        self.state["executor_state"] = "EXITED"
        self.save()

    def _recoverable_result_exists(self) -> bool:
        result_path = self.state.get("result_file")
        if not isinstance(result_path, str) or not Path(result_path).is_file():
            return False
        envelope_path = self.state.get("result_envelope_file") or f"{result_path}.envelope.json"
        try:
            read_result_envelope(envelope_path)
            Path(result_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError):
            return False
        return True

    def _reconcile_inflight_executor(self, source: RelayPromptSource, bridge: ArchitectPlaywright | None, timeout: float, emit: Callable[[str], None]) -> str | None:
        """Reconcile an in-flight child without inferring failure from age."""
        pid = self.state.get("active_codex_pid") or self.state.get("codex_pid")
        if self.process_alive(pid):
            self.state["in_flight"] = True
            self.state["executor_state"] = "RUNNING"
            self.save()
            emit(f"CODEX_RUNNING pid={pid}")
            return "RUNNING"
        if self._recoverable_result_exists():
            self.state["result_pending"] = True
            self.state["executor_completed"] = True
            self.state["executor_state"] = "EXITED_RESULT_RECOVERABLE"
            self.save()
            # Re-enter the durable-result path; this never launches Codex.
            return self.run_relay_once(source, bridge, timeout, emit)
        if pid is not None:
            self.state["executor_state"] = "EXITED_WITHOUT_RECOVERABLE_RESULT"
            self.save()
        return None

    def retire_unrecoverable_relay(self, publication_id: str, content_sha256: str, reason: str = "SUPERSEDED_UNRECOVERABLE") -> bool:
        """Retire one proven-lost execution without manufacturing a result."""
        key = f"{publication_id}:{content_sha256}"
        if self.state.get("in_flight_relay_key") != key or self.state.get("result_pending"):
            return False
        retired = self.state.setdefault("retired_relay_keys", {})
        retired[key] = {"publicationId": publication_id, "contentSha256": content_sha256, "state": reason, "resultRecovered": False}
        self.state.pop("in_flight_relay_key", None)
        self.state["relay_recovery_state"] = reason
        self.save()
        return True

    def migrate_legacy_inflight_state(self, supersession_proof: dict[str, Any] | None, consumed_relay_keys: dict[str, dict[str, Any]] | None = None) -> bool:
        """Atomically retire a proven legacy in-flight record without calling it PASS."""
        key = self.state.get("in_flight_relay_key")
        publication_id = self.state.get("relay_publication_id")
        if not self.state.get("in_flight") or not isinstance(key, str) or not isinstance(publication_id, str):
            return False
        if self.state.get("result_pending") or self.state.get("executor_completed") or self.state.get("active_codex_pid") or self.state.get("codex_pid"):
            return False
        if not isinstance(supersession_proof, dict) or supersession_proof.get("currentPublicationId") == publication_id:
            return False
        backup = self.state_path.with_name(self.state_path.name + ".pre-legacy-migration.bak")
        if backup.exists():
            raise RuntimeError("LEGACY_STATE_BACKUP_ALREADY_EXISTS")
        original = self.state_path.read_bytes()
        backup.write_bytes(original)
        if backup.read_bytes() != original:
            raise RuntimeError("LEGACY_STATE_BACKUP_HASH_MISMATCH")
        retired = self.state.setdefault("retired_relay_keys", {})
        retired[key] = {"publicationId": publication_id, "resolution": "SUPERSEDED_WITHOUT_RETRY", "executionAuthorized": False, "historical": True, "resolvedFromLegacyState": True, "supersessionEvidence": supersession_proof}
        for consumed_key, evidence in (consumed_relay_keys or {}).items():
            retired.setdefault(consumed_key, {"resolution": "ALREADY_EXECUTED_AND_REVIEWED", "executionAuthorized": False, "historical": True, "supersessionEvidence": evidence})
        self.state.pop("in_flight_relay_key", None)
        self.state["in_flight"] = False
        self.state["relay_recovery_state"] = "SUPERSEDED_WITHOUT_RETRY"
        self.state["legacy_inflight_migration"] = {"publicationId": publication_id, "resolution": "SUPERSEDED_WITHOUT_RETRY", "executionAuthorized": False, "historical": True, "resolvedFromLegacyState": True, "supersessionEvidence": supersession_proof}
        self.save()
        try:
            json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.state_path.write_bytes(original)
            self.state = json.loads(original.decode("utf-8"))
            raise RuntimeError("LEGACY_STATE_ATOMIC_WRITE_FAILED")
        return True

    def _ensure_durable_terminal(self, relay_publication_id: str, result_text: str, envelope: dict[str, Any]) -> dict[str, Any]:
        existing = self.state.get("durable_terminal_publication_id")
        if self.state.get("durable_terminal_published") and isinstance(existing, str) and existing:
            return {"publicationId": existing, "resultSha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(), "alreadyPublished": True}
        published = self.durable_terminal_publisher(relay_publication_id, result_text, envelope)
        if not isinstance(published, dict) or not isinstance(published.get("publicationId"), str):
            raise RuntimeError("DURABLE_TERMINAL_PUBLICATION_INVALID")
        self.state["durable_terminal_publication_id"] = published["publicationId"]
        self.state["durable_terminal_result_sha256"] = published.get("resultSha256") or hashlib.sha256(result_text.encode("utf-8")).hexdigest()
        self.state["durable_terminal_published"] = True
        self.state["durable_terminal_readback"] = True
        self.save()
        return published

    def _retire_after_durable_terminal(self, relay_key: str) -> None:
        self.state["last_completed_relay_key"] = relay_key
        self.state.pop("in_flight_relay_key", None)
        self.state["relay_execution_retired"] = True
        self.save()

    def reconcile_durable_recovery(self, emit: Callable[[str], None] = print) -> bool:
        """Clear recovery only after an exact durable Architect decision match."""
        if not self.state.get("in_flight_relay_key"):
            return False
        terminal_publication = (self.state.get("recovery_terminal_publication_id")
                                or self.state.get("in_flight_terminal_publication_id")
                                or self.state.get("executor_terminal_publication_id"))
        if not isinstance(terminal_publication, str) or not terminal_publication:
            return False
        record = self.durable_decision_reader(terminal_publication)
        if not isinstance(record, dict):
            return False
        decision, pointer = record.get("decision"), record.get("acceptedPointer")
        if not isinstance(decision, dict) or not isinstance(pointer, dict):
            return False
        if decision.get("reviewedPublicationId") != terminal_publication:
            return False
        if decision.get("requiresArchitectDecision") is not False:
            return False
        if decision.get("decision") not in {None, "ACCEPTED"} and decision.get("classification") != "ACCEPTED":
            return False
        if pointer.get("accepted") is not True or pointer.get("publicationId") != terminal_publication:
            return False
        relay_key = self.state["in_flight_relay_key"]
        self.state["last_completed_relay_key"] = relay_key
        self.state.pop("in_flight_relay_key", None)
        self.state.pop("in_flight_prompt_hash", None)
        self.state["in_flight"] = False
        self.state["relay_recovery_state"] = "ARCHITECT_REVIEWED"
        self.state["recovery_reconciled"] = True
        self.state["recovery_reconciled_publication_id"] = terminal_publication
        self.save()
        emit(f"RECOVERY_RECONCILED publication={terminal_publication}")
        return True

    def run_relay_once(self, source: RelayPromptSource, bridge: ArchitectPlaywright | None, timeout: float = 300.0, emit: Callable[[str], None] = print) -> str:
        observation = source.read_current()
        publication_id = observation["publicationId"]
        relay_key = relay_task_key(publication_id, observation["contentSha256"])
        emit(f"LATEST_PROMPT publication={publication_id}")
        if self.state.get("rolloverPending") and not self.state.get("handoverRequested"):
            emit("STATE=ROLLOVER_PENDING")
            return "ROLLOVER_PENDING"
        if self.state.get("result_pending"):
            pending_key = self.state.get("in_flight_relay_key") or relay_key
            result_path = self.state.get("result_file")
            emit("RECOVERING_PENDING_RESULT")
            emit(f"TASK_PUBLICATION={self.state.get('relay_publication_id', publication_id)}")
            if not bridge or not result_path or not Path(result_path).exists():
                emit("STATE=RESULT_PENDING")
                return "RESULT_PENDING"
            result_text = None
            try:
                envelope_path = self.state.get("result_envelope_file") or f"{result_path}.envelope.json"
                envelope = read_result_envelope(envelope_path)
                result_text = Path(result_path).read_text(encoding="utf-8")
                self._ensure_durable_terminal(self.state.get("relay_publication_id", publication_id), result_text, envelope)
                self._retire_after_durable_terminal(pending_key)
                submission_key = result_submission_key(self.state.get("relay_publication_id", publication_id), result_text)
                if self.state.get("last_submitted_result_key") != submission_key:
                    submit = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
                    submit(result_text)
                    self.state["last_submitted_result_key"] = submission_key
            except Exception as error:
                emit("STATE=RESULT_PENDING")
                emit(f"RESULT_SUBMISSION_DEFERRED reason={type(error).__name__}:{error}")
                emit(f"RESULT_FILE={result_path}")
                return "RESULT_PENDING"
            self.state["last_completed_relay_key"] = pending_key
            self.state.pop("in_flight_relay_key", None)
            self.state["result_pending"] = False
            self.state["executor_completed"] = False
            self.save()
            emit("RESULT_SENT_TO_ARCHITECT")
            emit("STATE=IDLE")
            return "COMPLETED"
        if relay_key in self.state.get("retired_relay_keys", {}):
            emit("STATE=IDLE")
            return "IDLE"
        if self.state.get("in_flight_relay_key"):
            if self.state.get("in_flight_relay_key") in self.state.get("retired_relay_keys", {}):
                emit("STATE=IDLE")
                return "IDLE"
            if self.reconcile_durable_recovery(emit=emit):
                if self.state.get("last_completed_relay_key") == relay_key:
                    emit("STATE=IDLE")
                    return "IDLE"
            reconciled = self._reconcile_inflight_executor(source, bridge, timeout, emit)
            if reconciled is not None:
                return reconciled
            emit("STATE=RECOVERY_REQUIRED")
            return "RECOVERY_REQUIRED"
        if self.state.get("last_completed_relay_key") == relay_key:
            emit("STATE=IDLE")
            return "IDLE"
        self.state["in_flight_relay_key"] = relay_key
        self.state["relay_publication_id"] = publication_id
        self.state["relay_content_sha256"] = observation["contentSha256"]
        self.save()
        emit(f"RELAY_PROMPT_DETECTED publication={publication_id}")
        try:
            self._execution_publication_id = publication_id
            self._execution_dispatch_id = observation.get("dispatchId")
            self._execution_relay_key = relay_key
            authority = observation if observation.get("snapshotCommit") else None
            submitted = self._execute_prompt(bridge, observation["prompt"], timeout, emit, relay_authority=authority)
        except Exception:
            self.save()
            raise
        if not submitted:
            return "RESULT_PENDING"
        self.state["last_completed_relay_key"] = relay_key
        self.state.pop("in_flight_relay_key", None)
        self.save()
        emit("STATE=IDLE")
        return "COMPLETED"

    def run_relay_forever(self, source: RelayPromptSource, bridge: ArchitectPlaywright | None, poll_seconds: float = 7.0, response_timeout: float = 300.0, emit: Callable[[str], None] = print) -> None:
        emit("RELAY_CONNECTED")
        reported_error = None
        idle_reported = False
        def cycle_emit(line: str) -> None:
            nonlocal idle_reported
            if line == "STATE=IDLE":
                if idle_reported:
                    return
                idle_reported = True
            elif line.startswith("RELAY_PROMPT_DETECTED") or line.startswith("STATE="):
                idle_reported = False
            emit(line)
        while True:
            try:
                source.refresh_enabled = True
                result = self.run_relay_once(source, bridge, response_timeout, cycle_emit)
                reported_error = None
                if result == "RECOVERY_REQUIRED":
                    time.sleep(poll_seconds)
                else:
                    time.sleep(poll_seconds)
            except RelayAuthorityError as error:
                code = str(error)
                if code != reported_error:
                    emit(f"STATE=RELAY_UNAVAILABLE" if "GIT" in code else "STATE=AUTHORITY_INVALID")
                    reported_error = code
                time.sleep(poll_seconds)

    def forward(self, prompt: str, milestone: str | None = None, blocked: bool = False, timeout: float = 300.0) -> CodexResult | dict[str, str]:
        if self.loop_guard.check(prompt, milestone, blocked) == "LOOP_SUSPECTED":
            return {"state": "LOOP_SUSPECTED"}
        self.state["in_flight"] = True
        self.state["in_flight_prompt_hash"] = hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()
        self.state["cycle_count"] = int(self.state.get("cycle_count", 0)) + 1
        self.save()
        result = self.runner.run(prompt, timeout)
        self.state["in_flight"] = False
        self.state.pop("in_flight_prompt_hash", None)
        self.loop_guard.record_result(result.output)
        self.save()
        return result

    def handover_due(self) -> bool:
        return int(self.state.get("cycle_count", 0)) >= 30 and not self.state.get("in_flight", False)

    def accept_documentation_closure(self, response: str) -> bool:
        if not self.handover_due() or DOCUMENTATION_SYNC not in response or not response.rstrip().endswith(COMPLETE):
            return False
        self.state["documentation_sync_complete"] = True
        self.save()
        return True

    def rotate_after_ready(self, handover: str, ready: bool) -> bool:
        if not self.state.get("documentation_sync_complete", False) or not ready:
            self.state["pending_handover"] = handover
            self.save()
            return False
        self.state["cycle_count"] = 0
        self.state.pop("documentation_sync_complete", None)
        self.state.pop("pending_handover", None)
        self.save()
        return True

    def observe_response(self, bridge: ArchitectPlaywright, baseline: dict[str, Any], timeout: float = 120.0) -> tuple[dict[str, Any], dict[str, Any]]:
        observed = bridge.wait_for_new_response(baseline, timeout)
        if observed["state"] != "COMPLETED":
            return observed, baseline
        self.session_rollover.observe_complete_response(observed["text"])
        prompt = extract_executor_prompt(observed["text"]) or extract_executor_prompt_envelope(observed["text"])
        snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline")
        next_baseline = snapshot()
        if prompt is None:
            return observed, next_baseline
        return {**observed, "prompt": prompt}, next_baseline

    def run_forever(self, bridge: ArchitectPlaywright, sleep_seconds: float = 0.5, response_timeout: float = 120.0, emit: Callable[[str], None] = print) -> None:
        self.session_rollover.sample_memory(emit=emit)
        snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline")
        baseline = snapshot()
        emit("ARCHITECT_CONNECTED")
        emit(f"STARTUP_SCAN_MOUNTED count={bridge.assistant_count()}")
        startup_prompt = self.startup_candidate(bridge, scan_history=False)
        history_scanned = False
        if startup_prompt is None:
            emit("STARTUP_HISTORY_SCAN")
            history_scanned = True
            startup_prompt = self.startup_candidate(bridge, scan_history=True, emit=emit)
        if history_scanned:
            bottom = bridge.restore_live_bottom()
            emit(f"LIVE_BOTTOM_RESTORE before={bottom.get('before')} after={bottom.get('after')}")
            snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline")
            baseline = snapshot()
            emit(f"LIVE_BOTTOM_READY assistants={baseline.get('count', 0)}")
            if startup_prompt is None:
                startup_prompt = self.startup_candidate(bridge, scan_history=False)
        if startup_prompt is not None:
            self._execute_prompt(bridge, startup_prompt, response_timeout, emit)
            snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline")
            baseline = snapshot()
        else:
            emit("STATE=IDLE")
        while True:
            self.session_rollover.sample_memory(emit=emit)
            observed, next_baseline = self.observe_response(bridge, baseline, response_timeout)
            if observed["state"] == "NOT_YET":
                continue
            baseline = next_baseline
            if observed["state"] != "COMPLETED":
                emit("STATE=IDLE")
                time.sleep(sleep_seconds)
                continue
            emit("ARCHITECT_NEW_RESPONSE")
            prompt = observed.get("prompt")
            if prompt is None:
                emit("STATE=IDLE")
                time.sleep(sleep_seconds)
                continue
            self._execute_prompt(bridge, prompt, response_timeout, emit)
            emit("STATE=IDLE")
            time.sleep(sleep_seconds)

    def _execute_prompt(self, bridge: ArchitectPlaywright | None, prompt: str, timeout: float, emit: Callable[[str], None], publication_id: str | None = None, dispatch_id: str | None = None, relay_authority: dict[str, Any] | None = None) -> bool:
        emit("EXECUTOR_PROMPT_READY")
        if relay_authority is not None:
            self.runner.relay_authority = relay_authority
        if hasattr(self.runner, "on_start"):
            self.runner.on_start = lambda pid: self._record_executor_start(pid, emit)
        result = self.forward(prompt, timeout=timeout)
        self._clear_executor_start()
        if isinstance(result, dict):
            emit(f"STATE={result['state']}")
            return False
        if not isinstance(result.exit_code, int):
            emit("CODEX_FAILED exit=unknown reason=missing_exit_evidence")
            return False
        emit(f"CODEX_COMPLETED exit=0" if result.exit_code == 0 else f"CODEX_FAILED exit={result.exit_code}")
        self.state["executor_completed"] = True
        self.state["result_pending"] = True
        result_path = result.last_message_path
        if not result_path:
            result_dir = Path(self.project_dir) / ".agent-work" / "executor-results"
            result_dir.mkdir(parents=True, exist_ok=True)
            result_path = str(result_dir / f"result-{hashlib.sha256(result.output.encode()).hexdigest()}.txt")
            Path(result_path).write_text(result.output, encoding="utf-8")
        self.state["result_file"] = result_path
        result_publication_id = publication_id or getattr(self, "_execution_publication_id", None) or self.state.get("relay_publication_id")
        submission_key = result_submission_key(result_publication_id or "LOCAL", result.output)
        self.state["pending_result_submission_key"] = submission_key
        envelope_path = f"{result_path}.envelope.json"
        envelope_status = "PASS" if result.state == "COMPLETED" and result.exit_code == 0 else ("BLOCKED" if result.state == "BLOCKED" else "FAILED")
        envelope = build_result_envelope(dispatch_id or getattr(self, "_execution_dispatch_id", None) or self.state.get("in_flight_relay_key") or f"LOCAL-{hashlib.sha256(normalize_prompt(prompt).encode()).hexdigest()}", envelope_status, result_publication_id)
        Path(envelope_path).write_text(stable_json(envelope), encoding="utf-8")
        self.state["result_envelope_file"] = envelope_path
        self.save()
        try:
            self._ensure_durable_terminal(result_publication_id or "LOCAL", result.output, envelope)
            self._retire_after_durable_terminal(getattr(self, "_execution_relay_key", self.state.get("in_flight_relay_key")))
        except Exception as error:
            emit("STATE=RESULT_PENDING")
            emit(f"DURABLE_TERMINAL_PUBLICATION_DEFERRED reason={type(error).__name__}:{error}")
            emit(f"RESULT_FILE={result_path}")
            return False
        if bridge is None or not Path(result_path).exists():
            emit("STATE=RESULT_PENDING")
            reason = "ARCHITECT_BRIDGE_UNAVAILABLE" if bridge is None else "RESULT_FILE_UNAVAILABLE"
            emit(f"RESULT_SUBMISSION_DEFERRED reason={reason}")
            emit(f"RESULT_FILE={result_path}")
            return False
        try:
            read_result_envelope(envelope_path)
            submit = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
            submit(result.output)
        except Exception as error:
            emit("STATE=RESULT_PENDING")
            emit(f"RESULT_SUBMISSION_DEFERRED reason={type(error).__name__}:{error}")
            emit(f"RESULT_FILE={result_path}")
            return False
        self.state["last_submitted_result_key"] = submission_key
        self.state.pop("pending_result_submission_key", None)
        self.state["result_pending"] = False
        self.state["executor_completed"] = False
        self.save()
        emit("RESULT_SENT_TO_ARCHITECT")
        return True


ORCHESTRATOR_STATES = {"IDLE", "EXECUTOR_RUNNING", "RESULT_READY", "ARCHITECT_RUNNING", "NEXT_PROMPT_READY", "HUMAN_REQUIRED", "EXECUTOR_CRASHED"}
DOCUMENTATION_DISPOSITIONS = {"NOT_REQUIRED", "REQUIRED", "COMPLETE"}
EXECUTOR_WORKTREE_RE = re.compile(r"(?im)^[ \t]*WORKTREE[ \t]*\r?\n[ \t]*(?P<path>(?:[A-Za-z]:[\\/]|/)[^\r\n]+)[ \t]*(?=\r?$|\r?\n)")
ORCHESTRATOR_RESULT_RE = re.compile(
    r"<ORCHESTRATOR_RESULT>\s*"
    r"classification=(ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT)\s*"
    r"action=(EXECUTE|HUMAN_REQUIRED|STOP)\s*"
    r"taskId=([^\r\n]+)\s*"
    r"documentation=(NOT_REQUIRED|REQUIRED|COMPLETE)\s*"
    r"promptBegin\s*\r?\n?(.*?)\r?\npromptEnd\s*"
    r"</ORCHESTRATOR_RESULT>\s*$",
    re.S,
)
PROJECT_RE = re.compile(r"(?im)^\s*repository\s*=\s*(.+?)\s*$")
BRANCH_RE = re.compile(r"(?im)^\s*branch\s*=\s*(.+?)\s*$")
AUTHORITY_HEAD_RE = re.compile(r"(?im)^\s*currentBranchHeadAtArchitectDecision\s*=\s*([0-9a-f]{40})\s*$")


def atomic_write(path: str | os.PathLike[str], data: bytes) -> None:
    """Durably replace one local file without exposing a partial document."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.{os.getpid()}.{threading.get_ident()}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor != -1:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def parse_orchestrator_result(text: str, completed_task_id: str) -> dict[str, str]:
    candidate = text.rstrip()
    if candidate.endswith(COMPLETE):
        candidate = candidate[: -len(COMPLETE)].rstrip()
    if candidate.count("<ORCHESTRATOR_RESULT>") != 1 or candidate.count("</ORCHESTRATOR_RESULT>") != 1:
        raise ValueError("ARCHITECT_ENVELOPE_INVALID")
    match = ORCHESTRATOR_RESULT_RE.search(candidate)
    if not match or match.group(3).strip() != completed_task_id:
        raise ValueError("ARCHITECT_ENVELOPE_INVALID")
    prompt = match.group(5).replace("\r\n", "\n").replace("\r", "\n")
    action = match.group(2)
    if action == "EXECUTE" and not prompt.strip():
        raise ValueError("ARCHITECT_ENVELOPE_PROMPT_REQUIRED")
    if action != "EXECUTE" and prompt.strip():
        raise ValueError("ARCHITECT_ENVELOPE_PROMPT_FORBIDDEN")
    return {"classification": match.group(1), "action": action, "taskId": completed_task_id, "prompt": prompt, "documentation": match.group(4) or "NOT_REQUIRED"}


def repository_evidence(repo: str | os.PathLike[str]) -> dict[str, str]:
    def git(*args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8", errors="replace").strip()
        except (OSError, subprocess.CalledProcessError):
            return "UNAVAILABLE"
    status = git("status", "--porcelain")
    return {"head": git("rev-parse", "HEAD"), "statusPorcelain": status, "changedFileSummary": status}


def resolve_executor_worktree(prompt: str, fallback_project: str | os.PathLike[str] | None = None) -> str:
    """Resolve an explicit Executor WORKTREE and fail closed if it is invalid."""
    match = EXECUTOR_WORKTREE_RE.search(prompt)
    candidate = match.group("path").strip().strip("`") if match else (str(fallback_project) if fallback_project is not None else None)
    if not candidate:
        raise RuntimeError("EXECUTOR_WORKTREE_MISSING")
    path = Path(candidate)
    if not path.is_absolute():
        raise RuntimeError("EXECUTOR_WORKTREE_NOT_ABSOLUTE")
    if not path.is_dir():
        raise RuntimeError(f"EXECUTOR_WORKTREE_INVALID:{candidate}")
    return str(path)


class LocalFirstOrchestrator:
    """Small durable local control loop; GitHub is deliberately absent from it."""
    def __init__(self, project_dir: str, state_dir: str | os.PathLike[str] | None = None, process_factory: Callable[[str, Path], Any] | None = None):
        self.project_dir = Path(project_dir)
        self.state_dir = Path(state_dir or self.project_dir / ".agent-work" / "orchestrator")
        self._state_lock = threading.RLock()
        self.results_dir, self.prompts_dir, self.logs_dir = (self.state_dir / name for name in ("results", "prompts", "logs"))
        self.inbox_dir = self.state_dir / "inbox"
        self.state_path = self.state_dir / "state.json"
        self.process_factory = process_factory
        self._live_bottom_recovery_attempted: set[str] = set()
        self.state = self._load_state()
        self._operator_restart_rollover_recovery_available = self.state.get("rolloverMaintenanceState") == "DEFERRED"
        if (
            self.state.get("state") == "NEXT_PROMPT_READY"
            and self.state.get("rolloverDue") is True
            and self.state.get("rolloverPending") is True
            and self.state.get("rolloverMaintenanceState") == "DEFERRED"
            and self.state.get("nextTaskId")
            and "rolloverAutomaticRecoveryEpochCount" not in self.state
        ):
            # Migrate an older deferred record to a durable finite budget.  The
            # legacy operator-restart flag is retained for prepared UNSENT
            # compatibility, but it can no longer reset an ambiguous/exhausted
            # epoch once these fields exist.
            self.state.update({
                "rolloverRecoveryEpoch": 1,
                "rolloverAutomaticRecoveryEpochCount": 1,
                "rolloverAutomaticRecoveryMaxEpochs": ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS,
                # Legacy DEFERRED records have no durable cooldown to honor;
                # establish the first epoch immediately.  Subsequent failures
                # write an explicit cooldown and cannot be bypassed by restart.
                "rolloverAutomaticRecoveryNextEligibleAt": time.time(),
            })
            self.save()
        self.state.setdefault("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)
        self.state.setdefault("executorSessionMode", "PERSISTENT")
        self.architect_memory_reader = None
        self.architect_memory_reader_thread_id = None
        self.architect_memory_session_id = None
        self.memory_ownership_required = False
        if self.state.get("architectMemoryOwnership") != "SESSION_SCOPED":
            self.state["architectMemoryOwnership"] = "UNBOUND"
            self.state.pop("architectMemoryOwnershipSource", None)
        self.state.pop("architectMemoryBytes", None)
        self.state.pop("architectMemoryMiB", None)
        self.state.pop("architectMemorySessionId", None)
        self.state["memoryThresholdBytes"] = ARCHITECT_MEMORY_THRESHOLD_BYTES
        self.session_rollover = ArchitectSessionRollover(self)

    def bind_architect_session_memory(self, bridge: Any, conversation_id: str | None = None) -> bool:
        """Bind memory authority to the currently attached Architect page."""
        reader = getattr(bridge, "current_session_memory_bytes", None)
        if not callable(reader):
            return False
        actual_id = conversation_id
        if not actual_id:
            actual_id = architect_conversation_id_from_url(str(getattr(getattr(bridge, "page", None), "url", "")))
        self.architect_memory_reader = reader
        self.architect_memory_reader_thread_id = threading.get_ident()
        self.architect_memory_session_id = actual_id
        self.state["architectMemoryOwnership"] = "SESSION_SCOPED"
        self.state["architectMemoryOwnershipSource"] = "ARCHITECT_PAGE_RENDERER"
        self.state["architectMemorySessionId"] = actual_id
        self.state.pop("architectMemoryError", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_MEMORY_READER_BIND", self.state,
                    conversationId=actual_id, ownerThreadId=self.architect_memory_reader_thread_id,
                    ownerThreadName=threading.current_thread().name, source="ARCHITECT_PAGE_RENDERER")
        return True

    def bind_architect_memory_owner(self, endpoint: str, emit: Callable[[str], None] = print) -> int:
        """Reject the obsolete browser-tree ownership API."""
        del endpoint, emit
        raise RuntimeError("ARCHITECT_SESSION_MEMORY_REQUIRES_ATTACHED_PAGE")

    def _configured_fallback_project(self) -> Path | None:
        """Use a caller-provided project root, never this Orchestrator source root."""
        if self.project_dir.resolve() == Path(__file__).resolve().parent:
            return None
        return self.project_dir

    def _project_config_path(self) -> Path:
        return self.state_dir / "project-config.json"

    def _project_config(self) -> dict[str, Any]:
        path = self._project_config_path()
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("projects"), dict):
                raise RuntimeError("PROJECT_CONFIG_INVALID")
            migrated = False
            for spec in value["projects"].values():
                if isinstance(spec, dict) and "branch" in spec:
                    spec.setdefault("defaultBranch", spec["branch"])
                    spec.pop("branch", None)
                    migrated = True
            if migrated:
                atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            return value
        base = Path(AFFOTECH_CHILD_PROJECT_DIR)
        if not base.is_dir():
            raise RuntimeError("PROJECT_CONFIG_MISSING")
        branch = self._git(base, "branch", "--show-current")
        if not branch:
            raise RuntimeError("PROJECT_CONFIG_BRANCH_MISSING")
        repository = AFFOTECH_CHILD_REMOTE
        config = {"version": 1, "projects": {self._repository_key(repository): {
            "repository": repository,
            "baseRepo": str(base), "defaultBranch": branch, "remote": "origin"
        }}}
        atomic_write(path, (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        return config

    @staticmethod
    def _repository_key(value: str) -> str:
        key = re.split(r"[/\\]", value.rstrip("/\\"))[-1]
        return key[:-4] if key.lower().endswith(".git") else key

    def _project_spec(self, prompt: str, context_task_id: str | None = None) -> dict[str, Any] | None:
        project_match = PROJECT_RE.search(prompt)
        config = self._project_config()
        projects = config["projects"]
        if project_match:
            requested_repository = project_match.group(1).strip().strip("`")
            key = self._repository_key(requested_repository)
        else:
            record = self.state.get("taskWorktrees", {}).get(context_task_id or "")
            key = str(record.get("project", "")) if isinstance(record, dict) else ""
            if not key:
                if context_task_id:
                    raise RuntimeError("EXECUTOR_PROJECT_CONTEXT_MISSING")
                return None
        spec = projects.get(key)
        if not isinstance(spec, dict):
            if project_match:
                raise RuntimeError(f"PROJECT_CONFIG_UNKNOWN:{requested_repository}")
            raise RuntimeError("EXECUTOR_PROJECT_CONTEXT_MISSING")
        configured_repository = str(spec.get("repository", ""))
        if self._repository_key(configured_repository) != key:
            raise RuntimeError("PROJECT_CONFIG_REPOSITORY_MISMATCH")
        branch_match = BRANCH_RE.search(prompt)
        requested_branch = branch_match.group(1).strip() if branch_match else None
        record = self.state.get("taskWorktrees", {}).get(context_task_id or "")
        inherited_branch = record.get("branch") if isinstance(record, dict) else None
        return {**spec, "key": key, "repository": configured_repository, "branch": requested_branch or inherited_branch or spec.get("defaultBranch")}

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        try:
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, encoding="utf-8", errors="strict").strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"GIT_COMMAND_FAILED:{' '.join(args)}") from error

    def _owned_task_worktree(self, task_id: str, prompt: str, context_task_id: str | None = None) -> Path | None:
        spec = self._project_spec(prompt, context_task_id)
        if spec is None:
            return None
        base = Path(str(spec.get("baseRepo", ""))).resolve()
        if not base.is_dir() or self._repository_key(self._git(base, "config", "--get", f"remote.{spec.get('remote', 'origin')}.url")) != self._repository_key(str(spec["repository"])):
            raise RuntimeError("PROJECT_BASE_REPOSITORY_INVALID")
        remote = str(spec.get("remote", "origin"))
        branch = str(spec.get("branch") or "")
        if not branch:
            raise RuntimeError("EXECUTOR_BRANCH_MISSING")
        try:
            subprocess.run(["git", "-C", str(base), "fetch", "--quiet", remote, branch], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("EXECUTOR_SOURCE_FETCH_FAILED") from error
        remote_ref = f"refs/remotes/{remote}/{branch}"
        base_commit = self._git(base, "rev-parse", remote_ref)
        expected_head = AUTHORITY_HEAD_RE.search(prompt)
        if expected_head and base_commit.lower() != expected_head.group(1).lower():
            raise RuntimeError("EXECUTOR_SOURCE_AUTHORITY_ADVANCED")
        root = (self.state_dir / "worktrees").resolve()
        path = (root / task_id).resolve()
        ownership = self.state.setdefault("taskWorktrees", {})
        existing = ownership.get(task_id)
        if existing:
            if Path(str(existing.get("worktreePath", ""))).resolve() != path or existing.get("baseRepo") != str(base):
                raise RuntimeError("EXECUTOR_WORKTREE_OWNERSHIP_MISMATCH")
            if not path.is_dir() or Path(self._git(path, "rev-parse", "--show-toplevel")).resolve() != path:
                raise RuntimeError("EXECUTOR_WORKTREE_INVALID")
            if self._git(path, "rev-parse", "HEAD") != base_commit:
                raise RuntimeError("EXECUTOR_WORKTREE_SOURCE_MISMATCH")
            return path
        if path.exists():
            raise RuntimeError("EXECUTOR_WORKTREE_OWNERSHIP_CONFLICT")
        root.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "-C", str(base), "worktree", "add", "--detach", str(path), base_commit], check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError("EXECUTOR_WORKTREE_CREATE_FAILED") from error
        ownership[task_id] = {"taskId": task_id, "project": spec["key"], "baseRepo": str(base), "branch": branch, "baseCommit": base_commit, "worktreePath": str(path)}
        self.state.update({"taskWorktree": ownership[task_id], "targetProject": str(path), "targetRepo": str(path), "targetWorktree": str(path)})
        self.save()
        return path

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"state": "IDLE", "targetProject": str(self.project_dir), "targetRepo": str(self.project_dir)}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("ORCHESTRATOR_STATE_INVALID")
        value.setdefault("state", "IDLE")
        return value

    def save(self) -> None:
        with self._state_lock:
            tracer = diagnostic_trace_for(self)
            before = dict(getattr(self, "_diagnostic_last_saved_state", {})) if tracer else {}
            snapshot = dict(self.state)
            atomic_write(self.state_path, (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            if tracer:
                tracer.state_write(before, snapshot)
                self._diagnostic_last_saved_state = snapshot

    def discussion_pause_active(self) -> bool:
        marker = self.state_dir / "discussion-pause.marker"
        try:
            value = marker.read_text(encoding="ascii").strip()
            if value in {"PAUSED", "RESUMED"}:
                return value == "PAUSED"
        except OSError:
            pass
        return bool(self.state.get("discussionPauseActive"))

    def _write_discussion_pause_marker(self, active: bool) -> None:
        atomic_write(self.state_dir / "discussion-pause.marker", ("PAUSED\n" if active else "RESUMED\n").encode("ascii"))

    def _restore_machine_protocol_after_discussion(self) -> None:
        """Durably create one machine-protocol epoch for the current task."""
        with self._state_lock:
            staged_task_id = self._exact_staged_prompt_recovery_task()
            task_id = staged_task_id or str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
            needs_protocol = (
                bool(task_id)
                and self.state.get("state") == "HUMAN_REQUIRED"
                and self.state.get("humanRequiredReason") in {
                    "ARCHITECT_DECISION_HUMAN_REQUIRED",
                    "ARCHITECT_PROTOCOL_ENVELOPE_MISSING_AFTER_DISCUSSION",
                    "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED",
                }
            ) or (
                bool(task_id)
                and
                self.state.get("state") == "ARCHITECT_RUNNING"
                and self.state.get("postDiscussionEnvelopeRequired") is True
            ) or bool(staged_task_id and self.state.get("discussionPauseActive"))
            prior_state = dict(self.state)
            if not needs_protocol:
                changed = bool(self.state.get("postDiscussionEnvelopeRequired") or self.state.get("postDiscussionEnvelopeRepairAwaiting") or self.state.get("discussionPauseActive"))
                self.state.update({"postDiscussionEnvelopeRequired": False, "postDiscussionEnvelopeRepairAwaiting": False, "discussionPauseActive": False})
                if changed:
                    self.save()
                try:
                    self._write_discussion_pause_marker(False)
                except Exception:
                    self.state = prior_state
                    if changed:
                        self.save()
                    raise
                return
            pause_epoch = self.state.get("discussionPauseEpoch")
            same_resume_epoch = (
                self.state.get("postDiscussionEnvelopeRequired") is True
                and self.state.get("postDiscussionProtocolTaskId") == task_id
                and pause_epoch is not None
                and self.state.get("postDiscussionResumePauseEpoch") == pause_epoch
            )
            if not same_resume_epoch:
                epoch = int(self.state.get("postDiscussionResumeEpoch", 0) or 0) + 1
                self.state.update({
                    "postDiscussionEnvelopeRequired": True,
                    "postDiscussionResumeEpoch": epoch,
                    "postDiscussionEnvelopeRepairAttempted": False,
                    "postDiscussionEnvelopeRepairTaskId": task_id,
                    "postDiscussionEnvelopeRepairEpoch": epoch,
                    "postDiscussionEnvelopeRepairAwaiting": False,
                    "postDiscussionProtocolTaskId": task_id,
                    "postDiscussionProtocolTransactionId": self.state.get("rolloverTransactionId") if staged_task_id else None,
                    "postDiscussionProtocolBaseline": self.state.get("architectDiscussionBaseline") or self.state.get("architectBaseline"),
                    "postDiscussionResumePauseEpoch": pause_epoch,
                    "discussionPauseActive": False,
                })
            self.save()
            try:
                self._write_discussion_pause_marker(False)
            except Exception:
                self.state = prior_state
                try:
                    self.save()
                finally:
                    raise
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DISCUSSION_MACHINE_PROTOCOL_RESTORED", self.state, taskId=task_id, resumeEpoch=self.state.get("postDiscussionResumeEpoch"))
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_EXPECTED", self.state, taskId=task_id, resumeEpoch=self.state.get("postDiscussionResumeEpoch"))

    def request_discussion_pause(self) -> None:
        with self._state_lock:
            if self.discussion_pause_active():
                task_id = self.state.get("taskId") or self.state.get("nextTaskId") or "NONE"
                print(f"ORCHESTRATOR ALREADY PAUSED state={self.state.get('state')} taskId={task_id} F10=RESUME")
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_DISCUSSION_PAUSE_ALREADY_ACTIVE", self.state, taskId=task_id)
                return
            self._write_discussion_pause_marker(True)
            self.state["discussionPauseActive"] = True
            task_id = self.state.get("taskId") or self.state.get("nextTaskId") or "NONE"
            if task_id != "NONE":
                self.state["discussionPauseEpoch"] = int(self.state.get("discussionPauseEpoch", 0) or 0) + 1
            self.save()
            print(f"ORCHESTRATOR PAUSED BY HUMAN state={self.state.get('state')} taskId={task_id} F10=RESUME")
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_DISCUSSION_PAUSE_REQUESTED", self.state, taskId=task_id)

    def request_discussion_resume(self) -> None:
        with self._state_lock:
            if not self.discussion_pause_active():
                task_id = self.state.get("taskId") or self.state.get("nextTaskId") or "NONE"
                print(f"ORCHESTRATOR ALREADY RESUMED state={self.state.get('state')} taskId={task_id}")
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_DISCUSSION_RESUME_ALREADY_ACTIVE", self.state, taskId=task_id)
                return
            self._restore_machine_protocol_after_discussion()
            task_id = self.state.get("taskId") or self.state.get("nextTaskId") or "NONE"
            print(f"ORCHESTRATOR RESUMED BY HUMAN state={self.state.get('state')} taskId={task_id}")
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_DISCUSSION_RESUME_REQUESTED", self.state, taskId=task_id)

    def request_remote_discussion_pause(self) -> None:
        self.request_discussion_pause()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "REMOTE_DISCUSSION_PAUSE_REQUESTED", self.state)

    def request_remote_discussion_resume(self) -> None:
        self.request_discussion_resume()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "REMOTE_DISCUSSION_RESUME_REQUESTED", self.state)


    def _result_path(self, task_id: str) -> Path:
        return self.results_dir / f"{task_id}.txt"

    def _reset_format_recovery_for_task(self, task_id: str) -> None:
        if self.state.get("architectFormatRecoveryTaskId") != task_id:
            self.state.update({"formatRecoveryCount": 0, "formatRecoveryExhausted": False, "architectFormatRecoveryTaskId": None})

    def _record_executor_success(self, task_id: str, result: Path, exit_code: int | None = None) -> None:
        completed_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        self.state.update({
            "state": "RESULT_READY",
            "taskId": task_id,
            "lastCompletedTaskId": task_id,
            "executorResultPath": str(result),
            "executorExitCode": exit_code,
            "executorProcessState": "COMPLETED_WITH_RESULT",
            "executorFailureClass": None,
            "executorCrash": None,
            "humanRequiredReason": None,
            "repositoryEvidence": repository_evidence(self.project_dir),
            "formatRecoveryCount": 0,
            "formatRecoveryExhausted": False,
            "architectFormatRecoveryTaskId": None,
        })
        if completed_pid:
            self.state["lastExecutorPid"] = completed_pid
            self.state.pop("codexPid", None)
            self.state.pop("active_codex_pid", None)
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "STALE_EXECUTOR_PID_RETIRED", self.state, taskId=task_id, nextTaskId=self.state.get("nextTaskId"))

    def retire_completed_executor_ownership(self) -> bool:
        """Retire only PID fields proven to belong to a completed result."""
        task_id = str(self.state.get("taskId") or "")
        completed_task_id = str(self.state.get("lastCompletedTaskId") or "")
        if (self.state.get("executorProcessState") != "COMPLETED_WITH_RESULT"
                or not task_id or task_id != completed_task_id):
            return False
        completed_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if not completed_pid:
            return False
        self.state["lastExecutorPid"] = completed_pid
        self.state.pop("codexPid", None)
        self.state.pop("active_codex_pid", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "STALE_EXECUTOR_PID_RETIRED", self.state, taskId=task_id, nextTaskId=self.state.get("nextTaskId"))
        return True

    def intake_inbox(self, launcher: Callable[[str, Path], Any]) -> bool:
        """Consume and launch one approved local prompt while IDLE."""
        if self.state.get("state") != "IDLE" or self.discussion_pause_active():
            return False
        consumed = self.state.setdefault("consumedInboxItems", {})
        failures = self.state.setdefault("inboxFailures", {})
        for source in sorted(self.inbox_dir.glob("*.txt")):
            identity = source.stem
            if identity in consumed or identity in failures:
                continue
            try:
                prompt = source.read_text(encoding="utf-8")
                if not prompt.strip():
                    raise RuntimeError("INBOX_PROMPT_EMPTY")
                target = resolve_executor_worktree(prompt, self.project_dir)
                canonical = self.prompts_dir / f"{identity}.txt"
                if canonical.exists():
                    raise RuntimeError("INBOX_IDENTITY_ALREADY_CANONICAL")
                atomic_write(canonical, prompt.encode("utf-8"))
                consumed[identity] = {"source": str(source), "canonicalPrompt": str(canonical), "status": "LAUNCH_AUTHORIZED"}
                self.state.update({"state": "NEXT_PROMPT_READY", "taskId": identity, "nextTaskId": identity, "nextPromptPath": str(canonical), "targetProject": target, "targetRepo": target, "targetWorktree": target})
                self.save()
                return True
            except (OSError, UnicodeError, RuntimeError) as error:
                failures[identity] = {"source": str(source), "error": str(error)}
                self.state.update({"state": "HUMAN_REQUIRED", "inboxFailure": str(error)})
                self.save()
                return True
        return False

    def _architect_response_task_id(self, response: str) -> str | None:
        candidate = response.rstrip()
        if candidate.endswith(COMPLETE):
            candidate = candidate[: -len(COMPLETE)].rstrip()
        match = ORCHESTRATOR_RESULT_RE.search(candidate)
        return match.group(3).strip() if match else None

    def _architect_response_attempts_authority(self, response: str) -> bool:
        """Recognize only the canonical opening marker, not ordinary prose."""
        return "<ORCHESTRATOR_RESULT>" in response

    def _idle_invalid_envelope(self, response: str) -> bool:
        if not self._architect_response_attempts_authority(response):
            return False
        if not str(self.state.get("taskId") or ""):
            return False
        try:
            parse_orchestrator_result(response, str(self.state.get("taskId") or ""))
        except ValueError:
            return True
        return False

    def reject_invalid_handover_response(self) -> None:
        self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_HANDOVER_RESPONSE_INVALID"})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_HANDOVER_RESPONSE_INVALID", self.state)

    def defer_failed_rollover(self, reason: str = "ARCHITECT_HANDOVER_RESPONSE_INVALID") -> None:
        """Drop a failed maintenance attempt without changing workflow authority."""
        tracer = diagnostic_trace_for(self)
        if tracer:
            tracer.record("ROLLOVER", "defer_failed_rollover", "FUNCTION", "BEGIN", self.state, reason=reason)
        preserve_candidate_recovery = (
            self.state.get("rolloverFreshCandidateState") in {"SUBMISSION_AMBIGUOUS", "ACK_PENDING"}
            or self.state.get("rolloverFreshCandidateConversationId")
            or self.state.get("rolloverFreshBootstrapPayloadHash")
            or self.state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
        )
        self.state.update({
            "rolloverInProgress": False,
            "rolloverDue": True,
            "rolloverPending": True,
            "handoverRequested": False,
            "handoverReady": False,
            "rolloverMaintenanceState": "DEFERRED",
            "rolloverLastFailureReason": reason,
            "rolloverDeferredForTaskId": self.state.get("nextTaskId") or self.state.get("taskId"),
        })
        if not preserve_candidate_recovery:
            self.state.pop("pending_handover", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ROLLOVER_MAINTENANCE_FAILED", self.state, reason=reason)

    def process_pending_handover_response(self, bridge: Any, response: str) -> bool:
        """Keep pending handover responses out of ordinary task decision parsing."""
        tracer = diagnostic_trace_for(self)
        if tracer:
            tracer.record("ROLLOVER", "process_pending_handover_response", "FUNCTION", "BEGIN", self.state, responseLength=len(response), responseSha256=hashlib.sha256(response.encode()).hexdigest())
        if not self.state.get("handoverRequested"):
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HANDOVER_RECOVERY_DECISION", self.state,
                        existingResponseFound=True, transactionMatched=False, decision="BLOCK", reason="HANDOVER_NOT_REQUESTED")
            return False
        if self.session_rollover.complete_from_response(bridge, response):
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HANDOVER_RECOVERY_DECISION", self.state,
                        existingResponseFound=True, transactionMatched=True, decision="ACCEPT", reason="HANDOVER_COMMITTED")
            return True
        if self.state.get("rolloverInProgress") or (self.state.get("rolloverDue") and self.state.get("rolloverPending")):
            self.defer_failed_rollover()
            return False
        self.reject_invalid_handover_response()
        return False

    def recover_pending_rollover_handover(self, bridge: Any) -> bool:
        """Recover one already-generated handover after the known format-recovery incident."""
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        result_ready = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        eligible = (
            self.state.get("state") == "HUMAN_REQUIRED"
            and self.state.get("humanRequiredReason") == "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED"
            and self.state.get("handoverRequested") is True
            and self.state.get("rolloverPending") is True
            and task_id
            and task_id == str(self.state.get("lastCompletedTaskId") or "")
            and result_ready
            and not executor_active
        )
        if not eligible:
            return False
        entries = bridge._assistant_entries()
        handover = next((entry.get("text") for entry in reversed(entries) if isinstance(entry, dict) and isinstance(entry.get("text"), str) and architect_handover_ready(entry["text"])), None)
        if not handover:
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_HANDOVER_RESPONSE_INVALID", self.state, reason="ARCHITECT_HANDOVER_NOT_FOUND")
            return False
        if not self.session_rollover.complete_from_response(bridge, handover):
            return False
        self.state.update({"state": "RESULT_READY", "taskId": task_id, "lastCompletedTaskId": task_id, "executorResultPath": result_path, "humanRequiredReason": None})
        self.save()
        self.deliver_result(bridge)
        return True

    def recover_preempted_rollover_failure(self) -> bool:
        """Remove only maintenance contamination from an interrupted result recovery."""
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            result_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        try:
            executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        except (TypeError, ValueError):
            executor_active = True
        if (self.state.get("state") != "HUMAN_REQUIRED"
                or self.state.get("humanRequiredReason") not in {"ARCHITECT_HANDOVER_RESPONSE_INVALID", "ARCHITECT_NEW_CONVERSATION_ACK_INVALID", "ARCHITECT_NEW_CONVERSATION_ACK_TIMEOUT"}
                or not task_id or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not result_ready or not self.state.get("architectDeliveryPayloadHash")
                or self.state.get("architectSendState") not in {"PENDING", "FAILED", "AMBIGUOUS", "CONFIRMED"}
                or self.state.get("nextPromptPath")
                or (self.state.get("nextTaskId") and self.state.get("nextTaskId") not in {task_id, str(self.state.get("lastCompletedTaskId") or "")})
                or executor_active):
            return False
        confirmed = self.state.get("architectSendState") == "CONFIRMED"
        self.state.update({
            "state": "ARCHITECT_RUNNING" if confirmed else "HUMAN_REQUIRED",
            "humanRequiredReason": None if confirmed else "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED",
            "handoverRequested": False,
            "handoverReady": False,
            "rolloverInProgress": False,
            "rolloverDue": True,
            "rolloverPending": False,
        })
        self.state.pop("pending_handover", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ROLLOVER_MAINTENANCE_RECOVERY", self.state, taskId=task_id)
        return True

    def recover_completed_confirmed_workflow(self) -> bool:
        """Restore a completed result workflow from durable facts, independent of its old error label."""
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            result_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        try:
            executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        except (TypeError, ValueError):
            executor_active = True
        if (self.state.get("state") != "HUMAN_REQUIRED" or not task_id
                or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not result_ready or not self.state.get("architectDeliveryPayloadHash")
                or self.state.get("architectSendState") != "CONFIRMED"
                or self.state.get("nextPromptPath")
                or (self.state.get("nextTaskId") and self.state.get("nextTaskId") not in {task_id, str(self.state.get("lastCompletedTaskId") or "")})
                or executor_active):
            return False
        self.state.update({"state": "ARCHITECT_RUNNING", "humanRequiredReason": None,
                           "handoverRequested": False, "handoverReady": False,
                           "rolloverInProgress": False, "rolloverPending": False})
        self.state.pop("pending_handover", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "COMPLETED_CONFIRMED_WORKFLOW_RECOVERED", self.state, taskId=task_id)
        return True

    def recover_legacy_rollover_cutout(self) -> bool:
        """Recover only the obsolete maintenance-preemption state, once."""
        if self.state.get("legacyRolloverCutoutRecovered"):
            return False
        if (self.state.get("state") != "HUMAN_REQUIRED"
                or self.state.get("humanRequiredReason") != "ARCHITECT_ROLLOVER_SAFETY_CUTOUT"):
            return False
        task_id = str(self.state.get("taskId") or "")
        result_path = self.state.get("executorResultPath")
        prompt_path = self.state.get("nextPromptPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and bool(Path(result_path).read_text(encoding="utf-8", errors="replace").strip())
            prompt_ready = isinstance(prompt_path, str) and Path(prompt_path).is_file() and bool(Path(prompt_path).read_bytes())
        except OSError:
            result_ready = prompt_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        next_task_id = str(self.state.get("nextTaskId") or "")
        if (not task_id or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not next_task_id or next_task_id == task_id or not prompt_ready or not result_ready
                or executor_active or self.state.get("architectDecisionAuthorityInvalid")):
            return False
        try:
            response_count = int(self.state.get("architectResponseCount", 0) or 0)
        except (TypeError, ValueError):
            return False
        try:
            if next_task_id != self._canonical_next_task_id(task_id):
                return False
        except (TypeError, ValueError):
            return False
        memory_trigger = self.session_rollover.rollover_trigger(self.state.get("architectMemoryBytes"), response_count)
        self.state.update({
            "state": "NEXT_PROMPT_READY",
            "humanRequiredReason": None,
            "rolloverRecoveryState": "PENDING",
            "legacyRolloverCutoutRecovered": True,
            "rolloverDue": bool(memory_trigger),
            "rolloverPending": False,
            "rolloverInProgress": False,
            "handoverRequested": False,
            "handoverReady": False,
        })
        if memory_trigger:
            self.state["rolloverTrigger"] = memory_trigger
        else:
            self.state.pop("rolloverTrigger", None)
        for key in (
            "pending_handover", "rolloverAttemptedForTaskId", "rolloverTransactionId",
            "rolloverTransactionTaskId", "rolloverHandoverSendState",
            "rolloverHandoverResponseIdentity", "rolloverHandoverRecoveryDisposition",
            "rolloverHandoverRecoveryReason", "rolloverHandoverRecoveryRetryAfter",
            "rolloverFreshCandidateConversationId", "rolloverFreshCandidateState",
            "rolloverFreshCandidateDiscoveryState", "rolloverFreshCandidateAttemptCount",
            "rolloverFreshCandidateRetryAfter", "rolloverFreshBootstrapPayloadHash",
            "rolloverFreshPageCreated", "rolloverRecoveryStartedAt",
            "rolloverRecoveryAttemptCount", "rolloverRecoveryLastAttemptAt",
            "rolloverRecoveryRetryAfter", "rolloverRecoveryTerminalReason",
            "rolloverMaintenanceState", "rolloverLastFailureReason",
            "rolloverDeferredForTaskId", "rolloverAutoAttemptCount",
        ):
            self.state.pop(key, None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "LEGACY_ROLLOVER_CUTOUT_RECOVERED", self.state, taskId=task_id)
        return True

    def _fail_closed_idle_envelope(self, response: str, fingerprint: str | None) -> str:
        self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_ENVELOPE_INVALID"})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_INVALID", self.state, hash=fingerprint)
        print("IDLE_GATE=invalid_architect_envelope taskId=%s responseHash=%s correctionRequired=True" % (self.state.get("taskId"), fingerprint))
        return "HUMAN_REQUIRED"

    def _canonical_next_task_id(self, task_id: str | None = None) -> str:
        """Return the single next-task ID used by Architect EXECUTE staging."""
        raw_sequence = self.state.get("taskSequence", 0)
        if isinstance(raw_sequence, bool):
            raise ValueError("TASK_SEQUENCE_INVALID")
        current_sequence = int(raw_sequence)
        if current_sequence < 0:
            raise ValueError("TASK_SEQUENCE_INVALID")
        current_task = str(task_id or self.state.get("taskId") or "")
        if current_task.isdigit():
            current_sequence = max(current_sequence, int(current_task))
        return f"{current_sequence + 1:06d}"

    def consume_idle_architect_response(self, response: str, launcher: Callable[[str, Path], Any] | None = None) -> str:
        """Use the canonical Architect decision staging path after IDLE recovery."""
        return self.accept_architect_response(response)["action"]

    def _prelaunch_incomplete(self, task_id: str) -> bool:
        """Recognize an EXECUTE transaction that never reached a child or result."""
        if task_id == self.state.get("lastCompletedTaskId"):
            return False
        if self.state.get("executorLaunchState") in {"LAUNCHED", "POSTLAUNCH_NO_RESULT"} or self.state.get("prelaunchRecoveryState") == "RECOVERED_AND_LAUNCHED":
            return False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if active_pid:
            try:
                if LocalWatcher.process_alive(int(active_pid)):
                    return False
            except (TypeError, ValueError):
                return False
        result_path = self.state.get("executorResultPath")
        result_exists = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        return not result_exists

    def _validate_prelaunch_recovery(self, task_id: str, prompt: str) -> Path | None:
        """Require unchanged task inputs before any same-task automatic relaunch."""
        result_path = self.state.get("executorResultPath")
        if isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip():
            raise RuntimeError("RECOVERY_RESULT_ALREADY_EXISTS")
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if active_pid and LocalWatcher.process_alive(int(active_pid)):
            raise RuntimeError("RECOVERY_CHILD_STILL_ALIVE")
        record = self.state.get("taskWorktrees", {}).get(task_id)
        if not isinstance(record, dict):
            return None
        expected_head = str(record.get("baseCommit", ""))
        persisted_path = Path(str(record.get("worktreePath", ""))).resolve()
        if not persisted_path.is_dir():
            raise RuntimeError("RECOVERY_WORKTREE_MISSING")
        if self._git(persisted_path, "rev-parse", "HEAD").lower() != expected_head.lower():
            raise RuntimeError("RECOVERY_WORKTREE_HEAD_CHANGED")
        if self._git(persisted_path, "status", "--porcelain"):
            raise RuntimeError("RECOVERY_WORKTREE_DIRTY")
        try:
            owned = self._owned_task_worktree(task_id, prompt, context_task_id=task_id)
        except RuntimeError as error:
            if "AUTHORITY_ADVANCED" in str(error) or "SOURCE_MISMATCH" in str(error):
                raise RuntimeError("RECOVERY_SOURCE_ADVANCED") from error
            raise
        if owned is None:
            raise RuntimeError("PRELAUNCH_WORKTREE_NOT_OWNED")
        if self._git(owned, "rev-parse", "HEAD").lower() != expected_head.lower():
            raise RuntimeError("RECOVERY_WORKTREE_HEAD_CHANGED")
        if self._git(owned, "status", "--porcelain"):
            raise RuntimeError("RECOVERY_WORKTREE_DIRTY")
        return owned

    def recover_prelaunch_incomplete(self, launcher: Callable[[str, Path], Any]) -> Any | None:
        """Recover one safely verified EXECUTE that died before producing a result."""
        task_id = str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
        if not task_id or task_id == self.state.get("lastCompletedTaskId"):
            return None
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        if active_pid:
            if LocalWatcher.process_alive(int(active_pid)):
                self.state.update({"state": "HUMAN_REQUIRED", "prelaunchRecoveryError": "RECOVERY_CHILD_STILL_ALIVE", "automaticRetryAuthorized": False})
                self.save()
                return None
            self.state.update({"state": "HUMAN_REQUIRED", "executorLaunchState": "POSTLAUNCH_NO_RESULT", "automaticRetryAuthorized": False})
            self.save()
            print("EXECUTOR_RETRY_BLOCKED reason=POSTLAUNCH_NO_RESULT taskId=%s" % task_id)
            return None
        prompt_path = self.state.get("nextPromptPath")
        if not isinstance(prompt_path, str) or not Path(prompt_path).is_file():
            self.state["prelaunchRecoveryError"] = "PRELAUNCH_PROMPT_MISSING"
            self.save()
            return None
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
            owned = self._validate_prelaunch_recovery(task_id, prompt)
            if owned is None:
                raise RuntimeError("PRELAUNCH_WORKTREE_NOT_OWNED")
            self.state.update({"state": "NEXT_PROMPT_READY", "taskId": task_id, "nextTaskId": task_id, "targetProject": str(owned), "targetRepo": str(owned), "targetWorktree": str(owned), "prelaunchRecoveryState": "PRELAUNCH_INCOMPLETE"})
            self.save()
            self.state["prelaunchRecoveryState"] = "RECOVERABLE"
            self.save()
            return True
        except (OSError, UnicodeError, RuntimeError) as error:
            self.state.update({"state": "HUMAN_REQUIRED", "prelaunchRecoveryError": str(error)})
            self.save()
            return None

    def authorize_postlaunch_retry(self, launcher: Callable[[str, Path], Any]) -> Any | None:
        """Perform one explicit human-authorized retry of a post-launch failure."""
        task_id = str(self.state.get("taskId") or "")
        supplied = os.environ.get("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY")
        if self.state.get("state") != "HUMAN_REQUIRED" or self.state.get("executorLaunchState") != "POSTLAUNCH_NO_RESULT":
            return None
        if not supplied:
            return None
        if supplied != task_id:
            self.state["humanRecoveryAuthorizationError"] = "HUMAN_RECOVERY_TASK_MISMATCH"
            self.save()
            return None
        if (
            self.state.get("humanRecoveryAuthorizationConsumed")
            and self.state.get("humanRecoveryAuthorizedTaskId") == task_id
        ):
            self.state["humanRecoveryAuthorizationError"] = "HUMAN_RECOVERY_AUTHORIZATION_CONSUMED"
            self.save()
            return None
        prompt_path = self.state.get("nextPromptPath")
        if not isinstance(prompt_path, str) or not Path(prompt_path).is_file():
            self.state.update({"humanRecoveryAuthorizationError": "RECOVERY_PROMPT_MISSING", "automaticRetryAuthorized": False})
            self.save()
            return None
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8")
            owned = self._validate_prelaunch_recovery(task_id, prompt)
            if owned is None:
                raise RuntimeError("PRELAUNCH_WORKTREE_NOT_OWNED")
            verify_executor_session(str(self.state.get("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)))
            self.state.update({"humanRecoveryAuthorizationConsumed": True, "humanRecoveryAuthorizedTaskId": task_id, "automaticRetryAuthorized": False, "state": "NEXT_PROMPT_READY", "nextTaskId": task_id, "targetProject": str(owned), "targetRepo": str(owned), "targetWorktree": str(owned)})
            self.state.pop("humanRecoveryAuthorizationError", None)
            self.save()
            return True
        except (OSError, UnicodeError, RuntimeError) as error:
            self.state.update({"state": "HUMAN_REQUIRED", "automaticRetryAuthorized": False, "humanRecoveryAuthorizationError": str(error)})
            self.save()
            return None

    def request_architect_bootstrap(self, bridge: Any) -> bool:
        """Ask Architect once to evaluate current project state and choose the next action."""
        if self.discussion_pause_active():
            return False
        source_fingerprint = self.state.get("continuationSourceFingerprint")
        if source_fingerprint and source_fingerprint == self.state.get("lastContinuationSourceFingerprint"):
            self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False})
            self.save()
            print("IDLE_GATE=continuation_fingerprint_duplicate")
            return False
        message = "\n".join([
            "Review the current authoritative project state after the completed work.",
            "You are the project Architect.",
            "Decide the next bounded action according to the project's existing governance, roadmap, accepted milestones, documentation, and current authoritative state.",
            "Do not repeat already accepted work.",
            "Do not invent work outside the established project direction.",
            "If an Executor or Documentation Curator action is appropriate, return the complete next instruction using the canonical ORCHESTRATOR_RESULT envelope.",
            ARCHITECT_MACHINE_PROTOCOL_CONTRACT,
            "Set documentation=NOT_REQUIRED|REQUIRED|COMPLETE.",
            "NOT_REQUIRED means this decision does not require milestone/release documentation closure.",
            "REQUIRED means the accepted milestone/release needs one bounded documentation closure task before ordinary advancement.",
            "COMPLETE means required milestone documentation is already synchronized and verified.",
            "Do not choose NOT_REQUIRED merely to continue execution; use REQUIRED or COMPLETE when governance requires it.",
            "If genuine human authority is required, return HUMAN_REQUIRED.",
            "Return STOP only if there is genuinely no further currently authorized project work.",
            "<ORCHESTRATOR_RESULT>",
            "classification=ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT",
            "action=EXECUTE|HUMAN_REQUIRED|STOP",
            "taskId=<task id>",
            "documentation=NOT_REQUIRED|REQUIRED|COMPLETE",
            "promptBegin <complete Executor prompt only when action=EXECUTE>",
            "promptEnd",
            "</ORCHESTRATOR_RESULT>",
        ])
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            source_fingerprint = "UNSPECIFIED"
        self.state["architectBootstrapPayload"] = message
        self.state["architectBootstrapPayloadHash"] = hashlib.sha256(message.encode("utf-8")).hexdigest()
        self.state["architectBootstrapDeliveryState"] = "UNSENT"
        self.state.update({"lastContinuationSourceFingerprint": source_fingerprint, "architectBootstrapCount": int(self.state.get("architectBootstrapCount", 0)) + 1, "architectBootstrapAwaiting": True, "architectSendState": "PENDING", "state": "IDLE"})
        self.save()
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        try:
            sender(message)
        except Exception as error:
            attempted = bool(getattr(bridge, "sendActionAttempted", False)) or bool(getattr(bridge, "last_send_method", None))
            ambiguous = attempted or (isinstance(error, ResultSubmissionError) and error.code == "ARCHITECT_SUBMISSION_ACK_TIMEOUT")
            if isinstance(error, ResultSubmissionError) and not attempted:
                if bool(getattr(bridge, "last_unsent_payload_cleared", False)):
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_UNSENT_PAYLOAD_CLEARED", self.state, taskId=self.state.get("taskId"))
                self.state.pop("lastContinuationSourceFingerprint", None)
                self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False, "architectSendState": "FAILED", "architectBootstrapDeliveryState": "UNSENT"})
                self.save()
                print("IDLE_GATE=pre_send_failed")
                return False
            if not ambiguous:
                self.state.pop("lastContinuationSourceFingerprint", None)
                self.state["architectSendState"] = "FAILED"
            else:
                self.state["architectSendState"] = "AMBIGUOUS"
            self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False, "architectBootstrapDeliveryState": "AMBIGUOUS", "architectBootstrapRetryAfter": time.time() + 5.0})
            self.save()
            print("IDLE_GATE=send_failed")
            raise
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectBootstrapDeliveryState": "CONFIRMED", "architectContactCount": int(self.state.get("architectContactCount", 0)) + 1})
        self.save()
        if hasattr(bridge, "assistant_fast_snapshot"):
            self.state["architectBaseline"] = bridge.assistant_fast_snapshot()
            self.save()
        print("IDLE_GATE=continuation_sent")
        return True

    def _reconcile_architect_bootstrap(self, bridge: Any) -> str | None:
        """Reconcile an ambiguous IDLE continuation without replaying it blindly."""
        delivery_state = self.state.get("architectBootstrapDeliveryState")
        payload = self.state.get("architectBootstrapPayload")
        if delivery_state not in {"AMBIGUOUS", "PENDING"} or not isinstance(payload, str) or not payload:
            return None
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if self.state.get("architectBootstrapPayloadHash") != payload_hash:
            return "WAIT"
        observed = False
        exact = getattr(bridge, "exact_user_message_payload_observed", None)
        if callable(exact):
            try:
                observed = bool(exact(payload))
            except Exception:
                observed = False
        else:
            messages = getattr(bridge, "user_message_texts", None)
            if callable(messages):
                try:
                    observed = any(isinstance(text, str) and normalize_prompt(text) == normalize_prompt(payload) for text in messages())
                except Exception:
                    observed = False
        if observed:
            snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline", None)
            baseline = snapshot() if callable(snapshot) else None
            self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectBootstrapDeliveryState": "CONFIRMED", "architectBootstrapAwaiting": True, "architectBaseline": baseline})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_BOOTSTRAP_RECONCILED", self.state, hash=payload_hash)
            return "CONFIRMED"
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False})
            self.save()
            return "WAIT"
        retry_after = float(self.state.get("architectBootstrapRetryAfter") or 0.0)
        attempts = int(self.state.get("architectBootstrapObservationAttempts", 0))
        if time.time() < retry_after or attempts >= 1:
            self.state["architectBootstrapObservationAttempts"] = attempts + 1
            self.state.update({"state": "IDLE", "architectBootstrapAwaiting": False})
            self.save()
            return "WAIT"
        self.state.update({"architectBootstrapDeliveryState": "UNSENT", "architectBootstrapObservationAttempts": attempts + 1})
        self.state.pop("lastContinuationSourceFingerprint", None)
        self.save()
        return "RETRY"

    def inspect_idle_architect(self, bridge: Any, launcher: Callable[[str, Path], Any]) -> str:
        """Inspect the configured Architect conversation while IDLE."""
        bootstrap_disposition = self._reconcile_architect_bootstrap(bridge)
        if bootstrap_disposition == "CONFIRMED":
            return "ARCHITECT_RUNNING"
        if bootstrap_disposition == "WAIT":
            print("IDLE_GATE=bootstrap_reconciliation_wait")
            return "IDLE"
        generation_visible = bridge.generation_visible()
        if generation_visible:
            snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline", None)
            self.state.update({"state": "ARCHITECT_RUNNING", "architectBaseline": snapshot() if callable(snapshot) else None})
            self.save()
            print("IDLE_GATE=generation_visible")
            print("IDLE_DIAGNOSTIC latestResponseFound=False latestResponseId=None latestResponseFingerprint=None latestResponseHasEnvelope=False generationVisible=True architectBootstrapAwaiting=%s architectBootstrapCount=%s architectContactCount=%s architectSendState=%s architectResultFingerprint=%s continuationSourceFingerprint=%s lastContinuationSourceFingerprint=%s responseConsumed=False continuationEligible=False requestArchitectBootstrapCalled=False requestArchitectBootstrapSent=False resultingState=%s" % (self.state.get("architectBootstrapAwaiting"), self.state.get("architectBootstrapCount", 0), self.state.get("architectContactCount", 0), self.state.get("architectSendState"), self.state.get("architectResultFingerprint"), self.state.get("continuationSourceFingerprint"), self.state.get("lastContinuationSourceFingerprint"), self.state.get("state")))
            return "ARCHITECT_RUNNING"
        def latest_substantive(entries: list[dict[str, Any]]) -> tuple[str, str | None]:
            substantive = [
                entry for entry in entries
                if not str(entry.get("id") or "").startswith("request-placeholder-")
                and (entry.get("text") or "").strip().lower() != "thinking"
            ]
            if not substantive:
                return "", None
            return substantive[-1].get("text", ""), substantive[-1].get("id")

        response, response_id = latest_substantive(bridge._assistant_entries())
        response_fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest() if response else None
        response_has_envelope = bool(self._architect_response_task_id(response)) if response else False
        consumed = self.state.setdefault("consumedArchitectResponses", {})
        response_consumed = bool(response_fingerprint and response_fingerprint in consumed)
        if response and not response_consumed and self._idle_invalid_envelope(response):
            return self._fail_closed_idle_envelope(response, response_fingerprint)
        continuation_eligible = not response_has_envelope and not response_consumed and response_fingerprint != self.state.get("lastContinuationSourceFingerprint")
        if response:
            consumed_record = consumed.get(response_fingerprint, {}) if response_fingerprint else {}
            origin = consumed_record.get("origin") if isinstance(consumed_record, dict) else None
            if response_consumed:
                legacy_review = (
                    not origin
                    and str(self.state.get("taskId") or "") == str(self.state.get("lastCompletedTaskId") or "")
                    and str(self.state.get("architectDeliveryTaskId") or "") == str(self.state.get("taskId") or "")
                    and self.state.get("architectSendState") == "CONFIRMED"
                    and isinstance(self.state.get("executorResultPath"), str)
                    and Path(self.state["executorResultPath"]).is_file()
                    and Path(self.state["executorResultPath"]).read_text(encoding="utf-8", errors="replace").strip()
                    and self.state.get("state") == "IDLE"
                )
                if origin == "RESULT_REVIEW" or legacy_review:
                    if legacy_review:
                        consumed[response_fingerprint]["origin"] = "RESULT_REVIEW"
                        self.save()
                    if response_fingerprint != self.state.get("lastContinuationSourceFingerprint"):
                        self.state["continuationSourceFingerprint"] = response_fingerprint
                        self.save()
                        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONSUMED_RESPONSE", self.state, hash=response_fingerprint, origin="RESULT_REVIEW")
                        sent = self.request_architect_bootstrap(bridge)
                        if sent:
                            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONTINUATION_BOOTSTRAP_REQUESTED", self.state, hash=response_fingerprint, origin="RESULT_REVIEW")
                            return "ARCHITECT_RUNNING"
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONTINUATION_SUPPRESSED", self.state, hash=response_fingerprint, origin="RESULT_REVIEW", reason="already_requested")
                    return "IDLE"
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "IDLE_CONTINUATION_SUPPRESSED", self.state, hash=response_fingerprint, origin=origin or "BOOTSTRAP", reason="consumed")
                return "IDLE"
            for attempt in range(2):
                try:
                    result = self.consume_idle_architect_response(response, launcher)
                except ValueError:
                    break
                if result != "DUPLICATE":
                    print("IDLE_GATE=valid_envelope_processed")
                    print("IDLE_DIAGNOSTIC latestResponseFound=True latestResponseId=%s latestResponseFingerprint=%s latestResponseHasEnvelope=True generationVisible=False architectBootstrapAwaiting=%s architectBootstrapCount=%s architectContactCount=%s architectSendState=%s architectResultFingerprint=%s continuationSourceFingerprint=%s lastContinuationSourceFingerprint=%s responseConsumed=%s continuationEligible=False requestArchitectBootstrapCalled=False requestArchitectBootstrapSent=False resultingState=%s" % (response_id, response_fingerprint, self.state.get("architectBootstrapAwaiting"), self.state.get("architectBootstrapCount", 0), self.state.get("architectContactCount", 0), self.state.get("architectSendState"), self.state.get("architectResultFingerprint"), self.state.get("continuationSourceFingerprint"), self.state.get("lastContinuationSourceFingerprint"), response_consumed, self.state.get("state")))
                    return result
                baseline_entries = self.state.get("architectBaseline", {}).get("entries", [])
                baseline_response, baseline_id = latest_substantive(baseline_entries if isinstance(baseline_entries, list) else [])
                baseline_fingerprint = hashlib.sha256(baseline_response.encode("utf-8")).hexdigest() if baseline_response else None
                baseline_is_new = bool(baseline_fingerprint and baseline_fingerprint != response_fingerprint and baseline_fingerprint not in consumed and baseline_fingerprint != self.state.get("lastContinuationSourceFingerprint"))
                if baseline_is_new:
                    self.state.pop("architectLiveBottomFingerprint", None)
                    self.state["continuationSourceFingerprint"] = baseline_fingerprint
                    self.save()
                    print("IDLE_RECOVERY_SOURCE=persisted_baseline")
                    sent = self.request_architect_bootstrap(bridge)
                    if sent:
                        return "ARCHITECT_RUNNING"
                    return self.state.get("state", "IDLE")
                if attempt or response_fingerprint in self._live_bottom_recovery_attempted:
                    print("IDLE_GATE=response_already_consumed")
                    return result
                self._live_bottom_recovery_attempted.add(response_fingerprint)
                self.state.pop("architectLiveBottomFingerprint", None)
                self.save()
                try:
                    print("IDLE_RECOVERY_SOURCE=live_bottom_restore")
                    bridge.restore_live_bottom()
                    response, response_id = latest_substantive(bridge._assistant_entries())
                except Exception:
                    self.save()
                    print("IDLE_GATE=live_bottom_restore_failed")
                    return "DUPLICATE"
                response_fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest() if response else None
                response_has_envelope = bool(self._architect_response_task_id(response)) if response else False
                response_consumed = bool(response_fingerprint and response_fingerprint in consumed)
                if response and not response_consumed and self._idle_invalid_envelope(response):
                    return self._fail_closed_idle_envelope(response, response_fingerprint)
                continuation_eligible = not response_has_envelope and not response_consumed and response_fingerprint != self.state.get("lastContinuationSourceFingerprint")
        self.state["continuationSourceFingerprint"] = hashlib.sha256(response.encode("utf-8")).hexdigest()
        sent = self.request_architect_bootstrap(bridge)
        print("IDLE_DIAGNOSTIC latestResponseFound=%s latestResponseId=%s latestResponseFingerprint=%s latestResponseHasEnvelope=%s generationVisible=False architectBootstrapAwaiting=%s architectBootstrapCount=%s architectContactCount=%s architectSendState=%s architectResultFingerprint=%s continuationSourceFingerprint=%s lastContinuationSourceFingerprint=%s responseConsumed=%s continuationEligible=%s requestArchitectBootstrapCalled=True requestArchitectBootstrapSent=%s resultingState=%s" % (bool(response), response_id, response_fingerprint, response_has_envelope, self.state.get("architectBootstrapAwaiting"), self.state.get("architectBootstrapCount", 0), self.state.get("architectContactCount", 0), self.state.get("architectSendState"), self.state.get("architectResultFingerprint"), self.state.get("continuationSourceFingerprint"), self.state.get("lastContinuationSourceFingerprint"), response_consumed, continuation_eligible, sent, self.state.get("state")))
        if sent:
            return "ARCHITECT_RUNNING"
        print("IDLE_GATE=send_suppressed")
        return self.state.get("state", "IDLE")

    def recover_5c(self, legacy_path: str | os.PathLike[str] = r"C:\Users\nitro\AppData\Local\Temp\codex-last-message-h_2bryhl.txt") -> bool:
        source = Path(legacy_path)
        if not source.is_file() or not source.read_text(encoding="utf-8", errors="replace").strip():
            self.state.update({"state": "HUMAN_REQUIRED", "current5CRecoverableResult": False})
            self.save()
            return False
        destination = self._result_path("PUB-aa3b4121887c4047b3c056bcccaa6a96")
        atomic_write(destination, source.read_bytes())
        self.state.update({"state": "RESULT_READY", "taskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96", "executorResultPath": str(destination), "current5CRecoverableResult": True, "current5CRecoveredResultPath": str(destination), "lastCompletedTaskId": "PUB-aa3b4121887c4047b3c056bcccaa6a96"})
        self.save()
        return True

    def _capture_result(self, task_id: str, source: Path) -> Path:
        destination = self._result_path(task_id)
        if source.resolve() != destination.resolve():
            atomic_write(destination, source.read_bytes())
        if not destination.is_file() or not destination.read_text(encoding="utf-8", errors="replace").strip():
            raise RuntimeError("EXECUTOR_RESULT_MISSING")
        return destination

    def reconcile_executor(self) -> str:
        if self.state.get("state") != "EXECUTOR_RUNNING":
            return self.state.get("state", "IDLE")
        pending_prompt = self.state.get("nextPromptPath")
        if isinstance(pending_prompt, str) and Path(pending_prompt).is_file():
            task_id = str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
            owned = self.state.get("taskWorktrees", {}).get(task_id)
            target = str(owned["worktreePath"]) if isinstance(owned, dict) and owned.get("worktreePath") else resolve_executor_worktree(Path(pending_prompt).read_text(encoding="utf-8"), self._configured_fallback_project())
            self.state.update({"targetProject": target, "targetRepo": target, "targetWorktree": target})
        pid = self.state.get("codexPid")
        if LocalWatcher.process_alive(pid):
            return "EXECUTOR_RUNNING"
        path = self.state.get("executorResultPath")
        if isinstance(path, str) and Path(path).is_file() and Path(path).read_text(encoding="utf-8", errors="replace").strip():
            task_id = str(self.state.get("taskId") or self.state.get("nextTaskId") or "")
            self._record_executor_success(task_id, Path(path), self.state.get("executorExitCode"))
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_RESULT_FOUND", self.state, pid=self.state.get("lastExecutorPid") or pid, resultPath=path)
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_READY", self.state)
        else:
            result_exists = bool(isinstance(path, str) and Path(path).is_file())
            process = getattr(self, "_active_process", None)
            exit_code = None
            if process is not None:
                try:
                    exit_code = process.poll()
                except (OSError, AttributeError):
                    pass
            stderr_path = self.state.get("stderrLogPath")
            stderr_summary = ""
            if isinstance(stderr_path, str) and Path(stderr_path).is_file():
                stderr_summary = Path(stderr_path).read_text(encoding="utf-8", errors="replace").strip()[:500]
            failure_class = "EXECUTOR_SESSION_ACTIVE_WRITER" if "already has an active writer" in stderr_summary else "POSTLAUNCH_NO_RESULT"
            self.state.update({
                "state": "EXECUTOR_CRASHED",
                "executorLaunchState": "POSTLAUNCH_NO_RESULT",
                "automaticRetryAuthorized": False,
                "executorProcessState": "EXITED_WITHOUT_RESULT",
                "executorFailureClass": failure_class,
                "executorExitCode": exit_code,
                "stderrSummary": stderr_summary,
                "executorCrash": {
                    "taskId": self.state.get("taskId"),
                    "pid": pid,
                    "targetWorktree": self.state.get("targetWorktree"),
                    "resultPath": path,
                    "resultExists": result_exists,
                    "exitCode": exit_code,
                    "stderrLogPath": stderr_path,
                    "stderrSummary": stderr_summary,
                },
            })
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_RESULT_MISSING", self.state, resultPath=path, pid=pid)
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_CRASHED", self.state, pid=pid, exitCode=exit_code)
            print("EXECUTOR_PROCESS_STATE=EXITED_WITHOUT_RESULT taskId=%s pid=%s exitCode=%s stderrLogPath=%s stderrSummary=%s" % (self.state.get("taskId"), pid, exit_code, stderr_path, stderr_summary))
        self.save()
        return self.state["state"]

    def wait_for_executor(self, poll_interval: float = 2.0) -> str:
        """Remain resident while the independently launched Executor runs."""
        while self.state.get("state") == "EXECUTOR_RUNNING":
            state = self.reconcile_executor()
            if state == "EXECUTOR_RUNNING":
                time.sleep(poll_interval)
            else:
                return state
        return self.state.get("state", "IDLE")

    def wait_for_idle(self, poll_interval: float = 2.0) -> str:
        """Remain resident in normal IDLE until local state requests work."""
        while self.state.get("state") == "IDLE":
            time.sleep(poll_interval)
            self.state = self._load_state()
        return self.state.get("state", "IDLE")

    def mark_executor_started(self, task_id: str, pid: int, result_path: str | os.PathLike[str]) -> None:
        target = self.state.get("targetWorktree") or self.state.get("targetProject")
        attempt = int(self.state.get("executorAttemptNumber", 0)) + 1
        log_path = self.state.get("stderrLogPath") or str(self.state_dir / "executor-logs" / f"{task_id}-attempt-{attempt}.stderr.txt")
        self.state.update({"state": "EXECUTOR_RUNNING", "executorLaunchState": "LAUNCHED", "automaticRetryAuthorized": False, "taskId": task_id, "taskSequence": int(self.state.get("taskSequence", 0)) + 1, "executorAttemptNumber": attempt, "codexPid": pid, "codexStartedAt": time.time(), "targetProject": target, "executorResultPath": str(result_path), "stderrLogPath": str(log_path), "executorFailureClass": None, "executorProcessState": None, "executorCrash": None, "stderrSummary": "", "executorExitCode": None})
        self._active_process = getattr(self, "_active_process", None)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "CODEX_STARTED", self.state, pid=pid, attempt=attempt)

    def mark_executor_exit(self, exit_code: int, result_path: str | os.PathLike[str]) -> str:
        task_id = str(self.state.get("taskId", "unknown"))
        try:
            result = self._capture_result(task_id, Path(result_path))
        except (OSError, RuntimeError):
            self.state["state"] = "EXECUTOR_CRASHED"
            self.save()
            return "EXECUTOR_CRASHED"
        self._record_executor_success(task_id, result, exit_code)
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "EXECUTOR_RESULT_FOUND", self.state, pid=self.state.get("lastExecutorPid"), exitCode=exit_code, resultPath=result_path)
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_READY", self.state)
        return "RESULT_READY"

    def _result_delivery_payload(self) -> tuple[str, str]:
        path = Path(self.state["executorResultPath"])
        report = path.read_text(encoding="utf-8")
        task_id = str(self.state["taskId"])
        instruction = ("Verify the completed Executor report below, classify it, decide the next bounded action, "
                       "and finish with exactly one <ORCHESTRATOR_RESULT> envelope using taskId=" + task_id + ".\n"
            "The envelope must end the response; action=EXECUTE requires the complete next Executor prompt.\n\n")
        instruction += ARCHITECT_MACHINE_PROTOCOL_CONTRACT + "\n\n"
        instruction += ("The Architect must also set documentation=NOT_REQUIRED|REQUIRED|COMPLETE: "
                        "NOT_REQUIRED when no milestone/release documentation closure applies; "
                        "REQUIRED when accepted work needs one bounded documentation closure task; "
                        "COMPLETE when required documentation is synchronized and verified. "
                        "Do not use NOT_REQUIRED merely to advance.\n\n")
        payload = instruction + report
        return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _delivery_evidence_advanced(self, bridge: Any, payload: str) -> bool:
        def usable_baseline(value: Any) -> bool:
            return isinstance(value, dict) and isinstance(value.get("count"), int) and value.get("count") >= 0 and isinstance(value.get("text_hash"), str) and bool(value.get("text_hash"))

        user_baseline = self.state.get("architectDeliveryUserBaseline")
        if usable_baseline(user_baseline) and callable(getattr(bridge, "user_baseline", None)):
            current = bridge.user_baseline()
            if usable_baseline(current):
                if int(current.get("count", 0)) > int(user_baseline.get("count", 0)):
                    return True
                if current.get("text_hash") and current.get("text_hash") != user_baseline.get("text_hash"):
                    return True
        assistant_baseline = self.state.get("architectDeliveryBaseline")
        assistant_snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline", None)
        if usable_baseline(assistant_baseline) and callable(assistant_snapshot):
            current = assistant_snapshot()
            if usable_baseline(current):
                if int(current.get("count", 0)) > int(assistant_baseline.get("count", 0)):
                    return True
                if current.get("text_hash") and current.get("text_hash") != assistant_baseline.get("text_hash"):
                    return True
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            return True
        latest = getattr(bridge, "latest_user_message", None)
        if callable(latest):
            observed = latest()
            if isinstance(observed, str) and observed.strip() and normalize_prompt(observed) == normalize_prompt(payload):
                return True
        return False

    @staticmethod
    def _delivery_tokens(text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9_]+", text)

    def _result_delivery_wire_payload(self, payload: str, payload_hash: str) -> str:
        return f"ORCHESTRATOR_DELIVERY_SHA256={payload_hash}\n\n{payload}"

    def _result_payload_proof(self, bridge: Any, payload: str, payload_hash: str | None = None, history: bool = True) -> bool:
        """Prove delivery from a user message, using marker or legacy proof."""
        expected_hash = payload_hash or hashlib.sha256(payload.encode("utf-8")).hexdigest()
        latest = getattr(bridge, "latest_user_message", None)
        try:
            observed_latest = latest() if callable(latest) else None
        except Exception:
            observed_latest = None
        observed_messages = [observed_latest] if isinstance(observed_latest, str) else []
        messages = getattr(bridge, "user_message_texts", None)
        if observed_latest is None and not callable(latest) and callable(messages):
            try:
                observed_messages = [text for text in messages() if isinstance(text, str)]
            except Exception:
                pass
        if history and callable(messages):
            try:
                observed_messages = [text for text in messages() if isinstance(text, str)]
            except Exception:
                pass
        observed_messages = [text for text in observed_messages if isinstance(text, str)]
        if self.state.get("architectDeliveryProofVersion") == ARCHITECT_DELIVERY_PROOF_VERSION:
            marker = f"ORCHESTRATOR_DELIVERY_SHA256={expected_hash}"
            return any(any(line.strip() == marker for line in text.splitlines()) for text in observed_messages)
        expected_tokens = self._delivery_tokens(payload)
        expected_token_hash = hashlib.sha256(" ".join(expected_tokens).encode("utf-8")).hexdigest()
        return any(
            (tokens := self._delivery_tokens(text)) == expected_tokens
            and len(tokens) == len(expected_tokens)
            and hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest() == expected_token_hash
            for text in observed_messages
        )

    def _exact_result_payload_observed(self, bridge: Any, payload: str, history: bool = True) -> bool:
        return self._result_payload_proof(bridge, payload, history=history)

    def _wait_for_exact_result_payload(self, bridge: Any, payload: str, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self._exact_result_payload_observed(bridge, payload, history=False):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def clear_confirmed_stale_composer(self, bridge: Any, payload: str, payload_hash: str) -> bool:
        try:
            composer = getattr(bridge, "_live_composer", lambda: None)()
            if composer is None:
                return False
            observed = composer.inner_text(timeout=1000)
            if isinstance(observed, str) and observed.strip() and normalize_prompt(observed) == normalize_prompt(payload):
                composer.focus(timeout=1000)
                composer.press("ControlOrMeta+A", timeout=1000)
                composer.press("Backspace", timeout=1000)
                cleared = composer.inner_text(timeout=1000)
                if not isinstance(cleared, str) or cleared.strip():
                    return False
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_STALE_COMPOSER_CLEARED", self.state, hash=payload_hash)
                return True
        except Exception:
            return False
        return False

    def reconcile_exhausted_result_delivery(self, bridge: Any) -> bool:
        if self.state.get("state") != "HUMAN_REQUIRED" or self.state.get("humanRequiredReason") != "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED":
            return False
        payload, payload_hash = self._result_delivery_payload()
        if self.state.get("architectDeliveryPayloadHash") != payload_hash or not self._exact_result_payload_observed(bridge, payload):
            return False
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectDeliveryFailureClass": None, "architectSendError": None, "humanRequiredReason": None})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECONCILED", self.state, hash=payload_hash)
        self.clear_confirmed_stale_composer(bridge, payload, payload_hash)
        return True

    def recover_false_result_reconciliation(self, bridge: Any) -> bool:
        """Undo only a proven IDLE STOP caused by a false result reconciliation."""
        task_id = str(self.state.get("taskId") or "")
        fingerprint = self.state.get("architectResultFingerprint")
        result_path = self.state.get("executorResultPath")
        try:
            result_ready = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            result_ready = False
        active_pid = self.state.get("codexPid") or self.state.get("active_codex_pid")
        try:
            executor_active = bool(active_pid and LocalWatcher.process_alive(int(active_pid))) if active_pid else False
        except (TypeError, ValueError):
            executor_active = True
        consumed = self.state.get("consumedArchitectResponses") or {}
        record = consumed.get(fingerprint) if isinstance(fingerprint, str) else None
        if (self.state.get("state") != "IDLE" or not task_id or task_id != str(self.state.get("lastCompletedTaskId") or "")
                or not result_ready or self.state.get("architectSendState") != "CONFIRMED"
                or not isinstance(fingerprint, str) or not isinstance(record, dict)
                or record.get("taskId") != task_id or record.get("action") != "STOP"
                or self.state.get("nextPromptPath")
                or (self.state.get("nextTaskId") and self.state.get("nextTaskId") not in {task_id, str(self.state.get("lastCompletedTaskId") or "")})
                or executor_active):
            return False
        payload, payload_hash = self._result_delivery_payload()
        if self.state.get("architectDeliveryPayloadHash") != payload_hash:
            return False
        if self._exact_result_payload_observed(bridge, payload):
            return False
        messages = getattr(bridge, "user_message_texts", None)
        if not callable(messages):
            return False
        task_marker = f"previous response for task {task_id}"
        try:
            current_messages = messages()
        except Exception:
            return False
        recovery_present = any(isinstance(text, str) and task_marker in text and "Return only the machine-readable envelope" in text for text in current_messages)
        if not recovery_present:
            return False
        self.state.update({
            "state": "RESULT_READY", "architectSendState": "FAILED",
            "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_PRE_SEND_FAILURE",
            "humanRequiredReason": None, "architectResultFingerprint": None,
            "formatRecoveryCount": 0, "formatRecoveryExhausted": False,
            "architectFormatRecoveryTaskId": None, "architectBaseline": None,
        })
        consumed.pop(fingerprint, None)
        if self.state.get("documentationClosureFingerprint") == fingerprint:
            self.state["documentationClosureFingerprint"] = None
            self.state["documentationClosureCompletedTaskId"] = None
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "FALSE_RESULT_RECONCILIATION_RECOVERED", self.state, taskId=task_id, payloadHash=payload_hash, invalidArchitectResultFingerprint=fingerprint, exactPayloadObserved=False)
        return True

    def deliver_result(self, bridge: Any) -> str | None:
        if self.state.get("state") != "RESULT_READY":
            raise RuntimeError("RESULT_NOT_READY")
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_DECISION", self.state,
                    taskId=self.state.get("taskId"), deliveryStateBefore=self.state.get("architectSendState"),
                    generationVisible="NOT_SAMPLED_AT_LOG_POINT", discussionPauseActive=bool(self.state.get("discussionPauseActive")),
                    decision="EVALUATE", reason="RESULT_READY")
        if self.discussion_pause_active():
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_DECISION", self.state,
                        taskId=self.state.get("taskId"), decision="BLOCK", reason="DISCUSSION_PAUSED", retryAllowed=False)
            return
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            if not getattr(self, "_result_delivery_deferred_logged", False):
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_DEFERRED", self.state, reason="ARCHITECT_GENERATING")
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECOVERY_DECISION", self.state,
                            recoveryBranch="WAIT_GENERATION", retryAllowed=False, retryBlockedReason="ARCHITECT_GENERATING")
                self._result_delivery_deferred_logged = True
            return RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING
        self._result_delivery_deferred_logged = False
        path = Path(self.state["executorResultPath"])
        payload, payload_hash = self._result_delivery_payload()
        task_id = str(self.state["taskId"])
        prior_hash = self.state.get("architectDeliveryPayloadHash")
        delivery_state = self.state.get("architectSendState")
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_ATTEMPT", self.state, hash=payload_hash)
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_SEND_STAGE", self.state,
                    taskId=task_id, payloadHash=payload_hash, deliveryStateBefore=delivery_state,
                    sendActionAttempted=False, sendMethod="PENDING", stage="PREPARE")
        if prior_hash == payload_hash and delivery_state == "CONFIRMED":
            if not self._exact_result_payload_observed(bridge, payload, history=False):
                self.state.update({"architectSendState": "AMBIGUOUS", "architectSendError": "ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
                self.save()
                raise ResultSubmissionError("ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED")
            self.state.update({"state": "ARCHITECT_RUNNING", "architectResultFingerprint": None})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_CONFIRMED", self.state, hash=payload_hash, disposition="ALREADY_DELIVERED")
            return
        ambiguous_history = self.state.get("architectDeliveryFailureClass") == "ARCHITECT_DELIVERY_AMBIGUOUS"
        pre_send_failure = (delivery_state == "FAILED" and self.state.get("architectDeliveryFailureClass") == "ARCHITECT_DELIVERY_PRE_SEND_FAILURE")
        if prior_hash == payload_hash and pre_send_failure:
            if self._exact_result_payload_observed(bridge, payload, history=False):
                baseline = self.state.get("architectDeliveryBaseline")
                self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectSendError": None, "architectDeliveryFailureClass": None, "architectResultFingerprint": None, "architectBaseline": baseline, "humanRequiredReason": None})
                self.save()
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECONCILED", self.state, hash=payload_hash)
                self.clear_confirmed_stale_composer(bridge, payload, payload_hash)
                return
        elif prior_hash == payload_hash and (delivery_state in {"PENDING", "AMBIGUOUS"} or (delivery_state == "FAILED" and ambiguous_history)):
            if self._exact_result_payload_observed(bridge, payload, history=False):
                baseline = self.state.get("architectDeliveryBaseline")
                self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectSendError": None, "architectDeliveryFailureClass": None, "architectResultFingerprint": None, "architectBaseline": baseline, "humanRequiredReason": None})
                self.save()
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECONCILED", self.state, hash=payload_hash)
                self.clear_confirmed_stale_composer(bridge, payload, payload_hash)
                return
            self.state.update({"architectSendState": "AMBIGUOUS", "architectSendError": "ARCHITECT_DELIVERY_AMBIGUOUS", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_AMBIGUOUS", self.state, hash=payload_hash, errorCode="ARCHITECT_DELIVERY_AMBIGUOUS")
            raise ResultSubmissionError("ARCHITECT_DELIVERY_AMBIGUOUS")
        snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline", None)
        baseline = snapshot() if callable(snapshot) else None
        user_baseline = bridge.user_baseline() if hasattr(bridge, "user_baseline") else None
        self.state.update({
            "architectDeliveryTaskId": task_id,
            "architectDeliveryPayloadHash": payload_hash,
            "architectDeliveryBaseline": baseline,
            "architectDeliveryUserBaseline": user_baseline,
            "architectSendState": "PENDING",
            "architectSendError": None,
            "state": "RESULT_READY",
        })
        self.save()
        use_receipt = prior_hash != payload_hash or not prior_hash or self.state.get("architectDeliveryProofVersion") == ARCHITECT_DELIVERY_PROOF_VERSION
        wire_payload = self._result_delivery_wire_payload(payload, payload_hash) if use_receipt else payload
        if use_receipt:
            self.state["architectDeliveryProofVersion"] = ARCHITECT_DELIVERY_PROOF_VERSION
            self.save()
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        try:
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_SEND_STAGE", self.state,
                        taskId=task_id, payloadHash=payload_hash, stage="SEND_BEGIN", sendMethod=getattr(bridge, "last_send_method", None) or "UNKNOWN")
            sender(wire_payload)
        except Exception as error:
            code = getattr(error, "code", None) or type(error).__name__
            attempted = bool(getattr(bridge, "sendActionAttempted", False)) or bool(getattr(bridge, "last_send_method", None))
            acknowledged = bool(getattr(bridge, "sendActionAcknowledged", False))
            ambiguous = (attempted and not acknowledged) or code == "ARCHITECT_SUBMISSION_ACK_TIMEOUT"
            failure_class = "ARCHITECT_DELIVERY_AMBIGUOUS" if ambiguous else "ARCHITECT_DELIVERY_PRE_SEND_FAILURE"
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_ACK_DECISION", self.state,
                        taskId=task_id, payloadHash=payload_hash, sendActionAttempted=attempted,
                        sendMethod=getattr(bridge, "last_send_method", None), ackObserved=acknowledged,
                        classification=failure_class, failureClass=failure_class, failureError=code,
                        decision="AMBIGUOUS" if ambiguous else "FAILED", retryAllowed=ambiguous)
            self.state.update({"state": "RESULT_READY", "architectSendState": "AMBIGUOUS" if ambiguous else "FAILED", "architectSendError": code, "architectDeliveryFailureClass": failure_class})
            self.save()
            if ambiguous:
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_AMBIGUOUS", self.state, hash=payload_hash, errorCode=code, failureClass=failure_class, attempt=self.state.get("architectTransportRecoveryCount"))
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_FAILED", self.state, errorClass=code, failureClass=failure_class)
            raise
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_SEND_STAGE", self.state,
                    taskId=task_id, payloadHash=payload_hash, stage="SEND_END", sendActionAttempted=True,
                    sendMethod=getattr(bridge, "last_send_method", None), ackObserved=True)
        if not self._wait_for_exact_result_payload(bridge, payload):
            self.state.update({"state": "RESULT_READY", "architectSendState": "AMBIGUOUS", "architectSendError": "ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED", "architectDeliveryFailureClass": "ARCHITECT_DELIVERY_AMBIGUOUS"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_AMBIGUOUS", self.state, hash=payload_hash, errorCode="ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED")
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_ACK_DECISION", self.state,
                        taskId=task_id, payloadHash=payload_hash, messageEvidenceObserved=False, ackObserved=False,
                        classification="AMBIGUOUS", timeoutStage="PAYLOAD_OBSERVATION", decision="BLOCK", retryAllowed=True)
            raise ResultSubmissionError("ARCHITECT_RESULT_PAYLOAD_NOT_OBSERVED")
        self.state.update({"state": "ARCHITECT_RUNNING", "architectSendState": "CONFIRMED", "architectSendError": None, "architectDeliveryFailureClass": None, "architectResultFingerprint": None, "architectBaseline": baseline, "humanRequiredReason": None})
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_CONFIRMED", self.state, hash=payload_hash)
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_ACK_DECISION", self.state,
                    taskId=task_id, payloadHash=payload_hash, messageEvidenceObserved=True, ackObserved=True,
                    deliveryStateAfter="CONFIRMED", classification="CONFIRMED", decision="ALLOW", retryAllowed=False)
        self.clear_confirmed_stale_composer(bridge, payload, payload_hash)

    def deliver_result_with_recovery(self, bridge_factory: Callable[[], Any], max_attempts: int = 3, initial_bridge: Any | None = None) -> Any | None:
        """Reconcile or deliver one result without exiting the resident watcher."""
        def confirmed_current_payload() -> bool:
            if self.state.get("state") != "ARCHITECT_RUNNING" or self.state.get("architectSendState") != "CONFIRMED":
                return False
            try:
                _payload, payload_hash = self._result_delivery_payload()
            except (KeyError, OSError, UnicodeError):
                return False
            return self.state.get("architectDeliveryPayloadHash") == payload_hash

        bridge = initial_bridge
        attempt = 0
        while attempt < max_attempts:
            try:
                if bridge is None:
                    bridge = bridge_factory()
                if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
                    self.deliver_result(bridge)
                    time.sleep(0.25)
                    continue
                self.state["architectTransportRecoveryPayloadHash"] = self.state.get("architectDeliveryPayloadHash")
                self.state["architectTransportRecoveryCount"] = attempt
                self.save()
                disposition = self.deliver_result(bridge)
                if disposition == RESULT_DELIVERY_DEFERRED_ARCHITECT_GENERATING:
                    time.sleep(0.25)
                    continue
                self.state["architectTransportRecoveryCount"] = 0
                self.save()
                return bridge
            except Exception as error:
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_RECOVERY_DECISION", self.state,
                            taskId=self.state.get("taskId"), payloadHash=self.state.get("architectDeliveryPayloadHash"),
                            recoveryBranch="EXCEPTION", failureClass=type(error).__name__, attempt=attempt + 1,
                            retryAllowed=not confirmed_current_payload() and attempt + 1 < max_attempts,
                            reason="CONFIRMED_TERMINAL" if confirmed_current_payload() else "RECOVERY_ATTEMPT")
                if confirmed_current_payload():
                    self.state["architectTransportRecoveryCount"] = 0
                    self.save()
                    return bridge
                try:
                    if bridge is not None:
                        bridge.close()
                except Exception:
                    pass
                bridge = None
                attempt += 1
                if attempt >= max_attempts:
                    self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED", "architectTransportRecoveryCount": attempt})
                    self.save()
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "RESULT_DELIVERY_EXHAUSTED", self.state, reason="ARCHITECT_RESULT_TRANSPORT_EXHAUSTED")
                    runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_REQUIRED", self.state, reason="ARCHITECT_RESULT_TRANSPORT_EXHAUSTED")
                    return None
                time.sleep(0.25)
        return None

    def request_format_recovery(self, bridge: Any) -> None:
        """Request one machine-readable envelope without replaying the result."""
        if self.discussion_pause_active():
            return
        if (self.state.get("state") == "RESULT_READY" or self.state.get("architectDeliveryPayloadHash")
                and self.state.get("architectSendState") in {"PENDING", "FAILED", "AMBIGUOUS"}):
            raise RuntimeError("RESULT_DELIVERY_NOT_CONFIRMED")
        if self.state.get("architectDeliveryPayloadHash"):
            payload, payload_hash = self._result_delivery_payload()
            if payload_hash != self.state.get("architectDeliveryPayloadHash") or not self._exact_result_payload_observed(bridge, payload):
                raise RuntimeError("RESULT_DELIVERY_NOT_CONFIRMED")
        task_id = str(self.state["taskId"])
        self._reset_format_recovery_for_task(task_id)
        if int(self.state.get("formatRecoveryCount", 0)) >= 1:
            self.state.update({"state": "HUMAN_REQUIRED", "formatRecoveryExhausted": True})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "HUMAN_REQUIRED", self.state, reason="FORMAT_RECOVERY_EXHAUSTED")
            return
        message = "\n".join([
            f"Your previous response for task {task_id} was received successfully but did not contain a valid ORCHESTRATOR_RESULT envelope.",
            "Do not redo the underlying task.",
            "Do not request the Executor report again.",
            "Return only the machine-readable envelope for your already-completed decision:",
            "<ORCHESTRATOR_RESULT>",
            "classification=ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT",
            "action=EXECUTE|HUMAN_REQUIRED|STOP",
            f"taskId={task_id}",
            "documentation=NOT_REQUIRED|REQUIRED|COMPLETE",
            "Preserve the documentation disposition from the already-completed decision; do not downgrade a pending documentation closure to NOT_REQUIRED.",
            "promptBegin <complete next Executor prompt only when action=EXECUTE>",
            "promptEnd",
            "</ORCHESTRATOR_RESULT>",
        ])
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "FORMAT_RECOVERY_REQUESTED", self.state)
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result")
        sender(message)
        snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline", None)
        baseline = snapshot() if callable(snapshot) else None
        self.state.update({"state": "ARCHITECT_RUNNING", "formatRecoveryCount": 1, "architectFormatRecoveryTaskId": task_id, "architectBaseline": baseline, "humanRequiredReason": None})
        self.save()

    def recover_stale_format_human_required(self) -> bool:
        """Re-enter Architect observation only for the known cross-task stale gate."""
        task_id = str(self.state.get("taskId") or "")
        recovery_task = self.state.get("architectFormatRecoveryTaskId")
        reason = self.state.get("humanRequiredReason")
        result_path = self.state.get("executorResultPath")
        result_ready = isinstance(result_path, str) and Path(result_path).is_file() and Path(result_path).read_text(encoding="utf-8", errors="replace").strip()
        if (self.state.get("state") != "HUMAN_REQUIRED" or not self.state.get("formatRecoveryExhausted")
                or recovery_task in (None, "", task_id) or reason not in (None, "") or not result_ready):
            return False
        pid = self.state.get("codexPid")
        if pid and LocalWatcher.process_alive(int(pid)):
            return False
        self.state.update({"formatRecoveryCount": 0, "formatRecoveryExhausted": False, "architectFormatRecoveryTaskId": None})
        self.state["state"] = "ARCHITECT_RUNNING" if self.state.get("architectSendState") == "CONFIRMED" else "RESULT_READY"
        self.save()
        return True

    def accept_architect_response(self, response: str) -> dict[str, str]:
        with self._state_lock:
            return self._accept_architect_response_locked(response)

    def _accept_architect_response_locked(self, response: str) -> dict[str, str]:
        fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
        consumed = self.state.setdefault("consumedArchitectResponses", {})
        if fingerprint == self.state.get("architectResultFingerprint") or fingerprint in consumed:
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_RESPONSE_DUPLICATE", self.state, hash=fingerprint)
            return {"action": "DUPLICATE"}
        task_id = str(self.state.get("taskId") or self._architect_response_task_id(response) or "")
        if not task_id:
            raise ValueError("ARCHITECT_ENVELOPE_INVALID")
        decision = parse_orchestrator_result(response, task_id)
        documentation = decision["documentation"]
        pending_documentation = bool(self.state.get("documentationClosurePending"))
        if pending_documentation and documentation != "COMPLETE":
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "DOCUMENTATION_CLOSURE_REQUIRED"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_BYPASS_BLOCKED", self.state, disposition=documentation)
            raise ValueError("DOCUMENTATION_CLOSURE_REQUIRED")
        if documentation == "REQUIRED" and (decision["classification"] != "ACCEPTED" or decision["action"] != "EXECUTE" or not decision["prompt"].strip()):
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "DOCUMENTATION_DISPOSITION_INVALID"})
            self.save()
            raise ValueError("DOCUMENTATION_DISPOSITION_INVALID")
        self.state.update({"formatRecoveryCount": 0, "formatRecoveryExhausted": False, "architectFormatRecoveryTaskId": None})
        self.state.update({
            "postDiscussionEnvelopeRequired": False,
            "postDiscussionEnvelopeRepairAwaiting": False,
            "postDiscussionProtocolFailure": None,
        })
        if documentation == "REQUIRED":
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_REQUIRED", self.state, taskId=task_id, disposition=documentation)
        if documentation == "COMPLETE":
            self.state.update({"documentationClosurePending": False, "documentationClosureCompletedTaskId": task_id, "documentationClosureFingerprint": fingerprint})
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_ACCEPTED", self.state, taskId=task_id, disposition=documentation)
        if decision["action"] == "EXECUTE":
            next_id = self._canonical_next_task_id(task_id)
            path = self.prompts_dir / f"{next_id}.txt"
            target = self._owned_task_worktree(next_id, decision["prompt"], context_task_id=str(self.state.get("taskId") or ""))
            if target is None:
                raise RuntimeError("EXECUTOR_PROJECT_CONTEXT_MISSING")
            prompt_bytes = decision["prompt"].encode("utf-8")
            if path.exists() and path.read_bytes() != prompt_bytes:
                raise RuntimeError("EXECUTOR_PROMPT_EVIDENCE_MISMATCH")
            if not path.exists():
                atomic_write(path, prompt_bytes)
            self.state["architectResultFingerprint"] = fingerprint
            target_text = str(target)
            self.state.update({"state": "NEXT_PROMPT_READY", "nextPromptPath": str(path), "nextTaskId": next_id, "targetProject": target_text, "targetRepo": target_text, "targetWorktree": target_text, "humanRequiredReason": None})
            if documentation == "REQUIRED":
                self.state.update({"documentationClosurePending": True, "documentationClosureSourceTaskId": task_id, "documentationClosureTaskId": next_id})
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "DOCUMENTATION_CLOSURE_TASK_STAGED", self.state, taskId=next_id, sourceTaskId=task_id)
        elif decision["action"] == "HUMAN_REQUIRED":
            self.state["architectResultFingerprint"] = fingerprint
            self.state.update({
                "state": "HUMAN_REQUIRED", "nextPromptPath": None,
                "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED",
                "architectDiscussionBaseline": self.state.get("architectBaseline"),
            })
        else:
            self.state["architectResultFingerprint"] = fingerprint
            self.state.update({"state": "IDLE", "nextPromptPath": None, "humanRequiredReason": None})
        consumed[fingerprint] = {"taskId": task_id, "classification": decision["classification"], "action": decision["action"], "state": "RECEIVED", "origin": "BOOTSTRAP" if self.state.get("architectBootstrapAwaiting") else "RESULT_REVIEW"}
        self.save()
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_RESPONSE_ACCEPTED", self.state, hash=fingerprint, classification=decision["classification"], action=decision["action"])
        return decision

    def launch_next(self, launcher: Callable[[str, Path], Any]) -> Any:
        if self.state.get("state") != "NEXT_PROMPT_READY" or self.discussion_pause_active():
            return None
        prompt_path = Path(self.state["nextPromptPath"])
        prompt = prompt_path.read_text(encoding="utf-8")
        task_id = str(self.state.get("nextTaskId") or self.state.get("taskId") or "")
        owned = self.state.get("taskWorktrees", {}).get(task_id)
        target = str(owned["worktreePath"]) if isinstance(owned, dict) and owned.get("worktreePath") else resolve_executor_worktree(prompt, self._configured_fallback_project())
        self.state.update({"targetProject": target, "targetRepo": target, "targetWorktree": target})
        self.save()
        process = launcher(prompt, self._result_path(str(self.state["nextTaskId"])))
        self._active_process = process
        self.mark_executor_started(str(self.state["nextTaskId"]), int(process.pid), self._result_path(str(self.state["nextTaskId"])))
        return process

    def _post_discussion_baseline_advanced(self, bridge: Any, response: str) -> bool:
        """Prove that a completed response belongs to the resumed discussion epoch."""
        baseline = self.state.get("postDiscussionProtocolBaseline") or self.state.get("architectDiscussionBaseline")
        if not isinstance(baseline, dict):
            return False
        count = baseline.get("count")
        text_hash = baseline.get("text_hash")
        if not isinstance(count, int) or count < 0 or not isinstance(text_hash, str) or not text_hash:
            return False
        current_reader = getattr(bridge, "assistant_baseline", None)
        if callable(current_reader):
            try:
                current = current_reader()
            except Exception:
                current = None
            if isinstance(current, dict):
                current_count = current.get("count")
                current_hash = current.get("text_hash")
                if isinstance(current_count, int) and current_count > count:
                    return True
                if current_hash and current_hash != text_hash:
                    return True
        return False

    def _exact_staged_prompt_recovery_task(self) -> str | None:
        """Return the pending task only when its staged prompt identity is exact."""
        state = self.state
        task_id = str(state.get("nextTaskId") or "").strip()
        transaction_task = str(state.get("rolloverTransactionTaskId") or "").strip()
        prompt_value = state.get("nextPromptPath")
        if not (
            state.get("state") in {"NEXT_PROMPT_READY", "ARCHITECT_RUNNING"}
            and (state.get("state") == "NEXT_PROMPT_READY" or state.get("postDiscussionEnvelopeRepairAwaiting") is True)
            and state.get("rolloverDue") is True
            and state.get("rolloverPending") is True
            and state.get("handoverRequested") is True
            and state.get("rolloverTransactionId")
            and transaction_task == task_id
            and prompt_value
            and task_id
        ):
            return None
        path = Path(str(prompt_value))
        expected = self.prompts_dir / f"{task_id}.txt"
        try:
            if path.resolve() != expected.resolve() or not path.is_file() or path.stat().st_size == 0:
                return None
            record = state.get("taskWorktrees", {}).get(task_id)
            if not isinstance(record, dict) or str(record.get("taskId") or "") != task_id:
                return None
            if not record.get("worktreePath") or not Path(str(record["worktreePath"])).is_dir():
                return None
            path.read_bytes()
        except (OSError, RuntimeError, TypeError):
            return None
        return task_id

    def _post_discussion_staged_prompt_evidence_valid(self, task_id: str) -> bool:
        transaction_id = self.state.get("postDiscussionProtocolTransactionId")
        task_id = str(task_id or "")
        if (not transaction_id or self.state.get("postDiscussionProtocolTaskId") != task_id
                or str(self.state.get("nextTaskId") or "") != task_id):
            return False
        if (self._exact_staged_prompt_recovery_task() == task_id
                and str(transaction_id) == str(self.state.get("rolloverTransactionId") or "")):
            return True
        # A successful fresh-Architect authority commit retires the live rollover
        # transaction. Preserve its identity as protocol evidence until the
        # staged-prompt envelope is consumed.
        if (str(self.state.get("postDiscussionProtocolRolloverCommittedTransactionId") or "") != str(transaction_id)
                or self.state.get("rolloverDue") is not False
                or self.state.get("rolloverPending") is not False
                or self.state.get("rolloverInProgress") is not False
                or self.state.get("handoverRequested") is not False
                or not self.state.get("architectConversationId")):
            return False
        prompt_value = self.state.get("nextPromptPath")
        expected = self.prompts_dir / f"{task_id}.txt"
        record = self.state.get("taskWorktrees", {}).get(task_id)
        try:
            return bool(
                prompt_value
                and Path(str(prompt_value)).resolve() == expected.resolve()
                and expected.is_file() and expected.stat().st_size > 0
                and isinstance(record, dict) and str(record.get("taskId") or "") == task_id
                and record.get("worktreePath") and Path(str(record["worktreePath"])).is_dir()
            )
        except (OSError, RuntimeError, TypeError):
            return False

    def _accept_exact_staged_prompt_envelope(self, response: str, task_id: str) -> dict[str, str]:
        """Accept only a wrapper around the already-persisted staged prompt."""
        if not self._post_discussion_staged_prompt_evidence_valid(task_id):
            raise ValueError("ARCHITECT_STAGED_PROMPT_EVIDENCE_INVALID")
        decision = parse_orchestrator_result(response, task_id)
        if decision["action"] != "EXECUTE":
            raise ValueError("ARCHITECT_STAGED_PROMPT_ENVELOPE_INVALID")
        prompt = Path(str(self.state["nextPromptPath"])).read_text(encoding="utf-8")
        parsed_prompt = decision["prompt"].replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        staged_prompt = prompt.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if hashlib.sha256(parsed_prompt.encode("utf-8")).hexdigest() != hashlib.sha256(staged_prompt.encode("utf-8")).hexdigest():
            raise ValueError("ARCHITECT_STAGED_PROMPT_HASH_MISMATCH")
        fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()
        consumed = self.state.setdefault("consumedArchitectResponses", {})
        if fingerprint == self.state.get("architectResultFingerprint") or fingerprint in consumed:
            return {"action": "DUPLICATE", "taskId": task_id}
        self.state.update({
            "architectResultFingerprint": fingerprint,
            "state": "NEXT_PROMPT_READY",
            "humanRequiredReason": None,
            "postDiscussionEnvelopeRequired": False,
            "postDiscussionEnvelopeRepairAwaiting": False,
            "postDiscussionProtocolFailure": None,
        })
        self.state.pop("postDiscussionProtocolRolloverCommittedTransactionId", None)
        consumed[fingerprint] = {
            "taskId": task_id,
            "classification": decision["classification"],
            "action": decision["action"],
            "state": "RECEIVED",
            "origin": "POST_DISCUSSION_STAGED_PROMPT_REPAIR",
        }
        self.save()
        return decision

    def request_post_discussion_envelope_repair(self, bridge: Any) -> bool:
        """Request exactly one formatting-only envelope correction for this epoch."""
        task_id = str(self.state.get("postDiscussionProtocolTaskId") or self.state.get("taskId") or "")
        epoch = int(self.state.get("postDiscussionResumeEpoch", 0) or 0)
        if (not self.state.get("postDiscussionEnvelopeRequired") or not task_id
                or self.state.get("postDiscussionEnvelopeRepairAttempted")
                or self.state.get("postDiscussionEnvelopeRepairTaskId") not in (None, "", task_id)
                or self.state.get("postDiscussionEnvelopeRepairEpoch") not in (None, "", epoch)):
            return False
        if self.state.get("postDiscussionProtocolTransactionId") and not self._post_discussion_staged_prompt_evidence_valid(task_id):
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED", "postDiscussionProtocolFailure": "ARCHITECT_STAGED_PROMPT_EVIDENCE_INVALID"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_FAILED", self.state, taskId=task_id, reason="STAGED_PROMPT_EVIDENCE_INVALID")
            return False
        message = "\n".join([
            "Your previous completed response did not contain the required final ORCHESTRATOR_RESULT envelope.",
            "Do not redo or re-evaluate the underlying task or human decision.",
            "Return only the machine-readable envelope for your already-completed decision, preserving classification, action, documentation disposition, and the complete next Executor prompt where applicable.",
            f"taskId={task_id}",
            "The envelope must be the final content of the response.",
        ])
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_REQUESTED", self.state, taskId=task_id, resumeEpoch=epoch)
        sender = getattr(bridge, "submit_result_bounded", None) or getattr(bridge, "submit_result", None)
        if not callable(sender):
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED", "postDiscussionProtocolFailure": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED"})
            self.save()
            return False
        try:
            sender(message)
        except Exception as error:
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED", "postDiscussionProtocolFailure": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_FAILED", self.state, taskId=task_id, errorClass=type(error).__name__)
            return False
        baseline = bridge.assistant_baseline() if callable(getattr(bridge, "assistant_baseline", None)) else self.state.get("postDiscussionProtocolBaseline")
        self.state.update({
            "state": "ARCHITECT_RUNNING",
            "postDiscussionEnvelopeRepairAttempted": True,
            "postDiscussionEnvelopeRepairAwaiting": True,
            "postDiscussionEnvelopeRepairTaskId": task_id,
            "postDiscussionEnvelopeRepairEpoch": epoch,
            "architectBaseline": baseline,
            "postDiscussionProtocolBaseline": baseline,
            "humanRequiredReason": None,
        })
        self.save()
        return True

    def reconcile_post_discussion_response(self, bridge: Any, response: str, completed: bool = True) -> str:
        """Consume or repair one response after F10/ORCH:RESUME."""
        if not self.state.get("postDiscussionEnvelopeRequired"):
            return "NOT_REQUIRED"
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            return "GENERATING"
        baseline = self.state.get("postDiscussionProtocolBaseline") or self.state.get("architectDiscussionBaseline")
        task_id = str(self.state.get("postDiscussionProtocolTaskId") or self.state.get("taskId") or "")
        if self.state.get("postDiscussionProtocolTransactionId") and not self._post_discussion_staged_prompt_evidence_valid(task_id):
            self.state.update({
                "state": "HUMAN_REQUIRED",
                "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED",
                "postDiscussionProtocolFailure": "ARCHITECT_STAGED_PROMPT_EVIDENCE_INVALID",
            })
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_FAILED", self.state, taskId=task_id, reason="STAGED_PROMPT_EVIDENCE_INVALID")
            return "FAILED"
        if isinstance(response, str) and response.strip() and not (
            isinstance(baseline, dict)
            and isinstance(baseline.get("count"), int) and baseline.get("count") >= 0
            and isinstance(baseline.get("text_hash"), str) and bool(baseline.get("text_hash"))
        ):
            task_id = str(self.state.get("postDiscussionProtocolTaskId") or self.state.get("taskId") or "")
            self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_RELATIONSHIP_INCONCLUSIVE", "postDiscussionProtocolFailure": "ARCHITECT_PROTOCOL_ENVELOPE_RELATIONSHIP_INCONCLUSIVE"})
            self.save()
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_FAILED", self.state, taskId=task_id, reason="BASELINE_UNPROVEN")
            return "FAILED"
        if not completed or not self._post_discussion_baseline_advanced(bridge, response):
            return "WAIT"
        task_id = str(self.state.get("postDiscussionProtocolTaskId") or self.state.get("taskId") or "")
        staged_task_id = task_id if self._post_discussion_staged_prompt_evidence_valid(task_id) else None
        try:
            if staged_task_id and staged_task_id == task_id:
                decision = self._accept_exact_staged_prompt_envelope(response, staged_task_id)
            else:
                decision = self.accept_architect_response(response)
        except (ValueError, RuntimeError):
            runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_PROTOCOL_ENVELOPE_MISSING_AFTER_DISCUSSION", self.state, taskId=task_id)
            self.state.update({"humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_MISSING_AFTER_DISCUSSION"})
            self.save()
            if self.state.get("postDiscussionEnvelopeRepairAttempted"):
                self.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED", "postDiscussionProtocolFailure": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED", "postDiscussionEnvelopeRepairAwaiting": False})
                self.save()
                runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_FAILED", self.state, taskId=task_id)
                return "FAILED"
            if self.request_post_discussion_envelope_repair(bridge):
                return "REPAIR_REQUESTED"
            return "FAILED"
        if isinstance(decision, dict) and decision.get("action") == "DUPLICATE":
            return "DUPLICATE"
        runtime_log(getattr(self, "runtime_logger", None), getattr(self, "runtime_run_id", None), "ARCHITECT_ENVELOPE_REPAIR_ACCEPTED" if self.state.get("postDiscussionEnvelopeRepairAttempted") else "ARCHITECT_RESPONSE_ACCEPTED", self.state, taskId=task_id)
        self.state["postDiscussionEnvelopeRepairAwaiting"] = False
        self.save()
        return str(decision.get("action") or "ACCEPTED")


class DiscussionHotkeyController:
    """Small process-local F9/F10 controller; the durable flag remains watcher-owned."""
    VK_F9 = 0x78
    VK_F10 = 0x79
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_NOREPEAT = 0x4000

    def __init__(self, watcher: LocalFirstOrchestrator, emit: Callable[[str], None] = print):
        self.watcher = watcher
        self.emit = emit
        self.active = False
        self._thread = None
        self._thread_id = None

    def dispatch(self, key: str) -> bool:
        try:
            if key == "F9":
                self.watcher.request_discussion_pause()
                return True
            if key == "F10":
                self.watcher.request_discussion_resume()
                return True
        except Exception as error:
            task_id = self.watcher.state.get("taskId") or self.watcher.state.get("nextTaskId")
            runtime_log(
                getattr(self.watcher, "runtime_logger", None),
                getattr(self.watcher, "runtime_run_id", None),
                "DISCUSSION_CONTROL_DISPATCH_FAILED",
                self.watcher.state,
                key=key,
                taskId=task_id,
                exceptionClass=type(error).__name__,
                safeDisposition="DISCUSSION_CONTROL_FAIL_CLOSED",
            )
            self.emit(f"DISCUSSION_CONTROL_DISPATCH_FAILED key={key} error={type(error).__name__} safeDisposition=DISCUSSION_CONTROL_FAIL_CLOSED")
            return False
        return False

    def start(self, log: Callable[[str], None] | None = None) -> bool:
        if os.name != "nt":
            self.emit("HUMAN_DISCUSSION_HOTKEYS_UNAVAILABLE platform=non_windows")
            return False
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
        registration = {"ok": False, "thread_id": None}

        def run() -> None:
            thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
            registration["thread_id"] = thread_id
            if not user32.RegisterHotKey(None, 9, self.MOD_NOREPEAT, self.VK_F9) or not user32.RegisterHotKey(None, 10, self.MOD_NOREPEAT, self.VK_F10):
                user32.UnregisterHotKey(None, 9)
                user32.UnregisterHotKey(None, 10)
                return
            registration["ok"] = True
            message = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == self.WM_HOTKEY:
                    self.dispatch("F9" if message.wParam == 9 else "F10" if message.wParam == 10 else "")
            user32.UnregisterHotKey(None, 9)
            user32.UnregisterHotKey(None, 10)

        self._thread = threading.Thread(target=run, name="orchestrator-hotkeys", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 1.0
        while registration["thread_id"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        while not registration["ok"] and registration["thread_id"] is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not registration["ok"]:
            self.emit("HUMAN_DISCUSSION_HOTKEY_REGISTRATION_FAILED")
            if log:
                log("HUMAN_DISCUSSION_HOTKEY_REGISTRATION_FAILED")
            return False
        self._thread_id = registration["thread_id"]
        self.active = True
        self.emit("F9 = PAUSE FOR ARCHITECT DISCUSSION")
        self.emit("F10 = RESUME ORCHESTRATOR")
        return True

    def stop(self) -> None:
        if self.active and self._thread_id and os.name == "nt":
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.active = False


class RemoteDiscussionControlMonitor:
    """Read-only Architect user-message monitor for exact pause/resume commands."""
    def __init__(self, watcher: LocalFirstOrchestrator, bridge_factory: Callable[[], Any], emit: Callable[[str], None] = print, startup_timeout: float = 45.0, shutdown_timeout: float = 5.0):
        self.watcher = watcher
        self.bridge_factory = bridge_factory
        self.emit = emit
        self.bridge = None
        self.active = False
        self._thread = None
        self._cursor = None
        self._conversation_id = None
        self._message_count = None
        self._consumed_command_ids: list[str] = []
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._startup_ok = False
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout

    @staticmethod
    def _identity(message: dict[str, Any], index: int) -> str:
        identity = message.get("id")
        return str(identity) if identity else hashlib.sha256(str(message.get("text") or "").encode("utf-8")).hexdigest() + f":{index}"

    def _messages(self, bridge: Any, start: int = 0) -> list[dict[str, Any]]:
        reader = getattr(bridge, "control_user_messages", None) or getattr(bridge, "user_messages", None)
        if not callable(reader):
            raise RuntimeError("REMOTE_CONTROL_USER_READER_UNAVAILABLE")
        try:
            messages = reader(start=start)
        except TypeError:
            messages = reader()
        return [message for message in messages if isinstance(message, dict) and message.get("author", "user") == "user"] if isinstance(messages, list) else []

    def establish_startup_baseline(self, bridge: Any | None = None) -> None:
        self.bridge = bridge or self.bridge_factory()
        requested_id = self.watcher.state.get("architectConversationId")
        self._conversation_id = canonicalize_attached_architect_conversation(
            self.watcher, self.bridge, requested_id, bind_memory=False
        )
        messages = self._messages(self.bridge)
        self._cursor = self._identity(messages[-1], len(messages) - 1) if messages else None
        count_reader = getattr(self.bridge, "control_user_message_count", None)
        self._message_count = count_reader() if callable(count_reader) else len(messages)
        atomic_write(self.watcher.state_dir / "remote-control.json", json.dumps({"cursor": self._cursor, "count": len(messages)}).encode("utf-8"))

    def poll_once(self) -> int:
        tracer = diagnostic_trace_for(self.watcher) or diagnostic_trace_for(self.bridge)
        poll_started = time.monotonic()
        connection_id = getattr(self.bridge, "_diagnostic_connection_id", None)
        if tracer:
            tracer.record("REMOTE_CONTROL", "RemoteDiscussionControlMonitor.poll_once", "REMOTE_CONTROL_POLL_BEGIN", "BEGIN", self.watcher.state, connectionId=connection_id)
        messages = []
        message_start_index = 0
        try:
            count_reader = getattr(self.bridge, "control_user_message_count", None)
            current_count = count_reader() if callable(count_reader) else None
            unchanged = current_count is not None and self._message_count is not None and current_count == self._message_count
            if not unchanged:
                read_started = time.monotonic()
                if tracer:
                    tracer.record("REMOTE_CONTROL", "RemoteDiscussionControlMonitor._messages", "REMOTE_CONTROL_USER_READ_BEGIN", "BEGIN", self.watcher.state, connectionId=connection_id, requestedStart=self._message_count or 0)
                try:
                    if current_count is not None and self._message_count is not None and current_count > self._message_count:
                        message_start_index = self._message_count
                        messages = self._messages(self.bridge, start=message_start_index)
                    else:
                        messages = self._messages(self.bridge)
                        message_start_index = 0
                except Exception as error:
                    if tracer:
                        tracer.record("REMOTE_CONTROL", "RemoteDiscussionControlMonitor._messages", "REMOTE_CONTROL_USER_READ_ERROR", "ERROR", self.watcher.state, connectionId=connection_id, errorClass=type(error).__name__, errorMessage=str(error), stackTrace=traceback.format_exc())
                    raise
                finally:
                    if tracer:
                        tracer.record("REMOTE_CONTROL", "RemoteDiscussionControlMonitor._messages", "REMOTE_CONTROL_USER_READ_END", "END", self.watcher.state, connectionId=connection_id, userMessageCount=len(messages), duration_ms=(time.monotonic() - read_started) * 1000)
            start = 0
            if message_start_index == 0 and self._cursor is not None:
                positions = [index for index, message in enumerate(messages) if self._identity(message, index) == self._cursor]
                start = positions[-1] + 1 if positions else len(messages)
            elif message_start_index:
                start = 0
            observed = 0
            for relative_index, message in enumerate(messages[start:], start=start):
                index = message_start_index + relative_index
                text = message.get("text")
                if isinstance(text, str):
                    command = text.strip()
                    if command in {"ORCH:PAUSE", "ORCH:RESUME"}:
                        identity = self._identity(message, index)
                        if identity in self._consumed_command_ids:
                            runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_COMMAND_DUPLICATE_IGNORED", self.watcher.state, messageHash=hashlib.sha256(text.encode("utf-8")).hexdigest())
                            continue
                        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_COMMAND_OBSERVED", self.watcher.state, command=command, messageHash=hashlib.sha256(text.encode("utf-8")).hexdigest())
                        if command == "ORCH:PAUSE":
                            self.watcher.request_remote_discussion_pause()
                        else:
                            self.watcher.request_remote_discussion_resume()
                        self._consumed_command_ids.append(identity)
                        self._consumed_command_ids = self._consumed_command_ids[-32:]
                        observed += 1
                        self._cursor = identity
            if messages:
                self._cursor = self._identity(messages[-1], message_start_index + len(messages) - 1)
            if current_count is not None:
                self._message_count = current_count
            atomic_write(self.watcher.state_dir / "remote-control.json", json.dumps({"cursor": self._cursor, "count": len(messages)}).encode("utf-8"))
            return observed
        finally:
            if tracer:
                tracer.record("REMOTE_CONTROL", "RemoteDiscussionControlMonitor.poll_once", "REMOTE_CONTROL_POLL_END", "END", self.watcher.state, connectionId=connection_id, userMessageCount=len(messages), duration_ms=(time.monotonic() - poll_started) * 1000)

    def start(self) -> bool:
        def run() -> None:
            try:
                self.establish_startup_baseline()
                if self._stop_event.is_set():
                    return
                self._startup_ok = True
                self.active = True
                self._ready.set()
                while not self._stop_event.is_set():
                    current = self.watcher.state.get("architectConversationId")
                    if current and self._conversation_id and current != self._conversation_id:
                        self.bridge.close()
                        self.bridge = None
                        self.establish_startup_baseline()
                    self.poll_once()
                    self._stop_event.wait(1.0)
            except Exception as error:
                self._startup_ok = False
                self._ready.set()
                runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_MONITOR_ERROR", self.watcher.state, errorClass=type(error).__name__)
            finally:
                self.active = False
                if self.bridge is not None:
                    try:
                        self.bridge.close()
                    except Exception:
                        pass
                    self.bridge = None

        self._ready.clear()
        self._stop_event.clear()
        self._startup_ok = False
        self._thread = threading.Thread(target=run, name="orchestrator-remote-control", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.startup_timeout) or not self._startup_ok:
            self._stop_event.set()
            if self._thread.is_alive():
                self._thread.join(timeout=self.shutdown_timeout)
            self.emit("REMOTE_CONTROL_UNAVAILABLE")
            return False
        self.emit("REMOTE CONTROL ACTIVE")
        self.emit("ORCH:PAUSE = pause Architect relay")
        self.emit("ORCH:RESUME = resume Orchestrator")
        runtime_log(getattr(self.watcher, "runtime_logger", None), getattr(self.watcher, "runtime_run_id", None), "REMOTE_CONTROL_MONITOR_STARTED", self.watcher.state)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self.active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


def visible_executor_launcher(project: str, watcher: LocalFirstOrchestrator) -> Callable[[str, Path], Any]:
    """Build the existing visible Codex launch surface for one owned child."""
    def launch(prompt: str, result_path: Path) -> Any:
        target = watcher.state["targetWorktree"]
        configured_session = watcher.state.get("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID)
        if configured_session != AFFOTECH_EXECUTOR_SESSION_ID:
            raise RuntimeError("EXECUTOR_SESSION_IDENTITY_MISMATCH")
        verify_executor_session(AFFOTECH_EXECUTOR_SESSION_ID)
        runner = CodexRunner(project, child_project_dir=target, session_id=AFFOTECH_EXECUTOR_SESSION_ID)
        watcher.state.update({"executorSessionId": AFFOTECH_EXECUTOR_SESSION_ID, "executorSessionMode": "PERSISTENT"})
        attempt = int(watcher.state.get("executorAttemptNumber", 0)) + 1
        stderr_identity = watcher.state.get("nextTaskId") or watcher.state.get("taskId") or "unknown"
        stderr_path = watcher.state_dir / "executor-logs" / f"{stderr_identity}-attempt-{attempt}.stderr.txt"
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.touch(exist_ok=True)
        watcher.state.update({"stderrLogPath": str(stderr_path)})
        watcher.save()
        command_args = ["exec", "resume", runner.session_id, "-o", str(result_path), "-"]
        command = (["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", runner.executable, *command_args] if os.name == "nt" and runner.launcher[0].lower().endswith(".ps1") else [*runner.launcher, *command_args])
        print("==================================================")
        print(f"AFFOTECH AUTOMATED EXECUTOR taskId={stderr_identity}")
        print("Executor is running")
        print("==================================================")
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=None, stderr=subprocess.PIPE, cwd=target, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        child_stderr = getattr(child, "stderr", None)
        if child_stderr is not None:
            def capture_stderr() -> None:
                with stderr_path.open("a", encoding="utf-8", errors="replace") as log:
                    while True:
                        chunk = child_stderr.readline()
                        if not chunk:
                            break
                        if isinstance(chunk, bytes):
                            text = chunk.decode("utf-8", errors="replace")
                        else:
                            text = str(chunk)
                        log.write(text)
                        log.flush()
                        try:
                            sys.stderr.write(text)
                            sys.stderr.flush()
                        except (OSError, AttributeError):
                            pass
            threading.Thread(target=capture_stderr, name="executor-stderr", daemon=True).start()
        assert child.stdin is not None
        child.stdin.write(runner.assemble_prompt(prompt).encode("utf-8")); child.stdin.close()
        return child

    return launch


def run_executor_state_once(watcher: LocalFirstOrchestrator, launch: Callable[[str, Path], Any]) -> str:
    """Advance one executor/crash/human state cycle, including same-invocation authorization."""
    state = watcher.state.get("state", "IDLE")
    if state == "EXECUTOR_RUNNING":
        state = watcher.wait_for_executor()
        if state != "EXECUTOR_RUNNING":
            print("CODEX_FINISHED taskId=%s pid=%s exitCode=%s" % (watcher.state.get("taskId"), watcher.state.get("codexPid"), watcher.state.get("executorExitCode")))
            runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "CODEX_FINISHED", watcher.state, pid=watcher.state.get("codexPid"), exitCode=watcher.state.get("executorExitCode"))
        if state == "EXECUTOR_CRASHED":
            watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "EXECUTOR_EXITED_WITHOUT_RESULT"})
            watcher.save()
            print("STATE=HUMAN_REQUIRED reason=EXECUTOR_EXITED_WITHOUT_RESULT")
            runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "HUMAN_REQUIRED", watcher.state, reason="EXECUTOR_EXITED_WITHOUT_RESULT")
        else:
            return state
    if watcher.state.get("state") == "HUMAN_REQUIRED":
        if watcher.state.get("humanRequiredReason") == "ARCHITECT_DECISION_HUMAN_REQUIRED":
            return "HUMAN_REQUIRED"
        recovered = watcher.recover_prelaunch_incomplete(launch)
        if recovered is not None:
            return "NEXT_PROMPT_READY"
        recovered = watcher.authorize_postlaunch_retry(launch)
        if recovered is not None:
            return "NEXT_PROMPT_READY"
        return "STOP"
    return watcher.state.get("state", "IDLE")


def resident_human_decision_response(watcher: LocalFirstOrchestrator, response: str, baseline: dict[str, Any] | None = None, bridge: Any | None = None) -> str:
    """Consume one response while waiting for a business decision."""
    if watcher.discussion_pause_active():
        if isinstance(baseline, dict):
            watcher.state["architectBaseline"] = baseline
            watcher.save()
        return "DISCUSSION"
    if watcher.state.get("postDiscussionEnvelopeRequired"):
        return watcher.reconcile_post_discussion_response(bridge, response, completed=True) if bridge is not None else "WAIT"
    if "<ORCHESTRATOR_RESULT>" not in response:
        if isinstance(baseline, dict):
            watcher.state["architectBaseline"] = baseline
            watcher.save()
        return "DISCUSSION"
    try:
        decision = watcher.accept_architect_response(response)
    except (ValueError, RuntimeError):
        watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_DECISION_HUMAN_REQUIRED"})
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_HUMAN_DECISION_RESPONSE_INVALID", watcher.state)
        return "INVALID"
    watcher.state["architectBaseline"] = baseline if isinstance(baseline, dict) else watcher.state.get("architectBaseline")
    watcher.save()
    return str(decision.get("action") or "")


def _trace_rollover_gate(watcher: Any, safe_boundary_state: str | None, decision: str, reason: str, **fields: Any) -> None:
    state = watcher.state
    runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_TRIGGER_DECISION", state,
                taskId=state.get("taskId"), nextTaskId=state.get("nextTaskId"), rolloverDue=bool(state.get("rolloverDue")),
                rolloverTrigger=state.get("rolloverTrigger"), decision=decision, reason=reason,
                discussionPauseActive=bool(state.get("discussionPauseActive")), workflowMutation=False)
    runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_EXECUTION_GATE", state,
                taskId=state.get("taskId"), nextTaskId=state.get("nextTaskId"), rolloverDue=bool(state.get("rolloverDue")),
                rolloverTrigger=state.get("rolloverTrigger"), rolloverPending=bool(state.get("rolloverPending")),
                rolloverInProgress=bool(state.get("rolloverInProgress")), handoverRequested=bool(state.get("handoverRequested")),
                discussionPauseActive=bool(state.get("discussionPauseActive")), safeBoundary=safe_boundary_state == "NEXT_PROMPT_READY",
                gateResult="ALLOW" if decision in {"ATTEMPT", "ALLOW"} else "BLOCK", reason=reason)
    tracer = diagnostic_trace_for(watcher)
    if not tracer:
        return
    active_pid = state.get("codexPid") or state.get("active_codex_pid")
    active_alive = None
    if active_pid:
        try:
            active_alive = LocalWatcher.process_alive(int(active_pid))
        except (TypeError, ValueError):
            active_alive = False
    tracer.record(
        "ROLLOVER",
        fields.pop("function", "rollover_gate"),
        "ROLLOVER_GATE",
        "DECISION",
        state,
        workflowState=state.get("state"),
        safeBoundaryState=safe_boundary_state or state.get("state"),
        memorySource=state.get("architectMemoryOwnershipSource"),
        memoryBytes=state.get("architectMemoryBytes"),
        memoryMiB=state.get("architectMemoryMiB"),
        thresholdBytes=ARCHITECT_MEMORY_THRESHOLD_BYTES,
        thresholdSatisfied=bool(isinstance(state.get("architectMemoryBytes"), int) and state.get("architectMemoryBytes") >= ARCHITECT_MEMORY_THRESHOLD_BYTES),
        rolloverDueBefore=state.get("rolloverDue"),
        rolloverTriggerBefore=state.get("rolloverTrigger"),
        discussionPaused=bool(state.get("discussionPauseActive")),
        architectGenerating=fields.pop("architectGenerating", None),
        executorRunning=fields.pop("executorRunning", state.get("executorProcessState") == "RUNNING"),
        activePid=active_pid,
        activePidAlive=active_alive,
        nextTaskId=state.get("nextTaskId"),
        nextPromptPathPresent=bool(state.get("nextPromptPath")),
        handoverRequested=state.get("handoverRequested"),
        rolloverAttemptedForTaskId=state.get("rolloverAttemptedForTaskId"),
        rolloverMaintenanceState=state.get("rolloverMaintenanceState"),
        decision=decision,
        reason=reason,
        **fields,
    )


def service_deferred_rollover_once(watcher: LocalFirstOrchestrator, endpoint: str, paused: Callable[[], bool], safe_boundary_state: str | None = None) -> bool:
    """Attempt optional rollover maintenance at the pre-dispatch boundary."""
    tracer = diagnostic_trace_for(watcher)
    if tracer:
        tracer.record("ROLLOVER", "service_deferred_rollover_once", "FUNCTION", "BEGIN", watcher.state, safeBoundaryState=safe_boundary_state)
    boundary_state = safe_boundary_state or watcher.state.get("state")
    if paused():
        _trace_rollover_gate(watcher, boundary_state, "DEFER", "DISCUSSION_PAUSED", function="service_deferred_rollover_once")
        return False
    if boundary_state != "NEXT_PROMPT_READY" or watcher.state.get("state") != boundary_state:
        _trace_rollover_gate(watcher, boundary_state, "SKIP", "NOT_NEXT_PROMPT_READY", function="service_deferred_rollover_once")
        return False
    if not watcher.state.get("rolloverDue"):
        _trace_rollover_gate(watcher, boundary_state, "NO_TRIGGER", "ROLLOVER_DUE_FALSE", function="service_deferred_rollover_once")
        return False
    watcher.retire_completed_executor_ownership()
    next_task_id = str(watcher.state.get("nextTaskId") or "")
    operator_restart_available = bool(getattr(watcher, "_operator_restart_rollover_recovery_available", False))
    restart_recovery_available = bool(
        operator_restart_available
        and (
            "rolloverAutomaticRecoveryEpochCount" not in watcher.state
            or (watcher.state.get("rolloverHandoverSendState") == "UNSENT" and not watcher.state.get("handoverRequested"))
        )
    )
    bridge = None
    def close_bridge() -> None:
        nonlocal bridge
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass
            bridge = None
    terminal_delivered_recovered = False
    prebudget_probe_performed = False
    prebudget_existing_response = None
    if (
        watcher.state.get("rolloverMaintenanceState") == "DEFERRED"
        and "rolloverAutomaticRecoveryEpochCount" not in watcher.state
    ):
        watcher.state.update({
            "rolloverRecoveryEpoch": 1,
            "rolloverAutomaticRecoveryEpochCount": 1,
            "rolloverAutomaticRecoveryMaxEpochs": ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS,
            "rolloverAutomaticRecoveryNextEligibleAt": time.time(),
        })
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_DEFERRED", watcher.state, reason="LEGACY_DEFERRED_STATE_MIGRATED", nextEligibleAt=watcher.state["rolloverAutomaticRecoveryNextEligibleAt"])
    rollover_decision = _evaluate_public_rollover_boundary(watcher, paused)
    automatic_epoch_eligible = rollover_decision.action is RolloverAction.START_RECOVERY_EPOCH
    terminal_candidate = watcher.session_rollover._terminal_same_task_transaction_eligible(
        next_task_id, restart_recovery_available or automatic_epoch_eligible
    )
    if terminal_candidate:
        transaction_id = str(watcher.state.get("rolloverTransactionId") or "").strip()
        try:
            conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
            bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
            conversation_id = canonicalize_attached_architect_conversation(watcher, bridge, conversation_id)
            expected_payload = handover_request_for_transaction(transaction_id, watcher.state.get("rolloverTransactionTaskId") or watcher.state.get("nextTaskId"))
            observer = getattr(bridge, "exact_user_message_payload_observed", None)
            delivered = bool(callable(observer) and observer(expected_payload))
            if delivered:
                terminal_delivered_recovered = watcher.session_rollover._revive_terminal_delivered_transaction(next_task_id)
            else:
                _disconnect_architect_bridge_read_only(bridge)
                bridge = None
        except Exception:
            if bridge is not None:
                _disconnect_architect_bridge_read_only(bridge)
                bridge = None
    terminal_retired = False if terminal_delivered_recovered else watcher.session_rollover._retire_terminal_same_task_transaction(
        next_task_id, restart_recovery_available
    )
    stale_retired = watcher.session_rollover._retire_stale_transaction_for_task(next_task_id)
    rollover = watcher.session_rollover
    active_pid = watcher.state.get("codexPid") or watcher.state.get("active_codex_pid")
    if (active_pid and watcher.state.get("executorProcessState") == "RUNNING"
            and LocalWatcher.process_alive(int(active_pid))):
        _trace_rollover_gate(watcher, boundary_state, "DEFER", "EXECUTOR_RUNNING", function="service_deferred_rollover_once", executorRunning=True)
        return False
    operator_restart_recovery = False
    def consume_operator_restart_recovery() -> None:
        nonlocal operator_restart_recovery
        operator_restart_recovery = True
        watcher._operator_restart_rollover_recovery_available = False
        watcher.state.update({
            "rolloverRecoveryState": "PENDING",
            "rolloverRecoveryAttemptCount": 0,
            "rolloverAutomaticRecoveryEpochCount": int(watcher.state.get("rolloverAutomaticRecoveryEpochCount", 0) or 0) or 1,
            "rolloverAutomaticRecoveryMaxEpochs": int(watcher.state.get("rolloverAutomaticRecoveryMaxEpochs", ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS) or ROLLOVER_AUTOMATIC_RECOVERY_MAX_EPOCHS),
            "rolloverRecoveryEpoch": int(watcher.state.get("rolloverRecoveryEpoch", 0) or 0) or 1,
        })
        for key in ("rolloverRecoveryStartedAt", "rolloverRecoveryLastAttemptAt", "rolloverRecoveryRetryAfter", "rolloverRecoveryTerminalReason"):
            watcher.state.pop(key, None)
        watcher.save()
        _trace_rollover_gate(watcher, boundary_state, "ATTEMPT", "OPERATOR_RESTART_RECOVERY", function="service_deferred_rollover_once")

    if terminal_delivered_recovered and restart_recovery_available:
        consume_operator_restart_recovery()
    elif watcher.state.get("rolloverMaintenanceState") == "DEFERRED" and watcher.state.get("rolloverDeferredForTaskId") == next_task_id:
        if automatic_epoch_eligible:
            watcher._operator_restart_rollover_recovery_available = False
            if watcher.state.get("rolloverHandoverSendState") != "UNSENT" or watcher.state.get("handoverRequested"):
                if bridge is None:
                    try:
                        conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                        bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                    except Exception:
                        bridge = None
                if bridge is None:
                    runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_COOLDOWN", watcher.state, reason="ARCHITECT_ATTACH_UNAVAILABLE", nextEligibleAt=watcher.state.get("rolloverAutomaticRecoveryNextEligibleAt"))
                    return False
                try:
                    if bool(getattr(bridge, "generation_visible", lambda: False)()):
                        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_COOLDOWN", watcher.state, reason="ARCHITECT_GENERATING", nextEligibleAt=watcher.state.get("rolloverAutomaticRecoveryNextEligibleAt"))
                        close_bridge()
                        return False
                    # The normal live-transaction pre-budget probe below is
                    # the single response scan.  Promote the deferred record
                    # to that read-only path without consuming another scan.
                    watcher.state.update({
                        "rolloverInProgress": True,
                        "rolloverMaintenanceState": "RECONCILE_PENDING",
                        "rolloverRecoveryState": "RECOVERING",
                    })
                    watcher.save()
                except Exception:
                    close_bridge()
                    return False
            attempts = int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0)
            if attempts >= ROLLOVER_RECOVERY_MAX_ATTEMPTS:
                if not watcher.session_rollover._begin_automatic_recovery_epoch(next_task_id, rollover_decision):
                    close_bridge()
                    return False
                runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_EPOCH_STARTED", watcher.state, taskId=next_task_id)
            else:
                watcher.state.update({
                    "rolloverRecoveryState": "PENDING",
                    "rolloverMaintenanceState": "IN_PROGRESS",
                    "rolloverRecoveryRetryAfter": None,
                    "rolloverAutomaticRecoveryNextEligibleAt": None,
                })
                watcher.save()
                runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_ELIGIBLE", watcher.state, taskId=next_task_id, epoch=watcher.state.get("rolloverRecoveryEpoch"), sameEpoch=True)
        elif rollover_decision.action in {
            RolloverAction.WAIT_DISCUSSION,
            RolloverAction.WAIT_EXECUTOR,
            RolloverAction.WAIT_ACTIVE_WRITER,
            RolloverAction.WAIT_ARCHITECT,
            RolloverAction.WAIT_COOLDOWN,
            RolloverAction.WAIT_RECONCILIATION,
            RolloverAction.BLOCK_EXECUTOR_LAUNCH,
        } and not restart_recovery_available:
            _trace_rollover_gate(
                watcher,
                boundary_state,
                "DEFER",
                rollover_decision.reason,
                function="service_deferred_rollover_once",
            )
            return False
        elif rollover_decision.action is RolloverAction.HUMAN_REQUIRED:
            _fail_closed_public_rollover(watcher, rollover_decision)
            return False
        elif restart_recovery_available:
            consume_operator_restart_recovery()
        else:
            retry_after = float(watcher.state.get("rolloverAutomaticRecoveryNextEligibleAt", 0.0) or 0.0)
            runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_AUTO_RECOVERY_COOLDOWN", watcher.state, nextEligibleAt=retry_after, remainingSeconds=max(0.0, retry_after - time.time()))
            _trace_rollover_gate(watcher, boundary_state, "DEFER", "MAINTENANCE_DEFERRED", function="service_deferred_rollover_once")
            return False
    elif (terminal_retired or stale_retired) and restart_recovery_available:
        consume_operator_restart_recovery()
    live_transaction = (
        watcher.state.get("rolloverDue") is True
        and watcher.state.get("rolloverPending") is True
        and watcher.state.get("handoverRequested") is True
        and watcher.state.get("rolloverTransactionId")
        and str(watcher.state.get("rolloverTransactionTaskId") or "") == next_task_id
        and watcher.state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
        and watcher.state.get("rolloverMaintenanceState") in {"IN_PROGRESS", "RECONCILE_PENDING"}
        and (
            watcher.state.get("rolloverInProgress") is True
            or watcher.state.get("rolloverMaintenanceState") == "RECONCILE_PENDING"
        )
        and bool(watcher.state.get("architectConversationId"))
    )
    if live_transaction:
        if bridge is None:
            try:
                conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
            except Exception:
                bridge = None
        existing_response = rollover.persisted_validated_handover()
        response_source = "DURABLE_STATE" if existing_response else "BROWSER_HISTORY"
        persisted_payload = watcher.state.get("pending_handover")
        if isinstance(persisted_payload, str) and persisted_payload and existing_response is None:
            watcher.state.update({
                "state": "HUMAN_REQUIRED",
                "humanRequiredReason": "ROLLOVER_DURABLE_HANDOVER_IDENTITY_INVALID",
            })
            watcher.save()
            close_bridge()
            return False
        if existing_response is None and bridge is not None:
            try:
                prebudget_probe_performed = True
                existing_response = rollover._read_existing_handover_response(
                    bridge, watcher.state.get("rolloverTransactionId")
                )
            except Exception:
                existing_response = None
        prebudget_existing_response = existing_response
        exact_existing_response = bool(
            existing_response
            and rollover._handover_response_valid(existing_response, watcher.state.get("rolloverTransactionId"))
        )
        runtime_log(
            getattr(watcher, "runtime_logger", None),
            getattr(watcher, "runtime_run_id", None),
            "HANDOVER_RECONCILIATION_PRE_BUDGET_PROBE",
            watcher.state,
            transactionId=watcher.state.get("rolloverTransactionId"),
            readOnly=True,
            responseFound=bool(existing_response),
            transactionMatched=exact_existing_response,
            handoverResent=False,
            responseSource=response_source if existing_response else "NOT_FOUND",
        )
        if exact_existing_response:
            try:
                if watcher.process_pending_handover_response(bridge, existing_response):
                    rollover._record_recovery_success()
                    return True
                return False
            finally:
                close_bridge()
        if (bridge is not None
                and rollover_decision.action in {RolloverAction.START_RECOVERY_EPOCH, RolloverAction.START_RECOVERY_ATTEMPT}
                and _legacy_handover_compatibility_allowed(watcher.state)):
            task_id = str(watcher.state.get("nextTaskId") or "")
            reemission_status, reemitted_response = rollover._request_legacy_handover_reemission_once(bridge, task_id)
            if reemission_status == "VALIDATED" and reemitted_response is not None:
                try:
                    if watcher.process_pending_handover_response(bridge, reemitted_response):
                        rollover._record_recovery_success()
                        return True
                    return False
                finally:
                    close_bridge()
            if reemission_status in {"WAIT", "FAILED", "INVALID", "CONFLICT"}:
                if reemission_status in {"INVALID", "CONFLICT"}:
                    watcher.state.update({
                        "state": "HUMAN_REQUIRED",
                        "humanRequiredReason": f"LEGACY_HANDOVER_REEMISSION_{reemission_status}",
                    })
                    watcher.save()
                close_bridge()
                return False
        if (
            rollover_decision.action is RolloverAction.WAIT_RECONCILIATION
            and watcher.state.get("rolloverMaintenanceState") == "RECONCILE_PENDING"
            and watcher.state.get("rolloverHandoverRecoveryDisposition") == "RETRYABLE"
            and int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0) < ROLLOVER_RECOVERY_MAX_ATTEMPTS
        ):
            retry_after = float(watcher.state.get("rolloverRecoveryRetryAfter", 0.0) or 0.0)
            remaining = max(0.0, retry_after - time.time())
            runtime_log(
                getattr(watcher, "runtime_logger", None),
                getattr(watcher, "runtime_run_id", None),
                "HANDOVER_RECONCILIATION_WAIT",
                watcher.state,
                recoveryDisposition=ROLLOVER_RECOVERY_WAIT_BACKOFF,
                attempts=int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0),
                retryAfter=retry_after,
                remainingBackoffSeconds=remaining,
                handoverResent=False,
            )
            _trace_rollover_gate(watcher, boundary_state, "DEFER", rollover_decision.reason, function="service_deferred_rollover_once")
            close_bridge()
            return False
    reconciliation_live_before_recovery = rollover.handover_reconciliation_pending()
    recovery_disposition = rollover._begin_bounded_recovery()
    if recovery_disposition == ROLLOVER_RECOVERY_WAIT_BACKOFF:
        retry_after = float(watcher.state.get("rolloverRecoveryRetryAfter", 0.0) or 0.0)
        remaining = max(0.0, retry_after - time.time())
        runtime_log(
            getattr(watcher, "runtime_logger", None),
            getattr(watcher, "runtime_run_id", None),
            "HANDOVER_RECONCILIATION_WAIT",
            watcher.state,
            recoveryDisposition=ROLLOVER_RECOVERY_WAIT_BACKOFF,
            attempts=int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0),
            retryAfter=retry_after,
            remainingBackoffSeconds=remaining,
            handoverResent=False,
        )
        _trace_rollover_gate(watcher, boundary_state, "DEFER", "ROLLOVER_RECONCILIATION_BACKOFF", function="service_deferred_rollover_once")
        close_bridge()
        return False
    if recovery_disposition == ROLLOVER_RECOVERY_EXHAUSTED:
        if reconciliation_live_before_recovery:
            started = float(watcher.state.get("rolloverRecoveryStartedAt", 0.0) or 0.0)
            runtime_log(
                getattr(watcher, "runtime_logger", None),
                getattr(watcher, "runtime_run_id", None),
                "HANDOVER_RECONCILIATION_EXHAUSTED",
                watcher.state,
                recoveryDisposition=ROLLOVER_RECOVERY_EXHAUSTED,
                attempts=int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0),
                elapsedSeconds=max(0.0, time.time() - started) if started else None,
                reason=watcher.state.get("rolloverRecoveryTerminalReason") or "BOUNDED_RECOVERY_EXHAUSTED",
            )
        if tracer:
            tracer.record("ROLLOVER", "service_deferred_rollover_once", "GATE", "DECISION", watcher.state, gate="bounded_recovery", result="BLOCK", reason="backoff_or_terminal")
        _trace_rollover_gate(watcher, boundary_state, "DEFER", "MAINTENANCE_DEFERRED", function="service_deferred_rollover_once")
        close_bridge()
        return False
    _trace_rollover_gate(watcher, boundary_state, "ATTEMPT", "ROLLOVER_DUE_SAFE_BOUNDARY", function="service_deferred_rollover_once")
    def failed() -> bool:
        rollover._record_recovery_failure()
        return False
    def reconcile_pending(bridge: Any, use_prebudget_result: bool = False) -> bool:
        """Reconcile one existing transaction without ever resending it."""
        attempt = int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0)
        transaction_id = watcher.state.get("rolloverTransactionId")
        if watcher.state.get("rolloverHandoverSendState") == "PENDING":
            observer = getattr(bridge, "exact_user_message_payload_observed", None)
            if callable(observer) and transaction_id:
                try:
                    payload = handover_request_for_transaction(transaction_id, watcher.state.get("rolloverTransactionTaskId") or watcher.state.get("nextTaskId"))
                    delivery_observed = bool(observer(payload))
                except Exception:
                    delivery_observed = False
                if delivery_observed:
                    watcher.state.update({
                        "rolloverHandoverSendState": "ACKNOWLEDGED",
                        "rolloverMaintenanceState": "RECONCILE_PENDING",
                        "rolloverInProgress": True,
                        "rolloverDue": True,
                        "rolloverPending": True,
                        "handoverRequested": True,
                        "handoverReady": False,
                    })
                    watcher.save()
                    runtime_log(
                        getattr(watcher, "runtime_logger", None),
                        getattr(watcher, "runtime_run_id", None),
                        "ARCHITECT_HANDOVER_DELIVERY_RECONCILED",
                        watcher.state,
                        transactionId=transaction_id,
                        transactionTaskId=watcher.state.get("rolloverTransactionTaskId"),
                        payloadSha256=hashlib.sha256(payload.encode()).hexdigest(),
                        deliveryObserved=True,
                        originalErrorCode="DURABLE_PENDING_AFTER_RESTART",
                        originalErrorClass="PROCESS_RESTART",
                        originalErrorMessage="delivery checked from durable PENDING state",
                        underlyingExceptionClass=None,
                        underlyingExceptionMessage=None,
                        sendActionAttempted=False,
                        sendActionAcknowledged=True,
                    )
        runtime_log(
            getattr(watcher, "runtime_logger", None),
            getattr(watcher, "runtime_run_id", None),
            "HANDOVER_RECONCILIATION_ATTEMPT",
            watcher.state,
            attempt=attempt,
            transactionId=transaction_id,
            readOnly=True,
            handoverResent=False,
        )
        observed_handover = prebudget_existing_response if use_prebudget_result else _HANDOVER_RESPONSE_UNSET
        result = rollover.reconcile_pending_handover(bridge, observed_handover)
        disposition = "SUCCESS" if result else watcher.state.get("rolloverHandoverRecoveryDisposition")
        if disposition not in {"RETRYABLE", "HUMAN_REQUIRED"}:
            disposition = "RETRYABLE" if rollover.handover_reconciliation_pending() else "EXHAUSTED"
        runtime_log(
            getattr(watcher, "runtime_logger", None),
            getattr(watcher, "runtime_run_id", None),
            "HANDOVER_RECONCILIATION_RESULT",
            watcher.state,
            found=bool(watcher.state.get("pending_handover") or watcher.state.get("handoverReady")),
            transactionMatched=bool(result or watcher.state.get("handoverReady")),
            disposition=disposition,
            handoverResent=False,
        )
        return result
    try:
        if bridge is None:
            conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
            bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
            conversation_id = canonicalize_attached_architect_conversation(watcher, bridge, conversation_id)
        else:
            conversation_id = str(watcher.state.get("architectConversationId") or "")
        snapshot = getattr(bridge, "assistant_fast_snapshot", None) or getattr(bridge, "assistant_baseline")
        baseline = snapshot()
        task_id = str(watcher.state.get("nextTaskId") or watcher.state.get("taskId") or "")
        legacy_task = str(watcher.state.get("taskId") or "")
        if (watcher.state.get("rolloverPending") and task_id
                and watcher.state.get("rolloverAttemptedForTaskId") in {task_id, legacy_task}):
            if reconcile_pending(bridge, prebudget_probe_performed):
                rollover._record_recovery_success()
                return True
            if (watcher.state.get("handoverRequested")
                    or watcher.state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}):
                if rollover.handover_reconciliation_pending():
                    return False
                return failed()
        send_state = watcher.state.get("rolloverHandoverSendState")
        recovery_evidence = any(
            watcher.state.get(key)
            for key in (
                "pending_handover", "rolloverFreshCandidateConversationId",
                "rolloverFreshCandidateState", "rolloverFreshCandidateDiscoveryState",
                "rolloverFreshCandidateAttemptCount", "rolloverFreshCandidateRetryAfter",
                "rolloverFreshBootstrapPayloadHash", "rolloverFreshPageCreated",
            )
        )
        allow_same_task_unsent_recovery = bool(
            (operator_restart_recovery or (
                automatic_epoch_eligible
                and watcher.state.get("rolloverHandoverSendState") == "UNSENT"
                and not watcher.state.get("handoverRequested")
            ))
            and send_state == "UNSENT"
            and not watcher.state.get("handoverRequested")
            and not recovery_evidence
        )
        if not rollover.request_if_due(
            bridge,
            latest_prompt_dispatched=True,
            executor_running=False,
            architect_generating=bridge.generation_visible(),
            safe_boundary_state=boundary_state,
            allow_same_task_unsent_recovery=allow_same_task_unsent_recovery,
        ):
            if rollover.handover_reconciliation_pending():
                runtime_log(
                    getattr(watcher, "runtime_logger", None),
                    getattr(watcher, "runtime_run_id", None),
                    "AMBIGUOUS_HANDOVER_RECONCILIATION_PENDING",
                    watcher.state,
                    transactionId=watcher.state.get("rolloverTransactionId"),
                    taskId=watcher.state.get("rolloverTransactionTaskId"),
                    handoverResent=False,
                    maintenanceState=watcher.state.get("rolloverMaintenanceState"),
                )
                if reconcile_pending(bridge, prebudget_probe_performed):
                    rollover._record_recovery_success()
                    return True
                # The send may have succeeded even though its acknowledgement
                # timed out.  A not-yet-visible response is retryable; it is
                # not a terminal maintenance failure and must not enter the
                # passive DEFERRED state here.
                if rollover.handover_reconciliation_pending():
                    return False
            return failed()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_WAIT_BEGIN", watcher.state,
                    conversationId=conversation_id, generationVisible="NOT_SAMPLED_AT_LOG_POINT", pollIntervalSeconds=0.5,
                    memorySampleAgeMs=None, memorySampleAvailable=bool(watcher.state.get("architectMemoryBytes") is not None),
                    rolloverDue=bool(watcher.state.get("rolloverDue")), discussionPauseActive=bool(watcher.state.get("discussionPauseActive")),
                    waitReason="ROLLOVER_HANDOVER_RESPONSE")
        observed = bridge.wait_for_new_response(baseline, poll_interval=0.5)
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ARCHITECT_WAIT_END", watcher.state,
                    conversationId=conversation_id, completionState=observed.get("state"), generationVisible="NOT_SAMPLED_AT_LOG_POINT",
                    rolloverDue=bool(watcher.state.get("rolloverDue")), discussionPauseActive=bool(watcher.state.get("discussionPauseActive")),
                    waitReason="ROLLOVER_HANDOVER_RESPONSE")
        if observed.get("state") != "COMPLETED" or not watcher.process_pending_handover_response(bridge, observed.get("text", "")):
            if watcher.state.get("rolloverInProgress"):
                watcher.defer_failed_rollover("ARCHITECT_HANDOVER_RESPONSE_INVALID")
            return failed()
        rollover._record_recovery_success()
        return True
    except Exception as error:
        if watcher.state.get("rolloverInProgress"):
            watcher.defer_failed_rollover(type(error).__name__)
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "ROLLOVER_MAINTENANCE_FAILED", watcher.state, errorClass=type(error).__name__)
        return failed()
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass


def sample_completed_architect_response_memory(watcher: Any, bridge: Any) -> str | None:
    """Record current-session memory after Architect completion, before acceptance."""
    rollover = getattr(watcher, "session_rollover", None)
    reader = getattr(bridge, "current_session_memory_bytes", None)
    if rollover is None or not callable(reader):
        return None
    return rollover.sample_memory(memory_reader=reader)


def dispatch_next_prompt_once(watcher: LocalFirstOrchestrator, launch: Callable[[str, Path], Any], endpoint: str, paused: Callable[[], bool], logger: logging.Logger | None = None, run_id: str | None = None) -> Any:
    """Dispatch project work only after any required Architect rollover completes."""
    tracer = diagnostic_trace_for(watcher)
    if tracer:
        tracer.record("EXECUTOR", "dispatch_next_prompt_once", "EXECUTOR_DISPATCH_GATE", "BEGIN", watcher.state, taskId=watcher.state.get("taskId"), nextTaskId=watcher.state.get("nextTaskId"), promptPath=watcher.state.get("nextPromptPath"), rolloverDue=watcher.state.get("rolloverDue"), discussionPause=bool(watcher.state.get("discussionPauseActive")), rolloverServiceCalled=False)
    if watcher.state.get("state") != "NEXT_PROMPT_READY":
        runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state, taskId=watcher.state.get("taskId"), nextTaskId=watcher.state.get("nextTaskId"),
                    rolloverDue=bool(watcher.state.get("rolloverDue")), rolloverPending=bool(watcher.state.get("rolloverPending")),
                    rolloverInProgress=bool(watcher.state.get("rolloverInProgress")), handoverRequested=bool(watcher.state.get("handoverRequested")),
                    discussionPauseActive=bool(paused()), promptPresent=bool(watcher.state.get("nextPromptPath")), gateResult="BLOCK", reason="NOT_NEXT_PROMPT_READY")
        if tracer:
            tracer.record("EXECUTOR", "dispatch_next_prompt_once", "EXECUTOR_DISPATCH_GATE", "DECISION", watcher.state, decision="BLOCK", reason="NOT_NEXT_PROMPT_READY", rolloverServiceCalled=False)
        return None
    if (watcher.state.get("postDiscussionEnvelopeRequired") is True
            and not _legacy_handover_must_precede_post_discussion_gate(watcher)):
        disposition = service_post_discussion_protocol_gate_once(watcher, endpoint, paused, logger, run_id)
        _wait_after_post_discussion_protocol_gate(watcher, disposition)
        return None
    decision = _evaluate_public_rollover_boundary(watcher, paused)
    if decision.action in {
        RolloverAction.WAIT_DISCUSSION,
        RolloverAction.WAIT_EXECUTOR,
        RolloverAction.WAIT_ACTIVE_WRITER,
        RolloverAction.WAIT_ARCHITECT,
        RolloverAction.WAIT_COOLDOWN,
        RolloverAction.WAIT_RECONCILIATION,
        RolloverAction.BLOCK_EXECUTOR_LAUNCH,
    }:
        runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state,
                    gateResult="BLOCK", reason=f"ROLLOVER_EVALUATOR_{decision.reason}",
                    evaluatorAction=decision.action.value, rolloverServiceCalled=False)
        _execute_public_rollover_decision_wait(watcher, decision, logger, run_id)
        return None
    if decision.action is RolloverAction.HUMAN_REQUIRED:
        runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state,
                    gateResult="BLOCK", reason=f"ROLLOVER_EVALUATOR_{decision.reason}",
                    evaluatorAction=decision.action.value, rolloverServiceCalled=False)
        _fail_closed_public_rollover(watcher, decision)
        return None
    if decision.action is RolloverAction.NORMALIZE_STATE:
        runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state,
                    gateResult="BLOCK", reason="ROLLOVER_EVALUATOR_NORMALIZE_STATE",
                    evaluatorAction=decision.action.value, rolloverServiceCalled=False)
        if decision.normalization == "SET_PENDING_ONLY_AFTER_SAFE_BOUNDARY_VALIDATION":
            watcher.state["rolloverPending"] = True
            watcher.save()
        return None
    watcher.retire_completed_executor_ownership()
    active_pid = watcher.state.get("codexPid") or watcher.state.get("active_codex_pid")
    if (watcher.state.get("executorProcessState") == "RUNNING"
            and active_pid and LocalWatcher.process_alive(int(active_pid))):
        runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state, taskId=watcher.state.get("taskId"), nextTaskId=watcher.state.get("nextTaskId"),
                    rolloverDue=bool(watcher.state.get("rolloverDue")), rolloverPending=bool(watcher.state.get("rolloverPending")),
                    rolloverInProgress=bool(watcher.state.get("rolloverInProgress")), handoverRequested=bool(watcher.state.get("handoverRequested")),
                    discussionPauseActive=bool(paused()), promptPresent=bool(watcher.state.get("nextPromptPath")), gateResult="BLOCK", reason="EXECUTOR_RUNNING")
        if tracer:
            tracer.record("EXECUTOR", "dispatch_next_prompt_once", "EXECUTOR_DISPATCH_GATE", "DECISION", watcher.state, decision="BLOCK", reason="EXECUTOR_RUNNING", activePid=active_pid, rolloverServiceCalled=False)
        return None
    rollover_called = False
    rollover_result = None
    rollover_complete = not bool(watcher.state.get("rolloverDue"))
    if watcher.state.get("rolloverDue"):
        previous_authority = watcher.state.get("architectConversationId")
        rollover_called = True
        rollover_result = service_deferred_rollover_once(watcher, endpoint, paused, "NEXT_PROMPT_READY")
        rollover_complete = bool(
            rollover_result
            and not watcher.state.get("rolloverDue")
            and not watcher.state.get("rolloverPending")
            and not watcher.state.get("rolloverInProgress")
            and not watcher.state.get("handoverRequested")
            and bool(watcher.state.get("architectConversationId"))
            and (not previous_authority or watcher.state.get("architectConversationId") != previous_authority)
        )
        if not rollover_complete:
            runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state, taskId=watcher.state.get("taskId"), nextTaskId=watcher.state.get("nextTaskId"),
                        rolloverDue=True, rolloverPending=bool(watcher.state.get("rolloverPending")), rolloverInProgress=bool(watcher.state.get("rolloverInProgress")),
                        handoverRequested=bool(watcher.state.get("handoverRequested")), discussionPauseActive=bool(paused()),
                        promptPresent=bool(watcher.state.get("nextPromptPath")), gateResult="BLOCK", reason="ROLLOVER_REQUIRED_BEFORE_DISPATCH",
                        rolloverServiceResult=rollover_result)
            if tracer:
                tracer.record("EXECUTOR", "dispatch_next_prompt_once", "EXECUTOR_DISPATCH_GATE", "DECISION", watcher.state, decision="BLOCK", reason="ROLLOVER_REQUIRED_BEFORE_DISPATCH", rolloverServiceCalled=True, rolloverServiceResult=rollover_result, rolloverComplete=False, authoritativeArchitectConversationId=watcher.state.get("architectConversationId"))
            runtime_log(logger, run_id, "NEXT_PROMPT_READY_DISPATCH_BLOCKED", watcher.state, reason="ROLLOVER_REQUIRED_BEFORE_DISPATCH", rolloverServiceResult=rollover_result)
            return None
    if watcher.state.get("postDiscussionEnvelopeRequired") is True:
        disposition = service_post_discussion_protocol_gate_once(watcher, endpoint, paused, logger, run_id)
        _wait_after_post_discussion_protocol_gate(watcher, disposition)
        return None
    if tracer:
        tracer.record("EXECUTOR", "dispatch_next_prompt_once", "EXECUTOR_DISPATCH_GATE", "DECISION", watcher.state, decision="LAUNCH", reason="ROLLOVER_COMPLETE" if rollover_called else "ROLLOVER_NOT_DUE", rolloverServiceCalled=rollover_called, rolloverServiceResult=rollover_result, rolloverComplete=rollover_complete, authoritativeArchitectConversationId=watcher.state.get("architectConversationId"), discussionPaused=bool(paused()))
    runtime_log(logger, run_id, "NEXT_PROMPT_READY", watcher.state)
    runtime_log(logger, run_id, "CODEX_DISPATCH_GATE", watcher.state, taskId=watcher.state.get("taskId"), nextTaskId=watcher.state.get("nextTaskId"),
                rolloverDue=bool(watcher.state.get("rolloverDue")), rolloverPending=bool(watcher.state.get("rolloverPending")),
                rolloverInProgress=bool(watcher.state.get("rolloverInProgress")), handoverRequested=bool(watcher.state.get("handoverRequested")),
                discussionPauseActive=bool(paused()), promptPresent=bool(watcher.state.get("nextPromptPath")), gateResult="ALLOW", reason="ROLLOVER_COMPLETE" if rollover_called else "ROLLOVER_NOT_DUE")
    runtime_log(logger, run_id, "CODEX_STARTING", watcher.state, attempt=int(watcher.state.get("executorAttemptNumber", 0)) + 1)
    process = watcher.launch_next(launch)
    if tracer:
        tracer.record("EXECUTOR", "dispatch_next_prompt_once", "CODEX_LAUNCH_END", "END", watcher.state, pid=getattr(process, "pid", None), decision="LAUNCH")
    print(f"CODEX_STARTED taskId={watcher.state.get('taskId') or watcher.state.get('nextTaskId')} pid={getattr(process, 'pid', 'UNKNOWN')}")
    return process


def deferred_rollover_passive_wait_required(watcher: LocalFirstOrchestrator) -> bool:
    """Compatibility predicate backed by the canonical evaluator."""
    decision = _evaluate_public_rollover_boundary(watcher, lambda: bool(watcher.state.get("discussionPauseActive")))
    return decision.action is RolloverAction.WAIT_COOLDOWN


def reconciliation_backoff_wait_required(watcher: LocalFirstOrchestrator) -> bool:
    """Compatibility predicate backed by the canonical evaluator."""
    decision = _evaluate_public_rollover_boundary(watcher, lambda: bool(watcher.state.get("discussionPauseActive")))
    return decision.action is RolloverAction.WAIT_RECONCILIATION


def passive_deferred_rollover_wait(
    watcher: LocalFirstOrchestrator,
    logger: logging.Logger | None = None,
    run_id: str | None = None,
    poll_interval: float | None = None,
) -> bool:
    """Sleep/reload once after deferred rollover or reconciliation backoff."""
    deferred_wait = deferred_rollover_passive_wait_required(watcher)
    backoff_wait = reconciliation_backoff_wait_required(watcher)
    if not deferred_wait and not backoff_wait:
        return False
    task_id = watcher.state.get("nextTaskId")
    interval = poll_interval if poll_interval is not None else float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0"))
    interval = max(0.25, interval)
    if backoff_wait:
        retry_after = float(watcher.state.get("rolloverRecoveryRetryAfter", 0.0) or 0.0)
        remaining = max(0.0, retry_after - time.time())
        wait_seconds = max(0.25, min(interval, remaining))
        wait_marker = (task_id, retry_after)
        if getattr(watcher, "_rollover_backoff_wait_logged", None) != wait_marker:
            watcher._rollover_backoff_wait_logged = wait_marker
            runtime_log(
                logger,
                run_id,
                "HANDOVER_RECONCILIATION_WAIT",
                watcher.state,
                recoveryDisposition=ROLLOVER_RECOVERY_WAIT_BACKOFF,
                attempts=int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0),
                retryAfter=retry_after,
                remainingBackoffSeconds=remaining,
                handoverResent=False,
            )
            tracer = diagnostic_trace_for(watcher)
            if tracer:
                tracer.record(
                    "ROLLOVER",
                    "passive_deferred_rollover_wait",
                    "HANDOVER_RECONCILIATION_WAIT",
                    "DECISION",
                    watcher.state,
                    recoveryDisposition=ROLLOVER_RECOVERY_WAIT_BACKOFF,
                    attempts=int(watcher.state.get("rolloverRecoveryAttemptCount", 0) or 0),
                    retryAfter=retry_after,
                    remainingBackoffSeconds=remaining,
                    handoverResent=False,
                )
    else:
        next_epoch = float(watcher.state.get("rolloverAutomaticRecoveryNextEligibleAt", 0.0) or 0.0)
        remaining = max(0.0, next_epoch - time.time()) if next_epoch else None
        wait_seconds = max(0.25, min(interval, remaining)) if remaining is not None and remaining > 0 else interval
        reason = "DISCUSSION_PAUSED" if watcher.state.get("discussionPauseActive") else "ROLLOVER_MAINTENANCE_DEFERRED"
        signature = (task_id, reason, next_epoch, remaining is not None and remaining <= 0)
        now = time.monotonic()
        last = getattr(watcher, "_rollover_passive_status_log", None)
        if last is None or last[:3] != signature[:3] or now - last[3] >= 10.0:
            watcher._rollover_passive_status_log = (*signature[:3], now)
            runtime_log(logger, run_id, "ROLLOVER_DEFERRED_WAIT" if reason == "DISCUSSION_PAUSED" else "ROLLOVER_AUTO_RECOVERY_COOLDOWN", watcher.state,
                        nextEligibleAt=next_epoch, remainingSeconds=remaining,
                        noManualRestartRequired=True, reason=reason, waitSeconds=wait_seconds)
    if deferred_wait and getattr(watcher, "_rollover_passive_wait_logged_task", None) != task_id:
        watcher._rollover_passive_wait_logged_task = task_id
        runtime_log(
            logger,
            run_id,
            "ROLLOVER_DEFERRED_PASSIVE_WAIT",
            watcher.state,
            reason="DISCUSSION_PAUSED" if watcher.state.get("discussionPauseActive") else "ROLLOVER_MAINTENANCE_DEFERRED",
            nextTaskId=task_id,
        )
        tracer = diagnostic_trace_for(watcher)
        if tracer:
            tracer.record(
                "ROLLOVER",
                "passive_deferred_rollover_wait",
                "ROLLOVER_DEFERRED_PASSIVE_WAIT",
                "DECISION",
                watcher.state,
                decision="WAIT",
                reason="ROLLOVER_MAINTENANCE_DEFERRED",
                nextTaskId=task_id,
            )
    time.sleep(wait_seconds)
    watcher.state = watcher._load_state()
    return True


def build_rollover_observations(
    watcher: LocalFirstOrchestrator,
    paused: Callable[[], bool],
    safe_boundary_state: str | None = None,
) -> RolloverObservations:
    """Collect facts for the pure evaluator without deciding an action."""
    state = watcher.state
    active_pid = state.get("codexPid") or state.get("active_codex_pid")
    process_alive = False
    if active_pid and state.get("executorProcessState") == "RUNNING":
        try:
            process_alive = bool(LocalWatcher.process_alive(int(active_pid)))
        except (TypeError, ValueError):
            process_alive = False
    session_matches = (
        state.get("executorSessionId", AFFOTECH_EXECUTOR_SESSION_ID) == AFFOTECH_EXECUTOR_SESSION_ID
        and state.get("executorSessionMode", "PERSISTENT") == "PERSISTENT"
    )
    owns_boundary = process_alive and bool(
        state.get("governedExecutorActiveWriter") is True
        or state.get("executorActiveWriter") is True
        or session_matches
    )
    return RolloverObservations(
        discussion_paused=bool(paused()),
        safe_boundary_state=safe_boundary_state or state.get("state"),
        next_task_id=state.get("nextTaskId"),
        executor_process_alive=process_alive,
        executor_owns_current_boundary=owns_boundary,
        governed_executor_active_writer=bool(
            state.get("governedExecutorActiveWriter") is True
            or state.get("executorActiveWriter") is True
        ),
        architect_generating=bool(state.get("architectGenerating") is True),
        current_prompt_available=bool(state.get("nextPromptPath")),
    )


def _evaluate_public_rollover_boundary(
    watcher: LocalFirstOrchestrator,
    paused: Callable[[], bool],
    now: float | None = None,
) -> RolloverDecision:
    return evaluate_rollover_state(
        watcher.state,
        build_rollover_observations(watcher, paused, "NEXT_PROMPT_READY"),
        time.time() if now is None else now,
    )


def _execute_public_rollover_decision_wait(
    watcher: LocalFirstOrchestrator,
    decision: RolloverDecision,
    logger: logging.Logger | None,
    run_id: str | None,
) -> None:
    interval = max(0.25, float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
    if decision.action in {RolloverAction.WAIT_COOLDOWN, RolloverAction.WAIT_RECONCILIATION} and decision.wait_until is not None:
        wait_seconds = max(0.25, min(interval, max(0.0, decision.wait_until - time.time())))
    else:
        wait_seconds = interval
    runtime_log(logger, run_id, "ROLLOVER_EVALUATOR_WAIT", watcher.state, action=decision.action.value, reason=decision.reason, waitSeconds=wait_seconds)
    time.sleep(wait_seconds)
    watcher.state = watcher._load_state()


def _fail_closed_public_rollover(watcher: LocalFirstOrchestrator, decision: RolloverDecision) -> None:
    watcher.state.update({
        "state": "HUMAN_REQUIRED",
        "humanRequiredReason": f"ROLLOVER_EVALUATOR_{decision.reason}",
    })
    watcher.save()
    runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "HUMAN_REQUIRED", watcher.state, reason=f"ROLLOVER_EVALUATOR_{decision.reason}")


def _legacy_handover_must_precede_post_discussion_gate(watcher: LocalFirstOrchestrator) -> bool:
    """Allow only exact preserved legacy evidence to reach reconciliation first."""
    state = watcher.state
    task_id = str(state.get("nextTaskId") or "")
    return bool(
        state.get("postDiscussionEnvelopeRequired") is True
        and state.get("state") == "NEXT_PROMPT_READY"
        and state.get("rolloverDue") is True
        and state.get("rolloverPending") is True
        and type(state.get("rolloverInProgress")) is bool
        and state.get("handoverRequested") is True
        and state.get("rolloverHandoverSendState") in {"PENDING", "ACKNOWLEDGED", "AMBIGUOUS"}
        and state.get("rolloverMaintenanceState") in {"DEFERRED", "IN_PROGRESS", "RECONCILE_PENDING"}
        and task_id
        and str(state.get("rolloverTransactionTaskId") or "") == task_id
        and str(state.get("postDiscussionProtocolTaskId") or "") == task_id
        and str(state.get("postDiscussionProtocolTransactionId") or "") == str(state.get("rolloverTransactionId") or "")
        and str(state.get("postDiscussionProtocolRolloverCommittedTransactionId") or "") != str(state.get("rolloverTransactionId") or "")
        and _legacy_handover_compatibility_allowed(state)
        and watcher._exact_staged_prompt_recovery_task() == task_id
    )


def service_post_discussion_protocol_gate_once(
    watcher: LocalFirstOrchestrator,
    endpoint: str,
    paused: Callable[[], bool],
    logger: logging.Logger | None = None,
    run_id: str | None = None,
) -> str:
    """Service one existing post-discussion protocol observation before scheduling."""
    if watcher.state.get("postDiscussionEnvelopeRequired") is not True:
        return "CLEAR"
    task_id = watcher.state.get("postDiscussionProtocolTaskId") or watcher.state.get("nextTaskId")
    runtime_log(logger, run_id, "POST_DISCUSSION_PROTOCOL_GATE", watcher.state, taskId=task_id, decision="BLOCK_SCHEDULING")
    if paused():
        runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason="DISCUSSION_PAUSED")
        return "WAIT"
    if watcher.state.get("postDiscussionProtocolTransactionId") and not watcher._post_discussion_staged_prompt_evidence_valid(str(task_id or "")):
        watcher.state.update({
            "state": "HUMAN_REQUIRED",
            "humanRequiredReason": "ARCHITECT_PROTOCOL_ENVELOPE_REPAIR_FAILED",
            "postDiscussionProtocolFailure": "ARCHITECT_STAGED_PROMPT_EVIDENCE_INVALID",
        })
        watcher.save()
        runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_FAILED", watcher.state, taskId=task_id, reason="STAGED_PROMPT_EVIDENCE_INVALID")
        return "FAILED"
    bridge = None
    try:
        conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
        bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
        if callable(getattr(bridge, "generation_visible", None)) and bridge.generation_visible():
            runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason="ARCHITECT_GENERATING")
            return "GENERATING"
        if (watcher.state.get("postDiscussionProtocolRolloverCommittedTransactionId")
                and not watcher.state.get("postDiscussionEnvelopeRepairAttempted")):
            if watcher.request_post_discussion_envelope_repair(bridge):
                return "REPAIR_REQUESTED"
            return "FAILED"
        baseline = watcher.state.get("postDiscussionProtocolBaseline") or watcher.state.get("architectDiscussionBaseline") or {}
        waiter = getattr(bridge, "wait_for_new_response", None)
        if not callable(waiter):
            runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason="STABLE_RESPONSE_OBSERVER_UNAVAILABLE")
            return "WAIT"
        observed = waiter(baseline, poll_interval=0.5)
        if not isinstance(observed, dict) or observed.get("state") != "COMPLETED":
            runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason="NO_COMPLETED_RESPONSE")
            return "WAIT"
        if paused():
            runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason="DISCUSSION_PAUSED")
            return "WAIT"
        disposition = watcher.reconcile_post_discussion_response(bridge, str(observed.get("text") or ""), completed=True)
        if disposition in {"WAIT", "GENERATING"}:
            runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason=disposition)
        elif disposition == "FAILED":
            runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_FAILED", watcher.state, taskId=task_id)
        return disposition
    except Exception as error:
        runtime_log(logger, run_id, "ARCHITECT_ENVELOPE_REPAIR_WAIT", watcher.state, taskId=task_id, reason="OBSERVATION_UNAVAILABLE", errorClass=type(error).__name__)
        return "WAIT"
    finally:
        if bridge is not None:
            try:
                bridge.close()
            except Exception:
                pass


def _wait_after_post_discussion_protocol_gate(watcher: LocalFirstOrchestrator, disposition: str) -> None:
    if disposition not in {"WAIT", "GENERATING"}:
        return
    time.sleep(max(0.25, float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0"))))
    watcher.state = watcher._load_state()


def run_next_prompt_ready_once(
    watcher: LocalFirstOrchestrator,
    launch: Callable[[str, Path], Any],
    endpoint: str,
    paused: Callable[[], bool],
    logger: logging.Logger | None = None,
    run_id: str | None = None,
) -> Any:
    """Run one NEXT_PROMPT_READY iteration through the canonical evaluator."""
    if (watcher.state.get("postDiscussionEnvelopeRequired") is True
            and not _legacy_handover_must_precede_post_discussion_gate(watcher)):
        disposition = service_post_discussion_protocol_gate_once(watcher, endpoint, paused, logger, run_id)
        _wait_after_post_discussion_protocol_gate(watcher, disposition)
        return None
    decision = _evaluate_public_rollover_boundary(watcher, paused)
    if decision.action in {
        RolloverAction.WAIT_DISCUSSION,
        RolloverAction.WAIT_EXECUTOR,
        RolloverAction.WAIT_ACTIVE_WRITER,
        RolloverAction.WAIT_ARCHITECT,
        RolloverAction.WAIT_COOLDOWN,
        RolloverAction.WAIT_RECONCILIATION,
        RolloverAction.BLOCK_EXECUTOR_LAUNCH,
    }:
        _execute_public_rollover_decision_wait(watcher, decision, logger, run_id)
        return None
    if decision.action is RolloverAction.HUMAN_REQUIRED:
        _fail_closed_public_rollover(watcher, decision)
        return None
    if decision.action is RolloverAction.NORMALIZE_STATE:
        if decision.normalization == "SET_PENDING_ONLY_AFTER_SAFE_BOUNDARY_VALIDATION":
            watcher.state["rolloverPending"] = True
            watcher.save()
        return None
    process = dispatch_next_prompt_once(watcher, launch, endpoint, paused, logger, run_id)
    return process


def handle_architect_value_error(watcher: LocalFirstOrchestrator, bridge: Any) -> bool:
    """Apply task-scoped format recovery from the resident main-loop path."""
    try:
        watcher.request_format_recovery(bridge)
    except ResultSubmissionError as error:
        watcher.state.update({"state": "HUMAN_REQUIRED", "humanRequiredReason": "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED", "architectSendError": getattr(error, "code", type(error).__name__)})
        watcher.save()
        runtime_log(getattr(watcher, "runtime_logger", None), getattr(watcher, "runtime_run_id", None), "HUMAN_REQUIRED", watcher.state, reason="ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED")
        return False
    return watcher.state.get("state") == "ARCHITECT_RUNNING"


def run_human_required_startup_once(watcher: LocalFirstOrchestrator, launch: Callable[[str, Path], Any]) -> str:
    """Run the production HUMAN_REQUIRED entry, including stale-format recovery."""
    if watcher.recover_stale_format_human_required():
        return watcher.state.get("state", "HUMAN_REQUIRED")
    return run_executor_state_once(watcher, launch)


def passive_human_required_wait(watcher: LocalFirstOrchestrator, poll_interval: float | None = None) -> str:
    """Wait once without changing HUMAN_REQUIRED workflow authority."""
    interval = poll_interval if poll_interval is not None else float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0"))
    time.sleep(interval)
    watcher.state = watcher._load_state()
    return watcher.state.get("state", "HUMAN_REQUIRED")


def human_required_startup_recovery_due(
    watcher: LocalFirstOrchestrator,
    reason: str,
    attempted_reason: Any,
) -> bool:
    """Allow one explicit current-task postlaunch authorization after restart."""
    if attempted_reason != reason:
        return True
    task_id = str(watcher.state.get("taskId") or "")
    return bool(
        task_id
        and reason == "EXECUTOR_EXITED_WITHOUT_RESULT"
        and watcher.state.get("executorLaunchState") == "POSTLAUNCH_NO_RESULT"
        and os.environ.get("ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY") == task_id
    )


def passive_architect_memory_sample_for_pause(
    watcher: LocalFirstOrchestrator,
    endpoint: str,
    existing_bridge: ArchitectPlaywright | None = None,
) -> bool:
    """Sample the current Architect renderer without allowing workflow actions.

    A bound reader is reused.  When none is bound, this makes one temporary
    read-only attachment to the persisted Architect conversation and closes
    only that bridge after the sample.  It deliberately does not canonicalize,
    navigate, send, create a page, or change Architect authority.
    """
    rollover = getattr(watcher, "session_rollover", None)
    if rollover is None:
        return False
    if time.monotonic() < rollover._next_memory_sample_at:
        rollover.sample_memory_for_loop()
        return False

    bridge = existing_bridge
    temporary_bridge = False
    reader = getattr(watcher, "architect_memory_reader", None)
    reader_owner_thread_id = getattr(watcher, "architect_memory_reader_thread_id", None)
    current_thread_id = threading.get_ident()
    if callable(reader) and reader_owner_thread_id and reader_owner_thread_id != current_thread_id:
        runtime_log(
            getattr(watcher, "runtime_logger", None),
            getattr(watcher, "runtime_run_id", None),
            "ARCHITECT_MEMORY_SAMPLE_SKIPPED",
            watcher.state,
            skipReason="MEMORY_READER_THREAD_MISMATCH",
            readerOwnerThreadId=reader_owner_thread_id,
            currentThreadId=current_thread_id,
            workflowMutation=False,
        )
        reader = None
    try:
        if not callable(reader) and bridge is None:
            conversation_id = (
                watcher.state.get("architectConversationId")
                or os.environ.get("ARCHITECT_CONVERSATION_ID")
                or VERIFIED_ARCHITECT_CONVERSATION_ID
            )
            runtime_log(
                getattr(watcher, "runtime_logger", None),
                getattr(watcher, "runtime_run_id", None),
                "ARCHITECT_MEMORY_PASSIVE_ATTACH_BEGIN",
                watcher.state,
                conversationId=conversation_id,
                readOnly=True,
                workflowMutation=False,
            )
            bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
            temporary_bridge = True
            runtime_log(
                getattr(watcher, "runtime_logger", None),
                getattr(watcher, "runtime_run_id", None),
                "ARCHITECT_MEMORY_PASSIVE_ATTACH_END",
                watcher.state,
                conversationId=conversation_id,
                readOnly=True,
                workflowMutation=False,
            )
        if not callable(reader) and bridge is not None:
            reader = getattr(bridge, "current_session_memory_bytes", None)
        if not callable(reader):
            rollover.sample_memory_for_loop()
            return False
        rollover.sample_memory_for_loop(reader)
        return True
    except Exception as error:
        runtime_log(
            getattr(watcher, "runtime_logger", None),
            getattr(watcher, "runtime_run_id", None),
            "ARCHITECT_MEMORY_SAMPLE_FAILED",
            watcher.state,
            error=f"PASSIVE_{type(error).__name__.upper()}",
            readOnly=True,
            workflowMutation=False,
        )
        return False
    finally:
        if temporary_bridge and bridge is not None:
            cleanup_error = _disconnect_architect_bridge_read_only(
                bridge,
                diagnostic_trace_for(watcher),
                watcher.state,
            )
            if cleanup_error is not None:
                runtime_log(
                    getattr(watcher, "runtime_logger", None),
                    getattr(watcher, "runtime_run_id", None),
                    "ARCHITECT_MEMORY_SAMPLE_FAILED",
                    watcher.state,
                    error=f"PASSIVE_CLOSE_{type(cleanup_error).__name__.upper()}",
                    readOnly=True,
                    workflowMutation=False,
                )


def main() -> None:
    global _ACTIVE_DIAGNOSTIC_TRACE
    project = os.environ.get("AFFOTECH_PROJECT_DIR", os.getcwd())
    state_dir = Path(os.environ.get("AFFOTECH_ORCHESTRATOR_STATE_DIR") or (Path(project) / ".agent-work" / "orchestrator"))
    instance_lock = WatcherInstanceLock(state_dir)
    try:
        instance_lock.acquire()
    except RuntimeError as error:
        print(str(error))
        return
    logger = None
    run_id = None
    try:
        logger, run_id, log_path = initialize_runtime_logging(state_dir)
    except Exception as error:
        print(f"ORCHESTRATOR_LOGGING_INIT_FAILED error={type(error).__name__}:{error}")
        instance_lock.release()
        return
    watcher = LocalFirstOrchestrator(project, state_dir)
    watcher.runtime_logger = logger
    watcher.runtime_run_id = run_id
    diagnostic_only = rollover_diagnostic_only_enabled()
    diagnostic_trace = DiagnosticTracer(state_dir, run_id) if (DiagnosticTracer.enabled() or diagnostic_only) else None
    watcher.diagnostic_trace = diagnostic_trace
    _ACTIVE_DIAGNOSTIC_TRACE = diagnostic_trace
    if diagnostic_trace:
        diagnostic_trace.record("MAIN", "main", "DIAGNOSTIC_TRACE_ENABLED", "BEGIN", watcher.state, traceDirectory=str(diagnostic_trace.root))
    endpoint = os.environ.get("ARCHITECT_CDP_ENDPOINT", "http://127.0.0.1:9333")
    recovery_state = watcher.state.get("state", "IDLE")
    recovery_action = {
        "EXECUTOR_RUNNING": "WAIT_EXISTING_EXECUTOR",
        "RESULT_READY": "DELIVER_RESULT",
        "ARCHITECT_RUNNING": "WAIT_ARCHITECT",
        "NEXT_PROMPT_READY": "LAUNCH_EXECUTOR",
        "HUMAN_REQUIRED": "STOP_FOR_HUMAN",
        "IDLE": "WAIT_IDLE",
    }.get(recovery_state, "STOP_FOR_HUMAN")
    print("WATCHER_STARTED state=%s taskId=%s nextTaskId=%s lastCompletedTaskId=%s codexPid=%s architectSendState=%s architectConversationId=%s recoveryAction=%s" % (
        recovery_state, watcher.state.get("taskId") or "NONE", watcher.state.get("nextTaskId") or "NONE",
        watcher.state.get("lastCompletedTaskId") or "NONE", watcher.state.get("codexPid") or "NONE",
        watcher.state.get("architectSendState") or "NONE", watcher.state.get("architectConversationId") or "NONE", recovery_action))
    runtime_log(logger, run_id, "WATCHER_STARTED", watcher.state, source=log_path)
    runtime_log(logger, run_id, "STATE_RECOVERED", watcher.state, recoveryAction=recovery_action)
    if diagnostic_only:
        result = run_rollover_diagnostic_only(watcher, endpoint, diagnostic_trace)
        if diagnostic_trace:
            diagnostic_trace.record("MAIN", "main", "DIAGNOSTIC_ONLY_COMPLETE", "END", watcher.state, result=result, workflowActionCount=0)
            diagnostic_trace.shutdown(watcher.state)
            _ACTIVE_DIAGNOSTIC_TRACE = None
        instance_lock.release()
        return
    hotkeys = DiscussionHotkeyController(watcher)
    if not hotkeys.start(lambda event: runtime_log(logger, run_id, event, watcher.state)) and os.name == "nt":
        print("ORCHESTRATOR_HOTKEY_REGISTRATION_FAILED")
        if diagnostic_trace:
            diagnostic_trace.shutdown(watcher.state)
            _ACTIVE_DIAGNOSTIC_TRACE = None
        instance_lock.release()
        return
    discussion_paused = getattr(watcher, "discussion_pause_active", lambda: bool(watcher.state.get("discussionPauseActive")))
    if discussion_paused():
        print(f"ORCHESTRATOR PAUSED BY HUMAN state={watcher.state.get('state')} taskId={watcher.state.get('taskId') or watcher.state.get('nextTaskId') or 'NONE'}")
        runtime_log(logger, run_id, "HUMAN_DISCUSSION_PAUSE_ACTIVE", watcher.state)
    launch = visible_executor_launcher(project, watcher)
    remote_monitor = None
    if hasattr(watcher, "discussion_pause_active"):
        remote_monitor = RemoteDiscussionControlMonitor(
            watcher,
            lambda: ArchitectPlaywright.attach(
                endpoint,
                watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID,
            ),
        )
        if not remote_monitor.start():
            print("REMOTE CONTROL UNAVAILABLE; F9/F10 remain available")
    idle_bridge = None
    human_wait_bridge = None
    last_logged_state = recovery_state
    stop_logged = False
    pause_notice = None
    try:
        while True:
            rollover = getattr(watcher, "session_rollover", None)
            if diagnostic_trace:
                diagnostic_trace.record("MAIN", "main", "LOOP_BEGIN", "BEGIN", watcher.state, discussionPauseActive=bool(discussion_paused()), rolloverRecoveryState=watcher.state.get("rolloverRecoveryState"))
            state = watcher.state.get("state", "IDLE")
            if state != "HUMAN_REQUIRED" and human_wait_bridge is not None:
                try:
                    human_wait_bridge.close()
                except Exception:
                    pass
                human_wait_bridge = None
            if state != last_logged_state:
                runtime_log(logger, run_id, "STATE_TRANSITION", watcher.state, **{"from": last_logged_state, "to": state, "reason": watcher.state.get("humanRequiredReason")})
                if state == "HUMAN_REQUIRED":
                    print(f"STATE=HUMAN_REQUIRED reason={watcher.state.get('humanRequiredReason') or 'UNSPECIFIED'}")
                last_logged_state = state
            if discussion_paused() and state in {"IDLE", "RESULT_READY", "NEXT_PROMPT_READY"}:
                log_main_loop_decision(watcher, "DISCUSSION_PAUSE_SKIP", "discussion_pause_active", False, True)
                memory_monitoring_observed = passive_architect_memory_sample_for_pause(
                    watcher, endpoint, existing_bridge=idle_bridge
                )
                runtime_log(logger, run_id, "DISCUSSION_PAUSE_DECISION", watcher.state, taskId=watcher.state.get("taskId"),
                            discussionPauseActive=True, workflowActionsBlocked=True, memoryMonitoringObserved=memory_monitoring_observed,
                            rolloverDue=bool(watcher.state.get("rolloverDue")), decision="BLOCK", reason="DISCUSSION_PAUSED")
                if diagnostic_trace:
                    diagnostic_trace.record("MAIN", "main", "LOOP_DECISION", "DECISION", watcher.state, decision="DISCUSSION_PAUSE_SKIP", reason="discussion_pause_active")
                notice = (state, watcher.state.get("taskId") or watcher.state.get("nextTaskId"))
                if notice != pause_notice:
                    print(f"ORCHESTRATOR PAUSED BY HUMAN state={state} taskId={notice[1] or 'NONE'} architectRelay=BLOCKED F10=RESUME")
                    pause_notice = notice
                time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                watcher.state = watcher._load_state()
                continue
            pause_notice = None
            if state != "IDLE" and idle_bridge is not None:
                idle_bridge.close()
                idle_bridge = None
            if state == "IDLE":
                log_main_loop_decision(watcher, "IDLE_WAIT", "idle_poll", False, False)
                if diagnostic_trace:
                    diagnostic_trace.record("MAIN", "main", "LOOP_DECISION", "DECISION", watcher.state, decision="IDLE_WAIT")
                if watcher.intake_inbox(launch):
                    continue
                if idle_bridge is None:
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                    try:
                        idle_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        conversation_id = canonicalize_attached_architect_conversation(watcher, idle_bridge, conversation_id)
                        if rollover is not None:
                            reader = getattr(idle_bridge, "current_session_memory_bytes", None)
                            if callable(reader):
                                rollover.sample_memory(memory_reader=reader)
                    except Exception as error:
                        if idle_bridge is not None:
                            idle_bridge.close()
                            idle_bridge = None
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                        if not isinstance(error, ResultSubmissionError) and type(error).__name__ != "TimeoutError" and not str(error).startswith("ARCHITECT_"):
                            raise
                        time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                        watcher.state = watcher._load_state()
                        continue
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                recover_false = getattr(watcher, "recover_false_result_reconciliation", None)
                if callable(recover_false) and recover_false(idle_bridge):
                    idle_bridge.close()
                    idle_bridge = None
                    continue
                try:
                    watcher.inspect_idle_architect(idle_bridge, launch)
                except Exception as error:
                    if idle_bridge is not None:
                        idle_bridge.close()
                    idle_bridge = None
                    bootstrap_transport = (
                        watcher.state.get("state") == "IDLE"
                        and watcher.state.get("architectBootstrapDeliveryState") in {"UNSENT", "AMBIGUOUS", "PENDING"}
                        and (isinstance(error, ResultSubmissionError) or type(error).__name__ == "TimeoutError")
                    )
                    if not bootstrap_transport:
                        raise
                    runtime_log(logger, run_id, "ARCHITECT_IDLE_BOOTSTRAP_TRANSPORT_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error))
                    time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                    watcher.state = watcher._load_state()
                    continue
                if watcher.state.get("state") == "IDLE":
                    time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                    watcher.state = watcher._load_state()
                continue
            if state == "EXECUTOR_RUNNING":
                log_main_loop_decision(watcher, "EXECUTOR_RUNNING_WAIT", "executor_active", False, True)
                if diagnostic_trace:
                    diagnostic_trace.record("MAIN", "main", "LOOP_DECISION", "DECISION", watcher.state, decision="EXECUTOR_RUNNING_WAIT")
                state = run_executor_state_once(watcher, launch)
                if state == "EXECUTOR_RUNNING":
                    continue
                if state == "STOP":
                    if watcher.state.get("state") == "HUMAN_REQUIRED":
                        continue
                    return
                continue
            if state == "NEXT_PROMPT_READY":
                log_main_loop_decision(watcher, "NEXT_PROMPT_READY_DISPATCH", "safe_boundary_evaluation", True, False)
                if diagnostic_trace:
                    diagnostic_trace.record("MAIN", "main", "LOOP_DECISION", "DECISION", watcher.state, decision="NEXT_PROMPT_READY_DISPATCH_ATTEMPT")
                process = run_next_prompt_ready_once(watcher, launch, endpoint, discussion_paused, logger, run_id)
                continue
            if state == "HUMAN_REQUIRED":
                log_main_loop_decision(watcher, "HUMAN_REQUIRED_WAIT", str(watcher.state.get("humanRequiredReason") or "UNSPECIFIED"), False, True)
                if diagnostic_trace:
                    diagnostic_trace.record("MAIN", "main", "LOOP_DECISION", "DECISION", watcher.state, decision="HUMAN_REQUIRED_PASSIVE_WAIT")
                if watcher.state.get("humanRequiredReason") == "ARCHITECT_DECISION_HUMAN_REQUIRED":
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    if human_wait_bridge is None:
                        try:
                            human_wait_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                            conversation_id = canonicalize_attached_architect_conversation(watcher, human_wait_bridge, conversation_id)
                            if rollover is not None:
                                reader = getattr(human_wait_bridge, "current_session_memory_bytes", None)
                                if callable(reader):
                                    rollover.sample_memory(memory_reader=reader)
                            runtime_log(logger, run_id, "ARCHITECT_HUMAN_WAIT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        except Exception as error:
                            if human_wait_bridge is not None:
                                try:
                                    human_wait_bridge.close()
                                except Exception:
                                    pass
                                human_wait_bridge = None
                            runtime_log(logger, run_id, "ARCHITECT_HUMAN_WAIT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                            time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                            watcher.state = watcher._load_state()
                            continue
                    baseline = watcher.state.get("architectBaseline")
                    if not isinstance(baseline, dict):
                        baseline = human_wait_bridge.assistant_baseline()
                    try:
                        runtime_log(logger, run_id, "ARCHITECT_WAIT_BEGIN", watcher.state, conversationId=conversation_id,
                                    generationVisible="NOT_SAMPLED_AT_LOG_POINT", pollIntervalSeconds=1.0,
                                    memorySampleAgeMs=None, memorySampleAvailable=bool(watcher.state.get("architectMemoryBytes") is not None),
                                    rolloverDue=bool(watcher.state.get("rolloverDue")), discussionPauseActive=True, waitReason="HUMAN_DECISION")
                        observed = human_wait_bridge.wait_for_new_response(baseline, poll_interval=1.0)
                        runtime_log(logger, run_id, "ARCHITECT_WAIT_END", watcher.state, conversationId=conversation_id,
                                    completionState=observed.get("state"), generationVisible="NOT_SAMPLED_AT_LOG_POINT",
                                    rolloverDue=bool(watcher.state.get("rolloverDue")), discussionPauseActive=True, waitReason="HUMAN_DECISION")
                    except Exception as error:
                        try:
                            human_wait_bridge.close()
                        except Exception:
                            pass
                        human_wait_bridge = None
                        runtime_log(logger, run_id, "ARCHITECT_HUMAN_WAIT_READ_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error))
                        time.sleep(float(os.environ.get("ORCHESTRATOR_POLL_INTERVAL", "2.0")))
                        watcher.state = watcher._load_state()
                        continue
                    response = str(observed.get("text") or "")
                    disposition = resident_human_decision_response(
                        watcher, response, human_wait_bridge.assistant_baseline(), bridge=human_wait_bridge
                    )
                    if disposition in {"EXECUTE", "STOP", "REPAIR_REQUESTED", "FAILED"}:
                        try:
                            human_wait_bridge.close()
                        except Exception:
                            pass
                        human_wait_bridge = None
                    continue
                reason = str(watcher.state.get("humanRequiredReason") or "UNSPECIFIED")
                attempted_reason = watcher.state.get("humanRequiredRecoveryAttemptedReason")
                recovery_attempt_due = human_required_startup_recovery_due(watcher, reason, attempted_reason)
                if recovery_attempt_due:
                    if watcher.recover_legacy_rollover_cutout():
                        continue
                    if watcher.recover_completed_confirmed_workflow():
                        continue
                    if watcher.recover_preempted_rollover_failure():
                        continue
                if recovery_attempt_due and watcher.state.get("humanRequiredReason") == "ARCHITECT_FORMAT_RECOVERY_TRANSPORT_FAILED":
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                    recovery_bridge = None
                    try:
                        recovery_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        try:
                            recovered = run_owned_recovery_bridge(recovery_bridge, watcher.recover_pending_rollover_handover)
                        except Exception as error:
                            runtime_log(logger, run_id, "ARCHITECT_PENDING_ROLLOVER_RECOVERY_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error))
                            recovered = False
                        if recovered:
                            continue
                    except Exception as error:
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                if recovery_attempt_due and watcher.state.get("humanRequiredReason") == "ARCHITECT_RESULT_TRANSPORT_EXHAUSTED":
                    conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                    runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                    recovery_bridge = None
                    try:
                        recovery_bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        if run_owned_recovery_bridge(recovery_bridge, watcher.reconcile_exhausted_result_delivery):
                            continue
                    except Exception as error:
                        if recovery_bridge is None:
                            runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                        else:
                            runtime_log(logger, run_id, "ARCHITECT_RESULT_DELIVERY_RECOVERY_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                if recovery_attempt_due:
                    state = run_human_required_startup_once(watcher, launch)
                    watcher.state["humanRequiredRecoveryAttemptedReason"] = reason
                    if watcher.state.get("state") == "HUMAN_REQUIRED":
                        watcher.save()
                    if state in {"EXECUTOR_RUNNING", "RESULT_READY", "ARCHITECT_RUNNING", "NEXT_PROMPT_READY"}:
                        continue
                passive_human_required_wait(watcher)
                continue
            if state not in {"RESULT_READY", "ARCHITECT_RUNNING"}:
                print(f"STATE={state}")
                return

            conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
            log_main_loop_decision(watcher, "RESULT_DELIVERY" if state == "RESULT_READY" else "ARCHITECT_WAIT", "architect_response_or_delivery", False, False)
            if diagnostic_trace:
                diagnostic_trace.record("MAIN", "main", "LOOP_DECISION", "DECISION", watcher.state, decision="RESULT_DELIVERY" if state == "RESULT_READY" else "ARCHITECT_WAIT")
            runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
            bridge = None
            try:
                legacy_recovery_used = False
                try:
                    bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                except Exception as attach_error:
                    if (
                        watcher.state.get("state") == "RESULT_READY"
                        and str(attach_error) == "ARCHITECT_CURRENT_CONVERSATION_NOT_FOUND"
                    ):
                        bridge = attach_legacy_provisional_architect(endpoint, conversation_id, watcher)
                        legacy_recovery_used = True
                    else:
                        raise
                if legacy_recovery_used:
                    conversation_id = watcher.state.get("architectConversationId") or conversation_id
                conversation_id = canonicalize_attached_architect_conversation(watcher, bridge, conversation_id)
            except Exception as error:
                if bridge is not None:
                    bridge.close()
                runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                raise
            runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
            if rollover is not None:
                reader = getattr(bridge, "current_session_memory_bytes", None)
                if callable(reader):
                    rollover.sample_memory(memory_reader=reader)
            if bridge.generation_visible():
                runtime_log(logger, run_id, "ARCHITECT_GENERATION_STARTED", watcher.state, conversationId=conversation_id)
            watcher.state["architectConversationId"] = conversation_id
            watcher.save()
            try:
                if (watcher.state.get("state") == "RESULT_READY" and not discussion_paused()
                        and (not watcher.state.get("handoverRequested", False) or not watcher.state.get("rolloverInProgress", False))):
                    bridge = watcher.deliver_result_with_recovery(
                        lambda: ArchitectPlaywright.attach(endpoint, conversation_id),
                        initial_bridge=bridge,
                    )
                    if bridge is None:
                        print(f"STATE=HUMAN_REQUIRED reason={watcher.state.get('humanRequiredReason', 'ARCHITECT_RESULT_TRANSPORT_EXHAUSTED')}")
                        continue
                baseline = watcher.state.get("architectBaseline")
                if not isinstance(baseline, dict):
                    baseline = bridge.assistant_fast_snapshot()
                while True:
                    try:
                        runtime_log(logger, run_id, "ARCHITECT_WAIT_BEGIN", watcher.state, conversationId=conversation_id,
                                    generationVisible="NOT_SAMPLED_AT_LOG_POINT", pollIntervalSeconds=5.0,
                                    memorySampleAgeMs=None, memorySampleAvailable=bool(watcher.state.get("architectMemoryBytes") is not None),
                                    rolloverDue=bool(watcher.state.get("rolloverDue")), discussionPauseActive=bool(discussion_paused()), waitReason="RESULT_OR_ARCHITECT_RESPONSE")
                        observed = bridge.wait_for_new_response(baseline, poll_interval=5.0)
                        runtime_log(logger, run_id, "ARCHITECT_WAIT_END", watcher.state, conversationId=conversation_id,
                                    completionState=observed.get("state"), generationVisible="NOT_SAMPLED_AT_LOG_POINT",
                                    rolloverDue=bool(watcher.state.get("rolloverDue")), discussionPauseActive=bool(discussion_paused()), waitReason="RESULT_OR_ARCHITECT_RESPONSE")
                        runtime_log(logger, run_id, "ARCHITECT_GENERATION_FINISHED", watcher.state, conversationId=conversation_id)
                    except Exception as error:
                        watcher.state["state"] = "ARCHITECT_RUNNING"
                        watcher.save()
                        runtime_log(logger, run_id, "ARCHITECT_WAIT_INTERRUPTED", watcher.state, errorClass=type(error).__name__, errorMessage=str(error), conversationId=conversation_id)
                        if bridge is not None:
                            bridge.close()
                        conversation_id = watcher.state.get("architectConversationId") or os.environ.get("ARCHITECT_CONVERSATION_ID") or VERIFIED_ARCHITECT_CONVERSATION_ID
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_START", watcher.state, conversationId=conversation_id)
                        try:
                            bridge = ArchitectPlaywright.attach(endpoint, conversation_id)
                        except Exception as attach_error:
                            runtime_log(logger, run_id, "ARCHITECT_ATTACH_FAILED", watcher.state, errorClass=type(attach_error).__name__, errorMessage=str(attach_error), conversationId=conversation_id)
                            raise
                        runtime_log(logger, run_id, "ARCHITECT_ATTACH_SUCCESS", watcher.state, conversationId=conversation_id)
                        time.sleep(1.0)
                        continue
                    if observed.get("state") == "COMPLETED" and rollover is not None:
                        sample_completed_architect_response_memory(watcher, bridge)
                    if watcher.state.get("postDiscussionEnvelopeRequired"):
                        disposition = watcher.reconcile_post_discussion_response(
                            bridge, observed["text"], completed=observed.get("state") == "COMPLETED"
                        )
                        baseline = watcher.state.get("architectBaseline") or bridge.assistant_baseline()
                        if disposition in {"REPAIR_REQUESTED", "FAILED", "EXECUTE", "STOP", "HUMAN_REQUIRED"} and watcher.state.get("state") != "ARCHITECT_RUNNING":
                            break
                        continue
                    if watcher.state.get("handoverRequested") and watcher.state.get("rolloverInProgress") and rollover is not None:
                        if watcher.process_pending_handover_response(bridge, observed["text"]):
                            baseline = bridge.assistant_baseline()
                            if watcher.state.get("state") == "RESULT_READY":
                                bridge = watcher.deliver_result_with_recovery(
                                    lambda: ArchitectPlaywright.attach(endpoint, watcher.state.get("architectConversationId") or conversation_id),
                                    initial_bridge=bridge,
                                )
                                if bridge is None:
                                    print(f"STATE=HUMAN_REQUIRED reason={watcher.state.get('humanRequiredReason', 'ARCHITECT_RESULT_TRANSPORT_EXHAUSTED')}")
                                    break
                                baseline = watcher.state.get("architectBaseline") or bridge.assistant_baseline()
                            continue
                        if not watcher.state.get("handoverRequested"):
                            baseline = bridge.assistant_baseline()
                            continue
                        watcher.reject_invalid_handover_response()
                        print("STATE=HUMAN_REQUIRED reason=ARCHITECT_HANDOVER_RESPONSE_INVALID")
                        break
                    try:
                        if watcher.state.get("architectBootstrapAwaiting"):
                            decision = watcher.consume_idle_architect_response(observed["text"], launch)
                            watcher.state.update({"architectBootstrapAwaiting": False, "architectBootstrapDeliveryState": None, "architectBootstrapPayload": None, "architectBootstrapPayloadHash": None, "architectBootstrapRetryAfter": None, "architectBootstrapObservationAttempts": 0})
                            watcher.save()
                        else:
                            decision = watcher.accept_architect_response(observed["text"])
                    except ValueError:
                        if not handle_architect_value_error(watcher, bridge):
                            print(f"STATE={watcher.state['state']}")
                            break
                        baseline = watcher.state.get("architectBaseline")
                        continue
                    if decision == "EXECUTE":
                        break
                    if not isinstance(decision, dict) or decision.get("action") != "EXECUTE":
                        if watcher.state.get("state") in {"HUMAN_REQUIRED", "IDLE"}:
                            if watcher.state.get("state") == "HUMAN_REQUIRED":
                                watcher.state["architectBaseline"] = bridge.assistant_baseline()
                                watcher.save()
                            break
                        print(f"STATE={watcher.state['state']}")
                        return
                    break
            finally:
                if bridge is not None:
                    bridge.close()
    except KeyboardInterrupt:
        print("STATE=STOPPED")
        runtime_log(logger, run_id, "WATCHER_STOPPED", watcher.state, reason="keyboard_interrupt")
        stop_logged = True
    except Exception as error:
        if logger is not None:
            logger.exception("unhandled production main-loop exception", extra={"runId": run_id or "UNKNOWN", "state": watcher.state.get("state", "UNKNOWN"), "taskId": watcher.state.get("taskId") or watcher.state.get("nextTaskId") or "NONE", "event": "WATCHER_EXCEPTION", "errorClass": type(error).__name__, "errorMessage": str(error)})
        raise
    finally:
        if remote_monitor is not None:
            remote_monitor.stop()
        hotkeys.stop()
        if idle_bridge is not None:
            idle_bridge.close()
        instance_lock.release()
        if not stop_logged and logger is not None and 'watcher' in locals():
            runtime_log(logger, run_id, "WATCHER_STOPPED", watcher.state, reason="shutdown")
        if diagnostic_trace:
            diagnostic_trace.record("MAIN", "main", "SHUTDOWN_BEGIN", "BEGIN", watcher.state if 'watcher' in locals() else None, activePlaywrightConnectionIds=list(diagnostic_trace._connections), remoteMonitorActive=bool(remote_monitor and remote_monitor.active), hotkeyThreadActive=bool(hotkeys and hotkeys._thread and hotkeys._thread.is_alive()) if 'hotkeys' in locals() else False)
            diagnostic_trace.shutdown(watcher.state if 'watcher' in locals() else None)
            _ACTIVE_DIAGNOSTIC_TRACE = None


if __name__ == "__main__":
    main()
