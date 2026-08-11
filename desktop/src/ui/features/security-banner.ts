/**
 * Codex 式「安装沙箱」顶部 banner。
 *
 * 显隐与状态由 gateway 的 /api/security/capabilities 派生：
 *   helper_present && filesystem_sandbox  → on（隐藏）
 *   helper_present && !filesystem_sandbox → off（黄条 + 安装按钮）
 *   !helper_present                       → missing（runtime 未找到）
 * 点击「安装」→ securitySetup('install')（UAC 提权）→ 成功后把当前会话切 auto_review。
 *
 * 设计：不引入 push 订阅；setup 是一次性用户动作，invoke 返回后重拉 capabilities 刷新即可。
 * 挂载点：.chat-composer 内、.composer-edit-banner 之前（与既有"正在编辑"提示条同带）。
 */

import { state, notify } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import { queryPrimaryComposer } from './composer-scope';
import { detectedRuntimePlatform, isMacOSPlatform, isWindowsPlatform } from './security-mode';
import { enableUacAndPromptRestart, prepareWindowsSecuritySetup } from './security-setup-flow';

type GatewayResult = { ok: boolean; status: number; body: unknown };

type SecurityCapabilities = {
  platform?: string;
  helper_present?: boolean;
  filesystem_sandbox?: boolean;
  managed_network?: boolean;
  runtime_stale?: boolean;
  state_dir_configured?: boolean;
  detail?: string;
};

type SecuritySetupResult = { ok: boolean; exitCode: number | null; detail?: string; code?: 'uac_disabled' | 'uac_restart_required' };

type BannerState = 'on' | 'off' | 'enabling' | 'failed' | 'missing' | 'stale' | 'hidden' | 'net-missing' | 'mac-incomplete' | 'restart-required' | 'service-restart-required';

const BANNER_ID = 'security-sandbox-banner';
const SEED_RETRY_MS = 5000;        // 失败后至少间隔 5s 再重试，避免每帧打 gateway
const REFRESH_INTERVAL_MS = 20000; // 周期刷新：覆盖 gateway 晚于桌面启动 / 沙箱状态变化
let current: BannerState = 'hidden';
let lastDetail = '';
let seeded = false;
let seeding = false;               // 一次 fetch 进行中
let lastSeedAttempt = 0;           // 上次 fetch 尝试时间戳（ms）
let refreshIntervalId: number | null = null;
let setupInFlight = false;
let uacRestartRequired = false;

