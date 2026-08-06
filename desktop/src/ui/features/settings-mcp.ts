/**
 * MCP 服务管理面板（设置弹窗的一个 Pane）。
 *
 * - 列出后端 /api/mcp/servers 配置的所有 MCP server + 连接状态 + 已注册工具，
 *   支持新增 / 编辑 / 删除 / 单 server 重连。登录用户可用（未登录 401）。
 * - 顶部 Computer Use（CUA Driver）一键安装卡片：调 /api/mcp/cua-driver/* 异步安装，
 *   轮询 steps/log 进度，装完自动注册成 MCP server。
 *
 * 渲染沿用 conn-row 卡片 + 弹层模式（与模型/渠道页一致），不引入新样式体系。
 */

import {
  backendApi,
  type McpServerRow,
  type McpServerPayload,
  type McpTransport,
  type CuaDriverStatus,
  type CuaSetupProgress,
} from '../backend-client';
import { showConfirmDialog } from '../ui-feedback';
import { $, notify } from '../state';
import { createIcon } from '../components/icon';
import {
  createSettingsIntegrationView,
  type SettingsIntegrationItem,
  type SettingsIntegrationView,
} from './settings-integrations';

const TRANSPORT_LABEL: Record<McpTransport, string> = {
  stdio: '本地(stdio)',
  http: 'HTTP',
  sse: 'SSE',
  unknown: '未知',
};

let mcpIntegrationView: SettingsIntegrationView | null = null;
let lastMcpServers: McpServerRow[] = [];

function ensureMcpIntegrationView(): SettingsIntegrationView | null {
  const pane = document.getElementById('settings-pane-mcp');
  if (!pane) return null;
  if (!mcpIntegrationView) {
    mcpIntegrationView = createSettingsIntegrationView({
      kind: 'mcp',
      title: 'MCP 服务',
      description: '管理外部工具服务、传输方式和连接状态。',
      primaryAction: { label: '添加服务' },
      onPrimaryAction: () => void openMcpServerModal(),
      onSelect: (name) => {
        const server = lastMcpServers.find((candidate) => candidate.name === name);
        if (server) void openMcpServerModal(server);
      },
      onAction: (action, name) => {
        if (action === 'reload') void reloadMcpServer(name);
        if (action === 'delete') void deleteMcpServer(name);
      },
    });
    const card = document.createElement('article');
    const symbol = document.createElement('span');
    const copy = document.createElement('span');
    const title = document.createElement('strong');
    const description = document.createElement('span');
    const action = document.createElement('button');
    const progress = document.createElement('div');
    card.id = 'cua-driver-card';
    card.className = 'settings-integrations__cua';
    symbol.className = 'settings-integrations__symbol';
    symbol.append(createIcon('process-terminal', { size: 20 }));
    copy.className = 'settings-integrations__copy';
    title.className = 'settings-integrations__item-title';
    title.textContent = 'Computer Use';
    description.id = 'cua-driver-desc';
    description.className = 'settings-integrations__item-description';
    description.textContent = '检测中…';
    copy.append(title, description);
    action.id = 'cua-driver-action';
    action.type = 'button';
    action.className = 'mw-button mw-button--secondary mw-button--small';
    action.textContent = '一键安装';
    card.append(symbol, copy, action);
    progress.id = 'cua-driver-progress';
    progress.className = 'cua-driver-progress';
    progress.hidden = true;
    mcpIntegrationView.leading.append(card, progress);
  }
  if (!pane.contains(mcpIntegrationView.element)) pane.replaceChildren(mcpIntegrationView.element);
  return mcpIntegrationView;
}

export function mcpStatusText(s: McpServerRow): string {
  if (s.error) return `失败：${s.error}`;
  if (s.connected) return '已连接';
  return '未连接';
}

export function mcpStatusChipClass(s: McpServerRow): string {
  if (s.error) return 'is-error';
  if (s.connected) return 'is-online';
  return 'is-configured';
}

export function mcpTransportLabel(t: McpTransport): string {
  return TRANSPORT_LABEL[t] ?? t;
}

// ── Computer Use（CUA Driver）一键安装 ──────────────────────────────

/** CUA 安装步骤名的中文展示。 */
const CUA_STEP_LABEL: Record<string, string> = {
  detect_platform: '检测平台',
  install_binary: '安装驱动',
  install_deps: '安装系统依赖',
  start_daemon: '启动后台服务',
  update_config: '写入 MCP 配置',
  reload_mcp: '热重载 MCP',
};

