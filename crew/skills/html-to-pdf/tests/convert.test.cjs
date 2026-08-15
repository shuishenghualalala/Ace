'use strict';

const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const fsp = fs.promises;
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  DENIED_CHROMIUM_FLAGS,
  PdfSecurityError,
  assertSafeHtml,
  browserArgs,
  buildBrowserEnvironment,
  createConverter,
  fixedBrowserCandidates,
  killBrowserTree,
  verifyChromiumSandbox,
  windowsSystemTools,
} = require('../scripts/convert.cjs');
const {
  nodeVersionSupported,
  scrubCliEnvironment,
} = require('../scripts/run.cjs');

class FakeCdpSession extends EventEmitter {
  constructor(hooks = {}) {
    super();
    this.hooks = hooks;
    this.calls = [];
  }

  async send(method, params) {
    this.calls.push({ method, params });
    if (method === 'SystemInfo.getProcessInfo') {
      return {
        processInfo:
          this.hooks.processInfo
          || [
            { id: 1, type: 'browser' },
            { id: 2, type: 'renderer' },
          ],
      };
    }
    return {};
  }
}

class FakePage extends EventEmitter {
  constructor(hooks = {}) {
    super();
    this.hooks = hooks;
    this.cdp = new FakeCdpSession(hooks);
    this.frame = { url: () => 'about:blank' };
    this.pageTarget = { type: () => 'page' };
    this.closed = false;
  }

  async setJavaScriptEnabled(value) {
    this.javascriptEnabled = value;
  }

  async setCacheEnabled(value) {
    this.cacheEnabled = value;
  }

  async setRequestInterception(value) {
    this.requestInterception = value;
  }

  async setOfflineMode(value) {
    this.offline = value;
  }

  async setViewport(value) {
    this.viewport = value;
  }

  setDefaultTimeout(value) {
    this.defaultTimeout = value;
  }

  setDefaultNavigationTimeout(value) {
    this.defaultNavigationTimeout = value;
  }

  async createCDPSession() {
    return this.cdp;
  }

  mainFrame() {
    return this.frame;
  }

  target() {
    return this.pageTarget;
  }

  async setContent(html) {
    this.html = html;
    return this.hooks.setContent?.(this, html);
  }

  async addStyleTag(value) {
    this.style = value;
  }

  async evaluate(_callback, argument) {
    if (argument?.maxNodes) {
      return this.hooks.pageBounds || {
        canvasPixelsMax: 0,
        height: 1000,
        imagePixelsMax: 0,
        imagePixelsTotal: 0,
        nodes: 20,
        styleRules: 2,
        width: 800,
      };
    }
    return undefined;
  }

  async metrics() {
    return this.hooks.metrics || { JSHeapUsedSize: 1024 };
  }

  async pdf(options) {
    if (this.hooks.pdf) {
      return this.hooks.pdf(this, options);
    }
    await fsp.writeFile(options.path, '%PDF-fake\n');
  }

  async close() {
    this.closed = true;
  }
}

class FakeSandboxPage {
  constructor(text) {
    this.text = text;
    this.closed = false;
  }

  async goto(url) {
    assert.equal(url, 'chrome://sandbox/');
  }

  async evaluate() {
    return this.text;
  }

  async close() {
    this.closed = true;
  }
}

class FakeBrowser extends EventEmitter {
  constructor(hooks = {}) {
    super();
    this.hooks = hooks;
    this.pid = hooks.pid || 424242;
    this.renderPage = new FakePage(hooks);
    this.cdp = new FakeCdpSession(hooks);
    this.sandboxPage = new FakeSandboxPage(
      hooks.sandboxText || 'Sandbox Status\nYou are adequately sandboxed.',
    );
  }

  process() {
    return {
      pid: this.pid,
      spawnargs:
        this.hooks.spawnargs
        || [this.hooks.browserExecutablePath || '/trusted/chromium', ...browserArgs()],
    };
  }

  async newPage() {
    return this.sandboxPage;
  }

  async createBrowserContext() {
    return {
      newPage: async () => this.renderPage,
    };
  }

  target() {
    return {
      createCDPSession: async () => this.cdp,
    };
  }

  disconnect() {
    this.disconnected = true;
  }
}

