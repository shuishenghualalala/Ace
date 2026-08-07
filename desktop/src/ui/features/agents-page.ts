import {
  backendApi,
  type ExternalAgent,
  type ExternalRuntime,
  type ExternalTeam,
  type ExternalTeamRole,
  type ExternalTeamSuggestion,
  type FormationPlan,
  type RequiredAgentConflict,
  type RuntimeModelProfile,
  type SessionAgentBinding,
} from '../backend-client';
import { setRuntimeStyle } from '../components/runtime-style';
import {
  $,
  $$,
  escapeHtml,
  loadFromStorage,
  notify,
  saveToStorage,
  setActiveExternalTeamForSession,
  state,
} from '../state';
import { STORAGE_KEYS } from '../../shared/storage-keys';
import { loadSessionModel, syncSessionModelAvailabilityUi } from './session-model';
import { refreshAllSessions } from './workspaces';
import {
  createAgentHubView,
  type AgentHubState,
  type AgentHubView,
} from './agent-hub';
import {
  EXTERNAL_AGENTS_DISABLED_MESSAGE,
  externalAgentsEnabled,
} from './external-agents-feature';
import { renderMarkdownHtml } from '../markdown';
import { showConfirmDialog } from '../ui-feedback';

type AgentsTab = 'mine' | 'runtime' | 'create-agent' | 'create-team';
type AgentsSelectOption = {
  value: string;
  label: string;
  description?: string;
  badge?: string;
};
type EnsureChatSessionFn = () => string;
type SessionAgentAssignedFn = (
  sessionId: string,
  agentLabel: { name?: string; provider?: string; display_badge?: string; model?: string },
  modelLabel?: string,
  agentBinding?: SessionAgentBinding,
) => void;
type FormationUiStatus =
  | 'idle'
  | 'fast_loading'
  | 'ai_reviewing'
  | 'ready_improved'
  | 'ready_unchanged'
  | 'ready_partial';
type AgentsGuideMode = 'hidden' | 'welcome' | 'tour';
type AgentsGuideStepNumber = 1 | 2 | 3;

export interface ExternalConversationCatalog {
  agents: ExternalAgent[];
  teams: ExternalTeam[];
  runtimes: ExternalRuntime[];
}

export interface ExternalConversationCatalog {
  agents: ExternalAgent[];
  teams: ExternalTeam[];
  runtimes: ExternalRuntime[];
}

const CREW_BUILTIN_AGENT_ID = 'crew::builtin';

const TEAM_DRAFT_DEBOUNCE_MS = 600;
const AGENTS_GUIDE_HIGHLIGHT_PADDING = 6;
const AGENTS_GUIDE_TOOLTIP_GAP = 12;
const AGENTS_GUIDE_VIEWPORT_MARGIN = 12;

const TEAM_REQUIRED_CAPABILITIES = [
  { key: 'information_retrieval', label: '检索', prompt: '必须包含信息检索能力。' },
  { key: 'analysis', label: '分析', prompt: '必须包含分析论证能力。' },
  { key: 'verification', label: '核验', prompt: '必须包含核验复核能力。' },
  { key: 'implementation', label: '实现', prompt: '必须包含执行实现能力。' },
  { key: 'documentation', label: '文档', prompt: '必须包含文档交付能力。' },
];

const defaultLeaderRole = [
  '### Leader 职责',
  '',
  '#### 工作原则',
  '- 拆清目标、拆小任务、持续汇总。',
  '- 每次推进都保证可交接、可验收、可继续。',
  '',
  '#### 职责',
  '- 理解团队目标，分配成员工作，检查阶段成果，形成最终输出。',
  '',
  '#### 团队协作关系',
  '- 向成员派发任务，收集结果后统一口径，必要时重新分配下一步。',
  '',
  '#### 输出格式',
  '- 当前成果：已经完成的结果。',
  '- 下一负责人：下一步由谁继续。',
  '- 下一动作：具体要做什么。',
  '- 风险/阻塞：缺少的信息、权限或依赖。',
].join('\n');

let activeTab: AgentsTab = 'mine';
let runtimes: ExternalRuntime[] = [];
let agents: ExternalAgent[] = [];
let teams: ExternalTeam[] = [];
let rolePresets: ExternalTeamRole[] = [];
let busy = false;
let runtimeScanning = false;
let message = '';
let activeTeamId = '';

let agentRuntimeId = '';
let agentName = '';
let agentModel = '';

let teamName = '';
let teamDescription = '';
let generatedTeamDescription = '';
let lastDescriptionDraftName = '';
let lastDescriptionDraftKey = '';
let descriptionDrafting = false;
let descriptionDraftStartedAt: number | null = null;
let descriptionDraftElapsedMs = 0;
let descriptionDraftMeta: { llmElapsedMs?: number; cacheHit?: boolean } | null = null;
let descriptionDraftSeq = 0;
let descriptionDraftAbort: AbortController | null = null;
let descriptionDraftTimer: number | null = null;
let descriptionDraftElapsedTimer: number | null = null;
let teamNameComposing = false;
let teamLeaderId = CREW_BUILTIN_AGENT_ID;
let teamWorkflow = '';
let selectedMembers: Record<string, boolean> = {};
let memberRoles: Record<string, string> = {};
let memberRoleKeys: Record<string, string> = {};
let memberRoleMeta: Record<string, ExternalTeamRole> = {};
let teamSpec: Record<string, unknown> | null = null;
let formationPlan: FormationPlan | null = null;
let formationStatus: FormationUiStatus = 'idle';
let formationElapsedMs = 0;
let formationImprovements: string[] = [];
let formationAiAttempted = false;
let formationRequestSeq = 0;
let formationRequestAbort: AbortController | null = null;
let formationElapsedTimer: number | null = null;
let teamRolesLocked = false;
let requiredTeamAgentIds: string[] = [];
let excludedTeamAgentIds: string[] = [];
let requiredTeamCapabilities: string[] = [];
let customTeamCapabilityInput = '';
let customTeamCapabilities: string[] = [];
let teamConstraintDecision: RequiredAgentConflict[] = [];
let ensureChatSession: EnsureChatSessionFn | null = null;
let onSessionAgentAssigned: SessionAgentAssignedFn | null = null;
let showTeamConstraints = false;
let showCustomCapabilityInput = false;
let agentsSelectPopover: HTMLElement | null = null;
let agentsSelectGlobalBound = false;
let agentHubView: AgentHubView | null = null;
let agentsGuideMode: AgentsGuideMode = 'hidden';
let agentsGuideStep: AgentsGuideStepNumber = 1;
let agentsGuideLayoutFrame: number | null = null;
let initialRuntimeScanStarted = false;

function stopFormationElapsedTimer(): void {
  if (formationElapsedTimer === null) return;
  window.clearInterval(formationElapsedTimer);
  formationElapsedTimer = null;
}

function startFormationElapsedTimer(startedAt: number): void {
  stopFormationElapsedTimer();
  formationElapsedMs = 0;
  formationElapsedTimer = window.setInterval(() => {
    formationElapsedMs = Date.now() - startedAt;
    $$('[data-formation-elapsed]').forEach((element) => {
      element.textContent = formatTeamDraftElapsed(formationElapsedMs);
    });
  }, 1000);
}

function invalidateFormationDecision(): void {
  formationRequestAbort?.abort();
  formationRequestAbort = null;
  formationRequestSeq += 1;
  stopFormationElapsedTimer();
  formationStatus = 'idle';
  formationElapsedMs = 0;
  formationImprovements = [];
  formationAiAttempted = false;
  teamRolesLocked = false;
  busy = false;
}

function resolveFormationUiStatus(suggestion: ExternalTeamSuggestion): FormationUiStatus {
  if (suggestion.selected_formation_mode === 'ai' && suggestion.ai_material_improvements?.length) {
    return 'ready_improved';
  }
  if ([
    'no_material_improvement',
    'quality_regressed',
    'baseline_coverage_regressed',
  ].includes(suggestion.fallback_reason || '')) {
    return 'ready_unchanged';
  }
  if (
    suggestion.requested_formation_mode === 'auto'
    && suggestion.selected_formation_mode === 'fast'
    && !suggestion.fallback_reason
  ) {
    return 'ready_unchanged';
  }
  return suggestion.selected_formation_mode === 'ai' ? 'ready_unchanged' : 'ready_partial';
}

function runtimeStatus(runtime: ExternalRuntime): 'ready' | 'degraded' | 'unavailable' {
  if (runtime.availability_status) return runtime.availability_status;
  if (runtime.available === true || runtime.healthy === true) return 'ready';
  return 'unavailable';
}

function externalActionError(prefix: string, error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error || '');
  if (
    detail.includes('external_agents_disabled')
    || detail.includes('功能已在配置中关闭')
    || detail.includes('外援功能暂未开放')
  ) {
    return EXTERNAL_AGENTS_DISABLED_MESSAGE;
  }
  return `${prefix}：${detail || '未知错误'}`;
}

function runtimeWasReplaced(runtime: ExternalRuntime): boolean {
  return runtime.metadata?.lifecycle_status === 'replaced'
    && typeof runtime.metadata?.replaced_by_runtime_id === 'string';
}

function runtimeStatusDetail(runtime: ExternalRuntime): string {
  const status = runtimeStatus(runtime);
  if (status === 'ready') return '';
  const probe = runtime.metadata?.probe;
  const probeRecord = probe && typeof probe === 'object'
    ? probe as Record<string, unknown>
    : undefined;
  const errorCode = String(probeRecord?.error_code || '').trim();
  const probeMessage = String(probeRecord?.message || '').trim();
  if (errorCode === 'models_empty') return '运行时没有返回可用模型，可点“再找找”重试';
  if (probeMessage) return probeMessage;
  if (status === 'degraded') return '已找到工具，但这次没能读取模型目录，可点“再找找”重试';
  return '当前未找到可执行文件；确认不再需要后可删除这条记录';
}

function runtimeModelOptions(runtimeId: string): RuntimeModelProfile[] {
  const runtime = runtimes.find((item) => item.id === runtimeId);
  const models = runtime?.metadata?.models;
  if (!Array.isArray(models)) return [];
  return models.flatMap((entry) => {
    if (typeof entry === 'string' && entry.trim()) return [{ id: entry, label: entry }];
    if (!entry || typeof entry !== 'object') return [];
    const model = entry as Record<string, unknown>;
    const id = String(model.id || model.modelId || model.model_id || '').trim();
    if (!id) return [];
    return [{
      id,
      label: String(model.label || model.name || id),
      provider: typeof model.provider === 'string' ? model.provider : undefined,
      default: model.default === true,
    }];
  });
}

