import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHmac, randomBytes, randomUUID } from 'node:crypto';
import { once } from 'node:events';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { chromium } from 'playwright-core';

if (process.argv.length !== 3) throw Error('Usage: node desktop/scripts/smoke-packaged-desktop.mjs <packaged executable>');
const executable = fs.realpathSync(process.argv[2]);
const platform = process.platform;
const gatewayRoot = platform === 'linux' ? '/opt/crew-gateway'
  : path.resolve(path.dirname(executable), '..', ...(platform === 'darwin' ? ['Resources'] : []), 'crew-gateway');
const python = path.join(gatewayRoot, 'runtimes', 'python', ...(platform === 'win32' ? ['python.exe'] : ['bin', 'python3']));
assert.ok(fs.statSync(python).isFile(), 'Packaged Gateway interpreter is required');
const installTrace = path.join(gatewayRoot, '.crew', 'logs', 'llm.jsonl');
assert.ok(!fs.existsSync(installTrace), 'Release payload already contains a runtime chat trace');

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ace-release-'));
const home = path.join(root, '用户 Home');
const profile = path.join(home, 'Electron');
const crewHome = path.join(home, '.Crew');
const workspace = path.join(home, 'workspace');
for (const dir of [profile, crewHome, workspace]) fs.mkdirSync(dir, { recursive: true });
const key = randomBytes(24).toString('hex');
const model = 'release-smoke';
const runId = randomUUID();
const prompts = ['first', 'stop', 'next'].map(turn => 'Release ' + turn + ' ' + runId);
const replies = ['First reply ', 'Pending reply ', 'Successor reply '].map(s => s + runId);
const received = [];
const timings = {};
const verifiedGateways = new Map();
let heldNext, providerError, desktop, failure, currentStage;
let launches = 0;

async function stage(name, callback) {
  currentStage = name;
  const started = Date.now();
  console.log('Starting ' + name);
  try { return await callback(); }
  finally { timings[name] = Date.now() - started; }
}

async function waitFor(name, read, timeout = 30000) {
  const deadline = Date.now() + timeout;
  do {
    if (providerError) throw providerError;
    const value = await read();
    if (value) return value;
    await sleep(100);
  } while (Date.now() < deadline);
  throw Error('Timed out waiting for ' + name);
}

function streamChunk(delta, finish_reason = null) {
  const chunk = { id: runId, object: 'chat.completion.chunk', model, created: Math.floor(Date.now() / 1000),
    choices: [{ index: 0, delta, finish_reason }] };
  return 'data: ' + JSON.stringify(chunk) + '\n\n';
}

function finish(res, reply) {
  res.end(streamChunk({ content: reply }) + streamChunk({}, 'stop') + 'data: [DONE]\n\n');
}

const provider = http.createServer(async (req, res) => {
  try {
    assert.equal(req.method, 'POST');
    assert.equal(req.url, '/v1/chat/completions');
    assert.equal(req.headers.authorization, 'Bearer ' + key);
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw);
    const user = body.messages.findLast(message => message.role === 'user');
    const content = typeof user?.content === 'string' ? user.content : JSON.stringify(user?.content);
    const turn = prompts.findIndex(prompt => content?.includes(prompt));
    assert.equal(turn, received.length, 'Unexpected or duplicated provider turn');
    assert.equal(body.model, model);
    received.push(turn);
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    res.write(streamChunk({ role: 'assistant' }));
    if (turn === 0) finish(res, replies[0]);
    else if (turn === 1) res.write(streamChunk({ content: replies[1] }));
    else heldNext = res;
  } catch (error) {
    providerError = error;
    res.destroy();
  }
});

