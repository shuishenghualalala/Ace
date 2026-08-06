/**
 * 把 scripts/ax-probe.ts 打包成 electron 可直接执行的 scripts/ax-probe.mjs。
 *
 * 单独一个构建脚本而不是并进 esbuild.config.mjs：探针是开发期量测工具，
 * 不该进产物，也不该拖慢正常构建。
 */

import * as esbuild from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

await esbuild.build({
  entryPoints: [path.join(here, 'ax-probe.ts')],
  outfile: path.join(here, 'ax-probe.mjs'),
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  // electron 由宿主提供；node 内置模块保持外部化，避免 esbuild 试图打包它们。
  external: ['electron'],
  banner: {
    js: "import { createRequire as __cr } from 'node:module';\nconst require = __cr(import.meta.url);",
  },
  logLevel: 'info',
});
