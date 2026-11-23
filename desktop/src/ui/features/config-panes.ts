/**
 * 配置能力（模型 + 渠道）渲染层。
 * 原本作为独立的「配置」Tab 承载，现已并入设置弹窗的两个 Pane：
 *   - settings-pane-model  →  renderConfigModels()
 *   - settings-pane-channel → renderPlatforms()
 * 入口由 settings.ts 在切换 Pane 时调用，不在此处绑定点击事件。
 */

import {
  backendApi,
  type BackendConfig,
  type ModelOption,
  type ModelPayload,
  type PlatformConfigResponse,
  type PlatformRow,
  type VendorProfileOption,
  type WikiSemanticConfig,
} from '../backend-client';
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
let switchingDefaultModel = false;
let savingSemanticConfig = false;

const DEFAULT_SEMANTIC_CONFIG: WikiSemanticConfig = {
  enabled: false,
  provider: 'openai',
  model: 'text-embedding-3-small',
  base_url: '',
  api_key_env: '',
  has_key: false,
};

function semanticConfig(): WikiSemanticConfig {
  return {
    ...DEFAULT_SEMANTIC_CONFIG,
    ...(state.config?.wiki?.semantic ?? {}),
  };
}

function semanticFieldValue(id: string): string {
  return (document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null)?.value.trim() ?? '';
}

function syncSemanticPanelVisibility(): void {
  const enabled = Boolean((document.getElementById('cfg-wiki-semantic-enabled') as HTMLInputElement | null)?.checked);
  const body = document.getElementById('cfg-wiki-semantic-body');
  if (body) body.hidden = !enabled;
}

function syncSemanticProviderFields(provider?: string): void {
  const isOpenAI = (provider ?? (document.getElementById('cfg-wiki-semantic-provider') as HTMLSelectElement | null)?.value) !== 'local';
  const baseUrl = document.getElementById('cfg-wiki-semantic-base-url-wrap');
  const apiKey = document.getElementById('cfg-wiki-semantic-api-key-wrap');
  if (baseUrl) baseUrl.hidden = !isOpenAI;
  if (apiKey) apiKey.hidden = !isOpenAI;
  const hint = document.getElementById('cfg-wiki-semantic-provider-hint');
  if (hint) {
    hint.textContent = isOpenAI
      ? '兼容 OpenAI / Azure / DeepSeek / Ollama 等 /embeddings 接口。'
      : '使用本地 fastembed 模型；首次启用可能需要下载模型依赖。';
  }
}

function fillSemanticConfigForm(config: WikiSemanticConfig = semanticConfig()): void {
  const enabled = document.getElementById('cfg-wiki-semantic-enabled') as HTMLInputElement | null;
  const provider = document.getElementById('cfg-wiki-semantic-provider') as HTMLSelectElement | null;
  const model = document.getElementById('cfg-wiki-semantic-model') as HTMLInputElement | null;
  const baseUrl = document.getElementById('cfg-wiki-semantic-base-url') as HTMLInputElement | null;
  const apiKey = document.getElementById('cfg-wiki-semantic-api-key') as HTMLInputElement | null;
  if (enabled) enabled.checked = Boolean(config.enabled);
  if (provider) provider.value = config.provider === 'local' ? 'local' : 'openai';
  if (model) model.value = config.model || (config.provider === 'local' ? 'BAAI/bge-small-zh-v1.5' : DEFAULT_SEMANTIC_CONFIG.model);
  if (baseUrl) baseUrl.value = config.base_url || '';
  if (apiKey) {
    apiKey.value = '';
    apiKey.placeholder = config.has_key ? '已配置，留空则保留原 Key' : '输入专用 Embedding API Key';
  }
  syncSemanticProviderFields(provider?.value);
  syncSemanticPanelVisibility();
  const keyStatus = document.getElementById('cfg-wiki-semantic-key-status');
  if (keyStatus) {
    keyStatus.textContent = config.provider === 'local'
      ? '本地 Provider 不需要 API Key。'
      : config.has_key
        ? `已配置 Key${config.api_key_masked ? `（${config.api_key_masked}）` : ''}`
        : '尚未配置 Key；建议填写专用 Embedding Key。';
  }
  const summaryStatus = document.getElementById('cfg-wiki-semantic-summary-status');
  if (summaryStatus) {
    summaryStatus.textContent = config.enabled ? '已启用' : '可选 · 未启用';
    summaryStatus.dataset.enabled = String(Boolean(config.enabled));
  }
}

