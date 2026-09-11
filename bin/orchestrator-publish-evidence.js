#!/usr/bin/env node
import { publishEvidence } from '../src/evidence/publish-evidence.js';

const args = process.argv.slice(2); const get = (flag) => { const i = args.indexOf(flag); return i < 0 ? undefined : args[i + 1]; }; const artifacts = args.flatMap((a, i) => a === '--artifact' ? [args[i + 1]] : []).filter(Boolean);
const required = ['--drive-root', '--external-root', '--report', '--role', '--milestone', '--result'];
if (required.some((flag) => !get(flag))) { console.error('explicit --external-root --report --role --milestone --result required'); process.exitCode = 2; }
else try { const result = await publishEvidence({ repositoryRoot: process.cwd(), driveMyDriveRoot: get('--drive-root'), externalEvidenceRoot: get('--external-root'), reportPath: get('--report'), role: get('--role'), milestone: get('--milestone'), result: get('--result'), artifacts }); console.log(JSON.stringify(result)); process.exitCode = 0; } catch (error) { console.log(JSON.stringify({ classification: error.message.startsWith('RECONCILIATION_REQUIRED') ? 'RECONCILIATION_REQUIRED' : 'FAIL_CLOSED', error: error.message })); process.exitCode = 1; }
