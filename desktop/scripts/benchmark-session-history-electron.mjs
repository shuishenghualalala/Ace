import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { log } from 'node:console';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { clearTimeout, setTimeout } from 'node:timers';
import process from 'node:process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const electronExecutable = require('electron');
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ace-session-history-electron-'));
const appRoot = path.join(tempRoot, 'app');
fs.mkdirSync(appRoot, { recursive: true });

const harnessSource = `
import { createSessionHistoryView } from '../src/ui/features/session-history-view';
import { state } from '../src/ui/state';

type Result = { count: number; renderMs: number; mountedRows: number; scrollHeight: number };
const assert = {
  ok(value: unknown, message: string): asserts value { if (!value) throw new Error(message); },
  equal(actual: unknown, expected: unknown, message = 'values differ'): void {
    if (!Object.is(actual, expected)) throw new Error(message + ': ' + String(actual) + ' !== ' + String(expected));
  },
  match(value: string, pattern: RegExp, message = 'value did not match'): void {
    if (!pattern.test(value)) throw new Error(message + ': ' + value);
  },
};
const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
const settleLayout = async () => { await frame(); };
const options = {
  openSession: (sessionId: string) => { window.__openedSession = sessionId; },
  createSession: () => undefined,
  createWorkspace: () => undefined,
  manageHistory: () => undefined,
  openWorkspace: () => undefined,
  refreshSessions: async () => undefined,
  retrySessions: async () => undefined,
  retryWorkspaces: async () => undefined,
  getLoadErrors: () => ({ sessions: null, workspaces: null }),
};

function setFixture(count: number, workspaceIds: string[]): void {
  const now = Date.now();
  const rows = Array.from({ length: count }, (_, index) => {
    const workspaceIndex = index % workspaceIds.length;
    return {
      id: 'history-' + count + '-' + index,
      title: 'Session ' + index,
      updatedAt: now - index,
      preview: 'Stable preview ' + index,
      badge: '',
      workspaceId: workspaceIds[workspaceIndex],
    };
  });
  state.sessions = rows;
  state.activeSessionId = null;
  state.sessionStatuses = {};
  state.historyFilter = '';
  state.channelSessionGroups = [];
  state.workspaces = workspaceIds.map((id) => ({ id, name: id, description: '', instructions: '' }));
  state.expandedWorkspaces = Object.fromEntries(workspaceIds.map((id) => [id, true]));
  state.channelExpanded = {};
  state.wsShowAll = Object.fromEntries(workspaceIds.map((id) => [id, true]));
}

async function measure(count: number, workspaceIds: string[]): Promise<Result> {
  setFixture(count, workspaceIds);
  const host = document.createElement('div');
  host.id = 'session-history-root';
  host.className = 'mw-assistant-context';
  document.querySelector('#mount')!.append(host);
  const started = performance.now();
  const view = createSessionHistoryView(host, options);
  const renderMs = performance.now() - started;
  const list = host.querySelector<HTMLElement>('[data-session-history-list]')!;
  const listHeight = list.clientHeight;
  const mountedRows = list.querySelectorAll('.mw-session-history__row').length;
  const listRect = list.getBoundingClientRect();
  assert.ok(listHeight > 0, 'the production history list must receive a real layout height');
  assert.ok(listRect.height > 0, 'the production history list must occupy layout space');
  assert.ok(list.scrollHeight > listHeight, 'large history must expose a scrollable surface');
  assert.ok(mountedRows < 80, 'virtualization must bound mounted rows, got ' + mountedRows);
  assert.equal(list.getAttribute('aria-label'), '最近会话');
  const firstOpen = list.querySelector<HTMLButtonElement>('[data-session-open]');
  assert.match(firstOpen?.getAttribute('aria-label') ?? '', /^打开会话：/u);

  const oldestId = 'history-' + count + '-' + (count - 1);
  list.scrollTop = list.scrollHeight;
  list.dispatchEvent(new Event('scroll'));
  await settleLayout();
  const oldest = list.querySelector<HTMLElement>('[data-session-id="' + oldestId + '"]');
  assert.ok(oldest, 'the last history row must mount after scrolling (' + oldestId + ')');
  const open = oldest.querySelector<HTMLButtonElement>('[data-session-open]')!;
  assert.equal(open.getAttribute('aria-label'), '打开会话：Session ' + (count - 1));
  open.click();
  assert.equal(window.__openedSession, oldestId, 'virtualized history row must open its session');

  const result = { count, renderMs, mountedRows, scrollHeight: list.scrollHeight };
  view.dispose();
  host.remove();
  return result;
}

async function measureMultipleGroups(): Promise<{ initialRows: number; scrolledRows: number; opened: string }> {
  const count = 20000;
  const workspaceIds = ['workspace-a', 'workspace-b'];
  setFixture(count, workspaceIds);
  const host = document.createElement('div');
  host.id = 'session-history-root';
  host.className = 'mw-assistant-context';
  document.querySelector('#mount')!.append(host);
  createSessionHistoryView(host, options);
  const list = host.querySelector<HTMLElement>('[data-session-history-list]')!;
  const initialRows = list.querySelectorAll('.mw-session-history__row').length;
  assert.ok(initialRows < 80, 'initial multi-group view must remain bounded, got ' + initialRows);

  const targetGroup = list.querySelector<HTMLElement>('[data-session-group="workspace:workspace-b"]');
  assert.ok(targetGroup, 'second expanded workspace group must exist');
  const listTop = list.getBoundingClientRect().top;
  const groupTop = targetGroup.getBoundingClientRect().top;
  list.scrollTop += Math.max(0, groupTop - listTop);
  list.dispatchEvent(new Event('scroll'));
  await settleLayout();
  assert.ok(
    list.querySelector('[data-session-id="history-20000-1"]'),
    'scrolling to the second workspace must mount its recent session',
  );

  const targetId = 'history-20000-19999';
  list.scrollTop = list.scrollHeight;
  list.dispatchEvent(new Event('scroll'));
  await settleLayout();
  const target = list.querySelector<HTMLElement>('[data-session-id="' + targetId + '"]');
  assert.ok(target, 'scrolling through multiple large groups must mount the tail row');
  target.querySelector<HTMLButtonElement>('[data-session-open]')!.click();
  assert.equal(window.__openedSession, targetId);
  const scrolledRows = list.querySelectorAll('.mw-session-history__row').length;
  assert.ok(scrolledRows < 100, 'scrolling must keep the multi-group DOM bounded, got ' + scrolledRows);
  return { initialRows, scrolledRows, opened: targetId };
}

declare global {
  interface Window {
    __openedSession?: string;
    runSessionHistoryBenchmark: () => Promise<{ sizes: Result[]; multipleGroups: { initialRows: number; scrolledRows: number; opened: string } }>;
  }
}

window.runSessionHistoryBenchmark = async () => {
  const sizes: Result[] = [];
  for (const count of [100, 1000, 10000, 20000]) {
    const samples: Result[] = [];
    for (let sample = 0; sample < 3; sample++) {
      samples.push(await measure(count, ['workspace-single']));
    }
    samples.sort((left, right) => left.renderMs - right.renderMs);
    sizes.push(samples[1]);
  }
  return { sizes, multipleGroups: await measureMultipleGroups() };
};
`;

