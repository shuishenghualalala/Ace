import * as esbuild from 'esbuild';
import fs from 'fs/promises';
import path from 'path';
import { createPublicKey } from 'crypto';

const isDev = process.argv.includes('--dev');
const isVisual = process.argv.includes('--visual');
const isPreflight = process.argv.includes('--preflight');
const requiresReleaseConfig = isPreflight || (!isDev && !isVisual);
const rootDir = process.cwd();
const distDir = path.join(rootDir, 'dist');
const mainOut = path.join(distDir, 'main');
const assetsSrc = path.join(rootDir, 'assets');
const assetsOut = path.join(distDir, 'assets');
const helpDocSource = path.join(assetsSrc, 'help-docs', 'crew-user-guide.md');
const { updatePublicKey, updateDownloadBaseUrl } = validateReleaseConfig({
  downloadBaseUrl: String(process.env.ACE_DOWNLOAD_BASE_URL ?? '').trim(),
  publicKey: String(process.env.ACE_UPDATE_PUBLIC_KEY ?? '').trim(),
});

function validateReleaseConfig({ downloadBaseUrl, publicKey }) {
  const errors = [];
  let normalizedDownloadBaseUrl = '';
  try {
    normalizedDownloadBaseUrl = validateUpdateDownloadBaseUrl(downloadBaseUrl);
  } catch (error) {
    errors.push(error instanceof Error ? error.message : String(error));
  }
  try {
    validateUpdatePublicKey(publicKey);
  } catch (error) {
    errors.push(error instanceof Error ? error.message : String(error));
  }
  if (errors.length > 0) {
    throw new Error([
      'Desktop production release configuration is invalid:',
      ...errors.map((message) => `- ${message}`),
      'Set the required release environment values; no artifact was built.',
    ].join('\n'));
  }
  return {
    updatePublicKey: publicKey,
    updateDownloadBaseUrl: normalizedDownloadBaseUrl,
  };
}

function validateUpdateDownloadBaseUrl(value) {
  if (!value) {
    if (requiresReleaseConfig) {
      throw new Error('Production desktop builds require ACE_DOWNLOAD_BASE_URL');
    }
    return '';
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('ACE_DOWNLOAD_BASE_URL must be an absolute HTTPS URL');
  }
  if (parsed.protocol !== 'https:') {
    throw new Error('ACE_DOWNLOAD_BASE_URL must use HTTPS');
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('ACE_DOWNLOAD_BASE_URL must not contain credentials, query, or fragment');
  }
  if (!parsed.pathname.endsWith('/')) parsed.pathname += '/';
  return parsed.href;
}

function validateUpdatePublicKey(value) {
  if (!value) {
    if (requiresReleaseConfig) {
      throw new Error('Production desktop builds require ACE_UPDATE_PUBLIC_KEY');
    }
    return;
  }
  try {
    const key = value.includes('BEGIN PUBLIC KEY')
      ? createPublicKey(value)
      : createPublicKey({
          key: Buffer.from(value, 'base64'),
          format: 'der',
          type: 'spki',
        });
    if (key.asymmetricKeyType !== 'ed25519') {
      throw new Error('not Ed25519');
    }
  } catch {
    throw new Error('ACE_UPDATE_PUBLIC_KEY must be an Ed25519 SPKI public key');
  }
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

function formatDocVersion(date) {
  return `${date.getFullYear()}.${pad2(date.getMonth() + 1)}.${pad2(date.getDate())}`;
}

async function ensureDir(d) {
  await fs.mkdir(d, { recursive: true });
}

async function copyDir(src, dst) {
  await ensureDir(dst);
  const entries = await fs.readdir(src, { withFileTypes: true });
  for (const entry of entries) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) await copyDir(s, d);
    else await fs.copyFile(s, d);
  }
}

async function copyFile(from, to) {
  await ensureDir(path.dirname(to));
  await fs.copyFile(from, to);
}