/** CUA Driver 当前状态 → 描述文案 + 按钮文案。纯函数，便于单测。 */
export function cuaDriverStatusText(s: CuaDriverStatus): { desc: string; action: string } {
  if (!s.ok) return { desc: '状态查询失败', action: '重试' };
  if (!s.installed) return { desc: '未安装，点击一键安装 Computer Use 驱动', action: '一键安装' };
  const toolCount = s.tools_registered.length;
  if (s.daemon_running && s.mcp_enabled) {
    return {
      desc: `已就绪 · v${s.version || '?'} · ${toolCount} 个工具已注册`,
      action: '重新安装',
    };
  }
  if (s.installed && !s.daemon_running) {
    return { desc: `已安装 v${s.version || '?'}，后台服务未运行`, action: '启动并安装' };
  }
  return { desc: `已安装 v${s.version || '?'}，MCP 未启用`, action: '一键安装' };
}

/** CUA 安装步骤 status → chip class。 */
export function cuaStepChipClass(status: string): string {
  switch (status) {
    case 'success': return 'is-online';
    case 'running': return 'is-configured';
    case 'failed': return 'is-error';
    case 'skipped': return '';
    default: return ''; // pending
  }
}

/** CUA 安装步骤 status → 中文。 */
export function cuaStepStatusText(status: string): string {
  switch (status) {
    case 'success': return '完成';
    case 'running': return '进行中';
    case 'failed': return '失败';
    case 'skipped': return '跳过';
    case 'pending': return '等待';
    default: return status;
  }
}

// 轮询状态（照 kanban-board.ts 骨架：模块级 timer + taskId 防竞态 + 终态自停）
let cuaPollingTimer: number | null = null;
let cuaTaskId: string | null = null;
const CUA_POLL_MS = 1500;

/** 终态：停止轮询。 */
function isCuaTerminalStatus(status: string): boolean {
  return status === 'success' || status === 'failed' || status === 'cancelled';
}

/** 渲染 CUA 卡片当前状态（拉 /api/mcp/cua-driver/status）。 */
export async function renderCuaDriverCard(): Promise<void> {
  const card = $('#cua-driver-card');
  const descEl = $('#cua-driver-desc');
  const actionBtn = $('#cua-driver-action') as HTMLButtonElement | null;
  if (!card || !descEl || !actionBtn) return;

  // 安装进行中时按钮转为「取消」语义（仍可点击 → 触发 cancelCuaSetup）
  if (cuaPollingTimer !== null) {
    actionBtn.disabled = false;
    actionBtn.textContent = '取消安装';
    actionBtn.title = '点击取消正在进行的安装';
    return;
  }
  actionBtn.title = '';

  let status: CuaDriverStatus;
  try {
    status = await backendApi.cuaDriverStatus();
  } catch (error) {
    descEl.textContent = `无法检测状态：${(error as Error).message}`;
    actionBtn.disabled = false;
    actionBtn.textContent = '一键安装';
    return;
  }
  const { desc, action } = cuaDriverStatusText(status);
  descEl.textContent = desc;
  actionBtn.textContent = action;
  actionBtn.disabled = false;
}

/** 渲染安装进度（steps + log）到 #cua-driver-progress。 */
export function renderCuaProgress(progress: CuaSetupProgress): void {
  const box = $('#cua-driver-progress');
  if (!box) return;
  box.hidden = false;
  // DOM 构建（textContent 自动转义），满足安全门禁「禁止 innerHTML 模板插值」。
  const nodes: HTMLElement[] = [];
  for (const st of progress.steps ?? []) {
    const step = document.createElement('div');
    step.className = 'cua-step';
    const chip = document.createElement('span');
    chip.className = `channel-status-chip ${cuaStepChipClass(st.status)}`;
    const dot = document.createElement('span');
    dot.className = 'channel-status-dot';
    chip.append(dot);
    const name = document.createElement('span');
    name.className = 'cua-step__name';
    name.textContent = CUA_STEP_LABEL[st.name] ?? st.name;
    const status = document.createElement('span');
    status.className = 'cua-step__status';
    status.textContent = `${cuaStepStatusText(st.status)}${st.message ? ` · ${st.message}` : ''}`;
    step.append(chip, name, status);
    nodes.push(step);
  }
  if ((progress.log ?? []).length) {
    const pre = document.createElement('pre');
    pre.className = 'cua-step__log';
    pre.textContent = progress.log.slice(-12).join('\n');
    nodes.push(pre);
  }
  if (progress.error) {
    const err = document.createElement('div');
    err.className = 'cua-step__error';
    err.textContent = `错误：${progress.error}`;
    nodes.push(err);
  }
  box.replaceChildren(...nodes);
}

