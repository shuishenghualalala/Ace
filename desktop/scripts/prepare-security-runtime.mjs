#!/usr/bin/env node
import { createHash } from 'node:crypto';
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { basename, relative, resolve, sep } from 'node:path';

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
const sourceRootArg = argument('source-root', false);
const sourceRoot = sourceRootArg ? resolve(sourceRootArg) : '';
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

function sourceMetadata(root) {
  const sourceDirectory = resolve(root, 'src');
  const cargoToml = resolve(root, 'Cargo.toml');
  const cargoLock = resolve(root, 'Cargo.lock');
  if (!existsSync(sourceDirectory) || !existsSync(cargoToml) || !existsSync(cargoLock)) {
    throw new Error('invalid --source-root: expected src/, Cargo.toml and Cargo.lock');
  }
  const selected = [];
  const visit = (directory, accept) => {
    if (!existsSync(directory)) return;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      if (entry.isDirectory()) visit(path, accept);
      else if (entry.isFile() && accept(path)) selected.push(path);
    }
  };
  visit(sourceDirectory, () => true);
  visit(resolve(root, 'tests'), (path) => path.endsWith('.rs'));
  selected.push(cargoToml, cargoLock);
  selected.sort((left, right) => {
    const leftName = relative(root, left).split(sep).join('/');
    const rightName = relative(root, right).split(sep).join('/');
    return leftName < rightName ? -1 : leftName > rightName ? 1 : 0;
  });
  const digest = createHash('sha256');
  for (const path of selected) {
    digest.update(relative(root, path).split(sep).join('/'));
    digest.update('\0');
    digest.update(readFileSync(path));
    digest.update('\0');
  }
  return { source_hash: digest.digest('hex'), source_files: selected.length };
}

const manifest = {
  schema: 2,
  runtime_version: '0.1.0',
  platform: process.platform,
  arch: process.arch,
  generated_by: 'desktop/scripts/prepare-security-runtime.mjs',
  binary_name: runtimeRecord.name,
  binary_sha256: runtimeRecord.sha256,
  files,
  ...(sourceRoot ? sourceMetadata(sourceRoot) : {}),
  ...(bwrap ? { bwrap_provenance: {
    source: 'distribution package copied at build time',
    version: bwrapVersion || 'unrecorded',
    license_file: bwrapLicense ? 'BWRAP-LICENSE' : '',
  } } : {}),
};
writeFileSync(resolve(output, 'runtime-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