export function readSemanticConfigForm(): {
  enabled: boolean;
  provider: 'openai' | 'local';
  model: string;
  base_url: string;
  api_key_env: string;
  api_key: string;
} {
  const provider = semanticFieldValue('cfg-wiki-semantic-provider') === 'local' ? 'local' : 'openai';
  return {
    enabled: Boolean((document.getElementById('cfg-wiki-semantic-enabled') as HTMLInputElement | null)?.checked),
    provider,
    model: semanticFieldValue('cfg-wiki-semantic-model'),
    base_url: semanticFieldValue('cfg-wiki-semantic-base-url'),
    api_key_env: semanticConfig().api_key_env,
    api_key: semanticFieldValue('cfg-wiki-semantic-api-key'),
  };
}

function createSemanticConfigView(): HTMLElement {
  const section = document.createElement('section');
  section.className = 'semantic-config';
  section.innerHTML = `
    <div class="semantic-config__row">
      <div class="semantic-config__summary-copy">
        <span class="semantic-config__summary-eyebrow">可选能力</span>
        <strong class="semantic-config__summary-title">Wiki 语义检索</strong>
        <span class="semantic-config__summary-desc">Embedding · 需要时再开启</span>
      </div>
      <div class="semantic-config__summary-meta">
        <span class="semantic-config__summary-status" id="cfg-wiki-semantic-summary-status">可选 · 未启用</span>
        <label class="set-v2-switch" aria-label="启用语义检索">
          <input type="checkbox" id="cfg-wiki-semantic-enabled">
          <span class="set-v2-switch__track"></span>
        </label>
      </div>
    </div>
    <div class="semantic-config__body" id="cfg-wiki-semantic-body">
      <p class="semantic-config__desc">失败时会自动回退到关键词检索。配置按当前账号生效，API Key 只会保存到账号环境文件。</p>
      <form class="semantic-config__form" id="cfg-wiki-semantic-form">
        <div class="semantic-config__fields">
          <div class="semantic-config__field">
            <label class="semantic-config__label" for="cfg-wiki-semantic-provider">Embedding 提供方</label>
            <select class="semantic-config__control" id="cfg-wiki-semantic-provider">
              <option value="openai">OpenAI 兼容 API</option>
              <option value="local">本地 fastembed</option>
            </select>
            <span class="semantic-config__hint" id="cfg-wiki-semantic-provider-hint"></span>
          </div>
          <div class="semantic-config__field">
            <label class="semantic-config__label" for="cfg-wiki-semantic-model">Embedding 模型</label>
            <input class="semantic-config__control" id="cfg-wiki-semantic-model" placeholder="例如 text-embedding-3-small">
            <span class="semantic-config__hint">OpenAI 兼容服务填写服务端模型名；本地模式填写 Hugging Face 模型 ID。</span>
          </div>
          <div class="semantic-config__field semantic-config__field--openai" id="cfg-wiki-semantic-base-url-wrap">
            <label class="semantic-config__label" for="cfg-wiki-semantic-base-url">Base URL</label>
            <input class="semantic-config__control" id="cfg-wiki-semantic-base-url" placeholder="可留空，复用主模型 Base URL">
            <span class="semantic-config__hint">需要时填写兼容接口地址，例如 https://api.openai.com/v1。</span>
          </div>
          <div class="semantic-config__field semantic-config__field--openai semantic-config__field--wide" id="cfg-wiki-semantic-api-key-wrap">
            <label class="semantic-config__label" for="cfg-wiki-semantic-api-key">专用 Embedding API Key</label>
            <input class="semantic-config__control" id="cfg-wiki-semantic-api-key" type="password" placeholder="输入专用 Embedding API Key" autocomplete="new-password">
            <span class="semantic-config__hint" id="cfg-wiki-semantic-key-status"></span>
          </div>
        </div>
        <div class="semantic-config__footer">
          <span class="semantic-config__save-status" id="cfg-wiki-semantic-save-status" role="status"></span>
          <button class="mw-button mw-button--secondary mw-button--small" type="submit" id="cfg-wiki-semantic-save">保存</button>
        </div>
      </form>
    </div>
  `;
  const provider = section.querySelector<HTMLSelectElement>('#cfg-wiki-semantic-provider');
  provider?.addEventListener('change', () => {
    syncSemanticProviderFields(provider.value);
    const model = section.querySelector<HTMLInputElement>('#cfg-wiki-semantic-model');
    if (model && !model.value.trim()) model.value = provider.value === 'local' ? 'BAAI/bge-small-zh-v1.5' : DEFAULT_SEMANTIC_CONFIG.model;
  });
  section.querySelector<HTMLInputElement>('#cfg-wiki-semantic-enabled')?.addEventListener('change', () => {
    syncSemanticPanelVisibility();
    void saveSemanticConfig();
  });
  section.querySelector<HTMLFormElement>('#cfg-wiki-semantic-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    void saveSemanticConfig();
  });
  return section;
}

