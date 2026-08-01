import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import type {
  ExternalAgent,
  ExternalRuntime,
  ExternalTeam,
  ExternalTeamSuggestion,
  ExternalTeamRole,
  FormationStaffingGap,
  FormationPlan,
  RequiredAgentConflict,
  RuntimeModelProfile,
} from "../types";
import MarkdownContent from "./MarkdownContent";

type TabKey = "mine" | "runtime" | "create-agent" | "create-team";
type AgentsGuideMode = "hidden" | "welcome" | "tour";
type AgentsGuideStepNumber = 1 | 2 | 3;
type AgentsGuideStep = {
  progress: "1/3" | "2/3" | "3/3";
  title: string;
  body: string;
  target: string;
  side: "left" | "right";
};
type FormationUiStatus =
  | "idle"
  | "fast_loading"
  | "ai_reviewing"
  | "ready_improved"
  | "ready_unchanged"
  | "ready_partial";

const TEAM_DRAFT_DEBOUNCE_MS = 600;
export const TEAM_REQUIRED_CAPABILITIES = [
  { key: "information_retrieval", label: "检索", prompt: "必须包含信息检索能力。" },
  { key: "analysis", label: "分析", prompt: "必须包含分析论证能力。" },
  { key: "verification", label: "核验", prompt: "必须包含核验复核能力。" },
  { key: "implementation", label: "实现", prompt: "必须包含执行实现能力。" },
  { key: "documentation", label: "文档", prompt: "必须包含文档交付能力。" },
] as const;

const TEAM_CAPABILITY_LABELS: Record<string, string> = {
  planning: "规划统筹",
  requirements: "需求澄清",
  information_retrieval: "信息检索",
  research: "研究调研",
  analysis: "分析论证",
  synthesis: "综合汇总",
  review: "独立审阅",
  design: "体验设计",
  frontend: "前端实现",
  backend: "后端实现",
  implementation: "执行实现",
  testing: "测试验证",
  verification: "核验复核",
  documentation: "文档交付",
};

export function buildTeamConstraintText({
  requiredAgentNames,
  excludedAgentNames,
  requiredCapabilities,
  customCapabilities,
}: {
  requiredAgentNames: string[];
  excludedAgentNames: string[];
  requiredCapabilities: string[];
  customCapabilities: string[];
}): string {
  const capabilityKeys = new Set(requiredCapabilities);
  return [
    ...requiredAgentNames.map((name) => `${name} 必须作为成员加入团队。`),
    ...excludedAgentNames.map((name) => `不要让 ${name} 加入团队。`),
    ...TEAM_REQUIRED_CAPABILITIES.filter((item) => capabilityKeys.has(item.key)).map((item) => item.prompt),
    ...customCapabilities.map((capability) => `团队还必须具备「${capability}」能力。`),
  ].filter(Boolean).join("\n");
}

export function formatTeamDraftElapsed(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds > 0 ? `${minutes}min ${seconds}s` : `${minutes}min`;
}

export function formatLlmElapsed(elapsedMs: number): string {
  return `${(Math.max(0, elapsedMs) / 1000).toFixed(1)}s`;
}

export function resolveFormationUiStatus(
  suggestion: Pick<ExternalTeamSuggestion, "requested_formation_mode" | "selected_formation_mode" | "fallback_reason" | "ai_material_improvements">,
): Exclude<FormationUiStatus, "idle" | "fast_loading" | "ai_reviewing"> {
  if (suggestion.selected_formation_mode === "ai" && suggestion.ai_material_improvements?.length) {
    return "ready_improved";
  }
  if (["no_material_improvement", "quality_regressed", "baseline_coverage_regressed"].includes(suggestion.fallback_reason)) {
    return "ready_unchanged";
  }
  if (
    suggestion.requested_formation_mode === "auto"
    && suggestion.selected_formation_mode === "fast"
    && !suggestion.fallback_reason
  ) {
    return "ready_unchanged";
  }
  return suggestion.selected_formation_mode === "ai" ? "ready_unchanged" : "ready_partial";
}

export function decideTeamDescriptionDraftRequest({
  name,
  description,
  generatedDescription,
  lastDescriptionName,
  lastDraftKey,
}: {
  name: string;
  description: string;
  generatedDescription: string;
  lastDescriptionName: string;
  lastDraftKey: string;
}): { shouldRequest: boolean; shouldInvalidate: boolean; regenerateDescription: boolean; draftKey: string } {
  const normalizedName = name.trim();
  const draftKey = normalizedName;
  const regenerateDescription = normalizedName !== lastDescriptionName;
  const descriptionCanBeGenerated = regenerateDescription
    || !description.trim()
    || description === generatedDescription;
  const draftChanged = draftKey !== lastDraftKey;
  return {
    draftKey,
    regenerateDescription,
    shouldInvalidate: !normalizedName,
    // A manual description remains untouched until the team name changes.
    shouldRequest: Boolean(
      normalizedName
      && draftChanged
      && descriptionCanBeGenerated,
    ),
  };
}

const TABS: Record<TabKey, string> = {
  mine: "我的阵容",
  runtime: "发现外援",
  "create-agent": "添加外援",
  "create-team": "组建团队",
};
const AGENTS_GUIDE_STORAGE_KEY = "crew.externalAgents.guideDismissed.v1";
const AGENTS_GUIDE_HIGHLIGHT_PADDING = 6;
const AGENTS_GUIDE_TOOLTIP_GAP = 12;
const AGENTS_GUIDE_VIEWPORT_MARGIN = 12;

type AgentsGuideLayout = {
  highlight: { left: number; top: number; width: number; height: number } | null;
  tooltip: { left: number; top: number };
};

export function calculateAgentsGuideTooltipPosition(
  target: { left: number; right: number; top: number; bottom: number; width: number; height: number },
  tooltip: { width: number; height: number },
  viewport: { width: number; height: number },
  preferredSide: "left" | "right",
): { left: number; top: number } {
  const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max));
  const leftPosition = target.left - AGENTS_GUIDE_TOOLTIP_GAP - tooltip.width;
  const rightPosition = target.right + AGENTS_GUIDE_TOOLTIP_GAP;
  const canPlaceLeft = leftPosition >= AGENTS_GUIDE_VIEWPORT_MARGIN;
  const canPlaceRight = rightPosition + tooltip.width <= viewport.width - AGENTS_GUIDE_VIEWPORT_MARGIN;

  let left: number;
  let top: number;
  if ((preferredSide === "left" && canPlaceLeft) || !canPlaceRight) {
    left = canPlaceLeft ? leftPosition : rightPosition;
    top = target.top + target.height / 2 - tooltip.height / 2;
  } else {
    left = rightPosition;
    top = target.top + target.height / 2 - tooltip.height / 2;
  }

  if (!canPlaceLeft && !canPlaceRight) {
    left = target.left + target.width / 2 - tooltip.width / 2;
    top = target.bottom + AGENTS_GUIDE_TOOLTIP_GAP;
    if (top + tooltip.height > viewport.height - AGENTS_GUIDE_VIEWPORT_MARGIN) {
      top = target.top - AGENTS_GUIDE_TOOLTIP_GAP - tooltip.height;
    }
  }

  return {
    left: Math.round(clamp(
      left,
      AGENTS_GUIDE_VIEWPORT_MARGIN,
      viewport.width - tooltip.width - AGENTS_GUIDE_VIEWPORT_MARGIN,
    )),
    top: Math.round(clamp(
      top,
      AGENTS_GUIDE_VIEWPORT_MARGIN,
      viewport.height - tooltip.height - AGENTS_GUIDE_VIEWPORT_MARGIN,
    )),
  };
}

export function agentsGuideStepDefinition(
  step: AgentsGuideStepNumber,
  hasAgents: boolean,
  activeTab: TabKey = "mine",
): AgentsGuideStep {
  if (step === 1) {
    return {
      progress: "1/3",
      title: "先认识一下附近的帮手",
      body: "这里会列出电脑上可用的 AI 工具。点“再找找”可以主动刷新，但引导不会替你操作。",
      target: '[data-agents-guide-target="scan"]',
      side: "left",
    };
  }
  if (step === 2) {
    return {
      progress: "2/3",
      title: "把合适的外援加入阵容",
      body: "选择一位可用外援，起个顺口的称呼并确认模型；这里只带你认位置，不用真的创建。",
      target: '[data-agents-guide-target="runtime-select"]',
      side: "right",
    };
  }
  if (activeTab === "create-team") {
    return {
      progress: "3/3",
      title: "复杂任务，还可以拉一支小队",
      body: "选好 Leader 和需要的外援，Crew 会帮你整理分工与协作方式。",
      target: ".team-create__heading",
      side: "left",
    };
  }
  return hasAgents
    ? {
        progress: "3/3",
        title: "准备好，就可以直接派活",
        body: "点“派活”让外援接手当前任务；复杂任务还可以把多位外援拉进团队。",
        target: '[data-agents-guide-target="assign"]',
        side: "left",
      }
    : {
        progress: "3/3",
        title: "外援到位后，就能派活或组队",
        body: "现在还没有已加入的外援。之后从“添加外援”加入一位，就会在我的阵容里看到“派活”。",
        target: '[data-agents-tab="create-agent"]',
        side: "left",
      };
}

const defaultLeaderRole = [
  "### Leader 职责",
  "",
  "#### 工作原则",
  "- 拆清目标、拆小任务、持续汇总。",
  "- 每次推进都保证可交接、可验收、可继续。",
  "",
  "#### 职责",
  "- 理解团队目标，分配成员工作，检查阶段成果，形成最终输出。",
  "",
  "#### 团队协作关系",
  "- 向成员派发任务，收集结果后统一口径，必要时重新分配下一步。",
  "",
  "#### 输出格式",
  "- 当前成果：已经完成的结果。",
  "- 下一负责人：下一步由谁继续。",
  "- 下一动作：具体要做什么。",
  "- 风险/阻塞：缺少的信息、权限或依赖。",
  "",
  "#### 工作安排",
  "- 启动：确认目标、交付物和边界。",
  "- 执行：按成员能力拆分子任务。",
  "- 汇总：检查遗漏并输出最终结果。",
].join("\n");

interface Props {
  onAssignAgent: (agent: ExternalAgent) => void;
  onAssignTeam: (team: ExternalTeam) => void;
  onStartLeaderChat: (agent: ExternalAgent) => void;
}

interface PendingTemporaryMember extends FormationStaffingGap {
  runtime_id: string;
  model_id: string;
}

const CREW_BUILTIN_AGENT_ID = "crew::builtin";
const crewBuiltinAgent: ExternalAgent = {
  id: CREW_BUILTIN_AGENT_ID,
  name: "Crew 队长",
  provider: "crew",
  display_badge: "M",
  runtime_id: "",
  model: "builtin",
  system_prompt: "",
  custom_args: [],
  custom_env: {},
  created_at: "",
  updated_at: "",
};

interface AgentFormSelectOption {
  value: string;
  label: string;
  description?: string;
  badge?: string;
}

