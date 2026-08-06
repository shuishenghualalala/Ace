/**
 * 把 scripts/pw-contract.ts 打包成 electron 可直接执行的 scripts/pw-contract.mjs。
 *
 * 与 ax-probe.build.mjs 同理：契约测试是开发/升级期的验证工具，不该进产物。
 * playwright-core 与 electron 一样保持外部化——由宿主的 node_modules 提供。
 */

import * as esbuild from 'esbuild';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

await esbuild.build({
  entryPoints: [path.join(here, 'pw-contract.ts')],
  outfile: path.join(here, 'pw-contract.mjs'),
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  external: ['electron', 'playwright-core'],
  banner: {
    js: "import { createRequire as __cr } from 'node:module';\nconst require = __cr(import.meta.url);",
  },
  logLevel: 'info',
});