/** 隐藏进度框。 */
function clearCuaProgress(): void {
  const box = $('#cua-driver-progress');
  if (box) {
    box.hidden = true;
    box.replaceChildren();
  }
}

/** 轮询单次刷新（两次校验 taskId 防竞态）。 */
async function refreshCuaSetup(taskId: string): Promise<void> {
  if (taskId !== cuaTaskId) return;
  let progress: CuaSetupProgress & { ok: boolean };
  try {
    progress = await backendApi.cuaDriverSetupStatus(taskId);
  } catch {
    return; // 单次失败不中断轮询
  }
  if (taskId !== cuaTaskId) return; // 异步期间 task 已变
  renderCuaProgress(progress);
  if (isCuaTerminalStatus(progress.status)) {
    stopCuaSetupPolling();
    const actionBtn = $('#cua-driver-action') as HTMLButtonElement | null;
    if (actionBtn) actionBtn.disabled = false;
    if (progress.status === 'success') {
      notify('Computer Use 安装完成');
      clearCuaProgress();
      void renderMcpServers(); // cua-driver 应出现在列表
    } else if (progress.status === 'failed') {
      notify(`安装失败：${progress.error || '见进度详情'}`);
    } else {
      notify('安装已取消');
      clearCuaProgress();
    }
    void renderCuaDriverCard();
  }
}

function startCuaSetupPolling(taskId: string): void {
  if (cuaPollingTimer !== null) {
    if (cuaTaskId === taskId) return;
    stopCuaSetupPolling();
  }
  cuaTaskId = taskId;
  cuaPollingTimer = window.setInterval(() => {
    void refreshCuaSetup(taskId);
  }, CUA_POLL_MS);
}

export function stopCuaSetupPolling(): void {
  if (cuaPollingTimer !== null) {
    window.clearInterval(cuaPollingTimer);
    cuaPollingTimer = null;
  }
  cuaTaskId = null;
}

/** 触发一键安装。 */
async function startCuaSetup(): Promise<void> {
  const actionBtn = $('#cua-driver-action') as HTMLButtonElement | null;
  // 请求 setup 接口期间禁用按钮防重复点击；拿到 task_id 后由 renderCuaDriverCard 转为「取消安装」
  if (actionBtn) {
    actionBtn.disabled = true;
    actionBtn.textContent = '启动安装…';
  }
  try {
    const resp = await backendApi.cuaDriverSetup({ start_daemon: true });
    if (!resp.task_id) {
      notify('安装任务启动失败');
      void renderCuaDriverCard();
      return;
    }
    startCuaSetupPolling(resp.task_id);
    void renderCuaDriverCard(); // 按钮转为「取消安装」可点击
  } catch (error) {
    notify(`安装启动失败：${(error as Error).message}`);
    if (actionBtn) actionBtn.disabled = false;
    void renderCuaDriverCard();
  }
}

/** 取消安装。 */
async function cancelCuaSetup(): Promise<void> {
  if (!cuaTaskId) return;
  const confirmed = await showConfirmDialog({
    title: '取消 Computer Use 安装',
    message: '确定取消正在进行的安装任务吗？',
    confirmText: '取消安装',
    cancelText: '继续安装',
  });
  if (!confirmed) return;
  const taskId = cuaTaskId;
  try {
    await backendApi.cuaDriverCancel(taskId);
    stopCuaSetupPolling();
    clearCuaProgress();
    void renderCuaDriverCard();
    notify('已取消安装');
  } catch (error) {
    notify(`取消失败：${(error as Error).message}`);
  }
}

/** 绑定 CUA 卡片按钮（幂等）。 */
let cuaBound = false;
function bindCuaCardOnce(): void {
  if (cuaBound) return;
  cuaBound = true;
  const actionBtn = $('#cua-driver-action');
  actionBtn?.addEventListener('click', () => {
    if (cuaPollingTimer !== null) {
      void cancelCuaSetup();
    } else {
      void startCuaSetup();
    }
  });
}

