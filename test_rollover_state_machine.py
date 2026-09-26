from __future__ import annotations

import ast
import copy
import inspect
import textwrap

import pytest

import local_orchestrator_watcher as watcher_module
from rollover_state_machine import (
    RolloverAction,
    RolloverObservations,
    evaluate_rollover_state,
)


def state(**overrides):
    value = {
        "state": "NEXT_PROMPT_READY",
        "taskId": "000102",
        "lastCompletedTaskId": "000102",
        "nextTaskId": "000103",
        "rolloverDue": True,
        "rolloverPending": True,
        "rolloverInProgress": False,
        "rolloverMaintenanceState": "DEFERRED",
        "rolloverRecoveryState": "DEFERRED",
        "rolloverAutomaticRecoveryEpochCount": 1,
        "rolloverAutomaticRecoveryMaxEpochs": 3,
        "rolloverAutomaticRecoveryNextEligibleAt": 100.0,
        "rolloverDeferredForTaskId": "000103",
        "rolloverTransactionId": "tx-1",
        "rolloverTransactionTaskId": "000103",
        "handoverRequested": True,
        "rolloverHandoverSendState": "AMBIGUOUS",
    }
    value.update(overrides)
    return value


def obs(**overrides):
    value = RolloverObservations(
        safe_boundary_state="NEXT_PROMPT_READY",
        next_task_id="000103",
        current_prompt_available=True,
    )
    return RolloverObservations(**{**value.__dict__, **overrides})


def decide(value, observations=None, now=50.0):
    return evaluate_rollover_state(value, observations or obs(), now)


def test_no_rollover_is_ordinary_no_rollover_decision():
    assert decide(state(rolloverDue=False, rolloverPending=False), now=50).action is RolloverAction.NO_ROLLOVER


@pytest.mark.parametrize(
    ("changes", "action"),
    [
        ({"rolloverDue": True, "rolloverPending": False, "rolloverInProgress": False}, RolloverAction.NORMALIZE_STATE),
        ({"rolloverDue": False, "rolloverPending": True}, RolloverAction.HUMAN_REQUIRED),
        ({"rolloverInProgress": True, "rolloverTransactionId": None}, RolloverAction.HUMAN_REQUIRED),
        ({"rolloverTransactionTaskId": "OTHER"}, RolloverAction.HUMAN_REQUIRED),
        ({"rolloverDeferredForTaskId": None, "rolloverTransactionId": None, "rolloverTransactionTaskId": None}, RolloverAction.HUMAN_REQUIRED),
        ({"rolloverMaintenanceState": "RECONCILE_PENDING", "rolloverInProgress": False, "rolloverTransactionId": None, "rolloverTransactionTaskId": None}, RolloverAction.HUMAN_REQUIRED),
        ({"rolloverAutomaticRecoveryEpochCount": 3}, RolloverAction.HUMAN_REQUIRED),
    ],
)
def test_illegal_or_contradictory_states_fail_closed(changes, action):
    assert decide(state(**changes)).action is action


def test_discussion_pause_precedes_expired_cooldown_without_budget_change():
    decision = decide(state(rolloverAutomaticRecoveryNextEligibleAt=1), obs(discussion_paused=True), now=50)
    assert decision.action is RolloverAction.WAIT_DISCUSSION


def test_observation_identity_mismatch_fails_closed():
    value = state()
    boundary = decide(value, obs(safe_boundary_state="RESULT_READY"), 50)
    task = decide(value, obs(next_task_id="000104"), 50)
    matching = decide(value, obs(), 50)
    assert boundary.action is RolloverAction.HUMAN_REQUIRED
    assert boundary.reason == "SAFE_BOUNDARY_STATE_MISMATCH"
    assert task.action is RolloverAction.HUMAN_REQUIRED
    assert task.reason == "NEXT_TASK_OBSERVATION_MISMATCH"
    assert matching.action is RolloverAction.WAIT_COOLDOWN


def test_future_cooldown_waits_with_deadline():
    decision = decide(state(rolloverAutomaticRecoveryNextEligibleAt=100), now=50)
    assert decision.action is RolloverAction.WAIT_COOLDOWN
    assert decision.wait_until == 100


def test_expired_cooldown_progresses_to_epoch_recovery():
    decision = decide(state(rolloverAutomaticRecoveryNextEligibleAt=49), now=50)
    assert decision.action is RolloverAction.START_RECOVERY_EPOCH
    assert decision.action is not RolloverAction.WAIT_COOLDOWN


