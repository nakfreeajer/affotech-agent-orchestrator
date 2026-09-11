import { validateControlObject } from '../protocol/validate-control-object.js';
import { ARCHITECT_ROLE, TERMINAL_WORKER_OUTCOMES, TRANSITION_CLASSIFICATIONS, WORKER_LIFECYCLE_COMPLETED, WORKER_ROLES } from './constants.js';

const isNonEmptyString = (value) => typeof value === 'string' && value.length > 0;
const isPlainObject = (value) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
};

const flags = (architectTriggerEligible = false) => ({
  architectTriggerEligible,
  workerDispatchAuthorized: false,
  mutationAuthorized: false,
  browserActionAuthorized: false,
  retryAuthorized: false
});

const unchanged = (classification, controlBundle, defects = []) => ({
  classification,
  transitionApplied: false,
  architectReviewRequired: false,
  ...flags(false),
  state: structuredClone(controlBundle),
  defects
});

function validateRoleResult(controlBundle, roleResult, assignment) {
  const defects = [];
  const fail = (code, detail) => defects.push({ code, detail });
  if (!roleResult || typeof roleResult !== 'object' || Array.isArray(roleResult)) return [{ code: 'INVALID_ROLE_RESULT', detail: 'roleResult must be an object' }];
  for (const field of ['messageId', 'missionId', 'milestone', 'senderRole', 'recipientRole', 'status', 'workerLifecycle', 'workerOutcome', 'mutationCounts']) {
    if (!Object.hasOwn(roleResult, field)) fail('MISSING_ROLE_RESULT_FIELD', field);
  }
  if (!isNonEmptyString(roleResult.messageId)) fail('INVALID_RESULT_MESSAGE_ID', roleResult.messageId);
  if (controlBundle.messages.some((message) => message.messageId === roleResult.messageId)) fail('DUPLICATE_RESULT_MESSAGE_ID', roleResult.messageId);
  if (roleResult.parentMessageId !== assignment.messageId) fail('INVALID_RESULT_PARENT', roleResult.parentMessageId);
  if (roleResult.missionId !== controlBundle.mission.missionId) fail('INVALID_RESULT_MISSION', roleResult.missionId);
  if (roleResult.milestone !== controlBundle.mission.currentMilestone || roleResult.milestone !== assignment.milestone) fail('INVALID_RESULT_MILESTONE', roleResult.milestone);
  if (roleResult.senderRole !== controlBundle.mission.currentRole || !WORKER_ROLES.includes(roleResult.senderRole)) fail('INVALID_RESULT_SENDER', roleResult.senderRole);
  if (roleResult.recipientRole !== ARCHITECT_ROLE) fail('INVALID_RESULT_RECIPIENT', roleResult.recipientRole);
  if (roleResult.status !== 'RESULT') fail('INVALID_RESULT_STATUS', roleResult.status);
  if (roleResult.workerLifecycle !== WORKER_LIFECYCLE_COMPLETED) fail('INVALID_WORKER_LIFECYCLE', roleResult.workerLifecycle);
  if (!TERMINAL_WORKER_OUTCOMES.includes(roleResult.workerOutcome)) fail('INVALID_WORKER_OUTCOME', roleResult.workerOutcome);
  if (!isPlainObject(roleResult.mutationCounts)) fail('INVALID_MUTATION_COUNTS', 'must be a plain object');
  else for (const [key, value] of Object.entries(roleResult.mutationCounts)) if (!Number.isInteger(value) || value < 0) fail('INVALID_MUTATION_COUNT', key);
  return defects;
}

export function applyRoleResultTransition(controlBundle, roleResult) {
  const controlResult = validateControlObject(controlBundle);
  if (controlResult.classification !== 'PASS') return unchanged(controlResult.classification, controlBundle, controlResult.defects);
  if (!WORKER_ROLES.includes(controlBundle.mission.currentRole)) return unchanged(TRANSITION_CLASSIFICATIONS.FAIL_CLOSED, controlBundle, [{ code: 'INVALID_CURRENT_WORKER_ROLE', detail: controlBundle.mission.currentRole }]);
  const assignments = controlBundle.messages.filter((message) => message.status === 'OPEN' && message.recipientRole === controlBundle.mission.currentRole);
  if (assignments.length !== 1) return unchanged(TRANSITION_CLASSIFICATIONS.FAIL_CLOSED, controlBundle, [{ code: assignments.length === 0 ? 'NO_CURRENT_OPEN_ASSIGNMENT' : 'MULTIPLE_CURRENT_OPEN_ASSIGNMENTS', detail: assignments.length }]);
  const assignment = assignments[0];
  const resultDefects = validateRoleResult(controlBundle, roleResult, assignment);
  if (resultDefects.length) return unchanged(TRANSITION_CLASSIFICATIONS.FAIL_CLOSED, controlBundle, resultDefects);

  const state = structuredClone(controlBundle);
  const consumed = state.messages.find((message) => message.messageId === assignment.messageId);
  consumed.status = 'CONSUMED';
  state.messages.push(structuredClone(roleResult));
  state.mission.currentRole = ARCHITECT_ROLE;
  const architectTriggerEligible = state.mission.missionState === 'ACTIVE' && state.mission.circuitState === 'CLOSED';
  return {
    classification: TRANSITION_CLASSIFICATIONS.APPLIED,
    transitionApplied: true,
    architectReviewRequired: true,
    ...flags(architectTriggerEligible),
    workerOutcome: roleResult.workerOutcome,
    reviewMessageId: roleResult.messageId,
    reviewedAssignmentMessageId: assignment.messageId,
    state
  };
}
