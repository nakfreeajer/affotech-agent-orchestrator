import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { doctorFile } from '../src/protocol/doctor.js';

const root = path.resolve('test/fixtures');
const fileFor = (name) => path.join(root, name, 'state.json');
const run = (name) => doctorFile(fileFor(name));
const expectClassification = (name, classification) => {
  const result = run(name);
  assert.equal(result.classification, classification, name);
  assert.equal(result.retryAuthorized, false, name);
};

test('healthy fixture passes with real canonical prompt SHA-256', () => {
  const state = JSON.parse(fs.readFileSync(fileFor('healthy'), 'utf8'));
  assert.equal(state.promptIntegrity.expectedSha256, crypto.createHash('sha256').update(state.promptIntegrity.canonicalPrompt, 'utf8').digest('hex'));
  expectClassification('healthy', 'PASS');
});

for (const name of ['missing-mission-id', 'duplicate-message-id', 'stale-architect-generation', 'prompt-hash-mismatch']) {
  test(`${name} fails closed`, () => expectClassification(name, 'FAIL_CLOSED'));
}

test('INTENT without RESULT requires reconciliation without retry authorization', () => expectClassification('intent-without-result', 'RECONCILIATION_REQUIRED'));

for (const name of ['invalid-parent-message', 'missing-sender-role', 'inconsistent-recipient-role', 'inconsistent-message-milestone', 'invalid-message-status', 'inconsistent-current-role', 'inconsistent-current-milestone', 'invalid-mission-state', 'invalid-circuit-state', 'canonical-prompt-tampered', 'hard-defect-with-unresolved-intent']) {
  test(`${name} fails closed`, () => expectClassification(name, 'FAIL_CLOSED'));
}

test('hard defect takes precedence over unresolved INTENT', () => {
  const result = run('hard-defect-with-unresolved-intent');
  assert.equal(result.classification, 'FAIL_CLOSED');
  assert.equal(result.retryAuthorized, false);
  assert.ok(result.defects.some((defect) => defect.code === 'INVALID_MISSION_STATE'));
  assert.ok(!result.defects.some((defect) => defect.code === 'INTENT_WITHOUT_RESULT'));
});

test('tampered canonical prompt cannot pass when stored hashes agree', () => {
  const result = run('canonical-prompt-tampered');
  assert.equal(result.classification, 'FAIL_CLOSED');
  assert.equal(result.retryAuthorized, false);
});

test('doctor does not mutate fixture files and repeated runs are deep-equivalent', () => {
  const names = ['healthy', 'intent-without-result', 'canonical-prompt-tampered', 'hard-defect-with-unresolved-intent'];
  const before = new Map(names.map((name) => [name, fs.readFileSync(fileFor(name))]));
  for (const name of names) assert.deepEqual(run(name), run(name));
  for (const [name, bytes] of before) assert.deepEqual(fs.readFileSync(fileFor(name)), bytes, name);
});