function writeConfig(port) {
  const config = {
    llm: { active: model, default: model, models: { [model]: { provider: 'openai', model, loaded: true,
      base_url: 'http://127.0.0.1:' + port + '/v1', api_key_env: 'ACE_RELEASE_MODEL_KEY', context_window: 200000, max_tokens: 4096 } } },
    agent: { title: { auto: false }, compaction: { enabled: false } },
    runtime: { db_path: path.join(crewHome, 'smoke.sqlite'), task_workspace_root: workspace, memory_enabled: false },
    auth: { mode: 'local' }, gateway: { host: '127.0.0.1' }, external_agents: { enabled: false },
  };
  fs.writeFileSync(path.join(crewHome, 'config.yaml'), JSON.stringify(config));
  fs.writeFileSync(path.join(profile, 'desktop-prefs.json'), JSON.stringify({ closeBehavior: 'quit' }));
}

function launchEnv() {
  const allowed = new Set(['PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT',
    'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'OS', 'PROCESSOR_ARCHITECTURE',
    'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'LANG', 'LC_ALL', '__CF_USER_TEXT_ENCODING']);
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => allowed.has(k.toUpperCase())));
  const temp = path.join(root, 'tmp');
  const xdgRuntime = path.join(root, 'xdg-runtime');
  fs.mkdirSync(temp, { recursive: true });
  fs.mkdirSync(xdgRuntime, { recursive: true, mode: 0o700 });
  return { ...env, HOME: home, USERPROFILE: home, APPDATA: path.join(home, 'roaming'),
    LOCALAPPDATA: path.join(home, 'local'), PROGRAMDATA: path.join(home, 'programdata'),
    XDG_DATA_HOME: path.join(home, 'data'), XDG_CONFIG_HOME: path.join(home, 'config'), XDG_CACHE_HOME: path.join(home, 'cache'),
    XDG_RUNTIME_DIR: xdgRuntime, TMP: temp, TEMP: temp, TMPDIR: temp, CREW_HOME: crewHome, ACE_RELEASE_MODEL_KEY: key,
    CREW_OFFLINE: '1', CREW_RIPGREP_INSTALLER: 'system', HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9',
    ALL_PROXY: 'http://127.0.0.1:9', NO_PROXY: '127.0.0.1,localhost,::1' };
}

function readOptional(file) {
  try { return fs.readFileSync(file, 'utf8'); }
  catch (error) { if (error.code === 'ENOENT' || error.code === 'EBUSY') return ''; throw error; }
}

function isLive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}

async function start() {
  const env = launchEnv();
  const args = ['--user-data-dir=' + profile, '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--disable-background-networking', '--proxy-server=http://127.0.0.1:9', '--password-store=basic', '--use-mock-keychain'];
  const portFile = path.join(profile, 'DevToolsActivePort');
  fs.rmSync(portFile, { force: true });
  const mainLog = path.join(root, launches++ === 0 ? 'desktop.log' : 'desktop-restart.log');
  const output = fs.openSync(mainLog, 'a');
  const mac = platform === 'darwin';
  const command = mac ? '/usr/bin/open' : executable;
  const macArgs = ['-W', '-n', '-g', '--stdout', mainLog, '--stderr', mainLog + '.stderr',
    ...Object.entries(env).flatMap(([k, v]) => ['--env', k + '=' + v]), path.resolve(path.dirname(executable), '../..'), '--args'];
  try {
    const child = spawn(command, mac ? [...macArgs, ...args] : args,
      { cwd: root, env, windowsHide: true, stdio: ['ignore', output, output] });
    desktop = { child, mainLog, gateway: null, page: null, browser: null, pid: mac ? null : child.pid };
    child.once('error', error => { desktop.launchError = error; });
  } finally { fs.closeSync(output); }
  const ws = await waitFor('packaged Electron debugging socket', () => {
    if (desktop.launchError) throw desktop.launchError;
    assert.equal(desktop.child.exitCode, null, 'Packaged launcher exited before opening its window');
    const [port, route] = readOptional(portFile).trim().split(/\r?\n/);
    if (/^\d+$/.test(port) && /^\/devtools\/browser\/[-a-z0-9]+$/i.test(route)) return 'ws://127.0.0.1:' + port + route;
  }, 60000);
  desktop.browser = await chromium.connectOverCDP(ws);
  const native = await desktop.browser.newBrowserCDPSession();
  const info = await native.send('SystemInfo.getProcessInfo');
  const browserPid = info.processInfo.find(item => item.type === 'browser')?.id;
  assert.ok(Number.isInteger(browserPid) && browserPid > 0);
  if (!mac) assert.equal(browserPid, desktop.child.pid);
  desktop.pid = browserPid;
  await native.detach();
  desktop.page = await primary();
  desktop.gateway = await verifyGateway();
  const launchMode = await desktop.page.evaluate(() => window.Crew.getLaunchMode());
  assert.equal(launchMode.isDevLaunch, false);
  return desktop;
}