/** 从 capabilities 派生 banner 状态。导出供单元测试覆盖网络降级分支（U3）。 */
export function deriveState(c: SecurityCapabilities): BannerState {
  if (isMacOSPlatform(c.platform)) {
    if (!c.helper_present || c.runtime_stale || !c.filesystem_sandbox || !c.managed_network) {
      return 'mac-incomplete';
    }
    return 'on';
  }
  if (!isWindowsPlatform(c.platform)) return 'hidden';
  if (!c.helper_present) return 'missing';
  if (c.state_dir_configured === false) return 'service-restart-required';
  if (!c.filesystem_sandbox) return 'off';
  // runtime 在、但 Rust 源码与已提交二进制不一致 -> 漂移，优先提示重 build
  if (c.runtime_stale) return 'stale';
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
      const platform = detectedRuntimePlatform(caps.platform);
      if (platform) caps.platform = platform;
      lastDetail = caps.detail ?? '';
      current = uacRestartRequired ? 'restart-required' : deriveState(caps);
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
  const reference = queryPrimaryComposer('.composer-edit-banner');
  const el = document.createElement('div');
  el.id = BANNER_ID;
  el.className = 'security-banner';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.innerHTML =
    '<span class="security-banner__icon"></span>' +
    '<span class="security-banner__content">' +
    '<strong class="security-banner__title"></strong>' +
    '<span class="security-banner__text"></span>' +
    '</span>' +
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
  const title = el.querySelector('.security-banner__title') as HTMLElement;
  const text = el.querySelector('.security-banner__text') as HTMLElement;
  const actions = el.querySelector('.security-banner__actions') as HTMLElement;
  if (current === 'off') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '请安装安全沙箱';
    text.textContent = '沙箱可限制命令对本机文件和网络的访问，降低误操作带来的安全风险。';
    actions.innerHTML =
      '<span class="security-banner__permission">需要管理员权限</span>' +
      '<button class="security-banner__btn" data-action="enable">安装安全沙箱</button>';
  } else if (current === 'enabling') {
    el.classList.add('is-info');
    icon.textContent = '…';
    title.textContent = '正在安装安全沙箱';
    text.textContent = '正在请求管理员权限并配置受限账户与网络防护。';
    actions.innerHTML = '<button class="security-banner__btn" disabled>处理中…</button>';
  } else if (current === 'failed') {
    el.classList.add('is-danger');
    icon.textContent = '!';
    const why = lastDetail ? `：${lastDetail}` : '';
    title.textContent = '安全沙箱安装未完成';
    text.textContent = `请重试安装${why}。安装完成前，受管命令不会执行。`;
    actions.innerHTML = '<button class="security-banner__btn" data-action="enable">重新安装</button>';
  } else if (current === 'missing') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '安全运行组件不可用';
    text.textContent = '未找到随应用提供的安全运行组件，请修复或重新安装应用。';
    actions.innerHTML = '';
  } else if (current === 'stale') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '安全运行组件需要更新';
    text.textContent = '当前运行组件与应用版本不一致，请重启或重新安装应用。';
    actions.innerHTML = '';
  } else if (current === 'net-missing') {
    // 文件沙箱已开但 WFP 缺失：降级语义必须直白，不能让用户以为出网受控（U3）。
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '网络防护尚未完成';
    text.textContent = '文件访问已受限，但网络防护规则尚未生效。';
    actions.innerHTML =
      '<span class="security-banner__permission">需要管理员权限</span>' +
      '<button class="security-banner__btn" data-action="enable">修复网络防护</button>';
  } else if (current === 'mac-incomplete') {
    el.classList.add('is-warn');
    icon.textContent = '!';
    title.textContent = '安全防护尚未就绪';
    text.textContent = lastDetail || '系统原生运行组件或联网管控未通过检测，请修复或重新安装应用。';
    actions.innerHTML = '';
  } else if (current === 'restart-required') {
    el.classList.add('is-info');
    icon.textContent = 'i';
    title.textContent = '请重启电脑以完成安全设置';
    text.textContent = 'UAC 已启用，重启后重新打开 Crew，即可继续安装安全沙箱。';
    actions.innerHTML = '<span class="security-banner__permission">等待重启</span>';
  } else if (current === 'service-restart-required') {
    el.classList.add('is-info');
    icon.textContent = 'i';
    title.textContent = '安全服务需要重启';
    text.textContent = lastDetail || '沙箱已安装，但当前 Gateway 尚未加载安全状态目录。请重启 Crew 后再试。';
    actions.innerHTML = '<span class="security-banner__permission">无需重新安装</span>';
  }
}

async function onEnable(): Promise<void> {
  if (setupInFlight) return;
  setupInFlight = true;
  try {
    const uacPreparation = await prepareWindowsSecuritySetup();
    if (uacPreparation !== 'ready') {
      uacRestartRequired = uacPreparation === 'restart-required';
      if (uacRestartRequired) setState('restart-required');
      return;
    }
    const accepted = await showConfirmDialog({
      title: '安装安全沙箱',
      message: '将请求一次系统管理员权限，创建受限执行环境并配置网络防护规则。安装完成后，受管命令将在安全边界内执行。是否继续？',
      confirmText: '安装并继续',
    });
    if (!accepted) return;
    setState('enabling');
    const result = (await window.Crew?.securitySetup?.({ action: 'install' })) as SecuritySetupResult | undefined;
    if (result?.code === 'uac_restart_required') {
      uacRestartRequired = true;
      setState('restart-required');
      return;
    }
    if (result?.code === 'uac_disabled') {
      const uacPreparation = await enableUacAndPromptRestart();
      uacRestartRequired = uacPreparation === 'restart-required';
      if (uacRestartRequired) setState('restart-required');
      return;
    }
    if (!result?.ok) {
      const detail = result?.detail || (result?.exitCode == null ? '未返回结果' : `退出码 ${result.exitCode}`);
      setState('failed', detail);
      notify(`沙箱安装未完成：${detail}。命令暂不可用`);
      return;
    }
    await refreshSecurityBanner();
    if (current !== 'on') {
      notify(`沙箱安装完成，但尚未通过运行检测${lastDetail ? `：${lastDetail}` : ''}`);
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
  } finally {
    setupInFlight = false;
  }
  await refreshSecurityBanner();
}