def test_existing_exact_handover_precedes_expired_cooldown():
    decision = decide(
        state(rolloverAutomaticRecoveryNextEligibleAt=1),
        obs(
            existing_handover_response_observed=True,
            existing_handover_response_valid=True,
            existing_handover_transaction_matches=True,
            existing_handover_task_matches=True,
        ),
        now=50,
    )
    assert decision.action is RolloverAction.RECONCILE_HANDOVER


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        (obs(executor_process_alive=True, executor_owns_current_boundary=True), RolloverAction.WAIT_EXECUTOR),
        (obs(governed_executor_active_writer=True), RolloverAction.WAIT_ACTIVE_WRITER),
        (obs(architect_generating=True), RolloverAction.WAIT_ARCHITECT),
    ],
)
def test_external_safety_observations_precede_recovery(observations, expected):
    assert decide(state(rolloverAutomaticRecoveryNextEligibleAt=1), observations, 50).action is expected


def test_reconciliation_backoff_has_future_wait_and_expired_progress():
    value = state(
        rolloverMaintenanceState="RECONCILE_PENDING",
        rolloverInProgress=True,
        rolloverRecoveryState="RECOVERING",
        rolloverHandoverRecoveryDisposition="RETRYABLE",
        rolloverRecoveryRetryAfter=100,
    )
    assert decide(value, now=50).action is RolloverAction.WAIT_RECONCILIATION
    assert decide(value, now=101).action is RolloverAction.START_RECOVERY_ATTEMPT


def test_production_shaped_000102_to_000103_fixture():
    value = state()
    assert decide(value, obs(discussion_paused=True), 50).action is RolloverAction.WAIT_DISCUSSION
    exact = obs(
        existing_handover_response_observed=True,
        existing_handover_response_valid=True,
        existing_handover_transaction_matches=True,
        existing_handover_task_matches=True,
    )
    assert decide(value, exact, 50).action is RolloverAction.RECONCILE_HANDOVER
    assert decide(value, now=50).action is RolloverAction.WAIT_COOLDOWN
    assert decide(value, now=101).action is RolloverAction.START_RECOVERY_EPOCH


def fresh_rollover_state():
    return {
        "state": "NEXT_PROMPT_READY",
        "taskId": "000200",
        "lastCompletedTaskId": "000200",
        "nextTaskId": "000201",
        "rolloverDue": True,
        "rolloverPending": True,
        "rolloverInProgress": False,
        "handoverRequested": False,
    }


def test_fresh_rollover_without_epoch_budget_requests_handover():
    assert decide(fresh_rollover_state(), obs(next_task_id="000201"), 50).action is RolloverAction.REQUEST_HANDOVER


@pytest.mark.parametrize(
    "missing",
    [
        "rolloverAutomaticRecoveryEpochCount",
        "rolloverAutomaticRecoveryMaxEpochs",
    ],
)
def test_deferred_incomplete_epoch_budget_fails_closed(missing):
    value = state()
    value.pop(missing)
    decision = decide(value, now=50)
    assert decision.action is RolloverAction.HUMAN_REQUIRED
    assert decision.reason == "RECOVERY_EPOCH_BUDGET_INVALID"


def test_normalization_then_request_journey_does_not_invent_budget():
    original = fresh_rollover_state()
    first = decide({**original, "rolloverPending": False}, obs(next_task_id="000201"), 50)
    normalized = {**original, "rolloverPending": True}
    second = decide(normalized, obs(next_task_id="000201"), 50)
    assert first.action is RolloverAction.NORMALIZE_STATE
    assert first.normalization == "SET_PENDING_ONLY_AFTER_SAFE_BOUNDARY_VALIDATION"
    assert second.action is RolloverAction.REQUEST_HANDOVER
    assert original["rolloverPending"] is True


def test_scheduler_has_no_legacy_identity_or_special_task_rule():
    source = inspect.getsource(evaluate_rollover_state)
    assert "7fdbd798659f42295a18dd2d" not in source
    assert '"000103"' not in source
    first = decide(state(nextTaskId="task-B", rolloverTransactionTaskId="task-B", rolloverDeferredForTaskId="task-B"), obs(next_task_id="task-B"), now=101)
    second = decide(state(nextTaskId="task-C", rolloverTransactionTaskId="task-C", rolloverDeferredForTaskId="task-C"), obs(next_task_id="task-C"), now=101)
    assert first.action is second.action is RolloverAction.START_RECOVERY_EPOCH


def test_evaluator_is_deterministic_and_does_not_mutate_inputs():
    durable = state(rolloverAutomaticRecoveryNextEligibleAt=49)
    observations = obs()
    before_state = copy.deepcopy(durable)
    before_observations = observations
    results = [decide(durable, observations, 50) for _ in range(10)]
    assert all(result == results[0] for result in results)
    assert durable == before_state
    assert observations == before_observations