export async function renderMcpServers(): Promise<void> {
  const view = ensureMcpIntegrationView();
  if (!view) return;
  view.update({ state: 'loading', message: '正在加载 MCP 服务…', items: [] });

  let servers: McpServerRow[] = [];
  try {
    const resp = await backendApi.mcpServers();
    servers = resp.servers ?? [];
  } catch (error) {
    lastMcpServers = [];
    view.update({
      state: 'error',
      message: `无法加载 MCP 服务：${(error as Error).message}`,
      items: [],
    });
    return;
  }

  lastMcpServers = servers;
  if (servers.length === 0) {
    view.update({
      state: 'empty',
      message: '暂无 MCP 服务，点击“添加服务”开始配置。',
      items: [],
    });
    return;
  }

  const items: SettingsIntegrationItem[] = servers.map((server) => ({
    id: server.name,
    title: server.name,
    description: `${mcpTransportLabel(server.transport)} · ${
      server.tools.length ? `${server.tools.length} 个工具` : '无工具'
    }`,
    status: server.error ? '失败' : mcpStatusText(server),
    ...(server.error ? { error: server.error } : {}),
    tone: server.error ? 'danger' : server.connected ? 'success' : 'warning',
    selectable: true,
    icon: 'process-terminal',
    actions: [
      { id: 'reload', label: '重连' },
      { id: 'delete', label: '删除', tone: 'danger' },
    ],
  }));
  view.update({ state: 'ready', message: '', items });
}

async function reloadMcpServer(name: string): Promise<void> {
  try {
    const response = await backendApi.reloadMcpServer(name);
    notify(response.ok ? `${name} 已重连` : `${name} 重连失败`);
  } catch (error) {
    notify(`重连失败：${(error as Error).message}`);
  }
  await renderMcpServers();
}

async function deleteMcpServer(name: string): Promise<void> {
  const confirmed = await showConfirmDialog({
    title: `删除 MCP 服务 ${name}`,
    message: '删除后该服务及其工具将不再可用。此操作不可撤销。',
    confirmText: '删除',
    cancelText: '取消',
  });
  if (!confirmed) return;
  try {
    await backendApi.deleteMcpServer(name);
    notify(`${name} 已删除`);
    await renderMcpServers();
  } catch (error) {
    notify(`删除失败：${(error as Error).message}`);
  }
}

/** 读取弹层表单为 payload。 */
function readMcpForm(): McpServerPayload {
  const value = (id: string): string => (document.getElementById(id) as HTMLInputElement | null)?.value.trim() ?? '';
  const transport = (document.querySelector<HTMLInputElement>('input[name="mcp-transport"]:checked')?.value ?? 'stdio') as McpTransport;
  const payload: McpServerPayload = { transport };
  if (transport === 'stdio') {
    payload.command = value('mcp-command');
    const argsRaw = value('mcp-args');
    payload.args = argsRaw ? argsRaw.split(',').map((a) => a.trim()).filter(Boolean) : [];
  } else {
    payload.url = value('mcp-url');
  }
  // env：多行 KEY=VALUE
  const envRaw = value('mcp-env');
  if (envRaw) {
    const env: Record<string, string> = {};
    for (const line of envRaw.split(/\r?\n/)) {
      const idx = line.indexOf('=');
      if (idx > 0) {
        const k = line.slice(0, idx).trim();
        const v = line.slice(idx + 1).trim();
        if (k) env[k] = v;
      }
    }
    if (Object.keys(env).length) payload.env = env;
  }
  // headers（http/sse）
  if (transport !== 'stdio') {
    const headersRaw = value('mcp-headers');
    if (headersRaw) {
      const headers: Record<string, string> = {};
      for (const line of headersRaw.split(/\r?\n/)) {
        const idx = line.indexOf(':');
        if (idx > 0) {
          const k = line.slice(0, idx).trim();
          const v = line.slice(idx + 1).trim();
          if (k) headers[k] = v;
        }
      }
      if (Object.keys(headers).length) payload.headers = headers;
    }
  }
  return payload;
}

function setTransportFields(transport: McpTransport): void {
  const stdioWrap = document.getElementById('mcp-stdio-wrap');
  const remoteWrap = document.getElementById('mcp-remote-wrap');
  if (stdioWrap) stdioWrap.hidden = transport !== 'stdio';
  if (remoteWrap) remoteWrap.hidden = transport === 'stdio';
}

/** 填充弹层（新增或编辑）。编辑时密钥类 env 显示为空（留空保留原值由后端处理）。 */
function fillMcpForm(server?: McpServerRow): void {
  const set = (id: string, v: string): void => {
    const el = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null;
    if (el) el.value = v;
  };
  const nameInput = document.getElementById('mcp-name') as HTMLInputElement | null;
  const transport = server?.config.transport && server.config.transport !== 'unknown'
    ? server.config.transport
    : (server?.config.command ? 'stdio' : 'http');
  document.querySelectorAll<HTMLInputElement>('input[name="mcp-transport"]').forEach((input) => {
    input.checked = input.value === transport;
  });
  setTransportFields(transport);

  if (server) {
    set('mcp-name', server.name);
    if (nameInput) nameInput.disabled = true; // 编辑时名称不可改
    set('mcp-command', server.config.command ?? '');
    set('mcp-args', (server.config.args ?? []).join(', '));
    set('mcp-url', server.config.url ?? '');
    // env：脱敏值（***）不回填，提示留空保留
    const env = server.config.env ?? {};
    const envLines = Object.entries(env)
      .filter(([, v]) => v !== '***')
      .map(([k, v]) => `${k}=${v}`);
    set('mcp-env', envLines.join('\n'));
    const headers = server.config.headers ?? {};
    set('mcp-headers', Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join('\n'));
  } else {
    set('mcp-name', '');
    if (nameInput) nameInput.disabled = false;
    set('mcp-command', '');
    set('mcp-args', '');
    set('mcp-url', '');
    set('mcp-env', '');
    set('mcp-headers', '');
  }
}

