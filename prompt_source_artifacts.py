"""Immutable, unbound Architect prompt-source ingress artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^prompt-source-([0-9a-f]{64})$")
_ARTIFACT_STATE = "VERIFIED_SOURCE"


def _fail(reason: str) -> None:
    raise RuntimeError(reason)


def _atomic_no_clobber(path: Path, data: bytes, conflict_code: str = "PROMPT_SOURCE_ARTIFACT_CONFLICT") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temp.read_bytes() != data:
            _fail("PROMPT_SOURCE_WRITE_FAILED")
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != data:
                _fail(conflict_code)
        except OSError as error:
            if path.exists():
                if path.read_bytes() == data:
                    return
                _fail(conflict_code)
            raise RuntimeError("PROMPT_SOURCE_WRITE_FAILED") from error
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _root(repository_root: str | os.PathLike[str]) -> Path:
    repo = Path(repository_root).resolve()
    work = (repo / ".agent-work").resolve()
    root = work / "prompt-ingress"
    if root.parent != work or not root.is_relative_to(work):
        _fail("PROMPT_SOURCE_PATH_INVALID")
    return root


def ingest_verified_prompt_source(
    repository_root: str | os.PathLike[str],
    *,
    expected_prompt_source_artifact_id: str,
    expected_sha256: str,
    expected_byte_length: int,
    prompt_bytes: bytes | None = None,
    carrier_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Verify and immutably materialize exact UTF-8 bytes into prompt ingress."""
    if (prompt_bytes is None) == (carrier_path is None):
        _fail("PROMPT_SOURCE_INPUT_INVALID")
    if not isinstance(expected_sha256, str) or not _SHA_RE.fullmatch(expected_sha256):
        _fail("PROMPT_SOURCE_HASH_INVALID")
    if isinstance(expected_byte_length, bool) or not isinstance(expected_byte_length, int) or expected_byte_length < 0:
        _fail("PROMPT_SOURCE_LENGTH_INVALID")
    match = _ID_RE.fullmatch(expected_prompt_source_artifact_id or "")
    if not match:
        _fail("PROMPT_SOURCE_ID_INVALID")
    try:
        data = prompt_bytes if prompt_bytes is not None else Path(carrier_path).read_bytes()
    except OSError as error:
        raise RuntimeError("PROMPT_SOURCE_UNAVAILABLE") from error
    if not isinstance(data, bytes):
        _fail("PROMPT_SOURCE_INPUT_INVALID")
    if not data:
        _fail("PROMPT_SOURCE_EMPTY")
    if expected_byte_length == 0:
        _fail("PROMPT_SOURCE_LENGTH_MISMATCH")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("PROMPT_SOURCE_UTF8_INVALID") from error

    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        _fail("PROMPT_SOURCE_HASH_MISMATCH")
    if len(data) != expected_byte_length:
        _fail("PROMPT_SOURCE_LENGTH_MISMATCH")
    if match.group(1) != digest:
        _fail("PROMPT_SOURCE_ID_MISMATCH")

    root = _root(repository_root)
    prompt_path = root / f"{digest}.md"
    manifest_path = root / f"{digest}.json"
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "promptSourceArtifactId": f"prompt-source-{digest}",
        "promptSha256": digest,
        "promptByteLength": len(data),
        "encoding": "UTF-8",
        "promptSourceArtifactPath": str(prompt_path),
        "artifactState": _ARTIFACT_STATE,
    }
    try:
        _atomic_no_clobber(prompt_path, data)
        readback = prompt_path.read_bytes()
    except RuntimeError:
        raise
    except OSError as error:
        raise RuntimeError("PROMPT_SOURCE_WRITE_FAILED") from error
    if readback != data:
        _fail("PROMPT_SOURCE_ARTIFACT_CONFLICT")
    if hashlib.sha256(readback).hexdigest() != digest or len(readback) != len(data):
        _fail("PROMPT_SOURCE_READBACK_INVALID")
    try:
        readback.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("PROMPT_SOURCE_UTF8_INVALID") from error

    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        _atomic_no_clobber(manifest_path, manifest_bytes, "PROMPT_SOURCE_MANIFEST_CONFLICT")
        persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PROMPT_SOURCE_MANIFEST_INVALID") from error
    if persisted_manifest != manifest:
        _fail("PROMPT_SOURCE_MANIFEST_CONFLICT")
    return {
        **manifest,
        "manifestPath": str(manifest_path),
        "artifactState": _ARTIFACT_STATE,
    }


def load_verified_prompt_source(
    repository_root: str | os.PathLike[str],
    prompt_source_artifact_id: str,
    prompt_sha256: str,
    prompt_byte_length: int,
) -> bytes:
    """Load one source artifact by its declared identity and reverify it."""
    match = _ID_RE.fullmatch(prompt_source_artifact_id or "")
    if not match or not isinstance(prompt_sha256, str) or not _SHA_RE.fullmatch(prompt_sha256):
        _fail("PROMPT_SOURCE_ID_INVALID")
    if match.group(1) != prompt_sha256:
        _fail("PROMPT_SOURCE_ID_MISMATCH")
    if isinstance(prompt_byte_length, bool) or not isinstance(prompt_byte_length, int) or prompt_byte_length <= 0:
        _fail("PROMPT_SOURCE_LENGTH_INVALID")
    root = _root(repository_root)
    artifact_path = root / f"{prompt_sha256}.md"
    manifest_path = root / f"{prompt_sha256}.json"
    try:
        resolved_root = root.resolve()
        if artifact_path.resolve().parent != resolved_root or manifest_path.resolve().parent != resolved_root:
            _fail("PROMPT_SOURCE_PATH_INVALID")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
        data = artifact_path.read_bytes()
    except RuntimeError:
        raise
    except FileNotFoundError as error:
        raise RuntimeError("PROMPT_SOURCE_UNAVAILABLE") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("PROMPT_SOURCE_MANIFEST_INVALID") from error
    expected_manifest = {
        "schemaVersion": 1,
        "promptSourceArtifactId": prompt_source_artifact_id,
        "promptSha256": prompt_sha256,
        "promptByteLength": prompt_byte_length,
        "encoding": "UTF-8",
        "promptSourceArtifactPath": str(artifact_path),
        "artifactState": _ARTIFACT_STATE,
    }
    if not isinstance(manifest, dict) or manifest != expected_manifest:
        _fail("PROMPT_SOURCE_MANIFEST_INVALID")
    if len(data) != prompt_byte_length:
        _fail("PROMPT_SOURCE_LENGTH_MISMATCH")
    if hashlib.sha256(data).hexdigest() != prompt_sha256:
        _fail("PROMPT_SOURCE_HASH_MISMATCH")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("PROMPT_SOURCE_UTF8_INVALID") from error
    return data
