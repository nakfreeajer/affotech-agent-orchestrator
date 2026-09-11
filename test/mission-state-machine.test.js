import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { applyRoleResultTransition } from '../src/state-machine/mission-state-machine.js';

const root = path.resolve('test/state-machine-fixtures');
const fixture = (name) => JSON.parse(fs.readFileSync(path.join(root, name, 'case.json'), 'utf8'));
const apply = (name) => { const input = fixture(name); return { input, output: applyRoleResultTransition(input.controlBundle, input.roleResult) }; };
const invalidCase = (name) => {
  const input = fixture('executor-pass');
  const assignment = input.controlBundle.messages[0];
  if (name === 'wrong-parent') input.roleResult.parentMessageId = 'OTHER';
  if (name === 'wrong-mission') input.roleResult.missionId = 'OTHER';
  if (name === 'wrong-milestone') input.roleResult.milestone = 'ORCH.P0.9X';
  if (name === 'wrong-sender') input.roleResult.senderRole = 'Curator';
  if (name === 'wrong-recipient') input.roleResult.recipientRole = 'Executor';
  if (name === 'duplicate-result-id') input.roleResult.messageId = assignment.messageId;
  if (name === 'no-current-open') input.controlBundle.messages = [];
  if (name === 'multiple-current-open') input.controlBundle.messages.push({ ...assignment, messageId: 'MSG-002' });
  if (name === 'invalid-worker-lifecycle') input.roleResult.workerLifecycle = 'FAILED';
  if (name === 'unsupported-worker-outcome') input.roleResult.workerOutcome = 'RETRY';
  if (name === 'negative-mutation-count') input.roleResult.mutationCounts.source = -1;
  if (name === 'non-integer-mutation-count') input.roleResult.mutationCounts.source = 1.5;
  if (name === 'invalid-base-state') input.controlBundle.mission.missionState = 'BOGUS';
  if (name === 'unresolved-base-state') input.controlBundle.intents = [{ type: 'INTENT', intentId: 'INT-001' }];
  return input;
};
const validNames = ['executor-pass', 'executor-blocked-zero-mutation', 'curator-pass', 'paused-executor-pass', 'circuit-open-executor-pass'];

test('Executor PASS routes to Architect review', () => { const { output } = apply('executor-pass'); assert.equal(output.classification, 'APPLIED'); assert.equal(output.transitionApplied, true); assert.equal(output.workerOutcome, 'PASS'); assert.equal(output.architectReviewRequired, true); assert.equal(output.architectTriggerEligible, true); assert.equal(output.state.mission.currentRole, 'Architect'); assert.equal(output.state.messages[0].status, 'CONSUMED'); assert.equal(output.state.messages.at(-1).status, 'RESULT'); assert.equal(output.missionAccepted, undefined); });
test('Executor BLOCKED with zero mutation remains reviewable', () => { const { output } = apply('executor-blocked-zero-mutation'); assert.equal(output.classification, 'APPLIED'); assert.equal(output.workerOutcome, 'BLOCKED'); assert.deepEqual(output.state.messages.at(-1).mutationCounts, { source: 0, test: 0, docs: 0 }); assert.equal(output.architectReviewRequired, true); assert.equal(output.architectTriggerEligible, true); assert.equal(output.retryAuthorized, false); });
test('Curator PASS has the same routing semantics', () => { const { output } = apply('curator-pass'); assert.equal(output.classification, 'APPLIED'); assert.equal(output.state.mission.currentRole, 'Architect'); assert.equal(output.state.messages[0].status, 'CONSUMED'); assert.equal(output.state.messages.at(-1).senderRole, 'Curator'); });
test('PAUSED_BY_RONY records result but disables trigger', () => { const { output } = apply('paused-executor-pass'); assert.equal(output.classification, 'APPLIED'); assert.equal(output.architectReviewRequired, true); assert.equal(output.architectTriggerEligible, false); assert.equal(output.workerDispatchAuthorized, false); });
test('OPEN circuit records result but disables trigger and preserves OPEN', () => { const { output } = apply('circuit-open-executor-pass'); assert.equal(output.classification, 'APPLIED'); assert.equal(output.architectTriggerEligible, false); assert.equal(output.state.mission.circuitState, 'OPEN'); });

function invalidResultTest(name) { test(`${name} fails closed`, () => { const input = invalidCase(name); const output = applyRoleResultTransition(input.controlBundle, input.roleResult); assert.equal(output.classification, 'FAIL_CLOSED'); assert.equal(output.transitionApplied, false); assert.equal(output.retryAuthorized, false); assert.equal(output.architectTriggerEligible, false); assert.deepEqual(output.state, input.controlBundle); }); }
for (const name of ['wrong-parent', 'wrong-mission', 'wrong-milestone', 'wrong-sender', 'wrong-recipient', 'duplicate-result-id', 'no-current-open', 'multiple-current-open', 'invalid-worker-lifecycle', 'unsupported-worker-outcome', 'negative-mutation-count', 'non-integer-mutation-count', 'invalid-base-state']) invalidResultTest(name);
test('unresolved P0.1A base state remains reconciliation-required', () => { const input = invalidCase('unresolved-base-state'); const output = applyRoleResultTransition(input.controlBundle, input.roleResult); assert.equal(output.classification, 'RECONCILIATION_REQUIRED'); assert.equal(output.transitionApplied, false); assert.deepEqual(output.state, input.controlBundle); });
test('transition is immutable and deterministic', () => { for (const name of validNames) { const input = fixture(name); const before = structuredClone(input.controlBundle); const first = applyRoleResultTransition(input.controlBundle, input.roleResult); const second = applyRoleResultTransition(input.controlBundle, input.roleResult); assert.deepEqual(input.controlBundle, before, name); assert.deepEqual(first, second, name); assert.equal(first.retryAuthorized, false, name); assert.equal(first.mutationAuthorized, false, name); assert.equal(first.browserActionAuthorized, false, name); } });
test('PASS is not acceptance and BLOCKED is not retry authorization', () => { for (const name of ['executor-pass', 'executor-blocked-zero-mutation']) { const { output } = apply(name); assert.equal(output.architectReviewRequired, true); assert.equal(output.retryAuthorized, false); assert.equal(output.workerDispatchAuthorized, false); assert.equal(output.mutationAuthorized, false); } });
test('milestone does not advance and result is appended unchanged', () => { for (const name of validNames) { const input = fixture(name); const { output } = apply(name); assert.equal(output.state.mission.currentMilestone, input.controlBundle.mission.currentMilestone); assert.deepEqual(output.state.messages.at(-1), input.roleResult); } });