const handledPages = new WeakSet();

async function primary(oldPage) {
  return waitFor('interactive packaged renderer and Gateway', async () => {
    for (const page of desktop.browser.contexts().flatMap(context => context.pages())) {
      if (page === oldPage || page.isClosed() || !page.url().startsWith('file:') || !page.url().includes('/dist/assets/index.html')) continue;
      try {
        const status = await page.evaluate(() => window.Crew?.getBackendStatus());
        if (status?.readiness.status === 'ready' && status.transportState === 'connected'
          && await page.locator('[data-composer-input]').first().isVisible()) {
          if (!handledPages.has(page)) {
            const skip = page.locator('.model-tour [data-tour-skip]');
            await page.addLocatorHandler(skip, () => skip.click());
            handledPages.add(page);
          }
          return page;
        }
      } catch (error) { if (!page.isClosed() && !/destroyed|closed|navigation/i.test(error.message)) throw error; }
    }
  }, 100000);
}

async function json(port, route, headers) {
  const response = await fetch('http://127.0.0.1:' + port + route, { headers, signal: AbortSignal.timeout(5000) });
  assert.equal(response.status, 200, route);
  return response.json();
}

async function verifyGateway() {
  const { host, pid, port } = JSON.parse(fs.readFileSync(path.join(crewHome, 'run', 'gateway.json'), 'utf8'));
  assert.equal(host, '127.0.0.1');
  assert.ok(Number.isInteger(pid) && pid > 0 && isLive(pid));
  const log = readOptional(path.join(profile, 'logs', 'gateway-startup.log'));
  assert.ok(log.includes('gateway pid=' + pid + ' port=' + port + ' python=' + python), 'Gateway must be the bundled Python child');
  const status = await desktop.page.evaluate(() => window.Crew.getBackendStatus());
  assert.equal(status.baseUrl, 'http://127.0.0.1:' + port);
  const challenge = randomBytes(32).toString('hex');
  const secret = Buffer.from(fs.readFileSync(path.join(crewHome, '.gateway-instance', 'gateway-instance.key'), 'utf8'), 'hex');
  const expected = createHmac('sha256', secret).update('crew-gateway-instance-v1\0' + challenge).digest('hex');
  const endpoints = await Promise.all([port, port + 1].map(p => json(p, '/api/health', { 'X-Crew-Gateway-Challenge': challenge })));
  for (const health of endpoints) {
    assert.equal(health.instance_proof, expected);
    assert.equal(health.service, 'crew-gateway');
    assert.match(health.desktop_launch_proof, /^[a-f0-9]{64}$/);
  }
  assert.equal(endpoints[0].desktop_launch_proof, endpoints[1].desktop_launch_proof);
  verifiedGateways.set(pid, { pid, port, healthPort: port + 1 });
  return { pid, port };
}

async function send(page, turn) {
  const input = page.locator('#chat-composer-root [data-composer-input]');
  await input.fill(prompts[turn]);
  await waitFor('composer send enabled', () => page.locator('#chat-composer-root [data-composer-send]').isEnabled());
  await input.press('Enter');
  await waitFor('local SSE request ' + turn, () => received.includes(turn));
}

function reply(page, turn) {
  return page.locator('.msg--agent-turn:visible').filter({ hasText: replies[turn] }).last().waitFor({ timeout: 35000 });
}

async function findSession(port) {
  for (const { session_id } of await json(port, '/api/sessions')) {
    const items = (await json(port, '/api/session/' + encodeURIComponent(session_id) + '/history')).items;
    if (items.some(item => item.role === 'user' && item.content.includes(prompts[0]))) return session_id;
  }
}