function closeMcpServerModal(): void {
  const overlay = $('#mcp-server-overlay') as HTMLElement | null;
  if (overlay) overlay.hidden = true;
}

/** 打开新增/编辑弹层。 */
export async function openMcpServerModal(server?: McpServerRow): Promise<void> {
  const overlay = $('#mcp-server-overlay') as HTMLElement | null;
  if (!overlay) return;
  fillMcpForm(server);
  const title = document.getElementById('mcp-server-title');
  if (title) title.textContent = server ? `编辑：${server.name}` : '新增 MCP 服务';
  const deleteBtn = document.getElementById('mcp-server-delete') as HTMLButtonElement | null;
  if (deleteBtn) deleteBtn.hidden = !server;
  overlay.hidden = false;
}

let formBound = false;
function bindMcpFormOnce(): void {
  if (formBound) return;
  formBound = true;

  // 传输类型切换显隐字段
  document.querySelectorAll<HTMLInputElement>('input[name="mcp-transport"]').forEach((input) => {
    input.addEventListener('change', () => {
      if (input.checked) setTransportFields(input.value as McpTransport);
    });
  });

  const form = document.getElementById('mcp-server-form') as HTMLFormElement | null;
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    void (async (): Promise<void> => {
      const nameInput = document.getElementById('mcp-name') as HTMLInputElement | null;
      const editing = !!nameInput?.disabled;
      const payload = readMcpForm();
      if (!editing && !nameInput?.value.trim()) {
        notify('请填写服务名称');
        return;
      }
      if (payload.transport === 'stdio' && !payload.command) {
        notify('请填写 command');
        return;
      }
      if (payload.transport !== 'stdio' && !payload.url) {
        notify('请填写 URL');
        return;
      }
      try {
        if (editing) {
          await backendApi.updateMcpServer(nameInput!.value.trim(), payload);
          notify('MCP 服务已更新');
        } else {
          payload.name = nameInput!.value.trim();
          await backendApi.createMcpServer(payload);
          notify('MCP 服务已新增，正在后台连接…');
        }
        await renderMcpServers();
        closeMcpServerModal();
        // 后台连接需数秒（worker.start 最多 30s 启动超时）。延迟二次刷新让 connected
        // 状态从 false 更新为 true，避免用户看到新 server 一直"未连接"。
        setTimeout(() => { void renderMcpServers(); }, 3000);
      } catch (error) {
        notify(`保存失败：${(error as Error).message}`);
      }
    })();
  });

  document.getElementById('mcp-server-close')?.addEventListener('click', closeMcpServerModal);
  document.getElementById('mcp-server-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeMcpServerModal();
  });
  document.getElementById('mcp-server-delete')?.addEventListener('click', async () => {
    const nameInput = document.getElementById('mcp-name') as HTMLInputElement | null;
    const name = nameInput?.value.trim() ?? '';
    if (!name) return;
    const confirmed = await showConfirmDialog({
      title: `删除 MCP 服务 ${name}`,
      message: '删除后该服务及其工具将不再可用。此操作不可撤销。',
      confirmText: '删除',
      cancelText: '取消',
    });
    if (!confirmed) return;
    try {
      await backendApi.deleteMcpServer(name);
      notify(`${name} 已删除`);
      await renderMcpServers();
      closeMcpServerModal();
    } catch (error) {
      notify(`删除失败：${(error as Error).message}`);
    }
  });
  document.getElementById('mcp-server-add')?.addEventListener('click', () => void openMcpServerModal());
}

/** 绑定面板（在 settings.ts 打开 mcp pane 时调用）。 */
export function bindMcpPane(): void {
  ensureMcpIntegrationView();
  bindMcpFormOnce();
  bindCuaCardOnce();
  void renderMcpServers();
  void renderCuaDriverCard();
}

/** 离开面板/关弹窗时清理（停 CUA 轮询）。 */
export function disposeMcpPane(): void {
  stopCuaSetupPolling();
}
