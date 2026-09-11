#!/usr/bin/env node
import { doctorFile } from '../src/protocol/doctor.js';

const file = process.argv[2];
if (!file) { console.error('Usage: node bin/orchestrator-doctor.js <fixture-or-state-bundle-path>'); process.exitCode = 2; }
else {
  try {
    const result = doctorFile(file);
    console.log(JSON.stringify(result, null, 2));
    console.error(result.classification === 'PASS' ? 'protocol healthy' : result.classification);
    process.exitCode = result.classification === 'PASS' ? 0 : 1;
  } catch (error) { console.error(`doctor error: ${error.message}`); process.exitCode = 2; }
}