async function assertHistory(port, sid) {
  const route = '/api/session/' + encodeURIComponent(sid) + '/history';
  return waitFor('durable distinct turns and successor reply', async () => {
    const { items } = await json(port, route);
    const users = items.filter(item => item.role === 'user');
    if (users.length < 3 || !items.some(i => i.role === 'assistant' && i.content.includes(replies[2]))) return false;
    assert.deepEqual(users.map(m => m.content), prompts);
    assert.ok(users.every(m => typeof m.request_id === 'string' && m.request_id));
    assert.equal(new Set(users.map(m => m.request_id)).size, 3);
    assert.ok(items.some(i => i.role === 'assistant' && i.content.includes(replies[0])));
    return users.map(m => m.request_id);
  });
}

async function showHistory(page, sid) {
  const answer = page.locator('.msg--agent-turn:visible').filter({ hasText: replies[2] }).last();
  const session = page.locator('[data-session-open="' + sid + '"], [data-session-id="' + sid + '"] .history-item-main').filter({ visible: true }).first();
  const restore = page.getByRole('button', { name: /展开.*(上下文|历史)/ }).filter({ visible: true }).first();
  await waitFor('restored conversation or its selectable history entry', async () => {
    if (await answer.isVisible()) return true;
    if (await session.isVisible()) { await session.click(); return true; }
    if (await restore.isVisible()) await restore.click();
    return false;
  });
  await reply(page, 0);
  await reply(page, 2);
}

async function bindable(port) {
  const server = net.createServer();
  return new Promise((resolve, reject) => {
    server.once('error', error => ['EADDRINUSE', 'EACCES'].includes(error.code) ? resolve(false) : reject(error));
    server.listen(port, '127.0.0.1', () => server.close(() => resolve(true)));
  });
}

async function quit() {
  const { page, gateway, pid, child, browser } = desktop;
  await page.evaluate(() => { void window.Crew.appQuit(); });
  await waitFor('desktop and packaged Gateway exit', () => !isLive(pid) && !isLive(gateway.pid), 25000);
  await waitFor('Gateway and health ports released', async () => (await Promise.all([gateway.port, gateway.port + 1].map(bindable))).every(Boolean));
  await waitFor('launcher exit code', () => child.exitCode !== null || child.signalCode);
  assert.equal(child.exitCode, 0);
  const gatewayLog = readOptional(path.join(profile, 'logs', 'gateway-startup.log'));
  assert.ok(gatewayLog.includes('[exit] code=0 signal=null pid=' + gateway.pid), 'Gateway must exit cleanly');
  const shutdownLog = readOptional(desktop.mainLog);
  assert.match(shutdownLog, /\[shutdown\] prepared\s*\{\s*reason: 'app-quit',\s*failed: \[\]\s*\}/);
  await browser.close();
  desktop = null;
}

