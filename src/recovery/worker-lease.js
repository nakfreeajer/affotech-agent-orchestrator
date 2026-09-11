import { LEASE_CLASSIFICATIONS } from './constants.js';

const nonEmpty = (x) => typeof x === 'string' && x.length > 0;
const positive = (x) => Number.isInteger(x) && x > 0;
const time = (x) => Number.isInteger(x) && x >= 0;
const roles = ['Executor', 'Curator'];
const identityFields = ['missionId', 'messageId', 'role', 'workerInstanceId', 'bootId', 'leaseEpoch'];

function defects(lease) {
  const out = [];
  if (!lease || typeof lease !== 'object' || Array.isArray(lease)) return ['INVALID_LEASE'];
  for (const field of ['missionId', 'messageId', 'workerInstanceId', 'bootId']) if (!nonEmpty(lease[field])) out.push(`INVALID_LEASE_${field.toUpperCase()}`);
  if (!roles.includes(lease.role)) out.push('INVALID_LEASE_ROLE');
  if (!positive(lease.leaseEpoch)) out.push('INVALID_LEASE_EPOCH');
  for (const field of ['issuedAtMs', 'lastHeartbeatAtMs', 'leaseExpiresAtMs']) if (!time(lease[field])) out.push(`INVALID_LEASE_${field.toUpperCase()}`);
  if (time(lease.issuedAtMs) && time(lease.lastHeartbeatAtMs) && lease.lastHeartbeatAtMs < lease.issuedAtMs) out.push('HEARTBEAT_BEFORE_ISSUE');
  if (time(lease.lastHeartbeatAtMs) && time(lease.leaseExpiresAtMs) && lease.leaseExpiresAtMs <= lease.lastHeartbeatAtMs) out.push('LEASE_EXPIRES_BEFORE_HEARTBEAT');
  return out;
}

export function createWorkerLease(input = {}) {
  const d = defects(input);
  if (d.length) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: d };
  return { classification: LEASE_CLASSIFICATIONS.ACTIVE, lease: structuredClone(input), defects: [] };
}

export function renewWorkerLease(lease, heartbeat = {}) {
  const d = defects(lease);
  if (d.length || !time(heartbeat.nowMs)) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: [...d, 'INVALID_HEARTBEAT_TIME'] };
  for (const field of identityFields) if (heartbeat[field] !== lease[field]) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: ['FOREIGN_WORKER_HEARTBEAT'] };
  if (heartbeat.nowMs < lease.lastHeartbeatAtMs) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: ['NON_MONOTONIC_HEARTBEAT'] };
  if (heartbeat.nowMs >= lease.leaseExpiresAtMs) return { classification: LEASE_CLASSIFICATIONS.WORKER_LIVENESS_UNKNOWN, defects: ['EXPIRED_LEASE_CANNOT_REVIVE'] };
  const renewed = { ...lease, lastHeartbeatAtMs: heartbeat.nowMs, leaseExpiresAtMs: heartbeat.leaseExpiresAtMs ?? lease.leaseExpiresAtMs };
  const renewedDefects = defects(renewed);
  if (renewedDefects.length) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: renewedDefects };
  return { classification: LEASE_CLASSIFICATIONS.ACTIVE, lease: renewed, defects: [] };
}

export function evaluateWorkerLease(lease, nowMs) {
  const d = defects(lease);
  if (d.length || !time(nowMs)) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: [...d, ...(time(nowMs) ? [] : ['INVALID_NOW'])] };
  return { classification: nowMs < lease.leaseExpiresAtMs ? LEASE_CLASSIFICATIONS.ACTIVE : LEASE_CLASSIFICATIONS.WORKER_LIVENESS_UNKNOWN, safeToReplace: false, retryAuthorized: false, defects: [] };
}

export function validateWorkerFence(lease, worker = {}) {
  const d = defects(lease);
  if (d.length) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: d, retryAuthorized: false };
  if (worker.leaseEpoch < lease.leaseEpoch) return { classification: LEASE_CLASSIFICATIONS.FENCED_STALE_WORKER, defects: ['STALE_LEASE_EPOCH'], retryAuthorized: false };
  for (const field of ['missionId', 'messageId', 'role', 'workerInstanceId', 'bootId', 'leaseEpoch']) if (worker[field] !== lease[field]) return { classification: LEASE_CLASSIFICATIONS.FAIL_CLOSED, defects: [`FENCE_${field.toUpperCase()}_MISMATCH`], retryAuthorized: false };
  const evaluation = evaluateWorkerLease(lease, worker.nowMs);
  return { classification: evaluation.classification, authoritative: evaluation.classification === LEASE_CLASSIFICATIONS.ACTIVE, retryAuthorized: false, defects: evaluation.defects };
}

export { defects as validateWorkerLease };
