/**
 * 配置能力（模型 + 渠道）渲染层。
 * 原本作为独立的「配置」Tab 承载，现已并入设置弹窗的两个 Pane：
 *   - settings-pane-model  →  renderConfigModels()
 *   - settings-pane-channel → renderPlatforms()
 * 入口由 settings.ts 在切换 Pane 时调用，不在此处绑定点击事件。
 */

import { backendApi, type ModelOption, type ModelPayload, type PlatformConfigResponse, type PlatformRow } from '../backend-client';
import { showConfirmDialog } from '../ui-feedback';
import { $, escapeHtml, notify, state } from '../state';
import { loadConfig } from './model-picker';
import { reconcileSessionModelsAfterDelete } from './session-model';
import {
  createSettingsIntegrationView,
  type SettingsIntegrationItem,
  type SettingsIntegrationView,
} from './settings-integrations';

const CHANNEL_MAP: Record<string, string> = {
  feishu: 'feishu',
  lark: 'feishu',
  weixin: 'weixin',
};

/** 设置页渠道卡片与弹窗使用的展示名（覆盖插件注册 label）。 */
const CHANNEL_DISPLAY_LABELS: Record<string, string> = {
  feishu: '飞书',
  weixin: '微信',
};

function channelDisplayLabel(apiName: string, fallback?: string): string {
  return CHANNEL_DISPLAY_LABELS[apiName] ?? fallback ?? apiName;
}

const CHANNEL_FIELDS: Record<string, Array<{ key: string; label: string; secret?: boolean; placeholder?: string; required?: boolean }>> = {
  feishu: [
    { key: 'appId', label: 'App ID', required: true, placeholder: 'App ID' },
    { key: 'appSecret', label: 'App Secret', secret: true, required: true, placeholder: 'App Secret' },
  ],
  // token 由扫码登录持久化到账号文件，这里只需 accountId。
  weixin: [
    { key: 'accountId', label: '账号 ID', required: true, placeholder: '扫码登录后自动填充' },
  ],
};

/** 与卡片列表一致的头像资源（`index.html` conn-row）。 */
const CHANNEL_ICON_SRC: Record<string, string> = {
  feishu: './image/channels/feishu-icon.png',
  weixin: './image/channels/weixin-icon.png',
};

const CHANNEL_ORDER = ['feishu', 'weixin'] as const;

/** 已配置账号也可即时切换的渠道功能开关。 */
const CHANNEL_TOGGLES: Record<string, Array<{ key: string; label: string; hint?: string }>> = {
};

let modelIntegrationView: SettingsIntegrationView | null = null;
let channelIntegrationView: SettingsIntegrationView | null = null;

function ensureModelIntegrationView(): SettingsIntegrationView | null {
  const pane = document.getElementById('settings-pane-model');
  if (!pane) return null;
  if (!modelIntegrationView) {
    modelIntegrationView = createSettingsIntegrationView({
      kind: 'model',
      title: '模型',
      description: '管理对话模型、接口能力和当前默认配置。',
      primaryAction: { label: '添加模型' },
      onPrimaryAction: () => openModelConfigModal(),
      onSelect: (id) => {
        const model = (state.config?.model_profiles ?? state.config?.models ?? [])
          .find((candidate) => candidate.id === id);
        if (model && !model.builtin) openModelConfigModal(model);
      },
      onAction: () => undefined,
    });
  }
  if (!pane.contains(modelIntegrationView.element)) pane.replaceChildren(modelIntegrationView.element);
  return modelIntegrationView;
}

function ensureChannelIntegrationView(): SettingsIntegrationView | null {
  const pane = document.getElementById('settings-pane-channel');
  if (!pane) return null;
  if (!channelIntegrationView) {
    channelIntegrationView = createSettingsIntegrationView({
      kind: 'channel',
      title: '渠道',
      description: '接入消息渠道，让 Agent 跨平台响应。',
      onSelect: (id) => void openChannelConfigModal(id),
      onAction: (action, id) => {
        if (action === 'toggle') void toggleChannelConnection(id);
      },
    });
  }
  if (!pane.contains(channelIntegrationView.element)) pane.replaceChildren(channelIntegrationView.element);
  return channelIntegrationView;
}

/** 渠道密钥字段 → 后端 `has_secret` 使用的环境变量名。 */
const CHANNEL_SECRET_ENV: Record<string, Record<string, string>> = {
  feishu: { appSecret: 'FEISHU_APP_SECRET' },
};

function channelFieldHasSecret(
  apiName: string,
  fieldKey: string,
  hasSecret: Record<string, boolean> | undefined,
): boolean {
  const envName = CHANNEL_SECRET_ENV[apiName]?.[fieldKey];
  if (envName && hasSecret?.[envName]) return true;
  return Boolean(hasSecret?.[fieldKey]);
}

