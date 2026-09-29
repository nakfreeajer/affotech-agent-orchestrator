import inspect
import json

import pytest

import qualification_gate_runner as gates
import orchestrator_real_browser_qualification as full_qualification
import local_orchestrator_watcher as runtime


@pytest.mark.parametrize("gate_id", gates.GATE_ORDER[:3])
def test_selected_gate_dispatches_only_requested_gate(gate_id):
    called = []
    handlers = {name: (lambda name=name: called.append(name)) for name in gates.GATE_ORDER}
    gates.dispatch_selected_gate(gate_id, handlers)
    assert called == [gate_id]


def test_failed_gate_stops_without_dispatching_later_gates():
    called = []
    def fail():
        called.append("g2-old-handover")
        raise RuntimeError("G2_BLOCKED")
    handlers = {
        "g2-old-handover": fail,
        "g3-fresh-bootstrap": lambda: called.append("g3-fresh-bootstrap"),
    }
    with pytest.raises(RuntimeError, match="G2_BLOCKED"):
        gates.dispatch_selected_gate("g2", handlers)
    assert called == ["g2-old-handover"]


def test_each_gate_session_has_its_own_evidence_and_result(tmp_path):
    state_root = tmp_path / ".agent-work" / "orchestrator"
    state_root.mkdir(parents=True)
    (state_root / "state.json").write_text("{}", encoding="utf-8")
    (state_root / "logs").mkdir()
    (state_root / "logs" / "orchestrator.log").write_text("baseline", encoding="utf-8")
    sessions = [gates._GateSession(tmp_path, "http://127.0.0.1:9333", gate) for gate in gates.GATE_ORDER]
    evidence_paths = []
    try:
        for session in sessions:
            session.start()
            result = session.finish("PASS", pageMutationCount=0)
            evidence_paths.append(session.evidence_path)
            assert result["runId"] == session.run_id
            assert result["gateId"] == session.gate_id
            assert result["result"] == "PASS"
            persisted = json.loads(session.evidence_path.read_text(encoding="utf-8"))
            assert persisted["result"] == "PASS"
    finally:
        for session in sessions:
            session.close()
    assert evidence_paths[0] != evidence_paths[1]
    assert len(set(session.run_id for session in sessions)) == len(sessions)
    assert len(set(evidence_paths)) == len(sessions)


def test_gate_prerequisites_and_full_chain_order_are_explicit():
    assert gates.GATE_PREREQUISITES["g2-old-handover"] == ("g1-cdp-attach",)
    assert gates.GATE_PREREQUISITES["g3-fresh-bootstrap"] == ("g2-old-handover",)
    assert gates.GATE_PREREQUISITES["g4-authority-switch"] == ("g3-fresh-bootstrap",)
    assert gates.GATE_PREREQUISITES["g5-post-discussion-envelope"] == ("g4-authority-switch",)
    assert gates.GATE_PREREQUISITES["g6-executor-dispatch"] == ("g5-post-discussion-envelope",)
    assert gates.GATE_PREREQUISITES["g7-full-chain"] == gates.GATE_ORDER
    source = inspect.getsource(full_qualification.run)
    sequence = ["connect_over_cdp", "old_bridge.submit_result_bounded",
                "complete_from_response", "request_post_discussion_envelope_repair",
                "reconcile_post_discussion_response", "run_next_prompt_ready_once"]
    offsets = [source.index(token) for token in sequence]
    assert offsets == sorted(offsets)
    coordinator = inspect.getsource(runtime.ArchitectSessionRollover.complete_from_response)
    assert coordinator.index("open_fresh_with_handover") < coordinator.index("COMMIT_NEW_CONVERSATION_AUTHORITY")


def test_borrowed_pages_remain_protected_except_for_proven_owned_ids(tmp_path):
    state_root = tmp_path / ".agent-work" / "orchestrator"
    state_root.mkdir(parents=True)
    (state_root / "state.json").write_text("{}", encoding="utf-8")
    session = gates._GateSession(tmp_path, "http://127.0.0.1:9333", "g4-authority-switch")
    session.protected_ids.update({"protected-production", "owned-old", "owned-fresh"})
    prior = {"protectedConversationIds": ["protected-production"], "ownedPages": []}
    view = session.evidence_view(prior, {"owned-old", "owned-fresh"})
    assert "protected-production" in view["protectedConversationIds"]
    assert "owned-old" not in view["protectedConversationIds"]
    assert "owned-fresh" not in view["protectedConversationIds"]


def test_gate_implementations_keep_existing_parser_hash_and_no_resend_authorities():
    source = inspect.getsource(gates)
    assert "runtime.parse_handover_envelope" in source
    assert "reconcile_post_discussion_response" in source
    assert '"stagedPromptSha256"' in source
    assert "_sha_bytes(prompt_bytes) != values[\"stagedPromptSha256\"]" in source
    assert "open_fresh_with_handover(handover)" in source
    assert "QUALIFICATION_UNEXPECTED_SEND_BLOCKED" in source
    assert '"freshBootstrapResendCount": 0' in source


def test_executor_gate_uses_only_qualification_spy():
    source = inspect.getsource(gates._run_one)
    spy_source = inspect.getsource(runtime._QualificationExecutorLaunchSpy)
    assert "run_next_prompt_ready_once" in source
    assert "_QualificationExecutorLaunchSpy" in source
    assert "subprocess" not in spy_source
    assert 'realExecutor=False' in spy_source


def test_bootstrap_timing_evidence_is_bounded_and_omits_payload(tmp_path):
    log_path = tmp_path / "qualification.log"
    log_path.write_text(
        "2026-09-29 level=INFO runtimeContext=QUALIFICATION runId=x state=UNKNOWN taskId=NONE "
        "event=FRESH_BOOTSTRAP_RECONCILIATION_POLL elapsedMs=120 poll=2 "
        "semanticUserMessageCount=1 semanticExactMatchCount=1 visibleUserMessageCount=0 "
        "visibleExactMatchCount=0 selectedSource=SEMANTIC_HISTORY observedExactMatch=True "
        "stabilityCount=2 deadlineMs=2000 payloadLength=100 payloadSha256=abc\n"
        "2026-09-29 level=INFO runtimeContext=QUALIFICATION runId=x state=UNKNOWN taskId=NONE "
        "event=FRESH_BOOTSTRAP_RECONCILIATION_COMPLETE disposition=SENT deadlineMs=2000 pollCount=2\n",
        encoding="utf-8")
    metrics = gates._bootstrap_timing_metrics(log_path)
    assert metrics["normalReconciliationResult"] == "SENT"
    assert metrics["normalPollCount"] == 1
    assert metrics["firstExactBootstrapObservedWithinNormalWindow"] is True
    assert metrics["firstExactBootstrapObservedSource"] == "SEMANTIC_HISTORY"
    assert "synthetic full bootstrap" not in json.dumps(metrics)
