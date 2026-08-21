#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readFileSync, readdirSync } from 'node:fs';
import { basename, relative, resolve, sep } from 'node:path';

const root = resolve(process.argv[2] || 'security-runtime-bin');
const manifestPath = resolve(root, 'runtime-manifest.json');
if (!existsSync(manifestPath)) throw new Error('security runtime manifest missing; rebuild security-runtime');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
if (manifest.schema !== 2 || !Array.isArray(manifest.files)) {
  throw new Error('invalid security runtime manifest');
}
const expectedRuntime = process.platform === 'win32'
  ? 'ace-security-runtime.exe'
  : 'ace-security-runtime';
const expectedArch = process.arch;
if (manifest.platform !== process.platform || manifest.arch !== expectedArch) {
  throw new Error('security runtime manifest targets a different platform or architecture');
}
const digestPattern = /^[0-9a-f]{64}$/;
const safeFileName = (name) => (
  typeof name === 'string'
  && name.length > 0
  && basename(name) === name
  && !name.includes('/')
  && !name.includes('\\')
  && name !== '.'
  && name !== '..'
);
const seenFiles = new Set();
const runtimeRecords = manifest.files.filter((item) => item && item.name === expectedRuntime);
if (runtimeRecords.length !== 1) {
  throw new Error(`required ${expectedRuntime} missing from manifest`);
}
const expectedRecord = runtimeRecords[0];
if (manifest.binary_name !== expectedRuntime || !digestPattern.test(manifest.binary_sha256)) {
  throw new Error('security runtime manifest binary integrity metadata missing');
}
for (const item of manifest.files) {
  if (
    !item
    || !safeFileName(item.name)
    || seenFiles.has(item.name)
    || !digestPattern.test(item.sha256)
    || !Number.isSafeInteger(item.size)
    || item.size < 0
  ) {
    throw new Error('invalid security runtime manifest file metadata');
  }
  seenFiles.add(item.name);
  const file = resolve(root, item.name);
  const fileStat = existsSync(file) ? lstatSync(file) : null;
  if (!fileStat?.isFile()) {
    throw new Error(`security runtime file missing: ${item.name}`);
  }
  if (fileStat.size !== item.size) {
    throw new Error(`security runtime file size mismatch: ${item.name}`);
  }
  const digest = createHash('sha256').update(readFileSync(file)).digest('hex');
  if (digest !== item.sha256) throw new Error(`security runtime digest mismatch: ${item.name}`);
  if (item.name === expectedRuntime && digest !== expectedRecord.sha256) {
    throw new Error(`security runtime binary metadata mismatch: ${item.name}`);
  }
}

const sourceRoot = resolve(root, '..', '..', 'security-runtime');
if (existsSync(resolve(sourceRoot, 'Cargo.toml'))) {
  if (!digestPattern.test(manifest.source_hash)) {
    throw new Error('security runtime source hash is missing or invalid');
  }
  const selectedSources = [];
  const visit = (directory, accept) => {
    if (!existsSync(directory)) return;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name);
      if (entry.isDirectory()) visit(path, accept);
      else if (entry.isFile() && accept(path)) selectedSources.push(path);
    }
  };
  visit(resolve(sourceRoot, 'src'), () => true);
  visit(resolve(sourceRoot, 'tests'), (path) => path.endsWith('.rs'));
  selectedSources.push(resolve(sourceRoot, 'Cargo.toml'), resolve(sourceRoot, 'Cargo.lock'));
  selectedSources.sort((left, right) => {
    const leftName = relative(sourceRoot, left).split(sep).join('/');
    const rightName = relative(sourceRoot, right).split(sep).join('/');
    return leftName < rightName ? -1 : leftName > rightName ? 1 : 0;
  });
  const digest = createHash('sha256');
  for (const path of selectedSources) {
    digest.update(relative(sourceRoot, path).split(sep).join('/'));
    digest.update('\0');
    digest.update(readFileSync(path));
    digest.update('\0');
  }
  if (digest.digest('hex') !== manifest.source_hash) {
    throw new Error('security runtime source is stale; rebuild scripts/build-security-runtime.ps1 or .sh');
  }
}
console.log(`verified ${manifest.files.length} security runtime file(s) from ${root}`);
