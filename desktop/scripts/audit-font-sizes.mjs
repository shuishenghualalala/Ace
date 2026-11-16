/**
 * audit-font-sizes.mjs
 *
 * Scans the assets/styles CSS tree and classifies every
 * `font-size: NNpx` declaration into one of three buckets:
 *   - shouldUseVar    (~80%): NN is one of {10..56} → `var(--mw-font-*)`
 *   - shouldUseCalc   (~15%): other integer that maps to a calc() expression
 *   - mustKeepLiteral (~5%):  rare / sub-pixel / icon-only sizes
 *
 * Pure Node, no external deps.
 *
 * Usage:
 *   node scripts/audit-font-sizes.mjs
 *   node scripts/audit-font-sizes.mjs path/to/dir
 */

import { readFileSync } from 'node:fs';
import { resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { walkCssFiles } from './audit-css-leaks.mjs';

const FONT_SIZE_PATTERN = /font-size\s*:\s*(\d+(?:\.\d+)?)px/g;

/**
 * Canonical mapping: integer px size → recommended token name.
 * 不变式：每一项都必须与 tokens.css 的 `--mw-font-*` 实际取值一致，代表
 * 「换成该 token 后像素不变」。标尺之外的整数（15/17/19/21/22/28/36/56…）
 * 一律 shouldUseCalc——需要在 tokens.css 增加 calc() 语义档位，不允许就近取整
 * （就近取整会改字号：15px 曾在此表里被指向 16px 的 --mw-font-lg）。
 */
const COMMON_SIZES = {
  9: '--mw-font-icon',
  10: '--mw-font-xxs',
  11: '--mw-font-xs',
  12: '--mw-font-sm',
  13: '--mw-font-md',
  14: '--mw-font-base',
  16: '--mw-font-lg',
  18: '--mw-font-xl',
  20: '--mw-font-2xl',
  24: '--mw-font-3xl',
  30: '--mw-font-4xl',
  40: '--mw-font-5xl',
};

/**
 * Classify a single `font-size: NNpx` declaration.
 *
 * @param {number} px
 * @returns {'shouldUseVar' | 'shouldUseCalc' | 'mustKeepLiteral'}
 */
export function classifyFontSize(px) {
  if (Object.prototype.hasOwnProperty.call(COMMON_SIZES, px)) {
    return 'shouldUseVar';
  }
  // Calc-friendly integer (1-100): any other common int. Sub-pixel → keep.
  if (Number.isInteger(px) && px > 0 && px <= 100) {
    return 'shouldUseCalc';
  }
  return 'mustKeepLiteral';
}

/**
 * Audit all CSS files under `rootDir` for `font-size: NNpx` literals.
 *
 * @param {string} rootDir
 * @returns {{
 *   summary: { total: number, shouldUseVar: number, shouldUseCalc: number, mustKeepLiteral: number },
 *   byFile: { path: string, items: { value: number, classification: 'shouldUseVar' | 'shouldUseCalc' | 'mustKeepLiteral', token?: string }[] }[]
 * }}
 */
export function auditFontSizes(rootDir) {
  const abs = resolve(rootDir);
  const files = walkCssFiles(abs);
  /** @type {{ path: string, items: { value: number, classification: 'shouldUseVar' | 'shouldUseCalc' | 'mustKeepLiteral', token?: string }[] }[]} */
  const byFile = [];

  let total = 0;
  let sVar = 0;
  let sCalc = 0;
  let sKeep = 0;

  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    // Strip comments to avoid false positives.
    const stripped = text.replace(/\/\*[\s\S]*?\*\//g, '');
    const items = [];
    for (const m of stripped.matchAll(FONT_SIZE_PATTERN)) {
      const px = Number(m[1]);
      const classification = classifyFontSize(px);
      total += 1;
      if (classification === 'shouldUseVar') sVar += 1;
      else if (classification === 'shouldUseCalc') sCalc += 1;
      else sKeep += 1;
      items.push({
        value: px,
        classification,
        ...(classification === 'shouldUseVar' ? { token: COMMON_SIZES[px] } : {}),
      });
    }
    if (items.length > 0) {
      byFile.push({ path: relative(process.cwd(), file), items });
    }
  }

  return {
    summary: {
      total,
      shouldUseVar: sVar,
      shouldUseCalc: sCalc,
      mustKeepLiteral: sKeep,
    },
    byFile,
  };
}

/* ── CLI entry ──────────────────────────────────────────────── */

function isCli() {
  if (typeof process === 'undefined' || !process.argv[1]) return false;
  try {
    return fileURLToPath(import.meta.url) === resolve(process.argv[1]);
  } catch {
    return false;
  }
}

/**
 * strict 门禁只卡「可动作」档位。
 *
 * `mustKeepLiteral` 按本脚本文档的定义就是允许保留的（亚像素 / 图标专用字号），
 * 把它也算进失败条件会让唯一通过态变成「0 个字面量」，与文档自相矛盾。
 */
export function hasActionableFontSizes(report) {
  return report.summary.shouldUseVar > 0 || report.summary.shouldUseCalc > 0;
}

if (isCli()) {
  const args = process.argv.slice(2);
  const strict = args.includes('--strict');
  const target = args.find((a) => !a.startsWith('--')) ?? 'assets/styles';
  const report = auditFontSizes(target);

  process.stdout.write(`font-size audit — ${report.summary.total} literals\n`);
  process.stdout.write(`  should use --mw-font-* : ${report.summary.shouldUseVar}\n`);
  process.stdout.write(`  should use calc()   : ${report.summary.shouldUseCalc}\n`);
  process.stdout.write(`  must keep literal  : ${report.summary.mustKeepLiteral}\n\n`);

  for (const f of report.byFile.slice(0, 10)) {
    process.stdout.write(`  ${f.path}  (${f.items.length})\n`);
  }
  if (report.byFile.length > 10) {
    process.stdout.write(`  … and ${report.byFile.length - 10} more files\n`);
  }

  if (strict && hasActionableFontSizes(report)) {
    process.exitCode = 1;
  }
}
