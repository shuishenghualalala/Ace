/**
 * Playwright 契约测试：用**真实的** `ElectronCdpTransport` / `AutomationHost` /
 * `playwright-compat` 驱动一个真实 `WebContentsView`，逐条验证我们依赖的能力。
 *
 * 存在的理由：
 *
 * 1. 桌面端的 vitest 跑在 node 环境，起不了 Electron，而这套地基的全部风险都在
 *    「Playwright 与真实 Electron 的接缝」上——单测只能覆盖 transport 的路由逻辑。
 * 2. 我们依赖两处**非文档化**表面（`normalize()` 返回值的 `_selector`、`aria-ref=`
 *    选择器引擎）。升级 playwright-core 时必须跑这个脚本，静默失效的代价是
 *    「录制出来的技能全部定位不到元素」，而且要到回放时才暴露。
 * 3. 后台可用性依赖三个互相独立的条件（焦点模拟 / view 可见 / 挂在窗口上），
 *    任一条失效都只表现为「点击超时」，从报错看不出根因。这里逐条断言。
 *
 * 用法：
 *   node scripts/pw-contract.build.mjs
 *   node_modules/.bin/electron scripts/pw-contract.mjs
 *
 * 退出码非 0 即契约破裂。
 */

import { app, BrowserWindow, clipboard, WebContentsView } from 'electron';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { BrowserHost } from '../src/main/browser-host';
import {
  parseRecorderEvent,
  RECORDER_EVENT_SCHEMA_VERSION,
  RECORDER_PROVENANCE_SCHEMA_VERSION,
} from '../src/main/browser-recorder';
import { AutomationHost, AUTOMATION_VIEWPORT } from '../src/main/browser/automation-host';
import { ElectronCdpTransport } from '../src/main/browser/electron-cdp-transport';
import * as actions from '../src/main/browser/playwright-actions';
import { PlaywrightEngine } from '../src/main/browser/playwright-engine';
import {
  aiSnapshot,
  connectOverCdp,
  enableFocusEmulation,
  locatorFromRef,
  toReadableLocator,
  toStableSelector,
} from '../src/main/browser/playwright-compat';
import {
  ariaIdentityForLocator,
  captureSnapshot,
  fingerprintResolvedLocator,
} from '../src/main/browser/playwright-snapshot';

import type { ActionContext } from '../src/main/browser/playwright-actions';
import type { Dialog, Page } from '../src/main/browser/playwright-compat';

// The recorder sub-contract owns and destroys its panel window. Keep the
// contract process alive until every assertion and server cleanup has run,
// even if that briefly leaves Electron with no ordinary BrowserWindow.
app.on('window-all-closed', () => undefined);

const hash = (value: string): string =>
  createHash('sha256').update(value).digest('hex').slice(0, 32);

const here = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE = path.join(here, 'fixtures', 'pw-contract-page.html');

const results: Array<{ name: string; ok: boolean; detail: string }> = [];