async function saveSemanticConfig(): Promise<void> {
  if (savingSemanticConfig) return;
  const form = readSemanticConfigForm();
  if (!form.model) {
    notify('请填写 Embedding 模型');
    return;
  }
  savingSemanticConfig = true;
  const button = document.getElementById('cfg-wiki-semantic-save') as HTMLButtonElement | null;
  const status = document.getElementById('cfg-wiki-semantic-save-status');
  if (button) {
    button.disabled = true;
    button.textContent = '保存中…';
  }
  if (status) status.textContent = '';
  try {
    const next = await backendApi.updateWikiSemantic(form);
    state.config = next;
    await loadConfig();
    fillSemanticConfigForm(semanticConfig());
    if (status) status.textContent = '已保存';
    notify('Embedding 配置已更新');
  } catch (error) {
    if (status) status.textContent = '保存失败';
    notify(`Embedding 配置保存失败：${(error as Error).message}`);
  } finally {
    savingSemanticConfig = false;
    if (button) {
      button.disabled = false;
      button.textContent = '保存';
    }
  }
}

async function setDefaultModel(modelId: string): Promise<void> {
  const config = state.config;
  if (!config || switchingDefaultModel || modelId === config.active_model_id) return;

  const model = (config.model_profiles ?? config.models ?? [])
    .find((candidate) => candidate.id === modelId);
  if (!model) {
    notify('模型配置不存在，请刷新后重试');
    return;
  }
  if (!model.loaded) {
    notify('模型未加载，不能设为默认');
    return;
  }
  if (!model.has_key) {
    notify('请先为该模型配置 API Key');
    return;
  }

  switchingDefaultModel = true;
  await renderConfigModels();
  try {
    state.config = await backendApi.switchModel(modelId);
    await loadConfig();
    notify(`默认模型已切换为 ${model.name || model.id}`);
  } catch (error) {
    notify(`切换默认模型失败：${(error as Error).message}`);
  } finally {
    switchingDefaultModel = false;
    await renderConfigModels();
  }
}

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
        if (model && (model.editable ?? !model.builtin)) openModelConfigModal(model);
      },
      onAction: (action, id) => {
        if (action === 'set-default') void setDefaultModel(id);
      },
    });
  }
  if (!pane.contains(modelIntegrationView.element)) pane.replaceChildren(modelIntegrationView.element);
  if (!modelIntegrationView.trailing.querySelector('.semantic-config')) {
    modelIntegrationView.trailing.appendChild(createSemanticConfigView());
  }
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

// ---- 厂商目录（GET /api/config/vendors）：厂商模式下自动填充 Base URL / 模型列表 ----

const CUSTOM_VENDOR_OPENAI = 'custom:openai';
const CUSTOM_VENDOR_ANTHROPIC = 'custom:anthropic';
const EXISTING_VENDOR = 'existing:provider';
const CUSTOM_MODEL_VALUE = '__custom';
const DEFAULT_MAX_TOKENS = 32768;

interface ModelFormDraft {
  model: ModelOption | undefined;
  changed: boolean;
  saving: boolean;
}

let modelFormDraft: ModelFormDraft | null = null;
let vendorCatalogCache: VendorProfileOption[] | null = null;
let vendorCatalogRequest: Promise<boolean> | null = null;

function loadVendorCatalog(): Promise<boolean> {
  if (vendorCatalogCache !== null) return Promise.resolve(true);
  vendorCatalogRequest ??= backendApi.configVendors()
    .then(({ vendors }) => {
      if (!Array.isArray(vendors)) return false;
      vendorCatalogCache = vendors;
      return true;
    })
    .catch(() => false)
    .finally(() => { vendorCatalogRequest = null; });
  return vendorCatalogRequest;
}

