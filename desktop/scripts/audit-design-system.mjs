/**
 * Enforce the renderer design contract at the source boundary.
 *
 * This is intentionally a small lexical audit: it catches the drift patterns
 * that are cheap to introduce and expensive to migrate later. `tokens.css`
 * owns primitives; every other stylesheet consumes contracts.
 *
 * Usage:
 *   node scripts/audit-design-system.mjs
 *   node scripts/audit-design-system.mjs --json
 *   node scripts/audit-design-system.mjs --strict
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { basename, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { walkCssFiles } from './audit-css-leaks.mjs';

const SPACING_PROPERTIES = new Set([
  'margin',
  'margin-block',
  'margin-block-end',
  'margin-block-start',
  'margin-bottom',
  'margin-inline',
  'margin-inline-end',
  'margin-inline-start',
  'margin-left',
  'margin-right',
  'margin-top',
  'padding',
  'padding-block',
  'padding-block-end',
  'padding-block-start',
  'padding-bottom',
  'padding-inline',
  'padding-inline-end',
  'padding-inline-start',
  'padding-left',
  'padding-right',
  'padding-top',
  'gap',
  'row-gap',
  'column-gap',
]);
const LENGTH_PATTERN = /\b-?\d+(?:\.\d+)?px\b/g;
const DECLARATION_PATTERN = /([\w-]+)\s*:\s*([^;{}]+);/g;
const INLINE_PROPERTY_PATTERN = /\.style\.([A-Za-z][\w]*)\s*=/g;
const INLINE_VARIABLE_PATTERN = /\.style\.setProperty\(\s*['"]([^'"]+)['"]/g;
const STYLE_ATTR_PATTERN = /\bstyle\s*=\s*\\?(["'`])([\s\S]*?)\\?\1/g;
const STYLE_ATTR_DECL_PATTERN = /(?:^|;)\s*([\w-]+)\s*:/g;
const EMBEDDED_COLOR_PATTERN =
  /(?:\b(?:fill|stroke|color|stop-color|flood-color|lighting-color)\s*[:=]\s*["']?\s*(?:#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\s*\())|(?:var\(\s*--[A-Za-z0-9_-]+\s*,\s*(?:#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\s*\())/g;
const BEHAVIORAL_INLINE_PROPERTIES = new Set(['display', 'visibility', 'colorScheme']);
/** 整值就是一个 token 引用（效果/阴影允许通过 token 落到 tokens.css）。 */
const MW_TOKEN_VALUE_PATTERN = /^var\(\s*--mw-[A-Za-z0-9_-]+\s*\)$/;
/** transition 里的字面量时长与字面量缓动函数——token 化的正是这两样。 */
const LITERAL_TIME_PATTERN = /(?:^|[\s,(])\d*\.?\d+(?:ms|s)\b/;
const LITERAL_EASING_PATTERN = /\b(?:cubic-bezier|steps|linear)\s*\(/;
/**
 * 品牌资产豁免：多色插画自带调色板，且以图片方式加载、读不到应用 CSS 变量，
 * 因此不能也不该跟随主题 token。新增品牌资产必须显式登记，避免成为硬编码颜色的后门。
 */
const BRAND_ASSET_ALLOWLIST = new Set(['icon-v2.svg']);
const DESIGN_VIOLATION_KEYS = [
  'spacing',
  'radius',
  'gradient',
  'effects',
  'motionLiterals',
  'transitionAll',
  'important',
  'customProperties',
  'embeddedColors',
  'inlineStyles',
];

function isDocumentedShapeGradient(property, value) {
  if (property === 'mask' || property === '-webkit-mask') return true;
  if (/linear-gradient\(\s*currentcolor\b/i.test(value)) return true;
  return /conic-gradient/.test(value) && /--(?:mw-)?(?:todo-progress|msg-fold-spinner-angle)/.test(value);
}

/**
 * 内联 style 属性是否只发布 token（`style="--mw-x: ..."`）。
 *
 * JS/模板只能把运行时算出来的值通过 `--mw-*` 自定义属性交给 CSS，
 * 真正的布局属性必须留在样式表里——所以属性里出现任何非 `--mw-*` 声明都算违规。
 */
function hasNonTokenInlineDeclaration(styleText) {
  for (const match of styleText.matchAll(STYLE_ATTR_DECL_PATTERN)) {
    if (!match[1].startsWith('--mw-')) return true;
  }
  return false;
}

function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, ' '));
}