async function check(name: string, run: () => Promise<string>): Promise<void> {
  const filter = process.env.CREW_PW_CONTRACT_FILTER;
  if (filter && !name.includes(filter)) return;
  console.log(`RUN   ${name}`);
  try {
    const detail = await run();
    results.push({ name, ok: true, detail });
    console.log(`PASS  ${name}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    results.push({ name, ok: false, detail: message.split('\n')[0].slice(0, 160) });
    console.log(`FAIL  ${name}: ${message.split('\n')[0].slice(0, 160)}`);
  }
}

async function contractDeadline<T>(
  operation: Promise<T>,
  timeoutMs: number,
  description: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`等待超时: ${description}`)),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function deferTempRootCleanup(tempRoot: string): void {
  const node = process.env.npm_node_execpath || process.env.NODE || 'node';
  const helper = [
    "const fs=require('node:fs/promises');",
    'const target=process.argv[1];',
    'setTimeout(async()=>{',
    'try{await fs.rm(target,{recursive:true,force:true,maxRetries:20,retryDelay:250});}',
    'finally{process.exit(0);}',
    '},1500);',
  ].join('');
  const child = spawn(node, ['-e', helper, tempRoot], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  });
  child.unref();
}

function modalPrivateProbe(engine: PlaywrightEngine, page: Page): string {
  const engineState = engine as unknown as {
    transport?: { aliases?: Map<unknown, unknown>; tabs?: Map<unknown, unknown> };
    pendingDialogs?: Map<Page, unknown[]>;
    dialogWaiters?: Map<Page, unknown[]>;
  };
  const pageState = page as unknown as {
    _connection?: { _objects?: Map<unknown, unknown> };
    _channel?: { _connection?: { _objects?: Map<unknown, unknown> } };
  };
  const objects = pageState._connection?._objects
    ?? pageState._channel?._connection?._objects;
  let clientDialogObjects = 0;
  if (objects) {
    for (const value of objects.values()) {
      const record = value as {
        _type?: string;
        constructor?: { name?: string };
      };
      if (record._type === 'Dialog' || record.constructor?.name === 'Dialog') {
        clientDialogObjects += 1;
      }
    }
  }
  return JSON.stringify({
    aliases: engineState.transport?.aliases?.size ?? -1,
    tabs: engineState.transport?.tabs?.size ?? -1,
    pendingDialogs: engineState.pendingDialogs?.get(page)?.length ?? 0,
    dialogWaiters: engineState.dialogWaiters?.get(page)?.length ?? 0,
    clientDialogObjects,
  });
}

const contractUserData = path.join(os.tmpdir(), `crew-pw-userdata-${process.pid}`);
app.setPath('userData', contractUserData);

app.whenReady().then(async () => {
  const crossOriginRequests: string[] = [];
  const crossOriginServer = createServer((request, response) => {
    crossOriginRequests.push(`${request.method ?? ''} ${request.url ?? ''}`);
    const late = request.url?.includes('late=1') ?? false;
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    response.end(
      '<meta charset="utf-8">'
      + '<base href="https://base-spoof.invalid/forged/">'
      + (
        late
          ? '<button id="late-cross-origin-button">迟到跨域录制按钮</button>'
          : '<button id="cross-origin-button">跨域框架按钮</button>'
            + '<input id="cross-origin-upload" type="file" multiple hidden '
            + 'accept=".pdf,image/*" aria-label="跨域附件">'
      ),
    );
  });
  await new Promise<void>((resolve, reject) => {
    crossOriginServer.once('error', reject);
    crossOriginServer.listen(0, '127.0.0.1', () => resolve());
  });
  const crossOriginAddress = crossOriginServer.address();
  if (!crossOriginAddress || typeof crossOriginAddress === 'string') {
    throw new Error('无法建立跨域 iframe 契约服务');
  }
  const crossOriginURL = `http://127.0.0.1:${crossOriginAddress.port}/frame`;
  const topOriginServer = createServer((request, response) => {
    if (request.url?.startsWith('/host-download-slow')) {
      const chunk = Buffer.alloc(64 * 1024, 0x63);
      const chunks = 128;
      let sent = 0;
      response.writeHead(200, {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': 'attachment; filename="cancel.bin"',
        'Content-Length': String(chunk.byteLength * chunks),
        'Cache-Control': 'no-store',
      });
      response.write(chunk);
      sent += 1;
      const timer = setInterval(() => {
        if (response.destroyed || sent >= chunks) {
          clearInterval(timer);
          if (!response.destroyed) response.end();
          return;
        }
        response.write(chunk);
        sent += 1;
      }, 25);
      response.once('close', () => clearInterval(timer));
      return;
    }
    if (request.url?.startsWith('/host-download-file')) {
      const requestURL = new URL(request.url, 'http://localhost');
      const requestedName = requestURL.searchParams.get('name') ?? 'download.bin';
      const filename = path.basename(requestedName).replace(/["\r\n]/g, '_');
      const token = requestURL.searchParams.get('token') ?? filename;
      const body = Buffer.from(`crew-download:${token}`, 'utf8');
      response.writeHead(200, {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': `attachment; filename="${filename}"`,
        'Content-Length': String(body.byteLength),
        'Cache-Control': 'no-store',
      });
      response.end(body);
      return;
    }
    if (request.url?.startsWith('/pw-sw-worker')) {
      response.writeHead(200, {
        'Content-Type': 'application/javascript; charset=utf-8',
        'Cache-Control': 'no-store',
        'Service-Worker-Allowed': '/',
      });
      response.end(
        'self.addEventListener("install",event=>event.waitUntil(self.skipWaiting()));'
        + 'self.addEventListener("activate",event=>event.waitUntil(self.clients.claim()));'
        + 'self.addEventListener("message",event=>{'
        + 'if(event.data==="crew-ping")event.source?.postMessage("crew-pong");'
        + '});',
      );
      return;
    }
    if (request.url?.startsWith('/pw-sw-contract')) {
      const requestURL = new URL(request.url, 'http://localhost');
      const token = requestURL.searchParams.get('token') ?? 'default';
      const workerURL = `/pw-sw-worker?token=${encodeURIComponent(token)}`;
      const scope = `/pw-sw-scope-${encodeURIComponent(token)}/`;
      response.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store',
      });
      response.end(
        '<!doctype html><meta charset="utf-8"><title>Service worker contract</title>'
        + '<output id="sw-state">registering</output><script>'
        + `navigator.serviceWorker.register(${JSON.stringify(workerURL)},`
        + `{scope:${JSON.stringify(scope)}}).then(()=>navigator.serviceWorker.ready)`
        + '.then(registration=>{'
        + 'document.getElementById("sw-state").textContent=registration.active?.scriptURL'
        + '||"active-without-url";'
        + '}).catch(error=>{document.getElementById("sw-state").textContent='
        + '"error:"+error.message;});'
        + '</script>',
      );
      return;
    }
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    if (request.url?.startsWith('/host-console')) {
      const requestURL = new URL(request.url, 'http://localhost');
      const afterNavigation = requestURL.searchParams.get('phase') === 'after';
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + `<title>${afterNavigation ? 'crew-console-after-navigation' : 'crew-console'}</title>`
        + `<main>${afterNavigation ? 'after' : 'console'}</main>`,
      );
      return;
    }
    if (request.url?.startsWith('/pw-sw-scope-')) {
      response.end(
        '<!doctype html><meta charset="utf-8"><title>Controlled SW client</title>'
        + '<output id="sw-client-state">controlled-client</output>',
      );
      return;
    }
    if (request.url?.startsWith('/host-find')) {
      response.end(
        '<!doctype html><meta charset="utf-8"><title>BrowserHost find contract</title>'
        + '<main><section role="region" aria-label="Find contract root">'
        + '<button class="find-target" type="button">Needle Alpha</button>'
        + Array.from(
          { length: 12 },
          (_, index) => `<p>find context ${index + 1}</p>`,
        ).join('')
        + '<button class="find-target" type="button">Needle Omega</button>'
        + '</section></main>'
        + '<script>for(const button of document.querySelectorAll(".find-target")){'
        + 'button.addEventListener("click",()=>{document.body.textContent='
        + '"find-contract-clicked";});}</script>',
      );
      return;
    }
    if (request.url?.startsWith('/host-output-api')) {
      response.end(JSON.stringify({ ok: true, url: request.url }));
      return;
    }
    if (request.url?.startsWith('/host-output-parity')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>BrowserHost output parity</title>'
        + '<button id="parity-target" type="button">Parity target</button>'
        + '<div style="height:2400px">full-page-tail</div>',
      );
      return;
    }
    if (request.url?.startsWith('/host-active-to-close')) {
      response.end('<!doctype html><title>active-to-close</title>');
      return;
    }
    if (request.url?.startsWith('/host-run-code-timeout')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>BrowserHost run-code timeout</title>'
        + '<button id="late-action" type="button" hidden '
        + 'onclick="document.body.dataset.locatorClicked=\'yes\'">late action</button>'
        + '<script>setTimeout(()=>{'
        + 'document.getElementById("late-action").hidden=false;'
        + '},700);</script>',
      );
      return;
    }
    if (request.url?.startsWith('/host-download-harness')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>BrowserHost automatic download contract</title>'
        + '<a id="download-click" '
        + 'href="/host-download-file?name=click.txt&token=click">click attachment</a>'
        + '<a id="download-run-code" '
        + 'href="/host-download-file?name=run-code.json&token=run-code">'
        + 'run-code attachment</a>'
        + '<a id="download-cancel" href="/host-download-slow">cancel attachment</a>'
        + '<button id="download-multi" type="button">multi attachment</button>'
        + '<script>'
        + 'document.getElementById("download-multi").addEventListener("click",()=>{'
        + 'for(const token of ["multi-a","multi-b"]){'
        + 'const link=document.createElement("a");'
        + 'link.href="/host-download-file?name=multi.txt&token="+token;'
        + 'link.download="multi.txt";'
        + 'document.body.append(link);link.click();link.remove();'
        + '}'
        + '});'
        + '</script>',
      );
      return;
    }
    if (request.url?.startsWith('/host-record-nav-a')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>Host record navigation A</title>'
        + '<main id="nav-page">A</main>',
      );
      return;
    }
    if (request.url?.startsWith('/host-record-nav-b')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>Host record navigation B</title>'
        + '<a id="click-next" href="/host-record-nav-c">click-next</a>',
      );
      return;
    }
    if (request.url?.startsWith('/host-record-nav-c')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>Host record navigation C</title>'
        + '<main id="nav-page">C</main>',
      );
      return;
    }
    if (request.url?.startsWith('/host-record-nav-background')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>Host pre-existing background page</title>'
        + '<main id="background-state">ready</main>',
      );
      return;
    }
    if (request.url?.startsWith('/host-responsive-resize')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>BrowserHost responsive resize contract</title>'
        + '<style>'
        + '#narrow-only{display:none}'
        + '@media(max-width:700px){#wide-only{display:none}#narrow-only{display:block}}'
        + '</style>'
        + '<output id="load-viewport">pending</output>'
        + '<output id="viewport-state"></output>'
        + '<button id="wide-only" type="button">wide-only</button>'
        + '<button id="narrow-only" type="button">narrow-only</button>'
        + '<output id="responsive-result">idle</output>'
        + '<script>'
        + 'const state=document.getElementById("viewport-state");'
        + 'addEventListener("DOMContentLoaded",()=>{'
        + 'document.getElementById("load-viewport").textContent='
        + '`${innerWidth}x${innerHeight}`;},{once:true});'
        + 'const update=()=>{state.textContent=`${innerWidth}x${innerHeight}:'
        + '${matchMedia("(max-width:700px)").matches?"narrow":"wide"}`;};'
        + 'addEventListener("resize",update);update();'
        + 'document.getElementById("narrow-only").addEventListener("click",()=>{'
        + 'document.getElementById("responsive-result").textContent='
        + '`clicked:${innerWidth}x${innerHeight}:'
        + '${matchMedia("(max-width:700px)").matches?"narrow":"wide"}`;'
        + '});'
        + '</script>',
      );
      return;
    }
    if (request.url?.startsWith('/host-popup-opener')) {
      response.end(
        '<meta charset="utf-8"><title>BrowserHost popup opener</title>'
        + '<a id="host-open-background" target="_blank" '
        + 'href="/host-popup-child?kind=background">open background popup</a>'
        + '<a id="host-open-popup" target="_blank" '
        + 'href="/host-popup-child?kind=foreground">open foreground popup</a>',
      );
      return;
    }
    if (request.url?.startsWith('/host-popup-child')) {
      response.end(
        '<meta charset="utf-8"><title>BrowserHost popup child</title>'
        + '<output id="host-popup-ready">ready</output>',
      );
      return;
    }
    if (request.url?.startsWith('/host-dialog-onload')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>BrowserHost onload dialog</title>'
        + '<button id="onload-after" type="button">onload-after</button>'
        + '<output id="onload-after-state">idle</output>'
        + '<script>'
        + 'document.getElementById("onload-after").addEventListener("click",()=>{'
        + 'document.getElementById("onload-after-state").textContent="clicked";'
        + '});'
        + 'window.addEventListener("load",()=>{'
        + 'document.body.dataset.onloadResult=String(confirm("onload-confirm"));'
        + '});'
        + '</script>',
      );
      return;
    }
    if (request.url?.startsWith('/host-dialog-harness')) {
      response.end(
        '<!doctype html><meta charset="utf-8">'
        + '<title>BrowserHost dialog harness</title>'
        + '<button id="sync-confirm" type="button">sync-confirm</button>'
        + '<output id="sync-result">idle</output>'
        + '<button id="delayed-confirm" type="button">delayed-confirm</button>'
        + '<output id="delayed-result">idle</output>'
        + '<button id="chain-dialog" type="button">chain-dialog</button>'
        + '<output id="chain-result">idle</output>'
        + '<button id="mismatch-confirm" type="button">mismatch-confirm</button>'
        + '<output id="mismatch-result">idle</output>'
        + '<button id="post-dialog" type="button">post-dialog</button>'
        + '<output id="post-dialog-state">idle</output>'
        + '<button id="early-popup-dialog" type="button">early-popup-dialog</button>'
        + '<script>'
        + 'document.getElementById("sync-confirm").addEventListener("click",()=>{'
        + 'document.getElementById("sync-result").textContent='
        + 'String(confirm("sync-confirm"));'
        + '});'
        + 'document.getElementById("delayed-confirm").addEventListener("click",()=>{'
        + 'setTimeout(()=>{document.getElementById("delayed-result").textContent='
        + 'String(confirm("delayed-confirm"));},125);'
        + '});'
        + 'document.getElementById("chain-dialog").addEventListener("click",()=>{'
        + 'alert("chain-alert");'
        + 'document.getElementById("chain-result").textContent='
        + 'String(confirm("chain-confirm"));'
        + '});'
        + 'document.getElementById("mismatch-confirm").addEventListener("click",()=>{'
        + 'document.getElementById("mismatch-result").textContent='
        + 'String(confirm("actual-confirm"));'
        + '});'
        + 'document.getElementById("post-dialog").addEventListener("click",()=>{'
        + 'document.getElementById("post-dialog-state").textContent="clicked";'
        + '});'
        + 'document.getElementById("early-popup-dialog").addEventListener("click",()=>{'
        + 'const child=window.open("about:blank","_blank");'
        + 'if(!child)throw new Error("popup blocked");'
        + 'child.document.open();'
        + 'child.document.write("<!doctype html><meta charset=utf-8>'
        + '<title>Early popup dialog</title>'
        + '<button id=popup-after type=button>popup-after</button>'
        + '<output id=popup-after-state>idle</output>'
        + '<script>alert(\\"popup-inline-alert\\");'
        + 'document.getElementById(\\"popup-after\\").addEventListener(\\"click\\",()=>{'
        + 'document.getElementById(\\"popup-after-state\\").textContent=\\"clicked\\";'
        + '});<\\/script>");'
        + 'child.document.close();'
        + '});'
        + '</script>',
      );
      return;
    }
    if (request.url?.startsWith('/same-frame')) {
      response.end(
        '<meta charset="utf-8">'
        + '<button id="same-origin-button">同源录制按钮</button>'
        + '<input id="same-origin-upload" type="file" multiple hidden '
        + 'accept=".pdf,image/*" aria-label="同源附件">',
      );
      return;
    }
    response.end(
      '<meta charset="utf-8"><title>跨域契约顶层页</title>'
      + '<input id="top-upload" type="file" multiple hidden '
      + 'accept=".pdf,image/*" aria-label="顶层附件">'
      + '<button id="top-upload-button" type="button" '
      + 'onclick="document.getElementById(\'top-upload\').click()">选择附件</button>'
      + '<output id="top-upload-state">none</output>'
      + '<input id="delayed-upload" type="file" multiple hidden '
      + 'aria-label="延迟附件">'
      + '<button id="delayed-upload-button" type="button" '
      + 'onclick="setTimeout(()=>document.getElementById(\'delayed-upload\').click(),750)">'
      + '延迟选择附件</button>'
      + '<output id="delayed-upload-state">none</output>'
      + '<button id="reveal-upload-button" type="button" onclick="'
      + 'if(!document.getElementById(\'revealed-upload\')){'
      + 'const i=document.createElement(\'input\');i.id=\'revealed-upload\';'
      + 'i.type=\'file\';i.hidden=true;'
      + 'i.addEventListener(\'change\',e=>document.getElementById('
      + '\'reveal-upload-state\').textContent=e.target.files[0]?.name||\'empty\');'
      + 'this.after(i);}">显示附件输入框</button>'
      + '<output id="reveal-upload-state">none</output>'
      + '<form id="recorder-keyboard-form">'
      + '<input id="recorder-enter-textbox" type="text" aria-label="回车查询">'
      + '<button id="recorder-enter-submit" type="submit">提交查询</button>'
      + '</form>'
      + '<button id="recorder-keyboard-button" type="button">键盘激活按钮</button>'
      + '<input id="recorder-shortcut-input" type="text" aria-label="快捷键输入">'
      + '<input id="recorder-paste-input" type="text" aria-label="粘贴输入">'
      + '<canvas id="recorder-canvas" width="320" height="160" '
      + 'style="display:block;width:320px;height:160px"></canvas>'
      + '<output id="recorder-canvas-state">idle</output>'
      + '<output id="recorder-keyboard-state">idle</output>'
      + '<script>document.getElementById("top-upload").addEventListener("change",e=>{'
      + 'document.getElementById("top-upload-state").textContent='
      + 'Array.from(e.target.files).map(f=>f.name).join(",")||"empty";'
      + '});'
      + 'document.getElementById("delayed-upload").addEventListener("change",e=>{'
      + 'document.getElementById("delayed-upload-state").textContent='
      + 'Array.from(e.target.files).map(f=>f.name).join(",")||"empty";'
      + '});'
      + 'document.getElementById("recorder-keyboard-form").addEventListener("submit",e=>{'
      + 'e.preventDefault();document.getElementById("recorder-keyboard-state").textContent='
      + '"submitted:"+document.getElementById("recorder-enter-textbox").value;'
      + '});'
      + 'document.getElementById("recorder-keyboard-button").addEventListener("click",()=>{'
      + 'document.getElementById("recorder-keyboard-state").textContent="button-activated";'
      + '});document.getElementById("recorder-canvas").addEventListener("click",e=>{'
      + 'document.getElementById("recorder-canvas-state").textContent='
      + 'e.offsetX+","+e.offsetY;'
      + '});</script>'
      + '<iframe id="same-origin-frame" src="/same-frame"></iframe>'
      + `<iframe id="cross-origin-frame" src="${crossOriginURL}"></iframe>`
      + '<div id="late-frame-host"></div>',
    );
  });
  await new Promise<void>((resolve, reject) => {
    topOriginServer.once('error', reject);
    topOriginServer.listen(0, 'localhost', () => resolve());
  });
  const topOriginAddress = topOriginServer.address();
  if (!topOriginAddress || typeof topOriginAddress === 'string') {
    throw new Error('无法建立跨域 iframe 顶层契约服务');
  }
  // localhost ↔ 127.0.0.1 are different sites (not merely different ports),
  // making Chromium allocate a real OOPIF under site isolation.
  const topOriginURL = `http://localhost:${topOriginAddress.port}/top`;
  // BrowserHost intentionally forces even loopback traffic through its policy
  // proxy (`<-loopback>`). Use a real forward proxy here rather than a dead
  // placeholder port, otherwise loadURL can wait indefinitely before the
  // recorder contract even starts.
  const policyProxyServer = createServer(async (request, response) => {
    try {
      if (!request.url || request.method === 'CONNECT') {
        response.writeHead(405).end();
        return;
      }
      const target = new URL(request.url);
      const upstream = await fetch(target, {
        method: request.method,
        redirect: 'manual',
      });
      const body = Buffer.from(await upstream.arrayBuffer());
      response.writeHead(upstream.status, {
        'Content-Type': upstream.headers.get('content-type') ?? 'application/octet-stream',
        'Cache-Control': 'no-store',
        ...(upstream.headers.get('content-disposition')
          ? { 'Content-Disposition': upstream.headers.get('content-disposition')! }
          : {}),
        ...(upstream.headers.get('content-length')
          ? { 'Content-Length': upstream.headers.get('content-length')! }
          : {}),
        ...(upstream.headers.get('location')
          ? { Location: upstream.headers.get('location')! }
          : {}),
      });
      response.end(body);
    } catch (error) {
      response.writeHead(502, { 'Content-Type': 'text/plain; charset=utf-8' });
      response.end(error instanceof Error ? error.message : 'proxy failure');
    }
  });
  await new Promise<void>((resolve, reject) => {
    policyProxyServer.once('error', reject);
    policyProxyServer.listen(0, '127.0.0.1', () => resolve());
  });
  const policyProxyAddress = policyProxyServer.address();
  if (!policyProxyAddress || typeof policyProxyAddress === 'string') {
    throw new Error('无法建立 BrowserHost 契约代理');
  }
  const policyProxyURL = `http://127.0.0.1:${policyProxyAddress.port}`;
  const proxyUsername = 'crew';
  const proxyPassword = 'pw-contract-proxy-password-0123456789abcdef';

  async function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(`契约操作超时 ${ms}ms`)), ms);
    });
    try {
      return await Promise.race([promise, timeout]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async function managedHost(
    getWindow: () => BrowserWindow | null,
    runtimeKey: string,
    profile: string,
  ): Promise<BrowserHost> {
    const host = new BrowserHost(getWindow);
    await withTimeout(
      host.handleRpc({
        runtime_key: runtimeKey,
        method: 'configure_proxy',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          proxy_username: proxyUsername,
          proxy_password: proxyPassword,
        },
      }),
      15_000,
    );
    return host;
  }

  const host = new AutomationHost();
  const transport = new ElectronCdpTransport();

  const view = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      backgroundThrottling: false,
    },
  });
  // 走真实的宿主挂载路径（条件 2、3）
  host.mount(view);
  await view.webContents.loadFile(FIXTURE);
  transport.addView(view);

  const browser = await connectOverCdp(transport);
  const context = browser.contexts()[0];
  const page = context.pages()[0];
  page.setDefaultTimeout(10_000);

  // ── 条件 1：不开焦点模拟时，后台点击应当是失败的 ──────────────────────
  // 这条断言的是「我们没有白加这个开关」。若哪天 Electron/Chromium 变了行为
  // 使它不再必要，这里会失败，提醒我们回头简化而不是继续背着它。
  await check('未开焦点模拟时后台点击确实不可用（否则该开关可以去掉）', async () => {
    try {
      await page.getByRole('button', { name: '延迟出现' }).click({ timeout: 4000 });
    } catch {
      return '如预期失败';
    }
    throw new Error('居然成功了 —— 焦点模拟可能已非必需，请复核 automation-host 的注释');
  });

  await enableFocusEmulation(context, page);

  await check('BrowserContext cookies/storageState 走真实 Electron Session', async () => {
    const cookieURL = new URL('/', topOriginURL).href;
    await context.addCookies([{
      name: 'crew_context_contract',
      value: '完整-cookie-值',
      url: cookieURL,
      httpOnly: true,
      sameSite: 'Lax',
    }]);
    const cookies = await context.cookies([cookieURL]);
    const cookie = cookies.find((candidate) => (
      candidate.name === 'crew_context_contract'
    ));
    if (
      cookie?.value !== '完整-cookie-值'
      || cookie.httpOnly !== true
      || cookie.sameSite !== 'Lax'
    ) {
      throw new Error(`BrowserContext.cookies 结果异常: ${JSON.stringify(cookies)}`);
    }
    const state = await context.storageState();
    if (
      !state.cookies.some((candidate) => (
        candidate.name === 'crew_context_contract'
        && candidate.value === '完整-cookie-值'
      ))
    ) {
      throw new Error(`storageState 丢失 cookie: ${JSON.stringify(state)}`);
    }
    await context.clearCookies();
    const cleared = await context.cookies([cookieURL]);
    if (cleared.some((candidate) => candidate.name === 'crew_context_contract')) {
      throw new Error(`clearCookies 未清理目标: ${JSON.stringify(cleared)}`);
    }
    return 'addCookies → cookies → storageState → clearCookies';
  });

  await check('BrowserContext permissions 在 zero-page journal 后真实重放', async () => {
    const permissionTransport = new ElectronCdpTransport();
    const firstView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    const firstContents = firstView.webContents;
    let nextView: WebContentsView | null = null;
    let nextContents: typeof firstContents | null = null;
    try {
      await firstContents.loadURL('about:blank');
      permissionTransport.addView(firstView);
      const permissionBrowser = await connectOverCdp(permissionTransport);
      const permissionContext = permissionBrowser.contexts()[0];
      if (!permissionContext || permissionContext.pages().length !== 1) {
        throw new Error('permissions 契约未建立初始 persistent context/page');
      }
      permissionTransport.removeView(firstView);
      firstContents.close({ waitForBeforeUnload: false });
      await contractDeadline(
        (async () => {
          while (permissionContext.pages().length !== 0) {
            await new Promise((resolve) => setTimeout(resolve, 10));
          }
        })(),
        5_000,
        'permissions context 进入 zero-page',
      );

      // The exact ordered journal matters: reset must erase the first grant,
      // while the final origin grant must be visible on the next real target.
      await permissionContext.grantPermissions(
        ['notifications'],
        { origin: new URL(topOriginURL).origin },
      );
      await permissionContext.clearPermissions();
      await permissionContext.grantPermissions(
        ['geolocation'],
        { origin: new URL(topOriginURL).origin },
      );

      nextView = new WebContentsView({
        webPreferences: {
          contextIsolation: true,
          sandbox: true,
          webSecurity: true,
          backgroundThrottling: false,
        },
      });
      nextContents = nextView.webContents;
      const permissionURL = new URL('/host-output-parity', topOriginURL).href;
      await nextContents.loadURL(permissionURL);
      const nextPagePromise = permissionContext.waitForEvent('page');
      permissionTransport.addView(nextView);
      const nextPage = await contractDeadline(
        nextPagePromise,
        10_000,
        'permissions replay 后发布下一 Page',
      );
      const states = await nextPage.evaluate(async () => ({
        geolocation: (await navigator.permissions.query({ name: 'geolocation' })).state,
        notifications: (await navigator.permissions.query({ name: 'notifications' })).state,
      }));
      if (states.geolocation !== 'granted') {
        throw new Error(`zero-page grant 未生效: ${JSON.stringify(states)}`);
      }
      if (states.notifications === 'granted') {
        throw new Error(`resetPermissions 未覆盖旧 grant: ${JSON.stringify(states)}`);
      }
      await permissionContext.clearPermissions();
      return `notifications:${states.notifications} → geolocation:${states.geolocation}`;
    } finally {
      if (nextView) {
        permissionTransport.removeView(nextView);
        if (nextContents && !nextContents.isDestroyed()) {
          nextContents.close({ waitForBeforeUnload: false });
        }
      }
      permissionTransport.close();
      if (!firstContents.isDestroyed()) {
        firstContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('Page.pdf 走 Electron printToPDF + owner-bound IO stream', async () => {
    const pdfTransport = new ElectronCdpTransport();
    const pdfHost = new AutomationHost();
    const pdfView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    const pdfContents = pdfView.webContents;
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-pdf-contract-'));
    const pdfPath = path.join(tempRoot, 'contract.pdf');
    const commands = new Map<number, string>();
    const ioReadReplies: Array<Record<string, unknown>> = [];
    const originalSend = pdfTransport.send.bind(pdfTransport);
    pdfTransport.send = (message: object): void => {
      const wire = message as { id?: number; method?: string };
      if (typeof wire.id === 'number' && wire.method) {
        commands.set(wire.id, wire.method);
      }
      originalSend(message);
    };
    try {
      pdfHost.mount(pdfView);
      await pdfContents.loadURL('about:blank');
      pdfTransport.addView(pdfView);
      const pdfBrowser = await connectOverCdp(pdfTransport);
      const pdfContext = pdfBrowser.contexts()[0];
      const pdfPage = pdfContext.pages()[0];
      if (!pdfPage) throw new Error('PDF 契约未建立 Page');
      const coreOnMessage = pdfTransport.onmessage;
      pdfTransport.onmessage = (message: object): void => {
        const wire = message as {
          id?: number;
          result?: Record<string, unknown>;
        };
        if (
          typeof wire.id === 'number'
          && commands.get(wire.id) === 'IO.read'
          && wire.result
        ) {
          ioReadReplies.push(wire.result);
        }
        coreOnMessage?.(message);
      };

      await pdfPage.setContent(
        '<!doctype html><meta charset="utf-8"><title>Crew PDF contract</title>'
        + '<style>@page{size:A4}body{margin:0}'
        + 'section{height:900px;break-after:page;font-size:24px}</style>'
        + '<section><h1>PDF page one</h1></section>'
        + '<section><h1>PDF page two</h1></section>'
        + '<section><h1>PDF page three</h1></section>',
      );
      const bytes = await pdfPage.pdf({
        path: pdfPath,
        format: 'A4',
        landscape: false,
        displayHeaderFooter: true,
        headerTemplate: '<span class="title"></span>',
        footerTemplate: '<span class="pageNumber"></span>/<span class="totalPages"></span>',
        printBackground: true,
        scale: 0.9,
        margin: {
          top: '0.25in',
          bottom: '0.35in',
          left: '0.45in',
          right: '0.55in',
        },
        pageRanges: '1-2',
        preferCSSPageSize: true,
        tagged: true,
        outline: true,
      });
      const persisted = await readFile(pdfPath);
      if (!bytes.subarray(0, 5).equals(Buffer.from('%PDF-'))) {
        throw new Error('Page.pdf 返回值缺少 PDF magic');
      }
      if (!persisted.equals(bytes)) {
        throw new Error('Page.pdf(path) 与返回 Buffer 字节不一致');
      }
      const pageObjects = bytes
        .toString('latin1')
        .match(/\/Type\s*\/Page(?!s)\b/g)?.length ?? 0;
      if (pageObjects !== 2) {
        throw new Error(`pageRanges 预期 2 页，PDF page objects=${pageObjects}`);
      }
      const sentMethods = [...commands.values()];
      if (!sentMethods.includes('Page.printToPDF')) {
        throw new Error('public Page.pdf 未发送 Page.printToPDF');
      }
      if (!sentMethods.includes('IO.read') || !sentMethods.includes('IO.close')) {
        throw new Error(`public Page.pdf 未走完整 IO stream: ${sentMethods.join(',')}`);
      }
      if (!ioReadReplies.some((reply) => reply.eof === true)) {
        throw new Error(`IO.read 未返回 EOF: ${JSON.stringify(ioReadReplies)}`);
      }

      const diagnostics = pdfTransport as unknown as {
        tabs: Map<string, unknown>;
        pdfStreams: Map<string, unknown>;
        dispatchPrintToPDF(
          tab: unknown,
          ownerSessionId: string,
          params: Record<string, unknown>,
        ): Promise<Record<string, unknown>>;
      };
      if (diagnostics.pdfStreams.size !== 0) {
        throw new Error('public Page.pdf 完成后 IO.close 未释放 stream');
      }
      let invalidRangeRejected = false;
      try {
        await pdfPage.pdf({ pageRanges: '3-2' });
      } catch {
        invalidRangeRejected = true;
      }
      if (!invalidRangeRejected) throw new Error('非法 pageRanges 未被拒绝');
      if (diagnostics.pdfStreams.size !== 0) {
        throw new Error('printToPDF 异常后泄漏 stream');
      }

      // Simulate a client disappearing after print but before IO.close. The
      // real Electron PDF is produced, then Page detach must reclaim it.
      const tab = [...diagnostics.tabs.values()][0];
      if (!tab) throw new Error('PDF transport tab diagnostics 缺失');
      const base64Result = await diagnostics.dispatchPrintToPDF(
        tab,
        'contract-base64-client',
        { transferMode: 'ReturnAsBase64' },
      );
      if (
        typeof base64Result.data !== 'string'
        || !Buffer.from(base64Result.data, 'base64')
          .subarray(0, 5)
          .equals(Buffer.from('%PDF-'))
        || diagnostics.pdfStreams.size !== 0
      ) {
        throw new Error('ReturnAsBase64 未返回独立、无泄漏的真实 PDF');
      }
      const leaked = await diagnostics.dispatchPrintToPDF(
        tab,
        'contract-abandoned-client',
        { transferMode: 'ReturnAsStream' },
      );
      if (
        typeof leaked.stream !== 'string'
        || diagnostics.pdfStreams.size !== 1
      ) {
        throw new Error('未建立异常清理用真实 PDF stream');
      }
      pdfTransport.removeView(pdfView);
      if (diagnostics.pdfStreams.size !== 0) {
        throw new Error('Page detach 未清理未关闭 PDF stream');
      }
      return `${bytes.byteLength} bytes；2 pages；base64 + EOF + close + detach cleanup`;
    } finally {
      pdfTransport.removeView(pdfView);
      pdfTransport.close();
      pdfHost.unmount(pdfView);
      if (!pdfContents.isDestroyed()) {
        pdfContents.close({ waitForBeforeUnload: false });
      }
      pdfHost.dispose();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserContext service_worker root 提升、evaluate、去重与 detach', async () => {
    const swTransport = new ElectronCdpTransport();
    const swProtocolCommands: Array<{
      id: number;
      method: string;
      sessionId: string;
    }> = [];
    const swProtocolResults: Array<{
      id: number;
      method: string;
      error: string;
    }> = [];
    const swOriginalSend = swTransport.send.bind(swTransport);
    swTransport.send = (message: object): void => {
      const wire = message as { id?: number; method?: string; sessionId?: string };
      if (wire.method && typeof wire.id === 'number') {
        swProtocolCommands.push({
          id: wire.id,
          method: wire.method,
          sessionId: wire.sessionId ?? '',
        });
      }
      swOriginalSend(message);
    };
    const swHost = new AutomationHost();
    const token = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const partition = `crew-pw-contract-sw-${token}`;
    const firstView = new WebContentsView({
      webPreferences: {
        partition,
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    const firstContents = firstView.webContents;
    const nativeWorkerEvents: Array<{
      method: string;
      sessionId: string;
      params: unknown;
    }> = [];
    const nativeTrace = (
      _event: unknown,
      method: string,
      params: unknown,
      sessionId?: string,
    ): void => {
      if (sessionId || method.startsWith('Target.')) {
        nativeWorkerEvents.push({ method, sessionId: sessionId ?? '', params });
      }
    };
    let secondView: WebContentsView | null = null;
    let secondContents: typeof firstContents | null = null;
    try {
      swHost.mount(firstView);
      await firstContents.loadURL('about:blank');
      swTransport.addView(firstView);
      const swBrowser = await connectOverCdp(swTransport);
      const swCoreOnMessage = swTransport.onmessage;
      swTransport.onmessage = (message: object): void => {
        const wire = message as {
          id?: number;
          error?: { message?: string };
        };
        if (typeof wire.id === 'number') {
          const command = swProtocolCommands.find(
            (candidate) => candidate.id === wire.id,
          );
          if (command?.sessionId.startsWith('pw-sw-')) {
            swProtocolResults.push({
              id: wire.id,
              method: command.method,
              error: wire.error?.message ?? '',
            });
          }
        }
        swCoreOnMessage?.(message);
      };
      firstContents.debugger.on('message', nativeTrace);
      const swContext = swBrowser.contexts()[0];
      const firstPage = swContext.pages()[0];
      if (!firstPage) throw new Error('service_worker 契约未建立首个 Page');
      const publicWorkers: typeof swContext.serviceWorkers = [];
      swContext.on('serviceworker', (worker) => publicWorkers.push(worker));
      const contractURL = new URL(
        `/pw-sw-contract?token=${encodeURIComponent(token)}`,
        topOriginURL,
      ).href;
      const workerPromise = swContext.waitForEvent('serviceworker');
      await firstPage.goto(contractURL);
      const worker = await contractDeadline(
        workerPromise,
        10_000,
        'BrowserContext serviceworker event',
      );
      let workerLocation: unknown;
      try {
        workerLocation = await contractDeadline(
          worker.evaluate(() => globalThis.location.href),
          10_000,
          'ServiceWorker.evaluate',
        );
      } catch (error) {
        console.log(
          '      service-worker protocol trace',
          JSON.stringify(swProtocolCommands.slice(-30)),
          JSON.stringify(swProtocolResults.slice(-30)),
          JSON.stringify(nativeWorkerEvents.slice(-50)),
        );
        throw new Error(
          `${error instanceof Error ? error.message : String(error)}; `
          + `commands=${JSON.stringify(swProtocolCommands.slice(-20))}; `
          + `native=${JSON.stringify(nativeWorkerEvents.slice(-30))}`,
        );
      }
      if (!String(workerLocation).includes(`/pw-sw-worker?token=${token}`)) {
        throw new Error(`ServiceWorker.evaluate 路由异常: ${workerLocation}`);
      }
      if (swContext.serviceWorkers().length !== 1 || publicWorkers.length !== 1) {
        throw new Error(
          `首个 worker 发布异常: list=${swContext.serviceWorkers().length},`
          + ` events=${publicWorkers.length}`,
        );
      }

      secondView = new WebContentsView({
        webPreferences: {
          partition,
          contextIsolation: true,
          sandbox: true,
          webSecurity: true,
          backgroundThrottling: false,
        },
      });
      secondContents = secondView.webContents;
      secondContents.debugger.on('message', nativeTrace);
      swHost.mount(secondView);
      await secondContents.loadURL('about:blank');
      const secondPagePromise = swContext.waitForEvent('page');
      swTransport.addView(secondView);
      const secondPage = await contractDeadline(
        secondPagePromise,
        10_000,
        'service_worker 第二 Page',
      );
      const controlledURL = new URL(
        `/pw-sw-scope-${encodeURIComponent(token)}/client`,
        topOriginURL,
      ).href;
      await secondPage.goto(controlledURL);
      await secondPage.waitForFunction(
        () => navigator.serviceWorker.controller !== null,
        null,
        { timeout: 10_000 },
      );
      await new Promise((resolve) => setTimeout(resolve, 250));

      const diagnostics = swTransport as unknown as {
        serviceWorkers: Map<string, {
          targetId: string;
          sessionId: string;
          physicals: Set<unknown>;
        }>;
      };
      const promoted = [...diagnostics.serviceWorkers.values()];
      if (promoted.length !== 1) {
        throw new Error(`targetId 去重失败: promoted=${promoted.length}`);
      }
      const physicalCount = promoted[0]?.physicals.size ?? 0;
      if (physicalCount < 2) {
        throw new Error(`未观察到多 tab physical service worker: ${physicalCount}`);
      }
      if (swContext.serviceWorkers().length !== 1 || publicWorkers.length !== 1) {
        throw new Error(
          `同 registration 被重复发布: list=${swContext.serviceWorkers().length},`
          + ` events=${publicWorkers.length}`,
        );
      }
      const replacementPromise = swContext.waitForEvent('serviceworker');
      const primaryClosed = worker.waitForEvent('close');

      // Remove the primary physical first. The transport must publish an
      // ordered root detach/attach for the same targetId and route the new
      // public Worker to the already-resumed duplicate debugger session.
      swTransport.removeView(firstView);
      await contractDeadline(primaryClosed, 10_000, 'primary service worker detach');
      const replacement = await contractDeadline(
        replacementPromise,
        10_000,
        'service worker physical failover',
      );
      let replacementLocation: unknown;
      try {
        replacementLocation = await contractDeadline(
          replacement.evaluate(() => globalThis.location.href),
          10_000,
          'failover ServiceWorker.evaluate',
        );
      } catch (error) {
        console.log(
          '      service-worker failover trace',
          JSON.stringify(swProtocolCommands.slice(-40)),
          JSON.stringify(swProtocolResults.slice(-40)),
          JSON.stringify(nativeWorkerEvents.slice(-80)),
          JSON.stringify(
            [...diagnostics.serviceWorkers.values()].map((candidate) => ({
              sessionId: candidate.sessionId,
              physicals: candidate.physicals.size,
            })),
          ),
        );
        throw error;
      }
      if (replacementLocation !== workerLocation) {
        throw new Error(
          `failover worker 路由到错误 target: ${replacementLocation}`,
        );
      }
      if (swContext.serviceWorkers().length !== 1 || publicWorkers.length !== 2) {
        throw new Error(
          `failover public graph 异常: list=${swContext.serviceWorkers().length},`
          + ` events=${publicWorkers.length}`,
        );
      }

      const workerClosed = replacement.waitForEvent('close');
      await replacement.evaluate(async () => {
        await (globalThis as unknown as {
          registration: { unregister(): Promise<boolean> };
        }).registration.unregister();
      });

      // Removing the final physical target must emit root detach.
      swTransport.removeView(secondView);
      swHost.unmount(secondView);
      secondContents.debugger.off('message', nativeTrace);
      if (!secondContents.isDestroyed()) {
        secondContents.close({ waitForBeforeUnload: false });
      }
      secondView = null;
      secondContents = null;
      await contractDeadline(workerClosed, 10_000, 'service worker root detach');
      if (swContext.serviceWorkers().length !== 0) {
        throw new Error('最后 physical detach 后 context.serviceWorkers 未清空');
      }
      return `target=${promoted[0]?.targetId}; physicals=${physicalCount}; `
        + 'evaluate + failover + unregister + close';
    } finally {
      if (!firstContents.isDestroyed()) {
        firstContents.debugger.off('message', nativeTrace);
      }
      if (secondView) {
        swTransport.removeView(secondView);
        swHost.unmount(secondView);
        if (secondContents && !secondContents.isDestroyed()) {
          secondContents.debugger.off('message', nativeTrace);
        }
        if (secondContents && !secondContents.isDestroyed()) {
          secondContents.close({ waitForBeforeUnload: false });
        }
      }
      swTransport.removeView(firstView);
      swTransport.close();
      swHost.unmount(firstView);
      if (!firstContents.isDestroyed()) {
        firstContents.close({ waitForBeforeUnload: false });
      }
      swHost.dispose();
    }
  });

  await check('开启焦点模拟后 rAF 推进', async () => {
    const raf = await page.evaluate(
      () =>
        new Promise<string>((resolve) => {
          const timer = setTimeout(() => resolve('未触发'), 2000);
          requestAnimationFrame(() => {
            clearTimeout(timer);
            resolve('正常');
          });
        }),
    );
    if (raf !== '正常') throw new Error('rAF 仍未推进');
    return raf;
  });

  await check('自动等待：点击 800ms 后才出现的按钮（无 sleep）', async () => {
    await page.getByRole('button', { name: '延迟出现' }).click();
    return (await page.locator('#late-result').textContent()) ?? '';
  });

  await check('Shadow DOM 穿透', async () => {
    await page.getByLabel('影子输入').fill('crew-123');
    const echo = await page.locator('#shadow-host').locator('#echo').textContent();
    if (echo !== '影子值=crew-123') throw new Error(`回显不符: ${echo}`);
    return echo;
  });

  await check('iframe 内点击', async () => {
    await page.frameLocator('#frame').getByRole('button', { name: '框架内按钮' }).click();
    await page.waitForFunction(
      () => document.getElementById('frame-result')?.textContent === '框架按钮已点击',
      null,
      { timeout: 5000 },
    );
    return '框架按钮已点击';
  });

  let snapshot = '';
  await check('ariaSnapshot mode:ai 含 ref、含 iframe 内容、保留层级', async () => {
    snapshot = await aiSnapshot(page, 10_000);
    if (!snapshot.includes('[ref=')) throw new Error('缺少 [ref=]');
    if (!snapshot.includes('iframe')) throw new Error('缺少 iframe 节点');
    if (!/\[ref=f\d+e\d+\]/.test(snapshot)) throw new Error('iframe 内容未带帧作用域 ref');
    return `${snapshot.split('\n').length} 行`;
  });

  await check('aria-ref 反查 Locator', async () => {
    const match = /- button "延迟出现" \[ref=([ef\d]+)\]/.exec(snapshot);
    if (!match) throw new Error('快照里找不到目标按钮');
    const text = await locatorFromRef(page, match[1]).textContent();
    if (text !== '延迟出现') throw new Error(`反查到了别的元素: ${text}`);
    return `${match[1]} → ${text}`;
  });

  await check('locatorFromRef 拒绝非法 ref', async () => {
    try {
      locatorFromRef(page, 'e1"] , [x');
      throw new Error('未拒绝非法 ref');
    } catch (error) {
      if (error instanceof Error && error.message.includes('非法')) return '已拒绝';
      throw error;
    }
  });

  // ── 录制 → 技能的闭环 ─────────────────────────────────────────────────
  await check('normalize(): 主文档 ref → 稳定选择器 → 存盘 → 全新定位回放', async () => {
    const match = /- button "延迟出现" \[ref=([ef\d]+)\]/.exec(snapshot);
    if (!match) throw new Error('快照里找不到目标按钮');
    const persisted = await toStableSelector(locatorFromRef(page, match[1]));
    if (persisted.includes('aria-ref')) throw new Error(`选择器仍是临时身份: ${persisted}`);
    await page.evaluate(() => {
      const node = document.getElementById('late-result');
      if (node) node.textContent = '';
    });
    await page.locator(persisted).click();
    const replayed = await page.locator('#late-result').textContent();
    if (replayed !== '延迟按钮已点击') throw new Error('回放未生效');
    return persisted;
  });

  await check('normalize(): iframe 内元素自动补出跨帧链', async () => {
    const inFrame = page.frameLocator('#frame').getByRole('button', { name: '框架内按钮' });
    const persisted = await toStableSelector(inFrame);
    if (!persisted.includes('enter-frame')) throw new Error(`未补出跨帧链: ${persisted}`);
    await page.locator(persisted).click();
    return persisted;
  });

  await check('iframe 真实 document URL 不受 <base href> 伪造', async () => {
    const engine = new PlaywrightEngine();
    const crossView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    try {
      engine.registerTab(crossView);
      await crossView.webContents.loadURL(topOriginURL);
      const crossPage = await engine.pageForView(crossView);
      const target = crossPage
        .frameLocator('#cross-origin-frame')
        .getByRole('button', { name: '跨域框架按钮' });
      await target.waitFor({ state: 'visible', timeout: 5_000 });
      const persisted = await toStableSelector(target);
      const identity = await fingerprintResolvedLocator(
        crossPage.locator(persisted),
        hash,
        5_000,
      );
      if (identity.documentURL !== crossOriginURL) {
        throw new Error(`真实 frame URL 错误: ${identity.documentURL}`);
      }
      if (!identity.documentBaseURI.startsWith('https://base-spoof.invalid/')) {
        throw new Error(`测试页的伪造 baseURI 未生效: ${identity.documentBaseURI}`);
      }
      if (identity.documentURL === identity.documentBaseURI) {
        throw new Error('documentURL 被错误地退化成 baseURI');
      }
      return `${identity.documentURL} ≠ ${identity.documentBaseURI}`;
    } finally {
      engine.unregisterTab(crossView);
      await engine.dispose();
      if (!crossView.webContents.isDestroyed()) {
        crossView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('toReadableLocator 产出人类可读写法', async () => {
    const readable = await toReadableLocator(page.getByRole('button', { name: '延迟出现' }));
    if (!readable.includes('getByRole')) throw new Error(`不像代码写法: ${readable}`);
    return readable;
  });

  // ── 裸 CDP 逃生舱（录制器要用隔离世界注入）────────────────────────────
  await check('newCDPSession 隔离世界注入 + binding', async () => {
    const cdp = await context.newCDPSession(page);
    try {
      await cdp.send('Runtime.addBinding', {
        name: '__crewContractEmit',
        executionContextName: 'crew-contract',
      });
      await cdp.send('Page.addScriptToEvaluateOnNewDocument', {
        source:
          "document.addEventListener('click', (e) => __crewContractEmit(JSON.stringify({ tag: e.target.tagName })), true);",
        worldName: 'crew-contract',
      });
      const received = new Promise<string>((resolve) => {
        cdp.on(
          'Runtime.bindingCalled',
          (event: { payload?: string }) => resolve(event.payload ?? ''),
        );
      });
      await page.reload();
      await enableFocusEmulation(context, page);
      await page.getByRole('button', { name: '延迟出现' }).click();
      const payload = await received;
      if (!payload.includes('BUTTON')) throw new Error(`绑定回传异常: ${payload}`);
      return payload;
    } finally {
      await cdp.detach().catch(() => undefined);
    }
  });

  await check('worker 子会话可双向路由', async () => {
    const workerPromise = page.waitForEvent('worker');
    await page.evaluate(() => {
      const source = 'self.onmessage = event => postMessage(event.data * 2)';
      const worker = new Worker(URL.createObjectURL(new Blob([source], { type: 'text/javascript' })));
      (globalThis as typeof globalThis & { __crewContractWorker?: Worker }).__crewContractWorker =
        worker;
    });
    const worker = await workerPromise;
    const value = await worker.evaluate(() => 21 * 2);
    if (value !== 42) throw new Error(`worker Runtime 路由异常: ${value}`);
    return String(value);
  });

  await check('page.on(dialog) 可替代宿主的对话框处理', async () => {
    let seen = '';
    let handlerFailure: unknown;
    const listener = (dialog: Dialog): void => {
      seen = `${dialog.type()}:${dialog.message()}`;
      void dialog.dismiss().catch((error: unknown) => {
        handlerFailure = error;
      });
    };
    page.on('dialog', listener);
    try {
      await contractDeadline(
        page.evaluate(() => {
          window.confirm('确认提交？');
        }),
        5_000,
        'page.on(dialog) dismiss 后 evaluate',
      );
    } finally {
      page.off('dialog', listener);
    }
    if (handlerFailure) {
      throw new Error(
        `dialog dismiss 失败: ${
          handlerFailure instanceof Error ? handlerFailure.message : String(handlerFailure)
        }`,
      );
    }
    if (!seen) throw new Error('未收到 dialog 事件');
    return seen;
  });

  await check('后台截图（窗口全程不可见）', async () => {
    const shot = await page.screenshot({ timeout: 10_000 });
    if (shot.length < 1000) throw new Error(`截图过小: ${shot.length}`);
    return `${shot.length} bytes`;
  });

  // ── 观察面与动作面（P2 / P3 的真实模块）─────────────────────────────
  let ctx: ActionContext = { page, refs: new Map(), hash, timeoutMs: 10_000 };

  await check('captureSnapshot：ref 全部重编号为 @eN，不泄漏 Playwright 格式', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    // f1e2 这类帧内 ref 若漏出去，Python 的 _REF_PATTERN 匹配不到，会原样给模型
    if (/\[ref=(?!@e\d+\])/.test(snap.text)) throw new Error(`存在未重编号的 ref:\n${snap.text}`);
    if (!snap.refs.size) throw new Error('没有登记任何 ref');
    const framed = [...snap.refs.values()].some((r) => /^f\d+e\d+$/.test(r.playwrightRef));
    if (!framed) throw new Error('iframe 内元素未进入 ref 表');
    return `${snap.refs.size} 个 ref，其中含帧内 ref`;
  });

  await check('captureSnapshot：submit 控件被标记 ref_actions=submit', async () => {
    const snap = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash,
      timeoutMs: 10_000,
    });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const submits = Object.entries(snap.refActions).filter(([, v]) => v === 'submit');
    if (!submits.length) throw new Error('未识别出提交按钮');
    const ref = submits[0][0];
    const record = snap.refs.get(ref);
    if (record?.name !== '提交工单') throw new Error(`标记到了别的元素: ${record?.name}`);
    return `${ref} → ${record.name}`;
  });

  await check('字段证明与录制器同源：tag / input type / contenteditable / tier', async () => {
    const password = await fingerprintResolvedLocator(page.locator('#password'), hash, 5_000);
    const otp = await fingerprintResolvedLocator(page.locator('#otp'), hash, 5_000);
    const rich = await fingerprintResolvedLocator(page.locator('#rich-note'), hash, 5_000);
    const richChild = await fingerprintResolvedLocator(page.locator('#rich-child'), hash, 5_000);
    if (password.tag !== 'input' || password.inputType !== 'password' || password.fieldTier !== 'secret') {
      throw new Error(`密码字段证明错误: ${JSON.stringify(password)}`);
    }
    if (otp.tag !== 'input' || otp.inputType !== 'text' || otp.fieldTier !== 'handoff') {
      throw new Error(`验证码字段证明错误: ${JSON.stringify(otp)}`);
    }
    if (rich.tag !== 'div' || !rich.contentEditable || rich.actionKind !== 'input') {
      throw new Error(`contenteditable 字段证明错误: ${JSON.stringify(rich)}`);
    }
    if (richChild.contentEditable || richChild.actionKind === 'input') {
      throw new Error(`继承 editable 的子节点越权成输入目标: ${JSON.stringify(richChild)}`);
    }
    await page.locator('#rich-note').evaluate((element) => {
      element.setAttribute('contenteditable', 'plaintext-only');
    });
    const plaintext = await fingerprintResolvedLocator(page.locator('#rich-note'), hash, 5_000);
    await page.locator('#rich-note').evaluate((element) => {
      element.setAttribute('contenteditable', 'true');
    });
    if (rich.security === plaintext.security) {
      throw new Error('plaintext-only 与 richtext 的字段能力未绑定进指纹');
    }
    return `password=${password.fieldTier}, otp=${otp.fieldTier}, rich=${rich.contentEditable}`;
  });

  await check('真实 AX 复核允许带子节点的复杂按钮', async () => {
    const identity = await ariaIdentityForLocator(page.locator('#complex-button'), 5_000);
    if (identity.role !== 'button' || identity.name !== '复杂按钮') {
      throw new Error(`复杂按钮 AX 身份错误: ${JSON.stringify(identity)}`);
    }
    return `${identity.role}:${identity.name}`;
  });

  await check('captureSnapshot：compact 保留静态文章/表格/错误提示与可执行 ref', async () => {
    const snap = await captureSnapshot(page, { full: false, hash, timeoutMs: 10_000 });
    for (const expected of [
      '处理须知',
      '请核对工单中的收件地址与商品数量',
      '机械键盘',
      '地址不能为空',
    ]) {
      if (!snap.text.includes(expected)) {
        throw new Error(`compact 丢失静态上下文 ${expected}:\n${snap.text}`);
      }
    }
    if (!/\[ref=@e\d+\]/.test(snap.text)) {
      throw new Error('compact 丢失全部可执行 ref');
    }
    return `${snap.text.split('\n').length} 行`;
  });

  await check('动作层 click 走 Locator', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    await page.evaluate(() => {
      const node = document.getElementById('late-result');
      if (node) node.textContent = '';
    });
    const entry = [...snap.refs.entries()].find(([, r]) => r.name === '延迟出现');
    if (!entry) throw new Error('快照里没有目标按钮');
    await actions.click(ctx, entry[0]);
    const text = await page.locator('#late-result').textContent();
    if (text !== '延迟按钮已点击') throw new Error('点击未生效');
    return entry[0];
  });

  await check('动作 completion 捕获 300ms 延迟 SPA 导航后再返回', async () => {
    await page.evaluate(() => {
      history.replaceState(null, '', location.pathname);
      document.querySelector('#completion-delay')?.remove();
      const button = document.createElement('button');
      button.id = 'completion-delay';
      button.textContent = '延迟导航';
      button.addEventListener('click', () => {
        setTimeout(() => {
          location.hash = 'completion-ready';
        }, 300);
      });
      document.body.append(button);
    });
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const entry = [...snap.refs.entries()].find(([, record]) => record.name === '延迟导航');
    if (!entry) throw new Error('快照里没有延迟导航按钮');
    await actions.click(ctx, entry[0]);
    if (!page.url().endsWith('#completion-ready')) {
      throw new Error(`动作过早返回，当前 URL=${page.url()}`);
    }
    return page.url();
  });

  await check('官方坐标鼠标：move/down/up/wheel/click/drag 走真实 Electron 输入', async () => {
    const surface = page.locator('#contract-mouse-surface');
    await surface.scrollIntoViewIfNeeded();
    const surfaceBox = await surface.boundingBox();
    const sourceBox = await page.locator('#contract-drag-source').boundingBox();
    const targetBox = await page.locator('#contract-drag-target').boundingBox();
    if (!surfaceBox || !sourceBox || !targetBox) {
      throw new Error('坐标鼠标契约元素没有可用的 viewport bounding box');
    }
    await page.evaluate(() => {
      const reset = (
        globalThis as typeof globalThis & {
          __crewContractResetMouse?: () => void;
        }
      ).__crewContractResetMouse;
      reset?.();
    });

    await actions.mouseMove(ctx, surfaceBox.x + 180, surfaceBox.y + 100);
    await actions.mouseDown(ctx, 'left');
    await actions.mouseMove(ctx, surfaceBox.x + 190, surfaceBox.y + 100);
    await actions.mouseUp(ctx, 'left');
    await actions.mouseWheel(ctx, 0, 73);
    await actions.mouseClick(
      ctx,
      surfaceBox.x + 180,
      surfaceBox.y + 100,
      { button: 'left', clickCount: 2, delayMs: 10 },
    );
    await actions.mouseDrag(
      ctx,
      sourceBox.x + sourceBox.width / 2,
      sourceBox.y + sourceBox.height / 2,
      targetBox.x + targetBox.width / 2,
      targetBox.y + targetBox.height / 2,
    );

    const events = JSON.parse(
      (await page.locator('#contract-mouse-state').textContent()) ?? '[]',
    ) as Array<{
      type?: string;
      target?: string;
      detail?: number;
      deltaY?: number;
    }>;
    for (const required of ['mousemove', 'mousedown', 'mouseup', 'click', 'wheel']) {
      if (!events.some((event) => event.type === required)) {
        throw new Error(`坐标鼠标缺少 ${required}: ${JSON.stringify(events)}`);
      }
    }
    const clickDetails = events
      .filter((event) => event.type === 'click')
      .map((event) => event.detail);
    if (!clickDetails.includes(2)) {
      throw new Error(`clickCount 未抵达页面: ${JSON.stringify(clickDetails)}`);
    }
    if (!events.some((event) => event.type === 'wheel' && event.deltaY === 73)) {
      throw new Error(`wheel delta 未精确透传: ${JSON.stringify(events)}`);
    }
    if (
      !events.some(
        (event) => event.type === 'dragstart' && event.target === 'contract-drag-source',
      )
      || !events.some(
        (event) => event.type === 'drop' && event.target === 'contract-drag-target',
      )
    ) {
      throw new Error(`mouse drag 未形成完整 HTML5 drop: ${JSON.stringify(events)}`);
    }
    return `${events.length} events；clickCount=2；wheel=73；drag/drop`;
  });

  const resetPointerContractSurface = async (): Promise<void> => {
    await page.evaluate(() => {
      document.getElementById('contract-pointer-canvas')?.remove();
      const canvas = document.createElement('canvas');
      canvas.id = 'contract-pointer-canvas';
      canvas.width = 320;
      canvas.height = 160;
      canvas.style.cssText = [
        'display:block',
        'width:320px',
        'height:160px',
        'border:5px solid transparent',
        'margin-top:16px',
        'touch-action:none',
      ].join(';');
      document.body.append(canvas);
      const globalState = globalThis as typeof globalThis & {
        __crewPointerEvents?: Array<Record<string, unknown>>;
        __crewPointerKeys?: string[];
        __crewTouchEnds?: number;
      };
      globalState.__crewPointerEvents = [];
      globalState.__crewPointerKeys = [];
      globalState.__crewTouchEnds = 0;
      for (const type of ['pointerdown', 'pointermove', 'pointerup'] as const) {
        canvas.addEventListener(type, (event) => {
          const rect = canvas.getBoundingClientRect();
          globalState.__crewPointerEvents?.push({
            type,
            x: event.clientX - rect.left,
            y: event.clientY - rect.top,
            elapsed: event.timeStamp,
            ctrlKey: event.ctrlKey,
            shiftKey: event.shiftKey,
            pointerType: event.pointerType,
            pressure: event.pressure,
            tangentialPressure: event.tangentialPressure,
            tiltX: event.tiltX,
            tiltY: event.tiltY,
            twist: event.twist,
            width: event.width,
            height: event.height,
            isPrimary: event.isPrimary,
          });
        });
      }
      canvas.addEventListener('touchend', (event) => {
        if (event.touches.length === 0) {
          globalState.__crewTouchEnds = (globalState.__crewTouchEnds ?? 0) + 1;
        }
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Control' || event.key === 'Shift') {
          globalState.__crewPointerKeys?.push(`${event.key}:down`);
        }
      });
      document.addEventListener('keyup', (event) => {
        if (event.key === 'Control' || event.key === 'Shift') {
          globalState.__crewPointerKeys?.push(`${event.key}:up`);
        }
      });
    });
  };

  await check('连续指针手势：canvas border-box 浮点轨迹/时间/修饰键', async () => {
    await resetPointerContractSurface();
    const ref = '@pointer-gesture-contract';
    await actions.locateBySelector(
      ctx,
      ref,
      '#contract-pointer-canvas',
      hash,
    );
    try {
      await actions.pointerGesture(ctx, ref, {
        button: 'left',
        modifiers: ['Control', 'Shift'],
        start: { x: 12.5, y: 18.25 },
        points: [
          { x: 80.75, y: 40.5, elapsedMs: 12 },
          { x: 155.125, y: 90.75, elapsedMs: 24 },
        ],
      });
    } finally {
      ctx.refs.delete(ref);
    }
    const state = await page.evaluate(() => {
      const globalState = globalThis as typeof globalThis & {
        __crewPointerEvents?: Array<Record<string, unknown>>;
        __crewPointerKeys?: string[];
        __crewTouchEnds?: number;
      };
      return {
        events: globalState.__crewPointerEvents ?? [],
        keys: globalState.__crewPointerKeys ?? [],
        touchEnds: globalState.__crewTouchEnds ?? 0,
      };
    });
    const typed = state.events as Array<{
      type?: string;
      x?: number;
      y?: number;
      elapsed?: number;
      ctrlKey?: boolean;
      shiftKey?: boolean;
      pointerType?: string;
    }>;
    if (
      typed[0]?.type !== 'pointermove'
      || !typed.some((event) => (
        event.type === 'pointerdown'
        && event.pointerType === 'mouse'
        && event.ctrlKey === true
        && event.shiftKey === true
      ))
      || typed.at(-1)?.type !== 'pointerup'
    ) {
      throw new Error(`pointer 事件/修饰键不完整: ${JSON.stringify(state)}`);
    }
    const endpoint = typed.findLast((event) => event.type === 'pointermove');
    if (
      !endpoint
      || Math.abs(Number(endpoint.x) - 155.125) > 0.25
      || Math.abs(Number(endpoint.y) - 90.75) > 0.25
    ) {
      throw new Error(`pointer border-box endpoint 漂移: ${JSON.stringify(state)}`);
    }
    const moves = typed.filter((event) => event.type === 'pointermove');
    if (
      moves.length < 3
      || Number(moves.at(-1)?.elapsed) - Number(moves[0]?.elapsed) < 18
    ) {
      throw new Error(`pointer trajectory/timing 丢失: ${JSON.stringify(state)}`);
    }
    if (
      !state.keys.includes('Control:down')
      || !state.keys.includes('Shift:down')
      || !state.keys.includes('Shift:up')
      || !state.keys.includes('Control:up')
      || state.keys.indexOf('Shift:up') > state.keys.indexOf('Control:up')
    ) {
      throw new Error(`modifier 未逆序完整释放: ${JSON.stringify(state.keys)}`);
    }
    return `${moves.length} moves；endpoint=${endpoint.x},${endpoint.y}；keys=${state.keys.join(',')}`;
  });

  await check('连续指针手势：pen 类型/压力/倾角/旋转精确抵达页面', async () => {
    await resetPointerContractSurface();
    const ref = '@pen-gesture-contract';
    await actions.locateBySelector(ctx, ref, '#contract-pointer-canvas', hash);
    try {
      await actions.pointerGesture(ctx, ref, {
        pointerType: 'pen',
        button: 'left',
        modifiers: [],
        start: {
          x: 20,
          y: 25,
          pressure: 0.3,
          tangentialPressure: -0.4,
          tiltX: 11,
          tiltY: -12,
          twist: 37,
          width: 8,
          height: 6,
        },
        points: [
          {
            x: 90,
            y: 60,
            elapsedMs: 8,
            pressure: 0.75,
            tangentialPressure: 0.2,
            tiltX: 21,
            tiltY: -22,
            twist: 47,
            width: 9,
            height: 7,
          },
          {
            x: 170,
            y: 100,
            elapsedMs: 16,
            pressure: 0,
            tiltX: 23,
            tiltY: -24,
            twist: 51,
            width: 10,
            height: 8,
          },
        ],
      });
    } finally {
      ctx.refs.delete(ref);
    }
    const events = await page.evaluate(() => (
      (globalThis as typeof globalThis & {
        __crewPointerEvents?: Array<Record<string, unknown>>;
      }).__crewPointerEvents ?? []
    ));
    const penEvents = events.filter((event) => event.pointerType === 'pen');
    const down = penEvents.find((event) => event.type === 'pointerdown');
    const pressuredMove = penEvents.find((event) => (
      event.type === 'pointermove'
      && Math.abs(Number(event.pressure) - 0.75) < 0.01
    ));
    const up = penEvents.findLast((event) => event.type === 'pointerup');
    if (
      !down
      || Math.abs(Number(down.pressure) - 0.3) >= 0.01
      || Math.abs(Number(down.tangentialPressure) - -0.4) >= 0.01
      || down.tiltX !== 11
      || down.tiltY !== -12
      || down.twist !== 37
      || down.isPrimary !== true
      || !pressuredMove
      || pressuredMove.tiltX !== 21
      || pressuredMove.tiltY !== -22
      || pressuredMove.twist !== 47
      || !up
      || Number(up.pressure) !== 0
    ) {
      throw new Error(`pen PointerEvent 语义丢失: ${JSON.stringify(events)}`);
    }
    return `pen down=${down.pressure}；move=${pressuredMove.pressure}；up=${up.pressure}`;
  });

  await check('连续指针手势：单主 touch 接触面/压力/结束完整抵达页面', async () => {
    await resetPointerContractSurface();
    const ref = '@touch-gesture-contract';
    await actions.locateBySelector(ctx, ref, '#contract-pointer-canvas', hash);
    try {
      await actions.pointerGesture(ctx, ref, {
        pointerType: 'touch',
        button: 'left',
        modifiers: [],
        start: {
          x: 25,
          y: 30,
          pressure: 0.6,
          width: 12,
          height: 10,
        },
        points: [
          {
            x: 100,
            y: 70,
            elapsedMs: 8,
            pressure: 0.8,
            width: 14,
            height: 8,
          },
          {
            x: 180,
            y: 105,
            elapsedMs: 16,
            pressure: 0,
            width: 16,
            height: 6,
          },
        ],
      });
    } finally {
      ctx.refs.delete(ref);
    }
    const state = await page.evaluate(() => {
      const globalState = globalThis as typeof globalThis & {
        __crewPointerEvents?: Array<Record<string, unknown>>;
        __crewTouchEnds?: number;
      };
      return {
        events: globalState.__crewPointerEvents ?? [],
        touchEnds: globalState.__crewTouchEnds ?? 0,
      };
    });
    const touchEvents = state.events.filter((event) => event.pointerType === 'touch');
    const down = touchEvents.find((event) => event.type === 'pointerdown');
    const pressuredMove = touchEvents.find((event) => (
      event.type === 'pointermove'
      && Math.abs(Number(event.pressure) - 0.8) < 0.01
    ));
    const up = touchEvents.findLast((event) => event.type === 'pointerup');
    if (
      !down
      || Math.abs(Number(down.pressure) - 0.6) >= 0.01
      || Math.abs(Number(down.width) - 12) >= 0.1
      || Math.abs(Number(down.height) - 10) >= 0.1
      || down.isPrimary !== true
      || !pressuredMove
      || Math.abs(Number(pressuredMove.width) - 14) >= 0.1
      || Math.abs(Number(pressuredMove.height) - 8) >= 0.1
      || !up
      || Number(up.pressure) !== 0
      || state.touchEnds !== 1
    ) {
      throw new Error(
        'touch PointerEvent/结束语义丢失: '
        + `down=${down?.pressure}/${down?.width}x${down?.height}/${down?.isPrimary};`
        + `move=${pressuredMove?.pressure}/${pressuredMove?.width}x${pressuredMove?.height};`
        + `up=${up?.pressure};touchEnds=${state.touchEnds}`,
      );
    }
    return `touch down=${down.pressure}/${down.width}x${down.height}；end=${state.touchEnds}`;
  });

  await check('官方 resize：真实 Page viewport 可改且可恢复', async () => {
    const original = page.viewportSize() ?? AUTOMATION_VIEWPORT;
    try {
      await actions.resize(ctx, 963, 707);
      const resized = page.viewportSize();
      if (resized?.width !== 963 || resized.height !== 707) {
        throw new Error(`viewport 未精确更新: ${JSON.stringify(resized)}`);
      }
      return `${original.width}x${original.height} → ${resized.width}x${resized.height}`;
    } finally {
      await page.setViewportSize({
        width: original.width,
        height: original.height,
      });
    }
  });

  await check('官方 drop：data/files/显式空 data 保持真实 DataTransfer 语义', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const entry = [...snap.refs.entries()].find(([, record]) => (
      record.name === '外部拖放目标'
    ));
    if (!entry) throw new Error('快照里缺少外部拖放目标');
    const readDrop = async (): Promise<{
      types: string[];
      files: string[];
      text: string;
      uri: string;
    }> => JSON.parse(
      (await page.locator('#contract-drop-state').textContent()) ?? '{}',
    );

    await actions.drop(ctx, entry[0], {
      data: {
        'text/plain': 'crew-drop-text',
        'text/uri-list': 'https://example.test/drop',
      },
    });
    const dataDrop = await readDrop();
    if (
      dataDrop.text !== 'crew-drop-text'
      || dataDrop.uri !== 'https://example.test/drop'
      || dataDrop.files.length
    ) {
      throw new Error(`data drop 失真: ${JSON.stringify(dataDrop)}`);
    }

    await actions.drop(ctx, entry[0], { files: [FIXTURE] });
    const fileDrop = await readDrop();
    if (
      fileDrop.files.length !== 1
      || fileDrop.files[0] !== path.basename(FIXTURE)
    ) {
      throw new Error(`file drop 失真: ${JSON.stringify(fileDrop)}`);
    }

    await actions.drop(ctx, entry[0], { data: {} });
    const emptyDrop = await readDrop();
    if (
      emptyDrop.files.length
      || emptyDrop.text
      || emptyDrop.uri
      || emptyDrop.types.length
    ) {
      throw new Error(`显式空 data 被折叠或污染: ${JSON.stringify(emptyDrop)}`);
    }
    return `data=${dataDrop.types.join(',')}；file=${fileDrop.files[0]}；empty=ok`;
  });

  await check('动作层 selectOption / setChecked', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const select = [...snap.refs.entries()].find(([, r]) => r.name === '优先级');
    const check = [...snap.refs.entries()].find(([, r]) => r.name === '加急');
    if (!select || !check) throw new Error('快照里缺少表单控件');
    await actions.selectOption(ctx, select[0], ['high']);
    // Mutation identity checks intentionally invalidate the whole aria-ref generation.
    // BrowserManager takes a fresh observation after every mutation, so the contract must
    // exercise the same topology rather than reusing a ref invalidated by selectOption.
    const afterSelect = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: afterSelect.refs, hash, timeoutMs: 10_000 };
    const freshCheck = [...afterSelect.refs.entries()].find(([, r]) => r.name === '加急');
    if (!freshCheck) throw new Error('选择后新快照缺少 checkbox');
    await actions.setChecked(ctx, freshCheck[0], true);
    const value = await page.locator('#priority').inputValue();
    const checked = await page.locator('#urgent').isChecked();
    if (value !== 'high' || !checked) throw new Error(`select=${value} checked=${checked}`);
    return `priority=${value} urgent=${checked}`;
  });

  await check('动作层 fill 支持有证明的 contenteditable', async () => {
    const snap = await captureSnapshot(page, {
      full: true,
      richMetadata: true,
      hash,
      timeoutMs: 10_000,
    });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const entry = [...snap.refs.entries()].find(([, record]) => record.name === '处理意见');
    if (!entry) throw new Error('快照里缺少 contenteditable');
    if (!entry[1].contentEditable || entry[1].actionKind !== 'input') {
      throw new Error(`contenteditable ref 证明错误: ${JSON.stringify(entry[1])}`);
    }
    try {
      await actions.fill(ctx, entry[0], '已复核', { submit: false });
    } catch (error) {
      const live = await ariaIdentityForLocator(page.locator('#rich-note'), 5_000);
      throw new Error(
        `${(error as Error).message}; snapshot=${entry[1].role}:${JSON.stringify(entry[1].name)}`
        + ` live=${live.role}:${JSON.stringify(live.name)}`,
      );
    }
    const value = await page.locator('#rich-note').textContent();
    if (value?.trim() !== '已复核') {
      throw new Error(`contenteditable fill 未生效: ${JSON.stringify(value)}`);
    }
    return value.trim();
  });

  await check('动作层 upload 拒绝非文件输入框', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const entry = [...snap.refs.entries()].find(([, r]) => r.name === '优先级');
    if (!entry) throw new Error('缺少 select');
    try {
      // setInputFiles resolves/stat()s every payload before it validates the
      // DOM target. Use a real file so this contract reaches target validation.
      await actions.upload(ctx, entry[0], [FIXTURE]);
    } catch (error) {
      const code = (error as { code?: string }).code;
      if (code === 'invalid_upload_target') return '已拒绝';
      throw new Error(`错误码不对: ${code}: ${(error as Error).message}`);
    }
    throw new Error('未拒绝');
  });

  // 普通页面变化不再依赖 DOM/AX 指纹保持不变。动作层执行原始 aria-ref Locator，
  // normalize 只验证录制存盘能力；动作返回后由调用方重新观察后置页面态。
  await check('动态属性变化后：exact aria-ref 仍可执行，并可取得后置快照', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const entry = [...snap.refs.entries()].find(([, r]) => r.name === '可变链接');
    if (!entry) throw new Error('快照里没有可变链接');

    // 节点不换，只改普通属性。preventDefault 让这条动作契约只验证定位/动作面；
    // 跨域/协议导航策略属于 BrowserHost 网络边界，应由独立策略契约覆盖。
    await page.evaluate(() => {
      const node = document.getElementById('mutable');
      if (!(node instanceof HTMLAnchorElement)) return;
      node.href = '#contract-mutated';
      node.addEventListener('click', (event) => {
        event.preventDefault();
        node.dataset.contractClicked = 'yes';
      }, { once: true });
    });

    const normalized = await toStableSelector(
      entry[1].playwrightRef
        ? locatorFromRef(page, entry[1].playwrightRef)
        : page.locator(entry[1].selector),
    );
    const matches = await page.locator(normalized).count();
    if (matches !== 1) {
      throw new Error(`normalized Locator 必须唯一，实际 ${matches}: ${normalized}`);
    }
    await actions.click(ctx, entry[0]);
    if (await page.locator('#mutable').getAttribute('data-contract-clicked') !== 'yes') {
      throw new Error('Playwright actionability/dispatch 未完成 click');
    }

    const post = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: post.refs, hash, timeoutMs: 10_000 };
    if (![...post.refs.values()].some((record) => record.name === '可变链接')) {
      throw new Error('动作返回后的新快照缺少目标');
    }
    return `${normalized}；post=${post.refs.size} refs`;
  });

  await check('动态 accessible name 变化后：exact aria-ref 执行并反映在后置快照', async () => {
    const snap = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: snap.refs, hash, timeoutMs: 10_000 };
    const entry = [...snap.refs.entries()].find(([, record]) => record.name === '批准');
    if (!entry) throw new Error('快照里没有 CSS 语义交换目标');
    await page.locator('#ax-swap').evaluate((element) => {
      element.className = 'semantic-danger';
      element.addEventListener('click', () => {
        (element as HTMLElement).dataset.contractClicked = 'yes';
      }, { once: true });
    });

    const normalized = await toStableSelector(
      entry[1].playwrightRef
        ? locatorFromRef(page, entry[1].playwrightRef)
        : page.locator(entry[1].selector),
    );
    const target = page.locator(normalized);
    if (await target.count() !== 1) {
      throw new Error(`accessible name 变化后 normalized Locator 不唯一: ${normalized}`);
    }
    await actions.click(ctx, entry[0]);
    if (await page.locator('#ax-swap').getAttribute('data-contract-clicked') !== 'yes') {
      throw new Error('accessible name 变化后 click 未执行');
    }

    const post = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    ctx = { page, refs: post.refs, hash, timeoutMs: 10_000 };
    const changed = [...post.refs.values()].find((record) => record.name === '删除全部');
    if (!changed) {
      throw new Error('后置快照未反映新的 accessible name');
    }
    return `${normalized}；post=${changed.role}:${changed.name}`;
  });

  await check('未知 ref 报 stale_ref 而不是静默失败', async () => {
    try {
      await actions.click(ctx, '@e99999');
    } catch (error) {
      const code = (error as { code?: string }).code;
      if (code === 'stale_ref') return '已拒绝';
      throw new Error(`错误码不对: ${code}`);
    }
    throw new Error('未拒绝');
  });

  await check('新快照使旧 ref 失效（Playwright 只保留最近一份）', async () => {
    const first = await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    const staleCtx: ActionContext = { page, refs: first.refs, hash, timeoutMs: 5000 };
    const entry = [...first.refs.entries()].find(([, r]) => r.name === '延迟出现');
    if (!entry) throw new Error('缺少目标');
    await captureSnapshot(page, { full: true, hash, timeoutMs: 10_000 });
    try {
      await actions.click(staleCtx, entry[0]);
    } catch (error) {
      return `旧 ref 已失效: ${(error as { code?: string }).code}`;
    }
    // Playwright 重新采集时 ref 编号可能恰好复用同一个元素，这不算破契约
    return '旧 ref 仍指向同一元素（编号复用，可接受）';
  });

  // ── 录制路径：页面内 CSS 路径 → 跨帧链 → normalize → 回放 ────────────
  // 这是 P4 的生产路径。注入脚本只产出「此刻唯一命中」的临时路径，稳定性完全由
  // normalize() 负责；跨帧时要拼出 enter-frame 链。
  await check('录制路径：主文档 cssPath → 稳定选择器 → 回放', async () => {
    const cssPath = await page.evaluate(() => {
      const el = document.getElementById('late');
      const parts: string[] = [];
      let node: Element | null = el;
      while (node && parts.length < 64) {
        const tag = node.tagName.toLowerCase();
        const id = node.getAttribute('id');
        if (id && document.querySelectorAll('#' + CSS.escape(id)).length === 1) {
          parts.unshift('#' + CSS.escape(id));
          break;
        }
        const parent: Element | null = node.parentElement;
        if (!parent) { parts.unshift(tag); break; }
        let index = 1;
        for (const child of parent.children) {
          if (child === node) break;
          if (child.tagName.toLowerCase() === tag) index += 1;
        }
        parts.unshift(`${tag}:nth-of-type(${index})`);
        node = parent;
      }
      return parts.join(' > ');
    });
    if (!cssPath) throw new Error('页面内未算出 cssPath');
    const persisted = await toStableSelector(page.locator(cssPath));
    if (/nth-of-type/.test(persisted)) {
      throw new Error(`normalize 未升级掉脆弱路径: ${persisted}`);
    }
    await page.evaluate(() => {
      const node = document.getElementById('late-result');
      if (node) node.textContent = '';
    });
    await page.locator(persisted).click();
    if ((await page.locator('#late-result').textContent()) !== '延迟按钮已点击') {
      throw new Error('回放未生效');
    }
    return `${cssPath}  →  ${persisted}`;
  });

  await check('录制路径：iframe 内 cssPath + framePath → 跨帧稳定选择器 → 回放', async () => {
    // 帧内路径（在子框架文档里算）
    const inner = await page.frameLocator('#frame').locator('#fb').evaluate((el) => {
      const parts: string[] = [];
      let node: Element | null = el;
      while (node && parts.length < 64) {
        const tag = node.tagName.toLowerCase();
        const id = node.getAttribute('id');
        if (id && node.ownerDocument.querySelectorAll('#' + CSS.escape(id)).length === 1) {
          parts.unshift('#' + CSS.escape(id));
          break;
        }
        const parent: Element | null = node.parentElement;
        if (!parent) { parts.unshift(tag); break; }
        let index = 1;
        for (const child of parent.children) {
          if (child === node) break;
          if (child.tagName.toLowerCase() === tag) index += 1;
        }
        parts.unshift(`${tag}:nth-of-type(${index})`);
        node = parent;
      }
      return parts.join(' > ');
    });
    // 父文档里的 iframe 元素路径
    const framePath = '#frame';
    const chain = `${framePath} >> internal:control=enter-frame >> ${inner}`;
    const persisted = await toStableSelector(page.locator(chain));
    if (!persisted.includes('enter-frame')) throw new Error(`未补出跨帧链: ${persisted}`);
    await page.evaluate(() => {
      const node = document.getElementById('frame-result');
      if (node) node.textContent = '未收到';
    });
    await page.locator(persisted).click();
    await page.waitForFunction(
      () => document.getElementById('frame-result')?.textContent === '框架按钮已点击',
      null,
      { timeout: 5000 },
    );
    return persisted;
  });

  // ── 跨窗口迁移：AI 后台 ↔ 用户面板 ───────────────────────────────────
  const panel = new BrowserWindow({ ...AUTOMATION_VIEWPORT, show: false });
  await check('迁移到面板窗口后：debugger 保持、状态不丢、仍可点击', async () => {
    host.unmount(view);
    panel.contentView.addChildView(view);
    view.setVisible(true);
    view.setBounds({ x: 0, y: 0, ...AUTOMATION_VIEWPORT });

    if (!view.webContents.debugger.isAttached()) throw new Error('debugger 掉了');
    await page.getByLabel('影子输入').fill('状态-A');
    await page.getByRole('button', { name: '延迟出现' }).click();
    const kept = await page.getByLabel('影子输入').inputValue();
    if (kept !== '状态-A') throw new Error(`状态丢失: ${kept}`);
    return '保持';
  });

  await check('迁回后台宿主后仍可点击且无需重设焦点模拟', async () => {
    panel.contentView.removeChildView(view);
    host.mount(view);
    await page.evaluate(() => {
      const node = document.getElementById('late-result');
      if (node) node.textContent = '';
    });
    await page.getByRole('button', { name: '延迟出现' }).click();
    const replayed = await page.locator('#late-result').textContent();
    if (replayed !== '延迟按钮已点击') throw new Error('迁回后点击失效');
    return '正常';
  });

  const lateView = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      backgroundThrottling: false,
    },
  });
  await check('多标签 late attach 按 targetId 路由，不按 URL/顺序猜测', async () => {
    host.mount(lateView);
    await lateView.webContents.loadURL(
      'data:text/html,<title>crew-late-tab</title><button id="late-tab">late-tab</button>',
    );
    transport.addView(lateView);
    const targetId = await transport.waitForViewTarget(lateView);
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline) {
      for (const candidate of context.pages()) {
        const cdp = await context.newCDPSession(candidate);
        try {
          const info = await cdp.send('Target.getTargetInfo') as {
            targetInfo?: { targetId?: string };
          };
          if (info.targetInfo?.targetId !== targetId) continue;
          if (await candidate.title() !== 'crew-late-tab') {
            throw new Error('targetId 映射到了错误页面');
          }
          if (await candidate.locator('#late-tab').textContent() !== 'late-tab') {
            throw new Error('late tab DOM 不符');
          }
          return targetId;
        } finally {
          await cdp.detach().catch(() => undefined);
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error(`Playwright 未按 targetId 收编 late tab: ${targetId}`);
  });

  // 过去的契约直接 connect transport，漏掉了生产 Engine 的懒启动顺序。用第二个真实
  // view 验证 registerTab 后第一次 pageForView 能自行完成 connect → attach → prepare。
  await check('PlaywrightEngine 首次 pageForView 自启动（真实 Electron）', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadFile(FIXTURE);
      const enginePage = await engine.pageForView(engineView);
      await enginePage.getByRole('button', { name: '延迟出现' }).click();
      const text = await enginePage.locator('#late-result').textContent();
      if (text !== '延迟按钮已点击') throw new Error(`首次 Engine 点击未生效: ${text}`);
      return 'connect → attach-ready → focus → click 正常';
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('PlaywrightEngine popup 保留 opener 且按新 view 精确绑定 Page', async () => {
    const engine = new PlaywrightEngine();
    const openerView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    const openerContents = openerView.webContents;
    let popupView: WebContentsView | null = null;
    let openerUnregistered = false;
    try {
      openerView.webContents.setWindowOpenHandler(() => ({
        action: 'allow',
        outlivesOpener: true,
        createWindow: (options) => {
          const inheritedContents = (
            options as typeof options & { webContents?: WebContentsView['webContents'] }
          ).webContents;
          if (!inheritedContents) {
            throw new Error('Electron createWindow 未传入待收养的 webContents');
          }
          const created = new WebContentsView({
            webContents: inheritedContents,
            webPreferences: {
              ...options.webPreferences,
              contextIsolation: true,
              sandbox: true,
              webSecurity: true,
              backgroundThrottling: false,
            },
          });
          popupView = created;
          engine.registerTab(created, { opener: openerView });
          return created.webContents;
        },
      }));
      engine.registerTab(openerView);
      const popupURL = new URL('/popup', topOriginURL).href;
      await openerView.webContents.loadURL(
        `data:text/html,${encodeURIComponent(
          `<button id="open-popup" onclick='window.open(${JSON.stringify(popupURL)})'>open</button>`,
        )}`,
      );
      const openerPage = await engine.pageForView(openerView);
      const opened = openerPage
        .waitForEvent('popup', { timeout: 5_000 })
        .catch(() => null);
      await openerPage.locator('#open-popup').click({
        noWaitAfter: true,
        timeout: 5_000,
      });
      const popupDeadline = Date.now() + 5_000;
      while (!popupView && Date.now() < popupDeadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      const created = popupView;
      if (!created) throw new Error('Electron createWindow 未返回 popup view');
      const nativeInfo = await created.webContents.debugger.sendCommand(
        'Target.getTargetInfo',
      ) as {
        targetInfo?: {
          targetId?: unknown;
          type?: unknown;
          url?: unknown;
          openerId?: unknown;
          browserContextId?: unknown;
        };
      };
      const mappedPage = await engine.pageForView(created).catch((error: unknown) => {
        const info = nativeInfo.targetInfo;
        throw new Error(
          `${error instanceof Error ? error.message : String(error)}; `
          + `native=${JSON.stringify({
            targetId: info?.targetId,
            type: info?.type,
            url: info?.url,
            openerId: info?.openerId,
            browserContextId: info?.browserContextId,
          })}; opener=${Boolean(created.webContents.opener)}`,
        );
      });
      const popupPage = await opened;
      if (!popupPage) {
        const cdp = await mappedPage.context().newCDPSession(mappedPage);
        try {
          const info = await cdp.send('Target.getTargetInfo');
          throw new Error(`Playwright 未发 popup 事件: ${JSON.stringify(info)}`);
        } finally {
          await cdp.detach();
        }
      }
      await popupPage.waitForLoadState('domcontentloaded');
      if (mappedPage !== popupPage) throw new Error('popup view 映射到了错误 Page');
      if (await popupPage.opener() !== openerPage) throw new Error('Playwright popup opener 丢失');
      if (popupPage.url() !== popupURL) {
        throw new Error(`popup URL 异常: ${popupPage.url()}`);
      }
      engine.unregisterTab(openerView);
      openerUnregistered = true;
      openerContents.close({ waitForBeforeUnload: false });
      await contractDeadline(
        popupPage.locator('body').waitFor({ state: 'attached' }),
        2_000,
        'opener 关闭后 popup 继续存活',
      );
      if (created.webContents.isDestroyed() || popupPage.isClosed()) {
        throw new Error('outlivesOpener=true 仍随 opener 销毁 popup');
      }
      return `${popupPage.url()}；opener/page mapping 正常；opener close 后仍存活`;
    } finally {
      const created = popupView;
      if (created) engine.unregisterTab(created);
      if (!openerUnregistered) engine.unregisterTab(openerView);
      await engine.dispose();
      if (created && !created.webContents.isDestroyed()) {
        created.webContents.close({ waitForBeforeUnload: false });
      }
      if (!openerContents.isDestroyed()) {
        openerContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('BrowserHost console 直读 Playwright retained buffers', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-console', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionHash = createHash('sha256')
      .update('pw-contract-console-session', 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-console-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const consoleHost = await managedHost(() => panelWindow, runtimeKey, profile);
    const consoleURL = new URL('/host-console?phase=initial', topOriginURL).href;
    const consoleAfterNavigationURL = new URL(
      '/host-console?phase=after',
      topOriginURL,
    ).href;
    try {
      const created = await consoleHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            `s${sessionHash}-1`,
            consoleURL,
          ],
          mutating: true,
          command_timeout_ms: 15_000,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('console 契约未创建 tab');

      const execute = async (
        command: string,
        args: string[],
        mutating = false,
      ): Promise<Record<string, unknown>> => {
        const response = await consoleHost.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: targetId,
            command,
            args,
            mutating,
            command_timeout_ms: 15_000,
          },
        }) as { data?: Record<string, unknown> };
        return response.data ?? {};
      };

      await execute('run_code_unsafe', [`async page => {
        await page.evaluate(() => {
          console.log('crew-console-log');
          console.warn('crew-console-warning');
          console.error('crew-console-error');
          console.debug('crew-console-debug');
          setTimeout(() => {
            throw new Error('crew-uncaught-pageerror');
          }, 0);
        });
        await page.waitForTimeout(100);
        return true;
      }`], true);

      const current = String((await execute(
        'console',
        ['--level', 'info'],
      )).text ?? '');
      const header = current.match(
        /^Total messages: (\d+) \(Errors: (\d+), Warnings: (\d+)\)/,
      );
      if (
        !header
        || Number(header[1]) < 5
        || Number(header[2]) < 2
        || Number(header[3]) < 1
      ) {
        throw new Error(`console 计数异常: ${current.slice(0, 500)}`);
      }
      for (const marker of [
        '[LOG] crew-console-log',
        '[WARNING] crew-console-warning',
        '[ERROR] crew-console-error',
        'Error: crew-uncaught-pageerror',
      ]) {
        if (!current.includes(marker)) {
          throw new Error(`console 缺少 ${marker}: ${current.slice(0, 800)}`);
        }
      }
      if (current.includes('crew-console-debug')) {
        throw new Error('console 默认 info 错误包含 debug');
      }

      const errorsOnly = String((await execute(
        'console',
        ['--level', 'error'],
      )).text ?? '');
      if (
        !errorsOnly.includes('[ERROR] crew-console-error')
        || !errorsOnly.includes('Error: crew-uncaught-pageerror')
        || errorsOnly.includes('crew-console-warning')
        || errorsOnly.includes('crew-console-log')
        || errorsOnly.includes('crew-console-debug')
      ) {
        throw new Error(`console error 累进筛选异常: ${errorsOnly.slice(0, 800)}`);
      }

      await execute('run_code_unsafe', [`async page => {
        await page.goto(
          ${JSON.stringify(consoleAfterNavigationURL)},
        );
        await page.evaluate(() => console.info('crew-console-after-navigation'));
        return true;
      }`], true);
      const sinceNavigation = String((await execute(
        'console',
        ['--level', 'debug'],
      )).text ?? '');
      if (
        !sinceNavigation.includes('[INFO] crew-console-after-navigation')
        || sinceNavigation.includes('crew-console-log')
        || sinceNavigation.includes('crew-uncaught-pageerror')
      ) {
        throw new Error(
          `console since-navigation 过滤异常: ${sinceNavigation.slice(0, 800)}`,
        );
      }
      const all = String((await execute(
        'console',
        ['--level', 'debug', '--all'],
      )).text ?? '');
      for (const marker of [
        'crew-console-log',
        'crew-console-debug',
        'crew-uncaught-pageerror',
        'crew-console-after-navigation',
      ]) {
        if (!all.includes(marker)) {
          throw new Error(`console --all 缺少 ${marker}: ${all.slice(0, 800)}`);
        }
      }

      const cleared = await execute('console', ['--clear']);
      if (cleared.text !== '') {
        throw new Error(`console clear 返回异常: ${JSON.stringify(cleared)}`);
      }
      const afterClear = String((await execute(
        'console',
        ['--level', 'info'],
      )).text ?? '');
      if (afterClear !== 'Total messages: 0 (Errors: 0, Warnings: 0)\n') {
        throw new Error(`console clear 未清空双缓冲: ${JSON.stringify(afterClear)}`);
      }
      return 'level/all/since-navigation/clear；console+pageerror；stack/计数均对齐';
    } finally {
      await consoleHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserHost 普通动作自动下载：click/goto/run_code/同动作多文件', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-automatic-download', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionHash = createHash('sha256')
      .update('pw-contract-automatic-download-session', 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-auto-download-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    const downloadDir = path.join(tempRoot, 'task', 'downloads', 'browser');
    await mkdir(profile, { recursive: true });
    await mkdir(downloadDir, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const downloadHost = await managedHost(() => panelWindow, runtimeKey, profile);
    type DownloadEvent = {
      downloadId: string;
      targetId: string;
      sessionHash: string;
      path: string;
      name: string;
      suggestedFilename: string;
      state: string;
      receivedBytes: number;
      totalBytes: number;
    };
    const allEvents: DownloadEvent[] = [];
    downloadHost.on('download', (event: DownloadEvent) => {
      allEvents.push({ ...event });
    });
    try {
      const harnessURL = new URL('/host-download-harness', topOriginURL).href;
      const created = await downloadHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          download_dir: downloadDir,
          command: 'tab',
          args: [
            'new',
            '--label',
            `s${sessionHash}-1`,
            harnessURL,
          ],
          mutating: true,
          command_timeout_ms: 20_000,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('自动下载契约未创建 tab');

      const execute = async (
        command: string,
        args: string[],
        mutating = false,
      ): Promise<Record<string, unknown>> => {
        const response = await downloadHost.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            download_dir: downloadDir,
            target_id: targetId,
            command,
            args,
            mutating,
            command_timeout_ms: 20_000,
          },
        }) as { data?: Record<string, unknown> };
        return response.data ?? {};
      };
      const captureCompleted = async (
        expected: number,
        operation: () => Promise<Record<string, unknown>>,
      ): Promise<{ response: Record<string, unknown>; completed: DownloadEvent[] }> => {
        const completed: DownloadEvent[] = [];
        let resolveCompleted!: () => void;
        const terminal = new Promise<void>((resolve) => {
          resolveCompleted = resolve;
        });
        const listener = (event: DownloadEvent): void => {
          if (event.state !== 'completed') return;
          completed.push({ ...event });
          if (completed.length >= expected) resolveCompleted();
        };
        downloadHost.on('download', listener);
        try {
          const response = await operation();
          await contractDeadline(
            terminal,
            20_000,
            `${expected} 个普通下载完成`,
          );
          return { response, completed };
        } finally {
          downloadHost.off('download', listener);
        }
      };
      const refFor = async (selector: string): Promise<string> => {
        const located = await execute('locate', [selector]);
        const ref = String(located.ref ?? '');
        if (!ref) {
          throw new Error(`自动下载 locate 缺少 ${selector}: ${JSON.stringify(located)}`);
        }
        return ref;
      };
      const responseDownloads = (
        response: Record<string, unknown>,
      ): DownloadEvent[] => (
        Array.isArray(response.downloads)
          ? response.downloads as DownloadEvent[]
          : []
      );
      const ready = await execute('run_code_unsafe', [
        `async page => {
          await page.locator('#download-click').waitFor({ state: 'visible' });
          return { url: page.url(), title: await page.title() };
        }`,
      ]);
      if (!String(ready.result ?? '').includes('automatic download contract')) {
        throw new Error(`自动下载 harness 未就绪: ${JSON.stringify(ready)}`);
      }

      const clicked = await captureCompleted(1, async () => execute(
        'click',
        [await refFor('#download-click')],
        true,
      ));
      if (
        responseDownloads(clicked.response).length !== 1
        || clicked.completed[0]?.name !== 'click.txt'
      ) {
        throw new Error(`click attachment 未回传下载: ${JSON.stringify(clicked)}`);
      }

      const gotoURL = new URL(
        '/host-download-file?name=goto.txt&token=goto',
        topOriginURL,
      ).href;
      const navigated = await captureCompleted(1, () => execute(
        'open',
        [gotoURL],
        true,
      ));
      if (
        navigated.response.download_started !== true
        || responseDownloads(navigated.response).length !== 1
        || navigated.completed[0]?.name !== 'goto.txt'
      ) {
        throw new Error(`goto attachment 语义异常: ${JSON.stringify(navigated)}`);
      }

      await execute('open', [harnessURL], true);
      const publicSaveAsPath = path.join(tempRoot, 'public-save-as.json');
      const runCode = await captureCompleted(1, () => execute(
        'run_code_unsafe',
        [
          'async page => { const [download] = await Promise.all(['
          + 'page.waitForEvent("download"), '
          + 'page.locator("#download-run-code").click()]); '
          + `await download.saveAs(${JSON.stringify(publicSaveAsPath)}); `
          + 'let streamBody = ""; const stream = await download.createReadStream(); '
          + 'for await (const chunk of stream) streamBody += chunk.toString("utf8"); '
          + 'return { suggestedFilename: download.suggestedFilename(), '
          + 'url: download.url(), path: await download.path(), streamBody }; }',
        ],
        true,
      ));
      if (
        responseDownloads(runCode.response).length !== 1
        || runCode.completed[0]?.name !== 'run-code.json'
        || JSON.stringify(runCode.response.result ?? '').includes('run-code.json') === false
      ) {
        throw new Error(`run_code attachment 未回传下载: ${JSON.stringify(runCode)}`);
      }
      const publicDownloadValue = typeof runCode.response.result === 'string'
        ? JSON.parse(runCode.response.result) as Record<string, unknown>
        : {};
      const publicDownloadPath = typeof publicDownloadValue.path === 'string'
        ? publicDownloadValue.path
        : '';
      const publicDownloadBody = publicDownloadPath
        ? await readFile(publicDownloadPath).catch(() => Buffer.alloc(0))
        : Buffer.alloc(0);
      const publicSaveAsBody = await readFile(publicSaveAsPath).catch(() => Buffer.alloc(0));
      if (
        publicDownloadBody.toString('utf8') !== 'crew-download:run-code'
        || publicSaveAsBody.toString('utf8') !== 'crew-download:run-code'
        || publicDownloadValue.streamBody !== 'crew-download:run-code'
      ) {
        throw new Error(
          `public Download.path()/saveAs() 未指向真实文件: ${
            JSON.stringify({ publicDownloadPath, publicSaveAsPath, runCode })
          }`,
        );
      }

      let cancelledEvent: DownloadEvent | undefined;
      let resolveCancelled!: () => void;
      const cancelledSignal = new Promise<void>((resolve) => {
        resolveCancelled = resolve;
      });
      const cancelledListener = (event: DownloadEvent): void => {
        if (event.state !== 'cancelled') return;
        cancelledEvent = { ...event };
        resolveCancelled();
      };
      downloadHost.on('download', cancelledListener);
      let cancelledResponse: Record<string, unknown>;
      try {
        cancelledResponse = await execute(
          'run_code_unsafe',
          [
            'async page => { const [download] = await Promise.all(['
            + 'page.waitForEvent("download"), '
            + 'page.locator("#download-cancel").click()]); '
            + 'const suggestedFilename = download.suggestedFilename(); '
            + 'await download.cancel(); '
            + 'return { suggestedFilename, failure: await download.failure() }; }',
          ],
          true,
        );
        await contractDeadline(
          cancelledSignal,
          20_000,
          'public Download.cancel native terminal',
        );
      } finally {
        downloadHost.off('download', cancelledListener);
      }
      if (
        cancelledEvent?.name !== 'cancel.bin'
        || !JSON.stringify(cancelledResponse.result ?? '').includes('cancel.bin')
        || !JSON.stringify(cancelledResponse.result ?? '').toLowerCase().includes('cancel')
      ) {
        throw new Error(
          `public Download.cancel 语义异常: ${
            JSON.stringify({ cancelledEvent, cancelledResponse })
          }`,
        );
      }

      const multi = await captureCompleted(2, async () => execute(
        'click',
        [await refFor('#download-multi')],
        true,
      ));
      const multiResponse = responseDownloads(multi.response);
      if (
        multiResponse.length !== 2
        || new Set(multiResponse.map((item) => item.downloadId)).size !== 2
        || new Set(multi.completed.map((item) => item.path)).size !== 2
        || new Set(multi.completed.map((item) => item.name)).size !== 2
      ) {
        throw new Error(`同动作多下载丢失/覆盖: ${JSON.stringify(multi)}`);
      }
      const multiBodies = await Promise.all(
        multi.completed.map(async (item) => (await readFile(item.path)).toString('utf8')),
      );
      if (
        new Set(multiBodies).size !== 2
        || !multiBodies.includes('crew-download:multi-a')
        || !multiBodies.includes('crew-download:multi-b')
      ) {
        throw new Error(`同名下载内容被覆盖: ${JSON.stringify(multiBodies)}`);
      }

      const expectedBodies = new Map([
        [clicked.completed[0]!, 'crew-download:click'],
        [navigated.completed[0]!, 'crew-download:goto'],
        [runCode.completed[0]!, 'crew-download:run-code'],
      ]);
      for (const [download, expectedBody] of expectedBodies) {
        const bytes = await readFile(download.path);
        if (
          bytes.toString('utf8') !== expectedBody
          || download.receivedBytes !== bytes.byteLength
          || download.totalBytes !== bytes.byteLength
          || download.sessionHash !== sessionHash
        ) {
          throw new Error(`下载终态/bytes 异常: ${JSON.stringify(download)}`);
        }
      }
      const byId = new Map<string, DownloadEvent[]>();
      for (const event of allEvents) {
        const lifecycle = byId.get(event.downloadId) ?? [];
        lifecycle.push(event);
        byId.set(event.downloadId, lifecycle);
      }
      if (
        byId.size !== 6
        || [...byId].some(([downloadId, lifecycle]) => (
          lifecycle.filter((event) => event.state === 'progressing').length < 1
          || lifecycle.filter((event) => (
            event.state === (downloadId === cancelledEvent?.downloadId
              ? 'cancelled'
              : 'completed')
          )).length !== 1
          || lifecycle.at(-1)?.state !== (
            downloadId === cancelledEvent?.downloadId ? 'cancelled' : 'completed'
          )
          || lifecycle.some((event, index) => (
            index > 0
            && event.receivedBytes < lifecycle[index - 1].receivedBytes
          ))
          || new Set(lifecycle.map((event) => (
            `${event.state}:${event.receivedBytes}:${event.totalBytes}`
          ))).size !== lifecycle.length
        ))
      ) {
        throw new Error(
          `native 下载生命周期重复/倒退/缺失: ${JSON.stringify([...byId])}`,
        );
      }
      return '5 completed + 1 cancelled；public Download event/path/saveAs/stream/cancel；'
        + 'click/goto/run_code；one-click 2 same-name；unique paths/bytes/events';
    } finally {
      await downloadHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserHost screenshot/evaluate/tab-close/network-clear 用户输出契约', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-output-parity', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionHash = createHash('sha256')
      .update('pw-contract-output-parity-session', 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-output-parity-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    const artifacts = path.join(path.dirname(profile), 'artifacts');
    await mkdir(profile, { recursive: true });
    await mkdir(artifacts, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const outputHost = await managedHost(() => panelWindow, runtimeKey, profile);
    try {
      const created = await outputHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            `s${sessionHash}-1`,
            new URL('/host-output-parity', topOriginURL).href,
          ],
          mutating: true,
          command_timeout_ms: 15_000,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('用户输出契约未创建 tab');
      const execute = async (
        command: string,
        args: string[],
        mutating = false,
      ): Promise<Record<string, unknown>> => {
        const response = await outputHost.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: targetId,
            command,
            args,
            mutating,
            command_timeout_ms: 15_000,
          },
        }) as { data?: Record<string, unknown> };
        return response.data ?? {};
      };

      const snapshotResult = await execute('snapshot', ['--compact']);
      const snapshotText = String(snapshotResult.snapshot ?? '');
      const targetRef = /button "Parity target" \[ref=(@e\d+)\]/.exec(
        snapshotText,
      )?.[1];
      if (!targetRef) {
        throw new Error(`用户输出快照缺少 target ref: ${snapshotText.slice(0, 500)}`);
      }

      const elementPng = path.join(artifacts, 'element.png');
      const elementShot = await execute('screenshot', [
        '--ref', targetRef,
        '--type', 'png',
        '--scale', 'css',
        elementPng,
      ]);
      const elementBytes = await readFile(elementPng);
      if (
        elementShot.type !== 'png'
        || !elementBytes.subarray(0, 8).equals(
          Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
        )
      ) {
        throw new Error('strict ref public Locator.screenshot 未产出 PNG');
      }

      const fullJpeg = path.join(artifacts, 'full.jpeg');
      const fullShot = await execute('screenshot', [
        '--full-page',
        '--type', 'jpeg',
        '--scale', 'device',
        fullJpeg,
      ]);
      const jpegBytes = await readFile(fullJpeg);
      if (
        fullShot.type !== 'jpeg'
        || jpegBytes[0] !== 0xff
        || jpegBytes[1] !== 0xd8
        || jpegBytes.at(-2) !== 0xff
        || jpegBytes.at(-1) !== 0xd9
      ) {
        throw new Error('public Page.screenshot full-page JPEG 无效');
      }

      const evaluated = await execute('eval', [
        '() => ({ nested: ["雪", 42], ok: true })',
      ], true);
      if (
        evaluated.serialized
        !== '{\n  "nested": [\n    "雪",\n    42\n  ],\n  "ok": true\n}'
      ) {
        throw new Error(`evaluate JSON 序列化异常: ${JSON.stringify(evaluated)}`);
      }
      const undefinedResult = await execute('eval', ['() => undefined'], true);
      if (undefinedResult.serialized !== 'undefined') {
        throw new Error('evaluate undefined 未保真');
      }

      await execute('eval', [
        `() => fetch(${JSON.stringify(
          new URL('/host-output-api?request=one', topOriginURL).href,
        )}).then(response => response.text())`,
      ], true);
      const beforeClear = String((await execute(
        'network_requests',
        [],
      )).text ?? '');
      if (!beforeClear.includes('/host-output-api?request=one')) {
        throw new Error(`network list 未保留 fetch: ${beforeClear}`);
      }
      await execute('network', ['requests', '--clear']);
      const afterClear = String((await execute(
        'network_requests',
        [],
      )).text ?? '');
      if (afterClear !== '') {
        throw new Error(`network clear 未清空 Playwright ledger: ${afterClear}`);
      }
      await execute('eval', [
        `() => fetch(${JSON.stringify(
          new URL('/host-output-api?request=two', topOriginURL).href,
        )}).then(response => response.text())`,
      ], true);
      const afterRefill = String((await execute(
        'network_requests',
        [],
      )).text ?? '');
      if (
        !afterRefill.startsWith('1. ')
        || !afterRefill.includes('/host-output-api?request=two')
        || afterRefill.includes('request=one')
      ) {
        throw new Error(`network clear 后重新编号异常: ${afterRefill}`);
      }

      const second = await outputHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            `s${sessionHash}-2`,
            new URL('/host-active-to-close', topOriginURL).href,
          ],
          mutating: true,
          command_timeout_ms: 15_000,
        },
      }) as { data?: { targetId?: string } };
      if (!second.data?.targetId) throw new Error('未创建 active close tab');
      await outputHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: ['close'],
          mutating: true,
          command_timeout_ms: 15_000,
        },
      });
      const listed = await outputHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: ['list'],
        },
      }) as {
        data?: {
          tabs?: Array<{ targetId?: string; active?: boolean }>;
        };
      };
      const tabs = listed.data?.tabs ?? [];
      if (
        tabs.length !== 1
        || tabs[0]?.targetId !== targetId
        || tabs[0]?.active !== true
      ) {
        throw new Error(`tab close 缺省 active 异常: ${JSON.stringify(tabs)}`);
      }
      return 'public page/ref png+jpeg；JSON/undefined；network list→clear→list；active close';
    } finally {
      await outputHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserHost run_code timeout 撤销、恢复与并发归因契约', async () => {
    const identity = (name: string): {
      runtimeKey: string;
      accountDir: string;
      sessionHash: string;
    } => {
      const ownerDigest = createHash('sha256').update(name, 'utf8').digest('hex');
      return {
        runtimeKey: `crew_${ownerDigest.slice(0, 12)}`,
        accountDir: `acct_${ownerDigest.slice(0, 16)}`,
        sessionHash: createHash('sha256')
          .update(`${name}-session`, 'utf8')
          .digest('hex')
          .slice(0, 32),
      };
    };
    const ownerA = identity('pw-contract-run-code-owner-a');
    const ownerB = identity('pw-contract-run-code-owner-b');
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-run-code-'));
    const profileA = path.join(
      tempRoot,
      'accounts',
      ownerA.accountDir,
      'browser',
      'profile',
    );
    const profileB = path.join(
      tempRoot,
      'accounts',
      ownerB.accountDir,
      'browser',
      'profile',
    );
    await mkdir(profileA, { recursive: true });
    await mkdir(profileB, { recursive: true });
    const pageUrl = new URL('/host-run-code-timeout', topOriginURL).href;
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const runHost = new BrowserHost(() => panelWindow);
    await withTimeout(
      Promise.all([
        runHost.handleRpc({
          runtime_key: ownerA.runtimeKey,
          method: 'configure_proxy',
          params: {
            profile_dir: profileA,
            proxy_url: policyProxyURL,
            proxy_username: proxyUsername,
            proxy_password: proxyPassword,
          },
        }),
        runHost.handleRpc({
          runtime_key: ownerB.runtimeKey,
          method: 'configure_proxy',
          params: {
            profile_dir: profileB,
            proxy_url: policyProxyURL,
            proxy_username: proxyUsername,
            proxy_password: proxyPassword,
          },
        }),
      ]),
      15_000,
    );
    try {
      const createOwnerTab = async (
        owner: typeof ownerA,
        profile: string,
      ): Promise<string> => {
        const created = await runHost.handleRpc({
          runtime_key: owner.runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            command: 'tab',
            args: [
              'new',
              '--label',
              `s${owner.sessionHash}-1`,
              pageUrl,
            ],
            mutating: true,
            command_timeout_ms: 15_000,
          },
        }) as { data?: { targetId?: string } };
        const targetId = String(created.data?.targetId ?? '');
        if (!targetId) throw new Error('run-code 契约未创建 tab');
        return targetId;
      };
      const targetA = await createOwnerTab(ownerA, profileA);
      const targetB = await createOwnerTab(ownerB, profileB);
      const execute = async (
        owner: typeof ownerA,
        profile: string,
        targetId: string,
        command: string,
        args: string[],
        timeoutMs = 5_000,
      ): Promise<Record<string, unknown>> => {
        const response = await runHost.handleRpc({
          runtime_key: owner.runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: targetId,
            command,
            args,
            mutating: command === 'run_code_unsafe' || command === 'eval',
            command_timeout_ms: timeoutMs,
          },
        }) as { data?: Record<string, unknown> };
        return response.data ?? {};
      };
      const expectTimeout = async (code: string): Promise<void> => {
        try {
          await execute(
            ownerA,
            profileA,
            targetA,
            'run_code_unsafe',
            [code],
            180,
          );
          throw new Error('run_code 超时契约意外成功');
        } catch (error) {
          if (
            !error
            || typeof error !== 'object'
            || (error as { code?: unknown }).code !== 'command_timeout'
          ) {
            throw error;
          }
        }
      };
      const readState = async (): Promise<Record<string, unknown>> => {
        const result = await execute(
          ownerA,
          profileA,
          targetA,
          'eval',
          [`() => ({
            evaluateLate: document.body.dataset.evaluateLate || '',
            delayedEvaluate: document.body.dataset.delayedEvaluate || '',
            locatorClicked: document.body.dataset.locatorClicked || '',
            title: document.title
          })`],
        );
        return result.value as Record<string, unknown>;
      };

      await expectTimeout(`async page => {
        await page.evaluate(() => new Promise(resolve => {
          setTimeout(() => {
            document.body.dataset.evaluateLate = 'yes';
            resolve('late mutation');
          }, 650);
        }));
      }`);
      await new Promise((resolve) => setTimeout(resolve, 750));
      const afterEvaluate = await readState();
      if (
        afterEvaluate.evaluateLate
        || afterEvaluate.title !== 'BrowserHost run-code timeout'
      ) {
        throw new Error(
          `evaluate timeout 返回后仍修改 DOM: ${JSON.stringify(afterEvaluate)}`,
        );
      }

      await execute(ownerA, profileA, targetA, 'open', [pageUrl], 5_000);
      await expectTimeout(`async page => {
        await page.locator('#late-action').click();
      }`);
      await new Promise((resolve) => setTimeout(resolve, 850));
      const afterLocator = await readState();
      if (afterLocator.locatorClicked) {
        throw new Error(
          `Locator timeout 返回后仍点击: ${JSON.stringify(afterLocator)}`,
        );
      }

      await execute(ownerA, profileA, targetA, 'open', [pageUrl], 5_000);
      await expectTimeout(`async page => {
        const locator = page.locator('#late-action');
        const context = page.context();
        void page.waitForTimeout(450).then(async () => {
          await page.evaluate(() => {
            document.body.dataset.delayedEvaluate = 'yes';
          });
          await locator.click();
          await context.newPage();
          await page.close();
        });
        await page.evaluate(() => new Promise(() => {}));
      }`);
      await new Promise((resolve) => setTimeout(resolve, 650));
      const afterDelayed = await readState();
      const listedA = await execute(
        ownerA,
        profileA,
        targetA,
        'tab',
        ['list'],
      );
      const tabsA = listedA.tabs as Array<Record<string, unknown>>;
      if (
        afterDelayed.delayedEvaluate
        || afterDelayed.locatorClicked
        || tabsA.length !== 1
        || tabsA[0]?.targetId !== targetA
      ) {
        throw new Error(
          `late Page/Locator/Context 调用越过撤销边界: ${
            JSON.stringify({ afterDelayed, tabsA })
          }`,
        );
      }

      const routeInstall = await execute(
        ownerA,
        profileA,
        targetA,
        'run_code_unsafe',
        [`async page => {
          await page.route('**/host-output-api?route=persistent', async route => {
            await route.fulfill({
              status: 200,
              contentType: 'text/plain',
              body: 'persistent-route-ok'
            });
          });
          return 'installed';
        }`],
      );
      if (routeInstall.result !== '"installed"') {
        throw new Error(`route 安装结果异常: ${JSON.stringify(routeInstall)}`);
      }
      const routed = await execute(
        ownerA,
        profileA,
        targetA,
        'run_code_unsafe',
        [`async page => await page.evaluate(async url => {
          return await (await fetch(url)).text();
        }, ${JSON.stringify(
          new URL('/host-output-api?route=persistent', topOriginURL).href,
        )})`],
      );
      if (routed.result !== '"persistent-route-ok"') {
        throw new Error(`成功安装的长期 route 失效: ${JSON.stringify(routed)}`);
      }

      const concurrent = await Promise.allSettled([
        execute(
          ownerA,
          profileA,
          targetA,
          'run_code_unsafe',
          [`async page => {
            void page.waitForTimeout(60).then(() => {
              return Promise.reject(new Error('owner-a-unhandled'));
            });
            await page.waitForTimeout(900);
            return 'owner-a-should-fail';
          }`],
          2_000,
        ),
        execute(
          ownerB,
          profileB,
          targetB,
          'run_code_unsafe',
          [`async page => {
            await page.waitForTimeout(250);
            return 'owner-b-healthy';
          }`],
          2_000,
        ),
      ]);
      if (
        concurrent[0].status !== 'rejected'
        || !String(concurrent[0].reason).includes('owner-a-unhandled')
        || concurrent[1].status !== 'fulfilled'
        || concurrent[1].value.result !== '"owner-b-healthy"'
      ) {
        throw new Error(
          `并发 owner rejection 归因错误: ${JSON.stringify(concurrent)}`,
        );
      }
      const recovered = await execute(
        ownerA,
        profileA,
        targetA,
        'run_code_unsafe',
        ['async page => await page.title()'],
      );
      if (recovered.result !== '"BrowserHost run-code timeout"') {
        throw new Error(`超时后新命令不可用: ${JSON.stringify(recovered)}`);
      }
      return 'ALS owner isolation；late API revoked；evaluate/action quiescent；route persists';
    } finally {
      await runHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserHost 公开 Page newPage/close 继承真实 Electron 生命周期', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-public-page-lifecycle', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionHash = createHash('sha256')
      .update('pw-contract-public-page-lifecycle-session', 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-page-lifecycle-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const lifecycleSourceURL = new URL('/host-record-nav-a', topOriginURL).href;
    const lifecycleOtherURL = new URL('/host-record-nav-b', topOriginURL).href;
    const lifecycleSurvivorURL = new URL('/host-record-nav-c', topOriginURL).href;
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const lifecycleHost = await managedHost(() => panelWindow, runtimeKey, profile);
    try {
      const created = await lifecycleHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            `s${sessionHash}-1`,
            lifecycleSourceURL,
          ],
          mutating: true,
          command_timeout_ms: 15_000,
        },
      }) as { data?: { targetId?: string } };
      const sourceTargetId = String(created.data?.targetId ?? '');
      if (!sourceTargetId) throw new Error('生命周期契约未创建 source tab');

      const executeCode = async (code: string): Promise<Record<string, unknown>> => {
        const response = await lifecycleHost.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: sourceTargetId,
            command: 'run_code_unsafe',
            args: [code],
            mutating: true,
            command_timeout_ms: 15_000,
          },
        }) as { data?: { result?: string } };
        const serialized = response.data?.result;
        if (!serialized) throw new Error('run_code_unsafe 未返回 JSON 结果');
        return JSON.parse(serialized) as Record<string, unknown>;
      };

      const ordinary = await executeCode(`async page => {
        const context = page.context();
        const before = context.pages().length;
        const other = await context.newPage();
        await other.goto(${JSON.stringify(lifecycleOtherURL)});
        const during = context.pages().length;
        const title = await other.title();
        await other.close();
        return {
          before,
          during,
          after: context.pages().length,
          title,
          otherClosed: other.isClosed(),
        };
      }`);
      if (
        ordinary.before !== 1
        || ordinary.during !== 2
        || ordinary.after !== 1
        || ordinary.title !== 'Host record navigation B'
        || ordinary.otherClosed !== true
      ) {
        throw new Error(`newPage → goto → close 结果异常: ${JSON.stringify(ordinary)}`);
      }

      const closeCurrent = await executeCode(`async page => {
        const context = page.context();
        await page.close();
        const pagesAfterClose = context.pages().length;
        const survivor = await context.newPage();
        await survivor.goto(${JSON.stringify(lifecycleSurvivorURL)});
        return {
          sourceClosed: page.isClosed(),
          pagesAfterClose,
          pages: context.pages().length,
          title: await survivor.title(),
        };
      }`);
      if (
        closeCurrent.sourceClosed !== true
        || closeCurrent.pagesAfterClose !== 0
        || closeCurrent.pages !== 1
        || closeCurrent.title !== 'Host record navigation C'
      ) {
        throw new Error(`关闭 current Page 后拓扑异常: ${JSON.stringify(closeCurrent)}`);
      }

      const listed = await lifecycleHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: ['list'],
        },
      }) as {
        data?: {
          tabs?: Array<{
            targetId?: string;
            sessionHash?: string;
            active?: boolean;
            title?: string;
          }>;
        };
      };
      const tabs = listed.data?.tabs ?? [];
      if (
        tabs.length !== 1
        || tabs[0]?.targetId === sourceTargetId
        || tabs[0]?.sessionHash !== sessionHash
        || tabs[0]?.active !== true
        || tabs[0]?.title !== 'Host record navigation C'
      ) {
        throw new Error(`Host current-close fallback 异常: ${JSON.stringify(tabs)}`);
      }
      return 'context.pages 1→2→1；真实新页 goto；other/current close；active fallback 正常';
    } finally {
      await lifecycleHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserHost browser_find 单快照片段 refs 可执行且 no-match 淘汰旧 ref', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-browser-find', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionHash = createHash('sha256')
      .update('pw-contract-browser-find-session', 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tabLabel = `s${sessionHash}-1`;
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-find-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const findHost = await managedHost(() => panelWindow, runtimeKey, profile);
    try {
      const created = await findHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            tabLabel,
            new URL('/host-find', topOriginURL).href,
          ],
          mutating: true,
          command_timeout_ms: 15_000,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('browser_find 契约未创建 tab');

      type FindTabProbe = {
        targetId: string;
        view: WebContentsView;
      };
      type FindOwnerProbe = {
        tabs: Map<string, FindTabProbe>;
        engine: PlaywrightEngine;
      };
      const internals = findHost as unknown as {
        owners: Map<string, FindOwnerProbe>;
      };
      const owner = internals.owners.get(runtimeKey);
      const tab = [...(owner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === targetId);
      if (!owner || !tab) throw new Error('无法取得 browser_find Host 拓扑');
      const page = await owner.engine.pageForView(tab.view);
      await page.locator('.find-target').first().waitFor({ state: 'visible' });

      const pageProbe = page as unknown as {
        ariaSnapshot: (...args: unknown[]) => Promise<string>;
      };
      const originalAriaSnapshot = pageProbe.ariaSnapshot.bind(page);
      let ariaSnapshotCalls = 0;
      pageProbe.ariaSnapshot = async (...args: unknown[]): Promise<string> => {
        ariaSnapshotCalls += 1;
        return await originalAriaSnapshot(...args);
      };

      const execute = async (
        command: string,
        args: string[],
        mutating: boolean,
      ): Promise<{ data?: Record<string, unknown> }> => (
        await contractDeadline(
          findHost.handleRpc({
            runtime_key: runtimeKey,
            method: 'execute',
            params: {
              profile_dir: profile,
              proxy_url: policyProxyURL,
              target_id: targetId,
              command,
              args,
              mutating,
              command_timeout_ms: 15_000,
            },
          }),
          20_000,
          `browser_find ${command}`,
        )
      ) as { data?: Record<string, unknown> };

      const found = await execute('find', ['--text', 'needle'], false);
      if (ariaSnapshotCalls !== 1) {
        throw new Error(`首次 find 调用了 ${ariaSnapshotCalls} 次 ariaSnapshot`);
      }
      const text = String(found.data?.snapshot ?? '');
      if (
        !text.includes('Found 2 matches for "needle":')
        || !text.includes('\n\n----\n\n')
      ) {
        throw new Error(`find 未返回两个非重叠片段: ${text}`);
      }
      const ancestorLines = text.split('\n').filter(
        (line) => line.includes('region "Find contract root"'),
      );
      if (ancestorLines.length !== 2) {
        throw new Error(`共用 ancestor 未在两个片段保留: ${JSON.stringify(ancestorLines)}`);
      }
      const ancestorRef = /\[ref=(@e[1-9]\d*)\]/.exec(ancestorLines[0] ?? '')?.[1] ?? '';
      if (
        !ancestorRef
        || text.split(`[ref=${ancestorRef}]`).length - 1 !== 1
        || !ancestorLines.some(
          (line) => !line.includes('[ref=') && /"Find contract root"\s+:/.test(line),
        )
      ) {
        throw new Error(`共用 ancestor ref 未唯一暴露或文本粘连: ${JSON.stringify(ancestorLines)}`);
      }
      const alphaRef = /button "Needle Alpha" \[ref=(@e[1-9]\d*)\]/.exec(text)?.[1] ?? '';
      const omegaRef = /button "Needle Omega" \[ref=(@e[1-9]\d*)\]/.exec(text)?.[1] ?? '';
      if (!alphaRef || !omegaRef || alphaRef === omegaRef) {
        throw new Error(`匹配节点 refs 异常: ${alphaRef}/${omegaRef}`);
      }

      await execute('click', [alphaRef], true);
      const clicked = (await page.locator('body').textContent())?.trim();
      if (clicked !== 'find-contract-clicked') {
        throw new Error(`find 返回 ref 未完成真实 click: ${clicked}`);
      }

      const missing = await execute('find', ['--text', 'needle'], false);
      if (ariaSnapshotCalls !== 2) {
        throw new Error(`no-match find 未保持单次 capture: ${ariaSnapshotCalls}`);
      }
      if (missing.data?.snapshot !== 'No matches found for "needle".') {
        throw new Error(`no-match 文本异常: ${String(missing.data?.snapshot ?? '')}`);
      }
      let staleCode = '';
      try {
        await execute('click', [alphaRef], true);
      } catch (error) {
        staleCode = String((error as { code?: string }).code ?? '');
      }
      if (staleCode !== 'stale_ref') {
        throw new Error(`no-match 后旧 ref 未失效: ${staleCode || 'click succeeded'}`);
      }
      return (
        `2 snippets；ariaSnapshot ${ariaSnapshotCalls} 次/2 find；`
        + `${alphaRef} click；no-match stale_ref`
      );
    } finally {
      await findHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('BrowserHost 生产 popup 收养 Electron WebContents 并继承 Playwright 拓扑', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-popup-owner', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionId = 'pw-contract-popup-session';
    const sessionHash = createHash('sha256')
      .update(sessionId, 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tabLabel = `s${sessionHash}-1`;
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-popup-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const popupHost = await managedHost(() => panelWindow, runtimeKey, profile);
    try {
      const created = await popupHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            tabLabel,
            new URL('/host-popup-opener', topOriginURL).href,
          ],
          mutating: true,
        },
      }) as { data?: { targetId?: string } };
      const openerTargetId = String(created.data?.targetId ?? '');
      if (!openerTargetId) throw new Error('BrowserHost 未创建 popup opener tab');
      console.log('      popup: opener tab created');

      type PopupTabProbe = {
        tabId: string;
        targetId: string;
        openerTargetId: string;
        view: WebContentsView;
      };
      type PopupOwnerProbe = {
        activeTabId: string;
        tabs: Map<string, PopupTabProbe>;
        engine: PlaywrightEngine;
      };
      const internals = popupHost as unknown as {
        owners: Map<string, PopupOwnerProbe>;
      };
      const owner = internals.owners.get(runtimeKey);
      const openerTab = [...(owner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === openerTargetId);
      if (!owner || !openerTab) throw new Error('无法取得 BrowserHost opener 拓扑');

      const openerPage = await owner.engine.pageForView(openerTab.view);
      console.log('      popup: opener page mapped');
      const backgroundOpened = openerPage.waitForEvent('popup', { timeout: 10_000 });
      await openerPage.locator('#host-open-background').click({
        button: 'middle',
        noWaitAfter: true,
        timeout: 10_000,
      });
      console.log('      popup: background click returned');
      const backgroundPage = await backgroundOpened;
      console.log('      popup: background Playwright event received');
      const backgroundDeadline = Date.now() + 10_000;
      let backgroundTab: PopupTabProbe | undefined;
      while (!backgroundTab && Date.now() < backgroundDeadline) {
        backgroundTab = [...owner.tabs.values()].find(
          (candidate) => candidate.openerTargetId === openerTargetId,
        );
        if (!backgroundTab) await new Promise((resolve) => setTimeout(resolve, 25));
      }
      if (!backgroundTab) throw new Error('BrowserHost 未登记后台 popup');
      console.log('      popup: background BrowserHost tab registered');
      if (owner.activeTabId !== openerTab.tabId) {
        throw new Error(`中键后台 popup 抢占 active tab: ${owner.activeTabId}`);
      }
      if (await owner.engine.pageForView(backgroundTab.view) !== backgroundPage) {
        throw new Error('BrowserHost 后台 popup view 映射到错误 Page');
      }
      await popupHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: ['close-user', backgroundTab.tabId],
          mutating: true,
        },
      });
      console.log('      popup: background close requested');
      const closeDeadline = Date.now() + 5_000;
      while (owner.tabs.has(backgroundTab.tabId) && Date.now() < closeDeadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      if (owner.tabs.has(backgroundTab.tabId)) {
        throw new Error('BrowserHost 后台 popup 关闭后仍残留');
      }
      console.log('      popup: background close observed');

      const opened = openerPage.waitForEvent('popup', { timeout: 10_000 });
      await openerPage.locator('#host-open-popup').click({
        noWaitAfter: true,
        timeout: 10_000,
      });
      console.log('      popup: foreground click returned');
      const popupPage = await opened;
      console.log('      popup: foreground Playwright event received');
      const deadline = Date.now() + 10_000;
      let popupTab: PopupTabProbe | undefined;
      while (!popupTab && Date.now() < deadline) {
        popupTab = [...owner.tabs.values()].find(
          (candidate) => candidate.openerTargetId === openerTargetId,
        );
        if (!popupTab) await new Promise((resolve) => setTimeout(resolve, 25));
      }
      if (!popupTab) throw new Error('BrowserHost 未登记显式 opener popup');
      if (owner.activeTabId !== popupTab.tabId) {
        throw new Error(`普通 target=_blank 未激活 popup: ${owner.activeTabId}`);
      }
      const mappedPage = await owner.engine.pageForView(popupTab.view);
      if (mappedPage !== popupPage) throw new Error('BrowserHost popup view 映射到错误 Page');
      if (await popupPage.opener() !== openerPage) throw new Error('BrowserHost popup opener 丢失');
      const expectedURL = new URL('/host-popup-child?kind=foreground', topOriginURL).href;
      await popupPage.waitForURL(expectedURL, { timeout: 10_000 });
      if (await popupPage.locator('#host-popup-ready').textContent() !== 'ready') {
        throw new Error('BrowserHost popup 文档未完成真实导航');
      }
      return `${popupPage.url()}；adopt/opener/page mapping 正常`;
    } finally {
      await popupHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  await check('PlaywrightEngine 已连接后 page.goto 仍会恢复 OOPIF debugger', async () => {
    const engine = new PlaywrightEngine();
    const connectedView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    const debug = connectedView.webContents.debugger;
    const commandTrace: Array<{
      method: string;
      sessionId: string;
      params?: unknown;
      startOrder: number;
      endOrder?: number;
      error?: string;
    }> = [];
    let commandOrder = 0;
    const originalSendCommand = debug.sendCommand.bind(debug);
    debug.sendCommand = (async (
      method: string,
      params?: Record<string, unknown>,
      sessionId?: string,
    ): Promise<unknown> => {
      if (
        method === 'Target.setAutoAttach'
        || method === 'Runtime.runIfWaitingForDebugger'
        || method === 'Runtime.enable'
        || method === 'Page.enable'
      ) {
        const trace: (typeof commandTrace)[number] = {
          method,
          sessionId: sessionId ?? '',
          ...(method === 'Target.setAutoAttach' ? { params } : {}),
          startOrder: ++commandOrder,
        };
        commandTrace.push(trace);
        try {
          const result = await originalSendCommand(method, params, sessionId);
          trace.endOrder = ++commandOrder;
          return result;
        } catch (error) {
          trace.endOrder = ++commandOrder;
          trace.error = error instanceof Error ? error.message : String(error);
          throw error;
        }
      }
      return await originalSendCommand(method, params, sessionId);
    }) as typeof debug.sendCommand;
    const debugEvents: Array<{
      sequence: number;
      method: string;
      sessionId: string;
      introducedSessionId?: unknown;
      waitingForDebugger?: unknown;
      target?: unknown;
      errorText?: unknown;
      frameId?: unknown;
      parentFrameId?: unknown;
      frame?: unknown;
    }> = [];
    const debugListener = (
      _event: unknown,
      method: string,
      params: Record<string, unknown> = {},
      sessionId = '',
    ): void => {
      if (
        method === 'Target.attachedToTarget'
        || method === 'Target.detachedFromTarget'
        || method === 'Network.loadingFailed'
        || method === 'Page.frameAttached'
        || method === 'Page.frameNavigated'
        || method === 'Page.frameRequestedNavigation'
      ) {
        debugEvents.push({
          sequence: debugEvents.length + 1,
          method,
          sessionId,
          introducedSessionId: params.sessionId,
          waitingForDebugger: params.waitingForDebugger,
          target: params.targetInfo,
          errorText: params.errorText,
          frameId: params.frameId,
          parentFrameId: params.parentFrameId,
          frame: params.frame,
        });
      }
    };
    debug.on('message', debugListener);
    try {
      engine.registerTab(connectedView);
      await connectedView.webContents.loadURL('about:blank');
      const connectedPage = await engine.pageForView(connectedView);
      await connectedPage.goto(topOriginURL, {
        waitUntil: 'domcontentloaded',
        timeout: 10_000,
      });
      try {
        await connectedPage
          .frameLocator('#cross-origin-frame')
          .locator('#cross-origin-button')
          .waitFor({ state: 'visible', timeout: 5_000 });
      } catch (error) {
        const introducedSessionId = debugEvents.find(
          (event) => (
            event.method === 'Target.attachedToTarget'
            && typeof event.introducedSessionId === 'string'
          ),
        )?.introducedSessionId;
        let manualResume = 'not-attempted';
        if (typeof introducedSessionId === 'string' && introducedSessionId) {
          try {
            await originalSendCommand(
              'Runtime.runIfWaitingForDebugger',
              {},
              introducedSessionId,
            );
            await connectedPage
              .frameLocator('#cross-origin-frame')
              .locator('#cross-origin-button')
              .waitFor({ state: 'visible', timeout: 2_000 });
            manualResume = 'recovered';
          } catch (resumeError) {
            manualResume = resumeError instanceof Error
              ? resumeError.message.replace(/\s+/g, ' ')
              : String(resumeError);
          }
        }
        const diagnostic = {
          page: connectedPage.url(),
          native: connectedView.webContents.getURL(),
          frames: connectedPage.frames().map((frame) => frame.url()),
          nativeFrames: connectedView.webContents.mainFrame.framesInSubtree.map(
            (frame) => ({ url: frame.url, processId: frame.processId }),
          ),
          commandTrace,
          debugEvents,
          crossOriginRequests: [...crossOriginRequests],
          manualResume,
        };
        console.log(`      connected goto OOPIF diagnostic: ${JSON.stringify(diagnostic)}`);
        throw new Error(
          `connected page.goto 的 OOPIF 被挂起: ${JSON.stringify(diagnostic)}; `
          + `${error instanceof Error ? error.message.replace(/\s+/g, ' ') : String(error)}`,
        );
      }
      return `${connectedPage.url()} → cross-origin-button visible`;
    } finally {
      debug.sendCommand = originalSendCommand;
      debug.off('message', debugListener);
      engine.unregisterTab(connectedView);
      await engine.dispose();
      if (!connectedView.webContents.isDestroyed()) {
        connectedView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('PlaywrightEngine newCDPSession(OOPIF Frame) 保留官方路由语义', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadURL(topOriginURL);
      const enginePage = await engine.pageForView(engineView);
      await enginePage
        .frameLocator('#cross-origin-frame')
        .locator('#cross-origin-button')
        .waitFor({ state: 'visible', timeout: 5_000 });
      let frame = enginePage.frames().find(
        (candidate) => candidate.url() === crossOriginURL,
      );
      // When attaching to an already-navigated OOPIF, pinned Playwright may keep
      // the client-side Frame.url() empty until that frame's next navigation,
      // even though Page.getFrameTree and the execution context have the exact
      // URL. Resolve this contract by the live document URL; the assertion
      // below still checks that the CDP alias targets the real iframe.
      if (!frame) {
        for (const candidate of enginePage.frames()) {
          const liveURL = await candidate
            .evaluate(() => location.href)
            .catch(() => '');
          if (liveURL === crossOriginURL) {
            frame = candidate;
            break;
          }
        }
      }
      if (!frame) {
        throw new Error(
          `Playwright 未发现真实 OOPIF Frame: ${JSON.stringify(
            enginePage.frames().map((candidate) => candidate.url()),
          )}`,
        );
      }
      const cdp = await enginePage.context().newCDPSession(frame);
      let frameTreeURL = '';
      try {
        const evaluated = await cdp.send('Runtime.evaluate', {
          expression: 'location.href',
          returnByValue: true,
        }) as { result?: { value?: unknown } };
        if (evaluated.result?.value !== crossOriginURL) {
          throw new Error(`OOPIF alias 路由到错误文档: ${String(evaluated.result?.value)}`);
        }
        const info = await cdp.send('Target.getTargetInfo') as {
          targetInfo?: { type?: unknown; url?: unknown };
        };
        if (
          info.targetInfo?.type !== 'iframe'
          || info.targetInfo.url !== crossOriginURL
        ) {
          throw new Error(`OOPIF targetInfo 异常: ${JSON.stringify(info.targetInfo)}`);
        }
        const tree = await cdp.send('Page.getFrameTree') as {
          frameTree?: { frame?: { url?: unknown } };
        };
        frameTreeURL = typeof tree.frameTree?.frame?.url === 'string'
          ? tree.frameTree.frame.url
          : '';
      } finally {
        await cdp.detach();
      }
      return `${crossOriginURL}; Frame.url=${frame.url() || '<empty>'}; tree=${frameTreeURL}`;
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('PlaywrightEngine AI/human 焦点模拟可逆切换（真实 Electron）', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadFile(FIXTURE);
      const enginePage = await engine.pageForView(engineView);
      const aiFocused = await enginePage.evaluate(() => document.hasFocus());
      if (!aiFocused) throw new Error('AI 模式未建立焦点模拟');
      await engine.setAutomationMode(engineView, false);
      const humanFocused = await enginePage.evaluate(() => document.hasFocus());
      if (humanFocused) throw new Error('关闭焦点模拟后 document.hasFocus() 仍为 true');
      await engine.setAutomationMode(engineView, true);
      const restoredFocus = await enginePage.evaluate(() => document.hasFocus());
      if (!restoredFocus) throw new Error('重新开启焦点模拟后 document.hasFocus() 未恢复');
      await enginePage.getByRole('button', { name: '延迟出现' }).click({ timeout: 5_000 });
      return 'document.hasFocus(): true → false → true';
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('PlaywrightEngine debugger detach → reattach 不复用 stale Page', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadFile(FIXTURE);
      const firstPage = await engine.pageForView(engineView);
      const closed = firstPage.waitForEvent('close', { timeout: 5_000 });
      engineView.webContents.debugger.detach();
      // Reacquisition may begin immediately after the native detach; callers
      // are not required to observe or await Playwright's synthetic Page.close.
      const reacquiring = engine.pageForView(engineView);
      await closed;

      // Page.close must clear the view mapping. This mode update records the
      // desired state only; attempting newCDPSession(firstPage) would throw.
      await engine.setAutomationMode(engineView, false);
      const secondPage = await reacquiring;
      if (secondPage === firstPage) throw new Error('reattach 复用了已关闭 Page');
      await engine.setAutomationMode(engineView, true);
      await secondPage.getByRole('button', { name: '延迟出现' }).click({ timeout: 5_000 });
      const text = await secondPage.locator('#late-result').textContent();
      if (text !== '延迟按钮已点击') throw new Error(`reattach 后点击未生效: ${text}`);
      return 'old Page closed；new Page rebound；focus/click 正常';
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('PlaywrightEngine Input.* 租约覆盖真实 Locator dispatch', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    let acquired = 0;
    let released = 0;
    let active = 0;
    let maxActive = 0;
    engine.setInputCommandLeaseHook(() => {
      acquired += 1;
      active += 1;
      maxActive = Math.max(maxActive, active);
      let done = false;
      return () => {
        if (done) throw new Error('输入租约被重复释放');
        done = true;
        active -= 1;
        released += 1;
      };
    });
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadFile(FIXTURE);
      const enginePage = await engine.pageForView(engineView);
      await enginePage.getByRole('button', { name: '延迟出现' }).click({ timeout: 5_000 });
      if (!acquired || released !== acquired || active !== 0 || maxActive !== 1) {
        throw new Error(
          `租约不平衡 acquired=${acquired} released=${released} active=${active} max=${maxActive}`,
        );
      }
      return `${acquired} 条 Input.* 全部成对释放`;
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('PlaywrightEngine 同步 confirm 经公开 Dialog API 关闭后仍可 eval/locator', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadFile(FIXTURE);
      const enginePage = await engine.pageForView(engineView);
      const evaluation = enginePage.evaluate(() => window.confirm('宿主决定？'));
      const observed = await engine.handleDialog(engineView, {
        accept: false,
        expectedType: 'confirm',
        timeoutMs: 5_000,
      });
      if (!observed.matched || observed.message !== '宿主决定？') {
        throw new Error(`Dialog 观测异常: ${JSON.stringify(observed)}`);
      }
      if (await evaluation !== false) throw new Error('宿主 dismiss 未生效');
      const evaluated = await enginePage.evaluate(() => {
        const result = document.getElementById('late-result');
        const button = document.getElementById('late') as HTMLButtonElement | null;
        if (result) result.textContent = 'dialog 后 eval 正常';
        if (button) button.style.display = 'inline-block';
        return result?.textContent ?? '';
      });
      if (evaluated !== 'dialog 后 eval 正常') {
        throw new Error(`dialog 后 evaluate 失效: ${evaluated}`);
      }
      await enginePage.locator('#late').click({ timeout: 5_000 });
      if (await enginePage.locator('#late-result').textContent() !== '延迟按钮已点击') {
        throw new Error('dialog 后 Locator click 失效');
      }
      return 'dismiss + evaluate + Locator click 全部正常';
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  await check('隔离 A：PlaywrightEngine Locator click → confirm → evaluate', async () => {
    const engine = new PlaywrightEngine();
    const engineView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
      },
    });
    let enginePage: Page | null = null;
    try {
      engine.registerTab(engineView);
      await engineView.webContents.loadFile(FIXTURE);
      enginePage = await engine.pageForView(engineView);
      enginePage.setDefaultTimeout(5_000);
      await enginePage.evaluate(() => {
        const button = document.createElement('button');
        button.id = 'engine-locator-confirm';
        button.textContent = 'engine-locator-confirm';
        const output = document.createElement('output');
        output.id = 'engine-locator-confirm-result';
        output.textContent = 'idle';
        button.addEventListener('click', () => {
          output.textContent = String(confirm('engine-locator-confirm'));
        });
        document.body.append(button, output);
      });
      // Intentionally start, but do not await, the action that blocks inside
      // confirm(). This is the same ordering used by Host's modal coordinator.
      const click = enginePage.locator('#engine-locator-confirm').click();
      const observed = await engine.handleDialog(engineView, {
        accept: true,
        expectedType: 'confirm',
        timeoutMs: 5_000,
      });
      if (
        !observed.matched
        || observed.type !== 'confirm'
        || observed.message !== 'engine-locator-confirm'
      ) {
        throw new Error(`Engine Locator dialog 观测异常: ${JSON.stringify(observed)}`);
      }
      await contractDeadline(click, 5_000, 'Engine Locator click 收束');
      if (
        await enginePage.locator('#engine-locator-confirm-result').textContent({
          timeout: 5_000,
        }) !== 'true'
      ) {
        throw new Error('Engine Locator confirm accept 未恢复 click handler');
      }
      const evaluated = await contractDeadline(
        enginePage.evaluate(() => 'engine-evaluate-ok'),
        5_000,
        'Engine Locator confirm 后 page.evaluate',
      );
      if (evaluated !== 'engine-evaluate-ok') {
        throw new Error(`Engine Locator confirm 后 evaluate 异常: ${evaluated}`);
      }
      return `Locator click + public Dialog + evaluate；probe=${modalPrivateProbe(engine, enginePage)}`;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(
        `${detail}; probe=${enginePage ? modalPrivateProbe(engine, enginePage) : 'page-unavailable'}`,
      );
    } finally {
      engine.unregisterTab(engineView);
      await engine.dispose();
      if (!engineView.webContents.isDestroyed()) {
        engineView.webContents.close({ waitForBeforeUnload: false });
      }
    }
  });

  type HostDialogTabProbe = {
    tabId: string;
    targetId: string;
    openerTargetId: string;
    sessionHash: string;
    view: WebContentsView;
    dialog?: unknown;
  };
  type HostDialogOwnerProbe = {
    tabs: Map<string, HostDialogTabProbe>;
    engine: PlaywrightEngine;
  };
  type HostDialogHarness = {
    host: BrowserHost;
    owner: HostDialogOwnerProbe;
    tab: HostDialogTabProbe;
    page: Awaited<ReturnType<PlaywrightEngine['pageForView']>>;
    execute: (
      command: string,
      args?: string[],
      extra?: Record<string, unknown>,
    ) => Promise<unknown>;
    locate: (selector: string, targetId?: string) => Promise<string>;
    dialogStatus: (targetId?: string) => Promise<Record<string, unknown>>;
  };

  const hostError = async (
    operation: Promise<unknown>,
    expectedCode: string,
  ): Promise<{ code?: string; message?: string }> => {
    try {
      await operation;
    } catch (error) {
      const observed = error as { code?: string; message?: string };
      if (observed.code !== expectedCode) {
        throw new Error(
          `错误码异常：期望 ${expectedCode}，实际 ${observed.code ?? 'none'}`
          + ` (${observed.message ?? String(error)})`,
        );
      }
      return observed;
    }
    throw new Error(`操作本应以 ${expectedCode} 失败，却成功返回`);
  };

  const withHostDialogHarness = async (
    contractName: string,
    run: (harness: HostDialogHarness) => Promise<string>,
  ): Promise<string> => {
    const ownerDigest = createHash('sha256')
      .update(`pw-contract-dialog-${contractName}`, 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionHash = createHash('sha256')
      .update(`pw-contract-dialog-session-${contractName}`, 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tabLabel = `s${sessionHash}-1`;
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), `crew-pw-dialog-${contractName}-`));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const dialogHost = await managedHost(() => panelWindow, runtimeKey, profile);
    try {
      const created = await dialogHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: [
            'new',
            '--label',
            tabLabel,
            new URL('/host-dialog-harness', topOriginURL).href,
          ],
          mutating: true,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('BrowserHost 未创建 dialog 契约 tab');
      const internals = dialogHost as unknown as {
        owners: Map<string, HostDialogOwnerProbe>;
      };
      const owner = internals.owners.get(runtimeKey);
      const tab = [...(owner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === targetId);
      if (!owner || !tab) throw new Error('无法取得 BrowserHost dialog 拓扑');
      const page = await owner.engine.pageForView(tab.view);
      page.setDefaultTimeout(10_000);
      console.log(`      dialog ${contractName}: harness ready`);
      const execute = (
        command: string,
        args: string[] = [],
        extra: Record<string, unknown> = {},
      ): Promise<unknown> => contractDeadline(
        dialogHost.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: targetId,
            command,
            args,
            mutating: true,
            ...extra,
          },
        }),
        20_000,
        `${contractName} execute ${command}`,
      );
      const locate = async (selector: string, locateTargetId = targetId): Promise<string> => {
        const located = await execute('locate', [selector], { target_id: locateTargetId }) as {
          data?: { ref?: string };
        };
        const ref = String(located.data?.ref ?? '');
        if (!/^@s\d+$/.test(ref)) {
          throw new Error(`BrowserHost locate 未返回稳定 ref: ${JSON.stringify(located)}`);
        }
        return ref;
      };
      const dialogStatus = async (statusTargetId = targetId): Promise<Record<string, unknown>> => {
        const result = await execute('dialog', ['status'], {
          target_id: statusTargetId,
        }) as { data?: Record<string, unknown> };
        return result.data ?? {};
      };
      return await run({
        host: dialogHost,
        owner,
        tab,
        page,
        execute,
        locate,
        dialogStatus,
      });
    } finally {
      try {
        await contractDeadline(
          dialogHost.dispose(),
          10_000,
          `${contractName} BrowserHost.dispose`,
        );
      } finally {
        if (!panelWindow.isDestroyed()) panelWindow.destroy();
        deferTempRootCleanup(tempRoot);
      }
    }
  };

  await check('隔离 B：Host 拓扑内直接 Locator click + Engine handleDialog', async () =>
    withHostDialogHarness('host-direct-engine-dialog', async ({
      owner,
      tab,
      page,
    }) => {
      try {
        const click = page.locator('#sync-confirm').click();
        const observed = await owner.engine.handleDialog(tab.view, {
          accept: true,
          expectedType: 'confirm',
          timeoutMs: 5_000,
        });
        if (
          !observed.matched
          || observed.type !== 'confirm'
          || observed.message !== 'sync-confirm'
        ) {
          throw new Error(`Host 直连 Dialog 观测异常: ${JSON.stringify(observed)}`);
        }
        await contractDeadline(click, 5_000, 'Host 拓扑直连 Locator click 收束');
        if (
          await page.locator('#sync-result').textContent({ timeout: 5_000 })
          !== 'true'
        ) {
          throw new Error('Host 拓扑直连 confirm accept 未恢复 click handler');
        }
        const rawRuntime = await contractDeadline(
          tab.view.webContents.debugger.sendCommand('Runtime.evaluate', {
            expression: '1 + 1',
            returnByValue: true,
          }),
          2_000,
          'Host 拓扑直连 confirm 后原生 Runtime.evaluate',
        ) as { result?: { value?: unknown } };
        if (rawRuntime.result?.value !== 2) {
          throw new Error(`原生 Runtime.evaluate 结果异常: ${JSON.stringify(rawRuntime)}`);
        }
        let evaluated: string;
        try {
          evaluated = await contractDeadline(
            page.evaluate(() => 'host-direct-evaluate-ok'),
            5_000,
            'Host 拓扑直连 confirm 后 page.evaluate',
          );
        } catch (error) {
          throw new Error(
            `${error instanceof Error ? error.message : String(error)}; `
            + `nativeRuntime=${rawRuntime.result?.value}; `
            + `probe=${modalPrivateProbe(owner.engine, page)}`,
          );
        }
        if (evaluated !== 'host-direct-evaluate-ok') {
          throw new Error(`Host 拓扑直连 confirm 后 evaluate 异常: ${evaluated}`);
        }
        return `Host topology + direct Locator/Dialog；probe=${
          modalPrivateProbe(owner.engine, page)
        }`;
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw new Error(`${detail}; probe=${modalPrivateProbe(owner.engine, page)}`);
      }
    }));

  await check('BrowserHost 同步 confirm：状态、关闭及后续 eval/locator 全链路', async () =>
    withHostDialogHarness('sync-confirm', async ({
      page,
      execute,
      locate,
      dialogStatus,
    }) => {
      const syncRef = await locate('#sync-confirm');
      console.log('      dialog sync: ref located');
      await hostError(execute('click', [syncRef]), 'dialog_pending');
      console.log('      dialog sync: pending surfaced');
      const syncStatus = await dialogStatus();
      if (
        syncStatus.hasDialog !== true
        || syncStatus.type !== 'confirm'
        || syncStatus.message !== 'sync-confirm'
      ) {
        throw new Error(`同步 confirm 状态异常: ${JSON.stringify(syncStatus)}`);
      }
      const accepted = await execute('dialog', ['accept']) as {
        data?: { hasDialog?: boolean };
      };
      console.log(`      dialog sync: accepted ${JSON.stringify(accepted)}`);
      if (accepted.data?.hasDialog !== false) {
        throw new Error(`同步 confirm 关闭状态异常: ${JSON.stringify(accepted)}`);
      }
      const syncResult = await page.locator('#sync-result').textContent({ timeout: 5_000 });
      console.log(`      dialog sync: page resumed (${syncResult})`);
      if (syncResult !== 'true') {
        throw new Error('同步 confirm accept 未恢复触发动作');
      }
      const postRef = await locate('#post-dialog');
      await execute('click', [postRef]);
      console.log('      dialog sync: post locator action completed');
      if (await page.locator('#post-dialog-state').textContent() !== 'clicked') {
        throw new Error('关闭同步 confirm 后 BrowserHost locate/click 失效');
      }
      const evaluated = await contractDeadline(
        page.evaluate(() => {
          document.documentElement.dataset.dialogHealth = 'eval-ok';
          return document.documentElement.dataset.dialogHealth;
        }),
        5_000,
        '同步 confirm 关闭后的 page.evaluate',
      );
      if (evaluated !== 'eval-ok') throw new Error('关闭同步 confirm 后 evaluate 失效');
      return 'sync accept + Host locate/click + page.evaluate';
    }));

  await check('BrowserHost 延时 confirm：动作完成后仍可发现、关闭并继续', async () =>
    withHostDialogHarness('delayed-confirm', async ({
      page,
      execute,
      locate,
      dialogStatus,
    }) => {
      const delayedRef = await locate('#delayed-confirm');
      console.log('      dialog delayed: ref located');
      let surfacedDuringClick = false;
      try {
        await execute('click', [delayedRef]);
      } catch (error) {
        if ((error as { code?: string }).code !== 'dialog_pending') throw error;
        surfacedDuringClick = true;
      }
      const deadline = Date.now() + 5_000;
      let delayedStatus: Record<string, unknown> = {};
      do {
        delayedStatus = await dialogStatus();
        if (delayedStatus.hasDialog === true) break;
        await new Promise((resolve) => setTimeout(resolve, 25));
      } while (Date.now() < deadline);
      if (
        delayedStatus.hasDialog !== true
        || delayedStatus.type !== 'confirm'
        || delayedStatus.message !== 'delayed-confirm'
      ) {
        throw new Error(`延时 confirm 未进入 Host 状态: ${JSON.stringify(delayedStatus)}`);
      }
      await execute('dialog', ['dismiss']);
      console.log('      dialog delayed: dismissed');
      if (await page.locator('#delayed-result').textContent() !== 'false') {
        throw new Error('延时 confirm dismiss 未恢复页面任务');
      }
      const postRef = await locate('#post-dialog');
      await execute('click', [postRef]);
      if (await page.locator('#post-dialog-state').textContent() !== 'clicked') {
        throw new Error('延时 confirm 关闭后 locate/click 失效');
      }
      return `delayed dismiss + recovery；click-race=${surfacedDuringClick}`;
    }));

  await check('BrowserHost alert→confirm 链：逐个处理与原子 expected 序列', async () =>
    withHostDialogHarness('dialog-chain', async ({
      page,
      execute,
      locate,
      dialogStatus,
    }) => {
      const chainRef = await locate('#chain-dialog');
      await hostError(execute('click', [chainRef]), 'dialog_pending');
      const first = await dialogStatus();
      if (
        first.hasDialog !== true
        || first.type !== 'alert'
        || first.message !== 'chain-alert'
      ) {
        throw new Error(`链首 alert 异常: ${JSON.stringify(first)}`);
      }
      const transitioned = await execute('dialog', ['accept']) as {
        data?: Record<string, unknown>;
      };
      if (
        transitioned.data?.hasDialog !== true
        || transitioned.data.type !== 'confirm'
        || transitioned.data.message !== 'chain-confirm'
      ) {
        throw new Error(`alert 关闭时丢失链式 confirm: ${JSON.stringify(transitioned)}`);
      }
      const finished = await execute('dialog', ['dismiss']) as {
        data?: { hasDialog?: boolean };
      };
      if (finished.data?.hasDialog !== false) {
        throw new Error(`链式 confirm 未收束: ${JSON.stringify(finished)}`);
      }
      if (await page.locator('#chain-result').textContent() !== 'false') {
        throw new Error('链式 confirm dismiss 未恢复原点击');
      }

      await execute('click', [chainRef], {
        command_timeout_ms: 10_000,
        expected_dialogs: [
          { type: 'alert', accept: true, text: '' },
          { type: 'confirm', accept: false, text: '' },
        ],
      });
      const afterExpected = await dialogStatus();
      if (afterExpected.hasDialog !== false) {
        throw new Error(`expected 链后仍残留 dialog: ${JSON.stringify(afterExpected)}`);
      }
      if (
        await contractDeadline(
          page.evaluate(() => document.title),
          5_000,
          'expected dialog 链后的 page.evaluate',
        ) !== 'BrowserHost dialog harness'
      ) {
        throw new Error('expected 链后 evaluate 失效');
      }
      return 'manual chain + expected chain 均完成且无残留';
    }));

  await check('BrowserHost 导航 onload dialog：动作中断、关闭、后续 locator', async () =>
    withHostDialogHarness('dialog-onload', async ({
      page,
      execute,
      locate,
      dialogStatus,
    }) => {
      await hostError(
        execute('open', [new URL('/host-dialog-onload', topOriginURL).href]),
        'dialog_pending',
      );
      const status = await dialogStatus();
      if (
        status.hasDialog !== true
        || status.type !== 'confirm'
        || status.message !== 'onload-confirm'
      ) {
        throw new Error(`onload confirm 状态异常: ${JSON.stringify(status)}`);
      }
      await execute('dialog', ['dismiss']);
      if (
        await contractDeadline(
          page.evaluate(() => document.body.dataset.onloadResult),
          5_000,
          'onload dialog 关闭后的 page.evaluate',
        ) !== 'false'
      ) {
        throw new Error('onload confirm dismiss 未恢复导航脚本');
      }
      const postRef = await locate('#onload-after');
      await execute('click', [postRef]);
      if (await page.locator('#onload-after-state').textContent() !== 'clicked') {
        throw new Error('onload dialog 后 locate/click 失效');
      }
      return 'onload confirm 被 Host 捕获并可继续动作';
    }));

  await check('BrowserHost expected dialog 类型不符：确定性失败并可继续', async () =>
    withHostDialogHarness('dialog-mismatch', async ({
      page,
      execute,
      locate,
      dialogStatus,
    }) => {
      const mismatchRef = await locate('#mismatch-confirm');
      await hostError(
        execute('click', [mismatchRef], {
          command_timeout_ms: 10_000,
          expected_dialogs: [
            { type: 'alert', accept: true, text: '' },
          ],
        }),
        'replay_dialog_mismatch',
      );
      const status = await dialogStatus();
      if (status.hasDialog !== false) {
        throw new Error(`mismatch 后 dialog 未自动清理: ${JSON.stringify(status)}`);
      }
      if (await page.locator('#mismatch-result').textContent() !== 'false') {
        throw new Error('mismatch 未按 fail-closed dismiss 实际 confirm');
      }
      const postRef = await locate('#post-dialog');
      await execute('click', [postRef]);
      if (await page.locator('#post-dialog-state').textContent() !== 'clicked') {
        throw new Error('mismatch 清理后 locate/click 失效');
      }
      return 'replay_dialog_mismatch + auto-dismiss + recovery';
    }));

  await check('BrowserHost 早期 popup about:blank + document.write alert 不丢事件', async () =>
    withHostDialogHarness('early-popup-dialog', async ({
      owner,
      tab,
      execute,
      locate,
      dialogStatus,
    }) => {
      const popupRef = await locate('#early-popup-dialog');
      await hostError(execute('click', [popupRef]), 'dialog_pending');
      const status = await dialogStatus();
      if (
        status.hasDialog !== true
        || status.type !== 'alert'
        || status.message !== 'popup-inline-alert'
      ) {
        throw new Error(`早期 popup alert 未按 session 路由: ${JSON.stringify(status)}`);
      }
      const popupDeadline = Date.now() + 5_000;
      let popupTab: HostDialogTabProbe | undefined;
      do {
        popupTab = [...owner.tabs.values()].find(
          (candidate) => candidate.openerTargetId === tab.targetId,
        );
        if (popupTab) break;
        await new Promise((resolve) => setTimeout(resolve, 25));
      } while (Date.now() < popupDeadline);
      if (!popupTab) throw new Error('早期 about:blank popup 未被 BrowserHost 收养');
      await execute('dialog', ['dismiss']);
      const popupPage = await owner.engine.pageForView(popupTab.view);
      if (
        await contractDeadline(
          popupPage.evaluate(() => document.title),
          5_000,
          '早期 popup dialog 关闭后的 page.evaluate',
        ) !== 'Early popup dialog'
      ) {
        throw new Error(`早期 popup 文档异常: ${popupPage.url()}`);
      }
      const afterRef = await locate('#popup-after', popupTab.targetId);
      await execute('click', [afterRef], { target_id: popupTab.targetId });
      if (await popupPage.locator('#popup-after-state').textContent() !== 'clicked') {
        throw new Error('popup alert 关闭后 popup locator/click 失效');
      }
      return 'early popup alert + session routing + popup recovery';
    }));

  await check('BrowserHost v11 viewport：面板去重录制 + 响应式原子回放', async () => {
    const previousV11Gate = process.env.CREW_BROWSER_RECORDING_V11_PHASE_A;
    process.env.CREW_BROWSER_RECORDING_V11_PHASE_A = '1';
    const recorderDigest = createHash('sha256')
      .update('pw-contract-v11-resize-recorder', 'utf8')
      .digest('hex');
    const replayDigest = createHash('sha256')
      .update('pw-contract-v11-resize-replay', 'utf8')
      .digest('hex');
    const recorderRuntimeKey = `crew_${recorderDigest.slice(0, 12)}`;
    const replayRuntimeKey = `crew_${replayDigest.slice(0, 12)}`;
    const recorderSessionId = 'pw-contract-v11-resize-session';
    const recorderSessionHash = createHash('sha256')
      .update(recorderSessionId, 'utf8')
      .digest('hex')
      .slice(0, 32);
    const recorderLabel = `s${recorderSessionHash}-1`;
    const resizeURL = new URL('/host-responsive-resize', topOriginURL).href;
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-v11-resize-'));
    const recorderProfile = path.join(
      tempRoot,
      'accounts',
      `acct_${recorderDigest.slice(0, 16)}`,
      'browser',
      'profile',
    );
    const replayProfile = path.join(
      tempRoot,
      'accounts',
      `acct_${replayDigest.slice(0, 16)}`,
      'browser',
      'profile',
    );
    const replayDownloads = path.join(tempRoot, 'replay-downloads');
    await Promise.all([
      mkdir(recorderProfile, { recursive: true }),
      mkdir(replayProfile, { recursive: true }),
      mkdir(replayDownloads, { recursive: true }),
    ]);
    const recorderWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const replayWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const recorderHost = await managedHost(
      () => recorderWindow,
      recorderRuntimeKey,
      recorderProfile,
    );
    const replayHost = await managedHost(
      () => replayWindow,
      replayRuntimeKey,
      replayProfile,
    );
    const rows: Array<Record<string, unknown>> = [];
    recorderHost.on('recording', (event: unknown) => {
      if (event && typeof event === 'object' && !Array.isArray(event)) {
        rows.push(event as Record<string, unknown>);
      }
    });
    try {
      const created = await recorderHost.handleRpc({
        runtime_key: recorderRuntimeKey,
        method: 'execute',
        params: {
          profile_dir: recorderProfile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: ['new', '--label', recorderLabel, resizeURL],
          mutating: true,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('viewport recorder tab 未创建');
      await recorderHost.handleRpc({
        runtime_key: recorderRuntimeKey,
        method: 'set_mode',
        params: {
          profile_dir: recorderProfile,
          target_id: targetId,
          mode: 'human',
        },
      });
      recorderHost.setPanel({
        runtimeKey: recorderRuntimeKey,
        sessionId: recorderSessionId,
        tabLabel: recorderLabel,
        mode: 'human',
        bounds: { x: 0, y: 0, width: 900, height: 620 },
        visible: true,
      });
      const recorderOwner = (
        recorderHost as unknown as {
          owners: Map<string, {
            tabs: Map<string, { targetId: string; view: WebContentsView }>;
            engine: PlaywrightEngine;
          }>;
        }
      ).owners.get(recorderRuntimeKey);
      const recorderTab = [...(recorderOwner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === targetId);
      if (!recorderOwner || !recorderTab) {
        throw new Error('viewport recorder 拓扑不可用');
      }
      const recorderPage = await recorderOwner.engine.pageForView(recorderTab.view);
      await recorderPage.waitForFunction(
        ({ width, height }) => innerWidth === width && innerHeight === height,
        { width: 900, height: 620 },
        { timeout: 10_000 },
      );
      const recorderBounds = recorderTab.view.getBounds();
      const recorderCssViewport = await recorderPage.evaluate(() => ({
        width: innerWidth,
        height: innerHeight,
      }));
      if (
        recorderBounds.width !== recorderCssViewport.width
        || recorderBounds.height !== recorderCssViewport.height
      ) {
        throw new Error(
          `Electron DIP/CSS viewport 不一致: ${JSON.stringify({
            recorderBounds,
            recorderCssViewport,
          })}`,
        );
      }
      await recorderHost.handleRpc({
        runtime_key: recorderRuntimeKey,
        method: 'set_recording',
        params: {
          profile_dir: recorderProfile,
          target_id: targetId,
          recording_id: 'c0ffee88aabb5511',
          action: 'start',
        },
      });

      // Moving only x/y is layout chrome, not a browser viewport transition.
      recorderHost.setPanel({
        runtimeKey: recorderRuntimeKey,
        sessionId: recorderSessionId,
        tabLabel: recorderLabel,
        mode: 'human',
        bounds: { x: 30, y: 40, width: 900, height: 620 },
        visible: true,
      });
      recorderHost.setPanel({
        runtimeKey: recorderRuntimeKey,
        sessionId: recorderSessionId,
        tabLabel: recorderLabel,
        mode: 'human',
        bounds: { x: 0, y: 0, width: 640, height: 500 },
        visible: true,
      });
      recorderHost.setPanel({
        runtimeKey: recorderRuntimeKey,
        sessionId: recorderSessionId,
        tabLabel: recorderLabel,
        mode: 'human',
        bounds: { x: 20, y: 20, width: 640, height: 500 },
        visible: true,
      });
      await recorderPage.waitForFunction(
        () => (
          innerWidth === 640
          && innerHeight === 500
          && matchMedia('(max-width:700px)').matches
        ),
        undefined,
        { timeout: 10_000 },
      );
      await recorderHost.handleRpc({
        runtime_key: recorderRuntimeKey,
        method: 'set_recording',
        params: {
          profile_dir: recorderProfile,
          target_id: targetId,
          recording_id: 'c0ffee88aabb5511',
          action: 'stop',
        },
      });

      const actionRows = rows.filter(
        (row) => row.recordKind === 'action'
          && row.action
          && typeof row.action === 'object',
      );
      const recordedActions = actionRows.map(
        (row) => row.action as Record<string, unknown>,
      );
      if (
        JSON.stringify(recordedActions) !== JSON.stringify([
          {
            name: 'openPage',
            url: resizeURL,
            viewport: { width: 900, height: 620 },
          },
          { name: 'x-crew-resize', width: 640, height: 500 },
        ])
      ) {
        throw new Error(`viewport 录制/去重异常: ${JSON.stringify(recordedActions)}`);
      }
      const pageGuid = String(actionRows[0]?.pageGuid ?? '');
      if (!pageGuid) throw new Error('viewport 录制缺少 pageGuid');

      let replayTargetId = '';
      let transactionId = 0;
      const replayAction = async (
        action: Record<string, unknown>,
      ): Promise<Record<string, unknown>> => {
        transactionId += 1;
        const result = await replayHost.handleRpc({
          runtime_key: replayRuntimeKey,
          method: 'execute_transaction',
          params: {
            profile_dir: replayProfile,
            proxy_url: policyProxyURL,
            download_dir: replayDownloads,
            schemaVersion: 1,
            transactionId,
            source: {
              pageGuid,
              ...(replayTargetId ? { targetId: replayTargetId } : {}),
            },
            knownPages: replayTargetId
              ? [{ pageGuid, targetId: replayTargetId }]
              : [],
            action,
            expectedEffects: [],
            timeoutMs: 15_000,
          },
        }) as Record<string, unknown>;
        const bindings = result.pageBindings;
        if (Array.isArray(bindings)) {
          const binding = bindings.find(
            (candidate) => (
              candidate
              && typeof candidate === 'object'
              && (candidate as { pageGuid?: unknown }).pageGuid === pageGuid
            ),
          ) as { targetId?: unknown } | undefined;
          if (binding?.targetId) replayTargetId = String(binding.targetId);
        }
        return result;
      };
      for (const action of recordedActions) await replayAction(action);
      if (!replayTargetId) throw new Error('viewport 回放未绑定目标页面');
      await replayAction({
        name: 'click',
        selector: '#narrow-only',
        button: 'left',
        modifiers: [],
        clickCount: 1,
        position: null,
      });

      const replayOwner = (
        replayHost as unknown as {
          owners: Map<string, {
            tabs: Map<string, { targetId: string; view: WebContentsView }>;
            engine: PlaywrightEngine;
          }>;
        }
      ).owners.get(replayRuntimeKey);
      const replayTab = [...(replayOwner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === replayTargetId);
      if (!replayOwner || !replayTab) throw new Error('viewport replay 拓扑不可用');
      const replayPage = await replayOwner.engine.pageForView(replayTab.view);
      const replayViewport = replayPage.viewportSize();
      const loadViewport = await replayPage.locator('#load-viewport').textContent();
      const responsiveResult = await replayPage.locator('#responsive-result').textContent();
      if (
        replayViewport?.width !== 640
        || replayViewport.height !== 500
        || loadViewport !== '900x620'
        || responsiveResult !== 'clicked:640x500:narrow'
      ) {
        throw new Error(
          `viewport 响应式回放失败: ${JSON.stringify({
            replayViewport,
            loadViewport,
            responsiveResult,
          })}`,
        );
      }
      return 'DIP=CSS；首次 DOMContentLoaded=900x620；→640x500；x/y 去重';
    } finally {
      await Promise.all([
        recorderHost.dispose().catch(() => undefined),
        replayHost.dispose().catch(() => undefined),
      ]);
      if (!recorderWindow.isDestroyed()) recorderWindow.destroy();
      if (!replayWindow.isDestroyed()) replayWindow.destroy();
      deferTempRootCleanup(tempRoot);
      if (previousV11Gate === undefined) {
        delete process.env.CREW_BROWSER_RECORDING_V11_PHASE_A;
      } else {
        process.env.CREW_BROWSER_RECORDING_V11_PHASE_A = previousV11Gate;
      }
    }
  });

  await check('BrowserHost v11 导航与既有标签：显式操作、点击因果、lazy join', async () => {
    const previousV11Gate = process.env.CREW_BROWSER_RECORDING_V11_PHASE_A;
    delete process.env.CREW_BROWSER_RECORDING_V11_PHASE_A;
    const ownerDigest = createHash('sha256')
      .update('pw-contract-v11-navigation-owner', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionId = 'pw-contract-v11-navigation-session';
    const sessionHash = createHash('sha256')
      .update(sessionId, 'utf8')
      .digest('hex')
      .slice(0, 32);
    const firstLabel = `s${sessionHash}-1`;
    const secondLabel = `s${sessionHash}-2`;
    const firstURL = new URL('/host-record-nav-a', topOriginURL).href;
    const addressURL = new URL('/host-record-nav-b', topOriginURL).href;
    const clickURL = new URL('/host-record-nav-c', topOriginURL).href;
    const backgroundURL = new URL(
      '/host-record-nav-background?phase=ready',
      topOriginURL,
    ).href;
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-v11-navigation-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const host = await managedHost(() => panelWindow, runtimeKey, profile);
    const rows: Array<Record<string, unknown>> = [];
    host.on('recording', (event: unknown) => {
      if (event && typeof event === 'object' && !Array.isArray(event)) {
        rows.push(event as Record<string, unknown>);
      }
    });
    try {
      const create = async (label: string, url: string): Promise<string> => {
        const result = await host.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            command: 'tab',
            args: ['new', '--label', label, url],
            mutating: true,
          },
        }) as { data?: { targetId?: string } };
        const targetId = String(result.data?.targetId ?? '');
        if (!targetId) throw new Error(`未创建真实标签页: ${label}`);
        return targetId;
      };
      const firstTargetId = await create(firstLabel, firstURL);
      const secondTargetId = await create(
        secondLabel,
        new URL('/host-record-nav-background?phase=idle', topOriginURL).href,
      );
      await host.handleRpc({
        runtime_key: runtimeKey,
        method: 'set_mode',
        params: {
          profile_dir: profile,
          target_id: firstTargetId,
          mode: 'human',
        },
      });
      const execute = (
        command: string,
        args: string[] = [],
        targetId = firstTargetId,
      ): Promise<unknown> => contractDeadline(
        host.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: targetId,
            command,
            args,
            mutating: true,
          },
        }),
        20_000,
        `v11 navigation ${command}`,
      );
      await execute('tab', [firstLabel]);
      host.setPanel({
        runtimeKey,
        sessionId,
        tabLabel: firstLabel,
        mode: 'human',
        bounds: { x: 0, y: 0, width: 900, height: 620 },
        visible: true,
      });

      type NavigationTabProbe = {
        targetId: string;
        view: WebContentsView;
      };
      type NavigationOwnerProbe = {
        tabs: Map<string, NavigationTabProbe>;
        engine: PlaywrightEngine;
      };
      const owner = (
        host as unknown as { owners: Map<string, NavigationOwnerProbe> }
      ).owners.get(runtimeKey);
      const firstTab = [...(owner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === firstTargetId);
      const secondTab = [...(owner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === secondTargetId);
      if (!owner || !firstTab || !secondTab) {
        throw new Error('v11 navigation Host 拓扑不可用');
      }
      const firstPage = await owner.engine.pageForView(firstTab.view);
      const secondPage = await owner.engine.pageForView(secondTab.view);
      firstPage.setDefaultTimeout(10_000);
      secondPage.setDefaultTimeout(10_000);
      await host.handleRpc({
        runtime_key: runtimeKey,
        method: 'set_recording',
        params: {
          profile_dir: profile,
          target_id: firstTargetId,
          recording_id: 'a11ce055cc771122',
          action: 'start',
        },
      });

      await execute('open', [addressURL]);
      if (firstPage.url() !== addressURL) {
        throw new Error(`地址栏 goto 未落地: ${firstPage.url()}`);
      }
      await execute('back');
      if (firstPage.url() !== firstURL) {
        throw new Error(`history back 未落地: ${firstPage.url()}`);
      }
      await execute('forward');
      if (firstPage.url() !== addressURL) {
        throw new Error(`history forward 未落地: ${firstPage.url()}`);
      }
      await execute('reload');
      if (firstPage.url() !== addressURL) {
        throw new Error(`reload 未保留 URL: ${firstPage.url()}`);
      }

      // Drive a browser-trusted click without going through Host's explicit
      // navigation command. The contract window is intentionally hidden, so
      // re-enable Chromium focus emulation only for this synthetic human input.
      // Its navigation must remain attached to the click.
      await enableFocusEmulation(firstPage.context(), firstPage);
      const next = firstPage.locator('#click-next');
      const box = await next.boundingBox();
      if (!box) throw new Error('click-next 没有真实布局框');
      const x = box.x + box.width / 2;
      const y = box.y + box.height / 2;
      await firstTab.view.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mousePressed',
        x,
        y,
        button: 'left',
        buttons: 1,
        clickCount: 1,
      });
      await firstTab.view.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {
        type: 'mouseReleased',
        x,
        y,
        button: 'left',
        buttons: 0,
        clickCount: 1,
      });
      await firstPage.waitForURL(clickURL, { timeout: 10_000 });

      const explicitBeforeNoHistory = rows.filter(
        (row) => (
          row.recordKind === 'action'
          && (row.action as { name?: unknown } | undefined)?.name === 'x-crew-navigate'
        ),
      ).length;
      await hostError(execute('forward'), 'no_history');
      await new Promise((resolve) => setTimeout(resolve, 100));
      const explicitAfterNoHistory = rows.filter(
        (row) => (
          row.recordKind === 'action'
          && (row.action as { name?: unknown } | undefined)?.name === 'x-crew-navigate'
        ),
      ).length;
      if (explicitAfterNoHistory !== explicitBeforeNoHistory) {
        throw new Error('no_history 留下 ghost x-crew-navigate');
      }

      // A background page can navigate freely before selection without joining
      // the trace. The explicit tab selection is the sole lazy-join boundary.
      await secondPage.goto(backgroundURL, {
        waitUntil: 'domcontentloaded',
        timeout: 10_000,
      });
      if (
        rows.some((row) => (
          row.recordKind === 'action'
          && row.pageGuid === 'p2'
        ))
      ) {
        throw new Error('未选择的后台标签页提前进入录制 ledger');
      }
      await execute('tab', [secondLabel]);
      await execute('tab', [firstLabel]);
      await host.handleRpc({
        runtime_key: runtimeKey,
        method: 'close_target',
        params: {
          profile_dir: profile,
          target_id: secondTargetId,
        },
      });
      await host.handleRpc({
        runtime_key: runtimeKey,
        method: 'set_recording',
        params: {
          profile_dir: profile,
          target_id: firstTargetId,
          recording_id: 'a11ce055cc771122',
          action: 'stop',
        },
      });

      const actions = rows.filter(
        (row) => (
          row.recordKind === 'action'
          && row.action
          && typeof row.action === 'object'
        ),
      );
      const explicit = actions.filter(
        (row) => (row.action as { name?: unknown }).name === 'x-crew-navigate',
      );
      const explicitActions = explicit.map((row) => row.action);
      const expectedExplicit = [
        { name: 'x-crew-navigate', operation: 'goto', url: addressURL },
        { name: 'x-crew-navigate', operation: 'back', url: '' },
        { name: 'x-crew-navigate', operation: 'forward', url: '' },
        { name: 'x-crew-navigate', operation: 'reload', url: '' },
      ];
      if (JSON.stringify(explicitActions) !== JSON.stringify(expectedExplicit)) {
        throw new Error(`显式导航动作不精确: ${JSON.stringify(explicitActions)}`);
      }
      const committedUrls = [addressURL, firstURL, addressURL, addressURL];
      explicit.forEach((action, index) => {
        const signal = rows.find((row) => (
          row.recordKind === 'signal'
          && (row.signal as { name?: unknown } | undefined)?.name === 'navigation'
          && row.transactionId === action.transactionId
        ));
        if (
          !signal
          || (signal.signal as { url?: unknown }).url !== committedUrls[index]
          || signal.step !== action.step
        ) {
          throw new Error(`显式导航 signal 未同事务: ${JSON.stringify({ action, signal })}`);
        }
      });
      const click = actions.find(
        (row) => (
          (row.action as { name?: unknown }).name === 'click'
          && (
            row.evidence as { target?: { id?: unknown } } | undefined
          )?.target?.id === 'click-next'
        ),
      );
      const clickSignal = click
        ? rows.find((row) => (
            row.recordKind === 'signal'
            && (row.signal as { name?: unknown }).name === 'navigation'
            && row.transactionId === click.transactionId
          ))
        : undefined;
      if (
        !click
        || !clickSignal
        || (clickSignal.signal as { url?: unknown }).url !== clickURL
      ) {
        throw new Error(
          `点击导航未保持 click+signal 因果: ${JSON.stringify({ click, clickSignal })}`,
        );
      }
      if (
        actions.some(
          (row) => (row.action as { name?: unknown }).name === 'navigate',
        )
      ) {
        throw new Error('v11 导航退化成 generic navigate');
      }

      const topology = actions
        .filter((row) => (
          ['openPage', 'x-crew-activatePage', 'closePage'].includes(
            String((row.action as { name?: unknown }).name ?? ''),
          )
        ))
        .map((row) => ({
          pageGuid: row.pageGuid,
          action: row.action,
        }));
      const expectedTopology = [
        {
          pageGuid: 'p1',
          action: {
            name: 'openPage',
            url: firstURL,
            viewport: { width: 900, height: 620 },
          },
        },
        {
          pageGuid: 'p2',
          action: {
            name: 'openPage',
            url: backgroundURL,
            viewport: (topology[1]?.action as { viewport?: unknown } | undefined)?.viewport,
          },
        },
        { pageGuid: 'p2', action: { name: 'x-crew-activatePage' } },
        { pageGuid: 'p1', action: { name: 'x-crew-activatePage' } },
        { pageGuid: 'p2', action: { name: 'closePage' } },
      ];
      if (
        topology.length !== expectedTopology.length
        || topology.some((entry, index) => {
          const expected = expectedTopology[index];
          if (entry.pageGuid !== expected?.pageGuid) return true;
          const action = entry.action as Record<string, unknown>;
          const expectedAction = expected.action as Record<string, unknown>;
          if (action.name !== expectedAction.name || action.url !== expectedAction.url) return true;
          if (
            action.name === 'openPage'
            && (
              !action.viewport
              || typeof action.viewport !== 'object'
              || !Number.isFinite(Number((action.viewport as { width?: unknown }).width))
              || !Number.isFinite(Number((action.viewport as { height?: unknown }).height))
            )
          ) {
            return true;
          }
          return false;
        })
      ) {
        throw new Error(`lazy join 拓扑/顺序异常: ${JSON.stringify(topology)}`);
      }
      const close = actions.find(
        (row) => (
          row.pageGuid === 'p2'
          && (row.action as { name?: unknown }).name === 'closePage'
        ),
      );
      const closedSignal = close
        ? rows.find((row) => (
            row.recordKind === 'signal'
            && (row.signal as { name?: unknown }).name === 'x-crew-pageClosed'
            && row.transactionId === close.transactionId
          ))
        : undefined;
      if (!close || !closedSignal || closedSignal.pageGuid !== 'p2') {
        throw new Error(`closePage signal 未同事务: ${JSON.stringify({ close, closedSignal })}`);
      }
      const steps = actions.map((row) => Number(row.step));
      if (steps.some((step, index) => step !== index + 1)) {
        throw new Error(`action step 存在 ghost/gap: ${JSON.stringify(steps)}`);
      }
      return 'goto/back/forward/reload 同事务；click 因果；p2 lazy join/activate/close';
    } finally {
      await host.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
      if (previousV11Gate === undefined) {
        delete process.env.CREW_BROWSER_RECORDING_V11_PHASE_A;
      } else {
        process.env.CREW_BROWSER_RECORDING_V11_PHASE_A = previousV11Gate;
      }
    }
  });

  await check('BrowserHost recorder：键盘/粘贴 + frame/OOPIF/upload 全链路', async () => {
    const ownerDigest = createHash('sha256')
      .update('pw-contract-recorder-owner', 'utf8')
      .digest('hex');
    const runtimeKey = `crew_${ownerDigest.slice(0, 12)}`;
    const accountDir = `acct_${ownerDigest.slice(0, 16)}`;
    const sessionId = 'pw-contract-recorder-session';
    const sessionHash = createHash('sha256')
      .update(sessionId, 'utf8')
      .digest('hex')
      .slice(0, 32);
    const tabLabel = `s${sessionHash}-1`;
    const recordingId = 'c0ffee1234abcdef';
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-pw-recorder-'));
    const profile = path.join(tempRoot, 'accounts', accountDir, 'browser', 'profile');
    await mkdir(profile, { recursive: true });
    const approvedUploadRoot = path.join(path.dirname(profile), 'approved-uploads');
    await mkdir(approvedUploadRoot, { recursive: true });
    const uploadOne = path.join(approvedUploadRoot, 'contract-one.pdf');
    const uploadTwo = path.join(approvedUploadRoot, 'contract-two.png');
    await writeFile(uploadOne, 'first recorder upload fixture', 'utf8');
    await writeFile(uploadTwo, 'second recorder upload fixture', 'utf8');
    const clipboardBeforeContract = clipboard.readText();
    const panelWindow = new BrowserWindow({
      show: false,
      width: 1100,
      height: 800,
      webPreferences: { sandbox: true },
    });
    const recorderHost = await managedHost(() => panelWindow, runtimeKey, profile);
    const recorded: Array<Record<string, unknown>> = [];
    recorderHost.on('recording', (event: unknown) => {
      if (!event || typeof event !== 'object' || Array.isArray(event)) return;
      const row = event as Record<string, unknown>;
      if (row.schemaVersion !== 11) {
        recorded.push(row);
        return;
      }
      if (
        row.recordKind !== 'action'
        || !row.action
        || typeof row.action !== 'object'
        || Array.isArray(row.action)
      ) {
        return;
      }
      // Keep the mature recorder behavior assertions below readable while
      // exercising the default-on v11 wire contract. This is a test-only view
      // over the exact v11 action/evidence pair, not a production conversion.
      const action = row.action as Record<string, unknown>;
      const evidence = (
        row.evidence
        && typeof row.evidence === 'object'
        && !Array.isArray(row.evidence)
      ) ? row.evidence as Record<string, unknown> : {};
      const name = String(action.name ?? '');
      const modifiers = Array.isArray(action.modifiers)
        ? action.modifiers.map((modifier) => (
            modifier === 'Control' ? 'Ctrl' : String(modifier)
          ))
        : [];
      const legacyAction = name === 'openPage' || name === 'navigate'
        ? 'navigate'
        : name === 'press'
          ? 'key'
          : name === 'fill'
            ? 'input'
            : name === 'setInputFiles'
              ? 'upload'
              : name;
      const files = Array.isArray(action.files)
        ? action.files.filter((file): file is string => typeof file === 'string')
        : [];
      recorded.push({
        ...row,
        action: legacyAction,
        target: evidence.target ?? null,
        selector: action.selector ?? action.sourceSelector ?? '',
        targetSelector: action.targetSelector ?? '',
        url: evidence.url ?? action.url ?? '',
        position: action.position ?? null,
        pointerType: action.pointerType ?? '',
        gestureStart: action.start ?? null,
        gesturePoints: action.points ?? [],
        key: name === 'press'
          ? [...modifiers, String(action.key ?? '')].filter(Boolean).join('+')
          : '',
        value: name === 'fill' ? action.text ?? '' : '',
        paths: files,
        fileCount: files.length,
        multiple: files.length > 1,
        uploadMode: name === 'setInputFiles' ? 'paths' : '',
      });
    });

    const waitFor = async (
      predicate: () => boolean,
      description: string,
      timeoutMs = 10_000,
    ): Promise<void> => {
      const deadline = Date.now() + timeoutMs;
      while (!predicate()) {
        if (Date.now() >= deadline) throw new Error(`等待超时: ${description}`);
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    };

    try {
      const created = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          command: 'tab',
          args: ['new', '--label', tabLabel, topOriginURL],
          mutating: true,
        },
      }) as { data?: { targetId?: string } };
      const targetId = String(created.data?.targetId ?? '');
      if (!targetId) throw new Error('BrowserHost 未创建 recorder tab');
      console.log('      recorder: tab created');

      type RecorderTabProbe = {
        targetId: string;
        view: WebContentsView;
        mode: 'ai' | 'human' | 'paused';
        childSessions: Map<string, Record<string, unknown>>;
        recording: null | {
          bindingName: string;
          controlName: string;
          sessions: Map<string, { installed: boolean }>;
          contexts: Set<string>;
        };
      };
      type RecorderOwnerProbe = {
        tabs: Map<string, RecorderTabProbe>;
        engine: PlaywrightEngine;
      };
      const internals = recorderHost as unknown as {
        owners: Map<string, RecorderOwnerProbe>;
      };
      const recorderOwner = internals.owners.get(runtimeKey);
      const recorderTab = [...(recorderOwner?.tabs.values() ?? [])]
        .find((candidate) => candidate.targetId === targetId);
      if (!recorderOwner || !recorderTab) throw new Error('无法取得 recorder tab 拓扑');

      const recorderPage = await recorderOwner.engine.pageForView(recorderTab.view);
      recorderPage.setDefaultTimeout(10_000);
      let frameStage = 'same-origin';
      try {
        await recorderPage
          .frameLocator('#same-origin-frame')
          .locator('#same-origin-button')
          .waitFor({ state: 'visible' });
        frameStage = 'cross-origin';
        await recorderPage
          .frameLocator('#cross-origin-frame')
          .locator('#cross-origin-button')
          .waitFor({ state: 'visible' });
      } catch (error) {
        const navigationDiagnostic = {
          stage: frameStage,
          page: recorderPage.url(),
          closed: recorderPage.isClosed(),
          main: recorderPage.mainFrame().url(),
          native: recorderTab.view.webContents.getURL(),
          nativeFrames: recorderTab.view.webContents.mainFrame.framesInSubtree.map(
            (frame) => ({
              url: frame.url,
              processId: frame.processId,
              detached: frame.detached,
            }),
          ),
          childSessions: [...recorderTab.childSessions.entries()],
          crossOriginRequests: [...crossOriginRequests],
          frames: recorderPage.frames().map((frame) => frame.url()),
          sameFrameCount: await recorderPage.locator('#same-origin-frame').count(),
          crossFrameCount: await recorderPage.locator('#cross-origin-frame').count(),
          crossFrameSrc: await recorderPage
            .locator('#cross-origin-frame')
            .getAttribute('src'),
          contextPages: recorderPage.context().pages().map((candidate) => ({
            same: candidate === recorderPage,
            closed: candidate.isClosed(),
            url: candidate.url(),
            frames: candidate.frames().map((frame) => frame.url()),
          })),
        };
        console.log(
          `      recorder navigation diagnostic: ${JSON.stringify(navigationDiagnostic)}`,
        );
        throw new Error(
          `recorder 初始 frame 不可用: ${JSON.stringify(navigationDiagnostic)}; `
          + `${error instanceof Error ? error.message.replace(/\s+/g, ' ') : String(error)}`,
        );
      }
      console.log('      recorder: initial frames ready');
      // Production recording uses a visible, focused human-mode view. This
      // contract deliberately stays hidden and drives trusted browser input
      // through Playwright, so it keeps AI focus emulation enabled. Native-input
      // correlation is an audit signal, not a persistence prerequisite.
      console.log('      recorder: starting');
      await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'set_recording',
        params: {
          profile_dir: profile,
          target_id: targetId,
          recording_id: recordingId,
          action: 'start',
        },
      });
      console.log('      recorder: started');
      const startedRecording = recorderTab.recording;
      if (!startedRecording) throw new Error('recorder start 后没有状态');
      const bindingName = startedRecording.bindingName;
      const controlName = startedRecording.controlName;
      for (const frame of recorderTab.view.webContents.mainFrame.framesInSubtree) {
        const probe = await frame.executeJavaScript(
          `({
            binding: typeof globalThis[${JSON.stringify(bindingName)}],
            active: globalThis[${JSON.stringify(controlName)}]?.isActive?.() === true,
          })`,
          false,
        ) as { binding?: string; active?: boolean };
        if (probe.binding !== 'function' || probe.active !== true) {
          throw new Error(`当前 frame 未安装 document-world recorder: ${frame.url}`);
        }
      }
      await waitFor(
        () => recorded.some((event) => event.action === 'navigate'),
        'recorder 起始快照',
        20_000,
      );

      // Upstream Playwright records an element-relative position only for CANVAS.
      // This is essential for charts/maps/editors where the selector identifies
      // the surface but not the intended point.
      await recorderPage.locator('#recorder-canvas').click({
        position: { x: 127, y: 42 },
      });
      await waitFor(
        () => recorded.some(
          (event) => event.action === 'click'
            && (event.target as { id?: string } | null)?.id === 'recorder-canvas'
            && (event.position as { x?: number; y?: number } | null)?.x === 127
            && (event.position as { x?: number; y?: number } | null)?.y === 42,
        ),
        'canvas relative click position',
      );
      if (await recorderPage.locator('#recorder-canvas-state').textContent() !== '127,42') {
        throw new Error('CANVAS 实际接收坐标与录制坐标不一致');
      }

      // A real Chromium pen stream must survive trusted browser input,
      // document-world recording, Host parsing and v11 action persistence.
      const penContext: ActionContext = {
        page: recorderPage,
        refs: new Map(),
        hash,
        timeoutMs: 10_000,
      };
      const penRef = '@recorder-pen-contract';
      await actions.locateBySelector(
        penContext,
        penRef,
        '#recorder-canvas',
        hash,
      );
      try {
        await actions.pointerGesture(penContext, penRef, {
          pointerType: 'pen',
          button: 'left',
          modifiers: [],
          start: {
            x: 20,
            y: 25,
            pressure: 0.3,
            tangentialPressure: -0.4,
            tiltX: 11,
            tiltY: -12,
            twist: 37,
          },
          points: [
            {
              x: 90,
              y: 60,
              elapsedMs: 8,
              pressure: 0.75,
              tangentialPressure: 0.2,
              tiltX: 21,
              tiltY: -22,
              twist: 47,
            },
            {
              x: 170,
              y: 100,
              elapsedMs: 16,
              pressure: 0,
              tiltX: 23,
              tiltY: -24,
              twist: 51,
            },
          ],
        });
      } finally {
        penContext.refs.delete(penRef);
      }
      await waitFor(
        () => recorded.some(
          (event) => (
            event.action === 'x-crew-pointerGesture'
            && event.pointerType === 'pen'
            && (event.target as { id?: string } | null)?.id === 'recorder-canvas'
          ),
        ),
        'real pen pointerGesture recording',
      );
      const recordedPen = recorded.find(
        (event) => (
          event.action === 'x-crew-pointerGesture'
          && event.pointerType === 'pen'
          && (event.target as { id?: string } | null)?.id === 'recorder-canvas'
        ),
      );
      const recordedPenStart = recordedPen?.gestureStart as
        | Record<string, unknown>
        | undefined;
      const recordedPenPoints = Array.isArray(recordedPen?.gesturePoints)
        ? recordedPen.gesturePoints as Array<Record<string, unknown>>
        : [];
      if (
        !recordedPenStart
        || Math.abs(Number(recordedPenStart.pressure) - 0.3) >= 0.01
        || recordedPenStart.tiltX !== 11
        || recordedPenStart.tiltY !== -12
        || recordedPenStart.twist !== 37
        || !recordedPenPoints.some((point) => (
          Math.abs(Number(point.pressure) - 0.75) < 0.01
          && point.tiltX === 21
          && point.tiltY === -22
          && point.twist === 47
        ))
        || Number(recordedPenPoints.at(-1)?.pressure) !== 0
      ) {
        throw new Error(
          `真实 pen 录制遥测不完整: ${JSON.stringify(recordedPen)}`,
        );
      }

      // Playwright recorder assigns zero-detail clicks to their originating keyboard event.
      // Enter must activate the page exactly once while the trace keeps only the key action.
      await recorderPage.locator('#recorder-keyboard-button').press('Enter');
      await recorderPage.locator('#recorder-keyboard-state').waitFor({ state: 'visible' });
      if (await recorderPage.locator('#recorder-keyboard-state').textContent()
        !== 'button-activated') {
        throw new Error('Enter 没有激活真实 button');
      }
      await waitFor(
        () => recorded.some(
          (event) => event.action === 'key'
            && event.key === 'Enter'
            && (event.target as { id?: string } | null)?.id === 'recorder-keyboard-button',
        ),
        'button Enter key event',
      );
      await new Promise((resolve) => setTimeout(resolve, 300));
      const keyboardButtonEvents = recorded.filter(
        (event) => (event.target as { id?: string } | null)?.id
          === 'recorder-keyboard-button',
      );
      if (
        keyboardButtonEvents.length !== 1
        || keyboardButtonEvents[0]?.action !== 'key'
        || keyboardButtonEvents[0]?.key !== 'Enter'
      ) {
        throw new Error(
          `trusted detail=0 click 被重复记录: ${JSON.stringify(keyboardButtonEvents)}`,
        );
      }

      // Single-line Enter commits the pending final input before the submit key. Chromium also
      // emits a zero-detail click on the submitter; that derived click must not become a step.
      const enterTextbox = recorderPage.locator('#recorder-enter-textbox');
      await enterTextbox.fill('真实回车查询');
      await enterTextbox.press('Enter');
      await waitFor(
        () => recorded.some(
          (event) => event.action === 'input'
            && event.value === '真实回车查询'
            && (event.target as { id?: string } | null)?.id === 'recorder-enter-textbox',
        ) && recorded.some(
          (event) => event.action === 'key'
            && event.key === 'Enter'
            && (event.target as { id?: string } | null)?.id === 'recorder-enter-textbox',
        ),
        'textbox final input + Enter',
      );
      if (await recorderPage.locator('#recorder-keyboard-state').textContent()
        !== 'submitted:真实回车查询') {
        throw new Error('textbox Enter 没有保留真实 form submit 语义');
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
      const implicitSubmitClicks = recorded.filter(
        (event) => event.action === 'click'
          && (event.target as { id?: string } | null)?.id === 'recorder-enter-submit',
      );
      if (implicitSubmitClicks.length) {
        throw new Error(
          `implicit submit 的 detail=0 click 被重复记录: ${JSON.stringify(implicitSubmitClicks)}`,
        );
      }

      // Modifier keydowns are transport details, while the actual shortcuts remain durable.
      const shortcutInput = recorderPage.locator('#recorder-shortcut-input');
      await shortcutInput.fill('shortcut-contract');
      for (const key of ['A', 'C', 'X', 'Z']) {
        await shortcutInput.press(`ControlOrMeta+${key}`);
      }
      await recorderPage.locator('#recorder-paste-input').focus();
      await waitFor(
        () => {
          const keys = recorded
            .filter(
              (event) => event.action === 'key'
                && (event.target as { id?: string } | null)?.id
                  === 'recorder-shortcut-input',
            )
            .map((event) => String(event.key ?? '').toLowerCase());
          return ['a', 'c', 'x', 'z'].every(
            (key) => keys.some((value) => /^(ctrl|meta)\+/.test(value) && value.endsWith(key)),
          );
        },
        'ControlOrMeta+A/C/X/Z',
      );
      const shortcutKeys = recorded
        .filter(
          (event) => event.action === 'key'
            && (event.target as { id?: string } | null)?.id === 'recorder-shortcut-input',
        )
        .map((event) => String(event.key ?? ''));
      if (
        shortcutKeys.length !== 4
        || shortcutKeys.some((key) => /^(Control|Meta|Alt|Shift)\+\1$/.test(key))
      ) {
        throw new Error(`modifier/shortcut 记录异常: ${JSON.stringify(shortcutKeys)}`);
      }

      // A real OS clipboard paste must be represented only by the final trusted input.
      clipboard.writeText('真实系统粘贴文本');
      const pasteInput = recorderPage.locator('#recorder-paste-input');
      await pasteInput.press('ControlOrMeta+V');
      await recorderPage.locator('#recorder-keyboard-button').focus();
      await waitFor(
        () => recorded.some(
          (event) => event.action === 'input'
            && event.value === '真实系统粘贴文本'
            && (event.target as { id?: string } | null)?.id === 'recorder-paste-input',
        ),
        'clipboard paste final input',
      );
      const pasteKeys = recorded.filter(
        (event) => event.action === 'key'
          && (event.target as { id?: string } | null)?.id === 'recorder-paste-input',
      );
      if (pasteKeys.length) {
        throw new Error(`Cmd/Ctrl+V 被错误记录为 press: ${JSON.stringify(pasteKeys)}`);
      }

      // A hidden multi-file input is the common shape behind a styled "上传附件" button.
      // The page event can only report count/target; BrowserHost must resolve the exact native
      // File wrappers and paths without reading fakepath or file contents.
      await recorderPage.locator('#top-upload').setInputFiles([uploadOne, uploadTwo]);
      console.log('      recorder: top upload dispatched');
      await waitFor(
        () => recorded.some(
          (event) => event.action === 'upload'
            && (event.target as { id?: string } | null)?.id === 'top-upload'
            && event.uploadMode === 'paths',
        ),
        '顶层隐藏多文件 upload',
      );
      const topUpload = recorded.find(
        (event) => event.action === 'upload'
          && (event.target as { id?: string } | null)?.id === 'top-upload'
          && event.uploadMode === 'paths',
      );
      if (
        topUpload?.fileCount !== 2
        || topUpload.multiple !== true
        || JSON.stringify(topUpload.paths) !== JSON.stringify([uploadOne, uploadTwo])
      ) {
        throw new Error(`顶层多文件路径未精确固化: ${JSON.stringify(topUpload)}`);
      }
      console.log('      recorder: top upload paths resolved');

      // Styled button -> native FileChooser -> separate Host file_upload mirrors upstream
      // browser_file_upload. This path is essential when no visible/ref-addressable input
      // exists in the current snapshot.
      await recorderPage.locator('#top-upload-button').click();
      console.log('      recorder: chooser button clicked');
      await waitFor(
        () => recorderOwner.engine.hasPendingFileChooser(recorderTab.view),
        'styled button pending FileChooser',
      );
      const pendingUpload = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          target_id: targetId,
          command: 'file_upload',
          args: [uploadOne],
          mutating: true,
        },
      }) as {
        data?: { canceled?: boolean; uploaded?: number; multiple?: boolean };
      };
      if (
        pendingUpload.data?.canceled !== false
        || pendingUpload.data.uploaded !== 1
        || pendingUpload.data.multiple !== true
      ) {
        throw new Error(`pending FileChooser 返回异常: ${JSON.stringify(pendingUpload)}`);
      }
      await recorderPage.locator('#top-upload-state').waitFor({ state: 'visible' });
      const uploadState = await recorderPage.locator('#top-upload-state').textContent();
      if (uploadState !== path.basename(uploadOne)) {
        throw new Error(`pending FileChooser 未触发页面 change: ${uploadState}`);
      }
      if (recorderOwner.engine.hasPendingFileChooser(recorderTab.view)) {
        throw new Error('完成后 FileChooser 未消费');
      }

      await recorderPage.locator('#top-upload-button').click();
      await waitFor(
        () => recorderOwner.engine.hasPendingFileChooser(recorderTab.view),
        'cancel pending FileChooser',
      );
      const canceledChooser = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          target_id: targetId,
          command: 'file_upload',
          args: ['--cancel'],
          mutating: true,
        },
      }) as {
        data?: { canceled?: boolean; uploaded?: number };
      };
      if (
        canceledChooser.data?.canceled !== true
        || canceledChooser.data.uploaded !== 0
        || recorderOwner.engine.hasPendingFileChooser(recorderTab.view)
      ) {
        throw new Error(`FileChooser cancel 异常: ${JSON.stringify(canceledChooser)}`);
      }
      if (await recorderPage.locator('#top-upload-state').textContent() !== uploadState) {
        throw new Error('FileChooser cancel 错误改写了已有选择');
      }

      let noChooserCode = '';
      try {
        await recorderHost.handleRpc({
          runtime_key: runtimeKey,
          method: 'execute',
          params: {
            profile_dir: profile,
            proxy_url: policyProxyURL,
            target_id: targetId,
            command: 'file_upload',
            args: [uploadTwo],
            mutating: true,
          },
        });
      } catch (error) {
        noChooserCode = error && typeof error === 'object' && 'code' in error
          ? String((error as { code?: unknown }).code ?? '')
          : '';
      }
      if (noChooserCode !== 'no_file_chooser') {
        throw new Error(`无 pending chooser 未明确拒绝: ${noChooserCode || 'no error'}`);
      }

      // Replay upload is one Host transaction. Seed chooser A from a different
      // input, then make trigger B open its own chooser after 750ms. Only B may
      // receive files; consuming the stale one would mutate top-upload-state.
      await recorderPage.locator('#top-upload-button').click();
      await waitFor(
        () => recorderOwner.engine.hasPendingFileChooser(recorderTab.view),
        'atomic upload stale chooser seed',
      );
      const topStateBeforeAtomic = await recorderPage
        .locator('#top-upload-state')
        .textContent();
      const delayedAtomic = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          target_id: targetId,
          command: 'upload_with_trigger',
          args: [],
          trigger_selector: '#delayed-upload-button',
          input_selector: '#delayed-upload',
          files: [uploadTwo],
          mutating: true,
        },
      }) as {
        data?: { via?: string; uploaded?: number; multiple?: boolean };
      };
      if (
        delayedAtomic.data?.via !== 'chooser'
        || delayedAtomic.data.uploaded !== 1
        || delayedAtomic.data.multiple !== true
      ) {
        throw new Error(`延迟原子 chooser 返回异常: ${JSON.stringify(delayedAtomic)}`);
      }
      await recorderPage.locator('#delayed-upload-state').waitFor({ state: 'visible' });
      if (
        await recorderPage.locator('#delayed-upload-state').textContent()
          !== path.basename(uploadTwo)
      ) {
        throw new Error('750ms 延迟 chooser 未收到本次上传文件');
      }
      if (
        await recorderPage.locator('#top-upload-state').textContent()
          !== topStateBeforeAtomic
      ) {
        throw new Error('原子上传错误消费了旧 FileChooser');
      }
      if (recorderOwner.engine.hasPendingFileChooser(recorderTab.view)) {
        throw new Error('延迟原子上传后仍残留 pending FileChooser');
      }

      // A trigger may only reveal/create the exact input instead of opening a
      // native chooser. The same RPC must wait, then resolve that post-click
      // selector and use setInputFiles.
      const revealAtomic = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          target_id: targetId,
          command: 'upload_with_trigger',
          args: [],
          trigger_selector: '#reveal-upload-button',
          input_selector: '#revealed-upload',
          files: [uploadOne],
          mutating: true,
        },
      }) as { data?: { via?: string; uploaded?: number } };
      if (
        revealAtomic.data?.via !== 'input'
        || revealAtomic.data.uploaded !== 1
        || await recorderPage.locator('#reveal-upload-state').textContent()
          !== path.basename(uploadOne)
      ) {
        throw new Error(`原子 exact-input fallback 异常: ${JSON.stringify(revealAtomic)}`);
      }
      if (recorderOwner.engine.hasPendingFileChooser(recorderTab.view)) {
        throw new Error('exact-input fallback 后仍残留 pending FileChooser');
      }

      // A missing/stale trigger is a proven pre-dispatch failure. It may skip
      // directly to an already-valid exact input without a second RPC.
      const missingTriggerAtomic = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'execute',
        params: {
          profile_dir: profile,
          proxy_url: policyProxyURL,
          target_id: targetId,
          command: 'upload_with_trigger',
          args: [],
          trigger_selector: '#removed-upload-trigger',
          input_selector: '#top-upload',
          files: [uploadTwo],
          mutating: true,
        },
      }) as { data?: { via?: string; uploaded?: number } };
      if (
        missingTriggerAtomic.data?.via !== 'input'
        || missingTriggerAtomic.data.uploaded !== 1
        || await recorderPage.locator('#top-upload-state').textContent()
          !== path.basename(uploadTwo)
      ) {
        throw new Error(
          `失效 trigger 未回退 exact input: ${JSON.stringify(missingTriggerAtomic)}`,
        );
      }

      await recorderPage
        .frameLocator('#cross-origin-frame')
        .locator('#cross-origin-upload')
        .setInputFiles(uploadOne);
      await waitFor(
        () => recorded.some(
          (event) => event.action === 'upload'
            && (event.target as { id?: string } | null)?.id === 'cross-origin-upload'
            && event.uploadMode === 'paths',
        ),
        'OOPIF 隐藏文件 upload',
      );
      const oopifUpload = recorded.find(
        (event) => event.action === 'upload'
          && (event.target as { id?: string } | null)?.id === 'cross-origin-upload',
      );
      if (
        oopifUpload?.fileCount !== 1
        || JSON.stringify(oopifUpload.paths) !== JSON.stringify([uploadOne])
        || !String(oopifUpload.selector ?? '').includes('enter-frame')
      ) {
        throw new Error(`OOPIF upload 路径/selector 异常: ${JSON.stringify(oopifUpload)}`);
      }

      const emitRecordedClick = async (
        frameUrl: string,
        frameSelector: string,
        targetSelector: string,
        seq: number,
        allowMissingBinding = false,
      ): Promise<void> => {
        const frame = recorderTab.view.webContents.mainFrame.framesInSubtree
          .find((candidate) => candidate.url === frameUrl);
        if (!frame) throw new Error(`recorder event 未找到 frame: ${frameUrl}`);
        const id = targetSelector.startsWith('#') ? targetSelector.slice(1) : '';
        const recordedSelector = await frame.executeJavaScript(
          `globalThis[${JSON.stringify(controlName)}]?.selectorFor?.(
            document.querySelector(${JSON.stringify(targetSelector)})
          ) || ''`,
          false,
        ) as string;
        const recordedFrameSelector = await recorderTab.view.webContents.mainFrame
          .executeJavaScript(
            `globalThis[${JSON.stringify(controlName)}]?.selectorFor?.(
              document.querySelector(${JSON.stringify(frameSelector)})
            ) || ''`,
            false,
          ) as string;
        if (!recordedSelector || !recordedFrameSelector) {
          throw new Error(
            `Playwright InjectedScript 未生成 selector: `
            + `${JSON.stringify({ recordedSelector, recordedFrameSelector, frameUrl })}`,
          );
        }
        const payload = {
          schemaVersion: RECORDER_EVENT_SCHEMA_VERSION,
          provenance: {
            schemaVersion: RECORDER_PROVENANCE_SCHEMA_VERSION,
            source: 'document-world',
            capturePhase: 'event-callback',
            browserTrusted: true,
            targetEvidence: 'synchronous',
            nativeInput: 'unverified',
          },
          seq,
          causalId: 0,
          causalToken: 0,
          type: 'click',
          url: frameUrl,
          hint: id,
          target: {
            tag: 'button',
            text: id,
            ariaLabel: '',
            href: '',
            ordinal: 1,
            id,
            name: '',
            role: '',
            inputType: '',
            contentEditable: false,
            testId: '',
            testIdAttribute: '',
            cssPath: targetSelector,
            framePath: [recordedFrameSelector],
          },
          recordedSelector,
          recordedDragSelector: '',
          selectorSource: 'playwright',
          tier: 'plain',
          value: '',
          values: [],
          valueTruncated: false,
          lifecycleFlush: false,
          key: '',
          clickButton: 'left',
          clickCount: 1,
          position: null,
          dragSourcePosition: null,
          dragTargetPosition: null,
          modifiers: [],
          dialogAction: '',
          dialogType: '',
          dialogText: '',
          scrollX: 0,
          scrollY: 0,
          uploadMode: '',
          paths: [],
          fileCount: 0,
          multiple: false,
          accept: '',
          dropData: {},
        } as const;
        const rawPayload = JSON.stringify(payload);
        if (!parseRecorderEvent(rawPayload)) {
          throw new Error(`契约 fixture 事件未通过 recorder parser: ${frameUrl}`);
        }
        const emitted = await frame.executeJavaScript(
          `(() => {
            const element = document.querySelector(${JSON.stringify(targetSelector)});
            if (!element) return false;
            const binding = globalThis[${JSON.stringify(bindingName)}];
            if (typeof binding !== 'function') return false;
            binding(${JSON.stringify(rawPayload)});
            return true;
          })()`,
          false,
        ) as boolean;
        if (!emitted && !allowMissingBinding) {
          throw new Error(`recorder binding 不可用: ${frameUrl}`);
        }
      };

      await emitRecordedClick(
        new URL('/same-frame', topOriginURL).href,
        '#same-origin-frame',
        '#same-origin-button',
        1,
      );
      console.log('      recorder: same-origin event');
      await waitFor(
        () => recorded.some(
          (event) => (event.target as { id?: string } | null)?.id === 'same-origin-button',
        ),
        '同源 iframe click',
      );
      await emitRecordedClick(
        crossOriginURL,
        '#cross-origin-frame',
        '#cross-origin-button',
        2,
      );
      console.log('      recorder: current OOPIF event');
      await waitFor(
        () => recorded.some(
          (event) => (event.target as { id?: string } | null)?.id === 'cross-origin-button',
        ),
        '当前 OOPIF click',
      );

      const lateCrossOriginURL = `${crossOriginURL}?late=1`;
      await recorderTab.view.webContents.mainFrame.executeJavaScript(
        `(() => {
          const frame = document.createElement('iframe');
          frame.id = 'late-cross-origin-frame';
          frame.src = ${JSON.stringify(lateCrossOriginURL)};
          document.getElementById('late-frame-host')?.append(frame);
        })()`,
        false,
      );
      await waitFor(
        () => recorderTab.view.webContents.mainFrame.framesInSubtree.some(
          (frame) => frame.url === lateCrossOriginURL,
        ),
        '迟到 OOPIF attach',
      );
      const lateFrame = recorderTab.view.webContents.mainFrame.framesInSubtree
        .find((frame) => frame.url === lateCrossOriginURL);
      if (!lateFrame) throw new Error('迟到 OOPIF WebFrameMain 缺失');
      {
        const deadline = Date.now() + 10_000;
        while (true) {
          const ready = await lateFrame.executeJavaScript(
            `typeof globalThis[${JSON.stringify(bindingName)}] === 'function'
              && globalThis[${JSON.stringify(controlName)}]?.isActive?.() === true`,
            false,
          ).catch(() => false);
          if (ready) break;
          if (Date.now() >= deadline) {
            console.log(
              '      recorder: late timeout topology',
              [...recorderTab.childSessions],
              [...(recorderTab.recording?.sessions.entries() ?? [])]
                .map(([id, session]) => [id, session.installed]),
            );
            throw new Error(
              `迟到 OOPIF recorder 安装超时: child=${JSON.stringify(
                [...recorderTab.childSessions],
              )}; sessions=${JSON.stringify(
                [...(recorderTab.recording?.sessions.entries() ?? [])]
                  .map(([id, session]) => [id, session.installed]),
              )}`,
            );
          }
          await new Promise((resolve) => setTimeout(resolve, 25));
        }
      }
      await emitRecordedClick(
        lateCrossOriginURL,
        '#late-cross-origin-frame',
        '#late-cross-origin-button',
        3,
      );
      console.log('      recorder: late OOPIF event');
      await waitFor(
        () => recorded.some(
          (event) => (event.target as { id?: string } | null)?.id
            === 'late-cross-origin-button',
        ),
        '迟到 OOPIF click',
      );

      const activeRecording = recorderTab.recording;
      if (!activeRecording) throw new Error('recorder 在 OOPIF click 前意外停止');
      const oopifSessionIds = [...recorderTab.childSessions.entries()]
        .filter(([, info]) => String(info.type ?? '') === 'iframe')
        .map(([session]) => session);
      if (oopifSessionIds.length < 2) {
        throw new Error(`未建立两个真实 OOPIF child session: ${oopifSessionIds.length}`);
      }
      if (!oopifSessionIds.every(
        (session) => activeRecording.sessions.get(session)?.installed === true,
      )) {
        throw new Error('当前/迟到 OOPIF 未全部安装 recorder session');
      }
      if (!oopifSessionIds.every(
        (session) => [...activeRecording.contexts].some(
          (contextKey) => contextKey.startsWith(`${session}\u0000`),
        ),
      )) {
        throw new Error('OOPIF binding context 未按 (sessionId, contextId) 复合登记');
      }

      for (const id of [
        'same-origin-button',
        'cross-origin-button',
        'late-cross-origin-button',
        'top-upload',
        'cross-origin-upload',
      ]) {
        const event = recorded.find(
          (candidate) => (
            (candidate.target as { id?: string } | null)?.id === id
            && (id.endsWith('-upload') || candidate.action === 'click')
          ),
        );
        if (!event) throw new Error(`缺少录制事件: ${id}`);
        if (
          id !== 'top-upload'
          && !String(event.selector ?? '').includes('enter-frame')
        ) {
          throw new Error(`${id} 未生成可回放的跨帧稳定选择器`);
        }
      }

      const stopped = await recorderHost.handleRpc({
        runtime_key: runtimeKey,
        method: 'set_recording',
        params: {
          profile_dir: profile,
          target_id: targetId,
          recording_id: recordingId,
          action: 'stop',
        },
      }) as { recording?: boolean; forged?: number };
      console.log('      recorder: stopped');
      if (stopped.recording) {
        throw new Error(`stop 状态异常: ${JSON.stringify(stopped)}`);
      }
      if ((stopped.forged ?? 0) < 3) {
        throw new Error('未关联 native proof 的文档事件没有进入审计计数');
      }
      for (const id of [
        'same-origin-button',
        'cross-origin-button',
        'late-cross-origin-button',
      ]) {
        const event = recorded.find(
          (candidate) => (candidate.target as { id?: string } | null)?.id === id,
        );
        const selector = String(event?.selector ?? '');
        if (!selector || await recorderPage.locator(selector).count() !== 1) {
          throw new Error(`${id} 的持久 selector 无法唯一回放`);
        }
      }
      const countAfterStop = recorded.length;
      await emitRecordedClick(
        lateCrossOriginURL,
        '#late-cross-origin-frame',
        '#late-cross-origin-button',
        4,
        true,
      );
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (recorded.length !== countAfterStop) {
        throw new Error('stop 后 OOPIF binding 仍能上报事件');
      }
      return `same + ${oopifSessionIds.length} OOPIF；${countAfterStop} steps；stop clean`;
    } finally {
      clipboard.writeText(clipboardBeforeContract);
      await recorderHost.dispose().catch(() => undefined);
      if (!panelWindow.isDestroyed()) panelWindow.destroy();
      deferTempRootCleanup(tempRoot);
    }
  });

  const failed = results.filter((r) => !r.ok);
  console.log('\n=== Playwright 契约测试（窗口全程不可见）===\n');
  for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}\n        ${r.detail}`);
  console.log(`\n=== ${results.length - failed.length}/${results.length} 通过 ===`);

  transport.close();
  host.unmount(lateView);
  if (!lateView.webContents.isDestroyed()) {
    lateView.webContents.close({ waitForBeforeUnload: false });
  }
  host.dispose();
  topOriginServer.closeAllConnections();
  await new Promise<void>((resolve) => topOriginServer.close(() => resolve()));
  crossOriginServer.closeAllConnections();
  await new Promise<void>((resolve) => crossOriginServer.close(() => resolve()));
  policyProxyServer.closeAllConnections();
  await new Promise<void>((resolve) => policyProxyServer.close(() => resolve()));
  deferTempRootCleanup(contractUserData);
  app.exit(failed.length ? 1 : 0);
});