function applyChannelModalIcon(apiName: string): void {
  const icon = document.getElementById('channel-connect-icon') as HTMLImageElement | null;
  if (!icon) return;
  icon.src = CHANNEL_ICON_SRC[apiName] ?? '';
  icon.alt = channelDisplayLabel(apiName);
}

/** 渠道进程是否已启动（不等于远端已连通）。 */
function isPlatformRunning(p: PlatformRow | undefined): boolean {
  return !!p?.running;
}

/** 渠道是否已与远端建立真实连接（飞书 bot 身份校验或通用连接状态）。 */
function isPlatformLiveConnected(p: PlatformRow | undefined): boolean {
  if (!p) return false;
  if (typeof p.live_connected === 'boolean') return p.live_connected;
  const detail = p.detail as { connected?: boolean; bot_identity_known?: boolean } | undefined;
  if (p.name === 'feishu') return !!detail?.bot_identity_known;
  return !!detail?.connected;
}

function setChannelFormMode(configured: boolean): void {
  const saveBtn = document.getElementById('channel-connect-submit') as HTMLButtonElement | null;
  const deleteBtn = document.getElementById('channel-connect-delete') as HTMLButtonElement | null;
  if (saveBtn) saveBtn.hidden = configured;
  if (deleteBtn) deleteBtn.hidden = !configured;
  document.querySelectorAll<HTMLInputElement>('[data-channel-field]').forEach((input) => {
    input.readOnly = configured;
    if (configured && input.type === 'password') {
      input.value = '';
      input.placeholder = '已配置（删除账号后可重新填写）';
    }
  });
  document.querySelectorAll<HTMLSelectElement>('[data-channel-environment]').forEach((select) => {
    select.disabled = configured;
  });
}

function isGatewayAdmin(): boolean {
  if (typeof state.config?.is_gateway_admin === 'boolean') {
    return state.config.is_gateway_admin;
  }
  return (state.config?.model_profiles ?? []).some((p) => p.builtin);
}

function selectedModelProtocol(): 'openai' | 'anthropic' {
  const checked = document.querySelector<HTMLInputElement>('input[name="cfg-model-protocol"]:checked');
  return checked?.value === 'anthropic' ? 'anthropic' : 'openai';
}

function setModelProtocol(provider?: string): void {
  const value = provider === 'anthropic' ? 'anthropic' : 'openai';
  document.querySelectorAll<HTMLInputElement>('input[name="cfg-model-protocol"]').forEach((input) => {
    input.checked = input.value === value;
  });
}

const MODEL_CAPABILITIES = [
  { id: 'text', label: '文本' },
  { id: 'tools', label: '工具调用' },
  { id: 'vision', label: '视觉（网页截图）' },
] as const;

function ensureModelCapabilitiesField(): HTMLElement | null {
  const existing = document.getElementById('cfg-model-capabilities-wrap');
  if (existing) return existing;
  const contextWindow = document.getElementById('cfg-model-context-window-wrap');
  const form = document.getElementById('cfg-model-form');
  if (!form) return null;

  const field = document.createElement('div');
  field.id = 'cfg-model-capabilities-wrap';
  field.className = 'channel-connect-field';

  const label = document.createElement('span');
  label.className = 'channel-connect-label';
  label.textContent = '模型能力';
  field.appendChild(label);

  const choices = document.createElement('div');
  choices.className = 'model-protocol-choice';
  for (const capability of MODEL_CAPABILITIES) {
    const item = document.createElement('label');
    item.className = 'model-protocol-choice__item';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = 'cfg-model-capability';
    input.value = capability.id;
    const text = document.createElement('span');
    text.textContent = capability.label;
    item.append(input, text);
    choices.appendChild(item);
  }
  field.appendChild(choices);

  const hint = document.createElement('span');
  hint.className = 'channel-connect-hint';
  hint.textContent = '只有真实支持图片输入的模型才应勾选视觉；未勾选时仍可使用 DOM 浏览。';
  field.appendChild(hint);

  if (contextWindow?.parentElement === form) {
    form.insertBefore(field, contextWindow);
  } else {
    form.appendChild(field);
  }
  return field;
}

function selectedModelCapabilities(): string[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>('input[name="cfg-model-capability"]:checked'))
    .map((input) => input.value)
    .filter((value) => MODEL_CAPABILITIES.some((item) => item.id === value));
}

function setModelCapabilities(capabilities?: string[]): void {
  ensureModelCapabilitiesField();
  const selected = new Set(capabilities?.length ? capabilities : ['text', 'tools']);
  document.querySelectorAll<HTMLInputElement>('input[name="cfg-model-capability"]').forEach((input) => {
    input.checked = selected.has(input.value);
  });
}

