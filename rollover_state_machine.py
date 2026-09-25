"""Pure, unused rollover decision model.

This module deliberately contains no runtime integrations.  Commit B defines
the decision contract; later work may add an execution adapter and only then
cut the watcher over to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class RolloverAction(str, Enum):
    NO_ROLLOVER = "NO_ROLLOVER"
    WAIT_DISCUSSION = "WAIT_DISCUSSION"
    WAIT_EXECUTOR = "WAIT_EXECUTOR"
    WAIT_ACTIVE_WRITER = "WAIT_ACTIVE_WRITER"
    WAIT_ARCHITECT = "WAIT_ARCHITECT"
    WAIT_COOLDOWN = "WAIT_COOLDOWN"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    REQUEST_HANDOVER = "REQUEST_HANDOVER"
    RECONCILE_HANDOVER = "RECONCILE_HANDOVER"
    START_RECOVERY_ATTEMPT = "START_RECOVERY_ATTEMPT"
    START_RECOVERY_EPOCH = "START_RECOVERY_EPOCH"
    REVIVE_TRANSACTION = "REVIVE_TRANSACTION"
    RETIRE_TRANSACTION = "RETIRE_TRANSACTION"
    CREATE_FRESH_ARCHITECT = "CREATE_FRESH_ARCHITECT"
    COMMIT_NEW_ARCHITECT_AUTHORITY = "COMMIT_NEW_ARCHITECT_AUTHORITY"
    COMPLETE_ROLLOVER = "COMPLETE_ROLLOVER"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    BLOCK_EXECUTOR_LAUNCH = "BLOCK_EXECUTOR_LAUNCH"
    ALLOW_EXECUTOR_LAUNCH = "ALLOW_EXECUTOR_LAUNCH"
    NORMALIZE_STATE = "NORMALIZE_STATE"


@dataclass(frozen=True)
class RolloverObservations:
    """External facts supplied by browser/process/persistence adapters."""

    discussion_paused: bool = False
    safe_boundary_state: str | None = None
    next_task_id: str | None = None
    executor_process_alive: bool = False
    executor_owns_current_boundary: bool = False
    governed_executor_active_writer: bool = False
    architect_generating: bool = False
    existing_handover_response_observed: bool = False
    existing_handover_response_valid: bool = False
    existing_handover_transaction_matches: bool = False
    existing_handover_task_matches: bool = False
    delivery_acknowledgement_proven: bool = False
    fresh_candidate_observed: bool = False
    fresh_candidate_proof_valid: bool = False
    fresh_candidate_is_current_transaction: bool = False
    fresh_architect_ready: bool = False
    contradictory_live_transaction: bool = False
    valid_result_already_present: bool = False
    current_prompt_available: bool = False


@dataclass(frozen=True)
class RolloverDecision:
    """Exactly one pure decision and its bounded metadata."""

    action: RolloverAction
    reason: str
    task_id: str | None = None
    transaction_id: str | None = None
    wait_until: float | None = None
    normalization: str | None = None


def _text(state: Mapping[str, Any], key: str) -> str:
    value = state.get(key)
    return str(value).strip() if value is not None else ""


def _decision(
    action: RolloverAction,
    reason: str,
    task_id: str | None,
    transaction_id: str | None,
    *,
    wait_until: float | None = None,
    normalization: str | None = None,
) -> RolloverDecision:
    return RolloverDecision(action, reason, task_id, transaction_id, wait_until, normalization)


def _identity_and_shape(state: Mapping[str, Any]) -> tuple[str, str, str | None]:
    workflow = _text(state, "state") or "IDLE"
    task_id = _text(state, "nextTaskId") or _text(state, "taskId")
    transaction_id = _text(state, "rolloverTransactionId") or None
    return workflow, task_id, transaction_id


def evaluate_rollover_state(
    durable_state: Mapping[str, Any],
    observations: RolloverObservations,
    now: float,
) -> RolloverDecision:
    """Return the one canonical rollover action for supplied facts.

    The function is intentionally conservative.  It never mutates the input
    mapping or observations and never discovers external facts itself.
    """

    workflow, task_id, transaction_id = _identity_and_shape(durable_state)
    due = durable_state.get("rolloverDue") is True
    pending = durable_state.get("rolloverPending") is True
    in_progress = durable_state.get("rolloverInProgress") is True
    maintenance = _text(durable_state, "rolloverMaintenanceState")
    recovery = _text(durable_state, "rolloverRecoveryState")
    send_state = _text(durable_state, "rolloverHandoverSendState")
    owner = _text(durable_state, "rolloverDeferredForTaskId")
    transaction_task = _text(durable_state, "rolloverTransactionTaskId")
    attempted_task = _text(durable_state, "rolloverAttemptedForTaskId")
    live_transaction = bool(
        transaction_id
        and task_id
        and transaction_task == task_id
        and pending
        and (in_progress or maintenance in {"DEFERRED", "RECONCILE_PENDING"} or bool(durable_state.get("handoverRequested")))
    )

    # Identity and shape are validated before any wait/recovery decision.
    if observations.contradictory_live_transaction:
        return _decision(RolloverAction.HUMAN_REQUIRED, "CONTRADICTORY_LIVE_TRANSACTION", task_id, transaction_id)
    if due and pending and in_progress and not transaction_id:
        return _decision(RolloverAction.HUMAN_REQUIRED, "IN_PROGRESS_TRANSACTION_ID_MISSING", task_id, transaction_id)
    if transaction_id and transaction_task and task_id and transaction_task != task_id:
        return _decision(RolloverAction.HUMAN_REQUIRED, "TRANSACTION_TASK_MISMATCH", task_id, transaction_id)
    if owner and task_id and owner != task_id:
        return _decision(RolloverAction.HUMAN_REQUIRED, "DEFERRED_TASK_OWNER_MISMATCH", task_id, transaction_id)
    if in_progress and not transaction_id:
        return _decision(RolloverAction.HUMAN_REQUIRED, "IN_PROGRESS_TRANSACTION_ID_MISSING", task_id, transaction_id)
    if due is False and pending:
        return _decision(RolloverAction.HUMAN_REQUIRED, "PENDING_WITHOUT_ROLLOVER_DUE", task_id, transaction_id)
    if maintenance == "RECONCILE_PENDING" and not live_transaction:
        return _decision(RolloverAction.HUMAN_REQUIRED, "RECONCILE_PENDING_WITHOUT_LIVE_TRANSACTION", task_id, transaction_id)
    if due and not pending:
        if workflow != "NEXT_PROMPT_READY" or observations.contradictory_live_transaction:
            return _decision(RolloverAction.HUMAN_REQUIRED, "DUE_WITHOUT_PENDING_INVALID_BOUNDARY", task_id, transaction_id)
        return _decision(
            RolloverAction.NORMALIZE_STATE,
            "DUE_WITHOUT_PENDING",
            task_id,
            transaction_id,
            normalization="SET_PENDING_ONLY_AFTER_SAFE_BOUNDARY_VALIDATION",
        )

    if workflow == "HUMAN_REQUIRED":
        return _decision(RolloverAction.HUMAN_REQUIRED, "HUMAN_REQUIRED_ALREADY_OWNS_BOUNDARY", task_id, transaction_id)

    if not due and not pending and not in_progress:
        return _decision(RolloverAction.NO_ROLLOVER, "NO_ROLLOVER", task_id, transaction_id)

    # Human pause and governed ownership always outrank deadlines and budget.
    if observations.discussion_paused:
        return _decision(RolloverAction.WAIT_DISCUSSION, "DISCUSSION_PAUSED", task_id, transaction_id)
    if observations.executor_process_alive:
        if observations.executor_owns_current_boundary:
            return _decision(RolloverAction.WAIT_EXECUTOR, "EXECUTOR_OWNS_CURRENT_BOUNDARY", task_id, transaction_id)
        return _decision(RolloverAction.HUMAN_REQUIRED, "EXECUTOR_OWNERSHIP_UNPROVEN", task_id, transaction_id)
    if observations.governed_executor_active_writer:
        return _decision(RolloverAction.WAIT_ACTIVE_WRITER, "GOVERNED_EXECUTOR_ACTIVE_WRITER", task_id, transaction_id)
    if observations.architect_generating:
        return _decision(RolloverAction.WAIT_ARCHITECT, "ARCHITECT_GENERATING", task_id, transaction_id)

    # Existing exact authority is consumable before any retry or exhaustion gate.
    exact_handover = (
        observations.existing_handover_response_observed
        and observations.existing_handover_response_valid
        and observations.existing_handover_transaction_matches
        and observations.existing_handover_task_matches
    )
    if exact_handover and live_transaction:
        return _decision(RolloverAction.RECONCILE_HANDOVER, "EXACT_EXISTING_HANDOVER", task_id, transaction_id)

    recovery_retry_after = durable_state.get("rolloverRecoveryRetryAfter")
    if live_transaction and _text(durable_state, "rolloverHandoverRecoveryDisposition") == "RETRYABLE":
        if isinstance(recovery_retry_after, (int, float)) and float(recovery_retry_after) > now:
            return _decision(RolloverAction.WAIT_RECONCILIATION, "RECONCILIATION_DEADLINE_FUTURE", task_id, transaction_id, wait_until=float(recovery_retry_after))

    max_epochs = durable_state.get("rolloverAutomaticRecoveryMaxEpochs", 0)
    epochs = durable_state.get("rolloverAutomaticRecoveryEpochCount", 0)
    try:
        max_epochs_i = int(max_epochs)
        epochs_i = int(epochs)
    except (TypeError, ValueError):
        return _decision(RolloverAction.HUMAN_REQUIRED, "RECOVERY_EPOCH_BUDGET_INVALID", task_id, transaction_id)
    if max_epochs_i < 0 or epochs_i < 0:
        return _decision(RolloverAction.HUMAN_REQUIRED, "RECOVERY_EPOCH_BUDGET_INVALID", task_id, transaction_id)
    if epochs_i >= max_epochs_i and (due or pending or in_progress):
        return _decision(RolloverAction.HUMAN_REQUIRED, "AUTOMATIC_RECOVERY_EXHAUSTED", task_id, transaction_id)

    next_eligible = durable_state.get("rolloverAutomaticRecoveryNextEligibleAt")
    if maintenance == "DEFERRED":
        deferred_owner_missing = not any((owner, transaction_task, attempted_task))
        if deferred_owner_missing:
            return _decision(RolloverAction.HUMAN_REQUIRED, "DEFERRED_TASK_OWNER_MISSING", task_id, transaction_id)
        if isinstance(next_eligible, (int, float)) and float(next_eligible) > now:
            return _decision(RolloverAction.WAIT_COOLDOWN, "AUTOMATIC_RECOVERY_COOLDOWN", task_id, transaction_id, wait_until=float(next_eligible))
        return _decision(RolloverAction.START_RECOVERY_EPOCH, "AUTOMATIC_RECOVERY_COOLDOWN_EXPIRED", task_id, transaction_id)

    if exact_handover:
        return _decision(RolloverAction.HUMAN_REQUIRED, "HANDOVER_EVIDENCE_NOT_LIVE_TRANSACTION", task_id, transaction_id)

    if observations.fresh_candidate_observed and observations.fresh_candidate_proof_valid and observations.fresh_candidate_is_current_transaction:
        if observations.fresh_architect_ready:
            return _decision(RolloverAction.COMMIT_NEW_ARCHITECT_AUTHORITY, "FRESH_ARCHITECT_READY", task_id, transaction_id)
        return _decision(RolloverAction.CREATE_FRESH_ARCHITECT, "FRESH_CANDIDATE_PROVEN", task_id, transaction_id)

    if live_transaction:
        if observations.delivery_acknowledgement_proven and send_state in {"ACKNOWLEDGED", "AMBIGUOUS"}:
            return _decision(RolloverAction.RECONCILE_HANDOVER, "DELIVERY_ACKNOWLEDGEMENT_RECONCILIATION", task_id, transaction_id)
        if recovery == "RECOVERING":
            return _decision(RolloverAction.START_RECOVERY_ATTEMPT, "LIVE_TRANSACTION_RECOVERY", task_id, transaction_id)
        return _decision(RolloverAction.WAIT_RECONCILIATION, "LIVE_TRANSACTION_REQUIRES_RECONCILIATION", task_id, transaction_id)

    if due and pending and not transaction_id and durable_state.get("handoverRequested") is not True:
        if observations.current_prompt_available and workflow == "NEXT_PROMPT_READY":
            return _decision(RolloverAction.REQUEST_HANDOVER, "ROLLOVER_REQUEST_REQUIRED", task_id, transaction_id)
        return _decision(RolloverAction.BLOCK_EXECUTOR_LAUNCH, "ROLLOVER_PROMPT_OR_BOUNDARY_UNAVAILABLE", task_id, transaction_id)

    if observations.fresh_architect_ready:
        return _decision(RolloverAction.COMPLETE_ROLLOVER, "FRESH_ARCHITECT_ALREADY_READY", task_id, transaction_id)
    return _decision(RolloverAction.BLOCK_EXECUTOR_LAUNCH, "ROLLOVER_UNRESOLVED", task_id, transaction_id)
