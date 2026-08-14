/**
 * detect-duplicate-selectors.mjs
 *
 * Scans a CSS file for selectors that appear more than once. The script
 * focuses on the same selector with potentially different declaration
 * blocks (cascade wars) — these are the real maintenance bomb in
 * the production stylesheet where patches stack over time.
 *
 * Pure Node, no external deps.
 *
 * Usage:
 *   node scripts/detect-duplicate-selectors.mjs [path/to/file.css] [--fail-on-different-value]
 *
 * Exit codes:
 *   0  no duplicate selectors, OR only same-value duplicates, OR (without
 *      --fail-on-different-value) any duplicates (report only).
 *   1  --fail-on-different-value is set AND at least one selector has
 *      multiple distinct declaration blocks (cascade conflict).
 *   2  file not found / bad usage.
 */

import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Match every `selector { body }` rule in `text`. The body may be
 * multi-line. The selector is captured greedily up to the first `{`.
 *
 * @param {string} text
 * @returns {{ selector: string, body: string, offset: number }[]}
 */
export function parseRules(text) {
  // Strip comments first to avoid matching braces inside them.
  const stripped = text.replace(/\/\*[\s\S]*?\*\//g, '');
  /** @type {{ selector: string, body: string, offset: number }[]} */
  const out = [];
  // Find each `{` outside of strings, and walk back to capture the selector.
  let i = 0;
  while (i < stripped.length) {
    const ch = stripped[i];
    if (ch === '{') {
      // Walk back to capture the selector (start at last `;` or `}` + 1).
      let start = i - 1;
      while (start >= 0 && stripped[start] !== '}' && stripped[start] !== ';') {
        start -= 1;
      }
      const selector = stripped.slice(start + 1, i).trim();
      // Find matching `}`.
      let depth = 1;
      let j = i + 1;
      while (j < stripped.length && depth > 0) {
        if (stripped[j] === '{') depth += 1;
        else if (stripped[j] === '}') depth -= 1;
        j += 1;
      }
      const body = stripped.slice(i + 1, j - 1).trim();
      out.push({ selector, body, offset: i });
      i = j;
    } else {
      i += 1;
    }
  }
  return out;
}

/**
 * Detect duplicate selectors in a CSS file. Returns selectors that appear
 * more than once, with the body of each occurrence.
 *
 * @param {string} css
 * @returns {{
 *   summary: { totalRules: number, duplicateSelectors: number, sameValue: number, differentValue: number },
 *   duplicates: { selector: string, occurrences: { body: string }[] }[]
 * }}
 */
export function detectDuplicateSelectors(css) {
  const rules = parseRules(css);
  /** @type {Map<string, { selector: string, occurrences: { body: string }[] }>} */
  const groups = new Map();
  for (const r of rules) {
    if (!r.selector) continue;
    // @media query containers have nested rules. Skip top-level @media.
    if (r.selector.startsWith('@')) continue;
    const key = r.selector;
    const cur = groups.get(key);
    if (cur) {
      cur.occurrences.push({ body: r.body });
    } else {
      groups.set(key, { selector: key, occurrences: [{ body: r.body }] });
    }
  }

  const duplicates = [];
  let sameValue = 0;
  let differentValue = 0;
  for (const v of groups.values()) {
    if (v.occurrences.length < 2) continue;
    const distinctBodies = new Set(v.occurrences.map((o) => o.body));
    if (distinctBodies.size === 1) {
      sameValue += 1;
    } else {
      differentValue += 1;
    }
    duplicates.push(v);
  }
  duplicates.sort((a, b) => a.selector.localeCompare(b.selector));

  return {
    summary: {
      totalRules: rules.length,
      duplicateSelectors: duplicates.length,
      sameValue,
      differentValue,
    },
    duplicates,
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
  const failOnDifferentValue = args.includes('--fail-on-different-value');
  // Drop flag(s) before resolving the positional file path.
  const positional = args.filter((a) => !a.startsWith('--'));
  const target = resolve(positional[0] ?? 'assets/styles/index.css');
  if (!existsSync(target)) {
    process.stderr.write(`detect-duplicate-selectors: file not found: ${target}\n`);
    process.exit(2);
  }
  const report = detectDuplicateSelectors(readFileSync(target, 'utf8'));

  process.stdout.write(
    `duplicate-selector audit — ${report.summary.totalRules} rules, ${report.summary.duplicateSelectors} duplicate\n`,
  );
  process.stdout.write(`  same value     : ${report.summary.sameValue}\n`);
  process.stdout.write(`  different value: ${report.summary.differentValue}\n\n`);

  for (const d of report.duplicates) {
    const distinct = new Set(d.occurrences.map((o) => o.body));
    const flag = distinct.size > 1 ? '⚠' : ' ';
    process.stdout.write(`${flag} ${d.selector}  (×${d.occurrences.length})\n`);
    if (distinct.size > 1) {
      for (let i = 0; i < d.occurrences.length; i += 1) {
        const preview = d.occurrences[i].body.length > 80
          ? d.occurrences[i].body.slice(0, 77) + '…'
          : d.occurrences[i].body;
        process.stdout.write(`    [${i + 1}] ${preview}\n`);
      }
    }
  }

  // Gate semantics: same-value dups are warnings (non-fatal); different-value
  // dups are failures ONLY when --fail-on-different-value is passed.
  if (failOnDifferentValue && report.summary.differentValue > 0) {
    process.stderr.write(
      `detect-duplicate-selectors: FAIL — ${report.summary.differentValue} selector(s) ` +
        `with conflicting values in ${target.replace(/\\/g, '/')}\n`,
    );
    process.exit(1);
  }
  process.exit(0);
}
