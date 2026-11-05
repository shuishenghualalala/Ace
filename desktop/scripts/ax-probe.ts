/**
 * AX 快照探针：用 Crew 自己的 BrowserHost 对任意 URL 打一次快照，把 compact 与
 * full 两种模式的真实输出打出来。
 *
 * 存在的理由：整套「浏览器录制 → 技能」方案押在「AX 快照够用」这个前提上，而这个
 * 前提只能用**宿主自己的 snapshot()** 来验——第三方浏览器的无障碍树读数只能做参考，
 * 不能替代。桌面端没有 Electron 集成测试地基（vitest 跑在 node env），所以单开这个
 * 探针，它同时也是 P1 开发期的量测工具。
 *
 * 用法：
 *   # 1) 先起 Crew 的网络策略代理（宿主强制要求，见 parseProxy 的 proxy_required）
 *   python3 -m tests.fixtures.policy_proxy_runner --allow-host 127.0.0.1
 *   # 2) 把它打印的 URL 传进来
 *   CREW_PROXY_URL=http://user:pass@127.0.0.1:PORT \
 *     node_modules/.bin/electron scripts/ax-probe.mjs <url> [more urls...]
 *
 * 该 .mjs 由 scripts/ax-probe.build.mjs 从本文件打包生成（external: electron）。
 */

import { app, BrowserWindow } from 'electron';
import { createHash } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { BrowserHost } from '../src/main/browser-host';

// runtime_key 必须匹配 /^crew_[0-9a-f]{12}$/，profile 必须落在
// .../accounts/acct_<16hex>/browser/profile 且前 12 位与 runtime_key 后缀一致。
const RUNTIME_KEY = 'crew_a1b2c3d4e5f6';
const ACCOUNT_DIR = 'acct_a1b2c3d4e5f60000';
// 标签页 label 必须是 `s<sessionId 的 sha256 前 32 位>-<序号>`，setPanel 会按
// sessionId 反查校验（requirePanelTab）。所以这里从真实 sessionId 算出来。
const SESSION_ID = 'crew-ax-probe-session';
const TAB_LABEL = `s${createHash('sha256').update(SESSION_ID, 'utf8').digest('hex').slice(0, 32)}-1`;

// 宿主拒绝不走代理的浏览器（parseProxy → proxy_required），所以这里没有默认值：
// 忘了起代理就该直接失败，而不是量出一份绕过网络策略的假数据。
const PROXY_URL = process.env.CREW_PROXY_URL ?? '';

// 打快照前的等待毫秒数。静态页给 0 就够；SPA 必须给足，否则量到的是半张页面。
const WAIT_MS = Number(process.env.CREW_PROBE_WAIT_MS ?? '2500');

interface SnapshotResult {
  snapshot: string;
  url: string;
  title: string;
  ref_keys?: Record<string, string>;
  ref_actions?: Record<string, string>;
}

function summarize(label: string, text: string): void {
  const lines = text.split('\n').filter((line) => line.trim());
  const refs = lines.filter((line) => line.includes('[ref=')).length;
  const submits = lines.filter((line) => line.includes('[action=submit]')).length;
  console.log(`\n  ── ${label} ──`);
  console.log(`  行数 ${lines.length} | 带 ref ${refs} | 标为 submit ${submits} | ${text.length} 字符`);
  for (const line of lines) console.log(`    ${line}`);
}

async function probe(host: BrowserHost, profile: string, url: string, first: boolean): Promise<void> {
  const call = (command: string, args: string[]) =>
    host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: profile, proxy_url: PROXY_URL, command, args },
    }) as Promise<{ success: boolean; data: unknown }>;

  if (first) {
    await call('tab', ['new', '--label', TAB_LABEL, url]);
  } else {
    await call('open', [url]);
  }

  console.log(`\n${'='.repeat(78)}\n${url}\n${'='.repeat(78)}`);

  // SPA 站点在 load 事件之后才渲染主体内容。本探针直连宿主，绕过了 Python 侧
  // 的稳定门（manager.py 的 _stable_capture_marker），所以必须自己等——否则量到
  // 的是「页面还没渲染完」，会被误读成「compact 模式内容少」。
  if (WAIT_MS > 0) await new Promise((resolve) => setTimeout(resolve, WAIT_MS));

  const compact = (await call('snapshot', ['--compact'])).data as SnapshotResult;
  const full = (await call('snapshot', [])).data as SnapshotResult;
  // 再打一次 compact：与第一次一致才说明页面已经稳定，两次不一致就是还在渲染，
  // 这一轮的数字不能用。
  const recheck = (await call('snapshot', ['--compact'])).data as SnapshotResult;

  const settled = recheck.snapshot === compact.snapshot;
  if (!settled) {
    console.log(
      `\n  ⚠️ 页面未稳定：两次 compact 不一致（${compact.snapshot.length} → `
      + `${recheck.snapshot.length} 字符）。加大 --wait 后重测，本轮数据不可用。`,
    );
  }
  summarize('compact（动作后自动返回的就是这一种）', settled ? compact.snapshot : recheck.snapshot);
  summarize('full（snapshot(full=true) 才拿得到）', full.snapshot);

  // 能力档拒绝提交类点击、以及提交类点击强制审批，判据都来自这份显式下发的映射。
  const actions = Object.entries(full.ref_actions ?? {});
  console.log(`\n  ── ref_actions（能力档与审批的机制判据）──`);
  if (actions.length === 0) {
    console.log('    （本页没有提交类元素）');
  } else {
    for (const [ref, action] of actions) {
      const line = full.snapshot.split('\n').find((item) => item.includes(`[ref=${ref}]`));
      console.log(`    ${ref} -> ${action}   ${line?.trim() ?? ''}`);
    }
  }
}

async function main(): Promise<void> {
  const urls = process.argv.slice(2).filter((arg) => /^https?:\/\//.test(arg));
  if (urls.length === 0) {
    console.error('用法：CREW_PROXY_URL=... electron scripts/ax-probe.mjs <url> [more urls...]');
    app.exit(2);
    return;
  }
  if (!PROXY_URL) {
    console.error(
      '缺少 CREW_PROXY_URL。先运行：\n'
      + '  python3 -m tests.fixtures.policy_proxy_runner --allow-host 127.0.0.1',
    );
    app.exit(2);
    return;
  }

  const root = mkdtempSync(path.join(tmpdir(), 'crew-ax-probe-'));
  const profile = path.join(root, 'accounts', ACCOUNT_DIR, 'browser', 'profile');

  // 视图不挂到窗口上时，Chromium 可能把整棵树判为不可见而大量 ignored，
  // 量出来的东西就不是真实的了。所以老老实实开一个窗口。
  const window = new BrowserWindow({ width: 1024, height: 720, show: false });
  const host = new BrowserHost(() => window);

  try {
    for (const [index, url] of urls.entries()) {
      await probe(host, profile, url, index === 0);
    }
  } finally {
    await host.dispose();
    if (!window.isDestroyed()) window.destroy();
  }
}

app.disableHardwareAcceleration();
app.whenReady().then(
  async () => {
    try {
      await main();
      app.exit(0);
    } catch (error) {
      console.error('\n探针失败：', error);
      app.exit(1);
    }
  },
  (error: unknown) => {
    console.error('Electron 启动失败：', error);
    process.exit(1);
  },
);
