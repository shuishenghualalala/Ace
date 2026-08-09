#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(process.argv[2] || 'security-runtime-bin');
const manifestPath = resolve(root, 'runtime-manifest.json');
if (!existsSync(manifestPath)) throw new Error('security runtime manifest missing; run security:prepare');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
if (manifest.schema !== 2 || !Array.isArray(manifest.files)) throw new Error('invalid security runtime manifest');
const expectedRuntime = process.platform === 'win32'
  ? 'ace-security-runtime.exe'
  : 'ace-security-runtime';
const expectedRecord = manifest.files.find((item) => item.name === expectedRuntime);
if (!expectedRecord || typeof expectedRecord.sha256 !== 'string') throw new Error(`required ${expectedRuntime} missing from manifest`);
if (manifest.binary_name === expectedRuntime
  && typeof manifest.binary_sha256 === 'string'
  && manifest.binary_sha256 !== expectedRecord.sha256) {
  throw new Error(`runtime manifest binary metadata mismatch: ${expectedRuntime}`);
}
for (const item of manifest.files) {
  const file = resolve(root, item.name);
  if (!existsSync(file) || !statSync(file).isFile()) throw new Error(`security runtime file missing: ${item.name}`);
  const digest = createHash('sha256').update(readFileSync(file)).digest('hex');
  if (digest !== item.sha256) throw new Error(`security runtime digest mismatch: ${item.name}`);
  if (item.name === expectedRuntime && digest !== expectedRecord.sha256) {
    throw new Error(`security runtime binary metadata mismatch: ${item.name}`);
  }
}
console.log(`verified ${manifest.files.length} security runtime file(s)`);