/** 测试注入厂商目录（null 恢复未加载状态）。 */
export function __setVendorCatalogForTest(vendors: VendorProfileOption[] | null): void {
  vendorCatalogCache = vendors;
}

function vendorSelectEl(): HTMLSelectElement | null {
  return document.getElementById('cfg-model-vendor') as HTMLSelectElement | null;
}

function modelSelectEl(): HTMLSelectElement | null {
  return document.getElementById('cfg-model-model-select') as HTMLSelectElement | null;
}

function selectedVendorProfile(): VendorProfileOption | null {
  const value = vendorSelectEl()?.value ?? '';
  return (vendorCatalogCache ?? []).find((vendor) => vendor.id === value) ?? null;
}

function populateVendorSelect(existingProvider?: string): void {
  const select = vendorSelectEl();
  if (!select) return;
  const options = (vendorCatalogCache ?? []).map(({ id, name }) => ({ value: id, label: name }));
  if (existingProvider) options.push({ value: EXISTING_VENDOR, label: existingProvider });
  options.push(
    { value: CUSTOM_VENDOR_OPENAI, label: '自定义（OpenAI 兼容）' },
    { value: CUSTOM_VENDOR_ANTHROPIC, label: '自定义（Anthropic）' },
  );
  select.replaceChildren(...options.map(({ value, label }) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    return option;
  }));
}

function showModelVendorFeedback(message: string, canRetry = false): void {
  const feedback = document.getElementById('cfg-model-vendor-feedback');
  const status = document.getElementById('cfg-model-vendor-status');
  const retry = document.getElementById('cfg-model-vendor-retry');
  if (status) status.textContent = message;
  if (retry) retry.hidden = !canRetry;
  if (feedback) feedback.hidden = !message;
}

async function refreshModelFormVendors(draft: ModelFormDraft): Promise<void> {
  showModelVendorFeedback('正在加载供应商目录…');
  const loaded = await loadVendorCatalog();
  if (modelFormDraft !== draft) return;

  showModelVendorFeedback(loaded ? '' : '供应商目录加载失败，可重试或手动配置。', !loaded);
  if (!loaded) return;

  const select = vendorSelectEl();
  if (!select) return;
  if (!draft.changed && !draft.saving) {
    fillModelForm(draft.model);
    return;
  }

  const selection = select.value;
  populateVendorSelect(selection === EXISTING_VENDOR ? draft.model?.provider : undefined);
  select.value = selection;
}

function populateVendorModelSelect(vendor: VendorProfileOption): void {
  const select = modelSelectEl();
  if (!select) return;
  select.replaceChildren();
  for (const model of vendor.models) {
    const opt = document.createElement('option');
    opt.value = model.id;
    opt.textContent = model.id;
    select.appendChild(opt);
  }
  const custom = document.createElement('option');
  custom.value = CUSTOM_MODEL_VALUE;
  custom.textContent = '自定义模型…';
  select.appendChild(custom);
}

/** 厂商模式下选中的目录模型元数据；选「自定义模型…」时返回 null。 */
function selectedVendorModel(): VendorProfileOption['models'][number] | null {
  const vendor = selectedVendorProfile();
  const select = modelSelectEl();
  if (!vendor || !select || select.value === CUSTOM_MODEL_VALUE) return null;
  return vendor.models.find((model) => model.id === select.value) ?? null;
}

/** 按模型选择填充目录参数和能力默认值。 */
function applyVendorModelSelection(): void {
  const vendor = selectedVendorProfile();
  if (!vendor) return;
  const select = modelSelectEl();
  const input = document.getElementById('cfg-model-model') as HTMLInputElement | null;
  const meta = selectedVendorModel();
  const customModel = !meta;
  if (select) select.hidden = false;
  if (input) input.hidden = !customModel;
  const contextWindowField = document.getElementById('cfg-model-context-window-wrap');
  if (contextWindowField) contextWindowField.hidden = !customModel;
  const maxTokensField = document.getElementById('cfg-model-max-tokens-wrap');
  if (maxTokensField) maxTokensField.hidden = !customModel;
  const maxTokensInput = maxTokensInputEl();
  if (maxTokensInput) maxTokensInput.disabled = !customModel;
  if (customModel) {
    fillContextWindowSelect(null);
    if (maxTokensInput && !maxTokensInput.value.trim()) maxTokensInput.value = String(DEFAULT_MAX_TOKENS);
  }
  if (meta?.context_window) fillContextWindowSelect(meta.context_window);
  const saved = modelFormDraft?.model;
  const savedCapabilities = meta && saved?.provider === vendor.id && saved.model === meta.id
    ? saved.capabilities
    : undefined;
  setModelCapabilities(savedCapabilities ?? (meta?.vision ? ['text', 'tools', 'vision'] : undefined));
  updateSuggestedModelId(meta?.id ?? '');
}