function agentsSelectOptions(key: string): AgentsSelectOption[] {
  if (key === 'agent-runtime') {
    return runtimes
      .filter((runtime) => runtimeStatus(runtime) === 'ready')
      .map((runtime) => ({
        value: runtime.id,
        label: runtime.name || runtime.provider,
        description: `${providerLabel(runtime.provider)} · ${runtime.protocol || runtime.version || 'runtime'}`,
      }));
  }
  if (key === 'agent-model') {
    return runtimeModelOptions(agentRuntimeId).map((model) => ({
      value: model.id,
      label: model.label,
      description: model.label === model.id ? model.provider || model.id : model.id,
      ...(model.default ? { badge: '默认' } : {}),
    }));
  }
  if (key === 'team-leader') {
    return teamAgentOptions().filter(agentReadyForFormation).map((agent) => ({
      value: agent.id,
      label: agent.name,
      description: agentProviderDisplay(agent.provider),
    }));
  }
  if (key === 'team-required-agent') {
    return agentOptionsForConstraint('required').map((agent) => ({
      value: agent.id,
      label: agent.name,
      description: agentProviderDisplay(agent.provider),
    }));
  }
  if (key === 'team-excluded-agent') {
    return agentOptionsForConstraint('excluded').map((agent) => ({
      value: agent.id,
      label: agent.name,
      description: agentProviderDisplay(agent.provider),
    }));
  }
  if (key.startsWith('member-role:')) {
    return rolePresets.map((role) => ({
      value: role.key,
      label: role.label,
      description: role.description,
    }));
  }
  return [];
}

function agentsSelectCheck(): string {
  return '<svg class="composer-select-item__check" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
}

function renderAgentsSelect(
  key: string,
  value: string,
  placeholder: string,
  disabled = false,
): string {
  const options = agentsSelectOptions(key);
  const selected = options.find((option) => option.value === value);
  return `
    <div class="agent-form-select">
      <button
        type="button"
        class="agent-form-select__trigger"
        data-agents-select-key="${escapeHtml(key)}"
        aria-haspopup="listbox"
        aria-expanded="false"
        ${disabled ? 'disabled' : ''}
      >
        <span>${escapeHtml(selected?.label || placeholder)}</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
      </button>
    </div>
  `;
}

function closeAgentsSelect(): void {
  agentsSelectPopover?.remove();
  agentsSelectPopover = null;
  $$('[data-agents-select-key]').forEach((trigger) => {
    trigger.classList.remove('is-open');
    trigger.setAttribute('aria-expanded', 'false');
  });
}

function applyAgentsSelect(key: string, value: string): void {
  if (key === 'agent-runtime') {
    agentRuntimeId = value;
    selectRuntimeModel(agentRuntimeId);
    render();
    return;
  }
  if (key === 'agent-model') {
    agentModel = value;
    render();
    return;
  }
  if (key === 'team-leader') {
    changeLeader(value);
    return;
  }
  if (key === 'team-required-agent') {
    if (value && !requiredTeamAgentIds.includes(value)) requiredTeamAgentIds = [...requiredTeamAgentIds, value];
    teamRolesLocked = false;
    render();
    return;
  }
  if (key === 'team-excluded-agent') {
    if (value && !excludedTeamAgentIds.includes(value)) excludedTeamAgentIds = [...excludedTeamAgentIds, value];
    teamRolesLocked = false;
    render();
    return;
  }
  if (key.startsWith('member-role:')) {
    const agentId = key.slice('member-role:'.length);
    const meta = roleMetaForKey(value);
    memberRoleKeys[agentId] = value;
    if (meta) memberRoleMeta[agentId] = meta;
    memberRoles[agentId] = defaultRoleFor(agentId, value);
    teamRolesLocked = false;
    render();
  }
}

function openAgentsSelect(trigger: HTMLElement): void {
  closeAgentsSelect();
  const key = trigger.getAttribute('data-agents-select-key') || '';
  const options = agentsSelectOptions(key);
  const currentValue = key === 'agent-runtime'
    ? agentRuntimeId
    : key === 'agent-model'
      ? agentModel
      : key === 'team-leader'
        ? teamLeaderId
        : key.startsWith('member-role:')
          ? memberRoleKeys[key.slice('member-role:'.length)] || ''
          : '';
  const popover = document.createElement('div');
  popover.className = 'composer-floating-popover composer-select-popover agents-select-popover';
  popover.setAttribute('role', 'listbox');
  popover.innerHTML = options.length
    ? `<div class="composer-select-popover__list">${options.map((option) => `
        <button type="button" class="composer-select-item${option.value === currentValue ? ' is-selected' : ''}" data-agents-select-value="${escapeHtml(option.value)}" role="option" aria-selected="${option.value === currentValue ? 'true' : 'false'}">
          <span class="composer-select-item__icon composer-select-item__icon--model">${escapeHtml((option.label || '?').slice(0, 1).toUpperCase())}</span>
          <span class="composer-select-item__body">
            <span class="composer-select-item__title">${escapeHtml(option.label)}</span>
            ${option.description ? `<span class="composer-select-item__desc">${escapeHtml(option.description)}</span>` : ''}
          </span>
          ${option.badge ? `<span class="agent-form-select__badge">${escapeHtml(option.badge)}</span>` : ''}
          ${option.value === currentValue ? agentsSelectCheck() : '<span class="composer-select-item__spacer"></span>'}
        </button>
      `).join('')}</div>`
    : '<div class="composer-select-popover__empty">暂无可选项</div>';
  document.body.appendChild(popover);
  agentsSelectPopover = popover;
  trigger.classList.add('is-open');
  trigger.setAttribute('aria-expanded', 'true');

  const rect = trigger.getBoundingClientRect();
  const width = Math.min(360, Math.max(280, rect.width));
  setRuntimeStyle(popover, 'width', `${width}px`);
  const height = popover.offsetHeight || 180;
  const openUp = window.innerHeight - rect.bottom < height + 12 && rect.top > height + 12;
  setRuntimeStyle(popover, 'left', `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`);
  setRuntimeStyle(popover, 'top', openUp ? `${Math.max(8, rect.top - height - 6)}px` : `${rect.bottom + 6}px`);

  popover.querySelectorAll<HTMLElement>('[data-agents-select-value]').forEach((option) => {
    option.addEventListener('click', (event) => {
      event.stopPropagation();
      const next = option.getAttribute('data-agents-select-value') || '';
      closeAgentsSelect();
      applyAgentsSelect(key, next);
    });
  });
}

function selectRuntimeModel(runtimeId: string): void {
  const runtime = runtimes.find((item) => item.id === runtimeId);
  const models = runtimeModelOptions(runtimeId);
  const defaultId = String(runtime?.metadata?.default_model_id || '');
  agentModel = defaultId || models.find((model) => model.default)?.id || models[0]?.id || '';
}

function providerLabel(value?: string): string {
  return (value || 'agent').toUpperCase();
}

function agentProviderDisplay(value?: string): string {
  return (value || '').toLowerCase() === 'crew' ? 'Crew' : (value || 'external');
}

function teamAgentOptions(): ExternalAgent[] {
  return [
    {
      id: CREW_BUILTIN_AGENT_ID,
      name: 'Crew 内置智能体',
      provider: 'crew',
      runtime_id: '',
      model: 'builtin',
      system_prompt: '',
      custom_args: [],
      custom_env: {},
    },
    ...agents,
  ];
}

function agentNameById(agentId: string): string {
  return teamAgentOptions().find((agent) => agent.id === agentId)?.name || agentId;
}

function externalModelLabel(agent: ExternalAgent): string {
  return (agent.model || agent.provider || agent.name || '').trim();
}

function formatTeamDraftElapsed(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds > 0 ? `${minutes}min ${seconds}s` : `${minutes}min`;
}

function formatLlmElapsed(elapsedMs: number): string {
  return `${(Math.max(0, elapsedMs) / 1000).toFixed(1)}s`;
}

function decideTeamDescriptionDraftRequest(): {
  shouldRequest: boolean;
  shouldInvalidate: boolean;
  draftKey: string;
} {
  const normalizedName = teamName.trim();
  const draftKey = normalizedName;
  const regenerateDescription = normalizedName !== lastDescriptionDraftName;
  const descriptionCanBeGenerated = regenerateDescription
    || !teamDescription.trim()
    || teamDescription === generatedTeamDescription;
  const draftChanged = draftKey !== lastDescriptionDraftKey;
  return {
    draftKey,
    shouldInvalidate: !normalizedName,
    shouldRequest: Boolean(normalizedName && draftChanged && descriptionCanBeGenerated),
  };
}

function stopDescriptionDraftTimers(): void {
  if (descriptionDraftTimer != null) {
    window.clearTimeout(descriptionDraftTimer);
    descriptionDraftTimer = null;
  }
  if (descriptionDraftElapsedTimer != null) {
    window.clearInterval(descriptionDraftElapsedTimer);
    descriptionDraftElapsedTimer = null;
  }
}

function cancelDescriptionDraftRequest(): void {
  stopDescriptionDraftTimers();
  descriptionDraftAbort?.abort();
  descriptionDraftAbort = null;
  descriptionDraftSeq += 1;
  descriptionDrafting = false;
  descriptionDraftStartedAt = null;
  syncTeamDescriptionDraftUi();
}

function setTeamDescriptionDrafting(value: boolean): void {
  descriptionDrafting = value;
  if (!value) {
    descriptionDraftStartedAt = null;
    descriptionDraftElapsedMs = 0;
    if (descriptionDraftElapsedTimer != null) {
      window.clearInterval(descriptionDraftElapsedTimer);
      descriptionDraftElapsedTimer = null;
    }
    syncTeamDescriptionDraftUi();
    return;
  }
  descriptionDraftStartedAt = Date.now();
  descriptionDraftElapsedMs = 0;
  if (descriptionDraftElapsedTimer != null) window.clearInterval(descriptionDraftElapsedTimer);
  descriptionDraftElapsedTimer = window.setInterval(() => {
    if (descriptionDraftStartedAt == null) return;
    descriptionDraftElapsedMs = Date.now() - descriptionDraftStartedAt;
    syncTeamDescriptionDraftUi();
  }, 1000);
  syncTeamDescriptionDraftUi();
}

function syncSuggestTeamButton(): void {
  const button = $('[data-suggest-team]') as HTMLButtonElement | null;
  if (button && !busy) button.disabled = !teamName.trim();
}

function teamDescriptionHelperText(): string {
  if (descriptionDrafting) {
    return `正在生成参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}），无需等待也可以继续。`;
  }
  if (descriptionDraftMeta) {
    return `参考已生成 · ${descriptionDraftMeta.cacheHit ? '缓存' : formatLlmElapsed(descriptionDraftMeta.llmElapsedMs || 0)}`;
  }
  return '系统会按四个要点生成，你只需按需修改。';
}

function syncTeamDescriptionDraftUi(): void {
  const textarea = $('#team-description-input') as HTMLTextAreaElement | null;
  if (textarea) {
    const shouldUpdateValue = document.activeElement !== textarea || textarea.value === generatedTeamDescription;
    if (shouldUpdateValue && textarea.value !== teamDescription) textarea.value = teamDescription;
    textarea.classList.toggle('is-generating', descriptionDrafting);
    textarea.placeholder = descriptionDrafting && !teamDescription.trim()
      ? `正在生成目标参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}）…`
      : '1. 负责范围\n2. 所需能力\n3. 交付结果\n4. 验收标准';
  }
  const helper = $('[data-team-description-helper]');
  if (helper) helper.textContent = teamDescriptionHelperText();
}

