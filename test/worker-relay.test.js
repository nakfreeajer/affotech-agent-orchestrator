import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { resolveGithubDispatchLocator } from '../src/browser-relay/worker-relay.js';

const locator = 'execute github dispatch nakfreeajer/affotech-agent-orchestrator-evidence DISPATCH-000034';
const prompt = Buffer.from('canonical prompt bytes\n', 'utf8');
const promptSha = crypto.createHash('sha256').update(prompt).digest('hex');

function git(cwd, ...args) { return execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf8' }).trim(); }
function fixture(overrides = {}) {
  const root = mkdtempSync(path.join(os.tmpdir(), 'worker-relay-'));
  const write = (p, value) => { const target = path.join(root, p); mkdirSync(path.dirname(target), { recursive: true }); writeFileSync(target, JSON.stringify(value)); };
  const accepted = { pointerKind: 'EXECUTOR_ACCEPTED', publicationId: 'GH-PUB-030-GITHUB-DISPATCH-LOCATOR-000001', accepted: true, requiresArchitectDecision: false };
  const dispatch = { evidenceProject: 'affotech-agent-orchestrator', recordType: 'DISPATCH', dispatchId: 'DISPATCH-000034', messageId: 'ORCH-000034', canonicalPromptPath: 'evidence/prompts/ORCH-000034.md', canonicalPromptSha256: promptSha, targetRole: 'executor', dispatchState: 'MANUAL_TRIGGER_REQUIRED' };
  const latestDispatch = { pointerKind: 'DISPATCH', ...dispatch };
  const latestPrompt = { pointerKind: 'ARCHITECT_PROMPT', messageId: 'ORCH-000034', promptPath: dispatch.canonicalPromptPath, promptSha256: promptSha, targetRole: 'executor' };
  const decision = { pointerKind: 'ARCHITECT_DECISION', decision: 'ACCEPTED', nextCanonicalMessageId: 'ORCH-000034', acceptedTransportAnchor: accepted.publicationId };
  write('evidence/dispatches/DISPATCH-000034/DISPATCH.json', { ...dispatch, ...overrides.dispatch });
  write('evidence/current/LATEST_DISPATCH.json', { ...latestDispatch, ...overrides.latestDispatch });
  write('evidence/current/LATEST_ARCHITECT_PROMPT.json', { ...latestPrompt, ...overrides.latestPrompt });
  write('evidence/current/LATEST_ARCHITECT_DECISION.json', { ...decision, ...overrides.decision });
  write('evidence/current/LATEST_EXECUTOR_ACCEPTED.json', { ...accepted, ...overrides.accepted });
  mkdirSync(path.join(root, 'evidence/prompts'), { recursive: true }); writeFileSync(path.join(root, 'evidence/prompts/ORCH-000034.md'), prompt);
  git(root, 'init'); git(root, 'config', 'user.email', 'test@example.invalid'); git(root, 'config', 'user.name', 'Test'); git(root, 'add', '.'); git(root, 'commit', '-m', 'fixture'); git(root, 'branch', '-M', 'main');
  return root;
}
function run(root, extra = {}) { return resolveGithubDispatchLocator({ repoRoot: root, ref: 'main', locatorText: locator, workerRole: 'executor', ...extra }); }
function rejects(root, extra = {}) { assert.throws(() => run(root, extra), /GITHUB_DISPATCH_LOCATOR_REJECTED/); }

test('resolves one captured Git ref and returns exact prompt bytes', () => { const root = fixture(); try { const result = run(root); assert.equal(result.dispatchId, 'DISPATCH-000034'); assert.deepEqual(result.canonicalPromptBytes, prompt); assert.equal(result.canonicalPromptSha256, promptSha); } finally { rmSync(root, { recursive: true, force: true }); } });
test('rejects malformed, wrong-repository, and wrong-role locators', () => { const root = fixture(); try { rejects(root, { locatorText: 'bad locator' }); rejects(root, { locatorText: 'execute github dispatch wrong/repo DISPATCH-000034' }); rejects(root, { workerRole: 'curator' }); } finally { rmSync(root, { recursive: true, force: true }); } });
test('rejects pointer, authorization, and prompt hash mismatches', () => { for (const overrides of [{ latestDispatch: { dispatchId: 'DISPATCH-000033' } }, { latestPrompt: { messageId: 'ORCH-000033' } }, { decision: { nextCanonicalMessageId: 'ORCH-000033' } }, { dispatch: { canonicalPromptSha256: '0'.repeat(64) } }]) { const root = fixture(overrides); try { rejects(root); } finally { rmSync(root, { recursive: true, force: true }); } } });
test('rejects missing Git objects and never executes the prompt', () => { const root = fixture(); try { rmSync(path.join(root, 'evidence/prompts/ORCH-000034.md')); git(root, 'add', '-u'); git(root, 'commit', '-m', 'remove prompt'); rejects(root); assert.equal(readFileSync(path.join(root, '.git/HEAD'), 'utf8').startsWith('ref:'), true); } finally { rmSync(root, { recursive: true, force: true }); } });
test('uses a bounded Git argv and disables shell execution', () => { const source = readFileSync(new URL('../src/browser-relay/worker-relay.js', import.meta.url), 'utf8'); assert.match(source, /execFileSync\('git', \['-C', repoRoot, 'show'/); assert.match(source, /shell: false/); assert.doesNotMatch(source, /shell:\s*true/); });
test('captures a single commit ref before object reads', () => { const source = readFileSync(new URL('../src/browser-relay/worker-relay.js', import.meta.url), 'utf8'); assert.match(source, /rev-parse.*verify.*\^\{commit\}/s); });
test('does not put prompt content in source command arguments or Base64', () => { const source = readFileSync(new URL('../src/browser-relay/worker-relay.js', import.meta.url), 'utf8'); assert.doesNotMatch(source, /base64/i); assert.doesNotMatch(source, /canonicalPromptBytes.*execFileSync/s); });
test('rejects a wrong current dispatch pointer', () => { const root = fixture({ latestDispatch: { messageId: 'ORCH-000033' } }); try { rejects(root); } finally { rmSync(root, { recursive: true, force: true }); } });
test('rejects an Architect prompt pointer mismatch', () => { const root = fixture({ latestPrompt: { promptPath: 'evidence/prompts/ORCH-000033.md' } }); try { rejects(root); } finally { rmSync(root, { recursive: true, force: true }); } });
test('rejects an unauthorized Architect decision', () => { const root = fixture({ decision: { decision: 'NO NEW REPORT' } }); try { rejects(root); } finally { rmSync(root, { recursive: true, force: true }); } });
test('rejects target-role mismatch in the current dispatch', () => { const root = fixture({ latestDispatch: { targetRole: 'curator' } }); try { rejects(root); } finally { rmSync(root, { recursive: true, force: true }); } });
test('rejects a missing ref', () => { const root = fixture(); try { assert.throws(() => run(root, { ref: 'missing-ref' }), /GITHUB_DISPATCH_LOCATOR_REJECTED/); } finally { rmSync(root, { recursive: true, force: true }); } });
test('does not contact browser ports or execute canonical prompt', () => { const root = fixture(); try { const result = run(root); assert.equal(result.messageId, 'ORCH-000034'); assert.equal(result.canonicalPromptBytes.toString(), prompt.toString()); } finally { rmSync(root, { recursive: true, force: true }); } });
test('supports an explicitly bounded output only through the CLI contract', () => { const source = readFileSync(new URL('../src/browser-relay/worker-relay.js', import.meta.url), 'utf8'); assert.match(source, /--repo-root|repo-root/); assert.match(source, /writeFileSync\(path\.resolve\(args\.output\)/); });
test('keeps legacy full-prompt delivery data outside resolver inputs', () => { const source = readFileSync(new URL('../src/browser-relay/worker-relay.js', import.meta.url), 'utf8'); assert.doesNotMatch(source, /worker-deliveries|deliveryPayload/); });
test('returns a structured resolution without automatic prompt execution', () => { const root = fixture(); try { const result = run(root); assert.equal(typeof result.ref, 'string'); assert.equal(result.canonicalPromptPath, 'evidence/prompts/ORCH-000034.md'); assert.equal(result.canonicalPromptBytes instanceof Buffer, true); } finally { rmSync(root, { recursive: true, force: true }); } });
