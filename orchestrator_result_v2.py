"""Isolated qualification parser for the Architect v2 control envelope.

This module deliberately has no watcher or dispatch integration. Production
acceptance continues to use the existing v1 parser directly.
"""
from __future__ import annotations

import re
from typing import Any


_CLASS = r"(?:ACCEPTED|BLOCKED|INCONCLUSIVE|NO_NEW_REPORT)"
_DOC = r"(?:NOT_REQUIRED|REQUIRED|COMPLETE)"
_DIGEST = r"[0-9a-f]{64}"
_OPEN = "<ORCHESTRATOR_RESULT>"
_CLOSE = "</ORCHESTRATOR_RESULT>"
_V2_HEADER_RE = re.compile(r"(?m)^schemaVersion=")
_V2_EXECUTE_RE = re.compile(
    rf"{re.escape(_OPEN)}\n"
    rf"schemaVersion=2\nclassification=({_CLASS})\n"
    rf"action=EXECUTE\ntaskId=([^\r\n]+)\ndocumentation=({_DOC})\n"
    rf"promptTransport=ARTIFACT_V1\n"
    rf"promptSourceArtifactId=(prompt-source-({_DIGEST}))\n"
    rf"promptSha256=({_DIGEST})\npromptByteLength=([1-9][0-9]*)\n"
    rf"{re.escape(_CLOSE)}\n?"
)
_V2_TERMINAL_RE = re.compile(
    rf"{re.escape(_OPEN)}\n"
    rf"schemaVersion=2\nclassification=({_CLASS})\n"
    rf"action=(HUMAN_REQUIRED|STOP)\ntaskId=([^\r\n]+)\ndocumentation=({_DOC})\n"
    rf"{re.escape(_CLOSE)}\n?"
)


def parse_v2_control_envelope(text: str, completed_task_id: str) -> dict[str, Any]:
    """Parse only the canonical v2 envelope, failing closed on every deviation."""
    if not isinstance(text, str) or not isinstance(completed_task_id, str):
        raise ValueError("ORCHESTRATOR_RESULT_V2_INVALID")
    if text.count(_OPEN) != 1 or text.count(_CLOSE) != 1:
        raise ValueError("ORCHESTRATOR_RESULT_V2_ENVELOPE_COUNT_INVALID")

    match = _V2_EXECUTE_RE.fullmatch(text)
    if match:
        classification, task_id, documentation, source_id, source_digest, digest, length = match.groups()
        if task_id != completed_task_id:
            raise ValueError("ORCHESTRATOR_RESULT_V2_TASK_MISMATCH")
        if source_digest != digest:
            raise ValueError("ORCHESTRATOR_RESULT_V2_SOURCE_ID_MISMATCH")
        return {
            "protocolVersion": 2,
            "classification": classification,
            "action": "EXECUTE",
            "taskId": task_id,
            "documentation": documentation,
            "promptTransport": "ARTIFACT_V1",
            "promptSourceArtifactId": source_id,
            "promptSha256": digest,
            "promptByteLength": int(length),
        }

    match = _V2_TERMINAL_RE.fullmatch(text)
    if match:
        classification, action, task_id, documentation = match.groups()
        if task_id != completed_task_id:
            raise ValueError("ORCHESTRATOR_RESULT_V2_TASK_MISMATCH")
        return {
            "protocolVersion": 2,
            "classification": classification,
            "action": action,
            "taskId": task_id,
            "documentation": documentation,
        }
    raise ValueError("ORCHESTRATOR_RESULT_V2_INVALID")


def parse_dual_protocol_control(text: str, completed_task_id: str) -> dict[str, Any]:
    """Qualification API that selects v2 explicitly and delegates v1 unchanged."""
    if not isinstance(text, str):
        raise ValueError("ORCHESTRATOR_RESULT_INVALID")
    opening = text.find(_OPEN)
    closing = text.find(_CLOSE, opening + len(_OPEN)) if opening >= 0 else -1
    header = text[opening + len(_OPEN):closing] if opening >= 0 and closing >= 0 else ""
    if _V2_HEADER_RE.search(header):
        parsed = parse_v2_control_envelope(text, completed_task_id)
        return parsed

    # Reuse the existing production parser without copying its semantics.
    from local_orchestrator_watcher import parse_orchestrator_result

    result = parse_orchestrator_result(text, completed_task_id)
    return {"protocolVersion": 1, **result}
