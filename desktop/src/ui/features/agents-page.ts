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
import { classifyRoleAssessment, type RoleAssessment, type RoleRiskDecision } from './team-role-review';
import type { PageContribution, PageLifecycleContext } from './page-registry';

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
  '#### 工作边界',
  '- Crew 内置智能体负责调度和知识库访问；专业成员负责各自的分析与交付。',
  '- 信息不足、证据冲突或成员阻塞时，先标记待确认并协调处理。',
  '',
  '#### 团队协作关系',
  '- 向成员派发任务，收集结果后统一口径，必要时重新分配下一步。',
  '',
  '#### 输出格式',
  '- 当前成果：已经完成的结果。',
  '- 下一负责人：下一步由谁继续。',
  '- 下一动作：具体要做什么。',
  '- 风险/阻塞：缺少的信息、权限或依赖。',
  '',
  '#### 工作安排',
  '- 先确认目标、证据来源和验收标准，再拆分任务。',
  '- 汇总前检查来源、待确认项和成员交付状态。',
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
let teamDescriptionTouched = false;
let generatedTeamDescription = '';
let lastDescriptionDraftKey = '';
let descriptionDrafting = false;
let descriptionDraftStartedAt: number | null = null;
let descriptionDraftElapsedMs = 0;
let descriptionDraftMeta: { llmElapsedMs?: number; cacheHit?: boolean } | null = null;
let originalTeamDescription = '';
let goalOrganizeNotice = '';
let descriptionDraftSeq = 0;
let descriptionDraftAbort: AbortController | null = null;
let descriptionDraftTimer: number | null = null;
let descriptionDraftElapsedTimer: number | null = null;
let teamNameComposing = false;
let teamLeaderId = CREW_BUILTIN_AGENT_ID;
let teamWorkflow = '';
let workflowEditedByUser = false;
let workflowNeedsReview = false;
let workflowDetailsOpen = false;
let selectedMembers: Record<string, boolean> = {};
let memberRoles: Record<string, string> = {};
let memberRoleEdits: Record<string, string> = {};
let memberRoleNeedsReview: Record<string, boolean> = {};
const expandedMemberRoles = new Set<string>();
let memberRoleKeys: Record<string, string> = {};
let memberRoleMeta: Record<string, ExternalTeamRole> = {};
let pendingRoleChanges: Record<string, string> = {};
let roleDutyMissions: Record<string, string> = {};
let suggestedUserDuties: Record<string, string> = {};
let priorUserDuties: Record<string, string> = {};
let pendingRoleAssessment: RoleAssessment | null = null;
let roleRiskDecisions: Record<string, RoleRiskDecision> = {};
let memberCapabilityChecks: Record<string, { available: boolean; covered: string[]; missing: string[]; unknown: string[]; unavailable: string[] }> = {};
let userResponsibilities: Record<string, string> = {};
let userRoleKeys: Record<string, string> = {};
let removedSuggestionIds = new Set<string>();
let allowSystemFill = true;
let runtimeAllowSuggestions = true;
let runtimeAllowAutoReassign = true;
let formationEditRevision = 0;
let formationNeedsReview = false;
let teamSpec: Record<string, unknown> | null = null;
let formationPlan: FormationPlan | null = null;
let formationResultKey = '';
let formationStatus: FormationUiStatus = 'idle';
let formationElapsedMs = 0;
let formationImprovements: string[] = [];
let formationAiAttempted = false;
let formationFallbackReason = '';
let assessmentRunning = false;
let assessmentFeedback: { kind: 'success' | 'error'; text: string } | null = null;
let formationRequestSeq = 0;
let formationRequestAbort: AbortController | null = null;
let formationElapsedTimer: number | null = null;
let teamRolesLocked = false;
let formationPreviewOnly = false;
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
let agentHubView: AgentHubView | null = null;
let agentsGuideMode: AgentsGuideMode = 'hidden';
let agentsGuideStep: AgentsGuideStepNumber = 1;
let agentsGuideLayoutFrame: number | null = null;
let initialRuntimeScanStarted = false;

// ── 页面生命周期状态 ──
let agentsLifecycleSignal: AbortSignal | null = null;
let agentsLifecycleEpoch = 0;
let agentsGlobalEventsDispose: (() => void) | null = null;

function agentsLifecycleAvailable(): boolean {
  return !agentsLifecycleSignal || !agentsLifecycleSignal.aborted;
}

function renderIfActive(epoch: number): void {
  if (epoch !== agentsLifecycleEpoch) return;
  render();
}

function bindAgentsGlobalEvents(signal: AbortSignal): () => void {
  const onMouseDown = (event: MouseEvent): void => {
    const target = event.target as HTMLElement;
    if (!agentsSelectPopover || target.closest('.agents-select-popover') || target.closest('[data-agents-select-key]')) return;
    closeAgentsSelect();
  };
  const onKeyDown = (event: KeyboardEvent): void => {
    if (event.key === 'Escape') closeAgentsSelect();
  };
  const onClick = (event: MouseEvent): void => {
    const tab = (event.target as HTMLElement).closest<HTMLElement>('[data-tab]');
    if (tab && tab.dataset.tab !== 'agents') removeAgentsGuide();
  };

  document.addEventListener('mousedown', onMouseDown, { signal });
  document.addEventListener('keydown', onKeyDown, { signal });
  document.addEventListener('click', onClick, { signal });
  return () => {
    document.removeEventListener('mousedown', onMouseDown);
    document.removeEventListener('keydown', onKeyDown);
    document.removeEventListener('click', onClick);
  };
}

function resetAgentsNavigationState(): void {
  activeTab = 'mine';
  activeTeamId = '';
  message = '';
}

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
  formationFallbackReason = '';
  assessmentRunning = false;
  assessmentFeedback = null;
  teamRolesLocked = false;
  formationPreviewOnly = false;
  busy = false;
}

function formationDraftKey(): string {
  return JSON.stringify({
    name: teamName.trim(), description: teamDescription.trim(), leader: teamLeaderId,
    required: [...requiredTeamAgentIds].sort(), excluded: [...excludedTeamAgentIds].sort(),
    capabilities: [...requiredTeamCapabilities].sort(), custom: [...customTeamCapabilities].sort(),
    allowSystemFill, responsibilities: Object.entries(userResponsibilities).sort(),
    roles: Object.entries(userRoleKeys).sort(), removed: [...removedSuggestionIds].sort(),
  });
}

