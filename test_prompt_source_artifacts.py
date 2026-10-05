import hashlib
import json

import pytest

from prompt_source_artifacts import ingest_verified_prompt_source, load_verified_prompt_source


def ingress(root, data, **overrides):
    digest = hashlib.sha256(data).hexdigest()
    args = {
        "expected_prompt_source_artifact_id": f"prompt-source-{digest}",
        "expected_sha256": digest,
        "expected_byte_length": len(data),
        "prompt_bytes": data,
    }
    args.update(overrides)
    return ingest_verified_prompt_source(root, **args)


@pytest.mark.parametrize("data", [
    b"ASCII prompt",
    "Unicode: café 雪\n".encode(),
    b"lf\nlines",
    b"crlf\r\nlines",
    b"trailing\n",
    b"no trailing newline",
])
def test_exact_bytes_sha_length_and_content_addressed_paths(tmp_path, data):
    result = ingress(tmp_path, data)
    path = tmp_path / ".agent-work" / "prompt-ingress" / f"{hashlib.sha256(data).hexdigest()}.md"
    assert path.read_bytes() == data
    assert result["promptSourceArtifactPath"] == str(path)
    assert result["promptSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["promptByteLength"] == len(data)
    manifest = json.loads((path.with_suffix(".json")).read_text(encoding="utf-8"))
    assert manifest["artifactState"] == "VERIFIED_SOURCE"
    assert manifest["promptSourceArtifactId"] == result["promptSourceArtifactId"]
    assert "taskId" not in manifest and "transactionId" not in manifest


def test_unicode_length_is_bytes(tmp_path):
    data = "é".encode()
    result = ingress(tmp_path, data)
    assert result["promptByteLength"] == 2


def test_line_endings_and_trailing_newline_have_distinct_identity(tmp_path):
    lf, crlf = b"a\nb", b"a\r\nb"
    assert ingress(tmp_path, lf)["promptSha256"] != ingress(tmp_path, crlf)["promptSha256"]
    assert ingress(tmp_path, b"tail")["promptSha256"] != ingress(tmp_path, b"tail\n")["promptSha256"]


@pytest.mark.parametrize(("data", "overrides", "reason"), [
    (b"text", {"expected_sha256": "0" * 64}, "PROMPT_SOURCE_HASH_MISMATCH"),
    (b"text", {"expected_byte_length": 99}, "PROMPT_SOURCE_LENGTH_MISMATCH"),
    (b"text", {"expected_prompt_source_artifact_id": "prompt-source-" + "0" * 64}, "PROMPT_SOURCE_ID_MISMATCH"),
    (b"\xff", {}, "PROMPT_SOURCE_UTF8_INVALID"),
    (b"", {}, "PROMPT_SOURCE_EMPTY"),
])
def test_invalid_ingress_fails_closed(tmp_path, data, overrides, reason):
    with pytest.raises(RuntimeError, match=reason):
        ingress(tmp_path, data, **overrides)


def test_carrier_path_is_transport_not_identity(tmp_path):
    data = b"same content"
    first = tmp_path / "download-a.bin"
    second = tmp_path / "untrusted-name.txt"
    first.write_bytes(data)
    second.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    kwargs = {"expected_prompt_source_artifact_id": f"prompt-source-{digest}",
              "expected_sha256": digest, "expected_byte_length": len(data)}
    a = ingest_verified_prompt_source(tmp_path, carrier_path=first, **kwargs)
    b = ingest_verified_prompt_source(tmp_path, carrier_path=second, **kwargs)
    assert a["promptSourceArtifactId"] == b["promptSourceArtifactId"]
    first.write_bytes(b"different")
    with pytest.raises(RuntimeError, match="PROMPT_SOURCE_HASH_MISMATCH"):
        ingest_verified_prompt_source(tmp_path, carrier_path=first, **kwargs)


def test_reingress_is_idempotent_and_does_not_rewrite_manifest(tmp_path):
    data = b"immutable"
    first = ingress(tmp_path, data)
    manifest = tmp_path / ".agent-work" / "prompt-ingress" / f"{first['promptSha256']}.json"
    before = manifest.read_bytes()
    second = ingress(tmp_path, data)
    assert first["promptSourceArtifactId"] == second["promptSourceArtifactId"]
    assert before == manifest.read_bytes()


def test_conflicting_artifact_and_manifest_fail_closed(tmp_path):
    data = b"conflict"
    result = ingress(tmp_path, data)
    artifact = tmp_path / ".agent-work" / "prompt-ingress" / f"{result['promptSha256']}.md"
    artifact.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="PROMPT_SOURCE_ARTIFACT_CONFLICT"):
        ingress(tmp_path, data)

    other = tmp_path / "other"
    result = ingress(other, data)
    manifest = other / ".agent-work" / "prompt-ingress" / f"{result['promptSha256']}.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="PROMPT_SOURCE_MANIFEST_CONFLICT"):
        ingress(other, data)


def test_state_isolation_and_no_task_binding(tmp_path):
    state = tmp_path / "state.json"
    state.write_text('{"state":"HUMAN_REQUIRED","nextTaskId":"000103"}', encoding="utf-8")
    before = state.read_bytes()
    result = ingress(tmp_path, b"source only")
    assert state.read_bytes() == before
    assert not any(key in result for key in ("taskId", "transactionId", "promptArtifactId"))
    assert not (tmp_path / ".agent-work" / "prompts").exists()


def test_verified_source_loader_rechecks_identity_manifest_and_bytes(tmp_path):
    data = "exact café\r\nbytes".encode("utf-8")
    identity = ingress(tmp_path, data)
    assert load_verified_prompt_source(tmp_path, identity["promptSourceArtifactId"],
                                       identity["promptSha256"], identity["promptByteLength"]) == data
    artifact = tmp_path / ".agent-work" / "prompt-ingress" / f"{identity['promptSha256']}.md"
    artifact.write_bytes(data[:-1])
    with pytest.raises(RuntimeError, match="PROMPT_SOURCE_LENGTH_MISMATCH|PROMPT_SOURCE_HASH_MISMATCH"):
        load_verified_prompt_source(tmp_path, identity["promptSourceArtifactId"],
                                    identity["promptSha256"], identity["promptByteLength"])


def test_verified_source_loader_rejects_manifest_identity_and_missing_artifact(tmp_path):
    data = b"source"
    identity = ingress(tmp_path, data)
    root = tmp_path / ".agent-work" / "prompt-ingress"
    manifest = root / f"{identity['promptSha256']}.json"
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["artifactState"] = "AUTHORIZED"
    manifest.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(RuntimeError, match="PROMPT_SOURCE_MANIFEST_INVALID"):
        load_verified_prompt_source(tmp_path, identity["promptSourceArtifactId"], identity["promptSha256"], len(data))
    manifest.unlink()
    with pytest.raises(RuntimeError, match="PROMPT_SOURCE_UNAVAILABLE"):
        load_verified_prompt_source(tmp_path, identity["promptSourceArtifactId"], identity["promptSha256"], len(data))