await build({
  stdin: {
    contents: harnessSource,
    resolveDir: path.join(desktopRoot, 'scripts'),
    sourcefile: path.join(desktopRoot, 'scripts', 'session-history-electron-harness.ts'),
    loader: 'ts',
  },
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: 'es2020',
  external: ['mermaid'],
  loader: { '.md': 'text', '.wasm': 'binary' },
  define: { 'import.meta.url': 'window.location.href' },
  outfile: path.join(appRoot, 'renderer.js'),
  write: true,
  logLevel: 'silent',
});
const stylesheet = pathToFileURL(path.join(desktopRoot, 'dist', 'styles.bundle.css')).href;
fs.writeFileSync(path.join(appRoot, 'index.html'), `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="${stylesheet}">
<style>html,body,#mount{width:100%;height:100%;margin:0;overflow:hidden}.mw-assistant-context{width:380px;height:820px;overflow:hidden}</style>
</head><body><main id="mount"></main><script src="./renderer.js"></script></body></html>`);

fs.writeFileSync(path.join(appRoot, 'package.json'), JSON.stringify({
  name: 'ace-session-history-electron-benchmark',
  version: '1.0.0',
  main: 'main.cjs',
}));
const mainSource = `
const { app, BrowserWindow } = require('electron');
const assert = require('node:assert/strict');
const path = require('node:path');
app.setPath('userData', path.join(__dirname, 'user-data'));
app.commandLine.appendSwitch('disable-background-timer-throttling');
app.whenReady().then(async () => {
  const window = new BrowserWindow({
    show: false,
    width: 420,
    height: 860,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
  });
  try {
    await window.loadFile(path.join(__dirname, 'index.html'));
    await window.webContents.executeJavaScript('window.runSessionHistoryBenchmark().then((value) => { window.__benchmarkResult = value; })');
    let result;
    const deadline = Date.now() + 60000;
    while (!result && Date.now() < deadline) {
      result = await window.webContents.executeJavaScript('window.__benchmarkResult || null');
      if (!result) await new Promise((resolve) => setTimeout(resolve, 25));
    }
    if (!result) throw new Error('Electron session history benchmark timed out');
    window.webContents.debugger.attach('1.3');
    try {
      await window.webContents.debugger.sendCommand('Accessibility.enable');
      const { nodes } = await window.webContents.debugger.sendCommand('Accessibility.getFullAXTree');
      const navigation = nodes.some((node) => node.role?.value === 'navigation' && node.name?.value === '最近会话');
      const openButton = nodes.some((node) => node.role?.value === 'button' && node.name?.value === '打开会话：Session 19999');
      if (!navigation || !openButton) {
        console.error('AX_SNAPSHOT ' + JSON.stringify(nodes.map((node) => ({
          role: node.role?.value, name: node.name?.value, ignored: node.ignored,
        })).slice(-60)));
      }
      assert.ok(navigation, 'Electron accessibility tree must expose the session list navigation name');
      assert.ok(openButton, 'Electron accessibility tree must expose a virtualized session open button');
      result.accessibility = { navigation, openButton };
    } finally {
      window.webContents.debugger.detach();
    }
    console.log('BENCH_RESULT ' + JSON.stringify(result));
  } catch (error) {
    console.error('BENCH_ERROR ' + (error?.stack || String(error)));
    process.exitCode = 1;
  } finally {
    window.destroy();
    app.quit();
  }
});
`;
fs.writeFileSync(path.join(appRoot, 'main.cjs'), mainSource);

