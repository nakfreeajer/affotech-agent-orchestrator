import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const LOCATOR = /^execute github dispatch ([^\s/]+\/[^\s/]+) (DISPATCH-[0-9]{6})$/;
const SHA256 = /^[0-9a-f]{64}$/;
const DISPATCH_PATH = (id) => `evidence/dispatches/${id}/DISPATCH.json`;

function fail(message) {
  throw new Error(`GITHUB_DISPATCH_LOCATOR_REJECTED: ${message}`);
}

function readObject(repoRoot, ref, relativePath) {
  try {
    return execFileSync('git', ['-C', repoRoot, 'show', `${ref}:${relativePath}`], {
      shell: false,
      encoding: null,
      maxBuffer: 4 * 1024 * 1024
    });
  } catch (error) {
    fail(`missing Git object ${relativePath}: ${error.message}`);
  }
}

function readJson(repoRoot, ref, relativePath) {
  try {
    return JSON.parse(readObject(repoRoot, ref, relativePath).toString('utf8'));
  } catch (error) {
    if (error.message.startsWith('GITHUB_DISPATCH_LOCATOR_REJECTED:')) throw error;
    fail(`invalid JSON object ${relativePath}: ${error.message}`);
  }
}

function captureRef(repoRoot, ref) {
  try {
    return execFileSync('git', ['-C', repoRoot, 'rev-parse', '--verify', `${ref}^{commit}`], {
      shell: false,
      encoding: 'utf8',
      maxBuffer: 1024
    }).trim();
  } catch (error) {
    fail(`cannot capture Git ref ${ref}: ${error.message}`);
  }
}

function parseLocator(locatorText) {
  if (typeof locatorText !== 'string') fail('locator must be a string');
  const match = LOCATOR.exec(locatorText);
  if (!match) fail('locator grammar is invalid');
  return { repository: match[1], dispatchId: match[2] };
}

function requireEqual(label, actual, expected) {
  if (actual !== expected) fail(`${label} mismatch`);
}

function verifyPointerShape(pointer, kind) {
  if (!pointer || pointer.pointerKind !== kind) fail(`invalid ${kind} pointer`);
}

export function resolveGithubDispatchLocator({ repoRoot, ref = 'origin/main', locatorText, locator, workerRole } = {}) {
  const text = locatorText ?? locator;
  if (typeof repoRoot !== 'string' || repoRoot.length === 0) fail('repoRoot is required');
  if (typeof workerRole !== 'string' || workerRole.length === 0) fail('workerRole is required');
  const { repository, dispatchId } = parseLocator(text);
  const capturedRef = captureRef(repoRoot, ref);
  const dispatch = readJson(repoRoot, capturedRef, DISPATCH_PATH(dispatchId));
  const latestDispatch = readJson(repoRoot, capturedRef, 'evidence/current/LATEST_DISPATCH.json');
  const latestPrompt = readJson(repoRoot, capturedRef, 'evidence/current/LATEST_ARCHITECT_PROMPT.json');
  const decision = readJson(repoRoot, capturedRef, 'evidence/current/LATEST_ARCHITECT_DECISION.json');
  const accepted = readJson(repoRoot, capturedRef, 'evidence/current/LATEST_EXECUTOR_ACCEPTED.json');

  requireEqual('evidence repository', dispatch.evidenceProject, 'affotech-agent-orchestrator');
  requireEqual('locator repository', repository, 'nakfreeajer/affotech-agent-orchestrator-evidence');
  requireEqual('dispatch identity', dispatch.dispatchId, dispatchId);
  requireEqual('current dispatch id', latestDispatch.dispatchId, dispatchId);
  requireEqual('dispatch message id', dispatch.messageId, latestDispatch.messageId);
  requireEqual('target role', dispatch.targetRole, workerRole);
  requireEqual('current target role', latestDispatch.targetRole, workerRole);
  requireEqual('dispatch prompt path', dispatch.canonicalPromptPath, latestDispatch.canonicalPromptPath);
  requireEqual('dispatch prompt hash', dispatch.canonicalPromptSha256, latestDispatch.canonicalPromptSha256);
  requireEqual('prompt message id', latestPrompt.messageId, dispatch.messageId);
  requireEqual('prompt path', latestPrompt.promptPath, dispatch.canonicalPromptPath);
  requireEqual('prompt hash', latestPrompt.promptSha256, dispatch.canonicalPromptSha256);
  requireEqual('architect prompt target role', latestPrompt.targetRole, workerRole);
  if (decision.decision !== 'ACCEPTED' || decision.nextCanonicalMessageId !== dispatch.messageId) fail('Architect decision does not authorize current message');
  if (decision.acceptedTransportAnchor !== accepted.publicationId || accepted.accepted !== true || accepted.requiresArchitectDecision !== false) fail('accepted transport contract is incompatible');

  const canonicalPromptBytes = readObject(repoRoot, capturedRef, dispatch.canonicalPromptPath);
  const observedSha256 = crypto.createHash('sha256').update(canonicalPromptBytes).digest('hex');
  if (!SHA256.test(dispatch.canonicalPromptSha256) || observedSha256 !== dispatch.canonicalPromptSha256) fail('canonical prompt SHA-256 mismatch');
  return Object.freeze({ dispatchId, messageId: dispatch.messageId, canonicalPromptPath: dispatch.canonicalPromptPath, canonicalPromptSha256: dispatch.canonicalPromptSha256, workerRole, ref: capturedRef, canonicalPromptBytes });
}

function cliArgs(argv) {
  const values = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith('--') || i + 1 >= argv.length) fail('CLI arguments must be bounded option/value pairs');
    values[key.slice(2)] = argv[++i];
  }
  return values;
}

export function runCli(argv = process.argv.slice(2), output = process.stdout) {
  const args = cliArgs(argv);
  const resolution = resolveGithubDispatchLocator({ repoRoot: args['repo-root'], ref: args.ref ?? 'origin/main', locatorText: args.locator, workerRole: args['worker-role'] });
  if (args.output) writeFileSync(path.resolve(args.output), resolution.canonicalPromptBytes);
  else output.write(resolution.canonicalPromptBytes);
  return resolution;
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  try {
    runCli();
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