export function platformStatusText(p: PlatformRow): string {
  if (p.error_kind === 'network') return '网络异常，请检查网络';
  if (p.error) return `错误：${p.error}`;
  if (p.reason === 'login_required') return '未连接（请先登录）';
  if (isPlatformLiveConnected(p)) return '已连接';
  if (p.running) {
    if (p.operation) return '处理中…';
    const detail = p.detail as { state?: string } | undefined;
    if (detail?.state === 'reconnecting') return '重连中…';
    return '连接中…';
  }
  if (p.has_account || p.configured) return '已配置';
  if (p.available) return '可用';
  return '未配置';
}

function modelInitials(name: string): string {
  const parts = name.trim().split(/[\s\-_\/]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

export function readModelForm(): ModelPayload {
  ensureModelCapabilitiesField();
  const value = (id: string): string => (document.getElementById(id) as HTMLInputElement | null)?.value.trim() ?? '';
  const modelId = value('cfg-model-id');
  const apiModel = value('cfg-model-model') || modelId;
  const cwRaw = (document.getElementById('cfg-model-context-window') as HTMLSelectElement | null)?.value;
  return {
    id: modelId,
    name: modelId,
    model: apiModel,
    provider: selectedModelProtocol(),
    base_url: value('cfg-model-base-url'),
    api_key: value('cfg-model-api-key'),
    context_window: Number(cwRaw) || 256000,
    loaded: true,
    capabilities: selectedModelCapabilities(),
  };
}

function fillModelForm(model?: ModelOption): void {
  ensureModelCapabilitiesField();
  const set = (id: string, value: string): void => {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (input) input.value = value;
  };
  const setReadonly = (id: string, readonly: boolean): void => {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (input) input.readOnly = readonly;
  };
  const builtinReadonly = !!(model?.builtin && isGatewayAdmin());
  set('cfg-model-id', model?.id ?? '');
  set('cfg-model-model', model?.model ?? '');
  setModelProtocol(model?.provider ?? 'openai');
  set('cfg-model-base-url', model?.builtin ? '' : (model?.base_url ?? ''));
  set('cfg-model-api-key', '');
  fillContextWindowSelect(model?.context_window);
  setModelCapabilities(model?.capabilities);
  setReadonly('cfg-model-id', !!model);
  setReadonly('cfg-model-model', builtinReadonly);
  setReadonly('cfg-model-base-url', builtinReadonly);
  document.querySelectorAll<HTMLInputElement>('input[name="cfg-model-protocol"]').forEach((input) => {
    input.disabled = builtinReadonly;
  });
  const idInput = document.getElementById('cfg-model-id') as HTMLInputElement | null;
  if (idInput) idInput.disabled = !!model;
  const baseWrap = document.getElementById('cfg-model-base-url-wrap');
  if (baseWrap) baseWrap.hidden = !!model?.builtin;
  const keyField = document.getElementById('cfg-model-api-key-wrap');
  if (keyField) keyField.hidden = !!model?.builtin;
  const protocolField = document.getElementById('cfg-model-protocol-wrap');
  if (protocolField) protocolField.hidden = !!model?.builtin;
  const contextWindowField = document.getElementById('cfg-model-context-window-wrap');
  if (contextWindowField) contextWindowField.hidden = !!model?.builtin;
  const capabilitiesField = document.getElementById('cfg-model-capabilities-wrap');
  if (capabilitiesField) capabilitiesField.hidden = !!model?.builtin;
}

/** 填充上下文窗口下拉：标准档位选中，非标准值动态加 option 承接（避免丢值）。 */
function fillContextWindowSelect(contextWindow?: number | null): void {
  const select = document.getElementById('cfg-model-context-window') as HTMLSelectElement | null;
  if (!select) return;
  const cw = typeof contextWindow === 'number' && contextWindow > 0 ? contextWindow : 256000;
  const exists = Array.from(select.options).some((o) => Number(o.value) === cw);
  if (!exists) {
    const opt = document.createElement('option');
    opt.value = String(cw);
    opt.textContent = `${Math.round(cw / 1000)}k（自定义）`;
    select.appendChild(opt);
  }
  select.value = String(cw);
}

function closeModelConfigModal(): void {
  const overlay = $('#model-connect-overlay') as HTMLElement | null;
  if (overlay) overlay.hidden = true;
}

/** 打开模型配置弹层（新增或编辑）。内置模型不允许打开。 */
export function openModelConfigModal(model?: ModelOption): void {
  if (model?.builtin) return;
  const overlay = $('#model-connect-overlay') as HTMLElement | null;
  if (!overlay) return;
  fillModelForm(model);
  const title = document.getElementById('model-connect-title');
  const desc = document.getElementById('model-connect-desc');
  const icon = document.getElementById('model-connect-icon');
  const deleteBtn = document.getElementById('cfg-model-delete') as HTMLButtonElement | null;
  const rawName = model?.name || model?.id || 'AI';
  if (title) title.textContent = model ? `编辑：${rawName}` : '新增模型';
  if (desc) {
    desc.textContent = model
      ? '修改后保存；编辑时 API Key 留空则保留原值。'
      : '填写模型 ID、接口模型名与 API Key 后保存。';
  }
  if (icon) {
    icon.textContent = modelInitials(rawName);
  }
  if (deleteBtn) deleteBtn.hidden = !model;
  overlay.hidden = false;
}

function bindModelFormOnce(): void {
  const form = document.getElementById('cfg-model-form') as HTMLFormElement | null;
  if (!form || form.dataset.bound === '1') return;
  form.dataset.bound = '1';
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    void (async (): Promise<void> => {
      const payload = readModelForm();
      const id = String(payload.id || '').trim();
      const apiModel = String(payload.model || '').trim();
      if (!id) {
        notify('请填写模型 ID');
        return;
      }
      if (!apiModel) {
        notify('请填写接口模型名');
        return;
      }
      if (!payload.capabilities?.includes('text')) {
        notify('对话模型必须支持文本能力');
        return;
      }
      const editing = (document.getElementById('cfg-model-id') as HTMLInputElement | null)?.disabled;
      const editingModel = editing ? (state.config?.model_profiles ?? state.config?.models ?? []).find((m) => m.id === id) : undefined;
      if (editingModel?.builtin && !isGatewayAdmin()) {
        notify('内置模型仅管理员可查看');
        return;
      }
      const baseUrlVisible = !(document.getElementById('cfg-model-base-url-wrap') as HTMLElement | null)?.hidden;
      if (baseUrlVisible && !payload.base_url) {
        notify('请填写 Base URL');
        return;
      }
      if (!editing && !payload.api_key) {
        notify('请填写 API Key');
        return;
      }
      if (editing && !editingModel?.has_key && !payload.api_key) {
        notify('请填写 API Key');
        return;
      }
      try {
        state.config = editing
          ? await backendApi.updateModel(id, payload)
          : await backendApi.createModel(payload);
        await loadConfig();
        await renderConfigModels();
        closeModelConfigModal();
        fillModelForm();
        notify(editing ? '模型配置已更新' : '模型配置已新增');
      } catch (error) {
        notify(`模型保存失败：${(error as Error).message}`);
      }
    })();
  });
}

