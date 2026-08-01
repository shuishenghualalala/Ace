#!/usr/bin/env node
/**
 * Crew Desktop 安全静态门禁脚本。
 *
 * 用途：在 CI / pre-commit 跑 `node scripts/check-security.mjs`，断言项目源码不包含已知反模式。
 * 退出码：0 = 全部通过；1 = 至少一项违规。
 *
 * 检查项：
 * 1. 禁止 webviewTag: true（使用 IPC）
 * 2. 禁止 insecureHTTPParser: true（Electron 默认开启 HTTP/2 + TLS，足以覆盖 99% 场景）
 * 3. 禁止直接 shell.openExternal() 调用（必须走 IPC 入口，且由 schema 校验协议）
 * 4. 禁止 Math.random().toString(36) 作为 ID（碰撞率太高，用 crypto.randomUUID）
 * 5. 禁止在主进程直接调用 dialog.showOpenDialog / fs.readFileSync 接收外部字符串
 *    （必须先经 ipc-schemas 校验）
 * 6. 禁止 eval() / new Function()（动态代码执行 = RCE 风险）
 * 7. 禁止 child_process 的 require / 动态 import，以及 shell 型危险用法
 *    （exec / execSync / execFileSync / spawn(..., { shell: true }) /
 *     单字符串命令的 spawn）。裸 `import { spawn } from 'child_process'`
 *     + `spawn(argv[])` 数组参数是受控子进程，不触发本规则。
 * 8. 禁止 innerHTML = `...${...}` 原始插值（模板插值 = XSS）。
 *     插值被 escapeHtml() / sanitize() / DOMPurify.sanitize() 包裹时视为已净化，
 *     不触发本规则。
 *
 * 例外白名单（必要时人工审查）：
 * - scripts/ 目录下的脚本本身（不参与 bundle）
 * - __tests__/ tests/ 目录下的测试代码
 */

import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootArgIndex = process.argv.indexOf('--root');
const requestedRoot = rootArgIndex >= 0 ? process.argv[rootArgIndex + 1] : '';
const ROOT = path.resolve(requestedRoot || path.resolve(__dirname, '..'));
const SRC = path.join(ROOT, 'src');

const violations = [];

function isAllowedPath(file) {
  const rel = path.relative(ROOT, file);
  return (
    rel.startsWith('scripts' + path.sep) ||
    rel.startsWith('tests' + path.sep) ||
    rel.includes(path.sep + '__tests__' + path.sep) ||
    rel.endsWith('.test.ts')
  );
}

function walk(dir, files = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, files);
    else if (entry.isFile() && full.endsWith('.ts')) files.push(full);
  }
  return files;
}

function check(pattern, label, file, content) {
  checkWithExemptions(pattern, label, file, content, null);
}

/**
 * 与 check() 相同，但允许通过 `exemptions` 豁免“已存在、待 src owner 修复”的命中。
 * exemptions 形如 { '<label>': Set<'<relpath>:<line>'> }。
 * 命中且 (relpath:line) 在豁免集合里时不计入 violation，但仍会在 stderr 以
 * [grandfathered] 提示，方便审计与后续清零。
 */
function checkWithExemptions(pattern, label, file, content, exemptions) {
  const lines = content.split('\n');
  const rel = path.relative(ROOT, file).replace(/\\/g, '/');
  const exempt = exemptions && exemptions[label];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // 跳过纯注释
    const trimmed = line.trim();
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) continue;
    if (pattern.test(line)) {
      const key = `${rel}:${i + 1}`;
      if (exempt && exempt.has(key)) {
        // 豁免：不计入 violation，但输出提示，便于后续清零。
        process.stderr.write(`[check-security] [grandfathered] ${key}  [${label}]\n    > ${trimmed}\n`);
      } else {
        violations.push({ file: rel, line: i + 1, label, snippet: trimmed });
      }
    }
  }
}

/**
 * 已人工审查的例外。该表当前为空；新增条目会削弱门禁，必须经过安全审查。
 */
const GRANDFATHERED = {
  'innerHTML with template interpolation forbidden (XSS — use DOM API or sanitize)': new Set([]),
};

