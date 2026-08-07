#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';

function argument(name, required = true) {
  const index = process.argv.indexOf(`--${name}`);
  const value = index >= 0 ? process.argv[index + 1] : '';
  if (required && !value) throw new Error(`missing --${name}`);
  return value;
}

const runtime = resolve(argument('runtime'));
const bwrapArg = argument('bwrap', false);
const bwrap = bwrapArg ? resolve(bwrapArg) : '';
const bwrapLicenseArg = argument('bwrap-license', false);
const bwrapLicense = bwrapLicenseArg ? resolve(bwrapLicenseArg) : '';
const bwrapVersion = argument('bwrap-version', false);
const output = resolve(argument('output', false) || 'security-runtime-bin');
rmSync(output, { recursive: true, force: true });
mkdirSync(output, { recursive: true });

const files = [];
for (const source of [runtime, bwrap, bwrapLicense].filter(Boolean)) {
  const name = source === bwrapLicense ? 'BWRAP-LICENSE' : basename(source);
  copyFileSync(source, resolve(output, name));
  const bytes = readFileSync(source);
  files.push({ name, sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length });
}
const runtimeRecord = files.find((item) => item.name === basename(runtime));
if (!runtimeRecord) throw new Error('runtime artifact was not staged');
const manifest = {
  schema: 2,
  runtime_version: '0.1.0',
  platform: process.platform,
  arch: process.arch,
  generated_by: 'desktop/scripts/prepare-security-runtime.mjs',
  binary_name: runtimeRecord.name,
  binary_sha256: runtimeRecord.sha256,
  files,
  ...(bwrap ? { bwrap_provenance: {
    source: 'distribution package copied at build time',
    version: bwrapVersion || 'unrecorded',
    license_file: bwrapLicense ? 'BWRAP-LICENSE' : '',
  } } : {}),
};
writeFileSync(resolve(output, 'runtime-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
const bwrapRecord = files.find((item) => item.name === 'bwrap');
writeFileSync(
  resolve(output, 'security-runtime.env'),
  bwrapRecord ? `ACE_BUNDLED_BWRAP_SHA256=${bwrapRecord.sha256}\n` : '',
);