export function bindModelConfigModal(): void {
  bindModelFormOnce();
  document.getElementById('model-connect-close')?.addEventListener('click', closeModelConfigModal);
  document.getElementById('model-connect-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeModelConfigModal();
  });
  document.getElementById('cfg-model-add')?.addEventListener('click', () => openModelConfigModal());
  document.getElementById('cfg-model-delete')?.addEventListener('click', async () => {
    const id = (document.getElementById('cfg-model-id') as HTMLInputElement | null)?.value.trim() || '';
    if (!id) return;
    const confirmed = await showConfirmDialog({ title: '删除模型', message: `删除模型配置 ${id}？` });
    if (!confirmed) return;
    const doDelete = (force: boolean) => {
      void backendApi.deleteModel(id, { force })
        .then(async (next) => {
          state.config = { ...state.config!, models: next.models, active_model_id: next.active_model_id };
          await loadConfig();
          reconcileSessionModelsAfterDelete(id, next.active_model_id, next.rebound_sessions ?? []);
          await renderConfigModels();
          closeModelConfigModal();
          notify('模型配置已删除');
        })
        .catch(async (error) => {
          const msg = (error as Error).message;
          if (msg.includes('正在使用') && !force) {
            const forceConfirmed = await showConfirmDialog({ title: '模型正在使用', message: '有会话正在使用该模型，是否停止并删除？', confirmText: '停止并删除' });
            if (forceConfirmed) { doDelete(true); return; }
          }
          notify(`删除失败：${msg}`);
        });
    };
    doDelete(false);
  });
}

/** 模型卡片状态文案。active/内置 标识不能吞掉「缺少 Key」警示——无 Key 才是用户最需要看到的。 */
export function modelStatusText(m: ModelOption, isDefault: boolean): string {
  if (isDefault) return m.has_key ? '默认模型' : '默认模型 · 缺少 Key';
  if (m.builtin) return m.has_key ? '内置' : '内置 · 缺少 Key';
  if (!m.has_key) return '缺少 Key';
  if (!m.loaded) return '未加载';
  return '已配置';
}

