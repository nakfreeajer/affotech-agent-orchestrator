import fs from 'node:fs/promises';
import path from 'node:path';

const defaultIo = { mkdir: fs.mkdir, readFile: fs.readFile, writeFile: fs.writeFile, rename: fs.rename };
const jsonBytes = (value) => Buffer.from(`${JSON.stringify(value)}\n`, 'utf8');
const safePublication = (value) => typeof value === 'string' && value.length > 0 && /^[A-Za-z0-9._-]+$/.test(value);
const inside = (root, candidate) => candidate === root || candidate.startsWith(`${root}${path.sep}`);

function publicationPath(root, publicationId, name) {
  if (!safePublication(publicationId)) throw new Error('invalid terminal publication id');
  return path.join(root, 'evidence', 'terminal', 'executor', publicationId, name);
}

async function immutableWrite(io, destination, bytes) {
  try {
    const existing = await io.readFile(destination);
    if (Buffer.compare(existing, bytes) === 0) return 'ALREADY_PUBLISHED';
    throw new Error(`immutable collision: ${destination}`);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  await io.writeFile(destination, bytes, { flag: 'wx' });
  const readback = await io.readFile(destination);
  if (Buffer.compare(readback, bytes) !== 0) throw new Error(`readback mismatch: ${destination}`);
  return 'PUBLISHED';
}

async function advancePointer(io, root, pointer) {
  const destination = path.join(root, 'evidence', 'current', 'LATEST_EXECUTOR_TERMINAL.json');
  const bytes = jsonBytes(pointer);
  let existing;
  try { existing = await io.readFile(destination); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (existing && Buffer.compare(existing, bytes) === 0) return 'ALREADY_PUBLISHED';
  const temp = `${destination}.tmp`;
  await io.writeFile(temp, bytes, { flag: 'wx' });
  await io.rename(temp, destination);
  const readback = await io.readFile(destination);
  if (Buffer.compare(readback, bytes) !== 0) throw new Error('terminal pointer readback mismatch');
  return existing ? 'UPDATED' : 'CREATED';
}

/** Publish immutable terminal artifacts and advance their current pointer. */
export async function publishExecutorTerminal({ evidenceRoot, publicationId, terminal, report, receipt, pointer }, options = {}) {
  const io = { ...defaultIo, ...(options.io ?? {}) };
  const root = path.resolve(evidenceRoot);
  const terminalDir = publicationPath(root, publicationId, 'terminal.json');
  const reportPath = publicationPath(root, publicationId, 'report.md');
  const receiptPath = publicationPath(root, publicationId, 'receipt.json');
  for (const destination of [terminalDir, reportPath, receiptPath]) {
    if (!inside(root, destination)) throw new Error('terminal path escapes evidence root');
    await io.mkdir(path.dirname(destination), { recursive: true });
  }
  await io.mkdir(path.join(root, 'evidence', 'current'), { recursive: true });
  const statuses = {
    terminal: await immutableWrite(io, terminalDir, Buffer.isBuffer(terminal) ? terminal : jsonBytes(terminal)),
    report: await immutableWrite(io, reportPath, Buffer.isBuffer(report) ? report : Buffer.from(String(report), 'utf8')),
    receipt: await immutableWrite(io, receiptPath, Buffer.isBuffer(receipt) ? receipt : jsonBytes(receipt)),
    pointer: await advancePointer(io, root, pointer),
  };
  return { publicationId, pointerPath: 'evidence/current/LATEST_EXECUTOR_TERMINAL.json', statuses };
}