async function fixture(t, html = '<h1>safe</h1>', hooks = {}) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), 'ace-pdf-test-'));
  t.after(async () => {
    await fsp.rm(root, { force: true, recursive: true });
  });
  const input = path.join(root, 'input.html');
  const output = path.join(root, 'output.pdf');
  await fsp.writeFile(input, html);

  const state = {
    browser: null,
    cleanupCalls: [],
    environmentDuringLaunch: null,
    launchOptions: null,
  };
  const puppeteer = {
    launch: async (options) => {
      state.launchOptions = options;
      state.environmentDuringLaunch = { ...process.env };
      if (hooks.launchError) {
        throw hooks.launchError;
      }
      state.browser = new FakeBrowser({
        ...hooks,
        browserExecutablePath: hooks.browserExecutablePath || '/trusted/chromium',
      });
      return state.browser;
    },
  };
  const killBrowserTree = async (_browser, pid) => {
    state.cleanupCalls.push(pid);
    if (hooks.cleanupError) {
      throw hooks.cleanupError;
    }
  };
  const converter = createConverter({
    browserExecutablePath: hooks.browserExecutablePath || '/trusted/chromium',
    io: hooks.io,
    killBrowserTree,
    limits: hooks.limits,
    outerSandbox:
      hooks.outerSandbox === undefined ? 'linux-bwrap' : hooks.outerSandbox,
    platform: hooks.platform || 'linux',
    puppeteer,
    syncIo: hooks.syncIo,
    tempBase: root,
  });
  return { converter, input, output, root, state };
}

async function expectCode(promise, code) {
  await assert.rejects(promise, (error) => {
    assert.ok(error instanceof PdfSecurityError);
    assert.equal(error.code, code);
    return true;
  });
}

test('CLI keeps the current Node executable and removes ambient credentials', () => {
  assert.equal(nodeVersionSupported('22.11.9'), false);
  assert.equal(nodeVersionSupported('22.12.0'), true);
  const environment = {
    ACE_SANDBOX: 'linux-bwrap',
    AWS_SECRET_ACCESS_KEY: 'secret',
    HOME: '/private/tmp/ace-home',
    PATH: '/attacker',
  };
  scrubCliEnvironment(environment);
  assert.deepEqual(environment, {
    ACE_SANDBOX: 'linux-bwrap',
    HOME: '/private/tmp/ace-home',
  });
});

test('uses the managed Windows root for fixed browser and cleanup paths', async () => {
  const environment = {
    SystemRoot: 'C:\\Windows',
    WINDIR: 'c:\\WINDOWS',
  };
  const candidates = fixedBrowserCandidates(
    'win32',
    'E:\\portable-node\\node.exe',
    environment,
  );
  assert.equal(
    candidates[0],
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  );

  const inspected = [];
  const io = {
    statSync(candidate) {
      inspected.push(candidate);
      return { isFile: () => true };
    },
  };
  const tools = windowsSystemTools(environment, io);
  assert.equal(tools.taskkill, 'C:\\Windows\\System32\\taskkill.exe');
  assert.deepEqual(inspected, [
    'C:\\Windows\\System32\\taskkill.exe',
    'C:\\Windows\\System32\\kernel32.dll',
  ]);
  const browserEnvironment = buildBrowserEnvironment('D:\\private', {
    environment,
    io,
    platform: 'win32',
  });
  assert.equal(browserEnvironment.APPDATA, undefined);
  assert.equal(browserEnvironment.LOCALAPPDATA, undefined);
  assert.equal(browserEnvironment.USERPROFILE, undefined);
  assert.equal(browserEnvironment.TEMP, 'D:\\private\\tmp');
  assert.throws(
    () => windowsSystemTools({ SystemRoot: 'E:\\attacker' }, io),
    (error) => error instanceof PdfSecurityError && error.code === 'cleanup_unavailable',
  );

  let alive = true;
  let disconnected = false;
  const cleanupOrder = [];
  let launch;
  await killBrowserTree(
    {
      disconnect: () => {
        disconnected = true;
        cleanupOrder.push('disconnect');
      },
    },
    424242,
    {
      environment,
      io,
      isProcessAlive: () => alive,
      platform: 'win32',
      spawn(executable, args, options) {
        cleanupOrder.push('taskkill');
        launch = { args, executable, options };
        alive = false;
        return { status: 128 };
      },
    },
  );
  assert.equal(disconnected, true);
  assert.deepEqual(cleanupOrder, ['taskkill', 'disconnect']);
  assert.equal(launch.executable, tools.taskkill);
  assert.deepEqual(launch.args, ['/PID', '424242', '/T', '/F']);
  assert.equal(launch.options.shell, false);
  assert.equal(launch.options.env.PATH, 'C:\\Windows\\System32');
});