export async function renderConfigModels(): Promise<void> {
  const view = ensureModelIntegrationView();
  if (!view) return;
  bindModelFormOnce();
  view.update({ state: 'loading', message: '正在加载模型配置…', items: [] });
  if (!state.config) await loadConfig();
  if (!state.config) {
    view.update({
      state: 'error',
      message: '无法连接服务，请稍后重试。',
      items: [],
    });
    return;
  }

  const models = state.config.model_profiles ?? state.config.models ?? [];
  if (models.length === 0) {
    view.update({
      state: 'empty',
      message: '暂无模型，点击“添加模型”开始配置。',
      items: [],
    });
    return;
  }
  const items: SettingsIntegrationItem[] = models.map((model) => {
    const active = model.id === state.config!.active_model_id;
    return {
      id: model.id,
      title: model.name || model.id,
      description: model.builtin
        ? model.model
        : `${model.model}${model.base_url ? ` · ${model.base_url}` : ''}`,
      status: modelStatusText(model, active),
      tone: !model.has_key ? 'danger' : active ? 'success' : model.loaded ? 'info' : 'warning',
      selectable: !model.builtin,
      icon: 'process-thinking',
      active,
    };
  });
  view.update({ state: 'ready', message: '', items });
}

/**
 * backend-client 暂未声明 weixin 扫码登录方法（/api/platforms/:name/qr-login/*），
 * 这里本地放宽以承接运行时接口；后端已实现该路由。
 */
type BackendApiWithQrLogin = typeof backendApi & {
  qrLoginStart: (name: string) => Promise<{
    ok: boolean;
    qr_id: string;
    qr_image: string;
    qrcode_url: string;
    error?: string;
  }>;
  qrLoginStatus: (name: string, qrId: string) => Promise<{
    ok: boolean;
    status: string;
    account_id?: string;
    token?: string;
    error?: string;
  }>;
};

/** 微信扫码登录区域 HTML（仅在 weixin 渠道弹窗内渲染）。 */
function weixinQrAreaHtml(apiName: string): string {
  if (apiName !== 'weixin') return '';
  return `
    <div class="channel-connect-qr" id="weixin-qr-area">
      <button type="button" class="set-v2-btn set-v2-btn--primary" id="weixin-qr-start">扫码登录</button>
      <div class="channel-connect-hint" id="weixin-qr-status"></div>
      <div class="channel-connect-qr__image-wrap" id="weixin-qr-image-wrap" hidden>
        <img class="channel-connect-qr__image" id="weixin-qr-image" alt="微信登录二维码" />
      </div>
    </div>
  `;
}

/** 绑定微信扫码登录：拉取二维码 -> 轮询状态 -> 确认后保存 accountId 并自动连接。 */
function bindWeixinQrLogin(apiName: string): void {
  if (apiName !== 'weixin') return;
  const startBtn = document.getElementById('weixin-qr-start') as HTMLButtonElement | null;
  const statusEl = document.getElementById('weixin-qr-status');
  const imageWrap = document.getElementById('weixin-qr-image-wrap');
  const image = document.getElementById('weixin-qr-image') as HTMLImageElement | null;
  if (!startBtn || !statusEl || !imageWrap || !image || startBtn.dataset.bound === '1') return;
  startBtn.dataset.bound = '1';

  startBtn.addEventListener('click', () => {
    void (async (): Promise<void> => {
      const api = backendApi as BackendApiWithQrLogin;
      startBtn.disabled = true;
      startBtn.textContent = '等待扫码…';
      statusEl.textContent = '正在获取二维码…';
      imageWrap.hidden = true;
      try {
        const start = await api.qrLoginStart('weixin');
        if (!start.ok || !start.qr_id) {
          statusEl.textContent = start.error || '获取二维码失败，请重试';
          return;
        }
        image.src = start.qr_image || '';
        imageWrap.hidden = !start.qr_image;
        statusEl.textContent = '请用微信扫一扫上面的二维码';

        const deadline = Date.now() + 480_000;
        while (Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          const st = await api.qrLoginStatus('weixin', start.qr_id);
          if (st.status === 'confirmed' && st.account_id) {
            statusEl.textContent = '登录成功，正在连接…';
            const platforms = await backendApi.platforms();
            const current = platforms.find((x) => x.name === 'weixin');
            await backendApi.savePlatformConfig('weixin', {
              enabled: current?.enabled ?? false,
              config: { accountId: st.account_id },
            });
            const result = await backendApi.connectPlatform('weixin');
            if (!result.ok) {
              await backendApi.deletePlatformAccount('weixin').catch(() => undefined);
              statusEl.textContent = `连接失败：${result.error || ''}`;
              return;
            }
            notify('微信已连接');
            await renderPlatforms();
            await openChannelConfigModal('weixin');
            return;
          }
          if (st.status === 'expired') {
            statusEl.textContent = '二维码已过期，请重新点击「扫码登录」';
            return;
          }
          if (st.status === 'error') {
            statusEl.textContent = st.error || '登录失败，请重试';
            return;
          }
          statusEl.textContent = st.status === 'scaned'
            ? '已扫码，请在手机上确认…'
            : '请用微信扫一扫上面的二维码';
        }
        statusEl.textContent = '扫码超时，请重新点击「扫码登录」';
      } catch (error) {
        statusEl.textContent = `扫码登录失败：${(error as Error).message}`;
      } finally {
        startBtn.disabled = false;
        startBtn.textContent = '扫码登录';
      }
    })();
  });
}

