import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import { describe, expect, it } from 'vitest';
import {
  hardenedChildProcessOptions,
  sanitizedChildProcessEnvironment,
} from '../../src/main/process-environment';

const require = createRequire(import.meta.url);
const {
  FORBIDDEN_CHROMIUM_SWITCHES,
  hardenElectronBootstrap,
  sanitizeEnvironment,
  stripDangerousArguments,
} = require('../../src/main/bootstrap-hardening.cjs') as {
  FORBIDDEN_CHROMIUM_SWITCHES: readonly string[];
  hardenElectronBootstrap: (options: {
    env: Record<string, string>;
    argv: string[];
    execArgv: string[];
    log: (message: string) => void;
  }) => {
    removedEnvironment: string[];
    removedArguments: string[];
  };
  sanitizeEnvironment: (env: Record<string, string>) => string[];
  stripDangerousArguments: (argv: string[]) => string[];
};

describe('Electron bootstrap hardening', () => {
  it('removes loader and runtime injection variables without exposing values', () => {
    const env = {
      PATH: 'C:\\safe',
      ACE_SECURITY_MODE: 'managed',
      LD_PRELOAD: '/tmp/inject.so',
      ld_audit: '/tmp/audit.so',
      LD_LIBRARY_PATH: '/tmp/libs',
      LD_PROFILE: '/tmp/profile-output',
      DYLD_INSERT_LIBRARIES: '/tmp/inject.dylib',
      DyLd_Custom_Hook: 'unsafe',
      PYTHONINSPECT: '1',
      PYTHONSTARTUP: '/tmp/start.py',
      PYTHONPATH: '/tmp/modules',
      PYTHONHOME: '/tmp/python',
      NODE_OPTIONS: '--require=/tmp/hook.js',
      NODE_PATH: '/tmp/node_modules',
      NODE_EXTRA_CA_CERTS: '/tmp/attacker-ca.pem',
      SSL_CERT_FILE: '/tmp/attacker-ca.pem',
      SSL_CERT_DIR: '/tmp/attacker-certs',
      HTTPS_PROXY: 'http://attacker.invalid:8080',
      ALL_PROXY: 'socks5://attacker.invalid:1080',
      ELECTRON_RUN_AS_NODE: '1',
      ELECTRON_NO_ASAR: '1',
      OPENSSL_CONF: '/tmp/openssl.cnf',
      PSModulePath: 'C:\\attacker\\modules',
      COR_ENABLE_PROFILING: '1',
      CORECLR_PROFILER_PATH: 'C:\\attacker\\profiler.dll',
      DOTNET_STARTUP_HOOKS: 'C:\\attacker\\startup-hook.dll',
      COMPlus_ReadyToRun: '0',
      JAVA_TOOL_OPTIONS: '-javaagent:C:\\attacker\\agent.jar',
    };

    const removed = sanitizeEnvironment(env);

    expect(new Set(removed)).toEqual(new Set([
      'LD_PRELOAD',
      'ld_audit',
      'LD_LIBRARY_PATH',
      'LD_PROFILE',
      'DYLD_INSERT_LIBRARIES',
      'DyLd_Custom_Hook',
      'PYTHONINSPECT',
      'PYTHONSTARTUP',
      'PYTHONPATH',
      'PYTHONHOME',
      'NODE_OPTIONS',
      'NODE_PATH',
      'NODE_EXTRA_CA_CERTS',
      'SSL_CERT_FILE',
      'SSL_CERT_DIR',
      'HTTPS_PROXY',
      'ALL_PROXY',
      'ELECTRON_RUN_AS_NODE',
      'ELECTRON_NO_ASAR',
      'OPENSSL_CONF',
      'PSModulePath',
      'COR_ENABLE_PROFILING',
      'CORECLR_PROFILER_PATH',
      'DOTNET_STARTUP_HOOKS',
      'COMPlus_ReadyToRun',
      'JAVA_TOOL_OPTIONS',
    ]));
    expect(env).toEqual({ PATH: 'C:\\safe', ACE_SECURITY_MODE: 'managed' });
  });

  it('strips remote-debugging, Node injection, and sandbox-weakening arguments', () => {
    const argv = [
      'electron',
      '.',
      '--remote-debugging-port=9222',
      '--remote-debugging-address',
      '0.0.0.0',
      '--no-sandbox',
      '--disable-web-security',
      '--js-flags',
      '--allow-natives-syntax',
      '--renderer-cmd-prefix=/tmp/inject',
      '--browser-subprocess-path',
      '/tmp/fake-browser',
      '--disable-features=NetworkServiceSandbox,site-per-process',
      '--disable-gpu-sandbox',
      '--proxy-server=http://attacker.invalid:8080',
      '--user-data-dir',
      '/tmp/attacker-profile',
      '--load-extension=/tmp/attacker-extension',
      '--allow-file-access-from-files',
      '--require',
      '/tmp/hook.js',
      '--dev',
    ];

    const removed = stripDangerousArguments(argv);

    expect(argv).toEqual(['electron', '.', '--dev']);
    expect(removed).toEqual([
      'remote-debugging-port',
      'remote-debugging-address',
      'no-sandbox',
      'disable-web-security',
      'js-flags',
      'renderer-cmd-prefix',
      'browser-subprocess-path',
      'disable-features',
      'disable-gpu-sandbox',
      'proxy-server',
      'user-data-dir',
      'load-extension',
      'allow-file-access-from-files',
      'require',
    ]);
  });

  it('is idempotent and records names, never attacker-controlled values', () => {
    const env = {
      NODE_OPTIONS: '--require=/private/evil.js',
      ELECTRON_RUN_AS_NODE: '1',
    };
    const argv = ['crew-desktop', '--remote-debugging-pipe'];
    const execArgv = ['--inspect=0', '--trace-warnings'];
    const messages: string[] = [];

    const first = hardenElectronBootstrap({ env, argv, execArgv, log: messages.push.bind(messages) });
    const second = hardenElectronBootstrap({ env, argv, execArgv, log: messages.push.bind(messages) });

    expect(first.removedEnvironment).toEqual(['NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE']);
    expect(first.removedArguments).toEqual(['remote-debugging-pipe', 'inspect']);
    expect(second).toEqual({ removedEnvironment: [], removedArguments: [] });
    expect(execArgv).toEqual(['--trace-warnings']);
    expect(messages.join('\n')).toContain('NODE_OPTIONS');
    expect(messages.join('\n')).not.toContain('/private/evil.js');
  });

  it('re-sanitizes descendants while allowing a trusted explicit child override', () => {
    const inherited = {
      PATH: '/safe/bin',
      LD_PRELOAD: '/ambient/inject.so',
      NODE_OPTIONS: '--inspect',
      DYLD_INSERT_LIBRARIES: '/ambient/inject.dylib',
      PSModulePath: 'C:\\attacker\\modules',
      COR_ENABLE_PROFILING: '1',
      CORECLR_PROFILER_PATH: 'C:\\attacker\\profiler.dll',
      DOTNET_STARTUP_HOOKS: 'C:\\attacker\\startup-hook.dll',
      COMPlus_ReadyToRun: '0',
      JAVA_TOOL_OPTIONS: '-javaagent:C:\\attacker\\agent.jar',
    };

    const child = sanitizedChildProcessEnvironment(
      { CREW_HOME: '/crew', LD_PRELOAD: '/trusted/skill-only.so' },
      inherited,
    );

    expect(child).toEqual({
      PATH: '/safe/bin',
      CREW_HOME: '/crew',
      LD_PRELOAD: '/trusted/skill-only.so',
    });
    expect(inherited.LD_PRELOAD).toBe('/ambient/inject.so');
  });

  it('forces non-shell child options and strips ambient secret injection hooks', () => {
    const inherited = {
      PATH: '/safe/bin',
      NODE_OPTIONS: '--require=/tmp/steal.js',
      LD_PRELOAD: '/tmp/steal.so',
      API_TOKEN: 'ordinary-child-secret',
      AWS_ACCESS_KEY_ID: 'AKIAEXAMPLEACCESSKEY',
      GITHUB_PAT: 'github-pat-secret',
      SSL_CERT_FILE: '/tmp/attacker-ca.pem',
      HTTPS_PROXY: 'http://attacker.invalid:8080',
    };

    const options = hardenedChildProcessOptions(
      { cwd: '/trusted', detached: false },
      { CREW_HOME: '/crew' },
      inherited,
    );

    expect(options).toEqual({
      cwd: '/trusted',
      detached: false,
      shell: false,
      env: {
        PATH: '/safe/bin',
        CREW_HOME: '/crew',
      },
    });
    expect(() => hardenedChildProcessOptions({ shell: true })).toThrow(/shell/i);
    expect(() => hardenedChildProcessOptions({ env: { PATH: '/bypass' } })).toThrow(/env/i);
  });

  it('uses a pre-Electron bootstrap as the packaged main entry', async () => {
    const [
      packageJsonText,
      build,
      builder,
      bootstrap,
      main,
      gatewayInstanceAuth,
      uninstall,
    ] = await Promise.all([
      fs.readFile('package.json', 'utf8'),
      fs.readFile('esbuild.config.mjs', 'utf8'),
      fs.readFile('electron-builder.yml', 'utf8'),
      fs.readFile('src/main/bootstrap.cjs', 'utf8'),
      fs.readFile('src/main/index.ts', 'utf8'),
      fs.readFile('src/main/gateway-instance-auth.ts', 'utf8'),
      fs.readFile('src/main/uninstall.ts', 'utf8'),
    ]);
    const packageJson = JSON.parse(packageJsonText) as { main?: string };

    expect(packageJson.main).toBe('dist/main/bootstrap.js');
    expect(build).toContain("src/main/bootstrap.cjs");
    expect(build).toContain("src/main/bootstrap-hardening.cjs");
    const hardeningIndex = bootstrap.indexOf('hardenElectronBootstrap()');
    const modulePathResetIndex = bootstrap.indexOf('NodeModule._initPaths()');
    const electronImportIndex = bootstrap.indexOf("require('electron')");
    const applicationImportIndex = bootstrap.indexOf("require('./index.js')");
    expect(hardeningIndex).toBeLessThan(modulePathResetIndex);
    expect(modulePathResetIndex).toBeLessThan(electronImportIndex);
    expect(hardeningIndex).toBeLessThan(electronImportIndex);
    expect(electronImportIndex).toBeLessThan(applicationImportIndex);
    expect(bootstrap).toContain('app.commandLine.removeSwitch(name)');
    expect(bootstrap).toContain('app.enableSandbox()');
    expect(FORBIDDEN_CHROMIUM_SWITCHES).toEqual(expect.arrayContaining([
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
      'browser-subprocess-path',
      'js-flags',
      'renderer-cmd-prefix',
      'utility-cmd-prefix',
    ]));
    expect(main).toContain('contextIsolation: true');
    expect(main).toContain('sandbox: true');
    expect(main).toContain('sanitizedChildProcessEnvironment');
    expect(main).toContain('hardenedChildProcessOptions');
    expect(main).toContain("stdio: ['pipe', 'pipe', 'pipe']");
    expect(main).not.toContain('shell: true');
    expect(gatewayInstanceAuth).toContain('hardenedChildProcessOptions(');
    expect(gatewayInstanceAuth).toContain(
      "PSModulePath: path.join(path.dirname(powershell), 'Modules')",
    );
    expect(gatewayInstanceAuth).toContain('cwd: path.dirname(powershell)');
    expect(builder).toContain('runAsNode: false');
    expect(builder).toContain('enableNodeOptionsEnvironmentVariable: false');
    expect(builder).toContain('enableNodeCliInspectArguments: false');
    expect(builder).toContain('onlyLoadAppFromAsar: true');
    expect(builder).toContain('asar: true');
    expect(builder).toContain('security-runtime-bin/runtime-manifest.json');
    expect(main).toContain("app.getAppPath(), 'security-runtime-bin', 'runtime-manifest.json'");
    expect(main).toContain('ACE_DESKTOP_SECURITY_RUNTIME_MANIFEST_SHA256');
    expect(main).toContain('ACE_DESKTOP_SECURITY_RUNTIME_SHA256');
    expect(main).toContain('Keep stdin open as a parent-liveness lease');
    expect(main).toContain('child.stdin.write');
    expect(main).not.toContain("spawn('tasklist'");
    expect(main).not.toContain("spawn('pkill'");
    expect(uninstall).not.toContain("spawn('tasklist'");
    expect(uninstall).not.toContain("spawn('cmd.exe'");
    expect(uninstall).not.toContain("spawn('bash'");
    expect(uninstall).toContain("['-c', script, 'crew-uninstall', ...postQuitDirs]");
  });

  it('keeps every Electron window surface isolated and sandboxed', async () => {
    for (const relativePath of [
      'src/main/index.ts',
      'src/main/browser-host.ts',
      'src/main/host-authority-dialog.ts',
      'src/main/browser/automation-host.ts',
    ]) {
      const source = await fs.readFile(relativePath, 'utf8');
      expect(source, relativePath).toContain('contextIsolation: true');
      expect(source, relativePath).toContain('nodeIntegration: false');
      expect(source, relativePath).toContain('sandbox: true');
      expect(source, relativePath).not.toContain('contextIsolation: false');
      expect(source, relativePath).not.toContain('nodeIntegration: true');
      expect(source, relativePath).not.toContain('sandbox: false');
    }
  });
});
