import type {
  UpdateStateSnapshot,
  VersionUpdateDownloadProgressPayload,
  VersionUpdatePackageResult,
  VersionUpdatePayload,
} from '../../shared/types';
import { notify } from '../state';
import { setRuntimeStyle } from '../components/runtime-style';
import { sessionStore } from '../stores/stores';
import {
  closeAccountOverlay,
  ensureAccountOverlay,
  openAccountOverlay,
} from './account-overlays';

type VersionUpdatePhase = 'idle' | 'available' | 'downloading' | 'paused' | 'downloaded' | 'installing' | 'error';

interface DownloadArgs {
  version: string;
  type: 'force' | 'reminder';
  url?: string | undefined;
}

interface VersionUpdateBridge {
  onVersionUpdate?: (cb: (data: VersionUpdatePayload) => void) => (() => void) | void;
  onVersionUpdateDownloadProgress?: (cb: (data: VersionUpdateDownloadProgressPayload) => void) => (() => void) | void;
  startDownload?: (args: DownloadArgs) => Promise<{ success: boolean; message?: string }>;
  pauseDownload?: () => Promise<{ success: boolean }>;
  resumeDownload?: () => Promise<{ success: boolean }>;
  retryDownload?: (args: DownloadArgs) => Promise<{ success: boolean; message?: string }>;
  installUpdatePackage?: () => Promise<VersionUpdatePackageResult>;
  getUpdateState?: () => Promise<UpdateStateSnapshot>;
  appQuit?: () => Promise<void>;
  getAppVersion?: () => Promise<{ version?: string; label?: string }>;
  heartbeat?: (version?: string) => Promise<{
    success?: boolean;
    message?: string;
    data?: { version?: string; update?: string; url?: string };
  }>;
}

const FALLBACK_VERSION = '2.0.0';
let currentVersionLabel = `Crew Desktop ${FALLBACK_VERSION}`;

let pendingUpdate: VersionUpdatePayload | null = null;
let phase: VersionUpdatePhase = 'idle';
let percent: number | null = null;
let bound = false;
let unsubscribeSessionStore: (() => void) | null = null;

function bridge(): VersionUpdateBridge | undefined {
  return (window as Window & { Crew?: VersionUpdateBridge }).Crew;
}

function isForce(): boolean {
  return pendingUpdate?.type === 'force';
}

function hasBusySessions(): boolean {
  return Object.values(sessionStore.get().busySessions).some(Boolean);
}

function updateButton(): HTMLButtonElement | null {
  return document.getElementById('version-update-sidebar-btn') as HTMLButtonElement | null;
}

function clampPercent(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(100, Math.max(0, Math.round(value)))
    : null;
}

function setBadge(text: string): void {
  const badge = document.getElementById('set-version-badge');
  if (badge) badge.textContent = text;
}

function renderUpdateButton(): void {
  const button = updateButton();
  if (!button) return;

  // reminder 才在侧边栏常驻；force 走全屏阻断层，不显示按钮
  const visible = Boolean(pendingUpdate) && !isForce();
  button.hidden = !visible;
  button.classList.toggle('is-downloading', phase === 'downloading');
  button.classList.toggle('is-paused', phase === 'paused');
  button.classList.toggle('is-downloaded', phase === 'downloaded');
  button.classList.toggle('is-error', phase === 'error');
  // downloading 可点击（=暂停）；仅 installing 禁用
  button.disabled = phase === 'installing';

  button.title =
    phase === 'downloading'
      ? `正在下载更新${percent !== null ? ` ${percent}%（点击暂停）` : '（点击暂停）'}`
      : phase === 'paused'
        ? `已暂停${percent !== null ? ` ${percent}%（点击继续）` : '（点击继续）'}`
        : phase === 'downloaded'
          ? '更新包已下载，点击安装'
          : phase === 'error'
            ? '下载失败，点击重试'
            : pendingUpdate?.version
              ? `下载 Crew ${pendingUpdate.version} 更新`
              : '下载 Crew 更新';
  button.setAttribute('aria-label', button.title);

  const percentEl = document.getElementById('version-update-percent');
  if (percentEl) {
    const showPercent = (phase === 'downloading' || phase === 'paused') && percent !== null;
    percentEl.hidden = !showPercent;
    percentEl.textContent = showPercent ? `${percent}%` : '';
  }
}

function openInstallModal(): void {
  const versionEl = document.getElementById('version-install-version');
  const messageEl = document.getElementById('version-install-message');

  if (versionEl) versionEl.textContent = pendingUpdate?.version ? `版本 ${pendingUpdate.version}` : '新版本';
  if (messageEl) {
    messageEl.textContent = pendingUpdate?.message || '更新包已下载完成，是否现在安装？安装程序启动后应用会退出。';
  }

  openAccountOverlay('version-install-modal', {
    initialFocus: document.getElementById('version-install-now') ?? undefined,
  });
}

