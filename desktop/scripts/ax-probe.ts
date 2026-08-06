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

/**
 * 录制链路端到端验证。
 *
 * 用 CDP `Input` 域派发真实输入 —— 它产生的事件 `isTrusted` 为 true，和用户手动
 * 操作走的是同一条路径（正因如此宿主的 before-input-event 才拦得住它）。页面用
 * `dispatchEvent` 合成的事件 `isTrusted` 为 false，会被录制器直接忽略。
 */
async function probeRecording(
  host: BrowserHost,
  profile: string,
  url: string,
  window: BrowserWindow,
): Promise<void> {
  const captured: Array<Record<string, unknown>> = [];
  host.on('recording', (event: unknown) => captured.push(event as Record<string, unknown>));

  const call = (command: string, args: string[]) =>
    host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: profile, proxy_url: PROXY_URL, command, args },
    }) as Promise<{ success: boolean; data: any }>;

  await call('tab', ['new', '--label', TAB_LABEL, url]);
  const tabs = (await call('tab', ['list'])).data;
  const targetId = tabs.tabs[0].targetId;

  // 视图必须真的挂到可见窗口上：点击走的是坐标命中测试，没有布局就没有命中点。
  // 走宿主自己的 setPanel（桌面端用的同一条路），而不是自己往窗口塞子视图。
  window.show();
  host.setPanel({
    runtimeKey: RUNTIME_KEY,
    sessionId: SESSION_ID,
    tabLabel: TAB_LABEL,
    mode: 'ai',
    bounds: { x: 0, y: 0, width: 1024, height: 720 },
    visible: true,
  });
  await new Promise((resolve) => setTimeout(resolve, 500));

  const setRecording = (action: string) =>
    host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'set_recording',
      params: { profile_dir: profile, proxy_url: PROXY_URL, target_id: targetId, action },
    });

  console.log(`\n${'='.repeat(78)}\n录制链路验证：${url}\n${'='.repeat(78)}`);
  await setRecording('start');
  console.log('  已开始录制，派发真实输入事件……');

  const refOf = (text: string, snapshot: string): string =>
    /\[ref=(@e\d+)\]/.exec(snapshot.split('\n').find((item) => item.includes(text)) ?? '')?.[1] ?? '';

  // 先点一个同文档的分类标签：这是 currentPageIdentity 看不见、只有内容摘要
  // 能识别的那类变化，必须验证轨迹里确实带上了新的页面态。
  const first = (await call('snapshot', ['--compact'])).data.snapshot as string;
  const tabRef = refOf('tab "数码"', first);
  if (tabRef) {
    await call('click', [tabRef]);
    await new Promise((resolve) => setTimeout(resolve, 600));
  }

  // 必须重新观察：切换标签后内容全换，旧 ref 会被 assertRefCurrent 以
  // stale_ref_security 拒绝——这正是 ref 生命周期该有的 fail-closed 行为。
  const second = (await call('snapshot', ['--compact'])).data.snapshot as string;
  const detailRef = refOf('link "详情"', second);
  console.log(`  切换分类后重新观察，目标 ref：${detailRef || '(没找到)'}`);
  if (detailRef) {
    await call('click', [detailRef]);
    await new Promise((resolve) => setTimeout(resolve, 800));
  }

  await setRecording('stop');
  console.log(`\n  捕获到 ${captured.length} 条录制事件：`);
  for (const event of captured) {
    const target = event.target as Record<string, unknown> | null;
    console.log(
      `    #${event.step} ${event.action} backendNodeId=${event.backendNodeId} `
      + `tier=${event.tier} value=${JSON.stringify(event.value)}`,
    );
    console.log(
      `        target=${target
        ? `<${target.tag}> text=${JSON.stringify(target.text)} `
          + `ordinal=${target.ordinal} href=${JSON.stringify(target.href)}`
        : '(无)'}`,
    );
    const page = String(event.page ?? '');
    console.log(
      `        page=${page ? `${page.split('\n').length} 行 / ${page.length} 字符` : '(与上一步相同，未重发)'}`,
    );
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
    if (process.argv.includes('--record')) {
      await probeRecording(host, profile, urls[0], window);
    } else {
      for (const [index, url] of urls.entries()) {
        await probe(host, profile, url, index === 0);
      }
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