test('launches only a fixed sandboxed browser with an empty-credential environment', async (t) => {
  process.env.AWS_SECRET_ACCESS_KEY = 'must-not-cross';
  t.after(() => {
    delete process.env.AWS_SECRET_ACCESS_KEY;
  });
  const { converter, input, output, state } = await fixture(t);

  const result = await converter(input, output, 'A4', {
    landscape: true,
    margin: { top: '12mm' },
  });

  assert.equal(result, output);
  assert.equal(await fsp.readFile(output, 'utf8'), '%PDF-fake\n');
  assert.equal(state.launchOptions.executablePath, '/trusted/chromium');
  assert.equal(state.launchOptions.pipe, true);
  assert.equal(state.launchOptions.env.AWS_SECRET_ACCESS_KEY, undefined);
  assert.equal(state.launchOptions.env.PUPPETEER_EXECUTABLE_PATH, undefined);
  assert.equal(state.environmentDuringLaunch.AWS_SECRET_ACCESS_KEY, 'must-not-cross');
  assert.equal(state.browser.renderPage.javascriptEnabled, false);
  assert.equal(state.browser.renderPage.offline, true);
  assert.deepEqual(state.cleanupCalls, [424242]);
  assert.equal(
    DENIED_CHROMIUM_FLAGS.some((flag) => state.launchOptions.args.includes(flag)),
    false,
  );
  const privateRoot = path.dirname(state.launchOptions.userDataDir);
  await assert.rejects(fsp.stat(privateRoot), { code: 'ENOENT' });
});

test('fails before launch when the outer managed sandbox marker is missing', async (t) => {
  const { converter, input, output, state } = await fixture(t, '<p>safe</p>', {
    outerSandbox: '',
  });
  await expectCode(converter(input, output), 'outer_sandbox_unavailable');
  assert.equal(state.launchOptions, null);
  assert.equal(fs.existsSync(output), false);
});

test('fails closed when Chromium cannot attest its process sandbox', async (t) => {
  const { converter, input, output, state } = await fixture(t, '<p>safe</p>', {
    sandboxText: 'Sandbox Status\nYou are not adequately sandboxed!',
  });
  await expectCode(converter(input, output), 'sandbox_unavailable');
  assert.deepEqual(state.cleanupCalls, [424242]);
  assert.equal(fs.existsSync(output), false);
});

test('attests Windows renderer lockdown from chrome sandbox status', async () => {
  const header = [
    'Sandbox Status',
    'Process\tType\tName\tSandbox\tLockdown\tIntegrity\tMitigations',
  ].join('\n');
  const sandboxed = new FakeBrowser({
    sandboxText:
      `${header}\n4242\tRenderer\t\tRenderer\tLockdown\tS-1-16-0 Untrusted\t0111`,
  });
  assert.equal(await verifyChromiumSandbox(sandboxed, 100, 'win32'), 424242);

  const unsandboxed = new FakeBrowser({
    sandboxText:
      `${header}\n4242\tRenderer\t\tNot Sandboxed\t\tMedium\t0000`,
  });
  await expectCode(
    verifyChromiumSandbox(unsandboxed, 100, 'win32'),
    'sandbox_unavailable',
  );
});

test('fails closed if the effective Chromium command disables a sandbox', async (t) => {
  const { converter, input, output, state } = await fixture(t, '<p>safe</p>', {
    spawnargs: ['/trusted/chromium', '--no-sandbox'],
  });
  await expectCode(converter(input, output), 'sandbox_unavailable');
  assert.deepEqual(state.cleanupCalls, [424242]);
  assert.equal(fs.existsSync(output), false);
});

test('does not retry a launch with no-sandbox after Chromium sandbox startup failure', async (t) => {
  const { converter, input, output, state } = await fixture(t, '<p>safe</p>', {
    launchError: new Error('Running as root without --no-sandbox is not supported'),
  });
  await expectCode(converter(input, output), 'sandbox_unavailable');
  assert.equal(state.launchOptions.args.includes('--no-sandbox'), false);
  assert.equal(state.cleanupCalls.length, 0);
  assert.equal(fs.existsSync(output), false);
});

test('denies host-file references before Chromium can read them', async (t) => {
  const secret = path.join(os.tmpdir(), `ace-pdf-secret-${process.pid}.txt`);
  await fsp.writeFile(secret, 'HOST_FILE_CANARY');
  t.after(async () => {
    await fsp.rm(secret, { force: true });
  });
  const fileUrl = `file:///${secret.replaceAll('\\', '/')}`;
  const { converter, input, output, state } = await fixture(
    t,
    `<img src="${fileUrl}">`,
  );
  await expectCode(converter(input, output), 'resource_denied');
  assert.equal(state.launchOptions, null);
  assert.equal(fs.existsSync(output), false);
});

