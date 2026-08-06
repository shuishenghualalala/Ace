/**
 * playwright-core 升级边界的静态门禁。
 *
 * 真实行为由 scripts/pw-contract.ts 在 Electron 上验证；本脚本负责确保那条
 * 契约不会被依赖漂移、旁路 import 或打包配置悄悄绕开。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const compatPath = path.join(desktopRoot, 'src', 'main', 'browser', 'playwright-compat.ts');
const packagePath = path.join(desktopRoot, 'package.json');
const lockPath = path.join(desktopRoot, 'package-lock.json');
const esbuildPath = path.join(desktopRoot, 'esbuild.config.mjs');
const contractPath = path.join(desktopRoot, 'scripts', 'pw-contract.ts');

const failures = [];
const candidateVersion = process.env.CREW_PLAYWRIGHT_CANDIDATE_VERSION || '';

function fail(message) {
  failures.push(message);
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function isExactVersion(value) {
  return typeof value === 'string'
    && /^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$/.test(value);
}

const manifest = readJson(packagePath);
const lock = readJson(lockPath);
const coreVersion = manifest.dependencies?.['playwright-core'];
const testVersion = manifest.devDependencies?.['@playwright/test'];

if (!isExactVersion(coreVersion))
  fail(`dependencies.playwright-core 必须锁精确 semver，当前为 ${String(coreVersion)}`);
if (!isExactVersion(testVersion))
  fail(`devDependencies.@playwright/test 必须锁精确 semver，当前为 ${String(testVersion)}`);
if (coreVersion !== testVersion)
  fail(`playwright-core 与 @playwright/test 必须同版，当前为 ${coreVersion} / ${testVersion}`);
if (candidateVersion && !isExactVersion(candidateVersion))
  fail(`CREW_PLAYWRIGHT_CANDIDATE_VERSION 必须是精确 semver，当前为 ${candidateVersion}`);
const expectedInstalledVersion = candidateVersion || coreVersion;

const lockRoot = lock.packages?.[''];
if (lockRoot?.dependencies?.['playwright-core'] !== coreVersion)
  fail('package-lock 根依赖中的 playwright-core 与 package.json 不一致');
if (lockRoot?.devDependencies?.['@playwright/test'] !== testVersion)
  fail('package-lock 根依赖中的 @playwright/test 与 package.json 不一致');
const lockedCore = lock.packages?.['node_modules/playwright-core'];
const lockedTest = lock.packages?.['node_modules/@playwright/test'];
const lockedPlaywright = lock.packages?.['node_modules/playwright'];
if (lockedCore?.version !== coreVersion)
  fail('package-lock 实际 playwright-core 版本与 package.json 不一致');
if (lockedTest?.version !== testVersion)
  fail('package-lock 实际 @playwright/test 版本与 package.json 不一致');
if (lockedTest?.dependencies?.playwright !== testVersion)
  fail('package-lock 中 @playwright/test 依赖的 playwright 版本不一致');
if (lockedPlaywright?.version !== coreVersion)
  fail('package-lock 实际 playwright 版本与 playwright-core 不一致');
if (lockedPlaywright?.dependencies?.['playwright-core'] !== coreVersion)
  fail('package-lock 中 playwright 依赖的 playwright-core 版本不一致');

const installedPackage = path.join(desktopRoot, 'node_modules', 'playwright-core', 'package.json');
if (fs.existsSync(installedPackage)) {
  const installedVersion = readJson(installedPackage).version;
  if (installedVersion !== expectedInstalledVersion) {
    fail(
      `node_modules/playwright-core=${installedVersion}，与期望版本`
      + ` ${expectedInstalledVersion} 不一致；`
      + '请重新执行 npm ci',
    );
  }
}
const installedTestPackage = path.join(
  desktopRoot,
  'node_modules',
  '@playwright',
  'test',
  'package.json',
);
if (fs.existsSync(installedTestPackage)) {
  const installedTestVersion = readJson(installedTestPackage).version;
  if (installedTestVersion !== expectedInstalledVersion) {
    fail(
      `node_modules/@playwright/test=${installedTestVersion}，与期望版本`
      + ` ${expectedInstalledVersion} 不一致；`
      + '请重新执行 npm ci',
    );
  }
}
const installedPlaywrightPackage = path.join(
  desktopRoot,
  'node_modules',
  'playwright',
  'package.json',
);
if (fs.existsSync(installedPlaywrightPackage)) {
  const installedPlaywrightVersion = readJson(installedPlaywrightPackage).version;
  if (installedPlaywrightVersion !== expectedInstalledVersion) {
    fail(
      `node_modules/playwright=${installedPlaywrightVersion}，与期望版本`
      + ` ${expectedInstalledVersion} 不一致；`
      + '请重新执行 npm ci',
    );
  }
}

const sourceExtensions = new Set(['.ts', '.tsx', '.js', '.mjs', '.cjs']);
const importPattern =
  /(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)['"]([^'"]*playwright-core[^'"]*)['"]/g;

function scanTree(root) {
  if (!fs.existsSync(root)) return;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === 'dist' || entry.name === 'release') continue;
    // pw-contract.build.mjs / ax-probe.build.mjs 会生成同名 bundle；bundle 中保留
    // compat 的 external import 是预期行为，审查源文件即可。
    if (entry.name === 'pw-contract.mjs' || entry.name === 'ax-probe.mjs') continue;
    const file = path.join(root, entry.name);
    if (entry.isDirectory()) {
      scanTree(file);
      continue;
    }
    if (!entry.isFile() || !sourceExtensions.has(path.extname(entry.name))) continue;
    const source = fs.readFileSync(file, 'utf8');
    for (const match of source.matchAll(importPattern)) {
      const specifier = match[1];
      if (specifier.includes('/lib/') || specifier.startsWith('playwright-core/lib')) {
        fail(`${path.relative(desktopRoot, file)} 禁止引用 playwright-core/lib/**`);
        continue;
      }
      if (path.resolve(file) !== compatPath || specifier !== 'playwright-core') {
        fail(
          `${path.relative(desktopRoot, file)} 直接引用了 ${specifier}；`
          + '只允许 playwright-compat.ts 引用包根',
        );
      }
    }
  }
}

scanTree(path.join(desktopRoot, 'src'));
scanTree(path.join(desktopRoot, 'tests'));
scanTree(path.join(desktopRoot, 'scripts'));

const esbuild = fs.readFileSync(esbuildPath, 'utf8');
if (!/external\s*:\s*\[[^\]]*['"]playwright-core['"]/s.test(esbuild))
  fail('esbuild 主进程配置必须把 playwright-core 保持为 external');

const compat = fs.readFileSync(compatPath, 'utf8');
const contract = fs.readFileSync(contractPath, 'utf8');
if (!compat.includes('._selector')) fail('兼容层缺少 normalize() 底层 selector 私有字段');
if (!compat.includes('aria-ref=')) fail('兼容层缺少 aria-ref 私有选择器引擎');
if (
  !compat.includes('officialInjectedScriptSource')
  || !compat.includes("require.resolve('playwright-core/lib/coreBundle')")
  || !compat.includes('packages/playwright-core/src/generated/injectedScriptSource.ts')
  || !compat.includes('generateSelectorSimple(')
) {
  fail('兼容层缺少官方 InjectedScript/selectorGenerator 的结构化提取边界');
}
if (
  !contract.includes('toStableSelector')
  || !contract.includes("persisted.includes('aria-ref')")
) {
  fail('真实 Electron 契约未覆盖 normalize() 底层 selector 的可持久化行为');
}
if (!contract.includes('locatorFromRef'))
  fail('真实 Electron 契约未覆盖 aria-ref 私有选择器引擎');
if (
  !contract.includes('selectorFor?.(')
  || !contract.includes("selectorSource: 'playwright'")
  || !contract.includes('recordedFrameSelector')
) {
  fail('真实 Electron 契约未覆盖官方 recorder selector 与 frame selector');
}
for (const [needle, description] of [
  ['connectOverCdp', '进程内 transport 握手'],
  ['setAutomationMode', 'AI/human 焦点模拟切换'],
  ['newCDPSession', 'CDP alias 会话'],
]) {
  if (!contract.includes(needle)) fail(`真实 Electron 契约未覆盖 ${description}`);
}

if (failures.length) {
  console.error('Playwright 依赖边界检查失败：');
  for (const message of failures) console.error(`- ${message}`);
  process.exitCode = 1;
} else {
  const suffix = candidateVersion ? `，候选升级 ${candidateVersion}` : '';
  console.log(`Playwright 依赖边界通过（锁定 ${coreVersion}${suffix}）`);
}
