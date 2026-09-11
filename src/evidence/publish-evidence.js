import fs from 'node:fs/promises';
import path from 'node:path';
import { buildPublication, validatePointer, validateReceipt } from './build-publication.js';
import { EVIDENCE_PROJECT } from './constants.js';

const defaultIo = { mkdir: fs.mkdir, readFile: fs.readFile, writeFile: fs.writeFile, lstat: fs.lstat, rename: fs.rename };
const full = (root, relative) => path.join(root, ...relative.split('/'));
const jsonBytes = (value) => Buffer.from(`${JSON.stringify(value)}\n`, 'utf8');

async function writeImmutable(io, destination, bytes) {
  try {
    const existing = await io.readFile(destination);
    if (Buffer.compare(existing, bytes) === 0) return 'ALREADY_PUBLISHED';
    throw new Error(`immutable collision: ${destination}`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  try { await io.writeFile(destination, bytes, { flag: 'wx' }); }
  catch (error) {
    try { const reconciled = await io.readFile(destination); if (Buffer.compare(reconciled, bytes) === 0) return 'RECONCILED_PUBLISHED'; } catch {}
    if (error.code !== 'EEXIST') throw new Error(`RECONCILIATION_REQUIRED: ${error.message}`);
    throw error;
  }
  const readback = await io.readFile(destination);
  if (Buffer.compare(readback, bytes) !== 0) throw new Error(`readback mismatch: ${destination}`);
  return 'PUBLISHED';
}

async function readExistingPointer(io, destination, roleOptions) {
  let bytes;
  try { bytes = await io.readFile(destination); } catch (error) { if (error.code === 'ENOENT') return undefined; throw new Error('invalid existing current pointer'); }
  let existing;
  try { existing = JSON.parse(bytes.toString('utf8')); } catch { throw new Error('invalid existing current pointer'); }
  if (!validatePointer(existing, roleOptions)) throw new Error('invalid existing current pointer');
  return existing;
}

async function writePointer(io, destination, model, roleOptions, preflight = undefined) {
  let previousBytes;
  try { previousBytes = await io.readFile(destination); } catch (error) { if (error.code !== 'ENOENT') throw new Error('invalid existing current pointer'); }
  const existing = preflight?.existing ?? (previousBytes ? await readExistingPointer(io, destination, roleOptions) : undefined);
  const bytes = jsonBytes(model);
  const temp = `${destination}.tmp`;
  try {
    await io.writeFile(temp, bytes, { flag: 'wx' });
    await io.rename(temp, destination);
  } catch (error) {
    try { const reconciled = await io.readFile(destination); if (Buffer.compare(reconciled, bytes) === 0) return { status: 'RECONCILED_PUBLISHED', existing }; } catch {}
    try {
      const current = await io.readFile(destination);
      if (previousBytes && Buffer.compare(current, previousBytes) === 0) throw new Error(`POINTER_WRITE_FAILED: ${error.message}`);
    } catch (readError) {
      if (!previousBytes && readError.code === 'ENOENT') throw new Error(`POINTER_WRITE_FAILED: ${error.message}`);
      if (readError.message.startsWith('POINTER_WRITE_FAILED')) throw readError;
    }
    throw new Error(`RECONCILIATION_REQUIRED: ${error.message}`);
  }
  const readback = await io.readFile(destination);
  if (Buffer.compare(readback, bytes) !== 0) throw new Error(`pointer readback mismatch: ${destination}`);
  return { status: existing ? 'UPDATED' : 'CREATED', existing };
}

export async function publishEvidence(input, options = {}) {
  const io = { ...defaultIo, ...(options.io ?? {}) };
  const model = await buildPublication(input);
  const root = model.externalEvidenceRoot;
  for (const directory of ['reports/executor', 'reports/curator', 'artifacts', 'current', 'receipts']) await io.mkdir(full(root, directory), { recursive: true });
  if (model.artifacts.length) await io.mkdir(full(root, `artifacts/${model.publicationId}`), { recursive: true });
  const reportStatus = await writeImmutable(io, full(root, model.report.externalRelativePath), model.report.bytes);
  const artifactStatuses = [];
  for (const artifact of model.artifacts) artifactStatuses.push(await writeImmutable(io, full(root, `artifacts/${model.publicationId}/${artifact.basename}`), await io.readFile(artifact.sourcePath)));
  const receiptStatus = await writeImmutable(io, full(root, `receipts/${model.publicationId}.json`), model.receiptBytes);
  if (!validateReceipt(JSON.parse(model.receiptBytes.toString('utf8')))) throw new Error('invalid generated receipt');
  const roleDestination = full(root, model.pointers.role.relativePath);
  const milestoneDestination = full(root, model.pointers.milestone.relativePath);
  const rolePreflight = await readExistingPointer(io, roleDestination, { expectedRole: model.pointers.role.model.role });
  const milestonePreflight = await readExistingPointer(io, milestoneDestination, { allowedRoles: ['executor', 'curator'] });
  let rolePointer;
  try { rolePointer = await writePointer(io, roleDestination, model.pointers.role.model, { expectedRole: model.pointers.role.model.role }, { existing: rolePreflight }); }
  catch (error) { throw new Error(`${error.message}; publicationId=${model.publicationId}; pointer=${model.pointers.role.relativePath}`); }
  let milestonePointer;
  try { milestonePointer = await writePointer(io, milestoneDestination, model.pointers.milestone.model, { allowedRoles: ['executor', 'curator'] }, { existing: milestonePreflight }); }
  catch (error) { throw new Error(`${error.message}; publicationId=${model.publicationId}; appliedPointer=${model.pointers.role.relativePath}; failedPointer=${model.pointers.milestone.relativePath}`); }
  return { classification: reportStatus === 'ALREADY_PUBLISHED' && receiptStatus === 'ALREADY_PUBLISHED' ? 'ALREADY_PUBLISHED' : 'PUBLISHED', publicationStatus: 'PUBLISHED', publicationId: model.publicationId, reportRelativePath: model.report.externalRelativePath, reportSha256: model.report.sha256, artifactRelativePaths: model.receipt.artifacts.map((a) => a.externalRelativePath), receiptRelativePath: `receipts/${model.publicationId}.json`, pointerPaths: [model.pointers.role.relativePath, model.pointers.milestone.relativePath], statuses: { report: reportStatus, artifacts: artifactStatuses, receipt: receiptStatus, pointers: [rolePointer.status, milestonePointer.status] } };
}

export { readExistingPointer, writePointer };