test('denies loopback, private, IPv6-local, and metadata resources before launch', async (t) => {
  const urls = [
    'http://127.0.0.1:8080/secret',
    'http://10.0.0.1/private',
    'http://172.16.0.1/private',
    'http://192.168.1.1/private',
    'http://[::1]/private',
    'http://169.254.169.254/latest/meta-data/',
    'data:image/png;base64,iVBORw0KGgo=',
  ];
  for (const url of urls) {
    assert.throws(
      () => assertSafeHtml(`<img src="${url}">`, {
        dataUrlBytes: 1024,
        domNodes: 10,
        resourceReferences: 10,
      }),
      (error) => error instanceof PdfSecurityError && error.code === 'resource_denied',
      url,
    );
  }

  let requests = 0;
  const server = http.createServer((_request, response) => {
    requests += 1;
    response.end('loopback canary');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  const { converter, input, output, state } = await fixture(
    t,
    `<img src="http://127.0.0.1:${address.port}/canary">`,
  );
  await expectCode(converter(input, output), 'resource_denied');
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(requests, 0);
  assert.equal(state.launchOptions, null);
});

test('denies URL inputs, navigation, popups, downloads, and active content', async (t) => {
  const base = await fixture(t);
  await expectCode(
    base.converter('https://example.com', base.output),
    'url_input_denied',
  );

  for (const html of [
    '<meta http-equiv="refresh" content="0;url=https://example.com">',
    '<script>location="https://example.com"</script>',
    '<a href="#ok" download>download</a>',
    '<iframe srcdoc="<p>nested</p>"></iframe>',
  ]) {
    assert.throws(
      () => assertSafeHtml(html, {
        dataUrlBytes: 1024,
        domNodes: 10,
        resourceReferences: 10,
      }),
      (error) => error instanceof PdfSecurityError,
      html,
    );
  }

  const popup = await fixture(t, '<p>safe</p>', {
    setContent: (page) => {
      page.emit('popup', { close: async () => {} });
    },
  });
  await expectCode(popup.converter(popup.input, popup.output), 'popup_denied');

  let requestAborted = false;
  const runtimeRequest = await fixture(
    t,
    '<style>body{background-image:image-set("http://127.0.0.1/x" 1x)}</style>',
    {
      setContent: (page) => {
        page.emit('request', {
          abort: async (reason) => {
            assert.equal(reason, 'blockedbyclient');
            requestAborted = true;
          },
        });
      },
    },
  );
  await expectCode(
    runtimeRequest.converter(runtimeRequest.input, runtimeRequest.output),
    'resource_denied',
  );
  assert.equal(requestAborted, true);

  const navigation = await fixture(t, '<p>safe</p>', {
    setContent: (page) => {
      page.emit('framenavigated', { url: () => 'https://example.com' });
    },
  });
  await expectCode(
    navigation.converter(navigation.input, navigation.output),
    'navigation_denied',
  );

  const download = await fixture(t, '<p>safe</p>', {
    setContent: (_page) => {
      download.state.browser.cdp.emit('Browser.downloadWillBegin', {
        url: 'data:text/plain,x',
      });
    },
  });
  await expectCode(download.converter(download.input, download.output), 'download_denied');
});

test('bounds input, DOM, page dimensions, heap, output, and options', async (t) => {
  const oversizedInput = await fixture(t, 'x'.repeat(129), {
    limits: { inputBytes: 128 },
  });
  await expectCode(
    oversizedInput.converter(oversizedInput.input, oversizedInput.output),
    'input_too_large',
  );

  const domBomb = await fixture(t, '<i></i>'.repeat(21), {
    limits: { domNodes: 20 },
  });
  await expectCode(domBomb.converter(domBomb.input, domBomb.output), 'resource_limit');

  const tallPage = await fixture(t, '<p>safe</p>', {
    pageBounds: {
      canvasPixelsMax: 0,
      height: 2000,
      imagePixelsMax: 0,
      imagePixelsTotal: 0,
      nodes: 2,
      styleRules: 1,
      width: 800,
    },
    limits: { documentHeightPx: 1000 },
  });
  await expectCode(tallPage.converter(tallPage.input, tallPage.output), 'page_limit');

  const forcedPageBomb = await fixture(t, '<p>safe</p>', {
    limits: { pages: 2 },
    pageBounds: {
      canvasPixelsMax: 0,
      forcedPageBreaks: 1,
      height: 100,
      imagePixelsMax: 0,
      imagePixelsTotal: 0,
      nodes: 2,
      styleRules: 1,
      width: 800,
    },
  });
  await expectCode(
    forcedPageBomb.converter(forcedPageBomb.input, forcedPageBomb.output),
    'page_limit',
  );

  const heapBomb = await fixture(t, '<p>safe</p>', {
    limits: { javascriptHeapBytes: 1024 },
    metrics: { JSHeapUsedSize: 1025 },
  });
  await expectCode(heapBomb.converter(heapBomb.input, heapBomb.output), 'memory_limit');

  const processBomb = await fixture(t, '<p>safe</p>', {
    limits: { chromiumProcesses: 4 },
    processInfo: Array.from({ length: 5 }, (_, index) => ({
      id: index + 1,
      type: index === 0 ? 'browser' : 'renderer',
    })),
  });
  await expectCode(
    processBomb.converter(processBomb.input, processBomb.output),
    'process_limit',
  );

  const outputBomb = await fixture(t, '<p>safe</p>', {
    limits: { outputBytes: 16 },
    pdf: async (_page, options) => {
      await fsp.writeFile(options.path, `%PDF-${'x'.repeat(20)}`);
    },
  });
  await expectCode(outputBomb.converter(outputBomb.input, outputBomb.output), 'output_limit');

  const invalidOptions = await fixture(t);
  await expectCode(
    invalidOptions.converter(invalidOptions.input, invalidOptions.output, 'A4', {
      displayHeaderFooter: true,
    }),
    'invalid_options',
  );
});

test('kills Chromium and removes private state on renderer crash', async (t) => {
  const { converter, input, output, state } = await fixture(t, '<p>safe</p>', {
    pdf: async () => {
      throw new Error('renderer crashed');
    },
  });
  await expectCode(converter(input, output), 'conversion_failed');
  assert.deepEqual(state.cleanupCalls, [424242]);
  assert.equal(fs.existsSync(output), false);
  const privateRoot = path.dirname(state.launchOptions.userDataDir);
  await assert.rejects(fsp.stat(privateRoot), { code: 'ENOENT' });
});

test('does not publish when private Chromium state cleanup fails', async (t) => {
  const io = new Proxy(fsp, {
    get(target, property) {
      if (property === 'rm') {
        return async (candidate, options) => {
          if (path.basename(candidate).startsWith('ace-html-pdf-')) {
            throw new Error('injected private-state cleanup failure');
          }
          return target.rm(candidate, options);
        };
      }
      return target[property];
    },
  });
  const { converter, input, output, state } = await fixture(t, '<p>safe</p>', { io });
  await expectCode(converter(input, output), 'cleanup_failed');
  assert.deepEqual(state.cleanupCalls, [424242]);
  assert.equal(fs.existsSync(output), false);
});

test('kills Chromium on timeout and cancellation without publishing output', async (t) => {
  let releaseTimedOutRender;
  const timeoutFixture = await fixture(t, '<p>safe</p>', {
    limits: { timeoutMs: 200 },
    setContent: () => new Promise((resolve) => {
      releaseTimedOutRender = resolve;
    }),
  });
  await expectCode(
    timeoutFixture.converter(timeoutFixture.input, timeoutFixture.output),
    'timeout',
  );
  assert.deepEqual(timeoutFixture.state.cleanupCalls, [424242]);
  assert.equal(fs.existsSync(timeoutFixture.output), false);
  releaseTimedOutRender();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(fs.existsSync(timeoutFixture.output), false);

  const cancellation = new AbortController();
  const cancelFixture = await fixture(t, '<p>safe</p>', {
    setContent: () => {
      setImmediate(() => cancellation.abort());
      return new Promise(() => {});
    },
  });
  const conversion = cancelFixture.converter(
    cancelFixture.input,
    cancelFixture.output,
    'A4',
    { signal: cancellation.signal },
  );
  await expectCode(conversion, 'cancelled');
  assert.deepEqual(cancelFixture.state.cleanupCalls, [424242]);
  assert.equal(fs.existsSync(cancelFixture.output), false);
});

test('refuses to overwrite an existing output', async (t) => {
  const { converter, input, output, state } = await fixture(t);
  await fsp.writeFile(output, 'existing');
  await expectCode(converter(input, output), 'output_exists');
  assert.equal(await fsp.readFile(output, 'utf8'), 'existing');
  assert.equal(state.launchOptions, null);
});
