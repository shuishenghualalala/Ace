/**
 * audit-font-sizes.mjs
 *
 * Scans the assets/styles CSS tree and classifies every
 * `font-size: NNpx` declaration into one of three buckets:
 *   - shouldUseVar    (~80%): NN is one of {10..56} → `var(--font-*)`
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
 * Sourced from variables.css `--font-*` scale at base-font-size = 14.
 */
const COMMON_SIZES = {
  10: '--font-xs',
  11: '--font-xs',
  12: '--font-sm',
  13: '--font-md',
  14: '--font-base',
  15: '--font-lg',
  16: '--font-lg',
  18: '--font-xl',
  20: '--font-2xl',
  21: '--font-2xl',
  24: '--font-3xl',
  28: '--font-3xl',
  30: '--font-4xl',
  36: '--font-4xl',
  40: '--font-5xl',
  56: '--font-5xl',
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

if (isCli()) {
  const args = process.argv.slice(2);
  const target = args.find((a) => !a.startsWith('--')) ?? 'assets/styles';
  const report = auditFontSizes(target);

  process.stdout.write(`font-size audit — ${report.summary.total} literals\n`);
  process.stdout.write(`  should use --font-* : ${report.summary.shouldUseVar}\n`);
  process.stdout.write(`  should use calc()   : ${report.summary.shouldUseCalc}\n`);
  process.stdout.write(`  must keep literal  : ${report.summary.mustKeepLiteral}\n\n`);

  for (const f of report.byFile.slice(0, 10)) {
    process.stdout.write(`  ${f.path}  (${f.items.length})\n`);
  }
  if (report.byFile.length > 10) {
    process.stdout.write(`  … and ${report.byFile.length - 10} more files\n`);
  }
}