function scheduleTeamDescriptionDraft(): void {
  const decision = decideTeamDescriptionDraftRequest();
  if (decision.shouldInvalidate) {
    cancelDescriptionDraftRequest();
    descriptionDraftMeta = null;
    lastDescriptionDraftKey = '';
    syncSuggestTeamButton();
    return;
  }
  if (!decision.shouldRequest) {
    syncSuggestTeamButton();
    return;
  }

  cancelDescriptionDraftRequest();
  const controller = new AbortController();
  descriptionDraftAbort = controller;
  const requestId = descriptionDraftSeq + 1;
  descriptionDraftSeq = requestId;
  lastDescriptionDraftKey = decision.draftKey;
  descriptionDraftMeta = null;
  setTeamDescriptionDrafting(true);
  syncSuggestTeamButton();

  descriptionDraftTimer = window.setTimeout(async () => {
    const name = teamName.trim();
    try {
      await backendApi.draftExternalTeamDescription(
        { name },
        {
          signal: controller.signal,
          onDescriptionDelta: (text) => {
            if (controller.signal.aborted || descriptionDraftSeq !== requestId) return;
            teamDescription = text;
            generatedTeamDescription = text;
            lastDescriptionDraftName = name;
            syncTeamDescriptionDraftUi();
          },
          onDraft: (draft, phase, meta) => {
            if (controller.signal.aborted || descriptionDraftSeq !== requestId) return;
            if (draft.description) {
              teamDescription = draft.description;
              generatedTeamDescription = draft.description;
              lastDescriptionDraftName = name;
            }
            descriptionDraftMeta = phase === 'optimized' ? meta : null;
            syncTeamDescriptionDraftUi();
          },
        },
      );
    } catch {
      if (controller.signal.aborted || descriptionDraftSeq !== requestId) return;
    } finally {
      if (descriptionDraftSeq === requestId) {
        if (descriptionDraftAbort === controller) descriptionDraftAbort = null;
        setTeamDescriptionDrafting(false);
        syncTeamDescriptionDraftUi();
      }
    }
  }, TEAM_DRAFT_DEBOUNCE_MS);
}

function roleMetaForKey(roleKey: string): ExternalTeamRole | undefined {
  return rolePresets.find((role) => role.key === roleKey);
}

function defaultRoleFor(agentId: string, roleKey: string): string {
  if (agentId === teamLeaderId) return defaultLeaderRole;
  const agentNameForRole = agentNameById(agentId);
  const meta = roleMetaForKey(roleKey);
  const label = meta?.label || '团队成员';
  const desc = meta?.description || '承担团队中的专业执行与交付工作。';
  return [
    `### ${label}`,
    '',
    '#### 工作原则',
    `- 围绕团队目标，按 Leader 安排完成 ${agentNameForRole} 负责的任务。`,
    '- 输出必须清晰、可交接、可验收。',
    '',
    '#### 职责',
    `- ${desc}`,
    '',
    '#### 团队协作关系',
    '- 接收 Leader 分配的上下文与约束。',
    '- 完成后向 Leader 提交结果、产物和风险。',
    '',
    '#### 输出格式',
    '- 已完成内容',
    '- 关键依据',
    '- 风险/待确认',
  ].join('\n');
}

function buildTeamConstraintText(): string {
  const capabilityKeys = new Set(requiredTeamCapabilities);
  return [
    ...requiredTeamAgentIds.map((id) => `${agentNameById(id)} 必须作为成员加入团队。`),
    ...excludedTeamAgentIds.map((id) => `不要让 ${agentNameById(id)} 加入团队。`),
    ...TEAM_REQUIRED_CAPABILITIES.filter((item) => capabilityKeys.has(item.key)).map((item) => item.prompt),
    ...customTeamCapabilities.map((capability) => `团队还必须具备「${capability}」能力。`),
  ].filter(Boolean).join('\n');
}

async function refresh(): Promise<void> {
  const [nextRuntimes, nextAgents, nextTeams, nextRoles] = await Promise.all([
    backendApi.runtimes().catch(() => []),
    backendApi.externalAgents().catch(() => []),
    backendApi.externalTeams().catch(() => []),
    backendApi.externalTeamRoles().catch(() => []),
  ]);
  runtimes = nextRuntimes;
  agents = nextAgents;
  teams = nextTeams;
  rolePresets = nextRoles;
  if (!rolePresets.some((role) => role.key === 'project_manager')) {
    rolePresets = [
      {
        key: 'project_manager',
        label: '项目统筹',
        description: '负责拆解目标、分配任务、检查结果并汇总交付。',
        capabilities: ['planning', 'review', 'synthesis'],
        workflow_lane: 'lead',
      },
      ...rolePresets,
    ];
  }
}

export async function loadExternalConversationCatalog(): Promise<ExternalConversationCatalog> {
  if (!externalAgentsEnabled()) return { agents: [], teams: [], runtimes: [] };
  const [nextRuntimes, nextAgents, nextTeams] = await Promise.all([
    backendApi.runtimes(),
    backendApi.externalAgents(),
    backendApi.externalTeams(),
  ]);
  runtimes = nextRuntimes;
  agents = nextAgents;
  teams = nextTeams;
  return {
    runtimes: [...runtimes],
    agents: [...agents],
    teams: [...teams],
  };
}

function teamLeaderName(team: ExternalTeam): string {
  if (!team.leader_agent_id) return '未指定 Leader';
  return team.leader_agent_id === CREW_BUILTIN_AGENT_ID ? 'Crew 内置智能体' : agentNameById(team.leader_agent_id);
}

function teamMemberCount(team: ExternalTeam): number {
  return Array.isArray(team.members) ? team.members.length : 0;
}

function renderTeamDetail(): string {
  const team = teams.find((item) => item.id === activeTeamId);
  if (!team) return '';
  const leader = teamAgentOptions().find((agent) => agent.id === team.leader_agent_id);
  const workflow = String(team.instructions || team.workflow || '').trim();
  const members = [...(team.members || [])].sort((left, right) => {
    if (left.agent_id === team.leader_agent_id) return -1;
    if (right.agent_id === team.leader_agent_id) return 1;
    return (left.sort_order || 0) - (right.sort_order || 0);
  });
  return `
    <div class="team-modal-backdrop" data-team-detail-backdrop>
      <div class="team-modal team-detail-modal" role="dialog" aria-modal="true" aria-labelledby="team-detail-title">
        <div class="team-modal__head">
          <div class="team-modal__title">
            <div class="formation-avatar formation-avatar--leader" aria-hidden="true"><span>${escapeHtml(leader?.display_badge || '?')}</span></div>
            <div>
              <span>团队</span>
              <strong id="team-detail-title">${escapeHtml(team.name || '未命名团队')}</strong>
              <p>Leader：${escapeHtml(teamLeaderName(team))}</p>
            </div>
          </div>
          <button type="button" class="agent-icon-btn" data-team-detail-close>关闭</button>
        </div>
        ${team.description ? `
          <section class="team-modal__section">
            <h3>团队描述</h3>
            <p class="team-modal__plain">${escapeHtml(team.description)}</p>
          </section>` : ''}
        ${workflow ? `
          <section class="team-modal__section">
            <h3>团队工作流</h3>
            <div class="md-body chat-markdown team-modal__md">${renderMarkdownHtml(workflow)}</div>
          </section>` : ''}
        <section class="team-modal__section">
          <h3>成员职责</h3>
          <div class="team-modal__members">
            ${members.map((member) => {
              const agent = teamAgentOptions().find((item) => item.id === member.agent_id);
              const isLeader = member.agent_id === team.leader_agent_id;
              const name = member.agent_name || agent?.name || member.agent_id;
              const roleMeta = [member.role_label, member.workflow_lane].filter(Boolean).join(' · ');
              return `
                <article class="team-modal-member${isLeader ? ' is-leader' : ''}">
                  <div class="team-modal-member__head">
                    <span class="formation-member-badge">${escapeHtml(member.display_badge || agent?.display_badge || '?')}</span>
                    <div>
                      <strong>${escapeHtml(name)}${isLeader ? '<span class="pixel-flag" title="Leader" aria-label="Leader"></span>' : ''}</strong>
                      <p>${escapeHtml(agent?.provider || 'external')}${isLeader ? ' · Leader' : ''}</p>
                      ${roleMeta ? `<p>${escapeHtml(roleMeta)}</p>` : ''}
                    </div>
                  </div>
                  <div class="md-body chat-markdown team-modal__md">${renderMarkdownHtml(member.role || '未填写职责')}</div>
                </article>`;
            }).join('')}
            ${members.length ? '' : '<div class="agents-empty">暂无团队成员</div>'}
          </div>
        </section>
      </div>
    </div>`;
}

function renderCreateAgent(): string {
  const models = runtimeModelOptions(agentRuntimeId);
  return `
    <section class="agents-section agents-form">
      <h2>添加外援</h2>
      <div class="agents-form__field">
        <span>可用外援</span>
        ${renderAgentsSelect('agent-runtime', agentRuntimeId, '请选择外援')}
      </div>
      <label>
        <span>外援称呼</span>
        <input id="agents-name-input" value="${escapeHtml(agentName)}" placeholder="给外援起个名字" maxlength="64" />
      </label>
      <div class="agents-form__field">
        <span>使用模型</span>
        ${renderAgentsSelect('agent-model', agentModel, models.length ? '请选择模型' : '当前运行时没有可选模型', !models.length)}
      </div>
      <button type="button" class="agent-btn agent-btn--dark" data-create-agent ${busy || !agentRuntimeId || !agentModel ? 'disabled' : ''}>加入我的外援</button>
    </section>
  `;
}

function renderCapabilityChecks(): string {
  return TEAM_REQUIRED_CAPABILITIES.map((capability) => {
    const active = requiredTeamCapabilities.includes(capability.key);
    return `
      <button type="button" class="${active ? 'is-active' : ''}" aria-pressed="${active ? 'true' : 'false'}" data-required-capability="${escapeHtml(capability.key)}">
        ${active ? '✓ ' : ''}${escapeHtml(capability.label)}
      </button>
    `;
  }).join('');
}

function teamCapabilityLabel(key: string): string {
  return TEAM_REQUIRED_CAPABILITIES.find((item) => item.key === key)?.label || key;
}

function teamConstraintCount(): number {
  return requiredTeamAgentIds.length + excludedTeamAgentIds.length + requiredTeamCapabilities.length + customTeamCapabilities.length;
}

function agentOptionsForConstraint(kind: 'required' | 'excluded'): ExternalAgent[] {
  const selectedIds = kind === 'required' ? requiredTeamAgentIds : excludedTeamAgentIds;
  const blockedIds = new Set(kind === 'required' ? excludedTeamAgentIds : requiredTeamAgentIds);
  return agents.filter((agent) => (
    agent.id !== teamLeaderId
    && !selectedIds.includes(agent.id)
    && !blockedIds.has(agent.id)
    && (kind === 'excluded' || agentReadyForFormation(agent))
  ));
}

function agentReadyForFormation(agent: ExternalAgent): boolean {
  if (agent.id === CREW_BUILTIN_AGENT_ID || !agent.profile) return true;
  return agent.profile.availability === 'ready'
    && agent.profile.model?.binding_status !== 'missing';
}

function renderConstraintSelect(kind: 'required' | 'excluded'): string {
  const key = kind === 'required' ? 'team-required-agent' : 'team-excluded-agent';
  return renderAgentsSelect(key, '', '选择外援', agentsSelectOptions(key).length === 0);
}

