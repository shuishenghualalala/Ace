/**
 * 会话级模型：Composer 切换走 PUT /api/session/{id}/model，不再调用全局 switchModel。
 */

import { backendApi, type ModelOption, type RuntimeModelProfile } from '../backend-client';
import { isBusySession, notify, setActiveExternalTeamForSession, state } from '../state';
import { composerWorkspaceId, ensureComposerDraftSession, getDraftSessionModelId, getSessionAgentDisplay, isDraftSession, setDraftSessionModelId } from './workspaces';

export interface SessionModelBinding {
  source?: 'crew' | 'external' | 'team';
  model_profile_id: string;
  pending_model_profile_id?: string | null;
  model_label?: string;
  pending_label?: string | null;
  has_pending?: boolean;
  pending?: boolean;
  models?: RuntimeModelProfile[];
  model_switchable?: boolean;
  runtime_id?: string;
  external_agent_id?: string;
  external_team_id?: string;
}

export interface ComposerModelOption {
  id: string;
  label: string;
  description: string;
  selectable: boolean;
  default?: boolean;
  warning?: boolean;
}

const bindingsBySession = new Map<string, SessionModelBinding>();

/** 从全局 config 解析模型展示名。 */
export function modelLabelForId(modelId: string | null | undefined): string {
  if (!modelId) return '';
  const m = state.config?.models?.find((x) => x.id === modelId)
    ?? state.config?.model_profiles?.find((x) => x.id === modelId);
  return m?.name || m?.model || modelId;
}

/** 把消息持久化的实际模型 id 映射为当前可用的展示名。 */
export function sessionMessageModelLabel(sessionId: string | null | undefined, modelId: string): string {
  const value = String(modelId || '').trim();
  if (!value) return '';
  const binding = sessionId ? bindingsBySession.get(sessionId) : undefined;
  if (binding?.source === 'external') {
    const model = binding.models?.find((item) => item.id === value);
    if (model) return model.label || model.id;
  }
  const model = [...(state.config?.models || []), ...(state.config?.model_profiles || [])]
    .find((item) => item.id === value || item.model === value);
  return model?.name || model?.model || value;
}

/** 当前 Composer 应高亮的模型 id（含 pending 预览）。 */
export function activeComposerModelId(): string {
  const sid = state.activeSessionId;
  if (!sid) return state.config?.active_model_id || '';
  const binding = bindingsBySession.get(sid);
  if (binding?.pending_model_profile_id) return binding.pending_model_profile_id;
  if (binding?.model_profile_id) return binding.model_profile_id;
  if (isDraftSession(sid)) return getDraftSessionModelId() || state.config?.active_model_id || '';
  return state.config?.active_model_id || '';
}

/**
 * 新建普通 Crew 对话时的初始模型。
 *
 * 普通会话可以继承当前选择；外部 Agent/Team 的 Runtime 模型只属于原会话，
 * 不能泄漏到“新建对话”的 Crew Composer。
 */
export function modelIdForNewCrewSession(): string {
  const fallback = state.config?.active_model_id || '';
  const sessionId = state.activeSessionId;
  if (!sessionId) return fallback;

  const binding = bindingsBySession.get(sessionId);
  const display = getSessionAgentDisplay(sessionId);
  const provider = String(display?.agentLabel?.provider || '').trim().toLowerCase();
  const isExternalIdentity = display?.agentBinding?.kind === 'external_agent'
    || display?.agentBinding?.kind === 'external_team'
    || Boolean(provider && !['crew', 'builtin', 'client'].includes(provider));

  if (binding?.source === 'external' || isExternalIdentity) return fallback;
  return activeComposerModelId() || fallback;
}

/** 当前会话绑定模型上下文窗口的默认回退值（缺失/无效时使用）。 */
export const DEFAULT_CONTEXT_WINDOW = 200000;

/** 当前会话绑定模型 id（= activeComposerModelId，UI 层语义别名，含 pending 优先）。 */
export function resolveSessionModelId(): string {
  return activeComposerModelId();
}