function resolveFormationUiStatus(suggestion: ExternalTeamSuggestion): FormationUiStatus {
  if (selectedReviewAgents().some((agent) => effectiveMemberRoleKey(agent.id) === 'unclassified')) {
    return 'ready_partial';
  }
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
    removedSuggestionIds.delete(value);
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
    if (value === effectiveMemberRoleKey(agentId)) delete pendingRoleChanges[agentId];
    else if (value && value !== 'unclassified') pendingRoleChanges[agentId] = value;
    pendingRoleAssessment = null;
    roleRiskDecisions = {};
    formationEditRevision += 1;
    formationNeedsReview = true;
    memberCapabilityChecks = {};
    assessmentFeedback = null;
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
          ? pendingRoleChanges[key.slice('member-role:'.length)] || effectiveMemberRoleKey(key.slice('member-role:'.length))
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

function decideTeamDescriptionDraftRequest(): {
  shouldRequest: boolean;
  shouldInvalidate: boolean;
  draftKey: string;
} {
  const normalizedName = teamName.trim();
  const draftKey = normalizedName;
  const descriptionCanBeGenerated = !teamDescription.trim()
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
  const button = $('[data-suggest-team], [data-view-existing-team]') as HTMLButtonElement | null;
  if (button && !busy) {
    button.disabled = !teamName.trim();
    const reusable = Boolean(formationPlan && formationResultKey === formationDraftKey());
    button.textContent = reusable ? '查看已有阵容' : '智能组队';
  }
}

function teamDescriptionHelperText(): string {
  if (descriptionDrafting) {
    return teamDescription.trim() ? '正在整理目标，请稍候。' : '正在准备目标参考，可以直接继续组队。';
  }
  if (generatedTeamDescription) {
    return '已生成目标参考，采用后才会作为团队目标。';
  }
  return '';
}

function syncTeamDescriptionDraftUi(): void {
  const textarea = $('#team-description-input') as HTMLTextAreaElement | null;
  if (textarea) {
    const shouldUpdateValue = document.activeElement !== textarea;
    if (shouldUpdateValue && textarea.value !== teamDescription) textarea.value = teamDescription;
    textarea.classList.toggle('is-generating', descriptionDrafting);
    textarea.placeholder = descriptionDrafting && !teamDescription.trim()
      ? `正在生成目标参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}）…`
      : '目标：完成什么任务？\n交付物：需要得到什么结果？\n验收：怎样算完成？';
  }
  const helper = $('[data-team-description-helper]');
  if (helper) {
    helper.textContent = teamDescriptionHelperText();
    helper.toggleAttribute('hidden', !descriptionDrafting && !generatedTeamDescription && !descriptionDraftMeta);
  }
  const suggestion = $('[data-generated-description]');
  if (suggestion) {
    suggestion.toggleAttribute('hidden', !generatedTeamDescription || Boolean(teamDescription.trim()) || descriptionDrafting);
    const content = suggestion.querySelector('p');
    if (content) content.textContent = generatedTeamDescription;
  }
  const action = $('[data-organize-team-description]') as HTMLButtonElement | null;
  if (action) {
    const showAction = teamDescriptionTouched && Boolean(teamDescription.trim());
    action.hidden = !showAction;
    action.disabled = descriptionDrafting || !showAction;
    action.parentElement?.toggleAttribute('hidden', !showAction);
  }
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
    const epoch = agentsLifecycleEpoch;
    const signal = agentsLifecycleSignal;
    const name = teamName.trim();
    try {
      await backendApi.draftExternalTeamDescription(
        { name },
        {
          signal: controller.signal,
          onDescriptionDelta: (text) => {
            if (controller.signal.aborted || descriptionDraftSeq !== requestId || signal?.aborted || epoch !== agentsLifecycleEpoch) return;
            generatedTeamDescription = text;
            syncTeamDescriptionDraftUi();
          },
          onDraft: (draft, phase, meta) => {
            if (controller.signal.aborted || descriptionDraftSeq !== requestId || signal?.aborted || epoch !== agentsLifecycleEpoch) return;
            if (draft.description) {
              generatedTeamDescription = draft.description;
            }
            descriptionDraftMeta = phase === 'optimized' ? meta : null;
            syncTeamDescriptionDraftUi();
          },
        },
      );
    } catch {
      if (controller.signal.aborted || descriptionDraftSeq !== requestId || signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    } finally {
      if (descriptionDraftSeq === requestId && epoch === agentsLifecycleEpoch) {
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

function capabilityCheckNote(check: { covered: string[]; missing: string[]; unknown: string[]; unavailable: string[] } | undefined): string {
  if (!check) return '';
  return [
    check.covered.length ? `画像支持：${check.covered.map(teamCapabilityLabel).join('、')}` : '',
    check.unknown.length ? `待验证：${check.unknown.map(teamCapabilityLabel).join('、')}` : '',
    check.missing.length ? `画像不匹配：${check.missing.map(teamCapabilityLabel).join('、')}` : '',
    check.unavailable.length ? `运行时不可用：${check.unavailable.map(teamCapabilityLabel).join('、')}` : '',
  ].filter(Boolean).join('；');
}

function effectiveMemberRoleKey(agentId: string): string {
  return userRoleKeys[agentId] || memberRoleKeys[agentId] || (agentId === teamLeaderId ? 'project_manager' : 'unclassified');
}

function recommendRoleKey(agentId: string, mission = ''): string {
  const text = mission.toLowerCase();
  if (agentId === teamLeaderId) return 'project_manager';

  // Give task-specific signals priority so a generic word such as “分析”
  // does not hide a clearer review, research, delivery, or implementation duty.
  const signals: Array<[string, RegExp]> = [
    ['security_engineer', /安全|权限|隐私|风控/],
    ['independent_reviewer', /评审|审阅|复核|证据|来源|核验|审计|评分|判断/],
    ['research_analyst', /文献|调研|研究|检索|资料|综述|比较|分析|商业|市场|客户|获客|成本|盈利|收入|定价/],
    ['qa_engineer', /测试|回归|缺陷|质量|验证|验收/],
    ['product_manager', /产品|需求|用户价值|范围|方案/],
    ['technical_writer', /文档|材料|纪要|报告|写作|整理|总结/],
    ['ux_designer', /用户路径|交互|体验|信息架构/],
    ['ui_designer', /视觉|界面|组件|样式/],
    ['frontend_developer', /前端|网页|界面实现/],
    ['backend_developer', /后端|接口|数据库|服务端/],
    ['fullstack_developer', /全栈|端到端|功能开发|编码|实现/],
    ['devops_engineer', /部署|发布|构建|环境|持续集成/],
    ['desktop_developer', /桌面端|electron|mac|本地应用/],
    ['server_developer', /服务端|运行时|调度|并发/],
    ['platform_developer', /平台开发|ios|android|端侧/],
  ];
  const inferred = signals.find(([, pattern]) => pattern.test(text))?.[0];
  if (inferred && roleMetaForKey(inferred)) return inferred;

  const current = memberRoleKeys[agentId];
  return current && current !== 'unclassified' && roleMetaForKey(current)
    ? current
    : 'unclassified';
}

function defaultRoleFor(agentId: string, roleKey: string, mission = ''): string {
  if (agentId === teamLeaderId) {
    return mission.trim()
      ? defaultLeaderRole.replace('- 理解团队目标，分配成员工作，检查阶段成果，形成最终输出。', `- ${mission.trim()}`)
      : defaultLeaderRole;
  }
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
    `- ${mission.trim() || desc}`,
    `- 岗位侧重点：${meta?.capabilities?.map(teamCapabilityLabel).join('、') || desc}`,
    '',
    '#### 工作边界',
    '- 只处理本角色分配的任务；遇到信息不足、权限或执行阻塞时及时报告。',
    '',
    '#### 团队协作关系',
    '- 接收 Leader 分配的上下文与约束。',
    '- 完成后向 Leader 提交结果、产物和风险。',
    '',
    '#### 输出格式',
    '- 已完成内容',
    '- 关键依据',
    '- 风险/待确认',
    '- 下一步与负责人',
    '',
    '#### 工作安排',
    '- 启动前确认输入、输出和验收点。',
    '- 完成后自检，并向 Leader 交付结果与风险。',
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
            <span class="session__team-logo team-detail-modal__team-logo" aria-hidden="true"><i></i><i></i></span>
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
            ${team.formation_plan?.original_description ? `<details><summary>整理前的原文</summary><p class="team-modal__plain">${escapeHtml(team.formation_plan.original_description)}</p></details>` : ''}
          </section>` : ''}
        ${workflow ? `
          <section class="team-modal__section">
            <h3>团队工作流</h3>
            <div class="md-body chat-markdown team-modal__md">${renderMarkdownHtml(workflow)}</div>
          </section>` : ''}
        <section class="team-modal__section">
          <h3>成员调配设置</h3>
          <p class="team-modal__plain">创建时允许推荐其他成员：${team.formation_plan?.formation_allow_system_fill === false ? '否' : '是'}。</p>
          <label><input type="checkbox" data-team-policy="runtime_allow_suggestions" ${team.formation_plan?.runtime_allow_suggestions === false ? '' : 'checked'} /> 执行时推荐临时成员（需确认）</label>
          <label><input type="checkbox" data-team-policy="runtime_allow_auto_reassign" ${team.formation_plan?.runtime_allow_auto_reassign === false ? '' : 'checked'} /> 允许自动改派已有成员</label>
          <p class="team-modal__plain">设置用于之后新开始的任务，正在执行的任务保持原设置。</p>
        </section>
        <section class="team-modal__section">
          <h3>成员职责</h3>
          <div class="team-modal__members">
            ${members.map((member) => {
              const agent = teamAgentOptions().find((item) => item.id === member.agent_id);
              const isLeader = member.agent_id === team.leader_agent_id;
              const name = member.agent_name || agent?.name || member.agent_id;
              const roleMeta = [member.role_label, member.workflow_lane].filter(Boolean).join(' · ');
              const check = team.formation_plan?.member_capability_assessments?.find((item) => item.agent_id === member.agent_id);
              const originalDuty = team.formation_plan?.members.find((item) => item.agent_id === member.agent_id)?.original_user_responsibility;
              const capabilityNote = check?.available === false ? '运行时或模型不可用。' : capabilityCheckNote(check?.coverage);
              return `
                <article class="team-modal-member${isLeader ? ' is-leader' : ''}">
                  <div class="team-modal-member__head">
                    <span class="formation-member-badge">${escapeHtml(member.display_badge || agent?.display_badge || '?')}</span>
                    <div>
                      <strong>${escapeHtml(name)}${isLeader ? '<span class="pixel-flag" title="Leader" aria-label="Leader"></span>' : ''}</strong>
                      <p>${escapeHtml(agent?.provider || 'external')}${isLeader ? ' · Leader' : ''}</p>
                      ${roleMeta ? `<p>${escapeHtml(roleMeta)}</p>` : ''}
                      ${capabilityNote ? `<p>组队时检查：${escapeHtml(capabilityNote)}</p>` : ''}
                    </div>
                  </div>
                  <div class="md-body chat-markdown team-modal__md">${renderMarkdownHtml(member.role || '未填写职责')}</div>
                  ${originalDuty ? `<details><summary>查看调整前的用户职责</summary><p class="team-modal__plain">${escapeHtml(originalDuty)}</p></details>` : ''}
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

function optionalTeamConstraintCount(): number {
  return excludedTeamAgentIds.length + requiredTeamCapabilities.length + customTeamCapabilities.length;
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
  const placeholder = kind === 'required' ? '添加必须参与的成员' : '选择要排除的 Agent';
  return renderAgentsSelect(key, '', placeholder, agentsSelectOptions(key).length === 0);
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

function renderRequiredMemberAssignments(): string {
  if (!requiredTeamAgentIds.length) {
    return '<p class="team-member-assignments__empty">尚未指定成员</p>';
  }
  return requiredTeamAgentIds.map((agentId) => `
    <article class="team-member-assignment">
      <div class="team-member-assignment__head">
        <div><strong>${escapeHtml(agentNameById(agentId))}</strong><span>必选成员</span></div>
        <button type="button" data-remove-required-agent="${escapeHtml(agentId)}" aria-label="移除 ${escapeHtml(agentNameById(agentId))}">移除</button>
      </div>
      <label>
        <span>负责事项 <small>选填</small></span>
        <textarea data-required-responsibility="${escapeHtml(agentId)}" rows="2" placeholder="例如：核对 AI 功能与材料证据，区分已实现能力、计划和待验证结论。">${escapeHtml(userResponsibilities[agentId] || '')}</textarea>
      </label>
    </article>
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
          const state = capabilityCoverageState(capability);
          return `
            <span class="${state.className} is-capability" data-coverage-ack>
              ${state.label} · ${escapeHtml(teamCapabilityLabel(capability))}
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

function capabilityCoverageState(capability: string): { className: string; label: string } {
  const coverage = formationPlan?.coverage;
  if (formationNeedsReview) return { className: 'is-considered', label: '待重新评估' };
  if (coverage?.covered?.includes(capability)) return { className: 'is-applied', label: '画像支持' };
  if (coverage?.unavailable?.includes(capability)) return { className: 'is-missing', label: '运行时不可用' };
  if (coverage?.unverified?.includes(capability)) return { className: 'is-unverified', label: '待验证' };
  if (coverage?.uncovered?.includes(capability)) return { className: 'is-missing', label: '画像不匹配' };
  return { className: 'is-considered', label: '待评估' };
}

function renderCoverageChips(): string {
  if (formationPreviewOnly || !formationPlan) return '';
  if (formationNeedsReview) return `
    <div class="team-review-section team-review-section--pending">
      <span>成员或职责已调整，能力建议待重新检查。</span>
    </div>
  `;
  return `
    <div class="team-review-section" data-team-coverage>
      <div class="team-review-section__head">
        <div>
          <strong>组队能力建议</strong>
          <span>按现有画像和角色分工形成建议，实际执行前会再次检查。</span>
        </div>
      </div>
      <div class="team-coverage-chips">
        ${formationPlan.coverage.required.map((capability) => {
          const state = capabilityCoverageState(capability);
          return `<span class="${state.className}">${escapeHtml(state.label)} · ${escapeHtml(teamCapabilityLabel(capability))}</span>`;
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

function workflowReviewNote(): string {
  return workflowNeedsReview
    ? '成员分工已变化，已保留你编辑的说明，请检查是否需要调整。'
    : workflowEditedByUser
      ? '已保留你编辑的说明。'
      : '成员或角色变化时，系统会更新这份参考。';
}

function syncWorkflowForRoster(): void {
  if (workflowEditedByUser) {
    workflowNeedsReview = true;
  } else {
    const members = selectedReviewAgents();
    const lines = members.map((agent) => {
      const key = effectiveMemberRoleKey(agent.id);
      const role = roleMetaForKey(key)?.label || memberRoleMeta[agent.id]?.label || '成员';
      return `- ${agent.name}（${role}${agent.id === teamLeaderId ? '，Leader' : ''}）：根据确认的职责参与协作。`;
    });
    teamWorkflow = ['团队协作参考：', ...lines, '具体任务的参与成员、分工和依赖关系根据任务需要确定。'].join('\n');
    workflowNeedsReview = false;
    const input = $('#team-workflow-input') as HTMLTextAreaElement | null;
    if (input) input.value = teamWorkflow;
  }
  const note = $('[data-workflow-note]');
  if (note) note.textContent = workflowReviewNote();
}

function memberResponsibilitySummary(agentId: string): string {
  const content = (memberRoles[agentId] || '').trim();
  const section = content.match(/#{2,4}\s*职责\s*\n+\s*[-*]\s*([^\n]+)/);
  if (section) return section[1].trim();
  if (content && !content.startsWith('#')) return content.split('\n').find((line) => line.trim())?.trim() || content;
  const mission = formationPlan?.members.find((member) => member.agent_id === agentId)?.responsibility?.mission;
  return typeof mission === 'string' && mission.trim() ? mission.trim() : '职责待补充';
}

function renderRoleRisks(): string {
  if (!pendingRoleAssessment || !Object.keys(roleRiskDecisions).length) return '';
  return Object.entries(roleRiskDecisions).map(([id, decision]) => {
    const member = pendingRoleAssessment?.members.find((item) => item.agent_id === id);
    const role = roleMetaForKey(pendingRoleChanges[id] || '')?.label || '新角色';
    const details = member?.coverage.missing.map(teamCapabilityLabel).join('、') || '';
    const text = decision === 'unavailable'
      ? '该成员的运行时或模型不可用，请处理运行环境后重新检查，或恢复原角色。'
      : decision === 'accepted'
        ? '已确认继续使用该角色。'
        : `现有画像显示${details ? `“${details}”` : '部分能力'}不匹配；是否仍使用该角色？`;
    return `<div class="team-review-guidance" data-role-risk="${escapeHtml(id)}"><div><strong>${escapeHtml(agentNameById(id))} · ${escapeHtml(role)}</strong><p>${escapeHtml(text)}</p></div><div>
      ${decision === 'pending' ? `<button type="button" class="agent-btn team-create__pixel-button" data-accept-role-risk="${escapeHtml(id)}">仍使用这个角色</button>` : ''}
      <button type="button" class="agent-btn team-create__pixel-button" data-revert-role-change="${escapeHtml(id)}">恢复原角色</button>
    </div></div>`;
  }).join('');
}

function renderManualRoleFallback(): string {
  if (!pendingRoleAssessment?.role_suggestion_error || pendingRoleAssessment.role_suggestion_error === 'member_unavailable') return '';
  const ids = Object.keys(pendingRoleChanges);
  const hasDuties = ids.length > 0 && ids.every((id) => (memberRoleEdits[id] ?? userResponsibilities[id] ?? '').trim());
  const risksResolved = Object.values(roleRiskDecisions).every((decision) => decision === 'accepted');
  return `<div class="team-review-guidance"><div><strong>也可以自行填写职责</strong><p>填写每位成员的职责并处理提示后，可保留原文使用新角色。</p></div><button type="button" class="agent-btn team-create__pixel-button" data-use-manual-role ${hasDuties && risksResolved ? '' : 'disabled'}>使用手写职责</button></div>`;
}

function renderMemberReview(): string {
  const reviewAgents = selectedReviewAgents();
  if (!reviewAgents.length) return '<div class="agents-empty">还没有成员。点击“智能组队”后会生成建议阵容。</div>';
  return `
    <div class="team-review-list">
      ${reviewAgents.map((agent) => {
        const roleKey = pendingRoleChanges[agent.id] || effectiveMemberRoleKey(agent.id);
        const roleLabel = roleMetaForKey(roleKey)?.label || memberRoleMeta[agent.id]?.label || '待补充';
        const check = memberCapabilityChecks[agent.id];
        const capabilityNote = check?.available === false ? '运行时或模型不可用。' : capabilityCheckNote(check) || (check ? '该角色没有可检查的标准能力项，暂无法判断。' : '');
        return `
          <article class="team-review-card${agent.id === teamLeaderId ? ' is-leader' : ''}">
            <div class="team-review-card__head">
              <strong>${escapeHtml(agent.name)}</strong>
              <span class="team-review-card__badge${agent.id === teamLeaderId ? ' is-leader' : ''}">${agent.id === teamLeaderId ? 'Leader' : escapeHtml(roleLabel)}</span>
              ${agent.id !== teamLeaderId ? `<button type="button" class="team-review-card__remove" data-remove-member="${escapeHtml(agent.id)}" aria-label="从团队移除 ${escapeHtml(agent.name)}">移除成员</button>` : ''}
            </div>
            <label>
              <span>角色${agent.id in userRoleKeys ? '' : roleKey === 'unclassified' ? '<small>等待推荐或手动选择</small>' : '<small>系统推荐，可改 · 能力另行核验</small>'}</span>
              ${renderAgentsSelect(`member-role:${agent.id}`, roleKey, roleLabel, rolePresets.length === 0)}
              ${pendingRoleChanges[agent.id] ? `<small>新角色待检查，原角色和职责暂未改变。 <button type="button" class="team-review-card__remove" data-revert-role-change="${escapeHtml(agent.id)}">恢复原角色</button></small>` : ''}
              ${roleKey === 'unclassified' ? '<small>AI 暂未推荐角色，可重试推荐，也可手动填写职责和角色。</small>' : ''}
              ${capabilityNote ? `<small data-member-capability-check>${escapeHtml(capabilityNote)}</small>` : ''}
            </label>
            <div class="team-review-card__responsibility">
              <span>执行职责</span>
              <p data-responsibility-summary="${escapeHtml(agent.id)}">${escapeHtml(memberResponsibilitySummary(agent.id))}</p>
              <details data-member-role-details="${escapeHtml(agent.id)}" ${expandedMemberRoles.has(agent.id) ? 'open' : ''}>
                <summary>查看并编辑完整职责</summary>
                <textarea aria-label="${escapeHtml(agent.name)}的执行职责" data-member-role="${escapeHtml(agent.id)}" rows="8">${escapeHtml(memberRoles[agent.id] || defaultRoleFor(agent.id, roleKey, userResponsibilities[agent.id] || ''))}</textarea>
              </details>
              <small data-member-role-review="${escapeHtml(agent.id)}" ${memberRoleNeedsReview[agent.id] ? '' : 'hidden'}>职责已修改，请检查角色是否仍然合适。</small>
              ${suggestedUserDuties[agent.id] ? `<details class="team-duty-suggestion"><summary>查看新角色的职责建议</summary><p>${escapeHtml(suggestedUserDuties[agent.id])}</p><button type="button" class="agent-btn team-create__pixel-button" data-apply-duty-suggestion="${escapeHtml(agent.id)}">采用建议</button></details>` : ''}
              ${agent.id in priorUserDuties ? `<button type="button" class="agent-btn team-create__pixel-button" data-restore-duty="${escapeHtml(agent.id)}">恢复原职责</button>` : ''}
            </div>
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
        <h2 id="agents-decision-title">指定成员暂时无法加入</h2>
        <p>请返回检查成员状态和组队要求，再重新组队。</p>
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
      : formationFallbackReason === 'provider_error'
        ? '模型调用未完成，已保留初步阵容，可以重试推荐。'
        : ['invalid_ai_output', 'incomplete_ai_output'].includes(formationFallbackReason)
          ? '模型未返回完整有效的成员职责，已保留初步阵容，可以重试推荐。'
          : formationFallbackReason === 'too_many_members'
            ? '本次需要推荐职责的成员超过 12 人，请减少成员后重试。'
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
          ? '<button class="agent-btn" type="button" data-retry-formation>重新推荐</button>'
          : ''}
      </div>
      <ol class="formation-progress__steps">
        <li class="${formationStatus === 'fast_loading' ? 'is-active' : 'is-done'}"><span class="formation-progress__icon" aria-hidden="true">${formationStatus === 'fast_loading' ? '·' : '✓'}</span><span>生成初步方案</span></li>
        <li class="${reviewStepClass}"><span class="formation-progress__icon" aria-hidden="true">${formationStatus === 'ready_partial' ? '!' : finished ? '✓' : '·'}</span><span>智能检查优化${formationStatus === 'ai_reviewing' || (finished && formationAiAttempted) ? ` · <b data-formation-elapsed>${escapeHtml(formatTeamDraftElapsed(formationElapsedMs))}</b>` : ''}</span></li>
        <li class="${finished ? 'is-done' : ''}"><span class="formation-progress__icon" aria-hidden="true">${finished ? '✓' : '·'}</span><span>方案已就绪</span></li>
      </ol>
      ${formationStatus === 'ready_improved' && formationImprovements.length
        ? `<div class="formation-progress__improvements">${formationImprovements.slice(0, 3).map((item) => `<span>✓ ${escapeHtml(item)}</span>`).join('')}</div>`
        : ''}
    </section>`;
}

function renderCreateTeam(): string {
  const coveredCount = formationPlan?.coverage?.covered?.length || 0;
  const requiredCount = formationPlan?.coverage?.required?.length || 0;
  const unverifiedCount = formationPlan?.coverage?.unverified?.length || 0;
  const reviewAgents = selectedReviewAgents();
  const unresolvedRoles = reviewAgents.filter((agent) => effectiveMemberRoleKey(agent.id) === 'unclassified');
  const reviewAgentIds = new Set(reviewAgents.map((agent) => agent.id));
  const warning = formationPreviewOnly ? '' : formationPlan?.warnings?.[0] || '';
  const constraintCount = optionalTeamConstraintCount();
  const canReviewExisting = Boolean(formationPlan && formationResultKey === formationDraftKey() && reviewAgents.length);
  return `
    <section class="agents-section agents-form team-create">
      <div class="team-create__heading">
        <span class="team-mark team-mark--hero team-create__team-mark" aria-hidden="true"><i></i><i></i></span>
        <div>
          <h2>${formationPreviewOnly ? '团队预览' : teamRolesLocked ? '确认阵容' : '创建团队'}</h2>
          <p>${formationPreviewOnly ? (formationPlan ? '先保留当前阵容；智能解析返回后自动更新角色和能力建议。' : '先展示你指定的成员；智能解析返回后自动更新阵容和职责。') : teamRolesLocked ? '检查成员和职责，满意后点击“确认并创建团队”。' : '按步骤填写，生成合适的团队方案。'}</p>
        </div>
        <div class="team-create__steps" aria-label="创建进度">
          <span class="${teamRolesLocked ? 'is-done' : 'is-active'}">设置任务</span>
          <span class="${teamRolesLocked ? 'is-active' : ''}">确认阵容</span>
        </div>
      </div>

      ${!teamRolesLocked ? `
        <div class="team-create__setup">
          <section class="team-create__step team-create__task-step" aria-label="步骤 1：描述团队任务">
            <div class="team-create__step-heading">
              <span class="team-create__step-number" aria-hidden="true">1</span>
              <strong>描述团队任务</strong>
            </div>
            <label>
              <span>团队名称 <em>必填</em></span>
              <input id="team-name-input" value="${escapeHtml(teamName)}" placeholder="例如：项目评审小队" maxlength="80" autofocus />
            </label>
            <label>
              <span>团队目标 <em>选填</em></span>
              <textarea id="team-description-input" class="${descriptionDrafting ? 'is-generating' : ''}" rows="3" placeholder="${descriptionDrafting && !teamDescription.trim() ? `正在生成目标参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}）…` : '目标：完成什么任务？&#10;交付物：需要得到什么结果？&#10;验收：怎样算完成？'}">${escapeHtml(teamDescription)}</textarea>
              <small data-team-description-helper aria-live="polite" ${descriptionDrafting || descriptionDraftMeta ? '' : 'hidden'}>${escapeHtml(teamDescriptionHelperText())}</small>
            </label>
            <div class="team-create__goal-suggestion" data-generated-description ${generatedTeamDescription && !teamDescription.trim() && !descriptionDrafting ? '' : 'hidden'}><div class="team-create__goal-suggestion-head"><span class="team-create__goal-suggestion-icon" aria-hidden="true">✦</span><div><strong>AI 目标参考</strong><small>根据团队名称生成，可修改后使用</small></div></div><p>${escapeHtml(generatedTeamDescription)}</p><button type="button" class="agent-btn team-create__pixel-button" data-apply-generated-description>采用此参考</button></div>
            <div class="team-create__goal-actions" ${teamDescriptionTouched && teamDescription.trim() ? '' : 'hidden'}>
              <button type="button" class="agent-btn team-create__pixel-button" data-organize-team-description ${teamDescriptionTouched && teamDescription.trim() ? '' : 'hidden'} ${descriptionDrafting || !teamDescriptionTouched || !teamDescription.trim() ? 'disabled' : ''}>✦ AI 帮我整理目标</button>
            </div>
            ${goalOrganizeNotice || originalTeamDescription ? `<div class="team-create__goal-feedback" role="status"><span>${escapeHtml(goalOrganizeNotice)}</span>${originalTeamDescription ? '<button type="button" class="agent-btn team-create__pixel-button" data-restore-original-description>恢复整理前的内容</button>' : ''}</div>` : ''}
          </section>
          <section class="team-member-assignments" aria-label="步骤 2：指定成员">
            <div class="team-member-assignments__head">
              <div class="team-create__step-title">
                <span class="team-create__step-number" aria-hidden="true">2</span>
                <strong>指定成员</strong>
                <span class="team-create__optional">选填</span>
              </div>
              <small>点名成员会固定入队；职责可留空。</small>
            </div>
            <div class="team-member-assignments__add">${renderConstraintSelect('required')}</div>
            <div class="team-member-assignments__list">${renderRequiredMemberAssignments()}</div>
          </section>
          <div class="agents-form__field team-create__leader" role="group" aria-label="步骤 3：选择 Leader">
            <div class="team-create__step-title">
              <span class="team-create__step-number" aria-hidden="true">3</span>
              <strong>选择 Leader</strong>
              <em>已默认推荐</em>
            </div>
            ${renderAgentsSelect('team-leader', teamLeaderId, '使用系统推荐 Leader')}
            <small>Crew 可调用 Wiki、拆分任务并汇总结果。</small>
          </div>
          <div class="team-constraints">
            <button class="team-constraints__toggle" type="button" aria-expanded="${showTeamConstraints ? 'true' : 'false'}" data-toggle-team-constraints>
              <span class="team-create__step-title team-constraints__title">
                <span class="team-create__step-number" aria-hidden="true">4</span>
                <strong>其他组队约束</strong>
                <span class="team-create__optional">选填</span>
              </span>
              <span class="team-constraints__toggle-meta">${constraintCount ? `已设置 ${constraintCount} 项` : '可跳过'}
                <svg class="team-constraints__chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
              </span>
            </button>
            ${showTeamConstraints ? `
              <div class="team-constraints__body">
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
                  ${customTeamCapabilities.length ? '<small class="team-capability-note">自定义文字会提供给智能检查；无法匹配标准能力的内容需要在成员职责中确认。</small>' : ''}
                </div>
                <label><input type="checkbox" data-allow-system-fill ${allowSystemFill ? 'checked' : ''} /> 组队时允许系统推荐其他成员</label>
                <label><input type="checkbox" data-runtime-suggestions ${runtimeAllowSuggestions ? 'checked' : ''} /> 执行遇到能力缺口时推荐临时成员（需确认）</label>
                <label><input type="checkbox" data-runtime-reassign ${runtimeAllowAutoReassign ? 'checked' : ''} /> 能力不足时允许自动改派给已有成员</label>
              </div>
            ` : ''}
          </div>
          <div class="team-create__primary-action">
            <button type="button" class="agent-btn team-create__pixel-button" data-cancel-team>取消组队</button>
            <button type="button" class="pixel-summon-btn" ${canReviewExisting ? 'data-view-existing-team' : 'data-suggest-team'} ${busy || !teamName.trim() ? 'disabled' : ''}>
              ${busy ? '正在生成方案…' : canReviewExisting ? '查看已有阵容' : '智能组队'}
            </button>
          </div>
          ${renderFormationProgress()}
        </div>
      ` : `
        <div class="team-create__review">
          ${!assessmentRunning && (busy || formationStatus === 'ready_partial') ? renderFormationProgress() : ''}
          <div class="team-review-next">
            <span>${assessmentRunning ? '正在检查当前阵容的能力证据…' : busy ? '正在完善组队建议，请稍候。' : teamDescription.trim() ? '检查成员和职责，确认后即可创建。' : '以下分工根据团队名称推测，请检查成员和职责。'}</span>
          </div>
          ${!busy && unresolvedRoles.length ? `<div class="team-review-guidance"><div><strong>以下成员的角色尚未确定：${unresolvedRoles.map((agent) => escapeHtml(agent.name)).join('、')}</strong><p>可以重试 AI 推荐，也可以手动设置；暂不设置仍可创建，后续应补充分工。</p></div><button type="button" class="agent-btn team-create__pixel-button" data-retry-roles>重试 AI 推荐</button></div>` : ''}
          ${!busy && reviewAgents.length === 1 ? '<div class="team-review-guidance"><div><strong>当前只有 Leader</strong><p>此阵容无法进行多成员协作；如需协作，请返回检查可用成员或组队设置。</p></div></div>' : ''}
          <div class="team-formation-summary">
            <div>
              <strong>${escapeHtml(teamName || '我的团队')}</strong>
              <p>${escapeHtml(teamDescription || '已准备初始阵容，可根据实际任务安排工作。')}</p>
            </div>
            <div class="team-formation-metrics" data-team-metrics>
              <span><b>${reviewAgents.length}</b> 名成员</span>
              ${formationPreviewOnly ? '<span data-coverage-metric>正在匹配角色与能力</span>' : formationNeedsReview ? '<span data-coverage-metric>分工已调整，能力建议待更新</span>' : `<span data-coverage-metric>${coveredCount}/${requiredCount} 项能力有画像支持</span>${unverifiedCount ? `<span>${unverifiedCount} 项待验证</span>` : ''}`}
              ${warning ? `<span data-formation-warning><b>!</b> ${escapeHtml(warning)}</span>` : ''}
            </div>
          </div>
          ${renderTeamConstraintAck(reviewAgentIds)}
          <div class="team-review-section team-review-section--members">
            <div class="team-review-section__head">
              <div>
                <strong>成员与职责</strong>
                <span>检查角色和职责，可按需调整。</span>
              </div>
              <em>${reviewAgents.length} 人</em>
            </div>
            <div class="team-review-guidance" data-review-guidance ${formationNeedsReview ? '' : 'hidden'}>
              <div><strong>${Object.keys(pendingRoleChanges).length ? '新角色待检查' : '分工已调整，建议重新检查能力'}</strong><p>${Object.keys(pendingRoleChanges).length ? '检查角色的能力证据并生成职责建议；已有手写职责会保留。工具实际可用性需另行确认。' : '可选：检查当前成员的能力证据，不改变阵容。'}</p></div>
              <button type="button" class="agent-btn team-create__pixel-button" data-recheck-formation ${busy ? 'disabled' : ''}>检查角色与职责</button>
            </div>
            ${renderRoleRisks()}
            ${renderManualRoleFallback()}
            ${assessmentFeedback ? `<div class="team-assessment-result${assessmentFeedback.kind === 'error' ? ' is-error' : ''}" data-assessment-result role="status" aria-live="polite"><strong>${assessmentFeedback.kind === 'success' ? '能力检查完成' : '能力检查未完成'}</strong><p>${escapeHtml(assessmentFeedback.text)}</p></div>` : ''}
            ${renderMemberReview()}
          </div>
          ${!formationNeedsReview && formationPlan && !formationPreviewOnly ? `<details class="team-review-more" data-coverage-details><summary>查看能力建议</summary>${renderCoverageChips()}</details>` : ''}
          <details class="team-workflow-preview" data-workflow-details ${workflowDetailsOpen ? 'open' : ''}>
            <summary>团队协作说明 <span>可查看和修改</span></summary>
            <div>
              <p>这是团队协作的参考说明；具体任务的分工和依赖关系在执行时确定。</p>
              <label for="team-workflow-input">协作说明</label>
              <textarea id="team-workflow-input" rows="5" placeholder="可填写团队协作方式。">${escapeHtml(teamWorkflow)}</textarea>
              <small data-workflow-note>${escapeHtml(workflowReviewNote())}</small>
            </div>
          </details>
          <div class="team-create__review-actions">
            <button type="button" class="agent-btn team-create__pixel-button" data-cancel-team>取消组队</button>
            <button type="button" class="agent-btn" data-return-team-setup>返回修改</button>
            <button type="button" class="agent-btn agent-btn--dark" data-create-team ${busy || !reviewAgents.length || Object.keys(pendingRoleChanges).length ? 'disabled' : ''}>确认并创建团队</button>
          </div>
        </div>
      `}
    </section>
  `;
}

function agentHubState(): AgentHubState {
  const formTab = activeTab;
  return {
    tab: activeTab,
    agents: agents.map((agent) => ({
      id: agent.id,
      name: agent.name || '未命名智能体',
      provider: providerLabel(agent.provider),
      ...(agent.display_badge ? { displayBadge: agent.display_badge } : {}),
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
    })),
    loading: busy && !runtimeScanning && activeTab !== 'create-team',
    scanning: runtimeScanning,
    message,
    featureEnabled: externalAgentsEnabled(),
    ...((formTab === 'create-agent' || formTab === 'create-team')
      ? {
          renderForm: (region: HTMLElement): void => {
            region.innerHTML = formTab === 'create-agent'
              ? renderCreateAgent()
              : `${renderCreateTeam()}${renderTeamConstraintDecision()}`;
          },
        }
      : {}),
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
  if (teamRolesLocked) {
    $$('[data-member-role-details]').forEach((details) => {
      const id = details.getAttribute('data-member-role-details') || '';
      if ((details as HTMLDetailsElement).open) expandedMemberRoles.add(id);
      else expandedMemberRoles.delete(id);
    });
  }
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
  $$('[data-team-policy]').forEach((input) => input.addEventListener('change', () => {
    const checkbox = input as HTMLInputElement;
    const key = checkbox.dataset.teamPolicy || '';
    const teamId = activeTeamId;
    checkbox.disabled = true;
    void backendApi.updateTeamStaffingPolicy(teamId, { [key]: checkbox.checked })
      .then(() => refresh())
      .then(() => render())
      .catch((error) => { checkbox.checked = !checkbox.checked; message = `调配设置保存失败：${(error as Error).message}`; render(); });
  }));
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
    generatedTeamDescription = '';
    goalOrganizeNotice = '';
    invalidateFormationDecision();
    if (!teamNameComposing && !(event as InputEvent).isComposing) scheduleTeamDescriptionDraft();
  });
  $('#team-description-input')?.addEventListener('input', (event) => {
    teamDescription = (event.target as HTMLTextAreaElement).value;
    teamDescriptionTouched = true;
    goalOrganizeNotice = '';
    generatedTeamDescription = '';
    cancelDescriptionDraftRequest();
    descriptionDraftMeta = null;
    invalidateFormationDecision();
    syncSuggestTeamButton();
    syncTeamDescriptionDraftUi();
  });
  $('[data-organize-team-description]')?.addEventListener('click', () => { void organizeTeamDescription(); });
  $('[data-apply-generated-description]')?.addEventListener('click', () => {
    if (!generatedTeamDescription || teamDescription.trim()) return;
    teamDescription = generatedTeamDescription;
    generatedTeamDescription = '';
    invalidateFormationDecision();
    render();
  });
  $('[data-restore-original-description]')?.addEventListener('click', () => {
    teamDescription = originalTeamDescription;
    originalTeamDescription = '';
    generatedTeamDescription = '';
    goalOrganizeNotice = '已恢复整理前的内容。';
    invalidateFormationDecision();
    render();
  });
  $('#team-workflow-input')?.addEventListener('input', (event) => {
    teamWorkflow = (event.target as HTMLTextAreaElement).value;
    workflowEditedByUser = true;
    workflowNeedsReview = false;
    const note = $('[data-workflow-note]');
    if (note) note.textContent = workflowReviewNote();
  });
  $('[data-workflow-details]')?.addEventListener('toggle', (event) => {
    workflowDetailsOpen = (event.target as HTMLDetailsElement).open;
  });
  $('[data-toggle-team-constraints]')?.addEventListener('click', () => {
    showTeamConstraints = !showTeamConstraints;
    render();
  });
  $$('[data-remove-required-agent]').forEach((button) => {
    button.addEventListener('click', () => {
      const value = button.getAttribute('data-remove-required-agent') || '';
      requiredTeamAgentIds = requiredTeamAgentIds.filter((id) => id !== value);
      delete userResponsibilities[value];
      delete userRoleKeys[value];
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
      memberRoleEdits[id] = memberRoles[id];
      memberRoleNeedsReview[id] = true;
      pendingRoleAssessment = null;
      roleRiskDecisions = {};
      const card = textarea.closest('.team-review-card');
      const roleNote = card?.querySelector<HTMLElement>('[data-member-role-review]');
      if (roleNote) roleNote.hidden = false;
      const summary = card?.querySelector<HTMLElement>('[data-responsibility-summary]');
      if (summary) summary.textContent = memberResponsibilitySummary(id);
      formationEditRevision += 1;
      formationNeedsReview = true;
      memberCapabilityChecks = {};
      assessmentFeedback = null;
      $('[data-assessment-result]')?.remove();
      $$('[data-member-capability-check]').forEach((item) => item.remove());
      syncWorkflowForRoster();
      const guidance = $('[data-review-guidance]');
      if (guidance) guidance.removeAttribute('hidden');
      const coverageDetails = $('[data-coverage-details]') as HTMLElement | null;
      if (coverageDetails) coverageDetails.hidden = true;
      const coverageMetric = $('[data-coverage-metric]');
      if (coverageMetric) coverageMetric.textContent = '分工已调整，能力建议待更新';
      const formationWarning = $('[data-formation-warning]');
      if (formationWarning) formationWarning.textContent = '能力建议待重新评估';
      $$('[data-coverage-ack]').forEach((item) => {
        item.textContent = (item.textContent || '').replace(/^(画像支持|待验证|画像不匹配|运行时不可用|已覆盖|未覆盖)/, '待重新评估');
        item.classList.remove('is-applied', 'is-missing', 'is-unverified');
        item.classList.add('is-considered');
      });
    });
  });
  $$('[data-member-role-details]').forEach((details) => {
    details.addEventListener('toggle', () => {
      if (!details.isConnected) return;
      const id = details.getAttribute('data-member-role-details') || '';
      if ((details as HTMLDetailsElement).open) expandedMemberRoles.add(id);
      else expandedMemberRoles.delete(id);
    });
  });
  $$('[data-revert-role-change]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.getAttribute('data-revert-role-change') || '';
      delete pendingRoleChanges[id];
      pendingRoleAssessment = null;
      roleRiskDecisions = {};
      assessmentFeedback = null;
      formationEditRevision += 1;
      render();
    });
  });
  $$('[data-accept-role-risk]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.getAttribute('data-accept-role-risk') || '';
      if (roleRiskDecisions[id] !== 'pending' || !pendingRoleAssessment) return;
      roleRiskDecisions[id] = 'accepted';
      if (!pendingRoleAssessment.role_suggestion_error && Object.values(roleRiskDecisions).every((value) => value === 'accepted')) {
        applyRoleAssessment(pendingRoleAssessment);
      }
      render();
    });
  });
  $('[data-use-manual-role]')?.addEventListener('click', () => {
    if (!pendingRoleAssessment || Object.values(roleRiskDecisions).some((decision) => decision !== 'accepted')) return;
    const suggestions = Object.entries(pendingRoleChanges).map(([id, roleKey]) => ({
      agent_id: id, role_key: roleKey, responsibility: (memberRoleEdits[id] ?? userResponsibilities[id] ?? '').trim(),
    }));
    if (!suggestions.length || suggestions.some((item) => !item.responsibility)) return;
    applyRoleAssessment({ ...pendingRoleAssessment, role_suggestions: suggestions });
    render();
  });
  $$('[data-apply-duty-suggestion]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.getAttribute('data-apply-duty-suggestion') || '';
      if (!suggestedUserDuties[id]) return;
      priorUserDuties[id] = memberRoles[id] || '';
      memberRoles[id] = suggestedUserDuties[id];
      memberRoleEdits[id] = memberRoles[id];
      delete suggestedUserDuties[id];
      formationEditRevision += 1;
      formationNeedsReview = true;
      memberCapabilityChecks = {};
      syncWorkflowForRoster();
      render();
    });
  });
  $$('[data-restore-duty]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.getAttribute('data-restore-duty') || '';
      if (!(id in priorUserDuties)) return;
      memberRoles[id] = priorUserDuties[id];
      memberRoleEdits[id] = memberRoles[id];
      delete priorUserDuties[id];
      formationEditRevision += 1;
      formationNeedsReview = true;
      memberCapabilityChecks = {};
      syncWorkflowForRoster();
      render();
    });
  });
  $$('[data-required-responsibility]').forEach((textarea) => {
    textarea.addEventListener('input', () => {
      const id = textarea.getAttribute('data-required-responsibility') || '';
      userResponsibilities[id] = (textarea as HTMLTextAreaElement).value;
    });
  });
  $('[data-allow-system-fill]')?.addEventListener('change', (event) => {
    allowSystemFill = (event.target as HTMLInputElement).checked;
  });
  $('[data-runtime-suggestions]')?.addEventListener('change', (event) => {
    runtimeAllowSuggestions = (event.target as HTMLInputElement).checked;
  });
  $('[data-runtime-reassign]')?.addEventListener('change', (event) => {
    runtimeAllowAutoReassign = (event.target as HTMLInputElement).checked;
  });
  $$('[data-remove-member]').forEach((button) => {
    button.addEventListener('click', () => {
      removeMember(button.getAttribute('data-remove-member') || '');
    });
  });
  $('[data-suggest-team]')?.addEventListener('click', () => {
    if (!teamRolesLocked && formationPlan && formationResultKey === formationDraftKey()) {
      teamRolesLocked = true;
      formationPreviewOnly = false;
      render();
    } else {
      void requestTeamSuggestion(requiredTeamAgentIds);
    }
  });
  $('[data-view-existing-team]')?.addEventListener('click', () => {
    if (formationPlan && formationResultKey === formationDraftKey()) {
      teamRolesLocked = true;
      formationPreviewOnly = false;
      render();
    } else {
      void requestTeamSuggestion(requiredTeamAgentIds);
    }
  });
  $('[data-cancel-team]')?.addEventListener('click', () => {
    resetTeamForm();
    message = '';
    activeTab = 'mine';
    render();
  });
  $$('[data-recheck-formation]').forEach((button) => {
    button.addEventListener('click', () => { void assessCurrentTeam(); });
  });
  $('[data-retry-formation]')?.addEventListener('click', () => { void requestTeamSuggestion(requiredTeamAgentIds, [], true); });
  $('[data-retry-roles]')?.addEventListener('click', () => { void requestTeamSuggestion(requiredTeamAgentIds, [], true); });
  $('[data-return-team-setup]')?.addEventListener('click', () => {
    invalidateFormationDecision();
    render();
  });
  $$('[data-create-team]').forEach((button) => {
    button.addEventListener('click', () => { void createTeam(); });
  });
  $('[data-reset-team]')?.addEventListener('click', () => {
    resetTeamForm();
    render();
  });
}

