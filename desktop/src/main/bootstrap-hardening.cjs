'use strict';

// This file must remain dependency-free and must never import Electron. It runs
// before the packaged main bundle so Chromium and imported modules inherit a
// sanitized process environment.
const DANGEROUS_ENV_NAMES = new Set([
  'ALL_PROXY',
  'BASH_ENV',
  'CURL_CA_BUNDLE',
  'ELECTRON_NO_ASAR',
  'ELECTRON_RUN_AS_NODE',
  'ENV',
  'FTP_PROXY',
  'GCONV_PATH',
  'GIT_SSL_CAINFO',
  'HTTPS_PROXY',
  'HTTP_PROXY',
  'JAVA_TOOL_OPTIONS',
  'JDK_JAVA_OPTIONS',
  'LD_AUDIT',
  'LD_LIBRARY_PATH',
  'LD_LIBRARY_PATH_64',
  'LD_PRELOAD',
  'LOCPATH',
  'NODE_EXTRA_CA_CERTS',
  'NODE_OPTIONS',
  'NODE_PATH',
  'NODE_REPL_EXTERNAL_MODULE',
  'NO_PROXY',
  'NPM_CONFIG_CAFILE',
  'OPENSSL_CONF',
  'OPENSSL_MODULES',
  'PERL5OPT',
  'PYTHONBREAKPOINT',
  'PYTHONHOME',
  'PYTHONINSPECT',
  'PYTHONPATH',
  'PYTHONSTARTUP',
  'PYTHONUSERBASE',
  'PYTHONWARNINGS',
  'REQUESTS_CA_BUNDLE',
  'PSMODULEPATH',
  'RUBYOPT',
  'SSL_CERT_DIR',
  'SSL_CERT_FILE',
  '_JAVA_OPTIONS',
]);
const DANGEROUS_ENV_PREFIXES = Object.freeze([
  'COMPLUS_',
  'CORECLR_',
  'COR_',
  'DOTNET_',
  'DYLD_',
  'LD_',
]);

const FORBIDDEN_CHROMIUM_SWITCHES = Object.freeze([
  'remote-debugging-address',
  'remote-debugging-pipe',
  'remote-debugging-port',
  'remote-allow-origins',
  'no-sandbox',
  'disable-sandbox',
  'disable-setuid-sandbox',
  'disable-web-security',
  'disable-features',
  'disable-gpu-sandbox',
  'proxy-server',
  'proxy-pac-url',
  'proxy-bypass-list',
  'host-resolver-rules',
  'user-data-dir',
  'load-extension',
  'allow-file-access-from-files',
  'allow-running-insecure-content',
  'ignore-certificate-errors',
  'disable-site-isolation-trials',
  'browser-subprocess-path',
  'gpu-launcher',
  'js-flags',
  'renderer-cmd-prefix',
  'utility-cmd-prefix',
]);

const FORBIDDEN_NODE_SWITCHES = new Set([
  'debug',
  'debug-brk',
  'debug-port',
  'experimental-loader',
  'import',
  'inspect',
  'inspect-brk',
  'inspect-port',
  'loader',
  'require',
]);
const FORBIDDEN_ARGUMENT_NAMES = new Set([
  ...FORBIDDEN_CHROMIUM_SWITCHES,
  ...FORBIDDEN_NODE_SWITCHES,
]);
const ARGUMENTS_WITH_SEPARATE_VALUES = new Set([
  'debug-port',
  'browser-subprocess-path',
  'disable-features',
  'experimental-loader',
  'gpu-launcher',
  'host-resolver-rules',
  'import',
  'inspect-port',
  'js-flags',
  'loader',
  'load-extension',
  'proxy-bypass-list',
  'proxy-pac-url',
  'proxy-server',
  'remote-allow-origins',
  'remote-debugging-address',
  'remote-debugging-port',
  'renderer-cmd-prefix',
  'require',
  'user-data-dir',
  'utility-cmd-prefix',
]);
const ARGUMENTS_WITH_SWITCH_LIKE_VALUES = new Set([
  'gpu-launcher',
  'js-flags',
  'renderer-cmd-prefix',
  'utility-cmd-prefix',
]);

function isDangerousEnvironmentName(name) {
  const normalized = String(name).toUpperCase();
  return (
    DANGEROUS_ENV_NAMES.has(normalized)
    || DANGEROUS_ENV_PREFIXES.some((prefix) => normalized.startsWith(prefix))
  );
}

function sanitizeEnvironment(env = process.env) {
  const removed = [];
  for (const name of Object.keys(env)) {
    if (!isDangerousEnvironmentName(name)) continue;
    if (!Reflect.deleteProperty(env, name) || Object.hasOwn(env, name)) {
      throw new Error(`failed to remove unsafe environment variable ${name}`);
    }
    removed.push(name);
  }
  return removed;
}

function argumentName(token) {
  if (token === '-r') return 'require';
  if (token.startsWith('-r') && token.length > 2) return 'require';
  if (!token.startsWith('--')) return '';
  return token.slice(2).split('=', 1)[0].toLowerCase();
}

function stripDangerousArguments(argv) {
  const kept = [];
  const removed = [];
  for (let index = 0; index < argv.length; index += 1) {
    const token = String(argv[index]);
    const name = argumentName(token);
    if (!FORBIDDEN_ARGUMENT_NAMES.has(name)) {
      kept.push(argv[index]);
      continue;
    }
    removed.push(name);
    const hasInlineValue = token.includes('=') || (name === 'require' && token.startsWith('-r') && token !== '-r');
    const next = argv[index + 1];
    if (
      !hasInlineValue
      && ARGUMENTS_WITH_SEPARATE_VALUES.has(name)
      && typeof next === 'string'
      && (ARGUMENTS_WITH_SWITCH_LIKE_VALUES.has(name) || !next.startsWith('-'))
    ) {
      index += 1;
    }
  }
  argv.splice(0, argv.length, ...kept);
  return removed;
}

function defaultLog(message) {
  process.stderr.write(`${message}\n`);
}

function hardenElectronBootstrap(options = {}) {
  const env = options.env ?? process.env;
  const argv = options.argv ?? process.argv;
  const execArgv = options.execArgv ?? process.execArgv;
  const log = options.log ?? defaultLog;

  const removedEnvironment = sanitizeEnvironment(env);
  const removedArguments = [
    ...stripDangerousArguments(argv),
    ...stripDangerousArguments(execArgv),
  ];
  if (removedEnvironment.length || removedArguments.length) {
    log(
      '[electron-hardening] removed ambient startup hooks '
      + `env=${removedEnvironment.join(',') || '-'} `
      + `args=${removedArguments.join(',') || '-'}`,
    );
  }
  return { removedEnvironment, removedArguments };
}

module.exports = {
  FORBIDDEN_CHROMIUM_SWITCHES,
  hardenElectronBootstrap,
  sanitizeEnvironment,
  stripDangerousArguments,
};
