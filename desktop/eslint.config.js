// ESLint 9 flat config（typescript-eslint v8 推荐写法）
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

/** @type {import('eslint').Linter.Config[]} */
export default [
  // 全局忽略：dist / node_modules / scripts 子目录的 mjs 单文件脚本（自检脚本）
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      'release/**',
      'scripts/check-security.mjs', // 自检脚本本身在 check 流程中独立跑
      'eslint.config.js',
      'vitest.config.ts',
      'esbuild.config.mjs',
    ],
  },
  // 基础 JS 推荐
  js.configs.recommended,
  // TypeScript 推荐（含 type-aware；本配置不显式传 project，故只跑非 type-aware 部分）
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.ts', 'tests/**/*.ts'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        // Browser (renderer)
        window: 'readonly',
        document: 'readonly',
        localStorage: 'readonly',
        navigator: 'readonly',
        crypto: 'readonly',
        queueMicrotask: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        setInterval: 'readonly',
        clearInterval: 'readonly',
        fetch: 'readonly',
        URL: 'readonly',
        URLSearchParams: 'readonly',
        Buffer: 'readonly',
        Blob: 'readonly',
        FileReader: 'readonly',
        Event: 'readonly',
        CustomEvent: 'readonly',
        HTMLElement: 'readonly',
        HTMLInputElement: 'readonly',
        HTMLSelectElement: 'readonly',
        HTMLButtonElement: 'readonly',
        Element: 'readonly',
        Node: 'readonly',
        NodeListOf: 'readonly',
        Element: 'readonly',
        // Node (main / tests)
        process: 'readonly',
        console: 'readonly',
        require: 'readonly',
        __dirname: 'readonly',
        __filename: 'readonly',
        module: 'readonly',
        exports: 'readonly',
        global: 'readonly',
        // Electron
        electron: 'readonly',
        // Node ESM imports
        setImmediate: 'readonly',
      },
    },
    rules: {
      // TypeScript 推荐默认开启规则太严，我们只挑合理的几条
      '@typescript-eslint/no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
      }],
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-non-null-assertion': 'off',
      'no-undef': 'off', // TS 已经处理
      'no-empty': ['warn', { allowEmptyCatch: true }],
      'no-prototype-builtins': 'off',
      // 注释 / 字符串里大量出现 '/',本规则对中文注释尤其噪声,关掉
      'no-useless-escape': 'off',
      // 大量遗留 console.warn,留 warn 让旧代码可见但不至于报错
      'no-console': 'off',
      'prefer-const': 'warn',
    },
  },
  // 测试文件放宽
  {
    files: ['tests/**/*.ts', '**/*.test.ts', 'src/**/*.test.ts'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
  // Visual harnesses are plain ESM executed by Node while evaluating snippets
  // against a browser document, so both global sets are intentional.
  {
    files: ['tests/visual/**/*.mjs'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        console: 'readonly',
        document: 'readonly',
        process: 'readonly',
      },
    },
  },
  // playwright-core 的收口。
  //
  // 全仓只允许 src/main/browser/playwright-compat.ts 直接 import 它，这样升级
  // Playwright 时的审查面固定为「那一个文件 + scripts/pw-contract.ts 契约测试」。
  // 这条约束写在注释里迟早会被绕过，所以让 lint 来挡。
  //
  // playwright-core/lib/** 是内部实现，无任何兼容承诺，任何文件都不许碰。
  {
    files: ['src/**/*.ts'],
    ignores: ['src/main/browser/playwright-compat.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        paths: [{
          name: 'playwright-core',
          message: '只允许 src/main/browser/playwright-compat.ts 直接依赖 playwright-core，请从那里导出。',
        }],
        patterns: [{
          group: ['playwright-core/lib/**', 'playwright-core/types/**'],
          message: 'playwright-core 内部实现无兼容承诺，禁止直接引用。',
        }],
      }],
    },
  },
  {
    files: ['src/main/browser/playwright-compat.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        patterns: [{
          group: ['playwright-core/lib/**'],
          message: 'playwright-core 内部实现无兼容承诺，即使在兼容层也禁止引用。',
        }],
      }],
    },
  },
];
