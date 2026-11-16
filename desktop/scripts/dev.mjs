/** Launch Electron even from IDE terminals that export ELECTRON_RUN_AS_NODE. */
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
// esbuild.config.mjs starts async main() without exporting its promise.
// Importing it is not a build barrier: a clean checkout can open Electron
// before dist exists. Wait for the build process to exit successfully instead.
await new Promise((resolve, reject) => {
  const build = spawn(process.execPath, ['esbuild.config.mjs', '--dev'], { stdio: 'inherit', env });
  build.on('error', reject);
  build.on('exit', (code) => code === 0 ? resolve() : reject(new Error(`Development build failed (${code})`)));
});
const child = spawn(require('electron'), ['.', '--dev'], { stdio: 'inherit', env });
child.on('error', (error) => { console.error(error); process.exitCode = 1; });
child.on('exit', (code) => { process.exitCode = code ?? 1; });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