function renderConstraintValues(kind: 'required' | 'excluded'): string {
  const selectedIds = kind === 'required' ? requiredTeamAgentIds : excludedTeamAgentIds;
  const dataName = kind === 'required' ? 'remove-required-agent' : 'remove-excluded-agent';
  if (!selectedIds.length) return '<span>未指定</span>';
  return selectedIds.map((agentId) => `
    <button type="button" data-${dataName}="${escapeHtml(agentId)}">
      ${escapeHtml(agentNameById(agentId))} ×
    </button>
  `).join('');
}

function renderTeamConstraintAck(reviewAgentIds: Set<string>): string {
  if (teamConstraintCount() === 0) return '';
  return `
    <div class="team-constraint-ack" aria-label="已考虑的组队约束">
      <div class="team-constraint-ack__head">
        <span aria-hidden="true">✓</span>
        <div>
          <strong>已考虑你的组队约束</strong>
          <small>下面这些要求已参与成员筛选和能力分工。</small>
        </div>
      </div>
      <div class="team-constraint-ack__items">
        ${requiredTeamAgentIds.map((agentId) => `
          <span class="${reviewAgentIds.has(agentId) ? 'is-applied' : 'is-missing'}">
            ${reviewAgentIds.has(agentId) ? '已保留' : '未保留'} · ${escapeHtml(agentNameById(agentId))}
          </span>
        `).join('')}
        ${excludedTeamAgentIds.map((agentId) => `
          <span class="${!reviewAgentIds.has(agentId) ? 'is-applied' : 'is-missing'}">
            ${!reviewAgentIds.has(agentId) ? '已排除' : '仍在团队'} · ${escapeHtml(agentNameById(agentId))}
          </span>
        `).join('')}
        ${requiredTeamCapabilities.map((capability) => {
          const covered = formationPlan?.coverage?.covered?.includes(capability) || false;
          return `
            <span class="${covered ? 'is-applied is-capability' : 'is-missing is-capability'}">
              ${covered ? '已覆盖' : '未覆盖'} · ${escapeHtml(teamCapabilityLabel(capability))}
            </span>
          `;
        }).join('')}
        ${customTeamCapabilities.map((capability) => `
          <span class="is-considered is-capability">已纳入目标 · ${escapeHtml(capability)}</span>
        `).join('')}
      </div>
    </div>
  `;
}

function renderCoverageChips(): string {
  if (!formationPlan) return '';
  return `
    <div class="team-review-section">
      <div class="team-review-section__head">
        <div>
          <strong>能力覆盖</strong>
          <span>系统按最小充分原则选择成员。</span>
        </div>
      </div>
      <div class="team-coverage-chips">
        ${formationPlan.coverage.required.map((capability) => {
          const covered = formationPlan?.coverage.covered.includes(capability);
          return `<span class="${covered ? 'is-covered' : 'is-missing'}">${covered ? '✓' : '!'} ${escapeHtml(capability)}</span>`;
        }).join('')}
      </div>
    </div>
  `;
}

function selectedReviewAgents(): ExternalAgent[] {
  return teamAgentOptions()
    .filter((agent) => selectedMembers[agent.id])
    .sort((a, b) => {
      if (a.id === teamLeaderId) return -1;
      if (b.id === teamLeaderId) return 1;
      const orderA = formationPlan?.members.findIndex((member) => member.agent_id === a.id) ?? 999;
      const orderB = formationPlan?.members.findIndex((member) => member.agent_id === b.id) ?? 999;
      return orderA - orderB;
    });
}

function renderMemberReview(): string {
  const reviewAgents = selectedReviewAgents();
  if (!reviewAgents.length) return '<div class="agents-empty">还没有成员。点击“智能组队”后会生成建议阵容。</div>';
  return `
    <div class="team-review-list">
      ${reviewAgents.map((agent) => {
        const roleKey = memberRoleKeys[agent.id] || (agent.id === teamLeaderId ? 'project_manager' : rolePresets[0]?.key || 'member');
        return `
          <article class="team-review-card">
            <div class="team-review-card__head">
              <strong>${escapeHtml(agent.name)}</strong>
              <span>${agent.id === teamLeaderId ? 'Leader' : escapeHtml(memberRoleMeta[agent.id]?.label || roleKey)}</span>
              ${agent.id !== teamLeaderId ? `<button type="button" class="pixel-action-btn" data-remove-member="${escapeHtml(agent.id)}" aria-label="移除 ${escapeHtml(agent.name)}">×</button>` : ''}
            </div>
            <label>
              <span>角色</span>
              ${renderAgentsSelect(`member-role:${agent.id}`, roleKey, '选择角色', rolePresets.length === 0)}
            </label>
            <label>
              <span>职责</span>
              <textarea data-member-role="${escapeHtml(agent.id)}" rows="5">${escapeHtml(memberRoles[agent.id] || defaultRoleFor(agent.id, roleKey))}</textarea>
            </label>
          </article>
        `;
      }).join('')}
    </div>
  `;
}

function renderTeamConstraintDecision(): string {
  if (!teamConstraintDecision.length) return '';
  return `
    <div class="team-modal-backdrop" data-team-decision-backdrop>
      <div class="team-modal agents-decision-modal" role="dialog" aria-modal="true" aria-labelledby="agents-decision-title">
        <button type="button" class="team-modal__close" data-team-decision-close aria-label="关闭">×</button>
        <h2 id="agents-decision-title">指定成员与能力要求不完全匹配</h2>
        <p>系统建议按能力筛选掉以下成员。你也可以坚持保留，系统会按你的选择重新组队。</p>
        <div class="agents-decision-list">
          ${teamConstraintDecision.map((conflict) => `
            <article class="agents-decision-item">
              <strong>${escapeHtml(conflict.agent_name || conflict.agent_id)}</strong>
              <p>${escapeHtml(conflict.reason || '当前能力画像未覆盖本次团队所需能力。')}</p>
              <div class="agents-display-tags">
                ${(conflict.required_capabilities || []).map((capability) => `<span>${escapeHtml(capability)}</span>`).join('')}
              </div>
            </article>
          `).join('')}
        </div>
        <div class="team-modal__actions">
          <button type="button" class="agent-btn" data-team-decision-edit>返回修改</button>
          <button type="button" class="agent-btn" data-team-decision-filter>按能力筛选</button>
          <button type="button" class="agent-btn agent-btn--dark" data-team-decision-force>仍然加入团队</button>
        </div>
      </div>
    </div>
  `;
}

function renderFormationProgress(): string {
  if (formationStatus === 'idle') return '';
  const running = formationStatus === 'fast_loading' || formationStatus === 'ai_reviewing';
  const finished = formationStatus.startsWith('ready_');
  const resultText = formationStatus === 'ready_improved'
    ? '团队方案已优化，成员分工和能力覆盖已经更新。'
    : formationStatus === 'ready_unchanged'
      ? '团队方案已检查，当前成员和分工已经合适，无需调整。'
      : '初步团队方案已生成，智能检查暂未完成，你仍然可以使用当前方案。';
  const reviewStepClass = formationStatus === 'ai_reviewing'
    ? 'is-active'
    : formationStatus === 'ready_partial'
      ? 'is-warning'
      : finished ? 'is-done' : '';
  return `
    <section class="formation-progress formation-progress--${formationStatus}" aria-live="polite">
      <div class="formation-progress__head">
        <div>
          <strong>${escapeHtml(teamName.trim() || '我的团队')}</strong>
          <span>${running
            ? `正在智能组队 · <b data-formation-elapsed>${escapeHtml(formatTeamDraftElapsed(formationElapsedMs))}</b>`
            : escapeHtml(resultText)}</span>
        </div>
        ${formationStatus === 'ready_partial'
          ? '<button class="agent-btn" type="button" data-recheck-formation>重新检查</button>'
          : ''}
      </div>
      <ol class="formation-progress__steps">
        <li class="${formationStatus === 'fast_loading' ? 'is-active' : 'is-done'}"><i aria-hidden="true">${formationStatus === 'fast_loading' ? '·' : '✓'}</i><span>生成初步方案</span></li>
        <li class="${reviewStepClass}"><i aria-hidden="true">${formationStatus === 'ready_partial' ? '!' : finished ? '✓' : '·'}</i><span>智能检查优化${formationStatus === 'ai_reviewing' || (finished && formationAiAttempted) ? ` · <b data-formation-elapsed>${escapeHtml(formatTeamDraftElapsed(formationElapsedMs))}</b>` : ''}</span></li>
        <li class="${finished ? 'is-done' : ''}"><i aria-hidden="true">${finished ? '✓' : '·'}</i><span>方案已就绪</span></li>
      </ol>
      ${formationStatus === 'ready_improved' && formationImprovements.length
        ? `<div class="formation-progress__improvements">${formationImprovements.slice(0, 3).map((item) => `<span>✓ ${escapeHtml(item)}</span>`).join('')}</div>`
        : ''}
    </section>`;
}