export async function openChannelConfigModal(channel: string): Promise<void> {
  const apiName = CHANNEL_MAP[channel] ?? channel;
  console.debug('[openChannelConfigModal] start', { channel, apiName });
  let platforms: PlatformRow[] = [];
  let config: PlatformConfigResponse;
  try {
    [platforms, config] = await Promise.all([
      backendApi.platforms(),
      backendApi.platformConfig(apiName),
    ]);
  } catch (error) {
    console.error('[openChannelConfigModal] API failed', { apiName }, error);
    notify(`无法加载渠道配置：${(error as Error).message}`);
    return;
  }
  const p = platforms.find((x) => x.name === apiName);
  const configured = !!(p?.has_account ?? config.has_account);
  const connected = isPlatformLiveConnected(p);
  const overlay = $('#channel-connect-overlay') as HTMLElement | null;
  const title = $('#channel-connect-title');
  const desc = $('#channel-connect-desc');
  const form = document.getElementById('channel-connect-form') as HTMLFormElement | null;
  const button = document.getElementById('channel-connect-submit') as HTMLButtonElement | null;
  console.debug('[openChannelConfigModal] elements', { overlay: !!overlay, form: !!form, configured, connected });
  if (!overlay || !form) return;
  overlay.dataset.channel = apiName;
  applyChannelModalIcon(apiName);
  if (title) title.textContent = channelDisplayLabel(apiName, p?.label || apiName);
  if (desc) {
    desc.textContent = configured
      ? (connected ? '凭据已保存且渠道已连接；删除账号后可重新填写。' : '凭据已保存；可在列表右侧点击「连接」。')
      : '填写凭据后保存；保存成功将自动尝试连接。';
  }
  const fields = CHANNEL_FIELDS[apiName] ?? [];
  const presets = config.presets ?? [];
  const selectedEnv = config.environment ?? '';
  const envField = presets.length
    ? `
      <label class="channel-connect-field">
        <span class="channel-connect-label">环境<span class="channel-connect-required">*</span></span>
        <select class="channel-connect-input" data-channel-environment ${configured ? 'disabled' : ''}>
          <option value="">请选择</option>
          ${presets.map((preset) => `
            <option value="${escapeHtml(preset.id)}" ${preset.id === selectedEnv ? 'selected' : ''}>
              ${escapeHtml(preset.label)}
            </option>
          `).join('')}
        </select>
      </label>
    `
    : '';
  form.innerHTML = weixinQrAreaHtml(apiName) + envField + fields.map((field) => {
    const value = config.config[field.key];
    const hasSecret = field.secret && channelFieldHasSecret(apiName, field.key, config.has_secret);
    const requiredMark = field.required ? '<span class="channel-connect-required">*</span>' : '';
    const placeholder = configured && field.secret
      ? '已配置（删除账号后可重新填写）'
      : (hasSecret && field.secret ? '已配置；留空则保留原密钥' : field.placeholder || field.label);
    return `
      <label class="channel-connect-field">
        <span class="channel-connect-label">${escapeHtml(field.label)}${requiredMark}</span>
        <input class="channel-connect-input" data-channel-field="${field.key}" ${field.secret ? 'type="password"' : 'type="text"'}
          placeholder="${escapeHtml(placeholder)}"
          value="${field.secret ? '' : escapeHtml(String(value ?? ''))}"
          ${configured ? 'readonly' : ''} />
      </label>
    `;
  }).join('') + (CHANNEL_TOGGLES[apiName] ?? []).map((toggle) => `
      <div class="channel-connect-toggle">
        <div class="channel-connect-toggle__copy">
          <span class="channel-connect-toggle__label">${escapeHtml(toggle.label)}</span>
          ${toggle.hint ? `<span class="channel-connect-toggle__hint">${escapeHtml(toggle.hint)}</span>` : ''}
        </div>
        <label class="channel-switch">
          <input type="checkbox" data-channel-toggle-field="${toggle.key}"
            ${config.config[toggle.key] === true ? 'checked' : ''} />
          <span class="channel-switch__track"><span class="channel-switch__thumb"></span></span>
        </label>
      </div>
    `).join('');
  form.querySelectorAll<HTMLInputElement>('[data-channel-toggle-field]').forEach((input) => {
    input.addEventListener('change', () => {
      void saveChannelToggle(apiName, input.dataset.channelToggleField || '', input.checked);
    });
  });
  if (button) button.disabled = false;
  setChannelFormMode(configured);
  bindWeixinQrLogin(apiName);
  overlay.hidden = false;
}

