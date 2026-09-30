import hashlib
import json
from pathlib import Path

import pytest

import qualification_gate_runner as gates
import local_orchestrator_watcher as runtime


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: dict) -> bytes:
    raw = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def provenance_fixture(tmp_path):
    root = tmp_path / ".agent-work" / "orchestrator" / "qualification"
    g2_root = root / "qualification-g2-old-handover-test"
    g3_root = root / "qualification-g3-fresh-bootstrap-test"
    g2_root.mkdir(parents=True)
    g3_root.mkdir(parents=True)
    handover = b"G2 proven synthetic handover\n"
    handover_path = g2_root / "handover-response.txt"
    handover_path.write_bytes(handover)
    staged = b"synthetic staged prompt\n"
    staged_path = g2_root / "staged-prompt.txt"
    staged_path.write_bytes(staged)
    expected_handover = b"expected handover\n"
    expected_path = g2_root / "expected-handover.txt"
    expected_path.write_bytes(expected_handover)
    g2 = {
        "runId": g2_root.name, "gateId": "g2-old-handover", "result": "PASS",
        "taskId": "QUAL-TASK-1", "transactionId": "QUAL-TX-1",
        "oldConversationId": "old-disposable", "handoverResponsePath": str(handover_path),
        "handoverResponseSha256": digest(handover), "stagedPromptPath": str(staged_path),
        "stagedPromptSha256": digest(staged), "expectedHandoverPath": str(expected_path),
        "expectedHandoverSha256": digest(expected_handover), "ownedPages": [],
    }
    g2_path = g2_root / "qualification-evidence.json"
    g2_raw = write_json(g2_path, g2)
    bootstrap = b"synthetic bootstrap\n"
    bootstrap_path = g3_root / "fresh-bootstrap.txt"
    bootstrap_path.write_bytes(bootstrap)
    g3 = {
        "runId": g3_root.name, "gateId": "g3-fresh-bootstrap", "result": "PASS",
        "taskId": g2["taskId"], "transactionId": g2["transactionId"],
        "oldConversationId": g2["oldConversationId"], "freshConversationId": "fresh-disposable",
        "handoverResponsePath": g2["handoverResponsePath"],
        "handoverResponseSha256": g2["handoverResponseSha256"],
        "g2EvidencePath": str(g2_path), "g2EvidenceSha256": digest(g2_raw),
        "prerequisiteEvidence": [{"gateId": "g2-old-handover", "path": str(g2_path), "sha256": digest(g2_raw)}],
        "bootstrapPath": str(bootstrap_path), "bootstrapSha256": digest(bootstrap),
        "readyObserved": True, "authorityCommitted": False,
    }
    g3_path = g3_root / "qualification-evidence.json"
    write_json(g3_path, g3)
    return tmp_path, g2, g2_path, g3, g3_path


def test_g4_resolves_valid_g3_to_g2_handover_provenance(tmp_path):
    repository, g2, g2_path, g3, g3_path = provenance_fixture(tmp_path)
    resolved_g2, resolved_path, resolved_hash, bootstrap, handover = gates._resolve_g4_provenance(
        g3, g3_path, repository)
    assert resolved_g2["gateId"] == "g2-old-handover"
    assert resolved_path == g2_path
    assert resolved_hash == digest(g2_path.read_bytes())
    assert bootstrap == Path(g3["bootstrapPath"]).read_bytes()
    assert handover == Path(g2["handoverResponsePath"]).read_bytes()
    assert resolved_path.parent == Path(g2["handoverResponsePath"]).parent
    assert resolved_path.parent != g3_path.parent


