import { validateControlObject } from '../protocol/validate-control-object.js';
import { classifySideEffect, validateIntent, validateResult } from './exactly-once.js';
import { evaluateWorkerLease, validateWorkerFence } from './worker-lease.js';
import { RECOVERY_CLASSIFICATIONS, SIDE_EFFECT_CLASSIFICATIONS, LEASE_CLASSIFICATIONS, INTENT_TYPE, RESULT_TYPE, RECONCILIATION_OBSERVATIONS } from './constants.js';

const flags = (extra = {}) => ({ safeToContinue: false, workerRestartEligible: false, workerDispatchAuthorized: false, architectTriggerAuthorized: false, newSideEffectAuthorized: false, retryAuthorized: false, reconciliationRequired: false, circuitOpen: false, ...extra });
const result = (classification, reasonCodes, extra = {}) => ({ classification, ...flags({ reconciliationRequired: classification === RECOVERY_CLASSIFICATIONS.RECONCILIATION_REQUIRED, circuitOpen: classification === RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ...extra }), reasonCodes: [...new Set(reasonCodes)] });
const plainObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);

function checkpointDefects(checkpoint, mission) {
  const d = [];
  if (!checkpoint || typeof checkpoint !== 'object') return ['CHECKPOINT_MISSING'];
  if (checkpoint.missionId !== mission.missionId) d.push('CHECKPOINT_MISSION_MISMATCH');
  if (checkpoint.currentMilestone !== mission.currentMilestone) d.push('CHECKPOINT_MILESTONE_MISMATCH');
  if (checkpoint.currentRole !== mission.currentRole) d.push('CHECKPOINT_ROLE_MISMATCH');
  for (const field of ['bootId', 'lastJournalEventId', 'currentMessageId']) if (typeof checkpoint[field] !== 'string' || checkpoint[field].length === 0) d.push(`CHECKPOINT_${field.toUpperCase()}_INVALID`);
  return d;
}

export function recoverBootState({ controlBundle, checkpoint, shutdownMarker, activeWorkerLease = null, sideEffectLedger = [], reconciliationObservations = {}, nowMs, currentBootId, workerMayHaveEnteredSideEffect = false, workerNeverStarted = false, workerIdentity = null } = {}) {
  const control = validateControlObject(controlBundle);
  if (control.classification === 'FAIL_CLOSED') return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['CONTROL_STATE_INVALID', ...control.defects.map((x) => x.code)]);
  const hard = checkpointDefects(checkpoint, controlBundle.mission);
  if (shutdownMarker !== undefined && shutdownMarker !== null && (typeof shutdownMarker.clean !== 'boolean' || typeof shutdownMarker.previousBootId !== 'string' || shutdownMarker.previousBootId.length === 0)) hard.push('SHUTDOWN_MARKER_INVALID');
  if (hard.length) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, hard);
  if (controlBundle.mission.missionState === 'PAUSED_BY_RONY') return result(RECOVERY_CLASSIFICATIONS.PAUSED_BY_RONY, ['PAUSED_BY_RONY']);
  if (controlBundle.mission.circuitState === 'OPEN') return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['CIRCUIT_OPEN']);
  if (!Array.isArray(sideEffectLedger)) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['SIDE_EFFECT_LEDGER_INVALID', 'LEDGER_NOT_ARRAY']);
  if (!plainObject(reconciliationObservations) || Object.values(reconciliationObservations).some((value) => !RECONCILIATION_OBSERVATIONS.includes(value))) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['SIDE_EFFECT_LEDGER_INVALID', 'OBSERVATIONS_INVALID']);
  const ledgerDefects = [];
  for (const record of sideEffectLedger) {
    if (!plainObject(record)) { ledgerDefects.push('LEDGER_RECORD_INVALID'); continue; }
    if (record.type === INTENT_TYPE) ledgerDefects.push(...validateIntent(record));
    else if (record.type === RESULT_TYPE) ledgerDefects.push(...validateResult(record));
    else ledgerDefects.push('UNSUPPORTED_LEDGER_RECORD_TYPE');
  }
  if (ledgerDefects.length) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['SIDE_EFFECT_LEDGER_INVALID', ...ledgerDefects]);
  const dedupeKeys = [...new Set(sideEffectLedger.map((x) => x.dedupeKey))];
  for (const dedupeKey of dedupeKeys) {
    const ledger = classifySideEffect({ ledger: sideEffectLedger, dedupeKey });
    if (ledger.classification === SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['SIDE_EFFECT_LEDGER_INVALID', ...ledger.reasonCodes]);
    if (ledger.classification === SIDE_EFFECT_CLASSIFICATIONS.RECONCILIATION_REQUIRED) {
      const observation = reconciliationObservations[dedupeKey];
      if (!observation || observation === 'UNKNOWN') return result(RECOVERY_CLASSIFICATIONS.RECONCILIATION_REQUIRED, ['UNRESOLVED_INTENT', `DEDUPE_${dedupeKey}`]);
      return result(RECOVERY_CLASSIFICATIONS.RECONCILIATION_REQUIRED, ['INTENT_REQUIRES_ARCHITECT_REVIEW', `OBSERVATION_${observation}`, `DEDUPE_${dedupeKey}`]);
    }
  }
  if (activeWorkerLease) {
    const evaluation = evaluateWorkerLease(activeWorkerLease, nowMs);
    if (evaluation.classification === LEASE_CLASSIFICATIONS.FAIL_CLOSED) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['LEASE_INVALID', ...evaluation.defects]);
    if (workerIdentity) {
      const fence = validateWorkerFence(activeWorkerLease, { ...workerIdentity, nowMs });
      if (fence.classification === LEASE_CLASSIFICATIONS.FENCED_STALE_WORKER) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['STALE_WORKER_FENCE']);
      if (fence.classification === LEASE_CLASSIFICATIONS.FAIL_CLOSED) return result(RECOVERY_CLASSIFICATIONS.CIRCUIT_OPEN, ['WORKER_FENCE_INVALID']);
    }
    if (evaluation.classification === LEASE_CLASSIFICATIONS.WORKER_LIVENESS_UNKNOWN) return result(RECOVERY_CLASSIFICATIONS.RECONCILIATION_REQUIRED, ['WORKER_LIVENESS_UNKNOWN', ...(workerMayHaveEnteredSideEffect ? ['WORKER_SIDE_EFFECT_AMBIGUOUS'] : [])]);
    if (workerNeverStarted && !workerMayHaveEnteredSideEffect) return result(RECOVERY_CLASSIFICATIONS.RECOVERED_RESTART_WORKER, ['WORKER_NEVER_STARTED'], { workerRestartEligible: true });
  }
  return result(RECOVERY_CLASSIFICATIONS.RECOVERED_SAFE_TO_CONTINUE, [shutdownMarker?.clean === false || !shutdownMarker ? 'UNCLEAN_BOOT_RECONCILED' : 'CLEAN_BOOT_RECONCILED'], { safeToContinue: true });
}