function renderCreateTeam(): string {
  const coveragePercent = Math.round((formationPlan?.confidence?.coverage || 0) * 100);
  const confidencePercent = Math.round((formationPlan?.confidence?.overall || 0) * 100);
  const reviewAgents = selectedReviewAgents();
  const reviewAgentIds = new Set(reviewAgents.map((agent) => agent.id));
  const warning = formationPlan?.warnings?.[0] || '';
  const constraintCount = teamConstraintCount();
  return `
    <section class="agents-section agents-form team-create">
      <div class="team-create__heading">
        <span class="team-mark team-mark--hero team-create__team-mark" aria-hidden="true"><i></i><i></i></span>
        <div>
          <h2>${teamRolesLocked ? '确认阵容' : '创建团队'}</h2>
          <p>${teamRolesLocked ? '阵容已经配好，看看成员和分工是否合适。' : '告诉我团队要做什么，成员和职责交给我来搭。'}</p>
        </div>
        <div class="team-create__steps" aria-label="创建进度">
          <span class="${teamRolesLocked ? 'is-done' : 'is-active'}">1 定义团队</span>
          <span class="${teamRolesLocked ? 'is-active' : ''}">2 确认阵容</span>
        </div>
      </div>

      ${!teamRolesLocked ? `
        <div class="team-create__setup">
          <label>
            <span>团队名称</span>
            <input id="team-name-input" value="${escapeHtml(teamName)}" placeholder="例如：产品研发小队" maxlength="80" autofocus />
            <small>先起个名字，团队目标参考会自动补上。</small>
          </label>
          <label>
            <span>团队目标 <em>系统生成参考</em></span>
            <textarea id="team-description-input" class="${descriptionDrafting ? 'is-generating' : ''}" rows="3" placeholder="${descriptionDrafting && !teamDescription.trim() ? `正在生成目标参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}）…` : '1. 负责范围&#10;2. 所需能力&#10;3. 交付结果&#10;4. 验收标准'}">${escapeHtml(teamDescription)}</textarea>
            <small data-team-description-helper aria-live="polite">${escapeHtml(teamDescriptionHelperText())}</small>
          </label>
          <div class="agents-form__field team-create__leader">
            <span>Leader <em>已默认推荐</em></span>
            ${renderAgentsSelect('team-leader', teamLeaderId, '使用系统推荐 Leader')}
            <small>Crew 会负责拆任务、盯进度和收口；你也可以换成其他 Agent。</small>
          </div>
          <div class="team-constraints">
            <button class="team-constraints__toggle" type="button" aria-expanded="${showTeamConstraints ? 'true' : 'false'}" data-toggle-team-constraints>
              <span><i aria-hidden="true">+</i> 组队约束（选填）</span>
              <small>${constraintCount ? `已设置 ${constraintCount} 项` : '不设置也可以'}</small>
            </button>
            ${showTeamConstraints ? `
              <div class="team-constraints__body">
                <p>只有明确的人选或能力要求才需要设置，其他情况直接智能组队即可。</p>
                <div class="team-constraint-row">
                  <div><strong>必须包含</strong><small>这些 Agent 一定会进入团队</small></div>
                  ${renderConstraintSelect('required')}
                  <div class="team-constraint-values">${renderConstraintValues('required')}</div>
                </div>
                <div class="team-constraint-row">
                  <div><strong>排除成员</strong><small>这些 Agent 不会进入团队</small></div>
                  ${renderConstraintSelect('excluded')}
                  <div class="team-constraint-values">${renderConstraintValues('excluded')}</div>
                </div>
                <div class="team-constraint-row team-constraint-row--capabilities">
                  <div><strong>必需能力</strong><small>团队里必须有人负责这些工作</small></div>
                  <div class="team-constraint-chips">
                    ${renderCapabilityChecks()}
                    ${customTeamCapabilities.map((capability) => `<button type="button" class="is-active is-custom" data-remove-custom-capability="${escapeHtml(capability)}">${escapeHtml(capability)} ×</button>`).join('')}
                    <button type="button" class="team-capability-add" data-toggle-custom-capability>+ 自定义能力</button>
                  </div>
                  ${showCustomCapabilityInput ? `
                    <div class="team-capability-custom-input">
                      <input id="team-custom-capability-input" value="${escapeHtml(customTeamCapabilityInput)}" placeholder="例如：数据分析、安全审查" />
                      <button type="button" ${customTeamCapabilityInput.trim() ? '' : 'disabled'} data-add-custom-capability>添加</button>
                    </div>
                  ` : ''}
                  ${customTeamCapabilities.length ? '<small class="team-capability-note">你填写的能力会一起提交，系统会从现有 Agent 中尽量匹配。</small>' : ''}
                </div>
              </div>
            ` : ''}
          </div>
          <div class="team-create__primary-action">
            <div>
              <strong>准备好了</strong>
              <span>确认目标和约束后，一键生成合适阵容。</span>
            </div>
            <button type="button" class="pixel-summon-btn" data-suggest-team ${busy || !teamName.trim() ? 'disabled' : ''}>
              ${busy ? '智能组队中…' : '智能组队'}
            </button>
          </div>
          ${renderFormationProgress()}
        </div>
      ` : `
        <div class="team-create__review">
          ${renderFormationProgress()}
          <div class="team-formation-summary">
            <div>
              <strong>${escapeHtml(teamName || '我的团队')}</strong>
              <p>${escapeHtml(teamDescription || '系统已根据团队目标完成组队。')}</p>
            </div>
            <div class="team-formation-metrics">
              <span><b>${reviewAgents.length}</b> 名成员</span>
              <span><b>${coveragePercent}%</b> 能力覆盖</span>
              <span><b>${confidencePercent}%</b> 组队置信度</span>
              ${warning ? `<span><b>!</b> ${escapeHtml(warning)}</span>` : ''}
            </div>
          </div>
          ${renderTeamConstraintAck(reviewAgentIds)}
          <div class="team-review-section">
            <div class="team-review-section__head">
              <div>
                <strong>成员与职责</strong>
                <span>可直接修改每个成员的角色和职责。</span>
              </div>
              <em>${reviewAgents.length} 人</em>
            </div>
            ${renderMemberReview()}
          </div>
          ${renderCoverageChips()}
          ${teamWorkflow ? `
            <details class="team-workflow-preview">
              <summary>默认协作方式 <span>按任务运行时再生成实际 DAG</span></summary>
              <div>${escapeHtml(teamWorkflow).replace(/\n/g, '<br />')}</div>
            </details>
          ` : ''}
          <label>
            <span>团队工作流 / 说明</span>
            <textarea id="team-workflow-input" rows="4" placeholder="智能组队会生成建议工作流，也可以手动修改。">${escapeHtml(teamWorkflow)}</textarea>
          </label>
          <div class="team-create__review-actions">
            <button type="button" class="agent-btn" data-return-team-setup>返回修改</button>
            <button type="button" class="agent-btn" data-suggest-team ${busy ? 'disabled' : ''}>重新组队</button>
            <button type="button" class="agent-btn agent-btn--dark" data-create-team ${busy || !reviewAgents.length ? 'disabled' : ''}>创建团队</button>
          </div>
        </div>
      `}
    </section>
  `;
}

function createLegacyAgentForm(): HTMLElement | undefined {
  if (activeTab !== 'create-agent' && activeTab !== 'create-team') return undefined;
  const host = document.createElement('div');
  host.className = 'mw-agent-hub__legacy-form';
  host.innerHTML = activeTab === 'create-agent'
    ? renderCreateAgent()
    : `${renderCreateTeam()}${renderTeamConstraintDecision()}`;
  return host;
}

function agentHubState(): AgentHubState {
  const form = createLegacyAgentForm();
  return {
    tab: activeTab,
    agents: agents.map((agent) => ({
      id: agent.id,
      name: agent.name || '未命名智能体',
      provider: providerLabel(agent.provider),
      detail: [
        agent.model || '默认模型',
        agent.status || 'ready',
        agent.runtime_id ? `runtime ${agent.runtime_id}` : '',
      ].filter(Boolean).join(' · ') || agent.description || '外部智能体',
      tags: (agent.capabilities?.length ? agent.capabilities : agent.tags || []).slice(0, 4),
      available: agentReadyForFormation(agent),
    })),
    teams: teams.map((team) => ({
      id: team.id,
      name: team.name || '未命名团队',
      description: team.description || `Leader：${teamLeaderName(team)}`,
      memberCount: teamMemberCount(team),
      available: true,
    })),
    runtimes: runtimes.filter((runtime) => !runtimeWasReplaced(runtime)).map((runtime) => ({
      id: runtime.id,
      name: runtime.name || runtime.provider,
      provider: providerLabel(runtime.provider),
      detail: runtime.version || providerLabel(runtime.provider),
      statusDetail: runtimeStatusDetail(runtime),
      availability: runtimeStatus(runtime),
      deletable: runtimeStatus(runtime) === 'unavailable'
        || typeof runtime.metadata?.runtime_profile_version !== 'number',
    })),
    loading: busy && !runtimeScanning && activeTab !== 'create-team',
    scanning: runtimeScanning,
    message,
    featureEnabled: externalAgentsEnabled(),
    ...(form ? { form } : {}),
  };
}

function selectAgentHubTab(next: AgentsTab): void {
  activeTab = next;
  if (agentsGuideMode === 'tour') {
    agentsGuideStep = next === 'runtime' ? 1 : next === 'create-agent' ? 2 : 3;
  }
  activeTeamId = '';
  message = '';
  render();
}

function selectRuntime(runtimeId: string): void {
  agentRuntimeId = runtimeId;
  agentName = '';
  selectRuntimeModel(agentRuntimeId);
  activeTab = 'create-agent';
  message = '已选择外援，可以继续加入阵容。';
  render();
}

function guideTarget(): {
  progress: string;
  title: string;
  body: string;
  selector: string;
} {
  if (agentsGuideStep === 1) {
    return {
      progress: '1/3',
      title: '先认识一下附近的帮手',
      body: '这里会列出电脑上可用的 AI 工具。点“再找找”可以主动刷新。',
      selector: '[data-scan-runtimes]',
    };
  }
  if (agentsGuideStep === 2) {
    return {
      progress: '2/3',
      title: '把合适的外援加入阵容',
      body: '选择一位可用外援，起个顺口的称呼并确认模型。',
      selector: '[data-agents-select-key="agent-runtime"]',
    };
  }
  return {
    progress: '3/3',
    title: agents.length ? '准备好，就可以直接派活' : '外援到位后，就能派活或组队',
    body: agents.length
      ? '点“派活”让外援接手当前任务；复杂任务还可以把多位外援拉进团队。'
      : '从“添加外援”加入一位，就会在我的阵容里看到“派活”。',
    selector: agents.length ? '[data-use-agent]' : '[data-agents-tab="create-agent"]',
  };
}