async function main() {
  await fs.rm(distDir, { recursive: true, force: true });
  await ensureDir(mainOut);
  const helpDocVersion = formatDocVersion((await fs.stat(helpDocSource)).mtime);

  // Main process
  await esbuild.build({
    entryPoints: [path.join(rootDir, 'src/main/index.ts')],
    outfile: path.join(mainOut, 'index.js'),
    bundle: true,
    platform: 'node',
    target: 'node18',
    format: 'cjs',
    // playwright-core 必须 external：它内部有大量运行时 require（注入脚本源码、
    // 浏览器注册表、驱动包路径），bundle 进来会在打包产物里静默失效。
    external: ['electron', 'ws', 'bufferutil', 'utf-8-validate', 'playwright-core'],
    define: {
      __ACE_UPDATE_PUBLIC_KEY__: JSON.stringify(updatePublicKey),
      __ACE_DOWNLOAD_BASE_URL__: JSON.stringify(updateDownloadBaseUrl),
    },
    sourcemap: isDev,
    minify: !isDev,
    logLevel: 'info',
  });

  // Packaged main is a dependency-free CommonJS bootstrap. It scrubs ambient
  // loader/Node/Electron hooks before index.js imports Electron or starts Chromium.
  await copyFile(
    path.join(rootDir, 'src/main/bootstrap.cjs'),
    path.join(mainOut, 'bootstrap.js'),
  );
  await copyFile(
    path.join(rootDir, 'src/main/bootstrap-hardening.cjs'),
    path.join(mainOut, 'bootstrap-hardening.cjs'),
  );

  // Preload
  await esbuild.build({
    entryPoints: [path.join(rootDir, 'src/main/preload.ts')],
    outfile: path.join(mainOut, 'preload.js'),
    bundle: true,
    platform: 'node',
    target: 'node18',
    format: 'cjs',
    external: ['electron', 'ws', 'bufferutil', 'utf-8-validate'],
    sourcemap: isDev,
    minify: !isDev,
    logLevel: 'info',
  });

  // 灵感便利贴使用独立 preload，只暴露关闭入口，不继承主窗口能力。
  await esbuild.build({
    entryPoints: [path.join(rootDir, 'src/main/inspiration-sticky-preload.ts')],
    outfile: path.join(mainOut, 'inspiration-sticky-preload.js'),
    bundle: true,
    platform: 'node',
    target: 'node18',
    format: 'cjs',
    external: ['electron'],
    sourcemap: isDev,
    minify: !isDev,
    logLevel: 'info',
  });

  // 1. 合并原 desktop 的所有 CSS 模块（从源目录直接读，不依赖 dist）
  //    KaTeX 的 katex.min.css 通过 @import 引入，里面 url() 引用了 woff2/woff/ttf 字体；
  //    用 dataurl loader 把字体内联成 base64（桌面端离线，体积可接受），避免 file:// 加载外部字体失败。
  await esbuild.build({
    entryPoints: [path.join(assetsSrc, 'styles/index.css')],
    outfile: path.join(distDir, 'styles.bundle.css'),
    bundle: true,
    loader: {
      '.css': 'css',
      '.woff2': 'dataurl',
      '.woff': 'dataurl',
      '.ttf': 'dataurl',
    },
    minify: !isDev,
    sourcemap: isDev,
    logLevel: 'info',
  });

  // 2. Renderer
  //    mermaid 走 external：不打包进 renderer.js（min.js ~3.3mb 会拖慢首屏），
  //    改为运行时按需 `<script src="./mermaid.min.js">` 懒加载（见 mermaid-render.ts）。
  await esbuild.build({
    entryPoints: [path.join(rootDir, 'src/ui/index.ts')],
    outfile: path.join(distDir, 'renderer.js'),
    bundle: true,
    platform: 'browser',
    target: 'es2020',
    format: 'iife',
    external: ['mermaid'],
    loader: {
      '.md': 'text',
      '.wasm': 'binary',
    },
    // renderer 以 IIFE 输出，import.meta.url 原本会被折叠为空；PPTX 本地渲染器在模块
    // 初始化时用它构造默认 WASM URL（实际运行传入的是内联字节），需保留一个合法基址。
    define: {
      __HELP_DOC_VERSION__: JSON.stringify(helpDocVersion),
      'import.meta.url': 'window.location.href',
    },
    sourcemap: isDev,
    minify: !isDev,
    logLevel: 'info',
  });

  // 3. Wiki 图谱布局 Worker（Phase 3）：零依赖自研力导向布局，独立 iife 产物。
  //    renderer 以 `new Worker('./wiki-graph-layout.worker.js')` 加载——主窗口
  //    loadFile(dist/assets/index.html)，相对路径相对 index.html 解析；
  //    实测 Electron 43 + file:// + CSP script-src 'self' 下 file Worker 可用。
  await esbuild.build({
    entryPoints: [path.join(rootDir, 'src/ui/wiki-graph-layout.worker.ts')],
    outfile: path.join(distDir, 'wiki-graph-layout.worker.js'),
    bundle: true,
    platform: 'browser',
    target: 'es2020',
    format: 'iife',
    sourcemap: isDev,
    minify: !isDev,
    logLevel: 'info',
  });

  // 4. Copy assets (含 image/ icon 等)
  await copyDir(assetsSrc, assetsOut);

  // 把 renderer.js 也复制到 dist/assets/，让 HTML 能用 ./renderer.js 引用
  await fs.copyFile(path.join(distDir, 'renderer.js'), path.join(assetsOut, 'renderer.js'));

  // worker 产物同样落到 dist/assets/，与 index.html 同目录（new Worker 相对路径解析）
  await fs.copyFile(
    path.join(distDir, 'wiki-graph-layout.worker.js'),
    path.join(assetsOut, 'wiki-graph-layout.worker.js'),
  );

  // mermaid.min.js 拷到 dist/assets/，供 mermaid-render.ts 运行时按需 `<script src="./mermaid.min.js">` 加载。
  // 不打进 renderer bundle 是为了首屏体积（min.js ~3.3mb）；只在出现 mermaid 图表时才加载。
  await copyFile(
    path.join(rootDir, 'node_modules/mermaid/dist/mermaid.min.js'),
    path.join(assetsOut, 'mermaid.min.js'),
  );

  // 5. 把合并后的 CSS 注入到 HTML，避免 file:// 跨目录加载问题
  const cssPath = path.join(distDir, 'styles.bundle.css');
  let css = await fs.readFile(cssPath, 'utf8');
  // 去除 Google Fonts 外部引用（离线环境加载失败）
  css = css.replace(/@import\s+["']https:\/\/fonts\.googleapis\.com[^"']*["'];?/g, '');
  const htmlPath = path.join(assetsOut, 'index.html');
  let html = await fs.readFile(htmlPath, 'utf8');
  // 删除 HTML 里原来的 <link rel="stylesheet" href="./styles/index.css" />
  html = html.replace(/<link\s+rel="stylesheet"\s+href="\.\/styles\/index\.css"\s*\/?>/g, '');
  // 关键：去掉 type="module"，否则 file:// 协议下被同源策略阻止
  html = html.replace(/<script\s+type="module"\s+src="\.\/renderer\.js"><\/script>/, '<script src="./renderer.js"></script>');
  // file:// 下 Chromium 不解析外部 <use href="./sprite.svg#id">。
  // 构建时从唯一生产源注入 symbols，页面只引用本地 #semantic-id。
  const sprite = (await fs.readFile(path.join(assetsSrc, 'crew-ui-symbols.svg'), 'utf8'))
    .replace('<svg ', '<svg id="mw-icon-sprite" aria-hidden="true" ');
  html = html.replace(/(<body\b[^>]*>)/i, `$1\n${sprite}`);
  const styleTag = `<style>\n${css}\n</style>`;
  // 插到 </head> 之前
  html = html.replace('</head>', `${styleTag}\n</head>`);
  await fs.writeFile(htmlPath, html, 'utf8');

  if (isVisual) {
    const visualOut = path.join(distDir, 'visual');
    await copyDir(assetsOut, visualOut);
    await esbuild.build({
      stdin: {
        contents: `
          import { startFixtureRenderer } from './src/ui/preview/fixture-adapter.ts';
          let disposeRenderer = null;
          const start = () => {
            if (!disposeRenderer) disposeRenderer = startFixtureRenderer();
          };
          if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', start, { once: true });
          } else {
            start();
          }
          window.addEventListener('pagehide', () => {
            disposeRenderer?.();
            disposeRenderer = null;
          }, { once: true });
        `,
        loader: 'ts',
        resolveDir: rootDir,
        sourcefile: 'visual-entry.ts',
      },
      outfile: path.join(visualOut, 'renderer.js'),
      bundle: true,
      platform: 'browser',
      target: 'es2020',
      format: 'iife',
      external: ['mermaid'],
      loader: {
        '.md': 'text',
        '.wasm': 'binary',
      },
      define: {
        __HELP_DOC_VERSION__: JSON.stringify(helpDocVersion),
        'import.meta.url': 'window.location.href',
      },
      sourcemap: isDev,
      minify: !isDev,
      logLevel: 'info',
    });
  }

  console.log('✓ Build complete');
}

if (isPreflight) {
  console.log('✓ Desktop production release configuration valid; no artifact built');
} else {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