function closeInstallModal(): void {
  closeAccountOverlay('version-install-modal');
}

function showForceOverlay(): void {
  openAccountOverlay('force-update-overlay', {
    dismissible: false,
    initialFocus: document.getElementById('force-update-action') ?? undefined,
  });
  renderForceOverlay();
}

function hideForceOverlay(): void {
  closeAccountOverlay('force-update-overlay');
}

function syncForceOverlay(): void {
  if (!isForce()) return;
  if (hasBusySessions()) {
    hideForceOverlay();
    return;
  }
  const overlay = document.getElementById('force-update-overlay');
  if (overlay?.hidden || !overlay?.classList.contains('show')) {
    showForceOverlay();
  } else {
    renderForceOverlay();
  }
}

function renderForceOverlay(): void {
  const msg = document.getElementById('force-update-message');
  if (msg) msg.textContent = pendingUpdate?.message || '当前版本过低，请更新后继续使用。';
  const version = document.getElementById('force-update-version');
  if (version) {
    version.textContent = pendingUpdate?.version ? `版本 ${pendingUpdate.version}` : '新版本';
  }

  const btn = document.getElementById('force-update-action') as HTMLButtonElement | null;
  if (btn) {
    if (phase === 'downloading') {
      btn.textContent = '下载中…';
      btn.disabled = true;
    } else if (phase === 'installing') {
      btn.textContent = '安装中…';
      btn.disabled = true;
    } else if (phase === 'downloaded') {
      btn.textContent = '立即安装';
      btn.disabled = false;
    } else if (phase === 'paused') {
      btn.textContent = '继续下载';
      btn.disabled = false;
    } else {
      btn.textContent = phase === 'error' ? '重试下载' : '立即更新';
      btn.disabled = false;
    }
  }

  const progress = document.getElementById('force-update-progress');
  if (progress) {
    const active = phase === 'downloading' || phase === 'paused' || phase === 'installing';
    progress.classList.toggle('is-active', active);
  }
  const fill = document.getElementById('force-update-progress-fill');
  if (fill) setRuntimeStyle(fill, 'width', `${percent ?? (phase === 'downloaded' ? 100 : 0)}%`);
  const txt = document.getElementById('force-update-progress-text');
  if (txt) {
    txt.textContent =
      phase === 'downloading'
        ? `下载中 ${percent ?? 0}%`
        : phase === 'paused'
          ? `已暂停 ${percent ?? 0}%`
          : phase === 'installing'
            ? '安装中…'
            : '';
  }
}

function setPhase(nextPhase: VersionUpdatePhase, nextPercent = percent): void {
  phase = nextPhase;
  percent = clampPercent(nextPercent);
  renderUpdateButton();
  if (isForce()) renderForceOverlay();
}

function triggerDownload(mode: 'start' | 'retry'): void {
  if (!pendingUpdate?.version) {
    notify('更新版本信息缺失，请稍后重试');
    return;
  }
  const args: DownloadArgs = { version: pendingUpdate.version, type: pendingUpdate.type, url: pendingUpdate.url };
  console.log('[VersionUpdate] triggerDownload:', mode, args);
  setPhase('downloading', 0);
  const promise = mode === 'retry' ? bridge()?.retryDownload?.(args) : bridge()?.startDownload?.(args);
  promise?.then((r) => {
    console.log('[VersionUpdate] download result:', r);
    if (r && !r.success && r.message) notify(r.message);
  });
}

async function installDownloadedUpdate(): Promise<void> {
  if (hasBusySessions()) {
    notify('当前仍有会话在执行，请等待完成后再安装更新');
    return;
  }
  closeInstallModal();
  setPhase('installing', 100);
  const result = await bridge()?.installUpdatePackage?.();
  if (!result?.success) {
    // 安装失败：保留已下载包，回到 downloaded 态供再次安装
    setPhase('downloaded', 100);
    notify(result?.message || '启动安装失败，可重试');
  }
}

function handleVersionUpdate(payload: VersionUpdatePayload): void {
  console.log('[VersionUpdate] handleVersionUpdate:', payload);
  if (!payload?.version) {
    notify(payload?.message || '发现新版本，但无法确定版本号');
    return;
  }
  // 去重：同版本且仍在进行中（下载/暂停/已下载/安装）→ 只刷新文案，不重置（修掉每 5min 心跳重新触发的竞态）
  if (
    pendingUpdate &&
    pendingUpdate.version === payload.version &&
    (phase === 'downloading' || phase === 'paused' || phase === 'downloaded' || phase === 'installing')
  ) {
    pendingUpdate = { ...payload };
    if (isForce()) syncForceOverlay();
    return;
  }

  pendingUpdate = payload;
  if (payload.type === 'force') {
    // force：先让运行中的会话收尾，空闲后再阻断使用。
    setPhase('available', 0);
    syncForceOverlay();
  } else {
    // reminder：侧边栏常驻提醒
    hideForceOverlay();
    setPhase('available', 0);
    setBadge('有新版本');
  }
}