function clampGuidePosition(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function cancelAgentsGuideLayout(): void {
  if (agentsGuideLayoutFrame === null) return;
  window.cancelAnimationFrame(agentsGuideLayoutFrame);
  agentsGuideLayoutFrame = null;
}

function clearAgentsGuideTarget(): void {
  document.querySelectorAll('.agents-guide-target').forEach((target) => {
    target.classList.remove('agents-guide-target');
  });
}

function placeAgentsGuideBubble(bubble: HTMLElement, rect: DOMRect): void {
  const bubbleRect = bubble.getBoundingClientRect();
  const bubbleWidth = bubbleRect.width || 320;
  const bubbleHeight = bubbleRect.height || 176;
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;

  let left = rect.left + rect.width / 2 - bubbleWidth / 2;
  let top = rect.bottom + AGENTS_GUIDE_TOOLTIP_GAP;
  if (top + bubbleHeight > viewportHeight - AGENTS_GUIDE_VIEWPORT_MARGIN) {
    top = rect.top - AGENTS_GUIDE_TOOLTIP_GAP - bubbleHeight;
  }
  if (rect.left > viewportWidth * 0.6
    && rect.left - AGENTS_GUIDE_TOOLTIP_GAP - bubbleWidth > AGENTS_GUIDE_VIEWPORT_MARGIN) {
    left = rect.left - AGENTS_GUIDE_TOOLTIP_GAP - bubbleWidth;
    top = rect.top + rect.height / 2 - bubbleHeight / 2;
  }

  setRuntimeStyle(
    bubble,
    'left',
    `${Math.round(clampGuidePosition(
      left,
      AGENTS_GUIDE_VIEWPORT_MARGIN,
      viewportWidth - bubbleWidth - AGENTS_GUIDE_VIEWPORT_MARGIN,
    ))}px`,
  );
  setRuntimeStyle(
    bubble,
    'top',
    `${Math.round(clampGuidePosition(
      top,
      AGENTS_GUIDE_VIEWPORT_MARGIN,
      viewportHeight - bubbleHeight - AGENTS_GUIDE_VIEWPORT_MARGIN,
    ))}px`,
  );
}

function layoutAgentsGuide(): void {
  if (agentsGuideMode !== 'tour') return;
  const portal = document.querySelector<HTMLElement>('[data-agents-guide-portal]');
  const highlight = portal?.querySelector<HTMLElement>('[data-agents-guide-highlight]');
  const bubble = portal?.querySelector<HTMLElement>('[data-agents-guide]');
  const target = document.querySelector<HTMLElement>(guideTarget().selector);
  if (!highlight || !bubble || !target) return;

  const rect = target.getBoundingClientRect();
  if (rect.width < 4 || rect.height < 4) {
    highlight.hidden = true;
    return;
  }

  highlight.hidden = false;
  setRuntimeStyle(highlight, 'left', `${Math.round(rect.left - AGENTS_GUIDE_HIGHLIGHT_PADDING)}px`);
  setRuntimeStyle(highlight, 'top', `${Math.round(rect.top - AGENTS_GUIDE_HIGHLIGHT_PADDING)}px`);
  setRuntimeStyle(
    highlight,
    'width',
    `${Math.round(rect.width + AGENTS_GUIDE_HIGHLIGHT_PADDING * 2)}px`,
  );
  setRuntimeStyle(
    highlight,
    'height',
    `${Math.round(rect.height + AGENTS_GUIDE_HIGHLIGHT_PADDING * 2)}px`,
  );
  placeAgentsGuideBubble(bubble, rect);
}

function scheduleAgentsGuideLayout(): void {
  cancelAgentsGuideLayout();
  agentsGuideLayoutFrame = window.requestAnimationFrame(() => {
    agentsGuideLayoutFrame = null;
    layoutAgentsGuide();
  });
}

function onAgentsGuideKeydown(event: KeyboardEvent): void {
  if (agentsGuideMode !== 'tour') return;
  if (event.key === 'Escape') finishAgentsGuide();
  if (event.key === 'ArrowLeft' && agentsGuideStep > 1) {
    setAgentsGuideStep((agentsGuideStep - 1) as AgentsGuideStepNumber);
  }
  if (event.key === 'ArrowRight') {
    if (agentsGuideStep === 3) finishAgentsGuide();
    else setAgentsGuideStep((agentsGuideStep + 1) as AgentsGuideStepNumber);
  }
}

function onAgentsGuideResize(): void {
  scheduleAgentsGuideLayout();
}

function bindAgentsGuideWindowEvents(): void {
  document.removeEventListener('keydown', onAgentsGuideKeydown);
  window.removeEventListener('resize', onAgentsGuideResize);
  document.addEventListener('keydown', onAgentsGuideKeydown);
  window.addEventListener('resize', onAgentsGuideResize);
}

function unbindAgentsGuideWindowEvents(): void {
  document.removeEventListener('keydown', onAgentsGuideKeydown);
  window.removeEventListener('resize', onAgentsGuideResize);
}

function removeAgentsGuide(): void {
  cancelAgentsGuideLayout();
  unbindAgentsGuideWindowEvents();
  document.querySelector('[data-agents-guide-portal]')?.remove();
  clearAgentsGuideTarget();
}

function finishAgentsGuide(): void {
  agentsGuideMode = 'hidden';
  saveToStorage(STORAGE_KEYS.externalAgentsGuideDismissed, true);
  render();
}

function setAgentsGuideStep(step: AgentsGuideStepNumber): void {
  agentsGuideMode = 'tour';
  agentsGuideStep = step;
  activeTab = step === 1 ? 'runtime' : step === 2 ? 'create-agent' : 'mine';
  activeTeamId = '';
  message = '';
  render();
}

function renderAgentsGuide(): void {
  if (agentsGuideMode === 'hidden') {
    removeAgentsGuide();
    return;
  }
  const root = $('#agents-page-root');
  const pane = root?.closest('.tab-pane');
  if (pane && !pane.classList.contains('active')) {
    removeAgentsGuide();
    return;
  }

  const target = agentsGuideMode === 'tour' ? guideTarget() : null;
  let portal = document.querySelector<HTMLElement>('[data-agents-guide-portal]');
  if (!portal) {
    portal = document.createElement('div');
    portal.dataset.agentsGuidePortal = '';
    document.body.appendChild(portal);
  }
  portal.dataset.guideMode = agentsGuideMode;

  if (agentsGuideMode === 'welcome') {
    cancelAgentsGuideLayout();
    unbindAgentsGuideWindowEvents();
    clearAgentsGuideTarget();
    portal.className = 'agents-guide-portal agents-guide-portal--right';
    portal.innerHTML = `
      <aside class="agents-guide-bubble mw-tour-card" data-agents-guide role="dialog" aria-label="外援中心新手引导">
        <div class="mw-tour-card__top"><span class="mw-tour-card__spark" aria-hidden="true"></span><span>外援小向导</span></div>
        <strong>第一次来外援中心？</strong>
        <p>用 30 秒认识发现、添加和派活。</p>
        <div class="mw-tour-card__actions mw-tour-card__actions--welcome">
          <button type="button" class="mw-tour-card__secondary" data-agents-guide-skip>稍后再说</button>
          <button type="button" class="mw-tour-card__primary" data-agents-guide-start>开始看看</button>
        </div>
      </aside>`;
    portal.querySelector<HTMLElement>('[data-agents-guide-skip]')?.addEventListener(
      'click',
      finishAgentsGuide,
    );
    portal.querySelector<HTMLElement>('[data-agents-guide-start]')?.addEventListener(
      'click',
      () => setAgentsGuideStep(1),
    );
    return;
  }

  const isExistingTour = portal.classList.contains('agents-guide-portal--tour');
  portal.className = 'agents-guide-portal agents-guide-portal--tour';
  if (!isExistingTour) {
    portal.innerHTML = `
      <div class="agents-guide-mask" data-agents-guide-mask></div>
      <div class="agents-guide-highlight" data-agents-guide-highlight hidden></div>
      <aside class="agents-guide-bubble mw-tour-card" data-agents-guide role="dialog" aria-label="外援中心引导：${target?.progress}">
        <div class="mw-tour-card__top"><span class="mw-tour-card__spark" aria-hidden="true"></span><span data-agents-guide-progress></span></div>
        <strong data-agents-guide-title></strong>
        <p data-agents-guide-body></p>
        <div class="mw-tour-card__actions">
          <button type="button" class="mw-tour-card__quiet" data-agents-guide-skip>跳过</button>
          <div class="mw-tour-card__steps">
            <button type="button" class="mw-tour-card__secondary" data-agents-guide-previous>上一步</button>
            <button type="button" class="mw-tour-card__primary" data-agents-guide-next></button>
          </div>
        </div>
      </aside>`;
    portal.querySelector<HTMLElement>('[data-agents-guide-skip]')?.addEventListener(
      'click',
      finishAgentsGuide,
    );
    portal.querySelector<HTMLElement>('[data-agents-guide-previous]')?.addEventListener('click', () => {
      if (agentsGuideStep > 1) {
        setAgentsGuideStep((agentsGuideStep - 1) as AgentsGuideStepNumber);
      }
    });
    portal.querySelector<HTMLElement>('[data-agents-guide-next]')?.addEventListener('click', () => {
      if (agentsGuideStep === 3) finishAgentsGuide();
      else setAgentsGuideStep((agentsGuideStep + 1) as AgentsGuideStepNumber);
    });
    portal.addEventListener('wheel', (event) => {
      const results = root?.querySelector<HTMLElement>('.mw-hub-template__results');
      if (!results) return;
      event.preventDefault();
      results.scrollBy({ top: event.deltaY, behavior: 'auto' });
      scheduleAgentsGuideLayout();
    }, { passive: false });
  }

  const bubble = portal.querySelector<HTMLElement>('[data-agents-guide]');
  if (bubble) bubble.setAttribute('aria-label', `外援中心引导：${target?.progress}`);
  const progress = portal.querySelector<HTMLElement>('[data-agents-guide-progress]');
  const title = portal.querySelector<HTMLElement>('[data-agents-guide-title]');
  const body = portal.querySelector<HTMLElement>('[data-agents-guide-body]');
  const previous = portal.querySelector<HTMLButtonElement>('[data-agents-guide-previous]');
  const next = portal.querySelector<HTMLButtonElement>('[data-agents-guide-next]');
  if (progress) progress.textContent = target?.progress || '';
  if (title) title.textContent = target?.title || '';
  if (body) body.textContent = target?.body || '';
  if (previous) previous.hidden = agentsGuideStep === 1;
  if (next) next.textContent = agentsGuideStep === 3 ? '完成' : '下一步';

  clearAgentsGuideTarget();
  if (target) document.querySelector<HTMLElement>(target.selector)?.classList.add('agents-guide-target');
  bindAgentsGuideWindowEvents();
  scheduleAgentsGuideLayout();
}

function render(): void {
  closeAgentsSelect();
  const root = $('#agents-page-root');
  if (!root) return;
  if (agentHubView && !agentHubView.element.isConnected) {
    agentHubView.dispose();
    agentHubView = null;
  }
  if (!agentHubView) {
    agentHubView = createAgentHubView({
      state: agentHubState(),
      onTabChange: (tab) => selectAgentHubTab(tab),
      onUseAgent: (id) => {
        const agent = agents.find((item) => item.id === id);
        if (agent) void useAgent(agent);
      },
      onDeleteAgent: (id) => {
        const agent = agents.find((item) => item.id === id);
        if (agent) void deleteAgent(agent);
      },
      onUseTeam: (id) => {
        const team = teams.find((item) => item.id === id);
        if (team) void useTeam(team);
      },
      onDeleteTeam: (id) => {
        const team = teams.find((item) => item.id === id);
        if (team) void deleteTeam(team);
      },
      onUseRuntime: selectRuntime,
      onDeleteRuntime: (id) => {
        const runtime = runtimes.find((item) => item.id === id);
        if (runtime) void deleteRuntime(runtime);
      },
      onScanRuntimes: () => void scanRuntimes(),
      onOpenGuide: () => setAgentsGuideStep(1),
    });
    root.replaceChildren(agentHubView.element);
  } else {
    agentHubView.update(agentHubState());
  }
  root.querySelector('[data-team-detail-backdrop]')?.remove();
  if (activeTeamId) root.insertAdjacentHTML('beforeend', renderTeamDetail());
  renderAgentsGuide();
  bind();
}

function bind(): void {
  $$('[data-team-id]').forEach((card) => {
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    const open = (event: Event): void => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('[data-use-team], [data-delete-team]')) return;
      activeTeamId = card.getAttribute('data-team-id') || '';
      render();
    };
    card.addEventListener('click', open);
    card.addEventListener('keydown', (event) => {
      const key = (event as KeyboardEvent).key;
      if (key !== 'Enter' && key !== ' ') return;
      event.preventDefault();
      open(event);
    });
  });
  $('[data-team-detail-close]')?.addEventListener('click', () => {
    activeTeamId = '';
    render();
  });
  $('[data-team-detail-backdrop]')?.addEventListener('click', (event) => {
    if (event.target !== event.currentTarget) return;
    activeTeamId = '';
    render();
  });
  $$('[data-agents-select-key]').forEach((trigger) => {
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      if ((trigger as HTMLButtonElement).disabled) return;
      if (agentsSelectPopover && trigger.classList.contains('is-open')) closeAgentsSelect();
      else openAgentsSelect(trigger);
    });
  });
  $('#agents-name-input')?.addEventListener('input', (event) => {
    agentName = (event.target as HTMLInputElement).value;
  });
  $('[data-create-agent]')?.addEventListener('click', () => {
    void createAgent();
  });
  bindTeamForm();
  bindDecisionModal();
}