function updateSuggestedModelId(modelId: string): void {
  const input = document.querySelector<HTMLInputElement>('#cfg-model-id');
  if (!input || input.disabled) return;
  if (input.value.trim() && input.value !== input.dataset.suggestedId) return;

  input.value = modelId;
  input.dataset.suggestedId = modelId;
}

function maxTokensInputEl(): HTMLInputElement | null {
  return document.getElementById('cfg-model-max-tokens') as HTMLInputElement | null;
}

function applyVendorSelection(modelId?: string): void {
  const select = vendorSelectEl();
  if (!select) return;
  const vendor = selectedVendorProfile();
  const protocolWrap = document.getElementById('cfg-model-protocol-wrap');
  if (protocolWrap) protocolWrap.hidden = !!vendor || select.value === EXISTING_VENDOR;
  const baseUrlInput = document.querySelector<HTMLInputElement>('#cfg-model-base-url');
  if (baseUrlInput) {
    if (!vendor && baseUrlInput.readOnly) baseUrlInput.value = '';
    baseUrlInput.readOnly = !!vendor;
    if (vendor) baseUrlInput.value = vendor.base_url;
  }
  const label = document.querySelector<HTMLLabelElement>('#cfg-model-model-label');
  if (label) label.htmlFor = vendor ? 'cfg-model-model-select' : 'cfg-model-model';
  const modelSelect = modelSelectEl();
  const modelInput = document.getElementById('cfg-model-model');
  if (!vendor) {
    setModelProtocol(select.value === CUSTOM_VENDOR_ANTHROPIC ? 'anthropic' : 'openai');
    if (modelSelect) modelSelect.hidden = true;
    if (modelInput) modelInput.hidden = false;
    for (const id of ['cfg-model-context-window-wrap', 'cfg-model-max-tokens-wrap']) {
      const field = document.getElementById(id);
      if (field) field.hidden = false;
    }
    const maxTokensInput = maxTokensInputEl();
    if (maxTokensInput) maxTokensInput.disabled = false;
    setModelCapabilities();
    updateSuggestedModelId('');
    return;
  }
  populateVendorModelSelect(vendor);
  if (modelId && modelSelect) {
    modelSelect.value = vendor.models.some((model) => model.id === modelId) ? modelId : CUSTOM_MODEL_VALUE;
  }
  applyVendorModelSelection();
}

function selectedModelCapabilities(): string[] {
  return Array.from(
    document.querySelectorAll<HTMLInputElement>('#cfg-model-form input[name="cfg-model-capability"]:checked'),
    (input) => input.value,
  );
}