function bindDecisionModal(): void {
  $('[data-team-decision-close]')?.addEventListener('click', () => {
    teamConstraintDecision = [];
    teamRolesLocked = false;
    render();
  });
  $('[data-team-decision-edit]')?.addEventListener('click', () => {
    teamConstraintDecision = [];
    teamRolesLocked = false;
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

async function organizeTeamDescription(): Promise<void> {
  const source = teamDescription.trim();
  if (!source) return;
  if (!teamName.trim()) {
    goalOrganizeNotice = '请先填写团队名称，再整理目标。';
    render();
    return;
  }
  if (source.length > 4096) {
    goalOrganizeNotice = '整理目标最多支持 4096 个字符，请先精简后再试；原文仍可用于组队。';
    render();
    return;
  }
  cancelDescriptionDraftRequest();
  const controller = new AbortController();
  descriptionDraftAbort = controller;
  const requestId = ++descriptionDraftSeq;
  goalOrganizeNotice = '';
  setTeamDescriptionDrafting(true);
  render();
  try {
    let optimized = '';
    await backendApi.draftExternalTeamDescription({ name: teamName.trim(), description: source }, {
      signal: controller.signal,
      onDraft: (draft, phase) => { if (phase === 'optimized') optimized = draft.description; },
    });
    if (requestId !== descriptionDraftSeq || controller.signal.aborted) return;
    if (!optimized.trim()) {
      goalOrganizeNotice = 'AI 暂时无法整理目标，已保留原文，请重试。';
    } else if (optimized.trim() === source) {
      goalOrganizeNotice = '整理完成，内容与原文一致。';
    } else {
      originalTeamDescription = teamDescription;
      teamDescription = optimized.trim();
      generatedTeamDescription = '';
      goalOrganizeNotice = '已整理为目标条目，请核对后使用。';
      invalidateFormationDecision();
    }
  } catch {
    if (requestId === descriptionDraftSeq && !controller.signal.aborted) goalOrganizeNotice = '整理失败，已保留原文，请重试。';
  } finally {
    if (requestId === descriptionDraftSeq) {
      descriptionDraftAbort = null;
      setTeamDescriptionDrafting(false);
      render();
    }
  }
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
  delete memberRoleEdits[agentId];
  delete memberRoleNeedsReview[agentId];
  expandedMemberRoles.delete(agentId);
  delete memberRoleKeys[agentId];
  delete memberRoleMeta[agentId];
  delete pendingRoleChanges[agentId];
  delete roleDutyMissions[agentId];
  delete suggestedUserDuties[agentId];
  delete priorUserDuties[agentId];
  pendingRoleAssessment = null;
  roleRiskDecisions = {};
  delete userResponsibilities[agentId];
  delete userRoleKeys[agentId];
  requiredTeamAgentIds = requiredTeamAgentIds.filter((id) => id !== agentId);
  removedSuggestionIds.add(agentId);
  formationEditRevision += 1;
  formationNeedsReview = true;
  memberCapabilityChecks = {};
  assessmentFeedback = null;
  syncWorkflowForRoster();
  render();
}

function applyTeamSuggestion(suggestion: ExternalTeamSuggestion): void {
  formationFallbackReason = suggestion.fallback_reason || '';
  memberCapabilityChecks = {};
  assessmentFeedback = null;
  const selected: Record<string, boolean> = {};
  const roles: Record<string, string> = {};
  const roleKeys: Record<string, string> = {};
  const roleMeta: Record<string, ExternalTeamRole> = {};
  suggestion.members.forEach((member) => {
    if (removedSuggestionIds.has(member.agent_id)) return;
    selected[member.agent_id] = true;
    if (pendingRoleChanges[member.agent_id] && selectedMembers[member.agent_id]) {
      roles[member.agent_id] = memberRoles[member.agent_id] || '';
      roleKeys[member.agent_id] = effectiveMemberRoleKey(member.agent_id);
      if (memberRoleMeta[member.agent_id]) roleMeta[member.agent_id] = memberRoleMeta[member.agent_id];
      return;
    }
    roles[member.agent_id] = memberRoleEdits[member.agent_id]
      ?? (userRoleKeys[member.agent_id] ? defaultRoleFor(member.agent_id, userRoleKeys[member.agent_id], userResponsibilities[member.agent_id] || '') : undefined)
      ?? member.role ?? member.responsibility_markdown ?? defaultRoleFor(member.agent_id, member.role_key || 'member', userResponsibilities[member.agent_id] || '');
    const roleKey = userRoleKeys[member.agent_id] || member.role_key;
    if (roleKey) roleKeys[member.agent_id] = roleKey;
    if (roleKey) {
      const preset = roleMetaForKey(roleKey);
      roleMeta[member.agent_id] = {
        key: roleKey,
        label: preset?.label || member.role_label || roleKey,
        description: preset?.description || '',
        capabilities: userRoleKeys[member.agent_id]
          ? preset?.capabilities || []
          : member.assigned_capabilities ?? member.capabilities ?? [],
        workflow_lane: userRoleKeys[member.agent_id]
          ? preset?.workflow_lane || member.workflow_lane || 'other'
          : member.workflow_lane || preset?.workflow_lane || 'other',
      };
    }
  });
  for (const agentId of new Set([...Object.keys(userResponsibilities), ...Object.keys(userRoleKeys), ...Object.keys(pendingRoleChanges)])) {
    if (removedSuggestionIds.has(agentId) || !selectedMembers[agentId] || selected[agentId]) continue;
    selected[agentId] = true;
    roles[agentId] = memberRoleEdits[agentId] ?? memberRoles[agentId] ?? defaultRoleFor(agentId, userRoleKeys[agentId] || 'member', userResponsibilities[agentId] || '');
    if (memberRoleKeys[agentId]) roleKeys[agentId] = memberRoleKeys[agentId];
    if (memberRoleMeta[agentId]) roleMeta[agentId] = memberRoleMeta[agentId];
  }
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
  formationResultKey = formationDraftKey();
  if (!workflowEditedByUser) teamWorkflow = suggestion.workflow || teamWorkflow;
  if (Object.keys(userRoleKeys).length || Object.keys(memberRoleEdits).length || removedSuggestionIds.size) syncWorkflowForRoster();
  teamRolesLocked = true;
  formationPreviewOnly = false;
  message = suggestion.reasons?.join(' ') || '已根据团队目标和约束生成组队建议';
}

function applyPendingUserTeamPreview(requiredAgentIds: string[]): void {
  const eligibleIds = new Set(teamAgentOptions().map((agent) => agent.id));
  const requestedIds = new Set([
    teamLeaderId,
    ...requiredAgentIds,
    ...Object.keys(selectedMembers).filter((id) => selectedMembers[id]),
    ...Object.keys(userResponsibilities).filter((id) => selectedMembers[id] || requiredAgentIds.includes(id)),
    ...Object.keys(userRoleKeys).filter((id) => selectedMembers[id] || requiredAgentIds.includes(id)),
  ]);
  const nextSelected: Record<string, boolean> = {};
  const nextRoles: Record<string, string> = {};
  const nextRoleKeys: Record<string, string> = {};
  const nextRoleMeta: Record<string, ExternalTeamRole> = {};

  for (const agentId of requestedIds) {
    if (!eligibleIds.has(agentId) || excludedTeamAgentIds.includes(agentId) || removedSuggestionIds.has(agentId)) continue;
    nextSelected[agentId] = true;
    const mission = userResponsibilities[agentId] || '';
    const roleKey = agentId === teamLeaderId
      ? 'project_manager'
      : userRoleKeys[agentId] || recommendRoleKey(agentId, mission);
    nextRoleKeys[agentId] = roleKey;
    const preset = roleMetaForKey(roleKey);
    if (preset) nextRoleMeta[agentId] = preset;
    nextRoles[agentId] = memberRoleEdits[agentId]
      ?? (userRoleKeys[agentId] ? memberRoles[agentId] : undefined)
      ?? defaultRoleFor(agentId, roleKey, mission);
  }

  selectedMembers = nextSelected;
  memberRoles = nextRoles;
  memberRoleKeys = nextRoleKeys;
  memberRoleMeta = nextRoleMeta;
  teamRolesLocked = true;
  if (formationPlan) formationNeedsReview = true;
  formationPreviewOnly = true;
}

async function assessCurrentTeam(): Promise<void> {
  if (busy) return;
  const requestSeq = formationRequestSeq;
  const revision = formationEditRevision;
  const members = selectedReviewAgents().map((agent) => ({
    agent_id: agent.id,
    role_key: pendingRoleChanges[agent.id] || effectiveMemberRoleKey(agent.id),
    assigned_capabilities: pendingRoleChanges[agent.id]
      ? roleMetaForKey(pendingRoleChanges[agent.id])?.capabilities || []
      : memberRoleMeta[agent.id]?.capabilities || [],
  }));
  busy = true;
  assessmentRunning = true;
  assessmentFeedback = null;
  message = '';
  render();
  try {
    const assessment = await backendApi.assessExternalTeam({
      members,
      required_capabilities: [...new Set([...requiredTeamCapabilities, ...(formationPlan?.coverage.required || [])])],
      team_name: teamName.trim(),
      team_goal: teamDescription.trim(),
      role_changes: Object.entries(pendingRoleChanges).map(([agentId, roleKey]) => ({
        agent_id: agentId, role_key: roleKey, responsibility: memberRoles[agentId] || '',
      })),
    });
    if (requestSeq !== formationRequestSeq) return;
    if (revision !== formationEditRevision) {
      assessmentFeedback = { kind: 'error', text: '检查期间分工发生变化，请重新检查能力。' };
      return;
    }
    const review = classifyRoleAssessment(pendingRoleChanges, assessment);
    if (review.kind === 'incomplete') {
      assessmentFeedback = { kind: 'error', text: '检查结果不完整，新角色尚未生效，请重新检查。' };
      return;
    }
    if (review.kind === 'unavailable' || review.kind === 'confirmation') {
      pendingRoleAssessment = assessment;
      roleRiskDecisions = review.risks;
      assessmentFeedback = { kind: 'error', text: '部分角色需要处理后才能生效；成员原职责仍已保留。' };
      return;
    }
    if (review.kind === 'manual') {
      pendingRoleAssessment = { ...assessment, role_suggestion_error: assessment.role_suggestion_error || 'invalid_ai_output' };
      roleRiskDecisions = review.risks;
      assessmentFeedback = { kind: 'error', text: 'AI 暂时未生成有效的职责建议。新角色尚未生效，可重试、使用手写职责或恢复原角色。' };
      return;
    }
    applyRoleAssessment(assessment);
  } catch (error) {
    if (requestSeq === formationRequestSeq) assessmentFeedback = { kind: 'error', text: `检查失败：${(error as Error).message}。请确认后端已启动并重试。` };
  } finally {
    if (requestSeq === formationRequestSeq) {
      assessmentRunning = false;
      busy = false;
      render();
    }
  }
}

function applyRoleAssessment(assessment: RoleAssessment): void {
    const changes = Object.entries(pendingRoleChanges);
    for (const [agentId, roleKey] of changes) {
      const suggestion = assessment.role_suggestions?.find((item) => item.agent_id === agentId && item.role_key === roleKey);
      if (!suggestion) return;
    }
    for (const [agentId, roleKey] of changes) {
      const suggestion = assessment.role_suggestions!.find((item) => item.agent_id === agentId)!;
      const userDuty = memberRoleEdits[agentId] ?? userResponsibilities[agentId];
      delete suggestedUserDuties[agentId];
      delete priorUserDuties[agentId];
      memberRoleKeys[agentId] = roleKey;
      userRoleKeys[agentId] = roleKey;
      const meta = roleMetaForKey(roleKey);
      if (meta) memberRoleMeta[agentId] = meta;
      if (userDuty !== undefined) {
        if (suggestion.responsibility.trim() !== userDuty.trim()) suggestedUserDuties[agentId] = suggestion.responsibility;
      } else {
        memberRoles[agentId] = suggestion.responsibility;
        roleDutyMissions[agentId] = suggestion.responsibility;
      }
      delete memberRoleNeedsReview[agentId];
    }
    pendingRoleChanges = {};
    roleRiskDecisions = {};
    pendingRoleAssessment = null;
    if (changes.length) syncWorkflowForRoster();
    const plan: FormationPlan = formationPlan || {
      version: 1, leader_agent_id: teamLeaderId, members: [],
      coverage: { required: [], covered: [], uncovered: [] },
      confidence: { requirement: 1, capability_evidence: 0, coverage: 0, overall: 0 },
      staffing_mode: 'user_confirmed', excluded_agent_ids: [], reasons: [], warnings: [],
    };
    const coverage = assessment.coverage;
    const score = coverage.required.length ? coverage.covered.length / coverage.required.length : 1;
    const evidence = assessment.capability_evidence_confidence;
    formationPlan = { ...plan, coverage, member_capability_assessments: assessment.members, confidence: {
      ...plan.confidence, capability_evidence: evidence, coverage: score,
      overall: (plan.confidence.requirement + evidence + score) / 3,
    } };
    memberCapabilityChecks = Object.fromEntries(assessment.members.map((member) => [member.agent_id, { ...member.coverage, available: member.available }]));
    formationNeedsReview = false;
    const hasCapabilities = coverage.required.length > 0;
    const counts = hasCapabilities
      ? `${coverage.covered.length} 项有画像支持，${coverage.unverified?.length || 0} 项待验证，${(coverage.uncovered?.length || 0) + (coverage.unavailable?.length || 0)} 项需关注。`
      : '当前角色没有可检查的标准能力项，暂无法从画像判断。';
    const changedText = changes.length ? '新角色已生效，系统职责已按角色更新；手写职责已保留。' : '成员和职责未改变。';
    const unavailableMembers = assessment.members.filter((item) => item.available === false).map((item) => agentNameById(item.agent_id));
    assessmentFeedback = { kind: 'success', text: `${counts}${unavailableMembers.length ? `运行时不可用：${unavailableMembers.join('、')}。` : ''}各成员结果显示在角色下方。${changedText}` };
}

async function requestTeamSuggestion(requiredAgentIds: string[], forceRequiredAgentIds: string[] = [], preserveRoster = false): Promise<void> {
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  formationRequestAbort?.abort();
  const controller = new AbortController();
  formationRequestAbort = controller;
  const requestSeq = ++formationRequestSeq;
  const startedAt = Date.now();
  const requestRevision = formationEditRevision;
  let fastApplied = false;
  busy = true;
  formationStatus = 'fast_loading';
  formationImprovements = [];
  formationAiAttempted = false;
  startFormationElapsedTimer(startedAt);
  message = '';
  renderIfActive(epoch);
  try {
    const constraints = buildTeamConstraintText();
    const description = [teamDescription.trim(), constraints && `组队约束：\n${constraints}`].filter(Boolean).join('\n\n');
    const request = {
      name: teamName.trim(),
      description,
      workflow: teamWorkflow.trim(),
      leader_agent_id: teamLeaderId,
      required_agent_ids: [...new Set([...requiredAgentIds, ...(preserveRoster ? selectedReviewAgents().map((agent) => agent.id) : []), ...Object.keys(userResponsibilities).filter((id) => selectedMembers[id]), ...Object.keys(userRoleKeys).filter((id) => selectedMembers[id]), ...Object.keys(memberRoleEdits).filter((id) => selectedMembers[id])])],
      excluded_agent_ids: [...new Set([...excludedTeamAgentIds, ...removedSuggestionIds])],
      force_required_agent_ids: forceRequiredAgentIds,
      required_capabilities: requiredTeamCapabilities,
      custom_capabilities: customTeamCapabilities,
      allow_system_fill: allowSystemFill,
      member_assignments: [...new Set([...requiredAgentIds, ...Object.keys(userResponsibilities), ...Object.keys(userRoleKeys), ...Object.keys(memberRoleEdits)])]
        .filter((id) => !removedSuggestionIds.has(id) && (requiredAgentIds.includes(id) || selectedMembers[id]))
        .map((id) => ({ agent_id: id, responsibility: memberRoleEdits[id] ?? userResponsibilities[id], role_key: userRoleKeys[id] })),
    };
    if (!selectedReviewAgents().length || !teamRolesLocked) {
      applyPendingUserTeamPreview(requiredAgentIds);
    } else formationPreviewOnly = true;
    render();
    const suggestion = await backendApi.suggestExternalTeamAuto(request, {
      signal: controller.signal,
      onSuggestion: (snapshot, phase) => {
        if (requestSeq !== formationRequestSeq || signal?.aborted || epoch !== agentsLifecycleEpoch) return;
        const conflicts = snapshot.required_agent_conflicts || [];
        if (snapshot.decision_required && conflicts.length) {
          teamConstraintDecision = conflicts;
          if (phase === 'final') formationStatus = 'idle';
          return;
        }
        teamConstraintDecision = [];
        applyTeamSuggestion(snapshot);
        formationNeedsReview = formationEditRevision !== requestRevision;
        fastApplied = true;
        if (phase === 'final') {
          formationStatus = resolveFormationUiStatus(snapshot);
          formationImprovements = snapshot.ai_material_improvements || [];
        }
        renderIfActive(epoch);
      },
      onStatus: () => {
        if (requestSeq !== formationRequestSeq || signal?.aborted || epoch !== agentsLifecycleEpoch) return;
        formationStatus = 'ai_reviewing';
        formationAiAttempted = true;
        message = '';
        renderIfActive(epoch);
      },
    });
    if (requestSeq !== formationRequestSeq || signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    const conflicts = suggestion.required_agent_conflicts || [];
    if (suggestion.decision_required && conflicts.length) {
      teamConstraintDecision = conflicts;
      formationStatus = 'idle';
      return;
    }
    teamConstraintDecision = [];
    applyTeamSuggestion(suggestion);
    formationNeedsReview = formationEditRevision !== requestRevision;
    formationStatus = resolveFormationUiStatus(suggestion);
    formationImprovements = suggestion.ai_material_improvements || [];
  } catch (error) {
    if (requestSeq !== formationRequestSeq || signal?.aborted || epoch !== agentsLifecycleEpoch || (error as Error).name === 'AbortError') return;
    formationStatus = fastApplied ? 'ready_partial' : 'idle';
    message = fastApplied
      ? '初步团队方案已保留，智能检查暂未完成'
      : `智能组队失败：${(error as Error).message}`;
  } finally {
    if (requestSeq === formationRequestSeq && epoch === agentsLifecycleEpoch) {
      formationRequestAbort = null;
      formationElapsedMs = Date.now() - startedAt;
      stopFormationElapsedTimer();
      busy = false;
      render();
    }
  }
}

async function scanRuntimes(): Promise<void> {
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  const startedAt = performance.now();
  busy = true;
  runtimeScanning = true;
  message = '正在探测本机运行时与模型，请稍候…';
  renderIfActive(epoch);
  try {
    runtimes = await backendApi.scanRuntimes();
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
    message = `已刷新 ${runtimes.length} 个运行时，耗时 ${elapsed} 秒`;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `刷新运行时失败：${(error as Error).message}`;
  } finally {
    // runtimeScanning/busy 是模块级状态，跨页面生命周期存活，
    // 必须无条件复位；只有渲染需要生命周期守卫。
    runtimeScanning = false;
    busy = false;
    renderIfActive(epoch);
  }
}

async function deleteRuntime(runtime: ExternalRuntime): Promise<void> {
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  const confirmed = await showConfirmDialog({
    title: '删除运行时记录',
    message: `删除“${runtime.name || runtime.provider}”的发现记录？如果工具仍安装在电脑上，下次点“再找找”时它会重新出现。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  busy = true;
  message = '';
  renderIfActive(epoch);
  try {
    await backendApi.deleteRuntime(runtime.id);
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    runtimes = runtimes.filter((item) => item.id !== runtime.id);
    message = `已删除 ${runtime.name || runtime.provider}`;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `删除运行时失败：${(error as Error).message}`;
  } finally {
    if (epoch === agentsLifecycleEpoch) {
      busy = false;
      render();
    }
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
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  busy = true;
  message = '';
  renderIfActive(epoch);
  try {
    const runtime = runtimes.find((item) => item.id === agentRuntimeId);
    const payload: Parameters<typeof backendApi.createExternalAgent>[0] = {
      name: agentName.trim() || `${runtime?.name || '外部'}智能体`,
      runtime_id: agentRuntimeId,
    };
    payload.model = agentModel.trim();
    const created = await backendApi.createExternalAgent(payload);
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    await refresh();
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    agentRuntimeId = '';
    agentName = '';
    agentModel = '';
    activeTab = 'mine';
    message = `已添加外援 ${created.name}`;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `添加外援失败：${(error as Error).message}`;
  } finally {
    if (epoch === agentsLifecycleEpoch) {
      busy = false;
      render();
    }
  }
}

async function createTeam(): Promise<void> {
  if (Object.keys(pendingRoleChanges).length) {
    message = '请先检查新角色，或恢复原角色。';
    render();
    return;
  }
  if (!teamLeaderId) {
    message = '请选择 Leader';
    render();
    return;
  }
  const reviewAgents = selectedReviewAgents();
  const missingResponsibility = reviewAgents.find((agent) => !(memberRoles[agent.id] || '').trim());
  if (missingResponsibility) {
    message = `请填写 ${missingResponsibility.name} 的职责，或先移除该成员。`;
    render();
    return;
  }
  const members = reviewAgents
    .map((agent, index) => {
      const meta = memberRoleMeta[agent.id];
      const member: Parameters<typeof backendApi.createExternalTeam>[0]['members'][number] = {
        agent_id: agent.id,
        role: (memberRoles[agent.id] || defaultRoleFor(agent.id, memberRoleKeys[agent.id] || 'member')).trim(),
        sort_order: index,
      };
      member.role_key = effectiveMemberRoleKey(agent.id);
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
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  busy = true;
  message = '';
  renderIfActive(epoch);
  try {
    const plan: FormationPlan = formationPlan || {
      version: 1, leader_agent_id: teamLeaderId, members: [],
      coverage: { required: [], covered: [], uncovered: [] },
      confidence: { requirement: 1, capability_evidence: 0, coverage: 1, overall: 1 },
      staffing_mode: 'user_confirmed', excluded_agent_ids: [], reasons: [], warnings: [],
    };
    const confirmedFormationPlan = {
      ...plan,
      ...(formationNeedsReview ? {
        member_capability_assessments: [],
        coverage: {
          required: [...plan.coverage.required],
          covered: [],
          uncovered: [],
          unverified: [...plan.coverage.required],
          unavailable: [],
        },
        confidence: {
          ...plan.confidence,
          coverage: 0,
          overall: Math.round(((plan.confidence.requirement + plan.confidence.capability_evidence) / 3) * 10000) / 10000,
        },
        warnings: [...plan.warnings, '角色或职责手动调整，能力建议待重新评估。'],
      } : {}),
      original_description: originalTeamDescription,
      formation_allow_system_fill: allowSystemFill,
      runtime_allow_suggestions: runtimeAllowSuggestions,
      runtime_allow_auto_reassign: runtimeAllowAutoReassign,
      leader_agent_id: teamLeaderId,
      members: members.map((member) => {
        const planned = plan.members.find((item) => item.agent_id === member.agent_id);
        return {
          agent_id: member.agent_id,
          role_key: member.role_key || planned?.role_key || '',
          role_label: member.role_label || planned?.role_label || '',
          assigned_capabilities: member.assigned_capabilities || planned?.assigned_capabilities || [],
          responsibility: (memberRoleEdits[member.agent_id] ?? userResponsibilities[member.agent_id])?.trim()
            ? { ...(planned?.responsibility || {}), mission: (memberRoleEdits[member.agent_id] ?? userResponsibilities[member.agent_id]).trim() }
            : roleDutyMissions[member.agent_id]
              ? { mission: roleDutyMissions[member.agent_id] }
              : planned?.responsibility || {},
          responsibility_markdown: member.role,
          responsibility_source: member.agent_id in userResponsibilities || member.agent_id in memberRoleEdits ? 'user' : member.agent_id in roleDutyMissions ? 'ai' : planned?.responsibility_source || 'system',
          ...(member.agent_id in priorUserDuties ? { original_user_responsibility: priorUserDuties[member.agent_id] } : {}),
          selection_source: member.agent_id in userResponsibilities || member.agent_id in userRoleKeys || member.agent_id in memberRoleEdits ? 'user' : planned?.selection_source || 'user',
          locked: member.agent_id in userResponsibilities || member.agent_id in userRoleKeys || member.agent_id in memberRoleEdits || (planned?.locked ?? true),
          selection_reason: planned?.selection_reason || '用户确认的团队成员。',
        };
      }),
    };
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
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    await refresh();
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    resetTeamForm();
    activeTab = 'mine';
    message = `已创建团队 ${created.name}`;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `创建团队失败：${(error as Error).message}`;
  } finally {
    if (epoch === agentsLifecycleEpoch) {
      busy = false;
      render();
    }
  }
}

function resetTeamForm(): void {
  invalidateFormationDecision();
  cancelDescriptionDraftRequest();
  teamNameComposing = false;
  teamName = '';
  teamDescription = '';
  teamDescriptionTouched = false;
  generatedTeamDescription = '';
  lastDescriptionDraftKey = '';
  descriptionDraftMeta = null;
  goalOrganizeNotice = '';
  originalTeamDescription = '';
  teamLeaderId = CREW_BUILTIN_AGENT_ID;
  teamWorkflow = '';
  workflowEditedByUser = false;
  workflowNeedsReview = false;
  workflowDetailsOpen = false;
  selectedMembers = {};
  memberRoles = {};
  memberRoleEdits = {};
  memberRoleNeedsReview = {};
  expandedMemberRoles.clear();
  memberRoleKeys = {};
  memberRoleMeta = {};
  pendingRoleChanges = {};
  roleDutyMissions = {};
  suggestedUserDuties = {};
  priorUserDuties = {};
  pendingRoleAssessment = null;
  roleRiskDecisions = {};
  memberCapabilityChecks = {};
  userResponsibilities = {};
  userRoleKeys = {};
  removedSuggestionIds = new Set();
  allowSystemFill = true;
  runtimeAllowSuggestions = true;
  runtimeAllowAutoReassign = true;
  formationEditRevision = 0;
  formationNeedsReview = false;
  teamSpec = null;
  formationPlan = null;
  formationResultKey = '';
  teamRolesLocked = false;
  formationPreviewOnly = false;
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
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  busy = true;
  message = '';
  renderIfActive(epoch);
  try {
    await backendApi.deleteExternalAgent(agent.id);
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    await refresh();
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `已删除 ${agent.name}`;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `删除失败：${(error as Error).message}`;
  } finally {
    if (epoch === agentsLifecycleEpoch) {
      busy = false;
      render();
    }
  }
}

async function deleteTeam(team: ExternalTeam): Promise<void> {
  if (!window.confirm(`删除团队「${team.name}」？`)) return;
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  busy = true;
  message = '';
  renderIfActive(epoch);
  try {
    await backendApi.deleteExternalTeam(team.id);
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    await refresh();
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `已删除团队 ${team.name}`;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `删除团队失败：${(error as Error).message}`;
  } finally {
    if (epoch === agentsLifecycleEpoch) {
      busy = false;
      render();
    }
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
  const epoch = agentsLifecycleEpoch;
  const signal = agentsLifecycleSignal;
  busy = true;
  renderIfActive(epoch);
  let shouldScanRuntimes = false;
  try {
    await refresh();
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    shouldScanRuntimes = runtimes.length === 0 && !initialRuntimeScanStarted;
    if (shouldScanRuntimes) initialRuntimeScanStarted = true;
  } catch (error) {
    if (signal?.aborted || epoch !== agentsLifecycleEpoch) return;
    message = `外援阵容加载失败：${(error as Error).message}`;
  }
  busy = false;
  if (shouldScanRuntimes) {
    await scanRuntimes();
    return;
  }
  renderIfActive(epoch);
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
  agentHubView?.element.remove();
  agentHubView?.dispose();
  agentHubView = null;
}

export function createAgentsPageContribution(): PageContribution {
  return {
    id: 'agents',
    isAvailable: () => externalAgentsEnabled(),
    activate: ({ signal }: PageLifecycleContext) => {
      agentsLifecycleEpoch += 1;
      agentsLifecycleSignal = signal;
      agentsGlobalEventsDispose?.();
      agentsGlobalEventsDispose = bindAgentsGlobalEvents(signal);
      resetAgentsNavigationState();
      render();
      void loadAgentsPage();
    },
    deactivate: async () => {
      agentsLifecycleEpoch += 1;
      agentsLifecycleSignal = null;
      agentsGlobalEventsDispose?.();
      agentsGlobalEventsDispose = null;
      disposeAgentsPage();
    },
  };
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
}