function bindTeamForm(): void {
  const teamNameInput = $('#team-name-input') as HTMLInputElement | null;
  teamNameInput?.addEventListener('compositionstart', () => {
    teamNameComposing = true;
    cancelDescriptionDraftRequest();
  });
  teamNameInput?.addEventListener('compositionend', (event) => {
    teamNameComposing = false;
    teamName = (event.target as HTMLInputElement).value;
    invalidateFormationDecision();
    scheduleTeamDescriptionDraft();
  });
  teamNameInput?.addEventListener('input', (event) => {
    teamName = (event.target as HTMLInputElement).value;
    invalidateFormationDecision();
    if (!teamNameComposing && !(event as InputEvent).isComposing) scheduleTeamDescriptionDraft();
  });
  $('#team-description-input')?.addEventListener('input', (event) => {
    cancelDescriptionDraftRequest();
    descriptionDraftMeta = null;
    teamDescription = (event.target as HTMLTextAreaElement).value;
    generatedTeamDescription = '';
    lastDescriptionDraftName = teamName.trim();
    invalidateFormationDecision();
    syncSuggestTeamButton();
  });
  $('#team-workflow-input')?.addEventListener('input', (event) => {
    teamWorkflow = (event.target as HTMLTextAreaElement).value;
  });
  $('[data-toggle-team-constraints]')?.addEventListener('click', () => {
    showTeamConstraints = !showTeamConstraints;
    render();
  });
  $$('[data-remove-required-agent]').forEach((button) => {
    button.addEventListener('click', () => {
      const value = button.getAttribute('data-remove-required-agent') || '';
      requiredTeamAgentIds = requiredTeamAgentIds.filter((id) => id !== value);
      teamRolesLocked = false;
      render();
    });
  });
  $$('[data-remove-excluded-agent]').forEach((button) => {
    button.addEventListener('click', () => {
      const value = button.getAttribute('data-remove-excluded-agent') || '';
      excludedTeamAgentIds = excludedTeamAgentIds.filter((id) => id !== value);
      teamRolesLocked = false;
      render();
    });
  });
  $$('[data-required-capability]').forEach((button) => {
    button.addEventListener('click', () => {
      const value = button.getAttribute('data-required-capability') || '';
      toggleListValue(requiredTeamCapabilities, value, !requiredTeamCapabilities.includes(value));
      teamRolesLocked = false;
      render();
    });
  });
  $('[data-toggle-custom-capability]')?.addEventListener('click', () => {
    showCustomCapabilityInput = !showCustomCapabilityInput;
    render();
  });
  $('#team-custom-capability-input')?.addEventListener('input', (event) => {
    customTeamCapabilityInput = (event.target as HTMLInputElement).value;
    const addButton = $('[data-add-custom-capability]') as HTMLButtonElement | null;
    if (addButton) addButton.disabled = !customTeamCapabilityInput.trim();
  });
  $('#team-custom-capability-input')?.addEventListener('keydown', (event) => {
    if ((event as KeyboardEvent).key === 'Enter') {
      event.preventDefault();
      addCustomCapability();
    }
  });
  $('[data-add-custom-capability]')?.addEventListener('click', () => {
    addCustomCapability();
  });
  $$('[data-remove-custom-capability]').forEach((button) => {
    button.addEventListener('click', () => {
      const value = button.getAttribute('data-remove-custom-capability') || '';
      customTeamCapabilities = customTeamCapabilities.filter((item) => item !== value);
      teamRolesLocked = false;
      render();
    });
  });
  $$('[data-member-role]').forEach((textarea) => {
    textarea.addEventListener('input', () => {
      const id = textarea.getAttribute('data-member-role') || '';
      memberRoles[id] = (textarea as HTMLTextAreaElement).value;
    });
  });
  $$('[data-remove-member]').forEach((button) => {
    button.addEventListener('click', () => {
      removeMember(button.getAttribute('data-remove-member') || '');
    });
  });
  $('[data-suggest-team]')?.addEventListener('click', () => {
    void requestTeamSuggestion(requiredTeamAgentIds);
  });
  $('[data-recheck-formation]')?.addEventListener('click', () => {
    void requestTeamSuggestion(requiredTeamAgentIds);
  });
  $('[data-return-team-setup]')?.addEventListener('click', () => {
    invalidateFormationDecision();
    render();
  });
  $('[data-create-team]')?.addEventListener('click', () => {
    void createTeam();
  });
  $('[data-reset-team]')?.addEventListener('click', () => {
    resetTeamForm();
    render();
  });
}

function bindDecisionModal(): void {
  $('[data-team-decision-close]')?.addEventListener('click', () => {
    teamConstraintDecision = [];
    render();
  });
  $('[data-team-decision-edit]')?.addEventListener('click', () => {
    teamConstraintDecision = [];
    render();
  });
  $('[data-team-decision-filter]')?.addEventListener('click', () => {
    const conflictedIds = new Set(teamConstraintDecision.map((item) => item.agent_id));
    teamConstraintDecision = [];
    requiredTeamAgentIds = requiredTeamAgentIds.filter((id) => !conflictedIds.has(id));
    void requestTeamSuggestion(requiredTeamAgentIds);
  });
  $('[data-team-decision-force]')?.addEventListener('click', () => {
    const forceIds = teamConstraintDecision.map((item) => item.agent_id);
    teamConstraintDecision = [];
    void requestTeamSuggestion(requiredTeamAgentIds, forceIds);
  });
}

function toggleListValue(list: string[], value: string, enabled: boolean): void {
  if (!value) return;
  if (enabled) {
    if (!list.includes(value)) list.push(value);
    return;
  }
  const idx = list.indexOf(value);
  if (idx >= 0) list.splice(idx, 1);
}

function addCustomCapability(): void {
  const capability = customTeamCapabilityInput.trim();
  if (!capability) return;
  if (!customTeamCapabilities.some((item) => item.toLowerCase() === capability.toLowerCase())) {
    customTeamCapabilities.push(capability);
  }
  customTeamCapabilityInput = '';
  teamRolesLocked = false;
  render();
}

function changeLeader(agentId: string): void {
  teamLeaderId = agentId || CREW_BUILTIN_AGENT_ID;
  requiredTeamAgentIds = requiredTeamAgentIds.filter((id) => id !== teamLeaderId);
  excludedTeamAgentIds = excludedTeamAgentIds.filter((id) => id !== teamLeaderId);
  selectedMembers[teamLeaderId] = true;
  memberRoles[teamLeaderId] = memberRoles[teamLeaderId] || defaultLeaderRole;
  memberRoleKeys[teamLeaderId] = memberRoleKeys[teamLeaderId] || 'project_manager';
  const meta = roleMetaForKey('project_manager');
  if (meta) memberRoleMeta[teamLeaderId] = meta;
  teamRolesLocked = false;
  render();
}

function removeMember(agentId: string): void {
  if (!agentId || agentId === teamLeaderId) return;
  selectedMembers[agentId] = false;
  delete memberRoles[agentId];
  delete memberRoleKeys[agentId];
  delete memberRoleMeta[agentId];
  teamRolesLocked = false;
  render();
}

function applyTeamSuggestion(suggestion: ExternalTeamSuggestion): void {
  const selected: Record<string, boolean> = {};
  const roles: Record<string, string> = {};
  const roleKeys: Record<string, string> = {};
  const roleMeta: Record<string, ExternalTeamRole> = {};
  suggestion.members.forEach((member) => {
    selected[member.agent_id] = true;
    roles[member.agent_id] = member.role || member.responsibility_markdown || defaultRoleFor(member.agent_id, member.role_key || 'member');
    if (member.role_key) roleKeys[member.agent_id] = member.role_key;
    if (member.role_key) {
      roleMeta[member.agent_id] = roleMetaForKey(member.role_key) || {
        key: member.role_key,
        label: member.role_label || member.role_key,
        description: '',
        capabilities: member.capabilities || member.assigned_capabilities || [],
        workflow_lane: member.workflow_lane || 'build',
      };
    }
  });
  if (suggestion.leader_agent_id) {
    teamLeaderId = suggestion.leader_agent_id;
    selected[teamLeaderId] = true;
    roles[teamLeaderId] ||= defaultLeaderRole;
    roleKeys[teamLeaderId] ||= 'project_manager';
  }
  selectedMembers = selected;
  memberRoles = roles;
  memberRoleKeys = roleKeys;
  memberRoleMeta = roleMeta;
  teamSpec = suggestion.team_spec || null;
  formationPlan = suggestion.formation_plan || null;
  teamWorkflow = suggestion.workflow || teamWorkflow;
  teamRolesLocked = true;
  message = suggestion.reasons?.join(' ') || '已根据团队目标和约束生成组队建议';
}

async function requestTeamSuggestion(requiredAgentIds: string[], forceRequiredAgentIds: string[] = []): Promise<void> {
  formationRequestAbort?.abort();
  const controller = new AbortController();
  formationRequestAbort = controller;
  const requestSeq = ++formationRequestSeq;
  const startedAt = Date.now();
  let fastApplied = false;
  busy = true;
  formationStatus = 'fast_loading';
  formationImprovements = [];
  formationAiAttempted = false;
  startFormationElapsedTimer(startedAt);
  message = '';
  render();
  try {
    const constraints = buildTeamConstraintText();
    const description = [teamDescription.trim(), constraints && `组队约束：\n${constraints}`].filter(Boolean).join('\n\n');
    const request = {
      name: teamName.trim(),
      description,
      workflow: teamWorkflow.trim(),
      leader_agent_id: teamLeaderId,
      required_agent_ids: requiredAgentIds,
      excluded_agent_ids: excludedTeamAgentIds,
      force_required_agent_ids: forceRequiredAgentIds,
      required_capabilities: requiredTeamCapabilities,
      custom_capabilities: customTeamCapabilities,
    };
    const suggestion = await backendApi.suggestExternalTeamAuto(request, {
      signal: controller.signal,
      onSuggestion: (snapshot, phase) => {
        if (requestSeq !== formationRequestSeq) return;
        const conflicts = snapshot.required_agent_conflicts || [];
        if (snapshot.decision_required && conflicts.length) {
          teamConstraintDecision = conflicts;
          if (phase === 'final') formationStatus = 'idle';
          return;
        }
        teamConstraintDecision = [];
        applyTeamSuggestion(snapshot);
        fastApplied = true;
        if (phase === 'final') {
          formationStatus = resolveFormationUiStatus(snapshot);
          formationImprovements = snapshot.ai_material_improvements || [];
        }
        render();
      },
      onStatus: () => {
        if (requestSeq !== formationRequestSeq) return;
        formationStatus = 'ai_reviewing';
        formationAiAttempted = true;
        message = '';
        render();
      },
    });
    if (requestSeq !== formationRequestSeq) return;
    const conflicts = suggestion.required_agent_conflicts || [];
    if (suggestion.decision_required && conflicts.length) {
      teamConstraintDecision = conflicts;
      formationStatus = 'idle';
      return;
    }
    teamConstraintDecision = [];
    applyTeamSuggestion(suggestion);
    formationStatus = resolveFormationUiStatus(suggestion);
    formationImprovements = suggestion.ai_material_improvements || [];
  } catch (error) {
    if (requestSeq !== formationRequestSeq || (error as Error).name === 'AbortError') return;
    formationStatus = fastApplied ? 'ready_partial' : 'idle';
    message = fastApplied
      ? '初步团队方案已保留，智能检查暂未完成'
      : `智能组队失败：${(error as Error).message}`;
  } finally {
    if (requestSeq === formationRequestSeq) {
      formationRequestAbort = null;
      formationElapsedMs = Date.now() - startedAt;
      stopFormationElapsedTimer();
      busy = false;
      render();
    }
  }
}

