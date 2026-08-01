import { defineConfig } from 'vitest/config';
import * as path from 'node:path';

export default defineConfig({
  define: {
    __HELP_DOC_VERSION__: JSON.stringify('test'),
  },
  test: {
    include: ['tests/unit/**/*.test.ts', 'src/**/*.test.ts'],
    environment: 'node',
    globals: false,
    setupFiles: ['tests/setup-happy-dom-storage.ts'],
    reporters: ['default'],
    coverage: {
      provider: 'v8',
      // 只统计有单测的核心层（shared / reducers / stores / 纯函数），
      // 不含 renderer 入口（index.ts 等 DOM 耦合模块，node 环境不可测）。
      include: ['src/shared/**', 'src/ui/reducers/**', 'src/ui/stores/**', 'src/main/version-compare.ts'],
      // 阈值覆盖当前核心层基线，防止覆盖率退化。
      thresholds: {
        lines: 80,
        statements: 80,
        functions: 65,
        branches: 70,
      },
    },
  },
  resolve: {
    alias: {
      '@shared': path.resolve(__dirname, 'src/shared'),
    },
  },
});