const inherited = new Set([
  'PATH', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT', 'PROGRAMFILES',
  'PROGRAMFILES(X86)', 'PROGRAMW6432', 'OS', 'PROCESSOR_ARCHITECTURE', 'DISPLAY',
  'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LANG', 'LC_ALL',
]);
const env = Object.fromEntries(
  Object.entries(process.env).filter(([key]) => inherited.has(key.toUpperCase())),
);
env.TEMP = tempRoot;
env.TMP = tempRoot;
env.TMPDIR = tempRoot;
env.HOME = tempRoot;
env.USERPROFILE = tempRoot;
env.APPDATA = path.join(tempRoot, 'appdata');
env.LOCALAPPDATA = path.join(tempRoot, 'localappdata');
fs.mkdirSync(env.APPDATA, { recursive: true });
fs.mkdirSync(env.LOCALAPPDATA, { recursive: true });

const child = spawn(electronExecutable, [appRoot], {
  cwd: appRoot,
  env,
  windowsHide: true,
  stdio: ['ignore', 'pipe', 'pipe'],
});
let stdout = '';
let stderr = '';
child.stdout.setEncoding('utf8').on('data', (chunk) => { stdout += chunk; });
child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
const exit = await new Promise((resolve, reject) => {
  const timer = setTimeout(() => {
    child.kill();
    reject(new Error('Electron benchmark process timed out'));
  }, 90000);
  child.once('error', reject);
  child.once('exit', (code, signal) => {
    clearTimeout(timer);
    resolve({ code, signal });
  });
});
assert.equal(exit.code, 0, `Electron benchmark failed (${exit.code}/${exit.signal})\n${stderr}\n${stdout}`);
const resultLine = stdout.split(/\r?\n/u).find((line) => line.startsWith('BENCH_RESULT '));
assert.ok(resultLine, `Electron benchmark returned no measurements\n${stderr}\n${stdout}`);
const result = JSON.parse(resultLine.slice('BENCH_RESULT '.length));
log(JSON.stringify(result, null, 2));

const tempReal = fs.realpathSync(tempRoot);
const systemTempReal = fs.realpathSync(os.tmpdir());
assert.equal(path.dirname(tempReal), systemTempReal, 'benchmark scratch directory must remain under the system temp root');
assert.ok(path.basename(tempReal).startsWith('ace-session-history-electron-'));
fs.rmSync(tempReal, { recursive: true, force: true });
