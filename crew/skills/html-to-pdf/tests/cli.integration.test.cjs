'use strict';

const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const fsp = fs.promises;
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const RUNNER = path.resolve(__dirname, '..', 'scripts', 'run.cjs');

async function runCli(args, environment) {
  const child = spawn(process.execPath, [RUNNER, ...args], {
    cwd: path.dirname(RUNNER),
    env: environment,
    shell: false,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  let stdout = '';
  let stderr = '';
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    stdout += chunk;
  });
  child.stderr.on('data', (chunk) => {
    stderr += chunk;
  });
  const status = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code, signal) => resolve({ code, signal }));
  });
  return { ...status, stderr, stdout };
}

function minimalEnvironment(root, sandbox = 'linux-bwrap') {
  return {
    ACE_SANDBOX: sandbox,
    HOME: root,
    LANG: 'C.UTF-8',
    LC_ALL: 'C.UTF-8',
    TEMP: root,
    TMP: root,
    TMPDIR: root,
  };
}

test('CLI rejects host-file HTML without reading or publishing it', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ace-pdf-cli-file-'));
  t.after(() => fsp.rm(root, { force: true, recursive: true }));
  const secret = path.join(root, 'host-secret.txt');
  const input = path.join(root, 'input.html');
  const output = path.join(root, 'output.pdf');
  await fsp.writeFile(secret, 'HOST_FILE_CANARY');
  await fsp.writeFile(
    input,
    `<img src="file:///${secret.replaceAll('\\', '/')}">`,
  );

  const result = await runCli([input, output], minimalEnvironment(root));

  assert.equal(result.code, 1);
  assert.match(result.stderr, /\[html-to-pdf:resource_denied\]/);
  assert.equal(await fsp.readFile(secret, 'utf8'), 'HOST_FILE_CANARY');
  assert.equal(fs.existsSync(output), false);
});

test('CLI rejects loopback/private/metadata resources without a connection', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ace-pdf-cli-net-'));
  t.after(() => fsp.rm(root, { force: true, recursive: true }));
  let requests = 0;
  const server = http.createServer((_request, response) => {
    requests += 1;
    response.end('LOOPBACK_CANARY');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  const cases = [
    `http://127.0.0.1:${address.port}/canary`,
    'http://10.0.0.1/private',
    'http://169.254.169.254/latest/meta-data/',
  ];

  for (let index = 0; index < cases.length; index += 1) {
    const input = path.join(root, `input-${index}.html`);
    const output = path.join(root, `output-${index}.pdf`);
    await fsp.writeFile(input, `<img src="${cases[index]}">`);
    const result = await runCli([input, output], minimalEnvironment(root));
    assert.equal(result.code, 1, cases[index]);
    assert.match(result.stderr, /\[html-to-pdf:resource_denied\]/);
    assert.equal(fs.existsSync(output), false);
  }
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(requests, 0);
});

test('CLI fails closed when invoked outside the Ace native sandbox', async (t) => {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ace-pdf-cli-sandbox-'));
  t.after(() => fsp.rm(root, { force: true, recursive: true }));
  const input = path.join(root, 'input.html');
  const output = path.join(root, 'output.pdf');
  await fsp.writeFile(input, '<p>safe</p>');
  const environment = minimalEnvironment(root);
  delete environment.ACE_SANDBOX;

  const result = await runCli([input, output], environment);

  assert.equal(result.code, 1);
  assert.match(result.stderr, /\[html-to-pdf:outer_sandbox_unavailable\]/);
  assert.equal(fs.existsSync(output), false);
});