function locationAt(text, offset) {
  const before = text.slice(0, offset);
  const line = before.split('\n').length;
  const lastBreak = before.lastIndexOf('\n');
  return { line, column: offset - lastBreak };
}

function walkFiles(dir, extensions) {
  const out = [];
  const stack = [resolve(dir)];
  while (stack.length) {
    const current = stack.pop();
    let entries;
    try {
      entries = readdirSync(current);
    } catch {
      continue;
    }
    for (const name of entries) {
      const file = join(current, name);
      let stats;
      try {
        stats = statSync(file);
      } catch {
        continue;
      }
      if (stats.isDirectory()) stack.push(file);
      else if (stats.isFile() && extensions.has(extname(file))) out.push(file);
    }
  }
  return out.sort();
}

function addViolation(list, file, text, offset, kind, value) {
  const location = locationAt(text, offset);
  list.push({
    file: relative(process.cwd(), file),
    line: location.line,
    column: location.column,
    kind,
    value,
  });
}

function auditCss(cssDir) {
  /** @type {Record<string, any[]>} */
  const violations = {
    spacing: [],
    radius: [],
    gradient: [],
    effects: [],
    motionLiterals: [],
    transitionAll: [],
    important: [],
    customProperties: [],
  };
  const files = walkCssFiles(resolve(cssDir));

  for (const file of files) {
    if (basename(file) === 'tokens.css') continue;
    const text = readFileSync(file, 'utf8');
    const source = stripComments(text);

    for (const match of source.matchAll(DECLARATION_PATTERN)) {
      const property = match[1];
      const value = match[2].trim();
      const offset = match.index ?? 0;
      if (property.startsWith('--')) {
        if (!property.startsWith('--mw-')) {
          addViolation(violations.customProperties, file, text, offset, 'custom-property', property);
        }
        continue;
      }
      if (SPACING_PROPERTIES.has(property) && LENGTH_PATTERN.test(value)) {
        LENGTH_PATTERN.lastIndex = 0;
        addViolation(violations.spacing, file, text, offset, 'spacing', `${property}: ${value}`);
      }
      if (property === 'border-radius' && LENGTH_PATTERN.test(value)) {
        LENGTH_PATTERN.lastIndex = 0;
        addViolation(violations.radius, file, text, offset, 'radius', value);
      }
      if (property === 'transition' && /\ball\b/.test(value)) {
        addViolation(violations.transitionAll, file, text, offset, 'transition-all', value);
      }
      if (
        property === 'transition' &&
        value !== 'none' &&
        !MW_TOKEN_VALUE_PATTERN.test(value) &&
        (LITERAL_TIME_PATTERN.test(value) || LITERAL_EASING_PATTERN.test(value))
      ) {
        addViolation(violations.motionLiterals, file, text, offset, 'motion-literal', value);
      }
      if (
        (property === 'backdrop-filter' || property === '-webkit-backdrop-filter' || property === 'filter') &&
        value !== 'none' &&
        !MW_TOKEN_VALUE_PATTERN.test(value)
      ) {
        addViolation(violations.effects, file, text, offset, 'forbidden-effect', `${property}: ${value}`);
      }
      if (property === 'text-shadow' && value !== 'none' && !MW_TOKEN_VALUE_PATTERN.test(value)) {
        addViolation(violations.effects, file, text, offset, 'forbidden-effect', `${property}: ${value}`);
      }
      if (/!important\b/.test(value)) {
        addViolation(violations.important, file, text, offset, 'important', value);
      }
      if (
        /\b(?:linear|radial|conic|repeating-linear|repeating-radial)-gradient\s*\(/.test(value) &&
        !isDocumentedShapeGradient(property, value)
      ) {
        addViolation(violations.gradient, file, text, offset, 'gradient', value);
      }
    }
  }
  for (const file of files) {
    if (basename(file) === 'tokens.css') continue;
    const text = readFileSync(file, 'utf8');
    for (const match of text.matchAll(/@property\s+(--[A-Za-z0-9_-]+)/g)) {
      if (!match[1].startsWith('--mw-')) {
        addViolation(violations.customProperties, file, text, match.index ?? 0, 'custom-property', match[1]);
      }
    }
  }
  return violations;
}

function auditInlineStyles(srcDir, assetsDir) {
  const violations = [];
  const sourceFiles = walkFiles(srcDir, new Set(['.ts', '.tsx']));
  for (const file of sourceFiles) {
    const text = readFileSync(file, 'utf8');
    for (const match of text.matchAll(INLINE_PROPERTY_PATTERN)) {
      const property = match[1];
      if (!BEHAVIORAL_INLINE_PROPERTIES.has(property)) {
        addViolation(violations, file, text, match.index ?? 0, 'inline-style', property);
      }
    }
    for (const match of text.matchAll(INLINE_VARIABLE_PATTERN)) {
      const name = match[1];
      // 只有把值发布进 token 命名空间才算合法桥接；其它自定义属性仍属漂移。
      if (!name.startsWith('--mw-')) {
        addViolation(violations, file, text, match.index ?? 0, 'inline-style-property', name);
      }
    }
    for (const match of text.matchAll(STYLE_ATTR_PATTERN)) {
      if (hasNonTokenInlineDeclaration(match[2] ?? '')) {
        addViolation(violations, file, text, match.index ?? 0, 'ts-inline-style', 'style');
      }
    }
  }
  for (const file of walkFiles(assetsDir, new Set(['.html']))) {
    const text = readFileSync(file, 'utf8');
    for (const match of text.matchAll(STYLE_ATTR_PATTERN)) {
      if (hasNonTokenInlineDeclaration(match[2] ?? '')) {
        addViolation(violations, file, text, match.index ?? 0, 'html-inline-style', 'style');
      }
    }
  }
  return violations;
}

function auditEmbeddedColors(assetsDir) {
  const violations = [];
  for (const file of walkFiles(assetsDir, new Set(['.svg']))) {
    if (BRAND_ASSET_ALLOWLIST.has(basename(file))) continue;
    const text = readFileSync(file, 'utf8');
    const source = stripComments(text);
    for (const match of source.matchAll(EMBEDDED_COLOR_PATTERN)) {
      addViolation(violations, file, text, match.index ?? 0, 'embedded-color', match[0]);
    }
  }
  return violations;
}

export function auditDesignSystem({ stylesDir, srcDir, assetsDir }) {
  const css = auditCss(stylesDir);
  const inline = auditInlineStyles(srcDir, assetsDir);
  const embeddedColors = auditEmbeddedColors(assetsDir);
  const summary = {
    cssFiles: walkCssFiles(resolve(stylesDir)).filter((file) => basename(file) !== 'tokens.css').length,
    spacing: css.spacing.length,
    radius: css.radius.length,
    gradient: css.gradient.length,
    effects: css.effects.length,
    motionLiterals: css.motionLiterals.length,
    transitionAll: css.transitionAll.length,
    important: css.important.length,
    customProperties: css.customProperties.length,
    embeddedColors: embeddedColors.length,
    inlineStyles: inline.length,
  };
  return { summary, violations: { ...css, embeddedColors, inline } };
}

export function hasDesignViolations(report) {
  return DESIGN_VIOLATION_KEYS.some((key) => report.summary[key] > 0);
}

function isCli() {
  if (!process.argv[1]) return false;
  return fileURLToPath(import.meta.url) === resolve(process.argv[1]);
}

if (isCli()) {
  const args = process.argv.slice(2);
  const root = resolve(args.find((arg) => !arg.startsWith('--')) ?? '.');
  const report = auditDesignSystem({
    stylesDir: join(root, 'assets', 'styles'),
    srcDir: join(root, 'src'),
    assetsDir: join(root, 'assets'),
  });
  if (args.includes('--json')) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  } else {
    process.stdout.write('design-system audit\n');
    for (const [name, count] of Object.entries(report.summary)) {
      process.stdout.write(`  ${name.padEnd(16)}: ${count}\n`);
    }
    for (const [kind, items] of Object.entries(report.violations)) {
      for (const item of items.slice(0, 12)) {
        process.stdout.write(`  ${kind}: ${item.file}:${item.line} ${item.value}\n`);
      }
      if (items.length > 12) process.stdout.write(`  ${kind}: … ${items.length - 12} more\n`);
    }
  }
  if (args.includes('--strict') && hasDesignViolations(report)) process.exitCode = 1;
}