async function run() {
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  writeConfig(provider.address().port);
  let { page, gateway } = await stage('cold-start', start);
  await stage('first-chat', async () => { await send(page, 0); await reply(page, 0); });
  const sid = await waitFor('first persisted session', () => findSession(gateway.port));
  await waitFor('chat trace in the isolated user home', () =>
    readOptional(path.join(crewHome, 'logs', 'llm.jsonl')).includes(prompts[0]));
  const debug = await json(gateway.port, '/api/session/' + encodeURIComponent(sid) + '/debug-log');
  assert.ok(debug.enabled && debug.events.length > 0, 'Session diagnostics must read the same user trace');
  assert.ok(!fs.existsSync(installTrace), 'Chat wrote into the installation');

  const status = () => json(gateway.port, '/api/session/' + encodeURIComponent(sid) + '/status');
  let stopped;
  await stage('precise-stop-and-successor', async () => {
    await page.evaluate(() => {
      window.releaseSmokeStops = [];
      window.releaseSmokeStoppedTurns = [];
      window.Crew.onGatewayWsEvent(event => {
        if (event.type === 'message') {
          const frame = JSON.parse(event.data);
          if (frame.body?.stop) window.releaseSmokeStops.push(frame.body.stop);
          if (frame.body?.outcome?.status === 'stopped') window.releaseSmokeStoppedTurns.push(frame.request_id);
        }
      });
    });
    await send(page, 1);
    await reply(page, 1);
    stopped = (await status()).active_request_id;
    assert.ok(stopped);
    await page.locator('#chat-composer-root [data-composer-stop]').click();
    const ack = state => page.waitForFunction(({ id, state }) =>
      window.releaseSmokeStops.some(ack => ack.request_id === id && ack.state === state), { id: stopped, state }, { timeout: 25000 });
    await ack('requested');
    await page.waitForFunction(id => window.releaseSmokeStoppedTurns.includes(id), stopped, { timeout: 25000 });
    await waitFor('named turn stopped', async () => !(await status()).active_request_id);
    await send(page, 2);
    const next = (await status()).active_request_id;
    assert.ok(next && next !== stopped);
    await page.evaluate(({ sid, stopped }) => window.Crew.gatewayWsSend({ action: 'stop', session_id: sid, request_id: stopped }), { sid, stopped });
    await ack('not_active');
    assert.equal((await status()).active_request_id, next);
    finish(heldNext, replies[2]);
    await reply(page, 2);
    const ids = await assertHistory(gateway.port, sid);
    assert.deepEqual(ids.slice(1), [stopped, next]);
  });
  await stage('renderer-recovery', async () => {
    const oldPage = page;
    const session = await page.context().newCDPSession(page);
    const crashed = page.waitForEvent('crash', { timeout: 20000 });
    void session.send('Page.crash').catch(() => {});
    await crashed;
    page = desktop.page = await primary(oldPage);
    assert.deepEqual(await verifyGateway(), gateway);
    await showHistory(page, sid);
  });
  await stage('gateway-restart', async () => {
    assert.deepEqual(await verifyGateway(), gateway);
    process.kill(gateway.pid, 'SIGTERM');
    await waitFor('replacement Gateway discovery', () => {
      const metadata = readOptional(path.join(crewHome, 'run', 'gateway.json'));
      if (!metadata) return false;
      const next = JSON.parse(metadata);
      return next.pid !== gateway.pid && !isLive(gateway.pid);
    }, 85000);
    page = desktop.page = await primary();
    gateway = desktop.gateway = await verifyGateway();
    await assertHistory(gateway.port, sid);
    await showHistory(page, sid);
  });
  await stage('first-quit', quit);
  ({ page, gateway } = await stage('restart', start));
  await stage('restart-history', async () => { await assertHistory(gateway.port, sid); await showHistory(page, sid); });
  await stage('final-quit', quit);
  assert.deepEqual(received, [0, 1, 2]);
  assert.ok(!fs.existsSync(installTrace), 'Runtime chat trace leaked into the installation');
}

console.log('Packaged smoke profile: ' + root);
try { await run(); }
catch (error) { failure = error; }
finally {
  if (desktop) {
    const { page, browser, pid, child } = desktop;
    if (page && !page.isClosed()) await page.evaluate(() => { void window.Crew.appQuit(); }).catch(() => {});
    if (pid && isLive(pid)) {
      await waitFor('owned process shutdown', () => !isLive(pid), 5000).catch(() => {});
      if (isLive(pid)) process.kill(pid);
    }
    if (child.exitCode === null && !child.signalCode) child.kill();
    await browser?.close().catch(() => {});
  }
  provider.closeAllConnections();
  if (provider.listening) await new Promise(resolve => provider.close(resolve));
  console.log(JSON.stringify({ platform, executable, stages_ms: timings, gateways: [...verifiedGateways.values()], status: failure ? 'failed' : 'passed',
    ...(failure ? { failed_stage: currentStage, profile: root } : {}) }, null, 2));
  if (!failure) fs.rmSync(root, { recursive: true, force: true, maxRetries: 4, retryDelay: 150 });
}
if (failure) throw failure;
