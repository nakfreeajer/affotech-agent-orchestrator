export const PROTOCOL_VERSION = '0.1';

export const CLASSIFICATIONS = Object.freeze({
  HEALTHY: 'PASS',
  FAIL_CLOSED: 'FAIL_CLOSED',
  RECONCILIATION_REQUIRED: 'RECONCILIATION_REQUIRED'
});

export const MISSION_STATES = Object.freeze(['ACTIVE', 'PAUSED_BY_RONY', 'RECONCILIATION_REQUIRED', 'COMPLETE']);
export const CIRCUIT_STATES = Object.freeze(['CLOSED', 'OPEN']);
export const ROLES = Object.freeze(['Architect', 'Executor', 'Curator', 'Orchestrator']);
export const MESSAGE_STATUSES = Object.freeze(['OPEN', 'RESULT', 'CONSUMED']);