def test_pure_module_has_no_forbidden_runtime_dependencies():
    import rollover_state_machine

    tree = ast.parse(inspect.getsource(rollover_state_machine))
    forbidden = {"playwright", "subprocess", "threading", "time", "os", "pathlib", "socket"}
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported.isdisjoint(forbidden)
    assert "now" in inspect.signature(evaluate_rollover_state).parameters
    source = inspect.getsource(evaluate_rollover_state)
    assert "time." not in source
    assert "open(" not in source


def test_no_unresolved_rollover_can_allow_executor_launch():
    cases = [
        state(),
        state(rolloverAutomaticRecoveryNextEligibleAt=1),
        state(rolloverTransactionId=None),
        state(rolloverDue=False, rolloverPending=True),
    ]
    for value in cases:
        decision = decide(value, now=50)
        assert decision.action is not RolloverAction.ALLOW_EXECUTOR_LAUNCH


def test_request_handover_requires_safe_boundary_and_prompt():
    value = state(
        rolloverTransactionId=None,
        rolloverHandoverSendState="UNSENT",
        handoverRequested=False,
        rolloverMaintenanceState="IN_PROGRESS",
        rolloverRecoveryState="PENDING",
        rolloverAutomaticRecoveryNextEligibleAt=0,
    )
    assert decide(value).action is RolloverAction.REQUEST_HANDOVER
    assert decide(value, obs(current_prompt_available=False)).action is RolloverAction.BLOCK_EXECUTOR_LAUNCH


def _function_node(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(source))
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _assert_delegated(source: str, function_name: str, required_call: str) -> None:
    node = _function_node(source, function_name)
    calls = _called_names(node)
    assert required_call in calls, f"{function_name} must delegate to {required_call}"


def test_rollover_transition_authority_structure_is_delegated():
    source = inspect.getsource(watcher_module)
    _assert_delegated(source, "run_next_prompt_ready_once", "_evaluate_public_rollover_boundary")
    _assert_delegated(source, "dispatch_next_prompt_once", "_evaluate_public_rollover_boundary")
    _assert_delegated(source, "deferred_rollover_passive_wait_required", "_evaluate_public_rollover_boundary")
    _assert_delegated(source, "reconciliation_backoff_wait_required", "_evaluate_public_rollover_boundary")
    _assert_delegated(source, "_automatic_recovery_epoch_eligible", "_evaluate_public_rollover_boundary")
    for name in (
        "_terminal_same_task_transaction_eligible",
        "_retire_stale_transaction_for_task",
        "_revive_terminal_delivered_transaction",
        "request_if_due",
    ):
        _assert_delegated(source, name, "rollover_transaction_lifecycle_action")

    evaluator_source = inspect.getsource(evaluate_rollover_state)
    lifecycle_source = inspect.getsource(watcher_module.rollover_transaction_lifecycle_action)
    assert "7fdbd798659f42295a18dd2d" not in evaluator_source
    assert "000103" not in evaluator_source
    assert "7fdbd798659f42295a18dd2d" not in lifecycle_source
    assert "000103" not in lifecycle_source


def test_rollover_transition_guard_detects_synthetic_duplicate_cooldown_authority():
    bad = """
    def deferred_rollover_passive_wait_required(watcher):
        return watcher.state.get('rolloverAutomaticRecoveryNextEligibleAt', 0) > time.time()
    """
    with pytest.raises(AssertionError):
        _assert_delegated(bad, "deferred_rollover_passive_wait_required", "_evaluate_public_rollover_boundary")


def test_rollover_transition_guard_detects_synthetic_duplicate_reconciliation_authority():
    bad = """
    def reconciliation_backoff_wait_required(watcher):
        state = watcher.state
        return state.get('rolloverRecoveryRetryAfter', 0) > time.time()
    """
    with pytest.raises(AssertionError):
        _assert_delegated(bad, "reconciliation_backoff_wait_required", "_evaluate_public_rollover_boundary")


def test_rollover_transition_guard_protects_lifecycle_delegation():
    bad = """
    def _terminal_same_task_transaction_eligible(state, task_id):
        return state.get('rolloverTransactionTaskId') == task_id
    """
    with pytest.raises(AssertionError):
        _assert_delegated(bad, "_terminal_same_task_transaction_eligible", "rollover_transaction_lifecycle_action")


def test_known_current_public_entry_path_reaches_expired_recovery():
    class FakeWatcher:
        def __init__(self):
            self.state = state(rolloverAutomaticRecoveryNextEligibleAt=1)
            self.reloads = 0

        def _load_state(self):
            self.reloads += 1
            return self.state

    watcher = FakeWatcher()
    service_called = []
    original_sleep = watcher_module.time.sleep
    original_dispatch = watcher_module.dispatch_next_prompt_once
    original_passive = watcher_module.passive_deferred_rollover_wait
    try:
        watcher_module.time.sleep = lambda _seconds: None
        watcher_module.dispatch_next_prompt_once = lambda *args, **kwargs: service_called.append(True)
        watcher_module.passive_deferred_rollover_wait = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("old passive authority was called"))
        watcher_module.run_next_prompt_ready_once(
            watcher,
            lambda *_args: None,
            "unused",
            lambda: False,
        )
    finally:
        watcher_module.time.sleep = original_sleep
        watcher_module.dispatch_next_prompt_once = original_dispatch
        watcher_module.passive_deferred_rollover_wait = original_passive
    assert service_called, "expired cooldown should reach the recovery service after Commit C"