function setModelCapabilities(capabilities: string[] = ['text', 'tools']): void {
  const selected = new Set(capabilities);
  document.querySelectorAll<HTMLInputElement>('#cfg-model-form input[name="cfg-model-capability"]').forEach((input) => {
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

export function readModelForm(): ModelPayload {
  const value = (id: string): string => (document.getElementById(id) as HTMLInputElement | null)?.value.trim() ?? '';
  const modelId = value('cfg-model-id');
  const cwRaw = (document.getElementById('cfg-model-context-window') as HTMLSelectElement | null)?.value;
  const vendor = selectedVendorProfile();
  const meta = selectedVendorModel();
  const apiModel = meta?.id ?? (value('cfg-model-model') || modelId);
  const apiKey = value('cfg-model-api-key');
  const existing = modelFormDraft?.model;
  const existingProvider = vendorSelectEl()?.value === EXISTING_VENDOR ? existing?.provider : undefined;
  return {
    id: modelId,
    ...(existing ? {} : { name: modelId }),
    model: apiModel,
    provider: vendor?.id ?? existingProvider ?? selectedModelProtocol(),
    base_url: value('cfg-model-base-url'),
    ...(apiKey ? { api_key: apiKey } : {}),
    context_window: meta?.context_window || Number(cwRaw) || 256000,
    max_tokens: meta ? (meta.max_tokens ?? null) : (Number(value('cfg-model-max-tokens')) || DEFAULT_MAX_TOKENS),
    loaded: existing?.loaded ?? true,
    capabilities: selectedModelCapabilities(),
  };
}

function fillModelForm(model?: ModelOption): void {
  const set = (id: string, value: string): void => {
    const input = document.getElementById(id) as HTMLInputElement | null;
    if (input) input.value = value;
  };
  const providerId = (model?.provider ?? '').trim().toLowerCase();
  const matchedVendor = (vendorCatalogCache ?? []).find((vendor) => vendor.id === providerId);
  const existingProvider = !matchedVendor && providerId && providerId !== 'openai' && providerId !== 'anthropic'
    ? model?.provider
    : undefined;

  populateVendorSelect(existingProvider);
  set('cfg-model-id', model?.id ?? '');
  const idInput = document.querySelector<HTMLInputElement>('#cfg-model-id');
  if (idInput) {
    idInput.disabled = !!model;
    delete idInput.dataset.suggestedId;
  }
  set('cfg-model-model', model?.model ?? '');
  set('cfg-model-base-url', '');
  set('cfg-model-max-tokens', String(model?.max_tokens ?? DEFAULT_MAX_TOKENS));

  const vendorSelect = vendorSelectEl();
  if (vendorSelect) {
    if (matchedVendor) vendorSelect.value = matchedVendor.id;
    else if (existingProvider) vendorSelect.value = EXISTING_VENDOR;
    else if (model) vendorSelect.value = providerId === 'anthropic' ? CUSTOM_VENDOR_ANTHROPIC : CUSTOM_VENDOR_OPENAI;
    else vendorSelect.value = vendorCatalogCache?.[0]?.id ?? CUSTOM_VENDOR_OPENAI;
  }

  applyVendorSelection(model?.model);
  if (model?.base_url) set('cfg-model-base-url', model.base_url);
  if (!selectedVendorModel()) {
    fillContextWindowSelect(model?.context_window);
    setModelCapabilities(model?.capabilities);
  }
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

function showModelFormError(message: string): void {
  const error = document.getElementById('cfg-model-error');
  if (!error) return;
  error.textContent = message;
  error.hidden = !message;
}

function closeModelConfigModal(): void {
  if (modelFormDraft?.saving) return;
  modelFormDraft = null;
  const overlay = $('#model-connect-overlay') as HTMLElement | null;
  if (overlay) overlay.hidden = true;
  const apiKey = document.querySelector<HTMLInputElement>('#cfg-model-api-key');
  if (apiKey) apiKey.value = '';
}

/** 打开模型配置弹层（新增或编辑）。不可编辑的内置模型不允许打开。 */
export function openModelConfigModal(model?: ModelOption): void {
  if (modelFormDraft?.saving || (model && !(model.editable ?? !model.builtin))) return;
  const overlay = $('#model-connect-overlay') as HTMLElement | null;
  if (!overlay) return;
  bindModelFormOnce();
  const draft: ModelFormDraft = { model, changed: false, saving: false };
  modelFormDraft = draft;
  fillModelForm(model);
  const apiKey = document.querySelector<HTMLInputElement>('#cfg-model-api-key');
  if (apiKey) apiKey.value = '';
  showModelFormError('');
  const title = document.getElementById('model-connect-title');
  const desc = document.getElementById('model-connect-desc');
  const deleteBtn = document.getElementById('cfg-model-delete');
  if (title) title.textContent = model ? `编辑：${model.name || model.id}` : '新增模型';
  if (desc) {
    desc.textContent = model
      ? '编辑时 API Key 留空会保留已保存的密钥。'
      : '选择供应商与模型，填写 API Key 后保存。';
  }
  if (deleteBtn) deleteBtn.hidden = !model;
  overlay.hidden = false;

  showModelVendorFeedback('');
  if (vendorCatalogCache === null && vendorSelectEl()) void refreshModelFormVendors(draft);
}

function setModelFormSaving(form: HTMLFormElement, saving: boolean): void {
  form.inert = saving;
  for (const id of ['cfg-model-save', 'cfg-model-delete', 'model-connect-close']) {
    const button = document.querySelector<HTMLButtonElement>(`#${id}`);
    if (button) button.disabled = saving;
  }
  const save = document.getElementById('cfg-model-save');
  if (save) save.textContent = saving ? '保存中…' : '保存';
}

async function saveModelForm(form: HTMLFormElement): Promise<void> {
  const draft = modelFormDraft;
  if (!draft || draft.saving) return;

  const payload = readModelForm();
  const id = payload.id;
  if (!id) return showModelFormError('请填写模型 ID');
  if (!payload.model) return showModelFormError('请填写接口模型名');
  if (!payload.capabilities?.includes('text')) return showModelFormError('对话模型必须支持文本能力');
  if (!payload.base_url) return showModelFormError('请填写 Base URL');
  if (!payload.api_key && !draft.model?.has_key) return showModelFormError('请填写 API Key');

  showModelFormError('');
  draft.saving = true;
  setModelFormSaving(form, true);
  let saved: BackendConfig;
  try {
    saved = draft.model
      ? await backendApi.updateModel(draft.model.id, payload)
      : await backendApi.createModel(payload);
  } catch (error) {
    showModelFormError(`模型保存失败：${(error as Error).message}`);
    return;
  } finally {
    draft.saving = false;
    setModelFormSaving(form, false);
  }

  closeModelConfigModal();
  await loadConfig(saved);
  void renderConfigModels();
  notify(draft.model ? '模型配置已更新' : '模型配置已新增');
}

function bindModelFormOnce(): void {
  const form = document.getElementById('cfg-model-form') as HTMLFormElement | null;
  if (!form || form.dataset.bound === '1') return;
  form.dataset.bound = '1';
  vendorSelectEl()?.addEventListener('change', () => applyVendorSelection());
  modelSelectEl()?.addEventListener('change', applyVendorModelSelection);
  document.getElementById('cfg-model-id')?.addEventListener('input', (event) => {
    delete (event.currentTarget as HTMLInputElement).dataset.suggestedId;
  });
  document.getElementById('cfg-model-vendor-retry')?.addEventListener('click', () => {
    if (modelFormDraft) void refreshModelFormVendors(modelFormDraft);
  });
  const markChanged = (): void => {
    if (modelFormDraft) modelFormDraft.changed = true;
  };
  form.addEventListener('input', markChanged);
  form.addEventListener('change', markChanged);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    void saveModelForm(form);
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

/** 厂商目录优先，其次是未收录的具名厂商，通用协议归入最后的「自定义」组。 */
function modelGroup(model: ModelOption): { label: string; order: number } {
  const pid = (model.provider ?? '').trim().toLowerCase();
  const catalog = vendorCatalogCache ?? [];
  const index = catalog.findIndex((vendor) => vendor.id === pid);
  if (index >= 0) return { label: catalog[index].name, order: index };
  if (pid === 'openai' || pid === 'anthropic' || !pid) return { label: '自定义', order: catalog.length + 1 };
  return { label: pid, order: catalog.length };
}

export async function renderConfigModels(): Promise<void> {
  const view = ensureModelIntegrationView();
  if (!view) return;
  bindModelFormOnce();
  view.update({ state: 'loading', message: '正在加载模型配置…', items: [] });
  await Promise.all([state.config ? Promise.resolve() : loadConfig(), loadVendorCatalog()]);
  if (!state.config) {
    view.update({
      state: 'error',
      message: '无法连接服务，请稍后重试。',
      items: [],
    });
    return;
  }

  fillSemanticConfigForm(semanticConfig());

  const models = state.config.model_profiles ?? state.config.models ?? [];
  if (models.length === 0) {
    view.update({
      state: 'empty',
      message: '暂无模型，点击“添加模型”开始配置。',
      items: [],
    });
    return;
  }
  // 按厂商目录顺序分组排序；同组内保持原有顺序（稳定排序）。
  const decorated = models
    .map((model) => ({ model, group: modelGroup(model) }))
    .sort((a, b) => a.group.order - b.group.order || a.group.label.localeCompare(b.group.label));
  const items: SettingsIntegrationItem[] = decorated.map(({ model, group }) => {
    const active = model.id === state.config!.active_model_id;
    return {
      id: model.id,
      title: model.name || model.id,
      description: model.builtin
        ? model.model
        : `${model.model}${model.base_url ? ` · ${model.base_url}` : ''}`,
      status: modelStatusText(model, active),
      tone: !model.has_key ? 'danger' : active ? 'success' : model.loaded ? 'info' : 'warning',
      selectable: model.editable ?? !model.builtin,
      icon: 'process-thinking',
      active,
      group: group.label,
      actions: active ? [] : [{
        id: 'set-default',
        label: '设为默认',
        disabled: switchingDefaultModel || !model.loaded || !model.has_key,
      }],
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