@pytest.mark.parametrize("mutation, expected", [
    ("g2_evidence_sha", "QUALIFICATION_PREREQUISITE_HASH_MISMATCH"),
    ("g2_path_substitution", "QUALIFICATION_PREREQUISITE_PROVENANCE_MISMATCH"),
    ("g2_gate_id", "QUALIFICATION_PREREQUISITE_NOT_PASSED"),
    ("g2_not_pass", "QUALIFICATION_PREREQUISITE_NOT_PASSED"),
    ("artifact_escape", "QUALIFICATION_EVIDENCE_ARTIFACT_PATH_INVALID"),
    ("artifact_hash", "QUALIFICATION_EVIDENCE_ARTIFACT_HASH_MISMATCH"),
    ("g3_substitute_prerequisite", "QUALIFICATION_PREREQUISITE_HASH_MISMATCH"),
])
def test_g4_rejects_invalid_transitive_g2_provenance(tmp_path, mutation, expected):
    repository, g2, g2_path, g3, g3_path = provenance_fixture(tmp_path)
    g3 = json.loads(g3_path.read_text(encoding="utf-8"))
    g2_live = json.loads(g2_path.read_text(encoding="utf-8"))
    if mutation == "g2_evidence_sha":
        g2_live["additionalMutation"] = True
        write_json(g2_path, g2_live)
    elif mutation == "g2_path_substitution":
        other = g3_path.parent.parent / "qualification-g2-other"
        other.mkdir()
        other_path = other / "qualification-evidence.json"
        write_json(other_path, {**g2_live, "runId": other.name})
        g3["g2EvidencePath"] = str(other_path)
    elif mutation == "g2_gate_id":
        g2_live["gateId"] = "g2-other"
        write_json(g2_path, g2_live)
    elif mutation == "g2_not_pass":
        g2_live["result"] = "BLOCKED"
        write_json(g2_path, g2_live)
    elif mutation == "artifact_escape":
        outside = repository / "outside-handover.txt"
        outside.write_bytes(Path(g2_live["handoverResponsePath"]).read_bytes())
        g2_live["handoverResponsePath"] = str(outside)
        g2_live["handoverResponseSha256"] = digest(outside.read_bytes())
        write_json(g2_path, g2_live)
        g3["handoverResponsePath"] = g2_live["handoverResponsePath"]
        g3["handoverResponseSha256"] = g2_live["handoverResponseSha256"]
        mutated_g2_bytes = g2_path.read_bytes()
        g3["g2EvidenceSha256"] = digest(mutated_g2_bytes)
        g3["prerequisiteEvidence"][0]["sha256"] = digest(mutated_g2_bytes)
    elif mutation == "artifact_hash":
        Path(g2_live["handoverResponsePath"]).write_bytes(b"tampered handover")
    elif mutation == "g3_substitute_prerequisite":
        other = g3_path.parent.parent / "qualification-g2-other"
        other.mkdir()
        other_path = other / "qualification-evidence.json"
        write_json(other_path, {**g2_live, "runId": other.name})
        g3["prerequisiteEvidence"][0]["path"] = str(other_path)
        g3["g2EvidencePath"] = str(other_path)
    write_json(g3_path, g3)
    with pytest.raises(RuntimeError, match=expected):
        gates._resolve_g4_provenance(g3, g3_path, repository)


def test_direct_artifact_loader_keeps_run_root_containment(tmp_path):
    repository, g2, g2_path, _g3, _g3_path = provenance_fixture(tmp_path)
    outside = repository / "outside.txt"
    outside.write_bytes(b"outside")
    g2["handoverResponsePath"] = str(outside)
    with pytest.raises(RuntimeError, match="QUALIFICATION_EVIDENCE_ARTIFACT_PATH_INVALID"):
        gates._read_evidence_artifact(g2, g2_path, "handoverResponsePath", "handoverResponseSha256")


def test_g4_invalid_transitive_provenance_blocks_before_browser_or_authority_commit(tmp_path, monkeypatch):
    repository, _g2, g2_path, g3, g3_path = provenance_fixture(tmp_path)
    state_root = repository / ".agent-work" / "orchestrator"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "state.json").write_text("{}", encoding="utf-8")
    (state_root / "logs").mkdir()
    (state_root / "logs" / "orchestrator.log").write_text("baseline", encoding="utf-8")
    g3["prerequisiteEvidence"][0]["sha256"] = "0" * 64
    write_json(g3_path, g3)
    monkeypatch.setattr(gates._GateSession, "connect_and_inventory",
                        lambda _self: pytest.fail("invalid provenance must fail before browser attach"))
    commits = []
    monkeypatch.setattr(runtime.ArchitectSessionRollover, "complete_from_response",
                        lambda *_args, **_kwargs: commits.append("commit"))
    evidence = gates._run_one("g4-authority-switch", repository, "http://127.0.0.1:9333", [str(g3_path)])
    assert evidence["result"] == "INCONCLUSIVE"
    assert "QUALIFICATION_PREREQUISITE_PROVENANCE_MISMATCH" in evidence["terminalFailure"]["reason"]
    assert commits == []


def test_provenance_repair_does_not_relax_artifact_root_containment():
    source = __import__("inspect").getsource(gates._read_evidence_artifact)
    assert "_inside(artifact, run_root)" in source


def test_g4_authority_and_other_gate_boundaries_remain_declared():
    source = __import__("inspect").getsource(gates._run_one)
    g4_body = source.split('if gate_id == "g4-authority-switch":', 1)[1].split(
        'if gate_id == "g5-post-discussion-envelope":', 1)[0]
    assert "complete_from_response" in g4_body
    assert "request_post_discussion_envelope_repair" not in g4_body
    assert "run_next_prompt_ready_once" not in g4_body