function readChannelForm(): { config: Record<string, unknown>; secrets: Record<string, string>; environment: string } {
  const config: Record<string, unknown> = {};
  const secrets: Record<string, string> = {};
  document.querySelectorAll<HTMLInputElement>('[data-channel-field]').forEach((input) => {
    const key = input.dataset.channelField || '';
    const value = input.value.trim();
    if (!key || !value) return;
    if (input.type === 'password') secrets[key] = value;
    else config[key] = value;
  });
  document.querySelectorAll<HTMLInputElement>('[data-channel-toggle-field]').forEach((input) => {
    const key = input.dataset.channelToggleField || '';
    if (key) config[key] = input.checked;
  });
  const envSelect = document.querySelector<HTMLSelectElement>('[data-channel-environment]');
  const environment = envSelect?.value.trim() ?? '';
  return { config, secrets, environment };
}

/** 保存单个渠道开关，并保留当前启用状态与环境。 */
async function saveChannelToggle(apiName: string, key: string, value: boolean): Promise<void> {
  const label = (CHANNEL_TOGGLES[apiName] ?? []).find((toggle) => toggle.key === key)?.label || key;
  const input = document.querySelector<HTMLInputElement>(`[data-channel-toggle-field="${key}"]`);
  if (input) input.disabled = true;
  try {
    const [platforms, config] = await Promise.all([
      backendApi.platforms(),
      backendApi.platformConfig(apiName),
    ]);
    const current = platforms.find((platform) => platform.name === apiName);
    await backendApi.savePlatformConfig(apiName, {
      enabled: current?.enabled ?? false,
      ...(config.environment ? { environment: config.environment } : {}),
      config: { [key]: value },
    });
    notify(value ? `已开启${label}` : `已关闭${label}`);
  } catch (error) {
    notify(`${label}保存失败：${(error as Error).message}`);
    if (input) input.checked = !value;
  } finally {
    if (input) input.disabled = false;
  }
}

function validateChannelForm(
  apiName: string,
  config: Record<string, unknown>,
  secrets: Record<string, string>,
  hasSecret: Record<string, boolean> | undefined,
  environment: string,
  presets?: Array<{ id: string; label: string }>,
): string | null {
  if (presets?.length && !environment) {
    return '请选择环境';
  }
  const fields = CHANNEL_FIELDS[apiName] ?? [];
  for (const field of fields) {
    if (!field.required) continue;
    if (field.secret) {
      if (!secrets[field.key] && !channelFieldHasSecret(apiName, field.key, hasSecret)) {
        return `请填写 ${field.label}`;
      }
    } else if (!config[field.key]) {
      return `请填写 ${field.label}`;
    }
  }
  return null;
}

/** 列表右侧按钮：连接 / 断开渠道。未配置凭据时先打开配置弹层。 */
export async function toggleChannelConnection(channel: string): Promise<void> {
  const apiName = CHANNEL_MAP[channel] ?? channel;
  console.debug('[toggleChannelConnection] start', { channel, apiName });
  let platforms: PlatformRow[] = [];
  try {
    platforms = await backendApi.platforms();
  } catch (error) {
    console.error('[toggleChannelConnection] platforms() failed', error);
    notify(`无法读取渠道状态：${(error as Error).message}`);
    return;
  }
  const p = platforms.find((x) => x.name === apiName);
  console.debug('[toggleChannelConnection] platform lookup', { apiName, found: !!p, hasAccount: p?.has_account, running: p?.running });
  if (!p) {
    notify('渠道未注册');
    return;
  }
  if (isPlatformRunning(p)) {
    try {
      const result = await backendApi.disconnectPlatform(apiName);
      notify(result.ok ? '渠道已断开' : `渠道断开失败：${result.error || ''}`);
    } catch (error) {
      notify(`渠道断开失败：${(error as Error).message}`);
    }
  } else {
    if (!p.has_account) {
      await openChannelConfigModal(channel);
      return;
    }
    try {
      const result = await backendApi.connectPlatform(apiName);
      notify(result.ok ? '渠道已连接' : `渠道连接失败：${result.error || ''}`);
    } catch (error) {
      notify(`渠道连接失败：${(error as Error).message}`);
    }
  }
  await renderPlatforms();
}

