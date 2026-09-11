import crypto from 'node:crypto';
import { CIRCUIT_STATES, MISSION_STATES } from '../protocol/constants.js';
import { ACTION_CLASSES, INTENT_TYPE, RESULT_TYPE, RESULT_OUTCOMES, RECONCILIATION_OBSERVATIONS, SIDE_EFFECT_CLASSIFICATIONS } from './constants.js';

const nonEmpty = (x) => typeof x === 'string' && x.length > 0;
const positive = (x) => Number.isInteger(x) && x > 0;
const sha256 = (x) => typeof x === 'string' && /^[0-9a-f]{64}$/.test(x);
const plain = (x) => x && typeof x === 'object' && !Array.isArray(x) && (Object.getPrototypeOf(x) === Object.prototype || Object.getPrototypeOf(x) === null);
const flags = (extra = {}) => ({ newSideEffectAuthorized: false, retryAuthorized: false, workerDispatchAuthorized: false, architectTriggerAuthorized: false, ...extra });

function validateIntent(intent) {
  const defects = [];
  if (!plain(intent) || intent.type !== INTENT_TYPE) return ['INVALID_INTENT'];
  for (const field of ['intentId', 'missionId', 'messageId', 'dedupeKey', 'targetRef', 'bootId']) if (!nonEmpty(intent[field])) defects.push(`INVALID_INTENT_${field.toUpperCase()}`);
  if (!ACTION_CLASSES.includes(intent.actionClass)) defects.push('INVALID_INTENT_ACTION_CLASS');
  if (!sha256(intent.payloadSha256)) defects.push('INVALID_INTENT_PAYLOAD_HASH');
  if (!positive(intent.sequence)) defects.push('INVALID_INTENT_SEQUENCE');
  if (intent.leaseEpoch !== undefined && intent.leaseEpoch !== null && !positive(intent.leaseEpoch)) defects.push('INVALID_INTENT_LEASE_EPOCH');
  return defects;
}

function validateResult(result) {
  const defects = [];
  if (!plain(result) || result.type !== RESULT_TYPE) return ['INVALID_RESULT'];
  for (const field of ['intentId', 'missionId', 'messageId', 'dedupeKey', 'evidenceRef']) if (!nonEmpty(result[field])) defects.push(`INVALID_RESULT_${field.toUpperCase()}`);
  if (!RESULT_OUTCOMES.includes(result.outcome)) defects.push('INVALID_RESULT_OUTCOME');
  if (!positive(result.sequence)) defects.push('INVALID_RESULT_SEQUENCE');
  return defects;
}

function validateLedgerContainer(ledger) {
  if (!Array.isArray(ledger)) return ['LEDGER_NOT_ARRAY'];
  const defects = [];
  for (let index = 0; index < ledger.length; index += 1) if (!Object.hasOwn(ledger, index)) defects.push('LEDGER_SPARSE_ARRAY');
  return defects;
}

export function classifySideEffect({ ledger = [], dedupeKey, missionState = 'ACTIVE', circuitState = 'CLOSED' } = {}) {
  if (!MISSION_STATES.includes(missionState)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['INVALID_MISSION_STATE'] };
  if (!CIRCUIT_STATES.includes(circuitState)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['INVALID_CIRCUIT_STATE'] };
  const containerDefects = validateLedgerContainer(ledger);
  if (containerDefects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: containerDefects };
  if (!nonEmpty(dedupeKey)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['INVALID_DEDUPE_KEY'] };
  const intents = ledger.filter((x) => x?.type === INTENT_TYPE && x.dedupeKey === dedupeKey);
  const results = ledger.filter((x) => x?.type === RESULT_TYPE && x.dedupeKey === dedupeKey);
  const defects = ledger.flatMap((x) => x?.type === INTENT_TYPE ? validateIntent(x) : x?.type === RESULT_TYPE ? validateResult(x) : ['INVALID_LEDGER_RECORD']);
  if (defects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: [...new Set(defects)] };
  const byIntent = new Map(intents.map((x) => [x.intentId, x]));
  if (new Set(intents.map((x) => x.intentId)).size > 1) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['MULTIPLE_INTENTS_FOR_DEDUPE_KEY'] };
  for (const result of results) {
    const intent = byIntent.get(result.intentId);
    if (!intent || result.missionId !== intent.missionId || result.messageId !== intent.messageId || result.dedupeKey !== intent.dedupeKey) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['RESULT_INTENT_MISMATCH'] };
  }
  if (new Set(intents.map((x) => x.intentId)).size !== intents.length) {
    const groups = new Map();
    for (const intent of intents) groups.set(intent.intentId, [...(groups.get(intent.intentId) ?? []), JSON.stringify(intent)]);
    if ([...groups.values()].some((x) => new Set(x).size > 1)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['CONFLICTING_INTENTS'] };
  }
  const definitive = results.filter((x) => x.outcome === 'APPLIED' || x.outcome === 'NOT_APPLIED');
  if (new Set(definitive.map((x) => x.outcome)).size > 1) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['CONFLICTING_RESULTS'] };
  const pause = missionState === 'PAUSED_BY_RONY';
  const open = circuitState === 'OPEN';
  if (pause) return { classification: SIDE_EFFECT_CLASSIFICATIONS.RECONCILIATION_REQUIRED, ...flags(), reasonCodes: ['PAUSED_BY_RONY'] };
  if (open) return { classification: SIDE_EFFECT_CLASSIFICATIONS.CIRCUIT_OPEN, ...flags(), reasonCodes: ['CIRCUIT_OPEN'] };
  if (definitive[0]?.outcome === 'APPLIED') return { classification: SIDE_EFFECT_CLASSIFICATIONS.ALREADY_COMPLETED, ...flags(), reasonCodes: [] };
  if (definitive[0]?.outcome === 'NOT_APPLIED') return { classification: SIDE_EFFECT_CLASSIFICATIONS.KNOWN_NOT_APPLIED, ...flags(), reasonCodes: [] };
  if (intents.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.RECONCILIATION_REQUIRED, ...flags(), reasonCodes: ['UNRESOLVED_INTENT'] };
  return { classification: SIDE_EFFECT_CLASSIFICATIONS.READY_TO_INTENT, ...flags({ newSideEffectAuthorized: true }), reasonCodes: [] };
}

