/**
 * Unit tests for the styling audit scripts.
 *
 * Each script is a pure-Node module that exports a function. Tests use
 * real temp directories (via node:fs.mkdtempSync) to keep the real
 * assets/styles tree untouched.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

// 1. audit-css-leaks
import {
  walkCssFiles,
  auditFile,
  auditCssLeaks,
} from '../../scripts/audit-css-leaks.mjs';

// 2. extract-theme-tokens
import {
  parseDefinedVariables,
  inspectRuleBody,
  extractThemeTokens,
} from '../../scripts/extract-theme-tokens.mjs';

// 3. audit-font-sizes
import { classifyFontSize, auditFontSizes } from '../../scripts/audit-font-sizes.mjs';

// 4. audit-dead-css-vars
import {
  parseDefinedVariables as parseDefinedVarsDead,
  findVarReferences,
  auditDeadCssVars,
} from '../../scripts/audit-dead-css-vars.mjs';

// 5. detect-duplicate-selectors
import {
  parseRules,
  detectDuplicateSelectors,
} from '../../scripts/detect-duplicate-selectors.mjs';

// 6. audit-design-system
import {
  auditDesignSystem,
  hasDesignViolations,
} from '../../scripts/audit-design-system.mjs';

let tempDir: string;
let cssDir: string;

beforeAll(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'audit-scripts-test-'));
  cssDir = join(tempDir, 'styles');
  mkdirSync(cssDir, { recursive: true });

  // A small representative sample of CSS covering common patterns.
  writeFileSync(
    join(cssDir, 'variables.css'),
    ':root { --bg: #fff; --tx1: #111; --accent: #2563eb; }',
  );

  writeFileSync(
    join(cssDir, 'theme.css'),
    [
      'body.theme-dark .x { background: var(--bg); }',
      'body.theme-dark .y { background: #0f172a; }',
      'body.theme-dark .x { color: var(--tx1); }',
      'body.theme-dark .x { background: var(--accent); }', // duplicate selector, different value
    ].join('\n'),
  );

  writeFileSync(
    join(cssDir, 'main.css'),
    '.a { font-size: 14px; color: #fff; background: rgba(0,0,0,0.04); }',
  );
});

afterAll(() => {
  rmSync(tempDir, { recursive: true, force: true });
});

/* ─── 1. audit-css-leaks ──────────────────────────────────── */

describe('audit-css-leaks / walkCssFiles', () => {
  it('finds all .css files in a directory recursively', () => {
    const files = walkCssFiles(cssDir);
    expect(files.length).toBe(3); // variables, theme, main
    expect(files.every((f: string) => f.endsWith('.css'))).toBe(true);
  });

  it('returns sorted paths', () => {
    const files = walkCssFiles(cssDir);
    const sorted = [...files].sort();
    expect(files).toEqual(sorted);
  });
});

describe('audit-css-leaks / auditFile', () => {
  it('counts hex and rgba literals', () => {
    const report = auditFile(join(cssDir, 'main.css'));
    expect(report.hex).toBe(1); // #fff
    expect(report.rgb).toBe(1); // rgba(0,0,0,0.04)
    expect(report.samples).toContain('#fff');
  });

  it('strips comments before counting', () => {
    const p = join(cssDir, '_with-comments.css');
    writeFileSync(p, '/* #fff */ .a { color: #000; }');
    const report = auditFile(p);
    expect(report.hex).toBe(1);
    expect(report.samples).not.toContain('#fff');
  });
});

describe('audit-css-leaks / auditCssLeaks', () => {
  it('aggregates per-file report with summary', () => {
    const out = auditCssLeaks(cssDir);
    expect(out.summary.totalFiles).toBeGreaterThanOrEqual(3);
    expect(out.summary.totalHex).toBeGreaterThan(0);
    expect(out.summary.totalRgb).toBeGreaterThan(0);
    expect(out.files.length).toBe(out.summary.totalFiles);
  });
});

/* ─── 2. extract-theme-tokens ─────────────────────────────── */

describe('extract-theme-tokens / parseDefinedVariables', () => {
  it('extracts every defined variable name', () => {
    const defined = parseDefinedVariables(':root { --a: 1; --b-c: 2; }');
    expect(defined.has('--a')).toBe(true);
    expect(defined.has('--b-c')).toBe(true);
    expect(defined.size).toBe(2);
  });
});

describe('extract-theme-tokens / inspectRuleBody', () => {
  it('lists every distinct var(--*) reference', () => {
    const r = inspectRuleBody('background: var(--bg); color: var(--tx1); border: 1px solid var(--bg);');
    expect(r.vars).toEqual(['--bg', '--tx1']);
    expect(r.hardcoded).toEqual([]);
  });

  it('captures hardcoded color literals', () => {
    const r = inspectRuleBody('color: #0f172a; background: rgba(0,0,0,0.4);');
    expect(r.hardcoded).toEqual(['#0f172a', 'rgba(0,0,0,0.4)']);
  });
});

