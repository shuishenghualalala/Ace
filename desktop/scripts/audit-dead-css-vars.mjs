/**
 * audit-dead-css-vars.mjs
 *
 * Scans the assets/styles CSS tree and src TS code for
 * `var(--X, fallback)` references where --X is not defined in the
 * token layer or by a component owner. Local component variables are valid
 * contracts; unresolved legacy aliases are not.
 *
 * The script only flags unfulfilled references (where --X is not
 * defined anywhere). It does NOT flag references intended to cascade
 * (e.g. `var(--bg, var(--bg1))`).
 *
 * Pure Node, no external deps.
 *
 * Usage:
 *   node scripts/audit-dead-css-vars.mjs
 *   node scripts/audit-dead-css-vars.mjs --json
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { resolve, relative, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { walkCssFiles } from './audit-css-leaks.mjs';

const VAR_REF_PATTERN = /var\(\s*(--[a-zA-Z0-9_-]+)\s*(?:,\s*([^)]*))?\s*\)/g;
const VAR_DEF_PATTERN = /(--[a-zA-Z0-9_-]+)\s*:/g;
const TS_VAR_DEF_PATTERN = /setProperty\(\s*['"](--[a-zA-Z0-9_-]+)['"]/g;
const RUNTIME_CONTRACT_PREFIX = '--mw-runtime-';

/**
 * Extract the set of CSS variable definitions in `text`.
 *
 * @param {string} text
 * @returns {Set<string>}
 */
export function parseDefinedVariables(text) {
  const defined = new Set();
  for (const m of text.matchAll(VAR_DEF_PATTERN)) {
    defined.add(m[1]);
  }
  return defined;
}

/**
 * Find all `var(--X, fallback)` references in `text`, line-numbered.
 *
 * @param {string} text
 * @returns {{ name: string, fallback: string | undefined, line: number, column: number }[]}
 */
export function findVarReferences(text) {
  /** @type {{ name: string, fallback: string | undefined, line: number, column: number }[]} */
  const out = [];
  // Build a quick line index so we can convert offsets to (line, column).
  const lineStarts = [0];
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) lineStarts.push(i + 1);
  }
  for (const m of text.matchAll(VAR_REF_PATTERN)) {
    const offset = m.index ?? 0;
    // Binary search for the line.
    let lo = 0;
    let hi = lineStarts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (lineStarts[mid] <= offset) lo = mid;
      else hi = mid - 1;
    }
    const line = lo + 1;
    const column = offset - lineStarts[lo] + 1;
    const fallback = m[2] ? m[2].trim() : undefined;
    out.push({ name: m[1], fallback, line, column });
  }
  return out;
}

/**
 * Audit all CSS + TS files for dead `var(--X)` references.
 *
 * @param {{ stylesDir: string, srcDir: string, variablesPath: string }} options
 * @returns {{
 *   summary: { totalRefs: number, deadRefs: number, filesWithDead: number },
 *   refs: { file: string, line: number, column: number, name: string, fallback?: string }[]
 * }}
 */
export function auditDeadCssVars({ stylesDir, srcDir, variablesPath }) {
  const cssFiles = walkCssFiles(resolve(stylesDir));
  const defined = parseDefinedVariables(readFileSync(variablesPath, 'utf8'));
  for (const file of cssFiles) {
    for (const name of parseDefinedVariables(readFileSync(file, 'utf8'))) defined.add(name);
  }
  // Restrict TS walk to ui + main (skip tests / node_modules).
  const tsFiles = walkTsFiles(resolve(srcDir));
  for (const file of tsFiles) {
    const text = readFileSync(file, 'utf8');
    for (const match of text.matchAll(TS_VAR_DEF_PATTERN)) defined.add(match[1]);
  }

  /** @type {typeof auditDeadCssVars extends (o: any) => infer R ? R extends { refs: infer U } ? U : never : never} */
  const refs = [];
  let totalRefs = 0;
  const filesWithDead = new Set();

  for (const file of [...cssFiles, ...tsFiles]) {
    const text = readFileSync(file, 'utf8');
    for (const r of findVarReferences(text)) {
      totalRefs += 1;
      if (!defined.has(r.name) && !r.name.startsWith(RUNTIME_CONTRACT_PREFIX)) {
        refs.push({
          file: relative(process.cwd(), file),
          line: r.line,
          column: r.column,
          name: r.name,
          ...(r.fallback ? { fallback: r.fallback } : {}),
        });
        filesWithDead.add(file);
      }
    }
  }

  return {
    summary: {
      totalRefs,
      deadRefs: refs.length,
      filesWithDead: filesWithDead.size,
    },
    refs,
  };
}

/**
 * Recursively walk a directory for .ts files (excluding node_modules / dist).
 *
 * @param {string} dir
 * @returns {string[]}
 */
export function walkTsFiles(dir) {
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
      if (name === 'node_modules' || name === 'dist' || name === '.git') continue;
      const full = join(cur, name);
      let st;
      try {
        st = statSync(full);
      } catch {
        continue;
      }
      if (st.isDirectory()) {
        stack.push(full);
      } else if (st.isFile() && (full.endsWith('.ts') || full.endsWith('.tsx'))) {
        out.push(full);
      }
    }
  }
  out.sort();
  return out;
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
  const report = auditDeadCssVars({
    stylesDir: 'assets/styles',
    srcDir: 'src',
    variablesPath: 'assets/styles/tokens.css',
  });

  if (json) {
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  } else {
    process.stdout.write(
      `dead CSS var audit — ${report.summary.deadRefs} of ${report.summary.totalRefs} refs are dead (${report.summary.filesWithDead} files)\n\n`,
    );
    for (const r of report.refs) {
      process.stdout.write(
        `  ${r.file}:${r.line}  ${r.name}${r.fallback ? `  (fallback: ${r.fallback})` : ''}\n`,
      );
    }
  }

  if (strict && report.summary.deadRefs > 0) {
    process.exitCode = 1;
  }
}