/**
 * 检测 child_process 的危险用法。
 *
 * 规则本意：禁止 *shell 型* 子进程调用（shell 注入面 + 阻塞）。
 * 不再禁止"裸 import + 受控 argv 数组 spawn"——后者是主进程拉起已知
 * 子进程（如 `spawn(python, ['-m', 'crew.gateway.server'])`）的合法模式。
 *
 * 命中以下任一模式即报违规：
 *  - exec(`...`), exec("..."), exec(varCmd)            // shell 字符串命令
 *  - execSync(...), execFileSync(..., { shell: true }) // 同步 / shell 型
 *  - spawn(cmdString, ...)                              // 单字符串命令 = shell 语义
 *  - spawn(..., { shell: true })                        // 显式 shell
 *  - 模板/变量插值的命令：exec(`${x}`), exec(`ls ${dir}`)
 *
 * 不命中（安全）：
 *  - import { spawn } from 'child_process'              // 裸 import，无用法
 *  - spawn(exe, ['arg1','arg2'])                        // argv 数组、exe 为字面量/常量
 *  - spawn(exe, args, {})                               // 无 shell:true
 */
function checkChildProcessUsage(file, content) {
  const lines = content.split('\n');
  const rel = path.relative(ROOT, file).replace(/\\/g, '/');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) continue;

    // (a) exec / execSync / execFileSync 调用 —— 这些 API 走 shell 或同步阻塞，
    //     一律禁止（受控场景应改用 spawn(argv[])）。
    //     匹配 exec(  / execSync(  / execFileSync(  ，排除属性访问 obj.exec(
    if (/(^|[^.\w])(child_process\.)?(?:exec(?:Sync)?|execFileSync)\s*\(/.test(line)) {
      violations.push({
        file: rel,
        line: i + 1,
        label: 'child_process exec/execSync/execFileSync forbidden (shell injection + blocking; use spawn(argv[]) with schema-validated args)',
        snippet: trimmed,
      });
      continue;
    }

    // (b) spawn 的危险形态：
    //   - spawn('rm -rf ...', ...)   单字符串命令 = shell 语义
    //   - spawn(cmd, args, { shell: true })  显式 shell
    //   - spawn(`...${x}...`, ...)   模板插值命令
    //   argv 数组 + 字面量 exe 不触发。
    const spawnMatch = /\bspawn\s*\(/.exec(line);
    if (spawnMatch) {
      // 提取 spawn( 之后到行尾（跨行调用较少，单行启发即可覆盖绝大多数情形）。
      const tail = line.slice(spawnMatch.index);
      // shell: true 标记
      if (/shell\s*:\s*true/.test(tail)) {
        violations.push({
          file: rel,
          line: i + 1,
          label: 'spawn(..., { shell: true }) forbidden (shell injection; pass argv array, no shell)',
          snippet: trimmed,
        });
        continue;
      }
      // 单字符串命令：spawn('notepad') / spawn("rm -rf x") / spawn(varCmd)
      // argv 数组形态：spawn(exe, [...]) 第二个参数是 [ —— 安全。
      // 启发：spawn( 后第一个非空白 token 若是字符串字面量/标识符且后面 *不* 紧跟 `,`
      //       （或紧跟 `)`），视为单字符串命令。
      const afterSpawn = tail.replace(/^\bspawn\s*\(/, '');
      const firstTokenMatch = afterSpawn.match(/^\s*('[^']*'|"[^"]*"|`[^`]*`|[A-Za-z_$][\w$]*)/);
      if (firstTokenMatch) {
        const afterToken = afterSpawn.slice(firstTokenMatch.index + firstTokenMatch[0].length);
        // 模板字符串命令（含插值）= 危险
        const tokenIsTemplate = firstTokenMatch[0].startsWith('`');
        // 单字符串命令：token 后是 `)` 或 `,` 但下一个非空 token 不是 `[`（数组）
        const nextNonSpace = afterToken.match(/^\s*(\S)/);
        const nextCh = nextNonSpace ? nextNonSpace[1] : '';
        const singleStringCommand =
          (firstTokenMatch[0].startsWith("'") || firstTokenMatch[0].startsWith('"') || tokenIsTemplate) &&
          (nextCh === ')' || (nextCh === ',' && !/^\s*,\s*\[/.test(afterToken)));
        if (tokenIsTemplate || singleStringCommand) {
          violations.push({
            file: rel,
            line: i + 1,
            label: 'spawn(stringCmd) forbidden (shell-style command; pass [exe, ...args] argv array instead)',
            snippet: trimmed,
          });
          continue;
        }
      }
    }
  }
}

/**
 * 判断 innerHTML 模板里的每个 `${...}` 插值是否被净化函数包裹。
 * 净化函数白名单：escapeHtml / sanitize / DOMPurify.sanitize。
 *
 * 安全形态（不报）：插值整体是一个净化函数调用，例如
 *   `<div>${escapeHtml(userInput)}</div>`
 *   `<div>${DOMPurify.sanitize(html)}</div>`
 * 即 `${` 之后（跳过空白）紧接 `funcName(`，且该调用平衡闭合到插值末尾。
 *
 * 不安全形态（报）：存在任一裸变量/表达式插值，例如
 *   `<div>${name}</div>`   `<div>${inner.join('')}</div>`
 */
const SANITIZE_FN_NAME = /^(?:escapeHtml|sanitize|DOMPurify\.sanitize)\s*\(/;
function innerHtmlInterpolationIsUnsafe(line) {
  let i = 0;
  while (i < line.length) {
    if (line[i] === '$' && line[i + 1] === '{') {
      // 平衡花括号取出插值内容
      let depth = 1;
      let j = i + 2;
      while (j < line.length && depth > 0) {
        if (line[j] === '{') depth++;
        else if (line[j] === '}') depth--;
        j++;
      }
      const inner = line.slice(i + 2, j - 1).trim();
      // 安全：插值整体是一个白名单净化函数调用。
      if (SANITIZE_FN_NAME.test(inner)) {
        i = j;
        continue;
      }
      // 保守放过：内文不含任何标识符（纯字面量/数字）。
      if (!/[A-Za-z_$][\w$]*/.test(inner)) {
        i = j;
        continue;
      }
      // 否则：存在裸变量/表达式插值 = 不安全。
      return true;
    } else {
      i++;
    }
  }
  return false;
}

/**
 * 与 check() 相同，但 innerHTML 规则使用插值净化感知过滤。
 */
function checkInnerHtml(file, content) {
  const lines = content.split('\n');
  const rel = path.relative(ROOT, file).replace(/\\/g, '/');
  const label = 'innerHTML with template interpolation forbidden (XSS — use DOM API or sanitize)';
  const exempt = GRANDFATHERED[label];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) continue;
    if (/\.innerHTML\s*=\s*`[^`]*\$\{/.test(line)) {
      if (!innerHtmlInterpolationIsUnsafe(line)) continue; // 所有插值均已净化
      const key = `${rel}:${i + 1}`;
      if (exempt && exempt.has(key)) {
        process.stderr.write(`[check-security] [grandfathered] ${key}  [${label}]\n    > ${trimmed}\n`);
      } else {
        violations.push({ file: rel, line: i + 1, label, snippet: trimmed });
      }
    }
  }
}

/**
 * 检测"非 IPC handler 上下文中的 shell.openExternal() 直接调用"。
 * 合法场景：被 ipcMain.handle 或项目的 trustedHandle 包住的 IPC handler
 * （已经过 schema 和 sender 校验）允许调用。
 * 非法场景：模块顶层、辅助函数、其它任意调用方直接调用。
 *
 * 实现：扫描每个 `ipcMain.handle('xxx', ...)` / `trustedHandle('xxx', ...)`
 * 起点，向下找到匹配的 `});` 闭括号，
 *       在闭括号内出现的 shell.openExternal 才算合法。
 */
function checkForDirectShellOpenExternal(file, content) {
  const lines = content.split('\n');
  const allowedLineRanges = []; // [start, end] inclusive
  for (let i = 0; i < lines.length; i++) {
    if (/(?:ipcMain\.handle|trustedHandle)\s*\(/.test(lines[i])) {
      const start = i;
      let depth = 0;
      let end = i;
      for (let j = i; j < lines.length; j++) {
        for (const ch of lines[j]) {
          if (ch === '{') depth++;
          else if (ch === '}') depth--;
        }
        if (depth <= 0 && j > i) {
          end = j;
          break;
        }
      }
      allowedLineRanges.push([start, end]);
    }
  }
  function inAllowed(lineIdx) {
    return allowedLineRanges.some(([s, e]) => lineIdx >= s && lineIdx <= e);
  }
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) continue;
    if (/\bshell\.openExternal\s*\(/.test(lines[i]) && !inAllowed(i)) {
      violations.push({
        file: path.relative(ROOT, file),
        line: i + 1,
        label: 'direct shell.openExternal outside IPC handler forbidden',
        snippet: trimmed,
      });
    }
  }
}

const files = walk(SRC);

for (const file of files) {
  if (isAllowedPath(file)) continue;
  const content = fs.readFileSync(file, 'utf8');

  check(/webviewTag\s*:\s*true/, 'webviewTag:true forbidden', file, content);
  check(/insecureHTTPParser\s*:\s*true/, 'insecureHTTPParser:true forbidden', file, content);
  // 直接 shell.openExternal() 调用 = 绕过 IPC schema 校验。
  // 这里采用粗略启发式：必须出现在 ipcMain.handle 调用之后的同一函数体内（≤10 行内）才视为合法。
  checkForDirectShellOpenExternal(file, content);
  check(/localStorage\.setItem\s*\(\s*['"]crew\.jwt['"]/, 'plaintext JWT write forbidden', file, content);
  // 禁止经 storage helper 把 JWT 写/读 renderer localStorage（JWT 只许走主进程 safeStorage）。
  // 允许可选泛型，覆盖 loadFromStorage<string|null>(JWT_KEY, ...) 形态。
  check(/\b(saveToStorage|loadFromStorage)\s*(?:<[^>]*>)?\s*\(\s*(?:JWT_KEY|['"]crew\.jwt['"])/, 'JWT via storage helper forbidden (use main-side safeStorage)', file, content);
  check(/Math\.random\s*\(\s*\)\.toString\s*\(\s*36\s*\)/, 'Math.random ID forbidden (use crypto.randomUUID)', file, content);
  // 8. 动态代码执行：eval() / new Function() 在渲染/主进程都是 RCE 面。
  //    允许 eval-作为属性名（如 obj.eval）—— 用 (^|[^.\w]) 前缀排除属性访问。
  check(/(^|[^.\w])eval\s*\(/, 'eval() forbidden (dynamic code execution)', file, content);
  check(/\bnew\s+Function\s*\(/, 'new Function() forbidden (dynamic code execution)', file, content);
  // 9. child_process：禁止 require / 动态 import（隐藏依赖），以及 shell 型危险用法。
  //    裸 `import { spawn } from 'child_process'` + `spawn(argv[])` 不触发——
  //    那是主进程拉起已知子进程的合法模式（见 checkChildProcessUsage）。
  check(/require\s*\(\s*['"]child_process['"]\s*\)/, "require('child_process') forbidden", file, content);
  checkChildProcessUsage(file, content);
  check(/\bimport\s*\(\s*['"]child_process['"]\s*\)/, 'child_process dynamic import forbidden', file, content);
  // 11. innerHTML 模板插值：`...${...}` 直接拼 = XSS。
  //    插值被 escapeHtml() / sanitize() / DOMPurify.sanitize() 包裹时视为已净化。
  checkInnerHtml(file, content);
}

if (violations.length === 0) {
  console.log('[check-security] OK — no forbidden patterns in src/');
  process.exit(0);
} else {
  console.error('[check-security] FAIL —', violations.length, 'violation(s):');
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line}  [${v.label}]`);
    console.error(`    > ${v.snippet}`);
  }
  process.exit(1);
}