class _PublicPathWatcher:
    def __init__(self, value):
        self.state = value
        self.reloaded = 0
        self.saved = 0
        self.launched = 0
        self.runtime_logger = None
        self.runtime_run_id = None

    def _load_state(self):
        self.reloaded += 1
        return self.state

    def save(self):
        self.saved += 1

    def retire_completed_executor_ownership(self):
        return False

    def launch_next(self, launch):
        self.launched += 1
        return launch(self.state.get("nextPromptPath", ""), None)


def test_public_path_future_cooldown_waits_without_dispatch(monkeypatch):
    watcher = _PublicPathWatcher(state(rolloverAutomaticRecoveryNextEligibleAt=100))
    sleeps = []
    dispatched = []
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(watcher_module, "dispatch_next_prompt_once", lambda *args: dispatched.append(True))
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: None, "unused", lambda: False)
    assert sleeps and 0 < sleeps[0] <= 2.0
    assert not dispatched


def test_public_path_expired_cooldown_reaches_dispatch(monkeypatch):
    watcher = _PublicPathWatcher(state(rolloverAutomaticRecoveryNextEligibleAt=49))
    dispatched = []
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher_module, "dispatch_next_prompt_once", lambda *args: dispatched.append(True))
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: None, "unused", lambda: False)
    assert dispatched == [True]


def test_public_path_reconciliation_deadline_waits_then_reaches_dispatch(monkeypatch):
    value = state(
        rolloverMaintenanceState="RECONCILE_PENDING",
        rolloverInProgress=True,
        rolloverRecoveryState="RECOVERING",
        rolloverHandoverRecoveryDisposition="RETRYABLE",
        rolloverRecoveryRetryAfter=100,
    )
    watcher = _PublicPathWatcher(value)
    dispatched = []
    sleeps = []
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(watcher_module, "dispatch_next_prompt_once", lambda *args: dispatched.append(True))
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: None, "unused", lambda: False)
    assert sleeps and not dispatched
    monkeypatch.setattr(watcher_module.time, "time", lambda: 101.0)
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: None, "unused", lambda: False)
    assert dispatched == [True]


def test_public_path_observation_mismatch_fails_closed(monkeypatch):
    watcher = _PublicPathWatcher(fresh_rollover_state())
    launched = []
    monkeypatch.setattr(
        watcher_module,
        "build_rollover_observations",
        lambda *_args: obs(safe_boundary_state="RESULT_READY", next_task_id="000201"),
    )
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: launched.append(True), "unused", lambda: False)
    assert watcher.state["state"] == "HUMAN_REQUIRED"
    assert watcher.state["humanRequiredReason"] == "ROLLOVER_EVALUATOR_SAFE_BOUNDARY_STATE_MISMATCH"
    assert not launched


def test_public_path_discussion_pause_does_not_consume_recovery(monkeypatch):
    watcher = _PublicPathWatcher(state(rolloverAutomaticRecoveryNextEligibleAt=1, rolloverRecoveryAttemptCount=1))
    before = copy.deepcopy(watcher.state)
    dispatched = []
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(watcher_module, "dispatch_next_prompt_once", lambda *args: dispatched.append(True))
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: None, "unused", lambda: True)
    assert watcher.state == before
    assert not dispatched


def test_public_path_active_executor_and_writer_block(monkeypatch):
    active = state(rolloverAutomaticRecoveryNextEligibleAt=1, executorProcessState="RUNNING", codexPid=123)
    watcher = _PublicPathWatcher(active)
    dispatched = []
    monkeypatch.setattr(watcher_module.LocalWatcher, "process_alive", staticmethod(lambda _pid: True))
    monkeypatch.setattr(watcher_module.time, "time", lambda: 50.0)
    monkeypatch.setattr(watcher_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(watcher_module, "dispatch_next_prompt_once", lambda *args: dispatched.append(True))
    watcher_module.run_next_prompt_ready_once(watcher, lambda *_: None, "unused", lambda: False)
    assert not dispatched
    writer = _PublicPathWatcher(state(rolloverAutomaticRecoveryNextEligibleAt=1, governedExecutorActiveWriter=True))
    watcher_module.run_next_prompt_ready_once(writer, lambda *_: None, "unused", lambda: False)
    assert not dispatched
