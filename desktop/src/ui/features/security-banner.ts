/**
 * Codex 式「开启沙箱」顶部 banner。
 *
 * 显隐与状态由 gateway 的 /api/security/capabilities 派生：
 *   helper_present && filesystem_sandbox  → on（隐藏）
 *   helper_present && !filesystem_sandbox → off（黄条 + 开启按钮）
 *   !helper_present                       → missing（runtime 未找到）
 * 点击「开启」→ securitySetup('install')（UAC 提权）→ 成功后把当前会话切 auto_review。
 *
 * 设计：不引入 push 订阅；setup 是一次性用户动作，invoke 返回后重拉 capabilities 刷新即可。
 * 挂载点：.chat-composer 内、#composer-edit-banner 之前（与既有"正在编辑"提示条同带）。
 */

import { state, notify } from '../state';

type GatewayResult = { ok: boolean; status: number; body: unknown };

type SecurityCapabilities = {
  platform?: string;
  helper_present?: boolean;
  filesystem_sandbox?: boolean;
  managed_network?: boolean;
  runtime_stale?: boolean;
  detail?: string;
};

type SecuritySetupResult = { ok: boolean; exitCode: number | null; detail?: string };

type BannerState = 'on' | 'off' | 'enabling' | 'failed' | 'missing' | 'stale' | 'hidden' | 'net-missing';

const BANNER_ID = 'security-sandbox-banner';
const SEED_RETRY_MS = 5000;        // 失败后至少间隔 5s 再重试，避免每帧打 gateway
const REFRESH_INTERVAL_MS = 20000; // 周期刷新：覆盖 gateway 晚于桌面启动 / 沙箱状态变化
let current: BannerState = 'hidden';
let lastDetail = '';
let seeded = false;
let seeding = false;               // 一次 fetch 进行中
let lastSeedAttempt = 0;           // 上次 fetch 尝试时间戳（ms）
let refreshIntervalId: number | null = null;

/** 从 capabilities 派生 banner 状态。导出供单元测试覆盖网络降级分支（U3）。 */
export function deriveState(c: SecurityCapabilities): BannerState {
  // 当前 banner 仅服务于 Windows 原生沙箱；其他平台走 Docker/bwrap，不在本条 banner 范围。
  if (c.platform && c.platform !== 'win32') return 'hidden';
  if (!c.helper_present) return 'missing';
  // runtime 在、但 Rust 源码与已提交二进制不一致 -> 漂移，优先提示重 build
  if (c.runtime_stale) return 'stale';
  if (!c.filesystem_sandbox) return 'off';
  // 文件沙箱已开但 WFP 网络管控缺失 -> 联网未受控。此前只看 filesystem_sandbox
  // 会显示 on 并隐藏 banner，让用户误以为出网受控（U3）。
  if (!c.managed_network) return 'net-missing';
  return 'on';
}

/** 拉取 capabilities 并重渲。网关不可达时不打扰（backend-status-guard 已有全屏提示）。 */
export async function refreshSecurityBanner(): Promise<void> {
  seeding = true;
  lastSeedAttempt = Date.now();
  try {
    const result = (await window.Crew?.securityCapabilities?.()) as GatewayResult | undefined;
    // 请求失败（未登录 / 网关没就绪 / proof 失败）时不能把空 body 当成"runtime 未找到"--
    // 否则未登录态会误报"未找到 runtime 二进制"，误导用户去查 exe。失败时隐藏 banner，
    // 让 backend-status-guard / 登录门处理；只有真正拿到 200 且 helper_present=false 才算 missing。
    if (!result?.ok) {
      current = 'hidden';
      lastDetail = '';
      seeded = false; // 允许后续重试（登录后/网关就绪后能重新拉到）
    } else {
      const caps = ((result.body ?? {}) as SecurityCapabilities);
      lastDetail = caps.detail ?? '';
      current = deriveState(caps);
      seeded = true; // 成功一次后不再每帧重试，交给周期刷新维持
    }
  } catch (error) {
    lastDetail = String(error);
    current = 'hidden';
    // 失败（如 gateway 尚未就绪）→ 允许后续重试（受 SEED_RETRY_MS 节流，不会每帧打 gateway）。
    seeded = false;
  } finally {
    seeding = false;
  }
  renderSecurityBanner();
}

/** 启动 20s 周期刷新（幂等）；pagehide 时停掉，避免 renderer 卸载后继续触发。 */
export function startSecurityBannerRefresh(): void {
  if (refreshIntervalId != null) return;
  refreshIntervalId = window.setInterval(() => {
    void refreshSecurityBanner();
  }, REFRESH_INTERVAL_MS);
  window.addEventListener('pagehide', stopSecurityBannerRefresh, { once: true });
}

export function stopSecurityBannerRefresh(): void {
  if (refreshIntervalId != null) {
    window.clearInterval(refreshIntervalId);
    refreshIntervalId = null;
  }
}

/**
 * 幂等渲染。由 chat-controller.updateComposerControls 每次 renderChat 调用。
 * 首次 / 上次失败后会触发一次异步 seed（seeding 防并发，SEED_RETRY_MS 防每帧打 gateway）；
 * 周期刷新由 startSecurityBannerRefresh 维持，覆盖 gateway 晚于桌面启动等场景。
 */