function setChannelActionLoading(loading: boolean): void {
  const btn = document.getElementById('channel-connect-submit') as HTMLButtonElement | null;
  if (btn) {
    if (loading) btn.disabled = true;
    btn.classList.toggle('is-loading', loading);
  }
}

function bindChannelButton(id: string, handler: (channel: string) => Promise<boolean>): void {
  const button = document.getElementById(id) as HTMLButtonElement | null;
  if (!button || button.dataset.bound === '1') return;
  button.dataset.bound = '1';
  button.addEventListener('click', () => {
    void (async (): Promise<void> => {
      const overlay = $('#channel-connect-overlay') as HTMLElement | null;
      const channel = overlay?.dataset.channel || '';
      if (!channel) return;
      setChannelActionLoading(true);
      try {
        await handler(channel);
      } catch (error) {
        notify(`渠道操作失败：${(error as Error).message}`);
      } finally {
        setChannelActionLoading(false);
        await renderPlatforms();
        if (!overlay?.hidden) {
          await openChannelConfigModal(channel);
        }
      }
    })();
  });
}

export function bindChannelConfigModal(): void {
  bindChannelButton('channel-connect-submit', async (channel) => {
    const form = readChannelForm();
    const configResp = await backendApi.platformConfig(channel);
    const err = validateChannelForm(
      channel,
      form.config,
      form.secrets,
      configResp.has_secret,
      form.environment,
      configResp.presets,
    );
    if (err) {
      notify(err);
      return false;
    }
    const platforms = await backendApi.platforms();
    const current = platforms.find((x) => x.name === channel);
    try {
      await backendApi.savePlatformConfig(channel, {
        enabled: current?.enabled ?? false,
        ...(form.environment ? { environment: form.environment } : {}),
        config: form.config,
        secrets: form.secrets,
      });
      const result = await backendApi.connectPlatform(channel);
      if (!result.ok) {
        await backendApi.deletePlatformAccount(channel).catch(() => undefined);
        throw new Error(result.error || '连接失败');
      }
      notify('渠道配置已保存并已连接');
      return true;
    } catch (error) {
      await backendApi.deletePlatformAccount(channel).catch(() => undefined);
      throw error;
    }
  });

  bindChannelButton('channel-connect-delete', async (channel) => {
    const label = channelDisplayLabel(channel);
    const confirmed = await showConfirmDialog({
      title: `删除 ${label} 凭据`,
      message: '删除后需重新填写环境与账号信息才能连接。此操作不可撤销。',
      confirmText: '删除',
      cancelText: '取消',
    });
    if (!confirmed) return false;
    const platforms = await backendApi.platforms();
    const current = platforms.find((x) => x.name === channel);
    if (isPlatformRunning(current)) {
      const disconnect = await backendApi.disconnectPlatform(channel);
      if (!disconnect.ok) {
        notify(`渠道断开失败：${disconnect.error || ''}`);
        return false;
      }
    }
    const result = await backendApi.deletePlatformAccount(channel);
    notify(result.ok ? '渠道凭据已删除' : `删除失败：${result.error || ''}`);
    return !!result.ok;
  });
}

export async function renderPlatforms(): Promise<void> {
  const view = ensureChannelIntegrationView();
  if (!view) return;
  view.update({ state: 'loading', message: '正在加载渠道状态…', items: [] });
  let platforms: PlatformRow[] = [];
  try {
    platforms = await backendApi.platforms();
  } catch (error) {
    view.update({
      state: 'error',
      message: `无法加载渠道：${(error as Error).message}`,
      items: [],
    });
    return;
  }

  const byName = Object.fromEntries(platforms.map((p) => [p.name.toLowerCase(), p]));
  const items: SettingsIntegrationItem[] = CHANNEL_ORDER.map((apiName) => {
    const platform = byName[apiName];
    const live = isPlatformLiveConnected(platform);
    const starting = isPlatformRunning(platform) && !live;
    const hasError = !!platform?.error || platform?.error_kind === 'network';
    return {
      id: apiName,
      title: channelDisplayLabel(apiName, platform?.label),
      description: '',
      status: platform ? platformStatusText(platform) : '不可用',
      tone: hasError ? 'danger' : live ? 'success' : starting ? 'running' :
        platform?.configured || platform?.has_account ? 'info' : 'neutral',
      selectable: Boolean(platform),
      image: { src: CHANNEL_ICON_SRC[apiName], alt: '' },
      actions: platform ? [{
        id: 'toggle',
        label: live ? '断开' : starting ? '连接中' : '连接',
        tone: live ? 'danger' : 'secondary',
        disabled: Boolean(platform.operation) || starting,
      }] : [],
    };
  });
  view.update({ state: 'ready', message: '', items });
}
