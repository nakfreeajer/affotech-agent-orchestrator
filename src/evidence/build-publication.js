import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { EVIDENCE_PROJECT, EVIDENCE_ROOT_NAME, PUBLICATION_SCHEMA_VERSION, RESULTS, ROLES } from './constants.js';

const hash = (bytes) => crypto.createHash('sha256').update(bytes).digest('hex');
const cleanName = (value) => path.basename(value);
const rel = (...parts) => parts.join('/');
const stableJson = (value) => `${JSON.stringify(value)}\n`;
const isInside = (root, candidate) => candidate === root || candidate.startsWith(`${root}${path.sep}`);
const isSha256 = (value) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const isPublicationId = (value) => typeof value === 'string' && /^PUB-[0-9a-f]{32}$/.test(value);

export function validateRelativePath(value, prefix, exactBasename = false) {
  if (typeof value !== 'string' || value.length === 0 || value.includes('\\') || path.posix.isAbsolute(value)) return false;
  const parts = value.split('/');
  if (parts.some((part) => part === '.' || part === '..' || part.length === 0)) return false;
  if (path.posix.normalize(value) !== value || !value.startsWith(`${prefix}/`)) return false;
  if (exactBasename && parts.length !== prefix.split('/').length + 1) return false;
  return true;
}

export async function assertExternalRoot(repositoryRoot, driveMyDriveRoot, externalEvidenceRoot) {
  const repo = await fs.realpath(path.resolve(repositoryRoot));
  const drive = await fs.realpath(path.resolve(driveMyDriveRoot));
  const external = await fs.realpath(path.resolve(externalEvidenceRoot));
  const expected = path.join(drive, EVIDENCE_ROOT_NAME);
  if (path.parse(external).root === external || isInside(repo, external) || external !== expected || /affotech-system-v2-hybrid/i.test(external) || external === drive) throw new Error('invalid external evidence root');
  return external;
}

async function readRegular(filePath, authorizedRoot) {
  const lexical = path.resolve(filePath);
  const realRoot = await fs.realpath(authorizedRoot);
  const realTarget = await fs.realpath(lexical);
  if (!isInside(realRoot, realTarget)) throw new Error(`realpath escapes authorized evidence root: ${filePath}`);
  const stat = await fs.lstat(lexical);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`not a regular file: ${filePath}`);
  return { bytes: await fs.readFile(lexical), realTarget };
}

export function validateReceipt(receipt) {
  if (!receipt || receipt.schemaVersion !== PUBLICATION_SCHEMA_VERSION || !isPublicationId(receipt.publicationId) || !isSha256(receipt?.sourceReport?.sha256) || !Number.isInteger(receipt?.sourceReport?.size) || !Array.isArray(receipt.artifacts) || receipt.publicationStatus !== 'PUBLISHED' || receipt.evidenceProject !== EVIDENCE_PROJECT || !ROLES.includes(receipt.role) || !RESULTS.includes(receipt.result) || typeof receipt.milestone !== 'string' || typeof receipt.sourceReport.externalRelativePath !== 'string') return false;
  if (!validateRelativePath(receipt.sourceReport.externalRelativePath, `reports/${receipt.role}`, true)) return false;
  return receipt.artifacts.every((artifact) => isSha256(artifact?.sha256) && Number.isInteger(artifact?.size) && typeof artifact?.externalRelativePath === 'string' && validateRelativePath(artifact.externalRelativePath, `artifacts/${receipt.publicationId}`, true));
}

export function validatePointer(pointer, roleOptions) {
  const options = typeof roleOptions === 'string' ? { expectedRole: roleOptions } : (roleOptions ?? {});
  const allowedRoles = options.allowedRoles ?? (options.expectedRole ? [options.expectedRole] : []);
  return Boolean(pointer && pointer.schemaVersion === PUBLICATION_SCHEMA_VERSION && isPublicationId(pointer.publicationId) && pointer.evidenceProject === EVIDENCE_PROJECT && allowedRoles.includes(pointer.role) && typeof pointer.milestone === 'string' && RESULTS.includes(pointer.result) && isSha256(pointer.reportSha256) && typeof pointer.reportRelativePath === 'string' && validateRelativePath(pointer.reportRelativePath, `reports/${pointer.role}`, true) && typeof pointer.receiptRelativePath === 'string' && pointer.receiptRelativePath === `receipts/${pointer.publicationId}.json` && validateRelativePath(pointer.receiptRelativePath, 'receipts', true));
}

export async function buildPublication({ repositoryRoot, driveMyDriveRoot, externalEvidenceRoot, reportPath, role, milestone, result, artifacts = [] }) {
  if (!ROLES.includes(role) || !RESULTS.includes(result) || typeof milestone !== 'string' || milestone.length === 0 || typeof driveMyDriveRoot !== 'string') throw new Error('invalid publication metadata');
  const repo = await fs.realpath(path.resolve(repositoryRoot));
  const external = await assertExternalRoot(repo, driveMyDriveRoot, externalEvidenceRoot);
  const report = path.resolve(reportPath);
  const allowedReportRoot = path.join(repo, '.agent-work', 'reports', role);
  const reportData = await readRegular(report, allowedReportRoot);
  const sourceReport = { externalRelativePath: rel('reports', role, `${cleanName(report)}--${hash(reportData.bytes).slice(0, 16)}${path.extname(report)}`), sha256: hash(reportData.bytes), size: reportData.bytes.length };
  const artifactModels = [];
  for (const artifactPath of artifacts) {
    const artifact = path.resolve(artifactPath);
    const outbox = path.join(repo, '.agent-work', 'outbox');
    const data = await readRegular(artifact, outbox);
    artifactModels.push({ sourcePath: artifact, basename: cleanName(artifact), sha256: hash(data.bytes), size: data.bytes.length });
  }
  const identity = { evidenceProject: EVIDENCE_PROJECT, role, milestone, result, reportBasename: cleanName(report), artifactHashes: artifactModels.map((a) => a.sha256).sort() };
  const publicationId = `PUB-${hash(Buffer.from(JSON.stringify(identity), 'utf8')).slice(0, 32)}`;
  const receiptRelativePath = rel('receipts', `${publicationId}.json`);
  const receipt = { schemaVersion: PUBLICATION_SCHEMA_VERSION, publicationId, evidenceProject: EVIDENCE_PROJECT, role, milestone, result, sourceReport, artifacts: artifactModels.map((a) => ({ externalRelativePath: rel('artifacts', publicationId, a.basename), sha256: a.sha256, size: a.size })), publicationStatus: 'PUBLISHED' };
  const pointer = { schemaVersion: PUBLICATION_SCHEMA_VERSION, publicationId, evidenceProject: EVIDENCE_PROJECT, role, milestone, result, reportRelativePath: sourceReport.externalRelativePath, reportSha256: sourceReport.sha256, receiptRelativePath };
  return { repositoryRoot: repo, driveMyDriveRoot: path.resolve(driveMyDriveRoot), externalEvidenceRoot: external, publicationId, report: { sourcePath: report, bytes: reportData.bytes, ...sourceReport }, artifacts: artifactModels, receipt, receiptBytes: Buffer.from(stableJson(receipt), 'utf8'), pointers: { role: { relativePath: rel('current', `LATEST_${role.toUpperCase()}_REPORT.json`), model: pointer }, milestone: { relativePath: rel('current', 'LATEST_MILESTONE.json'), model: pointer } } };
}