describe('extract-theme-tokens / extractThemeTokens', () => {
  it('classifies rules that only reference defined vars as autoDeletable', () => {
    const themeCss = 'body.theme-dark .a { background: var(--bg); color: var(--tx1); }';
    const varsCss = ':root { --bg: #fff; --tx1: #111; }';
    const out = extractThemeTokens(themeCss, varsCss);
    expect(out.summary.autoDeletable).toBe(1);
    expect(out.summary.needsReview).toBe(0);
    expect(out.autoDeletable[0].selector).toContain('.a');
  });

  it('classifies rules with hardcoded colors as needsReview', () => {
    const themeCss = 'body.theme-dark .b { background: #0f172a; }';
    const varsCss = ':root { --bg: #fff; }';
    const out = extractThemeTokens(themeCss, varsCss);
    expect(out.summary.autoDeletable).toBe(0);
    expect(out.summary.needsReview).toBe(1);
    expect(out.needsReview[0].hardcoded).toContain('#0f172a');
  });

  it('treats a var(--X) where --X is undefined as needsReview', () => {
    const themeCss = 'body.theme-dark .c { color: var(--undefined); }';
    const varsCss = ':root { --bg: #fff; }';
    const out = extractThemeTokens(themeCss, varsCss);
    expect(out.summary.autoDeletable).toBe(0);
    expect(out.summary.needsReview).toBe(1);
  });
});

/* ─── 3. audit-font-sizes ─────────────────────────────────── */

describe('audit-font-sizes / classifyFontSize', () => {
  it.each([
    [10, 'shouldUseVar'],
    [12, 'shouldUseVar'],
    [14, 'shouldUseVar'],
    [15, 'shouldUseVar'],
    [40, 'shouldUseVar'],
  ])('classifies %dpx as %s', (px, classification) => {
    const c = classifyFontSize(px);
    expect(c).toBe(classification);
  });

  it('classifies unknown but integer sizes as shouldUseCalc', () => {
    expect(classifyFontSize(17)).toBe('shouldUseCalc');
    expect(classifyFontSize(99)).toBe('shouldUseCalc');
  });

  it('classifies sub-pixel sizes as mustKeepLiteral', () => {
    expect(classifyFontSize(12.5)).toBe('mustKeepLiteral');
  });
});

describe('audit-font-sizes / auditFontSizes', () => {
  it('returns counts by classification and per-file items', () => {
    const out = auditFontSizes(cssDir);
    expect(out.summary.total).toBeGreaterThan(0);
    expect(out.byFile.length).toBeGreaterThan(0);
  });
});

/* ─── 4. audit-dead-css-vars ──────────────────────────────── */

describe('audit-dead-css-vars / parseDefinedVariables', () => {
  it('returns the set of declared variable names', () => {
    const set = parseDefinedVarsDead('--a: 1;\n--b-c: 2;');
    expect(set.has('--a')).toBe(true);
    expect(set.has('--b-c')).toBe(true);
  });
});

describe('audit-dead-css-vars / findVarReferences', () => {
  it('returns each var() reference with line + column + fallback', () => {
    const refs = findVarReferences('a { color: var(--x, #fff); }');
    expect(refs.length).toBe(1);
    expect(refs[0].name).toBe('--x');
    expect(refs[0].fallback).toBe('#fff');
    expect(refs[0].line).toBe(1);
  });

  it('handles nested selectors with multi-line input', () => {
    const refs = findVarReferences('.a\n  .b {\n    color: var(--c);\n  }');
    const c = refs.find((r: { name: string }) => r.name === '--c');
    expect(c).toBeDefined();
    expect(c!.line).toBe(3);
  });
});

describe('audit-dead-css-vars / auditDeadCssVars (integrated)', () => {
  it('flags references whose var is not in variables.css', () => {
    const dir = mkdtempSync(join(tmpdir(), 'dead-vars-'));
    mkdirSync(join(dir, 'styles'), { recursive: true });
    writeFileSync(
      join(dir, 'styles', 'main.css'),
      '.a { color: var(--bg); } .b { color: var(--unknown); } .c { left: var(--mw-runtime-left); }',
    );
    writeFileSync(join(dir, 'styles', 'variables.css'), ':root { --bg: #fff; }');
    mkdirSync(join(dir, 'src'), { recursive: true });
    writeFileSync(join(dir, 'src', 'index.ts'), '// noop');

    const out = auditDeadCssVars({
      stylesDir: join(dir, 'styles'),
      srcDir: join(dir, 'src'),
      variablesPath: join(dir, 'styles', 'variables.css'),
    });

    expect(out.summary.totalRefs).toBe(3);
    expect(out.summary.deadRefs).toBe(1);
    expect(out.refs[0].name).toBe('--unknown');
    rmSync(dir, { recursive: true, force: true });
  });
});

