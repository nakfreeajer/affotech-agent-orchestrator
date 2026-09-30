"""Compact, deterministic transport for verified Executor prompt artifacts."""
from __future__ import annotations

import json
from typing import Any, Mapping


ARTIFACT_STATE_KEYS = frozenset({
    "promptArtifactVersion", "promptArtifactId", "promptArtifactPath",
    "promptArtifactManifestPath", "promptSha256", "promptByteLength",
    "promptState", "promptTaskId", "promptTransactionId",
})


def has_prompt_artifact_state(state: Mapping[str, Any]) -> bool:
    """Any artifact marker opts into strict artifact mode; partial state fails closed."""
    return any(key in state for key in ARTIFACT_STATE_KEYS)


def build_executor_prompt_artifact_descriptor(state: Mapping[str, Any]) -> str:
    """Build a compact descriptor only; callers must verify the artifact first."""
    envelope = {
        "version": 1,
        "taskId": state.get("promptTaskId"),
        "transactionId": state.get("promptTransactionId"),
        "artifactId": state.get("promptArtifactId"),
        "artifactPath": state.get("promptArtifactPath"),
        "artifactSha256": state.get("promptSha256"),
        "artifactByteLength": state.get("promptByteLength"),
        "manifestPath": state.get("promptArtifactManifestPath"),
    }
    return (
        "<EXECUTOR_PROMPT_ARTIFACT>\n"
        + json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n</EXECUTOR_PROMPT_ARTIFACT>\n"
        "Read the exact artifact bytes at artifactPath. Verify taskId, SHA256, byte length, and manifest identity before execution. "
        "Execute exactly the verified artifact contents. If any check fails, stop without executing and report PROMPT_ARTIFACT_INVALID with a concise reason. "
        "Do not reconstruct the prompt from chat history, fetch a latest file, or substitute another path."
    )
