const STARTUP_HOOK_ENV_NAMES = new Set([
  'BASH_ENV',
  'ALL_PROXY',
  'CURL_CA_BUNDLE',
  'ELECTRON_NO_ASAR',
  'ELECTRON_RUN_AS_NODE',
  'ENV',
  'FTP_PROXY',
  'GCONV_PATH',
  'GIT_SSL_CAINFO',
  'HTTPS_PROXY',
  'HTTP_PROXY',
  'LOCPATH',
  'JAVA_TOOL_OPTIONS',
  'JDK_JAVA_OPTIONS',
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
const STARTUP_HOOK_ENV_PREFIXES = [
  'COMPLUS_',
  'CORECLR_',
  'COR_',
  'DOTNET_',
  'DYLD_',
  'LD_',
] as const;
const CREDENTIAL_ENV_COMPONENT =
  /(?:^|[_-])(?:ACCESS[_-]?KEY|API[_-]?KEY|AUTH(?:ORIZATION)?|CREDENTIALS?|PASS(?:WORD|WD)?|PAT|PRIVATE[_-]?KEY|SECRETS?|TOKENS?)(?:$|[_-])/i;

function isStartupHookEnvironmentName(name: string): boolean {
  const normalized = name.toUpperCase();
  return (
    STARTUP_HOOK_ENV_NAMES.has(normalized)
    || STARTUP_HOOK_ENV_PREFIXES.some((prefix) => normalized.startsWith(prefix))
  );
}

function isCredentialEnvironmentName(name: string): boolean {
  return CREDENTIAL_ENV_COMPONENT.test(name);
}

/**
 * Rebuild a child environment from sanitized ambient state.
 *
 * Trusted callers may add an explicit override after filtering. This is the only
 * supported way for a sandbox descendant such as the LibreOffice skill to opt in
 * to a purpose-built LD_PRELOAD shim; ambient loader hooks never cross the boundary.
 */
export function sanitizedChildProcessEnvironment(
  overrides: Readonly<NodeJS.ProcessEnv> = {},
  inherited: Readonly<NodeJS.ProcessEnv> = process.env,
): NodeJS.ProcessEnv {
  const sanitized: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(inherited)) {
    if (
      !isStartupHookEnvironmentName(name)
      && !isCredentialEnvironmentName(name)
      && value !== undefined
    ) {
      sanitized[name] = value;
    }
  }
  return { ...sanitized, ...overrides };
}

type MutableChildOptions<T> = {
  -readonly [Key in keyof T]: T[Key] extends readonly [...infer Items] ? Items : T[Key];
};

/**
 * Enforce the non-shell, sanitized environment contract for every direct child.
 * Reviewed values may be added only through ``trustedEnvironmentOverrides``.
 */
export function hardenedChildProcessOptions<const T extends Record<string, unknown>>(
  options: T,
  trustedEnvironmentOverrides: Readonly<NodeJS.ProcessEnv> = {},
  inherited: Readonly<NodeJS.ProcessEnv> = process.env,
): MutableChildOptions<T> & { shell: false; env: NodeJS.ProcessEnv } {
  if (options['shell'] === true) {
    throw new Error('shell child processes are forbidden');
  }
  if (options['env'] !== undefined) {
    throw new Error('child env must use trusted environment overrides');
  }
  return {
    ...options,
    shell: false,
    env: sanitizedChildProcessEnvironment(trustedEnvironmentOverrides, inherited),
  } as MutableChildOptions<T> & { shell: false; env: NodeJS.ProcessEnv };
}