/* ─── 5. detect-duplicate-selectors ──────────────────────── */

describe('detect-duplicate-selectors / parseRules', () => {
  it('extracts every rule with its body', () => {
    const rules = parseRules('.a { color: red; } .b { color: blue; }');
    expect(rules.length).toBe(2);
    expect(rules[0].selector).toBe('.a');
    expect(rules[1].selector).toBe('.b');
  });

  it('handles multi-line bodies', () => {
    const rules = parseRules('.x {\n  color: red;\n  background: blue;\n}');
    expect(rules[0].body).toContain('color: red');
    expect(rules[0].body).toContain('background: blue');
  });
});

describe('detect-duplicate-selectors / detectDuplicateSelectors', () => {
  it('returns empty when no duplicates exist', () => {
    const out = detectDuplicateSelectors('.a { color: red; } .b { color: blue; }');
    expect(out.summary.duplicateSelectors).toBe(0);
    expect(out.duplicates).toEqual([]);
  });

  it('groups same selector with same value', () => {
    const css = '.a { color: red; } .a { color: red; }';
    const out = detectDuplicateSelectors(css);
    expect(out.summary.duplicateSelectors).toBe(1);
    expect(out.summary.sameValue).toBe(1);
    expect(out.summary.differentValue).toBe(0);
  });

  it('groups same selector with different values (cascade war)', () => {
    const css = '.a { color: red; } .a { color: blue; }';
    const out = detectDuplicateSelectors(css);
    expect(out.summary.duplicateSelectors).toBe(1);
    expect(out.summary.differentValue).toBe(1);
    expect(out.duplicates[0].occurrences.length).toBe(2);
  });

  it('skips @media containers at top level', () => {
    const css = '@media (max-width: 600px) { .a { color: red; } }';
    const out = detectDuplicateSelectors(css);
    expect(out.summary.duplicateSelectors).toBe(0);
  });
});

/* ─── 6. audit-design-system ──────────────────────────────── */

describe('audit-design-system', () => {
  it('reports raw geometry, effects, important and visual inline styles', () => {
    const dir = mkdtempSync(join(tmpdir(), 'design-system-audit-'));
    mkdirSync(join(dir, 'assets', 'styles'), { recursive: true });
    mkdirSync(join(dir, 'src'), { recursive: true });
    writeFileSync(
      join(dir, 'assets', 'styles', 'feature.css'),
      '.a { padding: 8px; border-radius: 16px; background: linear-gradient(red, blue); backdrop-filter: blur(2px); transition: all 160ms; color: red !important; --legacy-accent: red; }',
    );
    writeFileSync(join(dir, 'assets', 'sprite.svg'), '<svg><path fill="var(--mw-status-danger,#f00)" /></svg>');
    writeFileSync(join(dir, 'src', 'feature.ts'), "el.style.width = '10px'; el.style.display = 'none'; el.style.setProperty('--mw-raw', 'x'); const html = `<div style=\"color: red\"></div>`;");
    writeFileSync(join(dir, 'assets', 'index.html'), '<div style="color: red"></div>');

    const report = auditDesignSystem({
      stylesDir: join(dir, 'assets', 'styles'),
      srcDir: join(dir, 'src'),
      assetsDir: join(dir, 'assets'),
    });

    expect(report.summary.spacing).toBe(1);
    expect(report.summary.radius).toBe(1);
    expect(report.summary.gradient).toBe(1);
    expect(report.summary.effects).toBe(1);
    expect(report.summary.motionLiterals).toBe(1);
    expect(report.summary.transitionAll).toBe(1);
    expect(report.summary.important).toBe(1);
    expect(report.summary.customProperties).toBe(1);
    expect(report.summary.embeddedColors).toBe(1);
    expect(report.summary.inlineStyles).toBe(4);
    expect(hasDesignViolations(report)).toBe(true);
    rmSync(dir, { recursive: true, force: true });
  });
});

/* ─── 7. check-security ───────────────────────────────────── */

function runSecurityFixture(name: 'safe' | 'unsafe') {
  const script = join(process.cwd(), 'scripts', 'check-security.mjs');
  const fixtureRoot = join(process.cwd(), 'tests', 'fixtures', 'security', name);
  return spawnSync(process.execPath, [script, '--root', fixtureRoot], {
    cwd: process.cwd(),
    encoding: 'utf8',
  });
}

describe('check-security / dynamic innerHTML', () => {
  it('accepts native DOM construction and explicitly sanitized interpolation', () => {
    const result = runSecurityFixture('safe');
    expect(result.status).toBe(0);
    expect(result.stdout).toContain('OK — no forbidden patterns');
  });

  it('rejects raw template interpolation with the XSS rule', () => {
    const result = runSecurityFixture('unsafe');
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('innerHTML with template interpolation forbidden');
    expect(result.stderr).toContain('unsafe-inner-html.ts');
  });
});