function handleDownloadProgress(payload: VersionUpdateDownloadProgressPayload): void {
  console.log('[VersionUpdate] handleDownloadProgress:', payload);
  if (payload.phase === 'downloading') {
    setPhase('downloading', payload.percent);
  } else if (payload.phase === 'paused') {
    setPhase('paused', payload.percent);
  } else if (payload.phase === 'downloaded') {
    const wasActive = phase === 'downloading' || phase === 'paused';
    setPhase('downloaded', 100);
    if (isForce()) {
      // force：下载完成自动安装，无需用户再点
      void installDownloadedUpdate();
    } else if (wasActive) {
      // reminder：刚下完 → 弹安装确认；若是启动恢复（非活跃下载来）则只亮按钮
      openInstallModal();
    }
  } else if (payload.phase === 'installing') {
    setPhase('installing', 100);
  } else if (payload.phase === 'completed') {
    setPhase('idle', 0);
    hideForceOverlay();
    setBadge('最新版');
  } else if (payload.phase === 'error') {
    setPhase('error', percent);
    if (payload.message) notify(payload.message);
  }
}

async function syncCurrentVersionLabel(): Promise<void> {
  try {
    const info = await bridge()?.getAppVersion?.();
    const version = info?.version?.trim() || FALLBACK_VERSION;
    currentVersionLabel = info?.label?.trim() || `Crew Desktop ${version}`;
    const versionText = document.getElementById('set-version-text');
    if (versionText) versionText.textContent = `v${version}`;
  } catch {
    const versionText = document.getElementById('set-version-text');
    if (versionText) versionText.textContent = `v${FALLBACK_VERSION}`;
  }
}

/** 启动恢复：若 update-state 记录了已下载待安装的包，恢复为 downloaded 态。 */
async function restoreFromState(): Promise<void> {
  try {
    const state = await bridge()?.getUpdateState?.();
    if (state?.downloaded) {
      pendingUpdate = {
        type: state.downloaded.type,
        title: '提示',
        message: state.downloaded.message || '更新包已下载完成，可立即安装。',
        version: state.downloaded.version,
        reportedVersion: currentVersionLabel,
      };
      setPhase('downloaded', 100);
    }
  } catch {
    /* 状态读取失败不影响主流程 */
  }
}

export function bindVersionUpdateUi(): void {
  if (bound) return;
  bound = true;
  ensureAccountOverlay('version-install-modal');
  ensureAccountOverlay('force-update-overlay');

  bridge()?.onVersionUpdate?.(handleVersionUpdate);
  bridge()?.onVersionUpdateDownloadProgress?.(handleDownloadProgress);
  unsubscribeSessionStore = sessionStore.subscribe((next, previous) => {
    if (next.busySessions !== previous.busySessions) syncForceOverlay();
  });
  void syncCurrentVersionLabel();
  void restoreFromState();

  // 侧边栏按钮（reminder）：按 phase 切换语义——开始/暂停/继续/安装/重试
  updateButton()?.addEventListener('click', () => {
    if (phase === 'downloaded') {
      openInstallModal();
      return;
    }
    if (phase === 'downloading') {
      void bridge()?.pauseDownload?.();
      return;
    }
    if (phase === 'paused') {
      void bridge()?.resumeDownload?.();
      return;
    }
    if (phase === 'error') {
      triggerDownload('retry');
      return;
    }
    triggerDownload('start');
  });

  document.getElementById('version-install-now')?.addEventListener('click', () => {
    void installDownloadedUpdate();
  });
  document.getElementById('version-install-later')?.addEventListener('click', closeInstallModal);
  document.getElementById('version-install-close')?.addEventListener('click', closeInstallModal);
  // force 阻断层：立即更新 / 退出
  document.getElementById('force-update-action')?.addEventListener('click', () => {
    if (phase === 'downloaded') {
      void installDownloadedUpdate();
      return;
    }
    if (phase === 'downloading' || phase === 'installing') return;
    if (phase === 'paused') {
      void bridge()?.resumeDownload?.();
      return;
    }
    triggerDownload(phase === 'error' ? 'retry' : 'start');
  });
  document.getElementById('force-update-exit')?.addEventListener('click', () => {
    if (!isForce() || hasBusySessions()) return;
    void bridge()?.appQuit?.();
  });

  document.getElementById('set-check-update')?.addEventListener('click', () => {
    void window.Crew?.openExternal?.('https://github.com/shuishenghualalala/Ace/releases');
  });
  renderUpdateButton();
}

export function resetVersionUpdateUiForTest(): void {
  unsubscribeSessionStore?.();
  unsubscribeSessionStore = null;
  pendingUpdate = null;
  phase = 'idle';
  percent = null;
  bound = false;
  currentVersionLabel = `Crew Desktop ${FALLBACK_VERSION}`;
}
