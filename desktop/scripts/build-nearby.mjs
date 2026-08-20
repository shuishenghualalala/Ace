import { execFileSync } from 'node:child_process';
import { chmodSync, copyFileSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktopRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = resolve(desktopRoot, '..');
const release = process.argv.includes('--release');
const cargo = process.platform === 'win32' ? 'cargo.exe' : 'cargo';
const manifest = join(repoRoot, 'nearby', 'Cargo.toml');
const buildArgs = ['build', '--manifest-path', manifest];
if (release) buildArgs.push('--release');

execFileSync(cargo, buildArgs, { cwd: repoRoot, stdio: 'inherit' });

const binaryName = process.platform === 'win32' ? 'crew-nearby.exe' : 'crew-nearby';
const profile = release ? 'release' : 'debug';
const source = join(repoRoot, 'nearby', 'target', profile, binaryName);
const outputDir = join(desktopRoot, 'nearby-bin');
const output = join(outputDir, binaryName);
mkdirSync(outputDir, { recursive: true });
copyFileSync(source, output);
if (process.platform !== 'win32') chmodSync(output, 0o755);
console.log(`[nearby] ${release ? 'release' : 'debug'} runtime copied to ${output}`);
