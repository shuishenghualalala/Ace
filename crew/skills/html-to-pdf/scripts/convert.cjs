'use strict';

const fs = require('node:fs');
const fsp = fs.promises;
const { randomBytes } = require('node:crypto');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { TextDecoder } = require('node:util');

const DEFAULT_LIMITS = Object.freeze({
  inputBytes: 2 * 1024 * 1024,
  outputBytes: 32 * 1024 * 1024,
  optionsBytes: 64 * 1024,
  timeoutMs: 30_000,
  launchTimeoutMs: 10_000,
  cleanupTimeoutMs: 5_000,
  domNodes: 20_000,
  styleRules: 10_000,
  resourceReferences: 256,
  chromiumProcesses: 16,
  documentHeightPx: 150_000,
  documentWidthPx: 10_000,
  pages: 100,
  imagePixelsEach: 16_000_000,
  imagePixelsTotal: 32_000_000,
  canvasPixelsEach: 16_000_000,
  javascriptHeapBytes: 128 * 1024 * 1024,
});

const ALLOWED_FORMATS = new Set(['A3', 'A4', 'A5', 'Legal', 'Letter', 'Tabloid']);
const FORMAT_HEIGHT_PX = Object.freeze({
  A3: 1587,
  A4: 1123,
  A5: 797,
  Legal: 1344,
  Letter: 1056,
  Tabloid: 1632,
});
const ALLOWED_OPTION_NAMES = new Set([
  'avoidChartBreak',
  'landscape',
  'margin',
  'printBackground',
  'scale',
  'signal',
]);
const ALLOWED_MARGIN_NAMES = new Set(['top', 'right', 'bottom', 'left']);
const DENIED_CHROMIUM_FLAGS = Object.freeze([
  '--disable-gpu-sandbox',
  '--disable-seccomp-filter-sandbox',
  '--disable-setuid-sandbox',
  '--disable-site-isolation-trials',
  '--no-sandbox',
  '--single-process',
]);
const EXPECTED_OUTER_SANDBOXES = new Set([
  'linux-bwrap',
  'macos-seatbelt',
  'windows-sandbox-account',
]);
const SECURITY_CSP = [
  "default-src 'none'",
  "base-uri 'none'",
  "child-src 'none'",
  "connect-src 'none'",
  "font-src 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "frame-src 'none'",
  "img-src 'none'",
  "manifest-src 'none'",
  "media-src 'none'",
  "object-src 'none'",
  "script-src 'none'",
  "style-src 'unsafe-inline'",
  "worker-src 'none'",
].join('; ');

class PdfSecurityError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'PdfSecurityError';
    this.code = code;
  }
}

function securityError(code, message) {
  return new PdfSecurityError(code, message);
}

function mergeLimits(overrides = {}) {
  const limits = { ...DEFAULT_LIMITS, ...overrides };
  for (const [name, value] of Object.entries(limits)) {
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new TypeError(`invalid converter limit: ${name}`);
    }
  }
  return Object.freeze(limits);
}

function assertOuterSandbox(value = process.env.ACE_SANDBOX) {
  if (!EXPECTED_OUTER_SANDBOXES.has(String(value || ''))) {
    throw securityError(
      'outer_sandbox_unavailable',
      'HTML-to-PDF requires the Ace managed OS sandbox',
    );
  }
}

function windowsSystemRoot(environment = process.env) {
  const candidates = [
    environment.SystemRoot,
    environment.SYSTEMROOT,
    environment.WINDIR,
  ].filter((value) => typeof value === 'string' && value.trim());
  if (!candidates.length) {
    throw securityError(
      'cleanup_unavailable',
      'the managed Windows system root is unavailable',
    );
  }
  const normalized = candidates.map((value) => path.win32.resolve(value));
  if (normalized.some((value) => value.startsWith('\\\\'))) {
    throw securityError(
      'cleanup_unavailable',
      'the managed Windows system root is invalid',
    );
  }
  const systemRoot = normalized[0];
  const volume = path.win32.parse(systemRoot).root;
  if (
    normalized.some((value) => value.toLowerCase() !== systemRoot.toLowerCase())
    || !/^[a-z]:\\$/i.test(volume)
    || path.win32.basename(systemRoot).toLowerCase() !== 'windows'
    || path.win32.dirname(systemRoot).toLowerCase()
      !== volume.toLowerCase()
  ) {
    throw securityError(
      'cleanup_unavailable',
      'the managed Windows system root is inconsistent',
    );
  }
  return systemRoot;
}

