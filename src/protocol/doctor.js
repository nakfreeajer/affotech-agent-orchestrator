import fs from 'node:fs';
import path from 'node:path';
import { validateControlObject } from './validate-control-object.js';

export function doctor(stateBundle) { return validateControlObject(stateBundle); }

export function doctorFile(filePath) {
  const absolute = path.resolve(filePath);
  return doctor(JSON.parse(fs.readFileSync(absolute, 'utf8')));
}