/** 按 id 在 config 中查模型配置（models 优先，回退 model_profiles）。 */
export function findModelOption(modelId: string): ModelOption | undefined {
  const models = state.config?.models ?? [];
  const profiles = state.config?.model_profiles ?? [];
  return models.find((m) => m.id === modelId) ?? profiles.find((m) => m.id === modelId);
}

/**
 * 当前会话绑定模型的上下文窗口（缺失/≤0 时回退默认值）。
 * 供 Inspector「上下文」页与 Composer 上下文圆环共用，确保两处口径一致。
 */
export function resolveSessionModelWindow(): number {
  const w = findModelOption(activeComposerModelId())?.context_window;
  return typeof w === 'number' && w > 0 ? w : DEFAULT_CONTEXT_WINDOW;
}

/** Composer 内联标签文案。 */
function externalSessionModelLabel(sessionId: string | null | undefined): string {
  if (!sessionId) return '';
  const binding = bindingsBySession.get(sessionId);
  if (binding?.source === 'external' && binding.model_label) return binding.model_label;
  const display = getSessionAgentDisplay(sessionId);
  const provider = String(display?.agentLabel?.provider || '').toLowerCase();
  if (
    (display?.agentLabel?.model || display?.modelLabel)
    && provider
    && provider !== 'crew'
    && provider !== 'builtin'
    && provider !== 'client'
    && provider !== 'team'
  ) {
    return display?.agentLabel?.model || display?.modelLabel || '';
  }
  return '';
}

export function sessionDisplayModelLabel(sessionId: string | null | undefined = state.activeSessionId): string {
  const externalLabel = externalSessionModelLabel(sessionId);
  if (externalLabel) return externalLabel;
  const binding = sessionId ? bindingsBySession.get(sessionId) : undefined;
  if (binding?.has_pending && binding.pending_label) {
    return `${binding.model_label || modelLabelForId(binding.model_profile_id)} → ${binding.pending_label}`;
  }
  if (binding?.model_label) return binding.model_label;
  if (sessionId && isDraftSession(sessionId)) {
    const draftId = getDraftSessionModelId();
    if (draftId) return modelLabelForId(draftId);
  }
  return modelLabelForId(state.config?.active_model_id);
}

/** Composer 内联标签文案。 */
export function resolveComposerModelLabel(): string {
  return sessionDisplayModelLabel(state.activeSessionId);
}

export function applySessionModelBinding(sessionId: string, binding: SessionModelBinding): void {
  // Team identity is persisted with the session model binding. Restore it at
  // the same boundary that restores the model so historical sessions can
  // populate the mention palette before the composer is opened.
  setActiveExternalTeamForSession(
    sessionId,
    binding.source === 'team' ? String(binding.external_team_id || '').trim() : '',
  );
  bindingsBySession.set(sessionId, binding);
  if (sessionId === state.activeSessionId) {
    syncSessionModelUi();
    // 会话级模型变化 → 通知 Inspector「上下文」页与 Composer 上下文圆环刷新。
    // 沿用现有 window 事件模式（session:changed / inspector:button-toggled），保持本模块与 inspector 解耦。
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('session:model-changed', { detail: { sessionId } }));
    }
  }
}

export function mergeSessionModelsFromBackend(
  rows: Array<{ session_id: string; model_profile_id?: string; pending_model_profile_id?: string | null; model_label?: string }>,
): void {
  for (const row of rows) {
    if (!row.session_id || !row.model_profile_id) continue;
    // /api/sessions 中的 model_profile_id 是 Crew 会话模型摘要，不能覆盖
    // 已由 /api/session/{id}/model 加载的外部 Agent Runtime 模型目录。
    if (bindingsBySession.get(row.session_id)?.source === 'external') continue;
    bindingsBySession.set(row.session_id, {
      model_profile_id: row.model_profile_id,
      ...(row.pending_model_profile_id !== undefined ? { pending_model_profile_id: row.pending_model_profile_id } : {}),
      model_label: row.model_label || modelLabelForId(row.model_profile_id),
      pending_label: row.pending_model_profile_id
        ? modelLabelForId(row.pending_model_profile_id)
        : null,
      has_pending: Boolean(row.pending_model_profile_id),
    });
  }
}

