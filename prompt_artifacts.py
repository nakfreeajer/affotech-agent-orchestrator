"""Immutable, content-addressed Architect prompt artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ARTIFACT_ID_PREFIX = "prompt-artifact-"


def _fail(code: str) -> None:
    raise RuntimeError(code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_id(task_id: str, transaction_id: str | None, digest: str) -> str:
    identity = f"{task_id}\0{transaction_id or ''}\0{digest}".encode("utf-8")
    return _ARTIFACT_ID_PREFIX + hashlib.sha256(identity).hexdigest()


def _safe_task_id(task_id: Any) -> str:
    value = str(task_id or "")
    if not _TASK_ID_RE.fullmatch(value) or value in {".", ".."}:
        _fail("PROMPT_ARTIFACT_TASK_MISMATCH")
    return value


def _task_root(repository_root: str | os.PathLike[str], task_id: str) -> Path:
    base = (Path(repository_root).resolve() / ".agent-work" / "prompts").resolve()
    root = (base / task_id).resolve()
    if root.parent != base:
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    return root


def _exclusive_atomic_write(path: Path, data: bytes) -> bool:
    """Atomically publish bytes without replacing an existing immutable file.

    A same-directory hard link is an atomic no-clobber publish on the local
    Windows/NTFS and POSIX filesystems used by the Orchestrator. If hard links
    are unavailable, fail closed rather than risk replacing an artifact.
    Returns False when an identical destination already existed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if temp_path.read_bytes() != data:
            _fail("PROMPT_ARTIFACT_WRITE_FAILED")
        try:
            os.link(temp_path, path)
            return True
        except FileExistsError:
            if path.read_bytes() == data:
                return False
            _fail("PROMPT_ARTIFACT_HASH_MISMATCH")
        except OSError as error:
            if path.exists():
                if path.read_bytes() == data:
                    return False
                _fail("PROMPT_ARTIFACT_HASH_MISMATCH")
            raise RuntimeError("PROMPT_ARTIFACT_WRITE_FAILED") from error
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _manifest_core_matches(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    keys = (
        "schemaVersion", "promptArtifactId", "taskId", "transactionId",
        "promptArtifactPath", "promptSha256", "promptByteLength", "promptState",
        "architectConversationId", "source", "classification", "action",
    )
    return all(existing.get(key) == expected.get(key) for key in keys)


def persist_verified_prompt_artifact(
    repository_root: str | os.PathLike[str],
    *,
    task_id: str,
    transaction_id: str | None,
    prompt: str | bytes,
    architect_conversation_id: str | None = None,
    source: str = "ARCHITECT_ORCHESTRATOR_RESULT",
    classification: str = "ACCEPTED",
    action: str = "EXECUTE",
) -> dict[str, Any]:
    """Persist exact accepted prompt bytes and return verified identity fields."""
    task_id = _safe_task_id(task_id)
    transaction_id = None if transaction_id in (None, "") else str(transaction_id)
    if isinstance(prompt, str):
        prompt_bytes = prompt.encode("utf-8")
    elif isinstance(prompt, bytes):
        prompt_bytes = prompt
    else:
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    try:
        prompt_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("PROMPT_ARTIFACT_MANIFEST_INVALID") from error
    if not prompt_bytes:
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    if not isinstance(source, str) or not source or len(source) > 128:
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    if classification not in {"ACCEPTED", "BLOCKED", "INCONCLUSIVE", "NO_NEW_REPORT"} or action != "EXECUTE":
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")

    digest = _sha256(prompt_bytes)
    byte_length = len(prompt_bytes)
    root = _task_root(repository_root, task_id)
    prompt_path = root / f"{digest}.md"
    manifest_path = root / f"{digest}.json"
    artifact_id = _artifact_id(task_id, transaction_id, digest)

    try:
        _exclusive_atomic_write(prompt_path, prompt_bytes)
        readback = prompt_path.read_bytes()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError("PROMPT_ARTIFACT_WRITE_FAILED") from error
    if len(readback) != byte_length:
        _fail("PROMPT_ARTIFACT_LENGTH_MISMATCH")
    if _sha256(readback) != digest:
        _fail("PROMPT_ARTIFACT_HASH_MISMATCH")

    created_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    expected_manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "promptArtifactId": artifact_id,
        "taskId": task_id,
        "transactionId": transaction_id,
        "promptArtifactPath": str(prompt_path),
        "promptSha256": digest,
        "promptByteLength": byte_length,
        "promptState": "STAGED",
        "createdAt": created_at,
        "architectConversationId": architect_conversation_id,
        "source": source,
        "classification": classification,
        "action": action,
    }
    try:
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or not _manifest_core_matches(existing, expected_manifest):
                _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
            manifest = existing
        else:
            manifest_bytes = (json.dumps(expected_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            _exclusive_atomic_write(manifest_path, manifest_bytes)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not _manifest_core_matches(manifest, expected_manifest):
                _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PROMPT_ARTIFACT_MANIFEST_INVALID") from error

    state_identity = {
        "promptArtifactVersion": 1,
        "promptArtifactId": manifest["promptArtifactId"],
        "promptArtifactPath": manifest["promptArtifactPath"],
        "promptArtifactManifestPath": str(manifest_path),
        "promptSha256": manifest["promptSha256"],
        "promptByteLength": manifest["promptByteLength"],
        "promptState": manifest["promptState"],
        "promptCreatedAt": manifest["createdAt"],
        "promptTaskId": manifest["taskId"],
        "promptTransactionId": manifest["transactionId"],
        "promptArchitectConversationId": manifest["architectConversationId"],
    }
    load_verified_staged_prompt(repository_root, state_identity)
    return state_identity


def load_verified_staged_prompt(
    repository_root: str | os.PathLike[str],
    state: Mapping[str, Any],
) -> bytes:
    """Load prompt bytes only when state, manifest, path, length, and hash agree."""
    if not isinstance(state, Mapping):
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    task_id = _safe_task_id(state.get("promptTaskId"))
    transaction_id = state.get("promptTransactionId")
    if transaction_id == "":
        transaction_id = None
    if state.get("promptState") != "STAGED":
        _fail("PROMPT_ARTIFACT_STATE_INVALID")
    if isinstance(state.get("promptArtifactVersion"), bool) or state.get("promptArtifactVersion") != 1:
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    digest = state.get("promptSha256")
    length = state.get("promptByteLength")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        _fail("PROMPT_ARTIFACT_LENGTH_MISMATCH")

    root = _task_root(repository_root, task_id)
    expected_prompt_path = root / f"{digest}.md"
    expected_manifest_path = root / f"{digest}.json"
    try:
        prompt_path = Path(str(state.get("promptArtifactPath") or "")).resolve()
        manifest_path = Path(str(state.get("promptArtifactManifestPath") or "")).resolve()
        if prompt_path != expected_prompt_path or manifest_path != expected_manifest_path:
            _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    except (OSError, RuntimeError, TypeError):
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    except FileNotFoundError as error:
        raise RuntimeError("PROMPT_ARTIFACT_UNAVAILABLE") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PROMPT_ARTIFACT_MANIFEST_INVALID") from error
    if (not isinstance(manifest, dict) or isinstance(manifest.get("schemaVersion"), bool)
            or manifest.get("schemaVersion") != 1):
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    if manifest.get("taskId") != task_id:
        _fail("PROMPT_ARTIFACT_TASK_MISMATCH")
    if manifest.get("transactionId") != transaction_id:
        _fail("PROMPT_ARTIFACT_TRANSACTION_MISMATCH")
    if manifest.get("promptState") != "STAGED" or state.get("promptState") != manifest.get("promptState"):
        _fail("PROMPT_ARTIFACT_STATE_INVALID")
    expected_id = _artifact_id(task_id, transaction_id, digest)
    if (manifest.get("promptArtifactId") != expected_id
            or manifest.get("promptArtifactPath") != str(prompt_path)
            or manifest.get("promptSha256") != digest
            or not isinstance(manifest.get("createdAt"), str) or not manifest.get("createdAt")
            or not isinstance(manifest.get("source"), str) or not manifest.get("source")
            or manifest.get("action") != "EXECUTE"
            or manifest.get("classification") not in {"ACCEPTED", "BLOCKED", "INCONCLUSIVE", "NO_NEW_REPORT"}):
        _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    if (manifest.get("promptByteLength") != length
            or isinstance(manifest.get("promptByteLength"), bool)
            or not isinstance(manifest.get("promptByteLength"), int)):
        _fail("PROMPT_ARTIFACT_LENGTH_MISMATCH")
    for state_key, manifest_key in (
        ("promptArtifactId", "promptArtifactId"), ("promptCreatedAt", "createdAt"),
        ("promptArchitectConversationId", "architectConversationId"),
    ):
        if state.get(state_key) != manifest.get(manifest_key):
            _fail("PROMPT_ARTIFACT_MANIFEST_INVALID")
    try:
        prompt_bytes = prompt_path.read_bytes()
    except FileNotFoundError as error:
        raise RuntimeError("PROMPT_ARTIFACT_UNAVAILABLE") from error
    except OSError as error:
        raise RuntimeError("PROMPT_ARTIFACT_UNAVAILABLE") from error
    if len(prompt_bytes) != length:
        _fail("PROMPT_ARTIFACT_LENGTH_MISMATCH")
    if _sha256(prompt_bytes) != digest:
        _fail("PROMPT_ARTIFACT_HASH_MISMATCH")
    try:
        prompt_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("PROMPT_ARTIFACT_HASH_MISMATCH") from error
    return prompt_bytes
