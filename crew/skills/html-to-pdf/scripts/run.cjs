'use strict';

const MIN_NODE_VERSION = Object.freeze([22, 12, 0]);
const CLI_ENV_ALLOWLIST = new Set([
  'ACE_SANDBOX',
  'COMSPEC',
  'HOME',
  'LANG',
  'LC_ALL',
  'SYSTEMROOT',
  'TEMP',
  'TMP',
  'TMPDIR',
  'WINDIR',
]);

function nodeVersionSupported(version = process.versions.node) {
  const parts = String(version).split('.').map((part) => Number.parseInt(part, 10));
  for (let index = 0; index < MIN_NODE_VERSION.length; index += 1) {
    if (!Number.isSafeInteger(parts[index])) return false;
    if (parts[index] > MIN_NODE_VERSION[index]) return true;
    if (parts[index] < MIN_NODE_VERSION[index]) return false;
  }
  return true;
}

function scrubCliEnvironment(environment = process.env) {
  for (const name of Object.keys(environment)) {
    if (!CLI_ENV_ALLOWLIST.has(name.toUpperCase())) {
      delete environment[name];
    }
  }
}

async function main(argv = process.argv.slice(2)) {
  if (!nodeVersionSupported()) {
    process.stderr.write(
      '[html-to-pdf:runtime_unavailable] Node.js 22.12.0+ is required\n',
    );
    return 1;
  }

  // Deliberately stay in the already-authorized Node process. The legacy wrapper
  // searched PATH/NVM/FNM and spawned a second, mutable executable.
  scrubCliEnvironment();
  const { runCli } = require('./convert.cjs');
  try {
    const output = await runCli(argv);
    process.stdout.write(`PDF created: ${output}\n`);
    return 0;
  } catch (error) {
    const code = typeof error?.code === 'string' ? error.code : 'conversion_failed';
    const message =
      error?.name === 'PdfSecurityError'
        ? error.message
        : 'HTML-to-PDF conversion failed';
    process.stderr.write(`[html-to-pdf:${code}] ${message}\n`);
    return 1;
  }
}

if (require.main === module) {
  main().then((code) => {
    process.exitCode = code;
  });
}

module.exports = { main, nodeVersionSupported, scrubCliEnvironment };