export async function loadSessionModel(sessionId: string): Promise<SessionModelBinding | null> {
  try {
    const binding = await backendApi.getSessionModel(sessionId);
    if (binding.source === 'external' && isDraftSession(sessionId)) {
      setDraftSessionModelId(binding.model_profile_id);
    }
    applySessionModelBinding(sessionId, binding);
    return binding;
  } catch {
    return null;
  }
}

export async function setSessionModel(modelId: string): Promise<void> {
  let sid = state.activeSessionId;
  if (!sid) {
    sid = ensureComposerDraftSession();
    if (!sid) return;
  }
  const busy = isBusySession(sid);
  try {
    const current = bindingsBySession.get(sid);
    if (current?.source === 'external') {
      if (busy) {
        notify('当前任务运行中，请在任务结束后切换模型');
        return;
      }
      if (!current.model_switchable) {
        notify('当前运行时暂不支持模型切换');
        return;
      }
      const binding = await backendApi.setSessionModel(sid, modelId, {
        workspace_id: composerWorkspaceId(),
      });
      if (isDraftSession(sid)) setDraftSessionModelId(binding.model_profile_id);
      applySessionModelBinding(sid, binding);
      notify(`已切换模型：${binding.model_label || modelId}`);
      return;
    }
    if (isDraftSession(sid)) {
      setDraftSessionModelId(modelId);
      const label = modelLabelForId(modelId);
      applySessionModelBinding(sid, {
        model_profile_id: modelId,
        model_label: label,
        has_pending: false,
        pending: false,
      });
      notify(busy ? `模型将在下条消息生效：${label}` : `已选择模型：${label}`);
      return;
    }
    const binding = await backendApi.setSessionModel(sid, modelId, {
      workspace_id: composerWorkspaceId(),
    });
    applySessionModelBinding(sid, binding);
    const label = binding.pending_label || binding.model_label || modelLabelForId(modelId);
    notify(binding.pending || busy ? `模型将在下条消息生效：${label}` : `已切换模型：${label}`);
  } catch {
    notify('切换模型失败');
  }
}

export function syncSessionModelUi(): void {
  const text = resolveComposerModelLabel();
  state.configModel = text;
  document.querySelectorAll('.chat-model-trigger-text').forEach((el) => {
    el.textContent = text;
  });
  const inline = document.getElementById('chat-model-picker-inline-label');
  if (inline) inline.textContent = text;
  syncSessionModelAvailabilityUi();
}

/** 外部 Team Session：优先读会话展示身份，draft/首发过渡期回退外源 Team 绑定。 */
export function isExternalTeamSession(sessionId: string | null | undefined = state.activeSessionId): boolean {
  if (!sessionId) return false;
  const provider = String(getSessionAgentDisplay(sessionId)?.agentLabel?.provider || '').trim().toLowerCase();
  return provider === 'team' || Boolean(state.activeExternalTeamIdBySession[sessionId]);
}

/** 仅刷新模型按钮可用态；busy 高频变化时不触碰标签或外部身份展示。 */
export function syncSessionModelAvailabilityUi(): void {
  const sessionId = state.activeSessionId;
  const binding = sessionId ? bindingsBySession.get(sessionId) : undefined;
  const isTeamSession = isExternalTeamSession(sessionId);
  const externalDisabled = binding?.source === 'external'
    && (!binding.model_switchable || isBusySession(sessionId || ''));
  const inlinePicker = document.getElementById('chat-model-picker-inline');
  if (inlinePicker) inlinePicker.hidden = isTeamSession;
  document.querySelectorAll<HTMLButtonElement>('.chat-model-trigger, #chat-model-picker-inline-btn').forEach((trigger) => {
    trigger.disabled = !state.backendConnected || isTeamSession || externalDisabled;
    trigger.title = isTeamSession
      ? '团队成员模型由团队配置决定'
      : externalDisabled
        ? (isBusySession(sessionId || '') ? '任务运行中，结束后可切换模型' : '当前运行时不支持模型切换')
      : '选择模型';
  });
  if ((isTeamSession || externalDisabled) && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('session:model-picker-disabled'));
  }
}

