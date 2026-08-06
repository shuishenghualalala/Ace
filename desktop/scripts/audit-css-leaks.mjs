/**
 * audit-css-leaks.mjs
 *
 * Scans the assets/styles CSS tree for hardcoded color literals
 * (hex like #fff / #ffffff and rgba(...) / rgb(...)) that should
 * eventually be tokenised to `var(--color-*)`.
 *
 * Pure Node, no external deps. Safe to run anywhere.
 *
 * Usage:
 *   node scripts/audit-css-leaks.mjs                 # scan default assets/styles
 *   node scripts/audit-css-leaks.mjs path/to/dir     # scan a custom dir
 *   node scripts/audit-css-leaks.mjs --json          # machine-readable output
 *
 * Exit code: 0 by default (advisory tool); --strict fails on any leak.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { basename, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HEX_PATTERN = /#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/g;
const RGB_PATTERN = /\brgba?\s*\([^)]+\)/g;

/**
 * Walk a directory recursively, returning all regular file paths.
 * Symlinks are not followed to avoid loops.
 *
 * @param {string} dir absolute directory path
 * @returns {string[]} absolute file paths
 */
export function walkCssFiles(dir) {
  /** @type {string[]} */
  const out = [];
  /** @type {string[]} */
  const stack = [dir];
  while (stack.length > 0) {
    const cur = stack.pop();
    let entries;
    try {
      entries = readdirSync(cur);
    } catch {
      continue;
    }
    for (const name of entries) {
      const full = join(cur, name);
      let st;
      try {
        st = statSync(full);
      } catch {
        continue;
      }
      if (st.isDirectory()) {
        stack.push(full);
      } else if (st.isFile() && full.endsWith('.css')) {
        out.push(full);
      }
    }
  }
  out.sort();
  return out;
}

/**
 * Audit a single CSS file for hardcoded color literals.
 *
 * Lines inside `:root` or `var(--*)` definition blocks are not
 * filtered here — this is a raw report, downstream tooling decides
 * what to do.
 *
 * @param {string} filePath absolute path
 * @returns {{ path: string, hex: number, rgb: number, samples: string[] }}
 */
export function auditFile(filePath) {
  const text = readFileSync(filePath, 'utf8');
  /** @type {string[]} */
  const samples = [];
  let hexCount = 0;
  let rgbCount = 0;

  // Strip comments to avoid false positives (e.g. /* uses #fff */).
  const stripped = text.replace(/\/\*[\s\S]*?\*\//g, '');

  for (const m of stripped.matchAll(HEX_PATTERN)) {
    hexCount += 1;
    if (samples.length < 5) samples.push(m[0]);
  }
  for (const m of stripped.matchAll(RGB_PATTERN)) {
    rgbCount += 1;
    if (samples.length < 10) samples.push(m[0]);
  }

  return { path: filePath, hex: hexCount, rgb: rgbCount, samples };
}

/**
 * Audit all feature CSS files under `rootDir`. `tokens.css` is intentionally
 * excluded: primitive literals belong in the token layer; this audit checks
 * that component and feature styles consume those tokens.
 *
 * @param {string} rootDir
 * @returns {{
 *   summary: { totalFiles: number, filesWithLeaks: number, totalHex: number, totalRgb: number },
 *   files: ReturnType<typeof auditFile>[]
 * }}
 */
export function auditCssLeaks(rootDir) {
  const abs = resolve(rootDir);
  const files = walkCssFiles(abs).filter((file) => basename(file) !== 'tokens.css');
  const reports = files.map(auditFile);
  const totalHex = reports.reduce((s, r) => s + r.hex, 0);
  const totalRgb = reports.reduce((s, r) => s + r.rgb, 0);
  const filesWithLeaks = reports.filter((r) => r.hex + r.rgb > 0).length;

  return {
    summary: {
      totalFiles: reports.length,
      filesWithLeaks,
      totalHex,
      totalRgb,
    },
    files: reports.map((r) => ({
      ...r,
      path: relative(process.cwd(), r.path),
    })),
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
  const json = args.includes('--json');
  const strict = args.includes('--strict');
  const target = args.find((a) => !a.startsWith('--')) ?? 'assets/styles';
  const report = auditCssLeaks(target);

  if (json) {
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  } else {
    process.stdout.write(`CSS leak audit — ${report.summary.totalFiles} files\n`);
    process.stdout.write(`  total hex literals : ${report.summary.totalHex}\n`);
    process.stdout.write(`  total rgb/rgba     : ${report.summary.totalRgb}\n`);
    process.stdout.write(`  files with leaks   : ${report.summary.filesWithLeaks}\n\n`);
    for (const f of report.files) {
      if (f.hex + f.rgb === 0) continue;
      process.stdout.write(
        `  ${f.path.padEnd(36)} hex=${String(f.hex).padStart(3)} rgb=${String(f.rgb).padStart(3)}  samples: ${f.samples.join(', ')}\n`,
      );
    }
  }

  if (strict && report.summary.totalHex + report.summary.totalRgb > 0) {
    process.exitCode = 1;
  }
}