export function renderSecurityBanner(): void {
  const container = document.querySelector('.chat-composer');
  if (!container) return;
  const banner = ensureBanner(container);
  if (!banner) return;
  applyState(banner);
  startSecurityBannerRefresh();
  if (!seeded && !seeding && Date.now() - lastSeedAttempt > SEED_RETRY_MS) {
    void refreshSecurityBanner();
  }
}

function ensureBanner(container: Element): HTMLElement | null {
  const existing = document.getElementById(BANNER_ID);
  if (existing) return existing;
  const reference = document.getElementById('composer-edit-banner');
  const el = document.createElement('div');
  el.id = BANNER_ID;
  el.className = 'security-banner';
  el.innerHTML =
    '<span class="security-banner__icon"></span>' +
    '<span class="security-banner__text"></span>' +
    '<span class="security-banner__actions"></span>';
  container.insertBefore(el, reference);
  // 事件委托一次：data-action="enable" 触发 onEnable
  el.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    if (target.dataset.action === 'enable') void onEnable();
  });
  return el;
}

function setState(next: BannerState, detail = ''): void {
  current = next;
  if (detail) lastDetail = detail;
  const banner = document.getElementById(BANNER_ID);
  if (banner) applyState(banner);
}

function applyState(el: HTMLElement): void {
  const visible = current !== 'on' && current !== 'hidden';
  el.classList.toggle('show', visible);
  if (!visible) return;
  el.classList.remove('is-warn', 'is-success', 'is-danger', 'is-info');
  const icon = el.querySelector('.security-banner__icon') as HTMLElement;
  const text = el.querySelector('.security-banner__text') as HTMLElement;
  const actions = el.querySelector('.security-banner__actions') as HTMLElement;
  if (current === 'off') {
    el.classList.add('is-warn');
    icon.textContent = '🟡';
    text.textContent = '沙箱未启用 · 命令以本机完整权限执行';
    actions.innerHTML = '<button class="security-banner__btn" data-action="enable">开启沙箱（需管理员）</button>';
  } else if (current === 'enabling') {
    el.classList.add('is-info');
    icon.textContent = '⏳';
    text.textContent = '正在提权并安装沙箱账户…';
    actions.innerHTML = '<button class="security-banner__btn" disabled>处理中…</button>';
  } else if (current === 'failed') {
    el.classList.add('is-danger');
    icon.textContent = '⭕';
    const why = lastDetail ? `：${lastDetail}` : '';
    text.textContent = `沙箱启用失败${why}（对话仍可用，但以完整权限执行）`;
    actions.innerHTML = '<button class="security-banner__btn" data-action="enable">重试</button>';
  } else if (current === 'missing') {
    el.classList.add('is-warn');
    icon.textContent = '⚠️';
    text.textContent = '未找到 runtime 二进制（security-runtime/bin/ 下无预编译 exe，也未设 ACE_SECURITY_RUNTIME）';
    actions.innerHTML = '';
  } else if (current === 'stale') {
    el.classList.add('is-warn');
    icon.textContent = '🔄';
    text.textContent = 'runtime 二进制落后于 Rust 源码：改了 security-runtime/ 需重跑 scripts/build-security-runtime 再提交';
    actions.innerHTML = '';
  } else if (current === 'net-missing') {
    // 文件沙箱已开但 WFP 缺失：降级语义必须直白，不能让用户以为出网受控（U3）。
    el.classList.add('is-warn');
    icon.textContent = '🟡';
    text.textContent = '沙箱已启用但联网未受控 · 文件访问受限，出网规则（WFP）缺失';
    actions.innerHTML = '<button class="security-banner__btn" data-action="enable">修复网络管控（需管理员）</button>';
  }
}

async function onEnable(): Promise<void> {
  if (!window.confirm('将弹出一次 Windows UAC，用于创建沙箱技术账号与网络规则。继续吗？')) return;
  setState('enabling');
  try {
    const result = (await window.Crew?.securitySetup?.({ action: 'install' })) as SecuritySetupResult | undefined;
    if (!result?.ok) {
      const detail = result?.detail || (result?.exitCode == null ? '未返回结果' : `退出码 ${result.exitCode}`);
      setState('failed', detail);
      notify('沙箱启用未完成；对话仍以完整权限执行');
      return;
    }
    // setup 成功 → 把当前活跃会话切到 AUTO_REVIEW（无活跃会话则仅完成基础设施安装）
    const sid = state.activeSessionId;
    if (sid) {
      const workspaceId =
        state.sessions.find((session) => session.id === sid)?.workspaceId ?? 'default';
      await window.Crew?.securitySetMode?.({ workspaceId, sessionId: sid, mode: 'auto_review' });
      notify('沙箱已启用，当前会话已切换为 AUTO_REVIEW');
    } else {
      notify('沙箱基础设施已就绪');
    }
  } catch (error) {
    setState('failed', String(error));
  }
  await refreshSecurityBanner();
}