export function composerModelOptions(): ComposerModelOption[] {
  const binding = state.activeSessionId ? bindingsBySession.get(state.activeSessionId) : undefined;
  if (binding?.source === 'external') {
    const selectable = Boolean(binding.model_switchable) && !isBusySession(state.activeSessionId || '');
    return (binding.models || []).map((model) => ({
      id: model.id,
      label: model.label || model.id,
      description: model.label === model.id ? (model.provider || model.id) : model.id,
      selectable,
      warning: !selectable,
      ...(model.default ? { default: true } : {}),
    }));
  }
  return (state.config?.models || []).map((model) => ({
    id: model.id,
    label: model.name || model.model || model.id,
    description: model.has_key ? (model.model || model.id) : '未配置 API Key',
    // 保持 Crew 原行为：无 Key 模型仍可点击，由既有后端校验负责反馈。
    selectable: true,
    warning: !model.has_key,
  }));
}

/** 新建草稿继承当前 Composer 所选模型。 */
export function seedDraftSessionModel(): void {
  const sid = state.activeSessionId;
  if (!sid || !isDraftSession(sid)) return;
  const inherited = activeComposerModelId() || state.config?.active_model_id || '';
  if (!inherited) return;
  setDraftSessionModelId(inherited);
  applySessionModelBinding(sid, {
    model_profile_id: inherited,
    model_label: modelLabelForId(inherited),
  });
}

/** 首条消息发送前把草稿模型写入后端。 */
export async function persistDraftSessionModel(sessionId: string): Promise<void> {
  const current = bindingsBySession.get(sessionId);
  const modelId = current?.source === 'external'
    ? current.model_profile_id
    : getDraftSessionModelId();
  if (!modelId || !isDraftSession(sessionId)) return;
  try {
    const binding = await backendApi.setSessionModel(sessionId, modelId, {
      workspace_id: composerWorkspaceId(),
    });
    applySessionModelBinding(sessionId, binding);
  } catch {
    /* ensure_session 可能在 WS 侧已创建；失败不阻塞发送 */
  }
}

/**
 * 删除模型配置后，把仍指向已删模型的会话绑定回退到默认模型。
 * 后端会 rebind 持久化会话；此处同步内存缓存与 Composer 展示。
 */
export function reconcileSessionModelsAfterDelete(
  deletedId: string,
  fallbackModelId: string,
  reboundSessionIds: string[] = [],
): void {
  if (!deletedId || !fallbackModelId) return;

  const fallbackLabel = modelLabelForId(fallbackModelId);
  const reboundSet = new Set(reboundSessionIds);

  const rebind = (sessionId: string): void => {
    bindingsBySession.set(sessionId, {
      model_profile_id: fallbackModelId,
      model_label: fallbackLabel,
      pending_model_profile_id: null,
      pending_label: null,
      has_pending: false,
      pending: false,
    });
  };

  for (const [sid, binding] of bindingsBySession.entries()) {
    const usesDeleted =
      binding.model_profile_id === deletedId
      || binding.pending_model_profile_id === deletedId;
    if (usesDeleted || reboundSet.has(sid)) {
      rebind(sid);
    }
  }

  for (const sid of reboundSessionIds) {
    if (!bindingsBySession.has(sid)) {
      rebind(sid);
    }
  }

  if (isDraftSession(state.activeSessionId || '') && getDraftSessionModelId() === deletedId) {
    setDraftSessionModelId(fallbackModelId);
    if (state.activeSessionId) {
      rebind(state.activeSessionId);
    }
  }

  syncSessionModelUi();
  if (typeof window !== 'undefined' && state.activeSessionId) {
    window.dispatchEvent(new CustomEvent('session:model-changed', {
      detail: { sessionId: state.activeSessionId },
    }));
  }
}

/** 测试钩子：清空会话模型绑定缓存（bindingsBySession 为模块私有，store 重置不动它）。 */
export function __resetSessionModelBindingsForTest(): void {
  bindingsBySession.clear();
}