function AgentFormSelect({
  value,
  placeholder,
  options,
  onChange,
  disabled = false,
}: {
  value: string;
  placeholder: string;
  options: AgentFormSelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const selected = options.find((option) => option.value === value);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div className="agent-form-select" ref={rootRef}>
      <button
        className={"agent-form-select__trigger" + (open ? " is-open" : "")}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => {
          if (disabled) return;
          setOpen((current) => !current);
        }}
      >
        <span>{selected?.label || placeholder}</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="m6 9 6 6 6-6"/>
        </svg>
      </button>
      {open && !disabled && (
        <div className="agent-form-select__popover" role="listbox">
          <button
            className={"agent-form-select__item" + (!value ? " is-active" : "")}
            type="button"
            role="option"
            aria-selected={!value}
            onClick={() => {
              onChange("");
              setOpen(false);
            }}
          >
            <span className="agent-form-select__name">{placeholder}</span>
          </button>
          {options.map((option) => (
            <button
              className={"agent-form-select__item" + (option.value === value ? " is-active" : "")}
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              <span className="agent-form-select__name">{option.label}</span>
              {option.description && <span className="agent-form-select__desc">{option.description}</span>}
              {option.badge && <span className="agent-form-select__badge">{option.badge}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function runtimeLabel(runtime?: ExternalRuntime): string {
  if (!runtime) return "来源未知";
  return runtime.version || runtime.name || runtime.provider;
}

function runtimeModels(runtime?: ExternalRuntime): RuntimeModelProfile[] {
  const raw = runtime?.metadata?.models;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item) => {
    if (typeof item === "string" && item.trim()) return [{ id: item, label: item }];
    if (!item || typeof item !== "object") return [];
    const model = item as Record<string, unknown>;
    const id = String(model.id || model.modelId || model.model_id || "").trim();
    if (!id) return [];
    return [{
      id,
      label: String(model.label || model.name || id),
      provider: typeof model.provider === "string" ? model.provider : undefined,
      default: model.default === true,
    }];
  });
}

function runtimeStatus(runtime: ExternalRuntime): "ready" | "degraded" | "unavailable" {
  if (runtime.availability_status) return runtime.availability_status;
  return runtime.available ? "ready" : "unavailable";
}

const runtimeStatusLabel = {
  ready: "随时可用",
  degraded: "已找到，模型信息还没准备好",
  unavailable: "暂时不可用",
};

export default function AgentsHub({ onAssignAgent, onAssignTeam, onStartLeaderChat }: Props) {
  const [tab, setTab] = useState<TabKey>("mine");
  const [runtimes, setRuntimes] = useState<ExternalRuntime[]>([]);
  const [runtimeScanning, setRuntimeScanning] = useState(false);
  const [agents, setAgents] = useState<ExternalAgent[]>([]);
  const [teams, setTeams] = useState<ExternalTeam[]>([]);
  const [busy, setBusy] = useState(false);
  const [summoning, setSummoning] = useState(false);
  const [message, setMessage] = useState<{ tab: TabKey; text: string } | null>(null);
  const [guideMode, setGuideMode] = useState<AgentsGuideMode>(() => {
    if (typeof window === "undefined") return "welcome";
    return window.localStorage.getItem(AGENTS_GUIDE_STORAGE_KEY) === "true" ? "hidden" : "welcome";
  });
  const [guideStep, setGuideStep] = useState<AgentsGuideStepNumber>(1);
  const [guideLayout, setGuideLayout] = useState<AgentsGuideLayout | null>(null);
  const guideBubbleRef = useRef<HTMLElement | null>(null);
  const guideAutoScrolledStep = useRef<AgentsGuideStepNumber | null>(null);

  const [agentRuntimeId, setAgentRuntimeId] = useState("");
  const [agentName, setAgentName] = useState("");
  const [agentModel, setAgentModel] = useState("");

  const [teamName, setTeamName] = useState("");
  const [teamNameComposing, setTeamNameComposing] = useState(false);
  const [teamDescription, setTeamDescription] = useState("");
  const [generatedTeamDescription, setGeneratedTeamDescription] = useState("");
  const [teamLeaderId, setTeamLeaderId] = useState(CREW_BUILTIN_AGENT_ID);
  const [teamWorkflow, setTeamWorkflow] = useState("");
  const [teamSpec, setTeamSpec] = useState<Record<string, unknown> | null>(null);
  const [formationPlan, setFormationPlan] = useState<FormationPlan | null>(null);
  const [formationStatus, setFormationStatus] = useState<FormationUiStatus>("idle");
  const [formationStartedAt, setFormationStartedAt] = useState<number | null>(null);
  const [formationElapsedMs, setFormationElapsedMs] = useState(0);
  const [formationImprovements, setFormationImprovements] = useState<string[]>([]);
  const [formationAiAttempted, setFormationAiAttempted] = useState(false);
  const [staffingDecision, setStaffingDecision] = useState<FormationStaffingGap[] | null>(null);
  const [staffingSelections, setStaffingSelections] = useState<PendingTemporaryMember[]>([]);
  const [pendingTemporaryMembers, setPendingTemporaryMembers] = useState<PendingTemporaryMember[]>([]);
  const [requiredTeamAgentIds, setRequiredTeamAgentIds] = useState<string[]>([]);
  const [excludedTeamAgentIds, setExcludedTeamAgentIds] = useState<string[]>([]);
  const [requiredTeamCapabilities, setRequiredTeamCapabilities] = useState<string[]>([]);
  const [customTeamCapabilities, setCustomTeamCapabilities] = useState<string[]>([]);
  const [customTeamCapabilityInput, setCustomTeamCapabilityInput] = useState("");
  const [showCustomCapabilityInput, setShowCustomCapabilityInput] = useState(false);
  const [showTeamConstraints, setShowTeamConstraints] = useState(false);
  const [descriptionDrafting, setDescriptionDrafting] = useState(false);
  const [descriptionDraftStartedAt, setDescriptionDraftStartedAt] = useState<number | null>(null);
  const [descriptionDraftElapsedMs, setDescriptionDraftElapsedMs] = useState(0);
  const [descriptionDraftMeta, setDescriptionDraftMeta] = useState<{
    llmElapsedMs?: number;
    cacheHit?: boolean;
  } | null>(null);
  const [singleLeaderConfirm, setSingleLeaderConfirm] = useState(false);
  const [teamConstraintDecision, setTeamConstraintDecision] = useState<RequiredAgentConflict[] | null>(null);
  const [selectedMembers, setSelectedMembers] = useState<Record<string, boolean>>({});
  const [memberRoles, setMemberRoles] = useState<Record<string, string>>({});
  const [memberRoleKeys, setMemberRoleKeys] = useState<Record<string, string>>({});
  const [memberRoleMeta, setMemberRoleMeta] = useState<Record<string, ExternalTeamRole>>({});
  const [teamRolesLocked, setTeamRolesLocked] = useState(false);
  const [rolePresets, setRolePresets] = useState<ExternalTeamRole[]>([]);
  const [roleGenerating, setRoleGenerating] = useState<Record<string, boolean>>({});
  const [editingAgent, setEditingAgent] = useState<ExternalAgent | null>(null);
  const [editingRole, setEditingRole] = useState("");
  const [editingRoleKey, setEditingRoleKey] = useState("");
  const [roleError, setRoleError] = useState("");
  const [activeTeamId, setActiveTeamId] = useState("");
  const lastDescriptionDraftName = useRef("");
  const lastDescriptionDraftKey = useRef("");
  const descriptionDraftSeq = useRef(0);
  const descriptionDraftAbort = useRef<AbortController | null>(null);
  const formationRequestSeq = useRef(0);
  const formationRequestAbort = useRef<AbortController | null>(null);
  const initialRuntimeScanStarted = useRef(false);

  const cancelDescriptionDraftRequest = useCallback(() => {
    descriptionDraftAbort.current?.abort();
    descriptionDraftAbort.current = null;
    descriptionDraftSeq.current += 1;
    setDescriptionDrafting(false);
    setDescriptionDraftStartedAt(null);
  }, []);

  const invalidateFormationDecision = () => {
    formationRequestAbort.current?.abort();
    formationRequestAbort.current = null;
    formationRequestSeq.current += 1;
    setTeamRolesLocked(false);
    setFormationStatus("idle");
    setFormationStartedAt(null);
    setFormationElapsedMs(0);
    setFormationImprovements([]);
    setFormationAiAttempted(false);
    setSummoning(false);
    setBusy(false);
    setStaffingDecision(null);
    setStaffingSelections([]);
    setPendingTemporaryMembers([]);
  };

  useEffect(() => {
    if (!teamRolesLocked) setFormationPlan(null);
  }, [teamRolesLocked]);

  const runtimeById = useMemo(
    () => new Map(runtimes.map((runtime) => [runtime.id, runtime])),
    [runtimes],
  );
  const teamAgentOptions = useMemo(() => [crewBuiltinAgent, ...agents], [agents]);
  const agentById = useMemo(
    () => new Map(teamAgentOptions.map((agent) => [agent.id, agent])),
    [teamAgentOptions],
  );
  const formationReadyAgents = useMemo(
    () => agents.filter((agent) => (
      !agent.profile
      || (agent.profile.availability === "ready" && agent.profile.model?.binding_status !== "missing")
    )),
    [agents],
  );
  const leaderOptions = useMemo(
    () => [
      {
        value: CREW_BUILTIN_AGENT_ID,
        label: "Crew 队长",
        description: "负责拆任务、盯进度和汇总交付",
        badge: "builtin",
      },
      ...formationReadyAgents.map((agent) => ({
        value: agent.id,
        label: agent.name,
        description: agent.provider,
        badge: agent.model || "默认模型",
      })),
    ],
    [formationReadyAgents],
  );
  const roleOptions = useMemo(
    () =>
      rolePresets.map((role) => ({
        value: role.key,
        label: role.label,
        description: role.description,
        badge: role.workflow_lane,
      })),
    [rolePresets],
  );
  const runtimeOptions = useMemo(
    () =>
      runtimes.filter((runtime) => runtimeStatus(runtime) === "ready").map((runtime) => ({
        value: runtime.id,
        label: runtime.name,
        description: runtimeStatusLabel[runtimeStatus(runtime)],
        badge: runtime.version || runtime.provider || "默认",
      })),
    [runtimes],
  );
  const runtimeModelOptions = useMemo(() => {
    const runtime = runtimeById.get(agentRuntimeId);
    return runtimeModels(runtime).map((model) => ({
      value: model.id,
      label: model.label,
      description: model.id === model.label ? undefined : model.id,
      badge: model.default ? "默认" : model.provider,
    }));
  }, [agentRuntimeId, runtimeById]);

  const staffingRuntimeOptions = runtimeOptions;
  const staffingModelOptions = (runtimeId: string) => runtimeModels(runtimeById.get(runtimeId)).map((model) => ({
    value: model.id,
    label: model.label,
    description: model.id === model.label ? undefined : model.id,
    badge: model.default ? "默认" : model.provider,
  }));

  const activeTeam = useMemo(
    () => teams.find((team) => team.id === activeTeamId) || null,
    [activeTeamId, teams],
  );

  const activeTeamMembers = useMemo(() => {
    if (!activeTeam) return [];
    return [...activeTeam.members].sort((a, b) => {
      if (a.agent_id === activeTeam.leader_agent_id) return -1;
      if (b.agent_id === activeTeam.leader_agent_id) return 1;
      return (a.sort_order || 0) - (b.sort_order || 0);
    });
  }, [activeTeam]);

  const leaderName = (team: ExternalTeam): string => {
    if (team.leader_agent_id === CREW_BUILTIN_AGENT_ID) return "Crew 队长";
    return agentById.get(team.leader_agent_id)?.name || team.leader_agent_id;
  };

  const guideDefinition = agentsGuideStepDefinition(guideStep, agents.length > 0, tab);
  const changeTab = (nextTab: TabKey) => {
    setTab(nextTab);
    if (guideMode === "tour") {
      setGuideStep(nextTab === "runtime" ? 1 : nextTab === "create-agent" ? 2 : 3);
    }
  };
  const moveGuide = (nextStep: AgentsGuideStepNumber) => {
    setGuideMode("tour");
    setGuideStep(nextStep);
    guideAutoScrolledStep.current = null;
    setTab(nextStep === 1 ? "runtime" : nextStep === 2 ? "create-agent" : "mine");
    setMessage(null);
  };
  const startGuide = () => moveGuide(1);
  const finishGuide = () => {
    setGuideMode("hidden");
    setGuideLayout(null);
    guideAutoScrolledStep.current = null;
    try {
      window.localStorage.setItem(AGENTS_GUIDE_STORAGE_KEY, "true");
    } catch {
      // Storage may be disabled; hiding the guide for this page lifecycle is enough.
    }
  };
  const locateGuideTarget = () => {
    if (guideMode !== "tour") return;
    document.querySelector<HTMLElement>(guideDefinition.target)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
      inline: "nearest",
    });
  };

  useEffect(() => {
    if (guideMode !== "tour") return;
    let frame = 0;
    let disposed = false;

    const layout = () => {
      if (disposed) return;
      const target = document.querySelector<HTMLElement>(guideDefinition.target);
      document.querySelectorAll(".agents-guide-target").forEach((item) => {
        item.classList.remove("agents-guide-target");
      });
      target?.classList.add("agents-guide-target");

      const bubbleRect = guideBubbleRef.current?.getBoundingClientRect();
      const tooltipSize = {
        width: bubbleRect?.width || 332,
        height: bubbleRect?.height || 176,
      };
      if (!target) {
        setGuideLayout({
          highlight: null,
          tooltip: {
            left: Math.max(AGENTS_GUIDE_VIEWPORT_MARGIN, window.innerWidth - tooltipSize.width - 24),
            top: Math.max(AGENTS_GUIDE_VIEWPORT_MARGIN, window.innerHeight - tooltipSize.height - 24),
          },
        });
        return;
      }

      if (guideAutoScrolledStep.current !== guideStep) {
        guideAutoScrolledStep.current = guideStep;
        target.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
        frame = window.requestAnimationFrame(layout);
        return;
      }

      const rect = target.getBoundingClientRect();
      if (rect.width < 4 || rect.height < 4) {
        setGuideLayout((current) => current ? { ...current, highlight: null } : null);
        return;
      }
      setGuideLayout({
        highlight: {
          left: Math.round(rect.left - AGENTS_GUIDE_HIGHLIGHT_PADDING),
          top: Math.round(rect.top - AGENTS_GUIDE_HIGHLIGHT_PADDING),
          width: Math.round(rect.width + AGENTS_GUIDE_HIGHLIGHT_PADDING * 2),
          height: Math.round(rect.height + AGENTS_GUIDE_HIGHLIGHT_PADDING * 2),
        },
        tooltip: calculateAgentsGuideTooltipPosition(
          rect,
          tooltipSize,
          { width: window.innerWidth, height: window.innerHeight },
          guideDefinition.side,
        ),
      });
    };

    const scheduleLayout = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(layout);
    };
    scheduleLayout();
    window.addEventListener("resize", scheduleLayout);
    document.addEventListener("scroll", scheduleLayout, true);
    return () => {
      disposed = true;
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", scheduleLayout);
      document.removeEventListener("scroll", scheduleLayout, true);
      document.querySelectorAll(".agents-guide-target").forEach((item) => {
        item.classList.remove("agents-guide-target");
      });
    };
  }, [guideDefinition.side, guideDefinition.target, guideMode, guideStep]);

  const scanRuntimes = useCallback(async () => {
    const startedAt = performance.now();
    setBusy(true);
    setRuntimeScanning(true);
    setMessage({ tab: "runtime", text: "正在看看这台电脑上有哪些外援…" });
    try {
      const items = await api.scanRuntimes();
      setRuntimes(items);
      const readyCount = items.filter((runtime) => runtimeStatus(runtime) === "ready").length;
      const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
      setMessage({
        tab: "runtime",
        text: readyCount > 0
          ? `发现 ${readyCount} 个可用外援啦！选一个使用吧。用时 ${elapsed} 秒`
          : items.length > 0
            ? `找到了 ${items.length} 个外援，但暂时都还没准备好。稍后可以再找找。`
            : "这次还没找到外援。确认已安装支持的 AI 工具后，再找找。",
      });
    } catch {
      setMessage({ tab: "runtime", text: "这次没找成功，请稍后再找找。" });
    } finally {
      setRuntimeScanning(false);
      setBusy(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    const [runtimeItems, agentItems, teamItems, roleItems] = await Promise.all([
      api.runtimes(),
      api.externalAgents(),
      api.externalTeams(),
      api.externalTeamRoles(),
    ]);
    setRuntimes(runtimeItems);
    setAgents(agentItems);
    setTeams(teamItems);
    setRolePresets(roleItems);
    return runtimeItems;
  }, []);

  useEffect(() => {
    let disposed = false;
    refresh()
      .then((runtimeItems) => {
        if (
          disposed
          || runtimeItems.length > 0
          || initialRuntimeScanStarted.current
        ) return;
        initialRuntimeScanStarted.current = true;
        void scanRuntimes();
      })
      .catch(() => setMessage({ tab: "mine", text: "阵容加载失败，请稍后再试。" }));
    return () => {
      disposed = true;
    };
  }, [refresh, scanRuntimes]);

  useEffect(() => () => {
    descriptionDraftAbort.current?.abort();
  }, []);

  useEffect(() => {
    if (!descriptionDrafting || descriptionDraftStartedAt == null) return;
    const updateElapsed = () => setDescriptionDraftElapsedMs(Date.now() - descriptionDraftStartedAt);
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [descriptionDraftStartedAt, descriptionDrafting]);

  useEffect(() => {
    if (
      formationStartedAt == null
      || (formationStatus !== "fast_loading" && formationStatus !== "ai_reviewing")
    ) return;
    const updateElapsed = () => setFormationElapsedMs(Date.now() - formationStartedAt);
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(timer);
  }, [formationStartedAt, formationStatus]);

  useEffect(() => {
    if (teamNameComposing) return;
    const name = teamName.trim();
    const decision = decideTeamDescriptionDraftRequest({
      name,
      description: teamDescription,
      generatedDescription: generatedTeamDescription,
      lastDescriptionName: lastDescriptionDraftName.current,
      lastDraftKey: lastDescriptionDraftKey.current,
    });
    if (decision.shouldInvalidate) {
      cancelDescriptionDraftRequest();
      setDescriptionDraftMeta(null);
      return;
    }
    if (!decision.shouldRequest) return;
    cancelDescriptionDraftRequest();
    const controller = new AbortController();
    descriptionDraftAbort.current = controller;
    lastDescriptionDraftKey.current = decision.draftKey;
    const requestId = descriptionDraftSeq.current + 1;
    descriptionDraftSeq.current = requestId;
    setDescriptionDrafting(true);
    setDescriptionDraftStartedAt(Date.now());
    setDescriptionDraftElapsedMs(0);
    setDescriptionDraftMeta(null);
    const timer = window.setTimeout(async () => {
      try {
        await api.draftExternalTeamDescription(
          { name },
          {
            signal: controller.signal,
            onDescriptionDelta: (text) => {
              if (
                controller.signal.aborted
                || descriptionDraftSeq.current !== requestId
              ) return;
              setTeamDescription(text);
              setGeneratedTeamDescription(text);
              lastDescriptionDraftName.current = name;
            },
            onDraft: (draft, phase, meta) => {
              if (controller.signal.aborted || descriptionDraftSeq.current !== requestId) return;
              if (draft.description) {
                setTeamDescription(draft.description);
                setGeneratedTeamDescription(draft.description);
                lastDescriptionDraftName.current = name;
              }
              if (phase === "optimized") {
                setDescriptionDraftMeta(meta);
              } else if (phase === "fallback") {
                setDescriptionDraftMeta(null);
              }
            },
          },
        );
      } catch {
        if (controller.signal.aborted) return;
        // 描述生成失败不阻塞本地草案。
      } finally {
        if (descriptionDraftAbort.current === controller) {
          descriptionDraftAbort.current = null;
        }
        if (descriptionDraftSeq.current === requestId) {
          setDescriptionDrafting(false);
          setDescriptionDraftStartedAt(null);
        }
      }
    }, TEAM_DRAFT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [
    cancelDescriptionDraftRequest,
    generatedTeamDescription,
    teamDescription,
    teamName,
    teamNameComposing,
  ]);

  const useRuntime = (runtime: ExternalRuntime) => {
    if (runtimeStatus(runtime) !== "ready") {
      setMessage({ tab: "runtime", text: runtimeStatusLabel[runtimeStatus(runtime)] });
      return;
    }
    setAgentRuntimeId(runtime.id);
    setAgentName("");
    const models = runtimeModels(runtime);
    const defaultId = String(runtime.metadata?.default_model_id || "");
    setAgentModel(defaultId || models.find((model) => model.default)?.id || models[0]?.id || "");
    setMessage({ tab: "create-agent", text: `已选好 ${runtime.name}，接下来给外援起个称呼吧。` });
    changeTab("create-agent");
  };

  const createAgent = async () => {
    if (!agentRuntimeId) {
      setMessage({ tab: "create-agent", text: "请先选择一位可用外援。" });
      return;
    }
    if (!agentModel) {
      setMessage({ tab: "create-agent", text: "请选择模型" });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const runtime = runtimeById.get(agentRuntimeId);
      const created = await api.createExternalAgent({
        name: agentName.trim() || `${runtime?.name || "新"}外援`,
        runtime_id: agentRuntimeId,
        model: agentModel.trim(),
      });
      await refresh();
      setAgentName("");
      setAgentModel("");
      setAgentRuntimeId("");
      setMessage({ tab: "mine", text: `新外援「${created.name}」已到位，可以派活啦！` });
      changeTab("mine");
    } catch {
      setMessage({ tab: "create-agent", text: "外援添加失败，请检查选择后再试。" });
    } finally {
      setBusy(false);
    }
  };

  const openRoleEditor = (agent: ExternalAgent) => {
    setEditingAgent(agent);
    setEditingRole(memberRoles[agent.id] || (agent.id === teamLeaderId ? defaultLeaderRole : ""));
    setEditingRoleKey(memberRoleKeys[agent.id] || (agent.id === teamLeaderId ? "project_manager" : "fullstack_developer"));
    setRoleError("");
  };

  const roleMetaForKey = (roleKey: string): ExternalTeamRole | undefined =>
    rolePresets.find((role) => role.key === roleKey);

  const generateRoleForAgent = async (agent: ExternalAgent, roleKey: string) => {
    const key = roleKey || "fullstack_developer";
    setRoleGenerating((prev) => ({ ...prev, [agent.id]: true }));
    setRoleError("");
    try {
      const generated = await api.suggestExternalTeamRole({
        name: teamName.trim(),
        description: teamDescription.trim(),
        workflow: teamWorkflow.trim(),
        agent_id: agent.id === CREW_BUILTIN_AGENT_ID ? undefined : agent.id,
        agent_name: agent.name,
        role_key: key,
        current_description: editingRole,
        is_leader: agent.id === teamLeaderId,
      });
      const meta: ExternalTeamRole = {
        key: generated.key,
        label: generated.label,
        description: generated.description,
        capabilities: generated.capabilities || [],
        workflow_lane: generated.workflow_lane,
      };
      setEditingRoleKey(generated.key);
      setEditingRole(generated.role);
      setMemberRoleKeys((prev) => ({ ...prev, [agent.id]: generated.key }));
      setMemberRoleMeta((prev) => ({ ...prev, [agent.id]: meta }));
    } catch {
      const preset = roleMetaForKey(key);
      if (preset) {
        setEditingRoleKey(preset.key);
        setMemberRoleKeys((prev) => ({ ...prev, [agent.id]: preset.key }));
        setMemberRoleMeta((prev) => ({ ...prev, [agent.id]: preset }));
      }
      setRoleError("职责智能生成失败，请稍后再试或手动填写");
    } finally {
      setRoleGenerating((prev) => ({ ...prev, [agent.id]: false }));
    }
  };

  const saveRole = () => {
    if (!editingAgent) return;
    const role = editingRole.trim();
    if (!role) {
      setRoleError("请先填写职责描述，保存后才会加入团队");
      return;
    }
    setSelectedMembers((prev) => ({ ...prev, [editingAgent.id]: true }));
    setMemberRoles((prev) => ({ ...prev, [editingAgent.id]: role }));
    setMemberRoleKeys((prev) => ({ ...prev, [editingAgent.id]: editingRoleKey || "fullstack_developer" }));
    const preset = roleMetaForKey(editingRoleKey);
    if (preset) setMemberRoleMeta((prev) => ({ ...prev, [editingAgent.id]: preset }));
    const keepRoleLocked = teamRolesLocked && (selectedMembers[editingAgent.id] || editingAgent.id === teamLeaderId);
    if (!keepRoleLocked) invalidateFormationDecision();
    setEditingAgent(null);
    setEditingRole("");
    setEditingRoleKey("");
    setRoleError("");
  };

  const removeMember = (agentId: string) => {
    if (agentId === teamLeaderId) return;
    setSelectedMembers((prev) => ({ ...prev, [agentId]: false }));
    setMemberRoles((prev) => {
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
    setMemberRoleKeys((prev) => {
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
    setMemberRoleMeta((prev) => {
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
    invalidateFormationDecision();
    setEditingAgent(null);
  };

  const changeLeader = (agentId: string) => {
    setTeamLeaderId(agentId);
    invalidateFormationDecision();
    if (!agentId) return;
    setRequiredTeamAgentIds((current) => current.filter((id) => id !== agentId));
    setExcludedTeamAgentIds((current) => current.filter((id) => id !== agentId));
    setSelectedMembers((prev) => ({ ...prev, [agentId]: true }));
    setMemberRoles((prev) => ({
      ...prev,
      [agentId]: prev[agentId] || defaultLeaderRole,
    }));
    setMemberRoleKeys((prev) => ({ ...prev, [agentId]: prev[agentId] || "project_manager" }));
    const preset = roleMetaForKey("project_manager");
    if (preset) setMemberRoleMeta((prev) => ({ ...prev, [agentId]: preset }));
  };

  const addCustomTeamCapability = () => {
    const capability = customTeamCapabilityInput.trim();
    if (!capability) return;
    setCustomTeamCapabilities((current) => (
      current.some((item) => item.toLowerCase() === capability.toLowerCase())
        ? current
        : [...current, capability]
    ));
    setCustomTeamCapabilityInput("");
    setShowCustomCapabilityInput(false);
    invalidateFormationDecision();
  };

  const applyTeamSuggestion = (suggestion: Awaited<ReturnType<typeof api.suggestExternalTeam>>) => {
    const selected: Record<string, boolean> = {};
    const roles: Record<string, string> = {};
    const roleKeys: Record<string, string> = {};
    const roleMeta: Record<string, ExternalTeamRole> = {};
    suggestion.members.forEach((member) => {
      selected[member.agent_id] = true;
      roles[member.agent_id] = member.role;
      if (member.role_key) roleKeys[member.agent_id] = member.role_key;
      if (member.role_key) {
        roleMeta[member.agent_id] = {
          key: member.role_key,
          label: member.role_label || member.role_key,
          description: rolePresets.find((role) => role.key === member.role_key)?.description || "",
          capabilities: member.capabilities || [],
          workflow_lane: member.workflow_lane || "build",
        };
      }
    });
    if (suggestion.leader_agent_id) {
      selected[suggestion.leader_agent_id] = true;
      roles[suggestion.leader_agent_id] ||= defaultLeaderRole;
      roleKeys[suggestion.leader_agent_id] ||= "project_manager";
    }
    setTeamLeaderId(suggestion.leader_agent_id);
    setTeamSpec(suggestion.team_spec || null);
    setFormationPlan(suggestion.formation_plan || null);
    setSelectedMembers(selected);
    setMemberRoles(roles);
    setMemberRoleKeys(roleKeys);
    setMemberRoleMeta(roleMeta);
    setTeamRolesLocked(true);
    if (suggestion.workflow) setTeamWorkflow(suggestion.workflow);
    setMessage({ tab: "create-team", text: suggestion.reasons?.join(" ") || "已根据团队目标和约束生成组队建议" });
  };

  const requestTeamSuggestion = async (
    requiredAgentIds: string[],
    forceRequiredAgentIds: string[] = [],
    excludedAgentIds: string[] = excludedTeamAgentIds,
  ) => {
    formationRequestAbort.current?.abort();
    const controller = new AbortController();
    formationRequestAbort.current = controller;
    const requestSeq = ++formationRequestSeq.current;
    const startedAt = Date.now();
    let fastApplied = false;
    setSummoning(true);
    setBusy(true);
    setFormationStatus("fast_loading");
    setFormationStartedAt(startedAt);
    setFormationElapsedMs(0);
    setFormationImprovements([]);
    setFormationAiAttempted(false);
    setMessage(null);
    setStaffingDecision(null);
    setStaffingSelections([]);
    setPendingTemporaryMembers([]);
    try {
      const constraints = buildTeamConstraintText({
        requiredAgentNames: requiredAgentIds.map((id) => agentById.get(id)?.name || id),
        excludedAgentNames: excludedAgentIds.map((id) => agentById.get(id)?.name || id),
        requiredCapabilities: requiredTeamCapabilities,
        customCapabilities: customTeamCapabilities,
      });
      const requestPayload = {
        name: teamName.trim(),
        description: [teamDescription.trim(), constraints && `组队约束：\n${constraints}`].filter(Boolean).join("\n\n"),
        leader_agent_id: teamLeaderId,
        required_agent_ids: requiredAgentIds,
        excluded_agent_ids: excludedAgentIds,
        force_required_agent_ids: forceRequiredAgentIds,
        required_capabilities: requiredTeamCapabilities,
        custom_capabilities: customTeamCapabilities,
      };
      const suggestion = await api.suggestExternalTeamAuto(requestPayload, {
        signal: controller.signal,
        onSuggestion: (snapshot, phase) => {
          if (requestSeq !== formationRequestSeq.current) return;
          const conflicts = snapshot.required_agent_conflicts || [];
          if (snapshot.decision_required && conflicts.length) {
            setTeamConstraintDecision(conflicts);
            if (phase === "final") setFormationStatus("idle");
            return;
          }
          setTeamConstraintDecision(null);
          applyTeamSuggestion(snapshot);
          fastApplied = true;
          if (phase === "final") {
            setFormationStatus(resolveFormationUiStatus(snapshot));
            setFormationImprovements(snapshot.ai_material_improvements || []);
          }
        },
        onStatus: () => {
          if (requestSeq !== formationRequestSeq.current) return;
          setFormationStatus("ai_reviewing");
          setFormationAiAttempted(true);
          setMessage(null);
        },
      });
      if (requestSeq !== formationRequestSeq.current) return;
      const conflicts = suggestion.required_agent_conflicts || [];
      if (suggestion.decision_required && conflicts.length) {
        setTeamConstraintDecision(conflicts);
        setFormationStatus("idle");
        return;
      }
      setTeamConstraintDecision(null);
      applyTeamSuggestion(suggestion);
      setFormationStatus(resolveFormationUiStatus(suggestion));
      setFormationImprovements(suggestion.ai_material_improvements || []);
      if (suggestion.staffing_decision_required && suggestion.staffing_gaps?.length) {
        setStaffingDecision(suggestion.staffing_gaps);
        setStaffingSelections(suggestion.staffing_gaps.map((gap) => ({
          ...gap,
          runtime_id: gap.recommended_runtime_id,
          model_id: gap.recommended_model_id,
        })));
      }
    } catch (error) {
      if (requestSeq !== formationRequestSeq.current) return;
      if ((error as Error).name === "AbortError") return;
      setFormationStatus(fastApplied ? "ready_partial" : "idle");
      setMessage({
        tab: "create-team",
        text: fastApplied ? "初步团队方案已保留，智能检查暂未完成" : "智能组队失败，请补充团队描述或稍后再试",
      });
    } finally {
      if (requestSeq === formationRequestSeq.current) {
        formationRequestAbort.current = null;
        setFormationElapsedMs(Date.now() - startedAt);
        setFormationStartedAt(null);
        setSummoning(false);
        setBusy(false);
      }
    }
  };

  const suggestTeam = () => requestTeamSuggestion(requiredTeamAgentIds);

  const createTeam = async () => {
    if (!teamLeaderId) {
      setMessage({ tab: "create-team", text: "请选择 Leader" });
      return;
    }
    const members = teamAgentOptions
      .filter((agent) => selectedMembers[agent.id] && (memberRoles[agent.id] || "").trim())
      .map((agent, index) => ({
        agent_id: agent.id,
        role: memberRoles[agent.id].trim(),
        role_key: memberRoleKeys[agent.id],
        role_label: memberRoleMeta[agent.id]?.label,
        capabilities: memberRoleMeta[agent.id]?.capabilities,
        assigned_capabilities: memberRoleMeta[agent.id]?.capabilities,
        workflow_lane: memberRoleMeta[agent.id]?.workflow_lane,
        sort_order: index,
      }));
    if (!members.some((member) => member.agent_id === teamLeaderId)) {
      setMessage({ tab: "create-team", text: "Leader 需要填写职责后加入团队" });
      return;
    }
    if (members.length === 1) {
      setSingleLeaderConfirm(true);
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const confirmedFormationPlan = formationPlan ? {
        ...formationPlan,
        leader_agent_id: teamLeaderId,
        members: members.map((member) => {
          const planned = formationPlan.members.find((item) => item.agent_id === member.agent_id);
          return {
            agent_id: member.agent_id,
            role_key: member.role_key || planned?.role_key || "",
            role_label: member.role_label || planned?.role_label || "",
            assigned_capabilities: member.assigned_capabilities || planned?.assigned_capabilities || [],
            responsibility: planned?.responsibility || {},
            responsibility_markdown: member.role,
            selection_source: planned?.selection_source || "user",
            locked: planned?.locked ?? true,
            selection_reason: planned?.selection_reason || "用户确认的团队成员。",
          };
        }),
      } : undefined;
      const created = await api.createExternalTeam({
        name: teamName.trim() || "我的团队",
        description: teamDescription.trim(),
        leader_agent_id: teamLeaderId,
        instructions: teamWorkflow.trim(),
        team_spec: teamSpec || undefined,
        formation_plan: confirmedFormationPlan,
        temporary_members: pendingTemporaryMembers.map((member) => ({
          gap_id: member.gap_id,
          name: `${teamName.trim() || "团队"} · 临时${member.role_label}`,
          role_key: member.role_key,
          required_capabilities: member.required_capabilities,
          responsibility_focus: member.responsibility_focus,
          reason: member.reason,
          runtime_id: member.runtime_id,
          model_id: member.model_id,
        })),
        members,
      });
      await refresh();
      setTeamName("");
      setTeamNameComposing(false);
      setTeamDescription("");
      setGeneratedTeamDescription("");
      setDescriptionDraftMeta(null);
      setTeamLeaderId(CREW_BUILTIN_AGENT_ID);
      setTeamWorkflow("");
      setTeamSpec(null);
      setFormationPlan(null);
      setStaffingDecision(null);
      setStaffingSelections([]);
      setPendingTemporaryMembers([]);
      setRequiredTeamAgentIds([]);
      setExcludedTeamAgentIds([]);
      setRequiredTeamCapabilities([]);
      setCustomTeamCapabilities([]);
      setCustomTeamCapabilityInput("");
      setShowCustomCapabilityInput(false);
      setShowTeamConstraints(false);
      setTeamConstraintDecision(null);
      setSelectedMembers({});
      setMemberRoles({});
      setMemberRoleKeys({});
      setMemberRoleMeta({});
      setTeamRolesLocked(false);
      setFormationStatus("idle");
      setFormationStartedAt(null);
      setFormationElapsedMs(0);
      setFormationImprovements([]);
      setFormationAiAttempted(false);
      setActiveTeamId(created.id);
      setMessage({ tab: "mine", text: `团队「${created.name}」集合完毕，开个任务试试吧！` });
      changeTab("mine");
    } catch {
      setMessage({ tab: "create-team", text: "团队没组成功，请检查配置后再试。" });
    } finally {
      setBusy(false);
    }
  };

  const deleteAgent = async (agent: ExternalAgent) => {
    if (!window.confirm(`移除外援「${agent.name}」？`)) return;
    setBusy(true);
    try {
      await api.deleteExternalAgent(agent.id);
      await refresh();
      setMessage({ tab: "mine", text: `已删除 ${agent.name}` });
    } catch {
      setMessage({ tab: "mine", text: "移除失败：这位外援可能还在团队中。" });
    } finally {
      setBusy(false);
    }
  };

  const deleteTeam = async (team: ExternalTeam) => {
    if (!window.confirm(`删除团队「${team.name}」？`)) return;
    setBusy(true);
    try {
      await api.deleteExternalTeam(team.id);
      await refresh();
      if (activeTeamId === team.id) setActiveTeamId("");
      setMessage({ tab: "mine", text: `已删除团队 ${team.name}` });
    } catch {
      setMessage({ tab: "mine", text: "删除团队失败" });
    } finally {
      setBusy(false);
    }
  };

  const renderRuntime = () => (
    <div className="agents-section">
      <div className="agents-section__bar">
        <div className="agents-section__intro">
          <h2>发现外援</h2>
          <p>Crew 会寻找这台电脑里已经安装、可以协作的 AI 工具。</p>
        </div>
        <button
          className="agent-btn agent-btn--dark"
          data-agents-guide-target="scan"
          disabled={busy}
          onClick={scanRuntimes}
        >
          {runtimeScanning ? "正在找…" : "再找找"}
        </button>
      </div>
      {runtimes.length === 0 ? (
        <div className="agents-empty agents-empty--wide">
          {runtimeScanning
            ? "正在看看这台电脑上有哪些外援…"
            : "这次还没找到外援。确认已安装支持的 AI 工具后，再找找。"}
        </div>
      ) : (
        <div className="agents-list">
          {runtimes.map((runtime) => (
            <article className="agent-card" key={runtime.id}>
              <div className="agent-card__main">
                <div className="agent-card__title">
                  <span
                    className={`runtime-status-dot runtime-status-dot--${runtimeStatus(runtime)}`}
                    title={runtimeStatusLabel[runtimeStatus(runtime)]}
                    aria-label={runtimeStatusLabel[runtimeStatus(runtime)]}
                  />
                  <span className="agent-pill">{runtime.provider}</span>
                  {runtime.name}
                </div>
                <div className="agent-card__meta">
                  {runtimeStatusLabel[runtimeStatus(runtime)]}
                  {runtime.version ? ` · ${runtime.version}` : ""}
                </div>
              </div>
              <button
                className="pixel-use-btn"
                type="button"
                disabled={runtimeStatus(runtime) !== "ready"}
                onClick={() => useRuntime(runtime)}
                title={runtimeStatus(runtime) === "ready" ? `使用 ${runtime.name}` : runtimeStatusLabel[runtimeStatus(runtime)]}
              >
                <span className="pixel-use-btn__spark" aria-hidden="true" />
                使用
              </button>
            </article>
          ))}
        </div>
      )}
    </div>
  );

  const renderCreateAgent = () => (
    <div className="agents-section agents-form">
      <div className="agents-section__intro">
        <h2>添加外援</h2>
        <p>选好外援和模型，再起个好记的称呼。以后派活时直接叫它。</p>
      </div>
      {agentRuntimeId && runtimeById.get(agentRuntimeId) && (
        <div className="runtime-prefill-note">
          <span className="pixel-use-btn__spark" aria-hidden="true" />
          已选择 {runtimeById.get(agentRuntimeId)?.name}
        </div>
      )}
      <div className="agents-form__field" data-agents-guide-target="runtime-select">
        <span>可用外援</span>
        <AgentFormSelect
          value={agentRuntimeId}
          placeholder="请选择外援"
          options={runtimeOptions}
          onChange={(runtimeId) => {
            setAgentRuntimeId(runtimeId);
            const runtime = runtimeById.get(runtimeId);
            const models = runtimeModels(runtime);
            const defaultId = String(runtime?.metadata?.default_model_id || "");
            setAgentModel(defaultId || models.find((model) => model.default)?.id || models[0]?.id || "");
          }}
        />
      </div>
      <label>
        <span>外援称呼</span>
        <input value={agentName} onChange={(event) => setAgentName(event.target.value)} placeholder="例如：调研搭档" />
      </label>
      <div className="agents-form__field">
        <span>使用模型</span>
        <AgentFormSelect
          value={agentModel}
          placeholder="请选择模型"
          options={runtimeModelOptions}
          onChange={setAgentModel}
        />
      </div>
      <button className="agent-btn agent-btn--dark" disabled={busy || !agentRuntimeId || !agentModel} onClick={createAgent}>
        加入我的外援
      </button>
    </div>
  );

  const renderCreateTeam = () => {
    const reviewAgents = teamAgentOptions
      .filter((agent) => selectedMembers[agent.id])
      .sort((a, b) => {
        if (a.id === teamLeaderId) return -1;
        if (b.id === teamLeaderId) return 1;
        const orderA = formationPlan?.members.findIndex((member) => member.agent_id === a.id) ?? 999;
        const orderB = formationPlan?.members.findIndex((member) => member.agent_id === b.id) ?? 999;
        return orderA - orderB;
      });
    const coveragePercent = Math.round((formationPlan?.confidence.coverage || 0) * 100);
    const confidencePercent = Math.round((formationPlan?.confidence.overall || 0) * 100);
    const reviewAgentIds = new Set(reviewAgents.map((agent) => agent.id));
    const requiredAgentOptions = leaderOptions.filter((option) => (
      option.value !== teamLeaderId
      && !requiredTeamAgentIds.includes(option.value)
      && !excludedTeamAgentIds.includes(option.value)
    ));
    const excludedAgentOptions = leaderOptions.filter((option) => (
      option.value !== teamLeaderId
      && !excludedTeamAgentIds.includes(option.value)
      && !requiredTeamAgentIds.includes(option.value)
    ));
    const constraintCount = requiredTeamAgentIds.length
      + excludedTeamAgentIds.length
      + requiredTeamCapabilities.length
      + customTeamCapabilities.length;
    const formationRunning = formationStatus === "fast_loading" || formationStatus === "ai_reviewing";
    const formationFinished = formationStatus.startsWith("ready_");
    const formationResultText = formationStatus === "ready_improved"
      ? "团队方案已优化，成员分工和能力覆盖已经更新。"
      : formationStatus === "ready_unchanged"
        ? "团队方案已检查，当前成员和分工已经合适，无需调整。"
        : "初步团队方案已生成，智能检查暂未完成，你仍然可以使用当前方案。";
    const formationProgress = formationStatus !== "idle" && (
      <section className={`formation-progress formation-progress--${formationStatus}`} aria-live="polite">
        <div className="formation-progress__head">
          <div>
            <strong>{teamName.trim() || "我的团队"}</strong>
            <span>
              {formationRunning
                ? `正在智能组队 · ${formatTeamDraftElapsed(formationElapsedMs)}`
                : formationResultText}
            </span>
          </div>
          {formationStatus === "ready_partial" && (
            <button className="agent-btn" type="button" disabled={busy} onClick={suggestTeam}>重新检查</button>
          )}
        </div>
        <ol className="formation-progress__steps">
          <li className={formationStatus === "fast_loading" ? "is-active" : "is-done"}>
            <i aria-hidden="true">{formationStatus === "fast_loading" ? "·" : "✓"}</i>
            <span>生成初步方案</span>
          </li>
          <li className={formationStatus === "ai_reviewing" ? "is-active" : formationStatus === "ready_partial" ? "is-warning" : formationFinished ? "is-done" : ""}>
            <i aria-hidden="true">{formationStatus === "ready_partial" ? "!" : formationFinished ? "✓" : "·"}</i>
            <span>
              {formationStatus === "ai_reviewing" || (formationFinished && formationAiAttempted)
                ? `智能检查优化 · ${formatTeamDraftElapsed(formationElapsedMs)}`
                : "智能检查优化"}
            </span>
          </li>
          <li className={formationFinished ? "is-done" : ""}>
            <i aria-hidden="true">{formationFinished ? "✓" : "·"}</i>
            <span>方案已就绪</span>
          </li>
        </ol>
        {formationStatus === "ready_improved" && formationImprovements.length > 0 && (
          <div className="formation-progress__improvements">
            {formationImprovements.slice(0, 3).map((improvement) => <span key={improvement}>✓ {improvement}</span>)}
          </div>
        )}
      </section>
    );

    return (
      <div className="agents-section agents-form team-create">
        <div className="team-create__heading">
          <span className="team-mark team-mark--hero team-create__team-mark" aria-hidden="true">
            <i />
            <i />
          </span>
          <div>
            <h2>{teamRolesLocked ? "确认阵容" : "组建团队"}</h2>
            <p>{teamRolesLocked ? "阵容已经配好，看看成员和分工是否合适。" : "告诉我团队要做什么，成员和职责交给我来搭。"}</p>
          </div>
          <div className="team-create__steps" aria-label="创建进度">
            <span className={!teamRolesLocked ? "is-active" : "is-done"}>1 定义团队</span>
            <span className={teamRolesLocked ? "is-active" : ""}>2 确认阵容</span>
          </div>
        </div>

        {!teamRolesLocked ? (
          <div className="team-create__setup">
            <label>
              <span>团队名称</span>
              <input
                value={teamName}
                onCompositionStart={() => {
                  setTeamNameComposing(true);
                  cancelDescriptionDraftRequest();
                }}
                onCompositionEnd={() => setTeamNameComposing(false)}
                onChange={(event) => {
                  cancelDescriptionDraftRequest();
                  setDescriptionDraftMeta(null);
                  setTeamName(event.target.value);
                  invalidateFormationDecision();
                }}
                placeholder="例如：产品研发小队"
                autoFocus
              />
              <small>先起个名字，团队目标参考会自动补上。</small>
            </label>

            <label>
              <span>团队目标 <em>系统生成参考</em></span>
              <textarea
                className={descriptionDrafting ? "is-generating" : ""}
                value={teamDescription}
                onChange={(event) => {
                  cancelDescriptionDraftRequest();
                  setDescriptionDraftMeta(null);
                  setTeamDescription(event.target.value);
                  setGeneratedTeamDescription("");
                  invalidateFormationDecision();
                  lastDescriptionDraftName.current = teamName.trim();
                }}
                rows={3}
                placeholder={descriptionDrafting && !teamDescription.trim()
                  ? `正在生成目标参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}）…`
                  : "1. 负责范围\n2. 所需能力\n3. 交付结果\n4. 验收标准"}
              />
              <small aria-live="polite">
                {descriptionDrafting
                  ? `正在生成参考（${formatTeamDraftElapsed(descriptionDraftElapsedMs)}），无需等待也可以继续。`
                  : descriptionDraftMeta
                    ? `参考已生成 · ${descriptionDraftMeta.cacheHit ? "缓存" : formatLlmElapsed(descriptionDraftMeta.llmElapsedMs || 0)}`
                    : "系统会按四个要点生成，你只需按需修改。"}
              </small>
            </label>

            <div className="agents-form__field team-create__leader">
              <span>Leader <em>已默认推荐</em></span>
              <AgentFormSelect
                value={teamLeaderId}
                placeholder="使用系统推荐 Leader"
                options={leaderOptions}
                onChange={changeLeader}
              />
              <small>Crew 会负责拆任务、盯进度和收口；你也可以换成其他外援。</small>
            </div>

            <div className="team-constraints">
              <button
                className="team-constraints__toggle"
                type="button"
                aria-expanded={showTeamConstraints}
                onClick={() => setShowTeamConstraints((current) => !current)}
              >
                <span><i aria-hidden="true">+</i> 组队约束（选填）</span>
                <small>{constraintCount ? `已设置 ${constraintCount} 项` : "不设置也可以"}</small>
              </button>
              {showTeamConstraints && (
                <div className="team-constraints__body">
                  <p>只有明确的人选或能力要求才需要设置，其他情况直接智能组队即可。</p>
                  <div className="team-constraint-row">
                    <div>
                      <strong>必须包含</strong>
                      <small>这些外援一定会进入团队</small>
                    </div>
                    <AgentFormSelect
                      value=""
                      placeholder="选择外援"
                      options={requiredAgentOptions}
                      onChange={(agentId) => {
                        if (agentId) setRequiredTeamAgentIds((current) => [...current, agentId]);
                        invalidateFormationDecision();
                      }}
                    />
                    <div className="team-constraint-values">
                      {requiredTeamAgentIds.map((agentId) => (
                        <button type="button" key={agentId} onClick={() => {
                          setRequiredTeamAgentIds((current) => current.filter((id) => id !== agentId));
                          invalidateFormationDecision();
                        }}>
                          {agentById.get(agentId)?.name || agentId} ×
                        </button>
                      ))}
                      {!requiredTeamAgentIds.length && <span>未指定</span>}
                    </div>
                  </div>
                  <div className="team-constraint-row">
                    <div>
                      <strong>排除成员</strong>
                      <small>这些外援不会进入团队</small>
                    </div>
                    <AgentFormSelect
                      value=""
                      placeholder="选择外援"
                      options={excludedAgentOptions}
                      onChange={(agentId) => {
                        if (agentId) setExcludedTeamAgentIds((current) => [...current, agentId]);
                        invalidateFormationDecision();
                      }}
                    />
                    <div className="team-constraint-values">
                      {excludedTeamAgentIds.map((agentId) => (
                        <button type="button" key={agentId} onClick={() => {
                          setExcludedTeamAgentIds((current) => current.filter((id) => id !== agentId));
                          invalidateFormationDecision();
                        }}>
                          {agentById.get(agentId)?.name || agentId} ×
                        </button>
                      ))}
                      {!excludedTeamAgentIds.length && <span>未指定</span>}
                    </div>
                  </div>
                  <div className="team-constraint-row team-constraint-row--capabilities">
                    <div>
                      <strong>必需能力</strong>
                      <small>团队里必须有人负责这些工作</small>
                    </div>
                    <div className="team-constraint-chips">
                      {TEAM_REQUIRED_CAPABILITIES.map((capability) => {
                        const active = requiredTeamCapabilities.includes(capability.key);
                        return (
                          <button
                            type="button"
                            className={active ? "is-active" : ""}
                            aria-pressed={active}
                            key={capability.key}
                            onClick={() => {
                              setRequiredTeamCapabilities((current) => active
                                ? current.filter((key) => key !== capability.key)
                                : [...current, capability.key]);
                              invalidateFormationDecision();
                            }}
                          >
                            {active ? "✓ " : ""}{capability.label}
                          </button>
                        );
                      })}
                      {customTeamCapabilities.map((capability) => (
                        <button
                          type="button"
                          className="is-active is-custom"
                          title="自定义能力要求"
                          key={capability}
                          onClick={() => {
                            setCustomTeamCapabilities((current) => current.filter((item) => item !== capability));
                            invalidateFormationDecision();
                          }}
                        >
                          {capability} ×
                        </button>
                      ))}
                      <button
                        type="button"
                        className="team-capability-add"
                        onClick={() => setShowCustomCapabilityInput((current) => !current)}
                      >
                        + 自定义能力
                      </button>
                    </div>
                    {showCustomCapabilityInput && (
                      <div className="team-capability-custom-input">
                        <input
                          value={customTeamCapabilityInput}
                          onChange={(event) => setCustomTeamCapabilityInput(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              addCustomTeamCapability();
                            }
                          }}
                          placeholder="例如：数据分析、安全审查"
                          autoFocus
                        />
                        <button type="button" disabled={!customTeamCapabilityInput.trim()} onClick={addCustomTeamCapability}>添加</button>
                      </div>
                    )}
                    {customTeamCapabilities.length > 0 && (
                      <small className="team-capability-note">你填写的能力会一起提交，系统会从现有外援中尽量匹配。</small>
                    )}
                  </div>
                </div>
              )}
            </div>

            <div className="team-create__primary-action">
              <div>
                <strong>智能组队</strong>
                <span>根据目标和约束自动生成合适阵容。</span>
              </div>
              <button className="pixel-summon-btn" disabled={busy || !teamName.trim()} onClick={suggestTeam}>
                {summoning ? "智能组队中…" : "智能组队"}
              </button>
            </div>
            {formationProgress}
            {summoning && <div className="summon-loading" role="status" aria-live="polite" />}
          </div>
        ) : (
          <div className="team-create__review">
            {formationProgress}
            <div className="team-formation-summary">
              <div>
                <strong>{teamName || "我的团队"}</strong>
                <p>{teamDescription || "系统已根据团队目标完成组队。"}</p>
              </div>
              <div className="team-formation-metrics">
                <span><b>{reviewAgents.length}</b> 名成员</span>
                <span><b>{coveragePercent}%</b> 能力覆盖</span>
                <span><b>{confidencePercent}%</b> 组队置信度</span>
              </div>
            </div>

            {constraintCount > 0 && (
              <div className="team-constraint-ack" aria-label="已考虑的组队约束">
                <div className="team-constraint-ack__head">
                  <span aria-hidden="true">✓</span>
                  <div>
                    <strong>已考虑你的组队约束</strong>
                    <small>下面这些要求已参与成员筛选和能力分工。</small>
                  </div>
                </div>
                <div className="team-constraint-ack__items">
                  {requiredTeamAgentIds.map((agentId) => (
                    <span className={reviewAgentIds.has(agentId) ? "is-applied" : "is-missing"} key={`required-${agentId}`}>
                      {reviewAgentIds.has(agentId) ? "已保留" : "未保留"} · {agentById.get(agentId)?.name || agentId}
                    </span>
                  ))}
                  {excludedTeamAgentIds.map((agentId) => (
                    <span className={!reviewAgentIds.has(agentId) ? "is-applied" : "is-missing"} key={`excluded-${agentId}`}>
                      {!reviewAgentIds.has(agentId) ? "已排除" : "仍在团队"} · {agentById.get(agentId)?.name || agentId}
                    </span>
                  ))}
                  {requiredTeamCapabilities.map((capability) => {
                    const covered = formationPlan?.coverage.covered.includes(capability) || false;
                    return (
                      <span className={covered ? "is-applied is-capability" : "is-missing is-capability"} key={`capability-${capability}`}>
                        {covered ? "已覆盖" : "未覆盖"} · {TEAM_CAPABILITY_LABELS[capability] || capability}
                      </span>
                    );
                  })}
                  {customTeamCapabilities.map((capability) => (
                    <span className="is-considered is-capability" key={`custom-${capability}`}>
                      已纳入目标 · {capability}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="team-review-section">
              <div className="team-review-section__head">
                <div>
                  <strong>成员与职责</strong>
                  <span>点击成员卡片可查看或修改完整职责。</span>
                </div>
                <em>{reviewAgents.length} 人</em>
              </div>
              <div className="team-review-grid">
                {reviewAgents.map((agent) => {
                  const planned = formationPlan?.members.find((member) => member.agent_id === agent.id);
                  const roleLabel = memberRoleMeta[agent.id]?.label || planned?.role_label || "协作成员";
                  const capabilities = planned?.assigned_capabilities || memberRoleMeta[agent.id]?.capabilities || [];
                  return (
                    <button
                      className={"team-review-member" + (agent.id === teamLeaderId ? " is-leader" : "")}
                      type="button"
                      key={agent.id}
                      disabled={formationRunning}
                      onClick={() => openRoleEditor(agent)}
                    >
                      <span className="pixel-badge">{agent.display_badge || "?"}</span>
                      <span className="team-review-member__main">
                        <strong>{agent.name}{agent.id === teamLeaderId && <em>Leader</em>}</strong>
                        <small>{roleLabel}</small>
                        <span className="team-review-member__caps">
                          {capabilities.slice(0, 4).map((capability) => <i key={capability}>{capability}</i>)}
                        </span>
                      </span>
                      <span className="team-review-member__edit">职责 ›</span>
                    </button>
                  );
                })}
              </div>
            </div>

            {pendingTemporaryMembers.length > 0 && (
              <div className="team-review-section team-temporary-review">
                <div className="team-review-section__head">
                  <div>
                    <strong>待创建的临时成员</strong>
                    <span>只会在确认创建团队时生成，并由该团队管理生命周期。</span>
                  </div>
                  <em>{pendingTemporaryMembers.length} 人</em>
                </div>
                <div className="team-constraint-confirm__members">
                  {pendingTemporaryMembers.map((member) => (
                    <div key={member.gap_id}>
                      <strong>{member.role_label}</strong>
                      <span>{member.reason}</span>
                      <small>
                        {runtimeById.get(member.runtime_id)?.name || member.runtime_id}
                        {" · "}
                        {staffingModelOptions(member.runtime_id).find((model) => model.value === member.model_id)?.label || member.model_id}
                      </small>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {formationPlan && (
              <div className="team-review-section">
                <div className="team-review-section__head">
                  <div>
                    <strong>能力覆盖</strong>
                    <span>系统按最小充分原则选择成员。</span>
                  </div>
                </div>
                <div className="team-coverage-chips">
                  {formationPlan.coverage.required.map((capability) => (
                    <span className={formationPlan.coverage.covered.includes(capability) ? "is-covered" : "is-missing"} key={capability}>
                      {formationPlan.coverage.covered.includes(capability) ? "✓" : "!"} {capability}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {teamWorkflow && (
              <details className="team-workflow-preview">
                <summary>默认协作方式 <span>正式开工时会按任务自动拆活</span></summary>
                <div className="md-body"><MarkdownContent content={teamWorkflow} /></div>
              </details>
            )}

            <div className="team-create__review-actions">
              <button className="agent-btn" type="button" disabled={formationRunning} onClick={() => invalidateFormationDecision()}>返回修改</button>
              <button className="agent-btn" type="button" disabled={busy} onClick={suggestTeam}>重新组队</button>
              <button className="agent-btn agent-btn--dark" disabled={busy || !teamLeaderId} onClick={createTeam}>确认组队</button>
            </div>
          </div>
        )}
      </div>
    );
  };

  const renderMine = () => (
    <div className="agents-section">
      <div className="agents-section__bar">
        <h2>我的外援</h2>
        <span className="agents-count">{agents.length} 个</span>
      </div>
      {agents.length === 0 ? (
        <div className="agents-empty agents-empty--action">
          <div>
            <strong>还没有外援</strong>
            <span>先看看这台电脑上有哪些 AI 工具能来帮忙。</span>
          </div>
          <button className="agent-btn" type="button" onClick={() => changeTab("runtime")}>发现外援</button>
        </div>
      ) : (
        <div className="mine-agent-grid">
          {agents.map((agent, index) => {
            const runtime = runtimeById.get(agent.runtime_id);
            return (
              <article className="mine-agent-card" key={agent.id}>
                <div className="mine-card-quick-actions">
                  <button
                    className="agent-btn agent-btn--dark"
                    data-agents-guide-target={index === 0 ? "assign" : undefined}
                    onClick={() => onAssignAgent(agent)}
                  >
                    派活
                  </button>
                  <button
                    className="pixel-action-btn"
                    disabled={busy}
                    onClick={() => deleteAgent(agent)}
                    title="移除外援"
                    aria-label="移除外援"
                  >
                    ×
                  </button>
                </div>
                <div className="pixel-avatar" aria-hidden="true">
                  <span>{agent.display_badge || "?"}</span>
                </div>
                <div className="mine-agent-card__body">
                  <div className="mine-agent-card__head">
                    <strong>{agent.name}</strong>
                    <span>{agent.provider}</span>
                  </div>
                  <div className="mine-agent-card__meta">
                    {agent.model || "默认模型"} · {runtimeLabel(runtime)}
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <div className="agents-section__bar agents-section__bar--spaced">
        <h2>我的团队</h2>
        <span className="agents-count">{teams.length} 个</span>
      </div>
      {teams.length === 0 ? (
        <div className="agents-empty agents-empty--action">
          <div>
            <strong>还没有团队</strong>
            <span>任务需要分工、并行或复核时，就把外援们组起来。</span>
          </div>
          <button className="agent-btn" type="button" onClick={() => changeTab("create-team")}>组建团队</button>
        </div>
      ) : (
        <div className="team-tile-grid">
          {teams.map((team) => {
            const leader = agentById.get(team.leader_agent_id);
            return (
              <article
                key={team.id}
                className="team-tile"
                onClick={() => setActiveTeamId(team.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setActiveTeamId(team.id);
                  }
                }}
                role="button"
                tabIndex={0}
              >
                <div className="team-tile__quick-actions">
                  <button
                    className="agent-btn agent-btn--dark"
                    onClick={(event) => {
                      event.stopPropagation();
                      onAssignTeam(team);
                    }}
                  >
                    派活
                  </button>
                  <button
                    className="pixel-action-btn"
                    disabled={busy}
                    onClick={(event) => {
                      event.stopPropagation();
                      deleteTeam(team);
                    }}
                    title="删除团队"
                    aria-label="删除团队"
                  >
                    ×
                  </button>
                </div>
                <div className="team-tile__top">
                  <div className="pixel-avatar pixel-avatar--leader" aria-hidden="true">
                    <span>{leader?.display_badge || "?"}</span>
                  </div>
                  <div>
                    <strong>{team.name}</strong>
                    <p>Leader: {leaderName(team)}</p>
                  </div>
                </div>
                <div className="team-badge-row">
                  {team.members.slice(0, 5).map((member) => (
                    <span
                      className="pixel-badge"
                      title={`${member.agent_name || member.agent_id}: ${member.role || "未填写职责"}`}
                      key={member.id}
                    >
                      {member.display_badge || "?"}
                    </span>
                  ))}
                  {team.members.length > 5 && <em>+{team.members.length - 5}</em>}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );

  const leaderInActiveTeam = activeTeam ? agentById.get(activeTeam.leader_agent_id) : null;
  const agentsGuidePortal = typeof document === "undefined" || guideMode === "hidden"
    ? null
    : createPortal(
        <div
          className={guideMode === "tour"
            ? "agents-guide-portal agents-guide-portal--tour"
            : "agents-guide-portal agents-guide-portal--right"}
          data-agents-guide-portal
          onWheel={(event) => {
            const panel = document.querySelector<HTMLElement>(".agents-panel");
            if (!panel) return;
            event.preventDefault();
            panel.scrollBy({ top: event.deltaY, behavior: "auto" });
          }}
        >
          {guideMode === "welcome" ? (
            <aside className="agents-guide-bubble agents-guide-bubble--welcome" role="dialog" aria-label="外援中心新手引导">
              <div className="agents-guide-bubble__top">
                <span className="agents-guide-bubble__spark" aria-hidden="true" />
                <span>外援小向导</span>
                <button type="button" onClick={finishGuide} aria-label="稍后再看外援引导" title="稍后再说">×</button>
              </div>
              <strong>第一次来外援中心？</strong>
              <p>用 30 秒认识发现、添加和派活，之后你可以随时点右上角“?”再看一遍。</p>
              <div className="agents-guide-bubble__actions agents-guide-bubble__actions--welcome">
                <button type="button" className="agents-guide-bubble__dismiss" onClick={finishGuide}>稍后再说</button>
                <button type="button" className="agents-guide-bubble__action" onClick={startGuide}>开始看看</button>
              </div>
            </aside>
          ) : (
            <>
              <div className="agents-guide-mask" aria-hidden="true" />
              <div
                className="agents-guide-highlight"
                aria-hidden="true"
                hidden={!guideLayout?.highlight}
                style={guideLayout?.highlight ?? undefined}
              />
              <aside
                ref={guideBubbleRef}
                className="agents-guide-bubble"
                role="dialog"
                aria-label={`外援中心引导：${guideDefinition.progress}`}
                style={guideLayout ? guideLayout.tooltip : { visibility: "hidden" }}
              >
                <div className="agents-guide-bubble__top">
                  <span className="agents-guide-bubble__spark" aria-hidden="true" />
                  <span>{guideDefinition.progress}</span>
                  <button type="button" onClick={finishGuide} aria-label="跳过外援引导" title="跳过">×</button>
                </div>
                <strong>{guideDefinition.title}</strong>
                <p>{guideDefinition.body}</p>
                <div className="agents-guide-bubble__actions">
                  <button type="button" className="agents-guide-bubble__locate" onClick={locateGuideTarget}>定位到操作</button>
                  <div className="agents-guide-bubble__steps">
                    <button type="button" className="agents-guide-bubble__quiet" onClick={finishGuide}>跳过</button>
                    {guideStep > 1 && (
                      <button
                        type="button"
                        className="agents-guide-bubble__dismiss"
                        onClick={() => moveGuide((guideStep - 1) as AgentsGuideStepNumber)}
                      >
                        上一步
                      </button>
                    )}
                    <button
                      type="button"
                      className="agents-guide-bubble__action"
                      onClick={() => guideStep === 3
                        ? finishGuide()
                        : moveGuide((guideStep + 1) as AgentsGuideStepNumber)}
                    >
                      {guideStep === 3 ? "完成" : "下一步"}
                    </button>
                  </div>
                </div>
              </aside>
            </>
          )}
        </div>,
        document.body,
      );

  return (
    <section className="agents-panel">
      <header className="agents-panel__head">
        <div>
          <h1>外援中心</h1>
          <p>Crew 会找到本机能帮上忙的 AI 工具。加为外援后，可以单独派活，也能组成团队分工协作。</p>
        </div>
        <button
          className="agents-guide-replay"
          type="button"
          onClick={startGuide}
          title="重新查看外援引导"
          aria-label="重新查看外援引导"
        >
          ?
        </button>
      </header>
      <div className="agents-tabs">
        {(Object.keys(TABS) as TabKey[]).map((key) => (
          <button
            key={key}
            className={"agents-tab" + (tab === key ? " active" : "")}
            data-agents-tab={key}
            onClick={() => changeTab(key)}
          >
            {TABS[key]}
          </button>
        ))}
      </div>
      {message?.tab === tab && <div className="agents-message">{message.text}</div>}
      {tab === "runtime" && renderRuntime()}
      {tab === "create-agent" && renderCreateAgent()}
      {tab === "create-team" && renderCreateTeam()}
      {tab === "mine" && renderMine()}
      {agentsGuidePortal}

      {activeTeam && (
        <div className="team-modal-backdrop" onClick={() => setActiveTeamId("")}>
          <div className="team-modal" onClick={(event) => event.stopPropagation()}>
            <div className="team-modal__head">
              <div className="team-modal__title">
                <div className="pixel-avatar pixel-avatar--leader" aria-hidden="true">
                  <span>{leaderInActiveTeam?.display_badge || "?"}</span>
                </div>
                <div>
                  <span>团队</span>
                  <strong>{activeTeam.name}</strong>
                  <p>Leader: {leaderInActiveTeam?.name || leaderName(activeTeam)}</p>
                </div>
              </div>
              <button
                className="agent-icon-btn"
                type="button"
                title="关闭"
                aria-label="关闭团队详情"
                onClick={() => setActiveTeamId("")}
              >
                ×
              </button>
            </div>
            {activeTeam.description && (
              <section className="team-modal__section">
                <h3>团队描述</h3>
                <p className="team-modal__plain">{activeTeam.description}</p>
              </section>
            )}
            {activeTeam.instructions && (
              <section className="team-modal__section">
                <h3>团队工作流</h3>
                <div className="md-body team-modal__md">
                  <MarkdownContent content={activeTeam.instructions} />
                </div>
              </section>
            )}
            <section className="team-modal__section">
              <h3>成员职责</h3>
              <div className="team-modal__members">
                {activeTeamMembers.map((member) => {
                  const isLeader = member.agent_id === activeTeam.leader_agent_id;
                  return (
                    <article className={"team-modal-member" + (isLeader ? " is-leader" : "")} key={member.id}>
                      <div className="team-modal-member__head">
                        <span className="pixel-badge">{member.display_badge || "?"}</span>
                        <div>
                          <strong>
                            {member.agent_name || member.agent_id}
                            {isLeader && <span className="pixel-flag" title="Leader" aria-label="Leader" />}
                          </strong>
                          <p>{member.agent_provider || "external"}{isLeader ? " · Leader" : ""}</p>
                          {member.role_label && <p>{member.role_label} · {member.workflow_lane || "workflow"}</p>}
                        </div>
                      </div>
                      <div className="md-body team-modal__md">
                        <MarkdownContent content={member.role || "未填写职责"} />
                      </div>
                    </article>
                  );
                })}
              </div>
            </section>
          </div>
        </div>
      )}

      {editingAgent && (() => {
        const roleSelectionLocked = teamRolesLocked && (selectedMembers[editingAgent.id] || editingAgent.id === teamLeaderId);
        return (
        <div className="role-popover-backdrop" onClick={() => setEditingAgent(null)}>
          <div className="role-popover" onClick={(event) => event.stopPropagation()}>
            <div className="role-popover__head">
              <div>
                <span>{editingAgent.provider}</span>
                <strong>{editingAgent.name}</strong>
              </div>
              <button className="agent-icon-btn" onClick={() => setEditingAgent(null)}>
                关闭
              </button>
            </div>
            <div className="agents-form__field role-popover__select">
              <span>角色</span>
              <AgentFormSelect
                value={editingRoleKey}
                placeholder="请选择角色"
                options={roleOptions}
                disabled={roleSelectionLocked}
                onChange={(value) => {
                  if (!editingAgent || !value) return;
                  setEditingRoleKey(value);
                  generateRoleForAgent(editingAgent, value);
                }}
              />
              {roleSelectionLocked && (
                <em className="role-popover__hint">智能组队后角色已由槽位确定，可直接修改职责描述。</em>
              )}
              {editingAgent && roleGenerating[editingAgent.id] && (
                <em className="role-popover__hint">正在按团队目标生成职责…</em>
              )}
            </div>
            <textarea
              value={editingRole}
              onChange={(event) => {
                setEditingRole(event.target.value);
                if (roleError) setRoleError("");
              }}
              placeholder="用 Markdown 写这个成员的工作原则、职责、协作关系、输出格式和下一动作。"
              autoFocus
            />
            {roleError && <div className="role-popover__error">{roleError}</div>}
            <div className="role-popover__actions">
              {editingAgent.id !== teamLeaderId && (
                <button className="agent-icon-btn" onClick={() => removeMember(editingAgent.id)}>
                  移除成员
                </button>
              )}
              <button className="agent-btn agent-btn--dark" onClick={saveRole}>
                保存
              </button>
            </div>
          </div>
        </div>
        );
      })()}

      {teamConstraintDecision && (
        <div className="role-popover-backdrop" onClick={() => setTeamConstraintDecision(null)}>
          <div className="team-constraint-confirm" role="dialog" aria-modal="true" aria-labelledby="team-constraint-confirm-title" onClick={(event) => event.stopPropagation()}>
            <div className="team-constraint-confirm__head">
              <div>
                <strong id="team-constraint-confirm-title">指定成员存在明确的能力差距</strong>
                <p>系统只在能力画像证据充分时提示，不会把“能力未知”当成“不具备”。请选择按能力筛选，或遵循你的原始指定继续保留。</p>
              </div>
              <button className="agent-icon-btn" type="button" aria-label="关闭" onClick={() => setTeamConstraintDecision(null)}>×</button>
            </div>
            <div className="team-constraint-confirm__members">
              {teamConstraintDecision.map((conflict) => (
                <div key={conflict.agent_id}>
                  <strong>{conflict.agent_name}</strong>
                  <span>{conflict.reason}</span>
                  <small>
                    判断能力：{conflict.required_capabilities.map((capability) => TEAM_CAPABILITY_LABELS[capability] || capability).join("、")}
                    {` · 最高评分 ${Math.round(conflict.best_score * 100)}% · 采用阈值 50%`}
                  </small>
                </div>
              ))}
            </div>
            <div className="team-constraint-confirm__actions">
              <button className="agent-icon-btn" type="button" onClick={() => setTeamConstraintDecision(null)}>返回修改</button>
              <button
                className="agent-btn"
                type="button"
                onClick={() => {
                  const conflictIds = new Set(teamConstraintDecision.map((item) => item.agent_id));
                  const nextRequiredIds = requiredTeamAgentIds.filter((agentId) => !conflictIds.has(agentId));
                  const nextExcludedIds = Array.from(new Set([...excludedTeamAgentIds, ...conflictIds]));
                  setRequiredTeamAgentIds(nextRequiredIds);
                  setExcludedTeamAgentIds(nextExcludedIds);
                  setTeamConstraintDecision(null);
                  void requestTeamSuggestion(nextRequiredIds, [], nextExcludedIds);
                }}
              >
                按能力筛选
              </button>
              <button
                className="agent-btn agent-btn--dark"
                type="button"
                onClick={() => {
                  const forcedIds = teamConstraintDecision.map((item) => item.agent_id);
                  setTeamConstraintDecision(null);
                  void requestTeamSuggestion(requiredTeamAgentIds, forcedIds);
                }}
              >
                仍然加入团队
              </button>
            </div>
          </div>
        </div>
      )}

      {staffingDecision && (
        <div className="role-popover-backdrop" onClick={() => undefined}>
          <div className="team-constraint-confirm formation-staffing-confirm" role="dialog" aria-modal="true" aria-labelledby="formation-staffing-title" onClick={(event) => event.stopPropagation()}>
            <div className="team-constraint-confirm__head">
              <span className="team-mark team-mark--hero formation-staffing-confirm__logo" aria-hidden="true">
                <i /><i />
              </span>
              <div>
                <strong id="formation-staffing-title">
                  {teamName.trim() || "我的团队"}建议补充 {staffingSelections.length} 名临时成员
                </strong>
                <p>现有成员仍有明确的能力缺口。你可以采用推荐配置、换一个外援或模型，或明确保留当前团队并接受这些缺口。</p>
              </div>
            </div>
            <div className="team-constraint-confirm__members">
              {staffingSelections.map((selection, index) => (
                <div key={selection.gap_id}>
                  <strong>{selection.role_label}</strong>
                  <span>{selection.reason}</span>
                  <small>需要：{selection.required_capabilities.map((capability) => TEAM_CAPABILITY_LABELS[capability] || capability).join("、")}</small>
                  <div className="formation-staffing-confirm__selects">
                    <AgentFormSelect
                      value={selection.runtime_id}
                      placeholder="请选择外援"
                      options={staffingRuntimeOptions}
                      onChange={(runtimeId) => {
                        const models = staffingModelOptions(runtimeId);
                        const runtime = runtimeById.get(runtimeId);
                        const defaultModel = String(runtime?.metadata?.default_model_id || "");
                        setStaffingSelections((current) => current.map((item, itemIndex) => (
                          itemIndex === index
                            ? {
                              ...item,
                              runtime_id: runtimeId,
                              model_id: defaultModel || models[0]?.value || "",
                            }
                            : item
                        )));
                      }}
                    />
                    <AgentFormSelect
                      value={selection.model_id}
                      placeholder="请选择模型"
                      options={staffingModelOptions(selection.runtime_id)}
                      disabled={!selection.runtime_id}
                      onChange={(modelId) => setStaffingSelections((current) => current.map((item, itemIndex) => (
                        itemIndex === index ? { ...item, model_id: modelId } : item
                      )))}
                    />
                  </div>
                </div>
              ))}
            </div>
            <div className="team-constraint-confirm__actions">
              <button
                className="agent-icon-btn"
                type="button"
                onClick={() => {
                  setStaffingDecision(null);
                  setStaffingSelections([]);
                  setPendingTemporaryMembers([]);
                  setMessage({ tab: "create-team", text: "已保留当前团队；未覆盖能力和风险仍保留在组队方案中。" });
                }}
              >
                仍使用当前团队
              </button>
              <button
                className="agent-btn agent-btn--dark"
                type="button"
                disabled={staffingSelections.some((item) => !item.runtime_id || !item.model_id)}
                onClick={() => {
                  setPendingTemporaryMembers(staffingSelections);
                  setStaffingDecision(null);
                  setMessage({ tab: "create-team", text: "已加入临时成员配置；创建团队时才会生成该成员。" });
                }}
              >
                同意创建临时成员
              </button>
            </div>
          </div>
        </div>
      )}

      {singleLeaderConfirm && (
        <div className="role-popover-backdrop" onClick={() => setSingleLeaderConfirm(false)}>
          <div className="single-leader-confirm" role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
            <strong>
              当前只有 {agentById.get(teamLeaderId)?.name || "Leader"} 一名成员，已改为直接与{" "}
              {agentById.get(teamLeaderId)?.name || "Leader"} 对话。
            </strong>
            <div>
              <button className="agent-icon-btn" type="button" onClick={() => setSingleLeaderConfirm(false)}>
                取消
              </button>
              <button
                className="agent-btn agent-btn--dark"
                type="button"
                onClick={() => {
                  const leader = agentById.get(teamLeaderId);
                  if (!leader) return;
                  setSingleLeaderConfirm(false);
                  onStartLeaderChat(leader);
                }}
              >
                确认
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