async function scanRuntimes(): Promise<void> {
  const startedAt = performance.now();
  busy = true;
  runtimeScanning = true;
  message = '正在探测本机运行时与模型，请稍候…';
  render();
  try {
    runtimes = await backendApi.scanRuntimes();
    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
    message = `已刷新 ${runtimes.length} 个运行时，耗时 ${elapsed} 秒`;
  } catch (error) {
    message = `刷新运行时失败：${(error as Error).message}`;
  } finally {
    runtimeScanning = false;
    busy = false;
    render();
  }
}

async function deleteRuntime(runtime: ExternalRuntime): Promise<void> {
  const confirmed = await showConfirmDialog({
    title: '删除运行时记录',
    message: `删除“${runtime.name || runtime.provider}”的发现记录？如果工具仍安装在电脑上，下次点“再找找”时它会重新出现。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  busy = true;
  message = '';
  render();
  try {
    await backendApi.deleteRuntime(runtime.id);
    runtimes = runtimes.filter((item) => item.id !== runtime.id);
    message = `已删除 ${runtime.name || runtime.provider}`;
  } catch (error) {
    message = `删除运行时失败：${(error as Error).message}`;
  } finally {
    busy = false;
    render();
  }
}

async function createAgent(): Promise<void> {
  if (!agentRuntimeId) {
    message = '请选择运行时';
    render();
    return;
  }
  if (!agentModel) {
    message = '请选择模型';
    render();
    return;
  }
  busy = true;
  message = '';
  render();
  try {
    const runtime = runtimes.find((item) => item.id === agentRuntimeId);
    const payload: Parameters<typeof backendApi.createExternalAgent>[0] = {
      name: agentName.trim() || `${runtime?.name || '外部'}智能体`,
      runtime_id: agentRuntimeId,
    };
    payload.model = agentModel.trim();
    const created = await backendApi.createExternalAgent(payload);
    await refresh();
    agentRuntimeId = '';
    agentName = '';
    agentModel = '';
    activeTab = 'mine';
    message = `已添加外援 ${created.name}`;
  } catch (error) {
    message = `添加外援失败：${(error as Error).message}`;
  } finally {
    busy = false;
    render();
  }
}

async function createTeam(): Promise<void> {
  if (!teamLeaderId) {
    message = '请选择 Leader';
    render();
    return;
  }
  const members = selectedReviewAgents()
    .filter((agent) => (memberRoles[agent.id] || '').trim())
    .map((agent, index) => {
      const meta = memberRoleMeta[agent.id];
      const member: Parameters<typeof backendApi.createExternalTeam>[0]['members'][number] = {
        agent_id: agent.id,
        role: (memberRoles[agent.id] || defaultRoleFor(agent.id, memberRoleKeys[agent.id] || 'member')).trim(),
        sort_order: index,
      };
      if (memberRoleKeys[agent.id]) member.role_key = memberRoleKeys[agent.id];
      if (meta?.label) member.role_label = meta.label;
      if (meta?.capabilities) {
        member.capabilities = meta.capabilities;
        member.assigned_capabilities = meta.capabilities;
      }
      if (meta?.workflow_lane) member.workflow_lane = meta.workflow_lane;
      return member;
    });
  if (!members.some((member) => member.agent_id === teamLeaderId)) {
    message = 'Leader 需要加入团队并填写职责';
    render();
    return;
  }
  busy = true;
  message = '';
  render();
  try {
    const plan = formationPlan;
    const confirmedFormationPlan = plan ? {
      ...plan,
      leader_agent_id: teamLeaderId,
      members: members.map((member) => {
        const planned = plan.members.find((item) => item.agent_id === member.agent_id);
        return {
          agent_id: member.agent_id,
          role_key: member.role_key || planned?.role_key || '',
          role_label: member.role_label || planned?.role_label || '',
          assigned_capabilities: member.assigned_capabilities || planned?.assigned_capabilities || [],
          responsibility: planned?.responsibility || {},
          responsibility_markdown: member.role,
          selection_source: planned?.selection_source || 'user',
          locked: planned?.locked ?? true,
          selection_reason: planned?.selection_reason || '用户确认的团队成员。',
        };
      }),
    } : undefined;
    const payload: Parameters<typeof backendApi.createExternalTeam>[0] = {
      name: teamName.trim() || '我的团队',
      leader_agent_id: teamLeaderId,
      members,
    };
    if (teamDescription.trim()) payload.description = teamDescription.trim();
    if (teamWorkflow.trim()) {
      payload.instructions = teamWorkflow.trim();
      payload.workflow = teamWorkflow.trim();
    }
    if (teamSpec) payload.team_spec = teamSpec;
    if (confirmedFormationPlan) payload.formation_plan = confirmedFormationPlan;
    const created = await backendApi.createExternalTeam(payload);
    await refresh();
    resetTeamForm();
    activeTab = 'mine';
    message = `已创建团队 ${created.name}`;
  } catch (error) {
    message = `创建团队失败：${(error as Error).message}`;
  } finally {
    busy = false;
    render();
  }
}

function resetTeamForm(): void {
  invalidateFormationDecision();
  cancelDescriptionDraftRequest();
  teamNameComposing = false;
  teamName = '';
  teamDescription = '';
  generatedTeamDescription = '';
  lastDescriptionDraftName = '';
  lastDescriptionDraftKey = '';
  descriptionDraftMeta = null;
  teamLeaderId = CREW_BUILTIN_AGENT_ID;
  teamWorkflow = '';
  selectedMembers = {};
  memberRoles = {};
  memberRoleKeys = {};
  memberRoleMeta = {};
  teamSpec = null;
  formationPlan = null;
  teamRolesLocked = false;
  requiredTeamAgentIds = [];
  excludedTeamAgentIds = [];
  requiredTeamCapabilities = [];
  customTeamCapabilityInput = '';
  customTeamCapabilities = [];
  teamConstraintDecision = [];
  showTeamConstraints = false;
  showCustomCapabilityInput = false;
}

async function deleteAgent(agent: ExternalAgent): Promise<void> {
  if (!window.confirm(`删除智能体「${agent.name}」？`)) return;
  busy = true;
  message = '';
  render();
  try {
    await backendApi.deleteExternalAgent(agent.id);
    await refresh();
    message = `已删除 ${agent.name}`;
  } catch (error) {
    message = `删除失败：${(error as Error).message}`;
  } finally {
    busy = false;
    render();
  }
}

async function deleteTeam(team: ExternalTeam): Promise<void> {
  if (!window.confirm(`删除团队「${team.name}」？`)) return;
  busy = true;
  message = '';
  render();
  try {
    await backendApi.deleteExternalTeam(team.id);
    await refresh();
    message = `已删除团队 ${team.name}`;
  } catch (error) {
    message = `删除团队失败：${(error as Error).message}`;
  } finally {
    busy = false;
    render();
  }
}

export async function useAgent(agent: ExternalAgent): Promise<void> {
  if (!externalAgentsEnabled()) {
    notify(EXTERNAL_AGENTS_DISABLED_MESSAGE);
    return;
  }
  const sessionId = ensureChatSession?.() || state.activeSessionId || '';
  if (!sessionId) {
    notify('无法创建对话，请先登录后再选择智能体。');
    return;
  }
  try {
    await backendApi.setSessionAgentConfig(sessionId, {
      executor: 'external',
      external_agent_id: agent.id,
      external: { external_agent_id: agent.id },
    });
    setActiveExternalTeamForSession(sessionId, '');
    const modelLabel = externalModelLabel(agent);
    await loadSessionModel(sessionId);
    await refreshAllSessions();
    onSessionAgentAssigned?.(
      sessionId,
      {
        name: agent.name,
        provider: agent.provider,
        display_badge: agent.display_badge || '?',
        ...(modelLabel ? { model: modelLabel } : {}),
      },
      modelLabel,
      { kind: 'external_agent', id: agent.id },
    );
    state.mode = 'agent';
    notify(`已创建对话并切换为 ${agent.name}`);
  } catch (error) {
    notify(externalActionError('外援接活失败', error));
  }
}

export async function useTeam(team: ExternalTeam): Promise<void> {
  if (!externalAgentsEnabled()) {
    notify(EXTERNAL_AGENTS_DISABLED_MESSAGE);
    return;
  }
  const sessionId = ensureChatSession?.() || state.activeSessionId || '';
  if (!sessionId) {
    notify('无法创建对话，请先登录后再选择团队。');
    return;
  }
  const previousTeamId = state.activeExternalTeamIdBySession[sessionId] || '';
  // 草稿创建后、后端绑定返回前就确定 Team 身份，避免 Composer 短暂露出默认模型。
  setActiveExternalTeamForSession(sessionId, team.id);
  syncSessionModelAvailabilityUi();
  try {
    await backendApi.setSessionAgentConfig(sessionId, {
      executor: 'team',
      team: { external_team_id: team.id },
    });
    await refreshAllSessions();
    onSessionAgentAssigned?.(
      sessionId,
      { name: team.name, provider: 'team', display_badge: team.display_badge || 'T' },
      '',
      { kind: 'external_team', id: team.id },
    );
    state.mode = 'team';
    syncSessionModelAvailabilityUi();
    notify(`已创建对话并切换为团队 ${team.name}`);
  } catch (error) {
    setActiveExternalTeamForSession(sessionId, previousTeamId);
    syncSessionModelAvailabilityUi();
    notify(externalActionError('切换团队失败', error));
  }
}

export async function loadAgentsPage(): Promise<void> {
  if (!externalAgentsEnabled()) return;
  busy = true;
  render();
  let shouldScanRuntimes = false;
  try {
    await refresh();
    shouldScanRuntimes = runtimes.length === 0 && !initialRuntimeScanStarted;
    if (shouldScanRuntimes) initialRuntimeScanStarted = true;
  } catch (error) {
    message = `外援阵容加载失败：${(error as Error).message}`;
  }
  busy = false;
  if (shouldScanRuntimes) {
    await scanRuntimes();
    return;
  }
  render();
}

export function renderAgentsPage(): void {
  render();
}

export function activateAgentsPage(): void {
  activeTab = 'mine';
  void loadAgentsPage();
}

export function disposeAgentsPage(): void {
  closeAgentsSelect();
  removeAgentsGuide();
  invalidateFormationDecision();
  cancelDescriptionDraftRequest();
  activeTab = 'mine';
  activeTeamId = '';
  agentHubView?.dispose();
  agentHubView = null;
}

export async function initAgentsPage(options: {
  ensureChatSession?: EnsureChatSessionFn;
  onSessionAgentAssigned?: SessionAgentAssignedFn;
} = {}): Promise<void> {
  ensureChatSession = options.ensureChatSession || null;
  onSessionAgentAssigned = options.onSessionAgentAssigned || null;
  initialRuntimeScanStarted = false;
  agentsGuideStep = 1;
  agentsGuideMode = loadFromStorage(STORAGE_KEYS.externalAgentsGuideDismissed, false)
    ? 'hidden'
    : 'welcome';
  if (!agentsSelectGlobalBound) {
    agentsSelectGlobalBound = true;
    document.addEventListener('mousedown', (event) => {
      const target = event.target as HTMLElement;
      if (!agentsSelectPopover || target.closest('.agents-select-popover') || target.closest('[data-agents-select-key]')) return;
      closeAgentsSelect();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeAgentsSelect();
    });
    document.addEventListener('click', (event) => {
      const tab = (event.target as HTMLElement).closest<HTMLElement>('[data-tab]');
      if (tab && tab.dataset.tab !== 'agents') removeAgentsGuide();
    });
  }
}