function fixedBrowserCandidates(
  platform = process.platform,
  _execPath = process.execPath,
  environment = process.env,
) {
  if (platform === 'linux') {
    return [
      '/usr/bin/chromium',
      '/usr/bin/chromium-browser',
      '/usr/bin/google-chrome',
      '/usr/bin/google-chrome-stable',
      '/usr/bin/microsoft-edge',
    ];
  }
  if (platform === 'darwin') {
    return [
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
      '/Applications/Chromium.app/Contents/MacOS/Chromium',
    ];
  }
  if (platform === 'win32') {
    const volume = path.win32.parse(windowsSystemRoot(environment)).root;
    return [
      path.win32.join(volume, 'Program Files', 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.win32.join(volume, 'Program Files (x86)', 'Google', 'Chrome', 'Application', 'chrome.exe'),
      path.win32.join(volume, 'Program Files', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
      path.win32.join(volume, 'Program Files (x86)', 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    ];
  }
  return [];
}

function isWithin(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function assertTrustedBrowserPath(candidate, platform = process.platform, io = fs) {
  if (!path.isAbsolute(candidate) || candidate.includes('\0')) {
    throw securityError('browser_unavailable', 'browser executable path is invalid');
  }
  let resolved;
  let stat;
  try {
    resolved = io.realpathSync(candidate);
    stat = io.statSync(resolved);
  } catch {
    throw securityError('browser_unavailable', 'browser executable is unavailable');
  }
  if (!stat.isFile()) {
    throw securityError('browser_unavailable', 'browser executable is not a regular file');
  }
  if (platform !== 'win32' && (stat.mode & 0o022) !== 0) {
    throw securityError('browser_unavailable', 'browser executable is writable by an untrusted account');
  }
  if (platform === 'linux' && !isWithin(resolved, '/usr')) {
    throw securityError(
      'browser_unavailable',
      'browser executable is outside the Linux sandbox platform root',
    );
  }
  return resolved;
}

function findBrowserExecutable({
  platform = process.platform,
  execPath = process.execPath,
  environment = process.env,
  io = fs,
} = {}) {
  for (const candidate of fixedBrowserCandidates(platform, execPath, environment)) {
    try {
      return assertTrustedBrowserPath(candidate, platform, io);
    } catch {
      // Continue only through the fixed candidate list. No PATH or environment lookup.
    }
  }
  return null;
}

function windowsSystemTools(environment = process.env, io = fs) {
  const systemRoot = windowsSystemRoot(environment);
  const system32 = path.win32.join(systemRoot, 'System32');
  const taskkill = path.win32.join(system32, 'taskkill.exe');
  const kernel = path.win32.join(system32, 'kernel32.dll');
  for (const item of [taskkill, kernel]) {
    let stat;
    try {
      stat = io.statSync(item);
    } catch {
      throw securityError(
        'cleanup_unavailable',
        'fixed Windows process-tree cleanup tools are unavailable',
      );
    }
    if (!stat.isFile()) {
      throw securityError(
        'cleanup_unavailable',
        'fixed Windows process-tree cleanup tools are invalid',
      );
    }
  }
  return { systemRoot, system32, taskkill };
}

async function makePrivateTempRoot(io = fsp, tempBase = os.tmpdir()) {
  const base = await io.realpath(tempBase);
  const root = await io.mkdtemp(path.join(base, 'ace-html-pdf-'));
  await io.chmod(root, 0o700);
  for (const name of ['cache', 'config', 'profile', 'runtime', 'tmp']) {
    await io.mkdir(path.join(root, name), { mode: 0o700 });
  }
  return root;
}

function buildBrowserEnvironment(
  tempRoot,
  {
    platform = process.platform,
    environment = process.env,
    io = fs,
  } = {},
) {
  if (platform === 'win32') {
    const { systemRoot, system32 } = windowsSystemTools(environment, io);
    return {
      PATH: system32,
      SystemRoot: systemRoot,
      TEMP: path.win32.join(tempRoot, 'tmp'),
      TMP: path.win32.join(tempRoot, 'tmp'),
      WINDIR: systemRoot,
    };
  }
  return {
    HOME: tempRoot,
    LANG: 'C.UTF-8',
    LC_ALL: 'C.UTF-8',
    PATH: '/usr/bin:/bin',
    TMPDIR: path.join(tempRoot, 'tmp'),
    XDG_CACHE_HOME: path.join(tempRoot, 'cache'),
    XDG_CONFIG_HOME: path.join(tempRoot, 'config'),
    XDG_RUNTIME_DIR: path.join(tempRoot, 'runtime'),
  };
}

function browserArgs() {
  return [
    '--disable-background-networking',
    '--disable-breakpad',
    '--disable-client-side-phishing-detection',
    '--disable-component-update',
    '--disable-component-extensions-with-background-pages',
    '--disable-crash-reporter',
    '--disable-default-apps',
    '--disable-domain-reliability',
    '--disable-extensions',
    '--disable-features=AutofillServerCommunication,CertificateTransparencyComponentUpdater,InterestFeedContentSuggestions,MediaRouter,OptimizationHints,Translate',
    '--disable-gpu',
    '--disable-hang-monitor',
    '--disable-notifications',
    '--disable-print-preview',
    '--disable-prompt-on-repost',
    '--disable-renderer-backgrounding',
    '--disable-sync',
    '--force-color-profile=srgb',
    '--host-resolver-rules=MAP * ~NOTFOUND',
    '--js-flags=--max-old-space-size=128',
    '--lang=zh-CN',
    '--metrics-recording-only',
    '--no-default-browser-check',
    '--no-first-run',
    '--no-pings',
    '--no-service-autorun',
    '--password-store=basic',
    '--renderer-process-limit=1',
    '--site-per-process',
    '--use-mock-keychain',
  ];
}

function hasDeniedChromiumFlag(args) {
  return args.find((argument) =>
    DENIED_CHROMIUM_FLAGS.some(
      (flag) => argument === flag || argument.startsWith(`${flag}=`),
    ));
}

function hasAdequateWindowsSandboxStatus(status) {
  const lines = String(status).split(/\r?\n/);
  const headerIndex = lines.findIndex((line) =>
    line.includes('Process\tType\tName\tSandbox\tLockdown\tIntegrity'));
  if (headerIndex < 0) {
    return false;
  }
  const rendererRows = lines
    .slice(headerIndex + 1)
    .map((line) => line.split('\t'))
    .filter((columns) => columns[1]?.trim().toLowerCase() === 'renderer');
  return rendererRows.length > 0 && rendererRows.every((columns) =>
    columns[3]?.trim().toLowerCase() === 'renderer'
    && columns[4]?.trim().toLowerCase() === 'lockdown'
    && columns[5]?.toLowerCase().includes('untrusted'));
}

async function verifyChromiumSandbox(
  browser,
  timeoutMs,
  platform = process.platform,
) {
  const child = browser.process?.();
  if (!child || !Number.isSafeInteger(child.pid) || child.pid <= 0) {
    throw securityError('sandbox_unavailable', 'Chromium process identity is unavailable');
  }
  const deniedFlag = hasDeniedChromiumFlag(Array.isArray(child.spawnargs) ? child.spawnargs : []);
  if (deniedFlag) {
    throw securityError('sandbox_unavailable', 'Chromium was launched with a sandbox-disabling flag');
  }

  let probe;
  try {
    const deadline = Date.now() + timeoutMs;
    probe = await browser.newPage();
    await probe.goto('chrome://sandbox/', {
      timeout: timeoutMs,
      waitUntil: 'domcontentloaded',
    });
    while (Date.now() < deadline) {
      const status = await probe.evaluate(() => document.body?.innerText || '');
      const normalized = String(status).toLowerCase();
      if (normalized.includes('you are not adequately sandboxed')) {
        break;
      }
      const adequate =
        normalized.includes('you are adequately sandboxed')
        || (platform === 'win32' && hasAdequateWindowsSandboxStatus(status));
      if (adequate) {
        return child.pid;
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw securityError(
      'sandbox_unavailable',
      'Chromium did not attest an adequate process sandbox',
    );
  } catch (error) {
    if (error instanceof PdfSecurityError) {
      throw error;
    }
    throw securityError(
      'sandbox_unavailable',
      'Chromium sandbox status could not be verified',
    );
  } finally {
    await probe?.close?.().catch(() => {});
  }
}

function normalizePathArgument(value, label) {
  if (typeof value !== 'string' || !value || value.length > 4096 || value.includes('\0')) {
    throw securityError('invalid_path', `${label} path is invalid`);
  }
  if (value.startsWith('\\\\')) {
    throw securityError('invalid_path', `${label} UNC and device paths are denied`);
  }
  if (
    /^[a-z][a-z0-9+.-]*:/i.test(value)
    && !/^[a-z]:[\\/]/i.test(value)
  ) {
    throw securityError('url_input_denied', `${label} URLs are not accepted`);
  }
  return path.resolve(value);
}

async function readInputHtml(inputPath, limits, io = fsp) {
  const absolute = normalizePathArgument(inputPath, 'input');
  let lexical;
  let resolved;
  try {
    lexical = await io.lstat(absolute);
    resolved = await io.realpath(absolute);
  } catch {
    throw securityError('input_unavailable', 'input HTML is unavailable');
  }
  if (lexical.isSymbolicLink()) {
    throw securityError('input_denied', 'symbolic-link HTML input is denied');
  }

  const noFollow = fs.constants.O_NOFOLLOW || 0;
  let handle;
  try {
    handle = await io.open(resolved, fs.constants.O_RDONLY | noFollow);
    const before = await handle.stat();
    if (!before.isFile()) {
      throw securityError('input_denied', 'input HTML is not a regular file');
    }
    if (before.size > limits.inputBytes) {
      throw securityError('input_too_large', 'input HTML exceeds the size limit');
    }
    const bytes = await handle.readFile();
    const after = await handle.stat();
    if (
      bytes.length > limits.inputBytes
      || before.dev !== after.dev
      || before.ino !== after.ino
      || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs
    ) {
      throw securityError('input_changed', 'input HTML changed while it was read');
    }
    let html;
    try {
      html = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch {
      throw securityError('input_encoding', 'input HTML must be valid UTF-8');
    }
    if (html.includes('\0')) {
      throw securityError('input_denied', 'input HTML contains a NUL byte');
    }
    return { html, resolved };
  } finally {
    await handle?.close?.().catch(() => {});
  }
}

async function resolveOutputPath(outputPath, inputResolved, io = fsp) {
  const absolute = normalizePathArgument(outputPath, 'output');
  if (path.extname(absolute).toLowerCase() !== '.pdf') {
    throw securityError('invalid_output', 'output path must end in .pdf');
  }
  let parent;
  try {
    parent = await io.realpath(path.dirname(absolute));
    const parentStat = await io.stat(parent);
    if (!parentStat.isDirectory()) {
      throw new Error('not a directory');
    }
  } catch {
    throw securityError('invalid_output', 'output parent directory is unavailable');
  }
  const resolved = path.join(parent, path.basename(absolute));
  if (resolved === inputResolved) {
    throw securityError('invalid_output', 'input and output paths must differ');
  }
  try {
    await io.lstat(resolved);
  } catch (error) {
    if (error?.code === 'ENOENT') {
      return resolved;
    }
    throw securityError('invalid_output', 'output path could not be inspected');
  }
  throw securityError('output_exists', 'refusing to overwrite an existing output file');
}

function inspectResourceReference(rawValue, state, limits) {
  const value = String(rawValue).trim();
  if (!value || value.startsWith('#')) {
    return;
  }
  state.references += 1;
  if (state.references > limits.resourceReferences) {
    throw securityError('resource_limit', 'HTML contains too many resource references');
  }
  throw securityError(
    'resource_denied',
    'embedded, external, local, relative, blob, and network resources are denied',
  );
}

function assertSafeHtml(html, limits) {
  const tagCount = (html.match(/<(?!!|\/|\?)[a-z][^>]*>/gi) || []).length;
  if (tagCount > limits.domNodes) {
    throw securityError('resource_limit', 'HTML element count exceeds the limit');
  }
  if (
    /<\s*(?:base|embed|fe[a-z]+|filter|foreignobject|frame|frameset|iframe|image|object|portal|script|use)\b/i.test(html)
  ) {
    throw securityError('active_content_denied', 'active or nested browsing content is denied');
  }
  if (/<meta\b[^>]*http-equiv\s*=\s*(['"]?)\s*(?:refresh|content-security-policy)\b/i.test(html)) {
    throw securityError('active_content_denied', 'HTML policy and refresh directives are denied');
  }
  if (/\son[a-z0-9_-]+\s*=/i.test(html) || /\sdownload(?:\s|=|>)/i.test(html)) {
    throw securityError('active_content_denied', 'event handlers and downloads are denied');
  }
  if (/<\s*(?:form|input|button|select|textarea)\b/i.test(html)) {
    throw securityError('active_content_denied', 'interactive form content is denied');
  }
  if (/@(?:font-face|import)\b/i.test(html) || /\blocal\s*\(/i.test(html)) {
    throw securityError('resource_denied', 'CSS imports and local fonts are denied');
  }

  const state = { references: 0 };
  const attributes =
    /\b(?:background|data|href|poster|src|srcset|xlink:href)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/gi;
  for (const match of html.matchAll(attributes)) {
    const name = match[0].slice(0, match[0].indexOf('=')).trim().toLowerCase();
    if (name === 'srcset') {
      throw securityError('resource_denied', 'srcset resources are denied');
    }
    inspectResourceReference(match[1] ?? match[2] ?? match[3] ?? '', state, limits);
  }
  const cssUrls = /\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^'")\s]+))\s*\)/gi;
  for (const match of html.matchAll(cssUrls)) {
    inspectResourceReference(match[1] ?? match[2] ?? match[3] ?? '', state, limits);
  }
}

function secureHtml(html) {
  const withoutDoctype = html.replace(/^\s*<!doctype[^>]*>/i, '');
  return [
    '<!doctype html>',
    `<meta http-equiv="Content-Security-Policy" content="${SECURITY_CSP}">`,
    '<style>',
    '*,*::before,*::after{animation:none!important;transition:none!important;}',
    'html,body{font-family:"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;}',
    '</style>',
    withoutDoctype,
  ].join('');
}

function normalizeLength(value, label) {
  const text = String(value);
  if (!/^(?:0|(?:\d{1,3})(?:\.\d{1,2})?)(?:mm|cm|in|px|pt)$/.test(text)) {
    throw securityError('invalid_options', `${label} is not a bounded CSS length`);
  }
  return text;
}

function normalizeOptions(format, options, limits) {
  if (!ALLOWED_FORMATS.has(format)) {
    throw securityError('invalid_options', 'unsupported PDF page format');
  }
  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    throw securityError('invalid_options', 'PDF options must be an object');
  }
  for (const name of Object.keys(options)) {
    if (!ALLOWED_OPTION_NAMES.has(name)) {
      throw securityError('invalid_options', `unsupported PDF option: ${name}`);
    }
  }
  const normalized = {
    avoidChartBreak: options.avoidChartBreak !== false,
    format,
    landscape: options.landscape === true,
    margin: {
      top: '10mm',
      right: '10mm',
      bottom: '10mm',
      left: '10mm',
    },
    preferCSSPageSize: false,
    printBackground: options.printBackground !== false,
    scale: options.scale === undefined ? 1 : Number(options.scale),
    timeout: limits.timeoutMs,
  };
  for (const name of ['avoidChartBreak', 'landscape', 'printBackground']) {
    if (options[name] !== undefined && typeof options[name] !== 'boolean') {
      throw securityError('invalid_options', `${name} must be boolean`);
    }
  }
  if (!Number.isFinite(normalized.scale) || normalized.scale < 0.1 || normalized.scale > 2) {
    throw securityError('invalid_options', 'scale must be between 0.1 and 2');
  }
  if (options.margin !== undefined) {
    if (!options.margin || typeof options.margin !== 'object' || Array.isArray(options.margin)) {
      throw securityError('invalid_options', 'margin must be an object');
    }
    for (const name of Object.keys(options.margin)) {
      if (!ALLOWED_MARGIN_NAMES.has(name)) {
        throw securityError('invalid_options', `unsupported margin: ${name}`);
      }
      normalized.margin[name] = normalizeLength(options.margin[name], `margin.${name}`);
    }
  }
  return normalized;
}

function installPageGuards(page, browser, limits) {
  const state = {
    blockedRequests: 0,
    violation: null,
  };
  const deny = (code, message) => {
    state.violation ||= securityError(code, message);
  };
  const onRequest = (request) => {
    state.blockedRequests += 1;
    if (state.blockedRequests > limits.resourceReferences) {
      deny('resource_limit', 'page initiated too many resource requests');
    } else {
      deny('resource_denied', 'page initiated a forbidden resource request');
    }
    Promise.resolve(request.abort?.('blockedbyclient')).catch(() => {});
  };
  const onPopup = (popup) => {
    deny('popup_denied', 'page attempted to create a popup');
    Promise.resolve(popup?.close?.()).catch(() => {});
  };
  const onDialog = (dialog) => {
    deny('dialog_denied', 'page attempted to open a dialog');
    Promise.resolve(dialog.dismiss?.()).catch(() => {});
  };
  const onFrameNavigated = (frame) => {
    const url = String(frame.url?.() || '');
    if (frame !== page.mainFrame?.() || url !== 'about:blank') {
      deny('navigation_denied', 'page attempted to navigate');
    }
  };
  const onWorker = (worker) => {
    deny('worker_denied', 'page attempted to create a worker');
    Promise.resolve(worker.close?.()).catch(() => {});
  };
  const allowedTarget = page.target?.();
  const onTargetCreated = (target) => {
    if (target !== allowedTarget && ['background_page', 'page', 'service_worker', 'shared_worker', 'web_worker'].includes(target.type?.())) {
      deny('popup_denied', 'page attempted to create another browser target');
      Promise.resolve(target.page?.())
        .then((targetPage) => targetPage?.close?.())
        .catch(() => {});
    }
  };

  page.on('request', onRequest);
  page.on('popup', onPopup);
  page.on('dialog', onDialog);
  page.on('framenavigated', onFrameNavigated);
  page.on('workercreated', onWorker);
  browser.on?.('targetcreated', onTargetCreated);

  return {
    assertSafe() {
      if (state.violation) {
        throw state.violation;
      }
    },
    denyDownload() {
      deny('download_denied', 'page attempted to start a download');
    },
    dispose() {
      page.off?.('request', onRequest);
      page.off?.('popup', onPopup);
      page.off?.('dialog', onDialog);
      page.off?.('framenavigated', onFrameNavigated);
      page.off?.('workercreated', onWorker);
      browser.off?.('targetcreated', onTargetCreated);
    },
  };
}

async function configurePage(page, browser, limits) {
  await page.setJavaScriptEnabled(false);
  await page.setCacheEnabled(false);
  await page.setRequestInterception(true);
  await page.setOfflineMode(true);
  await page.setViewport({ width: 1280, height: 960, deviceScaleFactor: 1 });
  page.setDefaultTimeout(limits.timeoutMs);
  page.setDefaultNavigationTimeout(limits.timeoutMs);

  const guards = installPageGuards(page, browser, limits);
  const client = await page.createCDPSession();
  const browserClient = await browser.target().createCDPSession();
  await client.send('Network.enable', {
    maxPostDataSize: 0,
    maxResourceBufferSize: 0,
    maxTotalBufferSize: 0,
  });
  await client.send('Network.setCacheDisabled', { cacheDisabled: true });
  await client.send('Network.emulateNetworkConditions', {
    connectionType: 'none',
    downloadThroughput: 0,
    latency: 0,
    offline: true,
    uploadThroughput: 0,
  });
  await browserClient.send('Browser.setDownloadBehavior', {
    behavior: 'deny',
    eventsEnabled: true,
  });
  browserClient.on?.('Browser.downloadWillBegin', () => {
    // The CDP policy denies the write. The guard still turns the attempt into failure.
    guards.denyDownload();
  });
  return { browserClient, client, guards };
}

async function inspectPageBounds(page, browserClient, pdfOptions, limits) {
  const metrics = await page.evaluate(
    ({ maxNodes, maxStyleRules }) => {
      const elements = Array.from(document.querySelectorAll('*'));
      let styleRules = 0;
      const countRules = (rules) => {
        for (const rule of Array.from(rules || [])) {
          styleRules += 1;
          if (styleRules > maxStyleRules) return;
          if (rule.cssRules) countRules(rule.cssRules);
          if (styleRules > maxStyleRules) return;
        }
      };
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          countRules(sheet.cssRules);
        } catch {
          styleRules += maxStyleRules + 1;
        }
      }
      let imagePixelsTotal = 0;
      let imagePixelsMax = 0;
      for (const image of Array.from(document.images)) {
        const pixels = Number(image.naturalWidth || 0) * Number(image.naturalHeight || 0);
        imagePixelsTotal += pixels;
        imagePixelsMax = Math.max(imagePixelsMax, pixels);
      }
      let canvasPixelsMax = 0;
      for (const canvas of Array.from(document.querySelectorAll('canvas'))) {
        canvasPixelsMax = Math.max(
          canvasPixelsMax,
          Number(canvas.width || 0) * Number(canvas.height || 0),
        );
      }
      let forcedPageBreaks = 0;
      const forcesPage = (value) =>
        ['always', 'left', 'page', 'recto', 'right', 'verso'].includes(
          String(value || '').toLowerCase(),
        );
      for (const element of elements) {
        const style = getComputedStyle(element);
        if (
          forcesPage(style.breakBefore)
          || forcesPage(style.breakAfter)
          || forcesPage(style.pageBreakBefore)
          || forcesPage(style.pageBreakAfter)
        ) {
          forcedPageBreaks += 1;
        }
      }
      const root = document.documentElement;
      const body = document.body;
      return {
        canvasPixelsMax,
        forcedPageBreaks,
        height: Math.max(root?.scrollHeight || 0, body?.scrollHeight || 0),
        imagePixelsMax,
        imagePixelsTotal,
        nodes: elements.length,
        styleRules,
        width: Math.max(root?.scrollWidth || 0, body?.scrollWidth || 0),
      };
    },
    { maxNodes: limits.domNodes, maxStyleRules: limits.styleRules },
  );
  if (metrics.nodes > limits.domNodes || metrics.styleRules > limits.styleRules) {
    throw securityError('resource_limit', 'rendered document complexity exceeds the limit');
  }
  if (
    metrics.width > limits.documentWidthPx
    || metrics.height > limits.documentHeightPx
  ) {
    throw securityError('page_limit', 'rendered document dimensions exceed the limit');
  }
  if (
    metrics.imagePixelsMax > limits.imagePixelsEach
    || metrics.imagePixelsTotal > limits.imagePixelsTotal
    || metrics.canvasPixelsMax > limits.canvasPixelsEach
  ) {
    throw securityError('resource_limit', 'decoded image or canvas pixels exceed the limit');
  }
  const pageHeight = FORMAT_HEIGHT_PX[pdfOptions.format];
  const estimatedPages = Math.max(
    1,
    Math.ceil(metrics.height / pageHeight) + (metrics.forcedPageBreaks || 0) * 2,
  );
  if (estimatedPages > limits.pages) {
    throw securityError('page_limit', 'rendered PDF page count exceeds the limit');
  }
  const runtimeMetrics = await page.metrics();
  if ((runtimeMetrics.JSHeapUsedSize || 0) > limits.javascriptHeapBytes) {
    throw securityError('memory_limit', 'renderer JavaScript heap exceeds the limit');
  }
  let processInfo;
  try {
    ({ processInfo } = await browserClient.send('SystemInfo.getProcessInfo'));
  } catch {
    throw securityError(
      'process_limit',
      'Chromium descendant process inventory is unavailable',
    );
  }
  if (!Array.isArray(processInfo) || processInfo.length > limits.chromiumProcesses) {
    throw securityError('process_limit', 'Chromium descendant process count exceeds the limit');
  }
}

async function applyChartBreaks(page, pdfOptions) {
  await page.addStyleTag({
    content: [
      'canvas,svg,figure,.chart,.echarts,.chart-container,.chart-wrapper,',
      '.highcharts-container,.apexcharts-canvas,.antv-chart,.g2plot,.g2-canvas,',
      '.report-chart,.report-section,.page-section{',
      'break-inside:avoid!important;page-break-inside:avoid!important;}',
    ].join(''),
  });
  await page.evaluate(
    ({ pageHeight, marginTop, marginBottom }) => {
      const toPixels = (raw) => {
        const match = String(raw).match(/^([\d.]+)(mm|cm|in|px|pt)$/);
        if (!match) return 0;
        const ratios = { mm: 3.78, cm: 37.8, in: 96, px: 1, pt: 1.333 };
        return Number(match[1]) * ratios[match[2]];
      };
      const contentHeight = pageHeight - toPixels(marginTop) - toPixels(marginBottom);
      const selector =
        'canvas,svg,figure,.chart,.echarts,.chart-container,.chart-wrapper,' +
        '.highcharts-container,.apexcharts-canvas,.antv-chart,.g2plot,.g2-canvas';
      for (const element of document.querySelectorAll(selector)) {
        const rectangle = element.getBoundingClientRect();
        const top = rectangle.top + (window.scrollY || 0);
        if (
          rectangle.height <= contentHeight
          && Math.floor(top / pageHeight) !== Math.floor((top + rectangle.height) / pageHeight)
        ) {
          element.style.breakBefore = 'page';
          element.style.pageBreakBefore = 'always';
        }
      }
    },
    {
      pageHeight: FORMAT_HEIGHT_PX[pdfOptions.format],
      marginTop: pdfOptions.margin.top,
      marginBottom: pdfOptions.margin.bottom,
    },
  );
}

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

async function killBrowserTree(
  browser,
  pid,
  {
    platform = process.platform,
    environment = process.env,
    io = fs,
    isProcessAlive = processExists,
    spawn = spawnSync,
    cleanupTimeoutMs = DEFAULT_LIMITS.cleanupTimeoutMs,
  } = {},
) {
  const disconnect = () => {
    try {
      browser?.disconnect?.();
    } catch {
      // Tree termination is authoritative.
    }
  };
  if (!Number.isSafeInteger(pid) || pid <= 0) {
    disconnect();
    throw securityError('cleanup_failed', 'Chromium process identity was lost');
  }
  try {
    if (platform === 'win32') {
      const { systemRoot, system32, taskkill } = windowsSystemTools(environment, io);
      const result = spawn(taskkill, ['/PID', String(pid), '/T', '/F'], {
        encoding: 'utf8',
        env: {
          PATH: system32,
          SystemRoot: systemRoot,
          TEMP: path.win32.join(systemRoot, 'Temp'),
          TMP: path.win32.join(systemRoot, 'Temp'),
          WINDIR: systemRoot,
        },
        shell: false,
        timeout: cleanupTimeoutMs,
        windowsHide: true,
      });
      if (result.error) {
        throw securityError('cleanup_failed', 'Chromium process tree could not be terminated');
      }
    } else {
      try {
        process.kill(-pid, 'SIGKILL');
      } catch (error) {
        if (error?.code !== 'ESRCH') {
          throw securityError('cleanup_failed', 'Chromium process group could not be terminated');
        }
      }
    }
  } finally {
    disconnect();
  }

  const deadline = Date.now() + cleanupTimeoutMs;
  while (isProcessAlive(pid) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  if (isProcessAlive(pid)) {
    throw securityError('cleanup_failed', 'Chromium root process remained after tree cleanup');
  }
}

async function validatePdf(tempPdf, limits, io = fsp) {
  let stat;
  try {
    stat = await io.stat(tempPdf);
  } catch {
    throw securityError('output_invalid', 'Chromium did not produce a PDF');
  }
  if (!stat.isFile() || stat.size < 5 || stat.size > limits.outputBytes) {
    throw securityError('output_limit', 'generated PDF is empty, invalid, or too large');
  }
  const handle = await io.open(tempPdf, fs.constants.O_RDONLY);
  try {
    const magic = Buffer.alloc(5);
    const { bytesRead } = await handle.read(magic, 0, magic.length, 0);
    if (bytesRead !== 5 || magic.toString('ascii') !== '%PDF-') {
      throw securityError('output_invalid', 'generated output is not a PDF');
    }
  } finally {
    await handle.close();
  }
}

async function publishPdf(
  tempPdf,
  outputPath,
  io = fsp,
  beforeCommit = async () => {},
) {
  const staging = path.join(
    path.dirname(outputPath),
    `.${path.basename(outputPath)}.ace-${randomBytes(12).toString('hex')}.tmp`,
  );
  let linked = false;
  try {
    const bytes = await io.readFile(tempPdf);
    const handle = await io.open(
      staging,
      fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY,
      0o600,
    );
    try {
      await handle.writeFile(bytes);
      await handle.sync();
    } finally {
      await handle.close();
    }
    await beforeCommit();
    // A same-directory hard link is atomic and refuses an output path created
    // after preflight. Rename would overwrite that concurrent winner on POSIX.
    await io.link(staging, outputPath);
    linked = true;
    await io.rm(staging);
  } catch (error) {
    await io.rm(staging, { force: true }).catch(() => {});
    if (linked) {
      await io.rm(outputPath, { force: true }).catch(() => {});
    }
    if (error instanceof PdfSecurityError) {
      throw error;
    }
    throw securityError('output_write_failed', 'PDF output could not be published');
  }
}

function publicError(error) {
  if (error instanceof PdfSecurityError) {
    return error;
  }
  const message = String(error?.message || '').toLowerCase();
  if (
    message.includes('no usable sandbox')
    || message.includes('running as root without --no-sandbox')
    || message.includes('sandbox')
  ) {
    return securityError('sandbox_unavailable', 'Chromium sandbox startup failed');
  }
  if (message.includes('timed out') || message.includes('timeout')) {
    return securityError('timeout', 'HTML-to-PDF conversion timed out');
  }
  return securityError('conversion_failed', 'HTML-to-PDF conversion failed');
}

function createConverter(dependencies = {}) {
  const platform = dependencies.platform || process.platform;
  const execPath = dependencies.execPath || process.execPath;
  const environment = dependencies.environment || process.env;
  const io = dependencies.io || fsp;
  const syncIo = dependencies.syncIo || fs;
  const limits = mergeLimits(dependencies.limits);
  const outerSandbox =
    dependencies.outerSandbox === undefined
      ? process.env.ACE_SANDBOX
      : dependencies.outerSandbox;

  return async function convert(inputPath, outputPath, format = 'A4', options = {}) {
    assertOuterSandbox(outerSandbox);
    const pdfOptions = normalizeOptions(String(format), options, limits);
    const externalSignal = options.signal;
    if (
      externalSignal !== undefined
      && (
        typeof externalSignal !== 'object'
        || typeof externalSignal.addEventListener !== 'function'
        || typeof externalSignal.aborted !== 'boolean'
      )
    ) {
      throw securityError('invalid_options', 'signal must be an AbortSignal');
    }

    let tempRoot;
    let browser;
    let browserPid;
    let page;
    let guards;
    let browserClient;
    let browserCleaned = false;
    const controller = new AbortController();
    const deadline = Date.now() + limits.timeoutMs;
    const timeout = setTimeout(() => {
      controller.abort(securityError('timeout', 'HTML-to-PDF conversion timed out'));
    }, limits.timeoutMs);
    const onExternalAbort = () => {
      controller.abort(securityError('cancelled', 'HTML-to-PDF conversion was cancelled'));
    };
    const aborted = new Promise((_, reject) => {
      controller.signal.addEventListener(
        'abort',
        () => reject(controller.signal.reason),
        { once: true },
      );
    });
    externalSignal?.addEventListener('abort', onExternalAbort, { once: true });
    if (externalSignal?.aborted) {
      onExternalAbort();
    }
    const assertRunning = () => {
      if (controller.signal.aborted) {
        throw controller.signal.reason;
      }
    };

    const work = async () => {
      assertRunning();
      const { html, resolved: inputResolved } =
        await readInputHtml(inputPath, limits, io);
      assertRunning();
      assertSafeHtml(html, limits);
      const resolvedOutput = await resolveOutputPath(outputPath, inputResolved, io);
      assertRunning();
      tempRoot = await makePrivateTempRoot(io, dependencies.tempBase || os.tmpdir());
      assertRunning();
      if (platform === 'win32') {
        windowsSystemTools(environment, syncIo);
      }
      const browserExecutable =
        dependencies.browserExecutablePath
        || findBrowserExecutable({
          platform,
          execPath,
          environment,
          io: syncIo,
        });
      if (!browserExecutable) {
        throw securityError(
          'browser_unavailable',
          'no fixed trusted Chrome, Chromium, or Edge executable is available',
        );
      }
      const executablePath = dependencies.browserExecutablePath
        ? browserExecutable
        : assertTrustedBrowserPath(browserExecutable, platform, syncIo);
      const browserEnvironment = buildBrowserEnvironment(
        tempRoot,
        { platform, environment, io: syncIo },
      );
      const puppeteer = dependencies.puppeteer || require('puppeteer-core');
      browser = await puppeteer.launch({
        acceptInsecureCerts: false,
        args: browserArgs(),
        env: browserEnvironment,
        executablePath,
        handleSIGHUP: false,
        handleSIGINT: false,
        handleSIGTERM: false,
        headless: true,
        pipe: true,
        signal: controller.signal,
        timeout: Math.min(limits.launchTimeoutMs, limits.timeoutMs),
        userDataDir: path.join(tempRoot, 'profile'),
        waitForInitialPage: false,
      });
      browserPid = browser.process?.()?.pid;
      assertRunning();
      await verifyChromiumSandbox(
        browser,
        Math.min(limits.launchTimeoutMs, limits.timeoutMs),
        platform,
      );
      assertRunning();

      const context = await browser.createBrowserContext();
      page = await context.newPage();
      ({ browserClient, guards } = await configurePage(page, browser, limits));
      assertRunning();
      await page.setContent(secureHtml(html), {
        timeout: limits.timeoutMs,
        waitUntil: 'domcontentloaded',
      });
      assertRunning();
      await new Promise((resolve) => setImmediate(resolve));
      guards.assertSafe();
      if (pdfOptions.avoidChartBreak) {
        await applyChartBreaks(page, pdfOptions);
        assertRunning();
      }
      await inspectPageBounds(page, browserClient, pdfOptions, limits);
      assertRunning();
      guards.assertSafe();

      const tempPdf = path.join(tempRoot, 'render.pdf');
      const { avoidChartBreak: _ignored, ...chromiumPdfOptions } = pdfOptions;
      await page.pdf({
        path: tempPdf,
        timeout: limits.timeoutMs,
        ...chromiumPdfOptions,
      });
      assertRunning();
      await new Promise((resolve) => setImmediate(resolve));
      guards.assertSafe();
      await validatePdf(tempPdf, limits, io);
      assertRunning();

      await (dependencies.killBrowserTree || killBrowserTree)(browser, browserPid, {
        cleanupTimeoutMs: limits.cleanupTimeoutMs,
        environment,
        io: syncIo,
        isProcessAlive: dependencies.isProcessAlive,
        platform,
        spawn: dependencies.spawn,
      });
      browserCleaned = true;
      browser = undefined;
      assertRunning();

      await publishPdf(tempPdf, resolvedOutput, io, async () => {
        assertRunning();
        try {
          await io.rm(tempRoot, { force: true, recursive: true, maxRetries: 3 });
          tempRoot = undefined;
        } catch {
          throw securityError('cleanup_failed', 'private Chromium state could not be removed');
        }
        if (Date.now() >= deadline) {
          controller.abort(securityError('timeout', 'HTML-to-PDF conversion timed out'));
        }
        assertRunning();
        clearTimeout(timeout);
        externalSignal?.removeEventListener?.('abort', onExternalAbort);
        assertRunning();
      });
      return resolvedOutput;
    };

    try {
      return await Promise.race([work(), aborted]);
    } catch (error) {
      throw publicError(error);
    } finally {
      clearTimeout(timeout);
      externalSignal?.removeEventListener?.('abort', onExternalAbort);
      let cleanupFailure;
      try {
        guards?.dispose?.();
      } catch {
        cleanupFailure = securityError(
          'cleanup_failed',
          'Chromium page guards could not be released',
        );
      }
      if (browser && !browserCleaned) {
        try {
          await (dependencies.killBrowserTree || killBrowserTree)(browser, browserPid, {
            cleanupTimeoutMs: limits.cleanupTimeoutMs,
            environment,
            io: syncIo,
            isProcessAlive: dependencies.isProcessAlive,
            platform,
            spawn: dependencies.spawn,
          });
        } catch {
          cleanupFailure ||= securityError(
            'cleanup_failed',
            'Chromium process tree could not be terminated',
          );
        }
      }
      await page?.close?.().catch(() => {});
      if (tempRoot) {
        try {
          await io.rm(tempRoot, { force: true, recursive: true, maxRetries: 3 });
          tempRoot = undefined;
        } catch {
          cleanupFailure ||= securityError(
            'cleanup_failed',
            'private Chromium state could not be removed',
          );
        }
      }
      if (cleanupFailure) {
        throw cleanupFailure;
      }
    }
  };
}

const convert = createConverter();

async function runCli(argv = process.argv.slice(2)) {
  const [input, output, format = 'A4', optionsJson = '{}', ...extra] = argv;
  if (!input || !output || extra.length) {
    throw securityError(
      'invalid_arguments',
      'usage: convert.cjs <input.html> <new-output.pdf> [format] [options-json]',
    );
  }
  if (Buffer.byteLength(optionsJson) > DEFAULT_LIMITS.optionsBytes) {
    throw securityError('invalid_options', 'options JSON exceeds the size limit');
  }
  let options;
  try {
    options = JSON.parse(optionsJson);
  } catch {
    throw securityError('invalid_options', 'options JSON is invalid');
  }
  return convert(input, output, format, options);
}

if (require.main === module) {
  runCli()
    .then((output) => {
      process.stdout.write(`PDF created: ${output}\n`);
    })
    .catch((error) => {
      const safe = publicError(error);
      process.stderr.write(`[html-to-pdf:${safe.code}] ${safe.message}\n`);
      process.exitCode = 1;
    });
}

module.exports = {
  DEFAULT_LIMITS,
  DENIED_CHROMIUM_FLAGS,
  PdfSecurityError,
  assertOuterSandbox,
  assertSafeHtml,
  browserArgs,
  buildBrowserEnvironment,
  createConverter,
  findBrowserExecutable,
  fixedBrowserCandidates,
  killBrowserTree,
  normalizeOptions,
  runCli,
  verifyChromiumSandbox,
  windowsSystemTools,
};