export function planSideEffectIntent({ ledger = [], intent, missionState = 'ACTIVE', circuitState = 'CLOSED' } = {}) {
  if (!MISSION_STATES.includes(missionState)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['INVALID_MISSION_STATE'] };
  if (!CIRCUIT_STATES.includes(circuitState)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['INVALID_CIRCUIT_STATE'] };
  const containerDefects = validateLedgerContainer(ledger);
  if (containerDefects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: containerDefects };
  const defects = validateIntent(intent);
  if (defects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: defects };
  const prior = classifySideEffect({ ledger, dedupeKey: intent.dedupeKey, missionState, circuitState });
  if (prior.classification !== SIDE_EFFECT_CLASSIFICATIONS.READY_TO_INTENT) return prior;
  return { classification: SIDE_EFFECT_CLASSIFICATIONS.READY_TO_INTENT, ...flags({ newSideEffectAuthorized: true }), intent: structuredClone(intent), reasonCodes: [] };
}

export function applySideEffectResult({ ledger = [], intent, result } = {}) {
  const containerDefects = validateLedgerContainer(ledger);
  if (containerDefects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: containerDefects };
  const intentDefects = validateIntent(intent);
  const resultDefects = validateResult(result);
  if (intentDefects.length || resultDefects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: [...intentDefects, ...resultDefects] };
  if (result.intentId !== intent.intentId || result.missionId !== intent.missionId || result.messageId !== intent.messageId || result.dedupeKey !== intent.dedupeKey) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['RESULT_INTENT_MISMATCH'] };
  const prior = classifySideEffect({ ledger, dedupeKey: intent.dedupeKey });
  if (prior.classification === SIDE_EFFECT_CLASSIFICATIONS.ALREADY_COMPLETED || prior.classification === SIDE_EFFECT_CLASSIFICATIONS.KNOWN_NOT_APPLIED || prior.classification === SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED) return prior;
  return { classification: result.outcome === 'APPLIED' ? SIDE_EFFECT_CLASSIFICATIONS.ALREADY_COMPLETED : SIDE_EFFECT_CLASSIFICATIONS.KNOWN_NOT_APPLIED, ...flags(), ledger: [...ledger.map(structuredClone), structuredClone(intent), structuredClone(result)], reasonCodes: [] };
}

export function reconcileSideEffect({ ledger = [], dedupeKey, observation } = {}) {
  if (!RECONCILIATION_OBSERVATIONS.includes(observation)) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: ['INVALID_OBSERVATION'] };
  const containerDefects = validateLedgerContainer(ledger);
  if (containerDefects.length) return { classification: SIDE_EFFECT_CLASSIFICATIONS.FAIL_CLOSED, ...flags(), reasonCodes: containerDefects };
  const current = classifySideEffect({ ledger, dedupeKey });
  if (![SIDE_EFFECT_CLASSIFICATIONS.RECONCILIATION_REQUIRED].includes(current.classification)) return current;
  if (observation === 'APPLIED') return { classification: SIDE_EFFECT_CLASSIFICATIONS.RECONCILED_APPLIED, ...flags(), reasonCodes: ['READ_ONLY_RECONCILIATION'] };
  if (observation === 'NOT_APPLIED') return { classification: SIDE_EFFECT_CLASSIFICATIONS.RECONCILED_NOT_APPLIED, ...flags(), reasonCodes: ['READ_ONLY_RECONCILIATION'] };
  return { classification: SIDE_EFFECT_CLASSIFICATIONS.RECONCILIATION_REQUIRED, ...flags(), reasonCodes: ['OBSERVATION_UNKNOWN'] };
}

export { validateIntent, validateResult };
