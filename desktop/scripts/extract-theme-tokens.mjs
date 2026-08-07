/**
 * extract-theme-tokens.mjs
 *
 * Scans the production stylesheet and classifies each
 * `body.theme-dark .X { ... }` rule into two buckets:
 *   - autoDeletable: rule body only references var(--*) and every
 *     referenced variable is defined in tokens.css. These rules
 *     can be safely removed; the underlying token does the work.
 *   - needsReview: rule body contains hardcoded color literals. The
 *     developer must extract a new token or change the rule to use
 *     an existing token before removing it.
 *
 * Pure Node, no external deps.
 *
 * Usage:
 *   node scripts/extract-theme-tokens.mjs
 *   node scripts/extract-theme-tokens.mjs index.css tokens.css
 */

import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const RULE_PATTERN = /(body\.theme-dark\s+[^{}]+)\s*\{\s*([^{}]*?)\s*\}/g;
const VAR_REF_PATTERN = /var\(\s*(--[a-zA-Z0-9_-]+)/g;
const HEX_PATTERN = /#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/g;
const RGB_PATTERN = /\brgba?\s*\([^)]+\)/g;

/**
 * Parse `tokens.css` and return the set of defined variable names.
 *
 * @param {string} text
 * @returns {Set<string>}
 */
export function parseDefinedVariables(text) {
  const defined = new Set();
  for (const m of text.matchAll(/(--[a-zA-Z0-9_-]+)\s*:/g)) {
    defined.add(m[1]);
  }
  return defined;
}

/**
 * Parse a single CSS rule's body and extract variable references and
 * hardcoded colors.
 *
 * @param {string} body
 * @returns {{ vars: string[], hardcoded: string[] }}
 */
export function inspectRuleBody(body) {
  /** @type {string[]} */
  const vars = [];
  for (const m of body.matchAll(VAR_REF_PATTERN)) {
    if (!vars.includes(m[1])) vars.push(m[1]);
  }
  /** @type {string[]} */
  const hardcoded = [];
  for (const m of body.matchAll(HEX_PATTERN)) hardcoded.push(m[0]);
  for (const m of body.matchAll(RGB_PATTERN)) hardcoded.push(m[0]);
  return { vars, hardcoded };
}

/**
 * Classify all `body.theme-dark` rules in `themeCss` against the set of
 * variables defined in `variablesCss`.
 *
 * @param {string} themeCss
 * @param {string} variablesCss
 * @returns {{
 *   summary: { autoDeletable: number, needsReview: number },
 *   autoDeletable: { selector: string, vars: string[] }[],
 *   needsReview: { selector: string, hardcoded: string[] }[]
 * }}
 */
export function extractThemeTokens(themeCss, variablesCss) {
  const defined = parseDefinedVariables(variablesCss);
  const autoDeletable = [];
  const needsReview = [];

  for (const m of themeCss.matchAll(RULE_PATTERN)) {
    const selector = m[1].trim();
    const body = m[2];
    const { vars, hardcoded } = inspectRuleBody(body);

    const allVarsDefined = vars.every((v) => defined.has(v));
    if (hardcoded.length === 0 && vars.length > 0 && allVarsDefined) {
      autoDeletable.push({ selector, vars });
    } else {
      needsReview.push({ selector, hardcoded });
    }
  }

  return {
    summary: {
      autoDeletable: autoDeletable.length,
      needsReview: needsReview.length,
    },
    autoDeletable,
    needsReview,
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
  const themePath = resolve(args[0] ?? 'assets/styles/index.css');
  const variablesPath = resolve(args[1] ?? 'assets/styles/tokens.css');

  if (!existsSync(themePath)) {
    process.stderr.write(`extract-theme-tokens: file not found: ${themePath}\n`);
    process.exit(2);
  }
  if (!existsSync(variablesPath)) {
    process.stderr.write(`extract-theme-tokens: file not found: ${variablesPath}\n`);
    process.exit(2);
  }

  const report = extractThemeTokens(
    readFileSync(themePath, 'utf8'),
    readFileSync(variablesPath, 'utf8'),
  );

  process.stdout.write(
    `theme.css token extraction — ${report.summary.autoDeletable} auto-deletable, ${report.summary.needsReview} need review\n\n`,
  );

  if (report.autoDeletable.length > 0) {
    process.stdout.write('── auto-deletable (only var(--*)) ──\n');
    for (const r of report.autoDeletable.slice(0, 20)) {
      process.stdout.write(`  ${r.selector}  →  ${r.vars.join(', ')}\n`);
    }
    if (report.autoDeletable.length > 20) {
      process.stdout.write(`  … and ${report.autoDeletable.length - 20} more\n`);
    }
    process.stdout.write('\n');
  }

  if (report.needsReview.length > 0) {
    process.stdout.write('── needs review (hardcoded color) ──\n');
    for (const r of report.needsReview.slice(0, 10)) {
      process.stdout.write(
        `  ${r.selector}  →  ${r.hardcoded.slice(0, 3).join(', ')}${r.hardcoded.length > 3 ? '…' : ''}\n`,
      );
    }
    if (report.needsReview.length > 10) {
      process.stdout.write(`  … and ${report.needsReview.length - 10} more\n`);
    }
  }
}
