import { useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import Sidebar, { type SidebarView } from "./components/Sidebar";
import TopBar from "./components/TopBar";
import ChatPanel from "./components/ChatPanel";
import type { Props as ChatPanelProps } from "./components/ChatPanel";
import AgentsHub from "./components/AgentsHub";
import SkillsHub from "./components/SkillsHub";
import WikiHub from "./components/WikiHub";
import TaskBoard from "./components/TaskBoard";
import WorkspaceModal from "./components/WorkspaceModal";
import { useSessions } from "./hooks/useSessions";
import { useWorkspaces } from "./hooks/useWorkspaces";
import { useChat } from "./hooks/useChat";
import { api } from "./api";
import { externalAgentsAvailable } from "./lib/featureFlags";
import type { AppConfig, Attachment, ExternalTeam, Mode, Session, Task, TeamExecutionTier, UiMessage, Workspace } from "./types";

const genId = () => `web_${Math.random().toString(36).slice(2, 8)}`;
const CREW_BUILTIN_AGENT_ID = "crew::builtin";
type WikiAgentSessionBinding = { kbId: string; sessionId: string };

export function resolveWikiAgentSessionId(
  binding: WikiAgentSessionBinding | null,
  kbId: string,
): string | null {
  return binding?.kbId === kbId ? binding.sessionId : null;
}

const CONFIG_RETRY_INTERVAL_MS = 3000;

export function isExternalAgentSession(session: Session | undefined): boolean {
  const kind = session?.agent_binding?.kind;
  return kind === "external_agent" || kind === "external_team";
}

function compactRolePhrase(value: string, max = 10): string {
  const text = value
    .replace(/^\s*\d+[.、]\s*/, "")
    .replace(/^\s*[-*]\s*/, "")
    .replace(/^(作为)?(团队)?(leader|Leader|负责人|角色)[:：]?/i, "")
    .replace(/^根据.*?(进行|完成|开展)/, "")
    .replace(/^(主要)?(负责|承担|进行|参与|协助|执行)/, "")
    .replace(/^(整体|最终|相关|对应)的?/, "")
    .replace(/的(?=编码|开发|实现|测试|验收|评审|审查|设计|规划|管理)/g, "")
    .replace(/\s+/g, "")
    .trim();
  if (!text) return "";
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

function responsibilityBlock(role: string): string {
  const text = String(role || "");
  const match = text.match(/(?:^|\n)##\s*职责\s*\n([\s\S]*?)(?=\n##\s|\n#\s|$)/);
  if (match?.[1]) return match[1];
  return text;
}

function splitRoleClauses(role: string, options: { max?: number } = {}): string[] {
  const max = options.max ?? 12;
  return responsibilityBlock(role)
    .replace(/[`*_#>]/g, "")
    .split(/\n/)
    .filter((line) => !/^\s*(工作原则|职责|团队协作关系|输出格式|工作安排|备注)[:：]?\s*$/.test(line))
    .join("\n")
    .split(/[；;。.!！\n，,、/|｜+&和及]/)
    .map((part) => compactRolePhrase(part, max))
    .filter((part) => part && !/工作原则|团队协作关系|输出格式|工作安排|每日|提交前|迭代开始|迭代中|迭代结束/.test(part));
}

function summarizeLeaderCoord(role: string): string {
  const text = responsibilityBlock(role).replace(/\s+/g, "");
  const parts: string[] = [];
  if (/拆解|分配|派活|规划/.test(text)) parts.push("任务拆解");
  if (/协调|协同|沟通|推进|跟踪/.test(text)) parts.push("协作推进");
  if (/汇总|反馈|总结|交付/.test(text)) parts.push("结果汇总");
  if (parts.length === 0) return "Leader 统筹团队协作";
  return `Leader 统筹${parts.slice(0, 2).join("、")}`;
}

function summarizeBusinessDuty(role: string, fallback: string, options: { leader?: boolean; max?: number } = {}): string {
  const max = options.max ?? 10;
  const clauses = splitRoleClauses(role, { max })
    .filter((part) => !/leader|Leader|统筹|拆解|分配|派活|协调|协同|沟通|推进|跟踪|汇总|反馈|总结/.test(part));
  const priority = options.leader
    ? [
        /测试|验收|质检|质量|回归/,
        /评审|审查|把关/,
        /计划|里程碑|项目/,
        /开发|编码|实现|工程|前端|后端|接口/,
        /架构|设计/,
        /研究|调研|方案|文档|记录/,
      ]
    : [
        /开发|编码|实现|工程|前端|后端|接口/,
        /测试|验收|质检|质量|回归/,
        /架构|设计/,
        /评审|审查|把关/,
        /研究|调研|方案|文档|记录/,
      ];
  const source = priority
    .map((pattern) => clauses.find((part) => pattern.test(part)))
    .find(Boolean)
    || clauses[0]
    || splitRoleClauses(role, { max })[0]
    || "";
  if (!source) return fallback;
  if (/测试.*验收|验收.*测试/.test(source)) return compactRolePhrase(source, max);
  if (/测试|验收|质检|质量|回归/.test(source)) return compactRolePhrase(source, max);
  if (/开发|编码|实现|工程|前端|后端|接口/.test(source)) return compactRolePhrase(source, max);
  if (/架构|设计|评审|审查|把关/.test(source)) return compactRolePhrase(source, max);
  if (/研究|调研|方案|文档|记录/.test(source)) return compactRolePhrase(source, max);
  return compactRolePhrase(source, max) || fallback;
}

function summarizeLeaderRole(role: string): string {
  const coord = summarizeLeaderCoord(role);
  const duty = summarizeBusinessDuty(role, "", { leader: true, max: 80 });
  return duty ? `${coord}，负责${duty}` : coord;
}

function teamMembersForBoard(team?: ExternalTeam) {
  if (!team) return undefined;
  const sortedMembers = team.members
    .slice()
    .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
  const leaderMember = sortedMembers.find((member) => member.agent_id === team.leader_agent_id);
  const leaderName = team.leader_agent_id === CREW_BUILTIN_AGENT_ID
    ? "Crew 队长"
    : leaderMember?.agent_name || team.leader_agent_id || "Leader";
  const leader = {
    agentId: team.leader_agent_id,
    name: leaderName,
    displayBadge: leaderMember?.display_badge || (team.leader_agent_id === CREW_BUILTIN_AGENT_ID ? "M" : "?"),
    role: summarizeLeaderRole(leaderMember?.role || ""),
    isLeader: true,
    tone: 0,
  };
  const members = sortedMembers
    .filter((member) => member.agent_id !== team.leader_agent_id)
    .map((member, index) => ({
      agentId: member.agent_id,
      name: member.agent_name || member.agent_id,
      displayBadge: member.display_badge || "?",
      role: `负责${summarizeBusinessDuty(member.role || "", "协作执行", { max: 80 })}`,
      isLeader: false,
      tone: (index + 1) % 6,
    }));
  return [leader, ...members];
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [configLoadState, setConfigLoadState] = useState<"loading" | "ready" | "retrying">("loading");
  const [configRetryKey, setConfigRetryKey] = useState(0);
  const externalAgentsEnabled = externalAgentsAvailable(config);
  const [currentWorkspaceId, setCurrentWorkspaceId] = useState("default");
  const [currentSessionId, setCurrentSessionId] = useState<string>(genId);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["default"]));
  const [mode, setMode] = useState<Mode>("agent");
  const [boardOpen, setBoardOpen] = useState(false);
  const [boardWidth, setBoardWidth] = useState(() => {
    const saved = Number(window.localStorage.getItem("crew:board-width") || "");
    return Number.isFinite(saved) && saved >= 300 && saved <= 680 ? saved : 380;
  });
  const [view, setView] = useState<SidebarView>("chat");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [modal, setModal] = useState<{ open: boolean; ws: Workspace | null }>({ open: false, ws: null });
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [editDraft, setEditDraft] = useState<{ messageId: string; text: string } | null>(null);
  const [sessionExternalTeams, setSessionExternalTeams] = useState<Record<string, string>>({});
  const [sessionTeamTiers, setSessionTeamTiers] = useState<Record<string, TeamExecutionTier | undefined>>({});
  const [externalTeams, setExternalTeams] = useState<ExternalTeam[]>([]);
  const [wikiKbId, setWikiKbId] = useState("default");
  const [wikiAgentSession, setWikiAgentSession] = useState<WikiAgentSessionBinding | null>(null);
  const wikiAgentSessionId = resolveWikiAgentSessionId(wikiAgentSession, wikiKbId);

  const { sessions, refresh: refreshSessions } = useSessions();
  const { workspaces, refresh: refreshWorkspaces } = useWorkspaces();

  const refreshTasks = useCallback(async () => {
    try {
      setTasks(await api.tasks(currentSessionId));
    } catch {
      setTasks([]);
    }
  }, [currentSessionId]);

  const onAfterFinal = useCallback(() => {
    refreshSessions();
    refreshTasks();
  }, [refreshSessions, refreshTasks]);

  const chat = useChat(currentSessionId, onAfterFinal);

  // 进入 Wiki 视图或切换 KB 时，创建/复用该 KB 自己的 Wiki Agent 会话。
  useEffect(() => {
    if (view !== "wiki") return;
    let cancelled = false;
    setWikiAgentSession(null);
    api.wikiAgentSession(wikiKbId).then(({ session_id }) => {
      if (cancelled) return;
      setWikiAgentSession({ kbId: wikiKbId, sessionId: session_id });
      chat.loadHistory(session_id);
    }).catch(() => {});
    return () => { cancelled = true; };
    // 会话获取只由视图与 KB 身份驱动。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, wikiKbId]);

  // 新建 Wiki 对话（force_new，与桌面端「新建对话」一致）：旧会话保留在历史列表。
  const newWikiSession = useCallback(async () => {
    const { session_id } = await api.wikiAgentSession(wikiKbId, { forceNew: true });
    setWikiAgentSession({ kbId: wikiKbId, sessionId: session_id });
    chat.loadHistory(session_id);
  }, [wikiKbId, chat]);

  // 切换到历史 Wiki 对话。
  const selectWikiSession = useCallback((sessionId: string) => {
    if (!sessionId || sessionId === wikiAgentSessionId) return;
    setWikiAgentSession({ kbId: wikiKbId, sessionId });
    chat.loadHistory(sessionId);
  }, [wikiKbId, wikiAgentSessionId, chat]);

  // 删除 Wiki 对话；删的是当前会话则复用后端「取最近、无则新建」语义切到下一条。
  const deleteWikiSession = useCallback(async (sessionId: string) => {
    await api.deleteSession(sessionId);
    chat.clearSession(sessionId);
    if (wikiAgentSessionId === sessionId) {
      const { session_id } = await api.wikiAgentSession(wikiKbId);
      setWikiAgentSession({ kbId: wikiKbId, sessionId: session_id });
      chat.loadHistory(session_id);
    }
  }, [wikiKbId, wikiAgentSessionId, chat]);

  useEffect(() => {
    window.localStorage.setItem("crew:board-width", String(boardWidth));
  }, [boardWidth]);

  useEffect(() => {
    let cancelled = false;
    let retryTimer: number | null = null;

    const loadConfig = async (): Promise<void> => {
      try {
        const nextConfig = await api.config();
        if (cancelled) return;
        setConfig(nextConfig);
        setConfigLoadState("ready");
      } catch {
        if (cancelled) return;
        setConfigLoadState("retrying");
        retryTimer = window.setTimeout(() => {
          void loadConfig();
        }, CONFIG_RETRY_INTERVAL_MS);
      }
    };

    void loadConfig();
    return () => {
      cancelled = true;
      if (retryTimer != null) window.clearTimeout(retryTimer);
    };
  }, [configRetryKey]);

  useEffect(() => {
    if (!externalAgentsEnabled) {
      setExternalTeams([]);
      return;
    }
    api.externalTeams().then(setExternalTeams).catch(() => setExternalTeams([]));
  }, [externalAgentsEnabled]);

  useEffect(() => {
    if (!externalAgentsEnabled && view === "agents") setView("chat");
  }, [externalAgentsEnabled, view]);

  const jumpToMessage = useCallback((messageId: string) => {
    const target = document.getElementById(`message-${messageId}`);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("msg--located");
    window.setTimeout(() => target.classList.remove("msg--located"), 1400);
  }, []);

  useEffect(() => {
    if (!boardOpen) return;
    void refreshTasks();
    const timer = window.setInterval(() => void refreshTasks(), 2000);
    return () => window.clearInterval(timer);
  }, [boardOpen, refreshTasks]);

  useEffect(() => {
    if (view !== "chat" && boardOpen) {
      setBoardOpen(false);
    }
  }, [view, boardOpen]);

  // 会话列表变化（含首次加载/刷新）后，从后端拉回各会话运行态，恢复左侧栏状态点
  const seedStatuses = chat.seedStatuses;
  useEffect(() => {
    api.sessionsStatus().then(seedStatuses).catch(() => {});
  }, [sessions, seedStatuses]);

  const changeModel = useCallback(async (modelId: string) => {
    try {
      setConfig(await api.switchModel(modelId));
    } catch (err) {
      console.error("切换模型失败", err);
    }
  }, []);

  const expand = (wsId: string) =>
    setExpanded((prev) => new Set(prev).add(wsId));

  const toggleExpand = (wsId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(wsId) ? next.delete(wsId) : next.add(wsId);
      return next;
    });

  const newSession = (wsId: string) => {
    const sid = genId();
    setCurrentWorkspaceId(wsId);
    setCurrentSessionId(sid);
    setMode("agent");
    setSessionExternalTeams((prev) => {
      if (!prev[sid]) return prev;
      const next = { ...prev };
      delete next[sid];
      return next;
    });
    setSessionTeamTiers((prev) => ({ ...prev, [sid]: undefined }));
    setBoardOpen(false);
    setView("chat");
    setEditDraft(null);
    // 不再清空全局消息：新 session id 的消息桶天然为空，旧会话仍可在后台继续流式
    setTasks([]);
    expand(wsId);
  };

  const selectSession = (s: Session) => {
    if (!externalAgentsEnabled && isExternalAgentSession(s)) return;
    setCurrentSessionId(s.session_id);
    setCurrentWorkspaceId(s.workspace_id);
    setView("chat");
    setEditDraft(null);
    setMode(s.agent_label?.provider === "team" || sessionExternalTeams[s.session_id] ? "team" : "agent");
    chat.loadHistory(s.session_id);
    api.tasks(s.session_id).then(setTasks).catch(() => setTasks([]));
    api.getSessionAgentConfig(s.session_id).then((config) => {
      const team = config.team as { external_team_id?: string } | undefined;
      const externalTeamId = String(team?.external_team_id || "");
      if (externalTeamId) {
        setSessionExternalTeams((prev) => ({ ...prev, [s.session_id]: externalTeamId }));
        setMode("team");
      }
    }).catch(() => {});
  };

  const renameSession = async (id: string, title: string) => {
    await api.renameSession(id, title);
    await refreshSessions();
  };

  const deleteSession = async (id: string) => {
    await api.deleteSession(id);
    chat.clearSession(id);
    await refreshSessions();
    if (id === currentSessionId) newSession(currentWorkspaceId);
  };

  // ---- 工作空间 ----
  const submitWorkspace = async (fields: { name: string; description: string; instructions: string }) => {
    if (modal.ws) {
      await api.updateWorkspace(modal.ws.id, fields);
    } else {
      const created = await api.createWorkspace(fields);
      await refreshWorkspaces();
      newSession(created.id);
      setModal({ open: false, ws: null });
      return;
    }
    await refreshWorkspaces();
    setModal({ open: false, ws: null });
  };

  const deleteWorkspace = async (wsId: string) => {
    await api.deleteWorkspace(wsId);
    await Promise.all([refreshWorkspaces(), refreshSessions()]);
    if (wsId === currentWorkspaceId) newSession("default");
  };

  const handleSend = (text: string, sendAttachments: Attachment[], subScenario?: string) => {
    const cmd = text.trim().toLowerCase();
    // 与 CLI 一致：/plan 进入 Plan 模式（不发给模型）
    if (!editDraft && cmd === "/plan") {
      chat.enterPlan(currentSessionId);
      return;
    }
    const query = editDraft
      ? `继续执行未完成任务。\n\n这是我修改后的需求：\n${text.trim()}`
      : text;
    const effectiveMode: Mode = mode === "team" && sessionExternalTeams[currentSessionId] ? "team" : "agent";
    const teamExecutionTier = effectiveMode === "team"
      ? (sessionTeamTiers[currentSessionId] ?? "auto")
      : undefined;
    chat.send(query, currentSessionId, effectiveMode, currentWorkspaceId, sendAttachments, {
      subScenario,
      externalTeamId: effectiveMode === "team" ? sessionExternalTeams[currentSessionId] : undefined,
      teamExecutionTier,
    });
    setEditDraft(null);
  };

  const editMessage = (message: UiMessage) => {
    if (chat.busy || message.role !== "user" || !message.text.trim()) return;
    setEditDraft({ messageId: message.id, text: message.text });
    setAttachments([]);
  };

  const handleWikiSend = useCallback((text: string, attachments: Attachment[]) => {
    if (!wikiAgentSessionId) return;
    chat.send(text, wikiAgentSessionId, mode, currentWorkspaceId, attachments, {
      wikiKbId,
    });
  }, [chat.send, wikiAgentSessionId, mode, currentWorkspaceId, wikiKbId]);

  const currentSession = sessions.find((s) => s.session_id === currentSessionId);
  const visibleSessions = useMemo(
    () => externalAgentsEnabled
      ? sessions
      : sessions.filter((session) => !isExternalAgentSession(session)),
    [externalAgentsEnabled, sessions],
  );
  useEffect(() => {
    if (externalAgentsEnabled || !isExternalAgentSession(currentSession)) return;
    const nextSessionId = genId();
    setCurrentSessionId(nextSessionId);
    setMode("agent");
    setBoardOpen(false);
    setView("chat");
    setEditDraft(null);
    setTasks([]);
  }, [currentSession, externalAgentsEnabled]);
  const title = currentSession?.title || "新会话";
  const currentAgentLabel = currentSession?.agent_label;
  const currentExternalTeam = externalTeams.find((team) => team.id === sessionExternalTeams[currentSessionId]);
  const currentTeamMembers = useMemo(() => teamMembersForBoard(currentExternalTeam), [currentExternalTeam]);
  const isCurrentTeamSession = mode === "team" && Boolean(sessionExternalTeams[currentSessionId]);
  const toolsMenuChrome = boardOpen;
  const toolCollapseLevel = !boardOpen ? 0 : boardWidth >= 520 ? 3 : boardWidth >= 420 ? 2 : 1;
  const canSelectSessionAgent = (
    externalAgentsEnabled
    &&
    mode === "agent"
    && !chat.busy
    && chat.messages.length === 0
    && (!currentAgentLabel || currentAgentLabel.provider === "crew")
  );

  const bindCurrentSessionAgent = async (agent: { id: string; name: string }) => {
    if (!externalAgentsEnabled || !canSelectSessionAgent) return;
    await api.setSessionAgentConfig(
      currentSessionId,
      {
        executor: "external",
        external: { external_agent_id: agent.id },
      },
      {
        workspace_id: currentWorkspaceId,
        title: "新会话",
      },
    );
    setMode("agent");
    chat.clearSession(currentSessionId);
    setTasks([]);
    await refreshSessions();
  };

  const chatProps: ChatPanelProps = {
    messages: chat.messages,
    busy: chat.busy,
    queueHint: chat.queueHint,
    pendingQueue: chat.pendingQueue,
    config,
    attachments,
    onSend: handleWikiSend,
    onAsk: (text) => handleWikiSend(text, []),
    onStop: () => chat.stop(currentSessionId),
    onSteer: (text) => chat.steer(currentSessionId, text),
    onRemoveFromQueue: (i) => chat.removeFromQueue(currentSessionId, i),
    onEditQueueItem: (i, q) => chat.editQueueItem(currentSessionId, i, q),
    onSendQueueItemNow: (id) => chat.sendQueueItemNow(currentSessionId, id),
    onAttachmentsChange: setAttachments,
    onModelChange: changeModel,
    currentAgentLabel,
    canSelectAgent: canSelectSessionAgent,
    onSelectAgent: bindCurrentSessionAgent,
    planActive: chat.planActive,
    onEnterPlan: () => chat.enterPlan(currentSessionId),
    onExitPlan: () => chat.exitPlan(currentSessionId),
    onApprovePlan: () => chat.approvePlan(currentSessionId, mode, currentWorkspaceId),
    onRejectPlan: () => chat.rejectPlan(currentSessionId),
    onRejectAndExitPlan: () => chat.rejectAndExitPlan(currentSessionId),
    followupQuestion: chat.followupQuestion,
    onAnswerFollowup: (questionId, answers) => chat.answerFollowup(currentSessionId, questionId, answers),
    onDismissFollowup: () => chat.dismissFollowup(currentSessionId),
    editDraft,
    onEditMessage: editMessage,
    onCancelEdit: () => setEditDraft(null),
    todos: chat.todos,
  };

  // Wiki Agent 独立会话：chatProps 保持不变，wikiChatProps 仅覆盖 session 相关字段。
  const wikiSid = wikiAgentSessionId || "";
  const wikiChatProps: ChatPanelProps = useMemo(() => ({
    ...chatProps,
    ...chat.forSession(wikiSid),
    onStop: () => chat.stop(wikiSid),
    onSteer: (text) => chat.steer(wikiSid, text),
    onRemoveFromQueue: (i) => chat.removeFromQueue(wikiSid, i),
    onEditQueueItem: (i, q) => chat.editQueueItem(wikiSid, i, q),
    onSendQueueItemNow: (id) => chat.sendQueueItemNow(wikiSid, id),
    onEnterPlan: () => chat.enterPlan(wikiSid),
    onExitPlan: () => chat.exitPlan(wikiSid),
    onApprovePlan: () => chat.approvePlan(wikiSid, mode, currentWorkspaceId),
    onRejectPlan: () => chat.rejectPlan(wikiSid),
    onRejectAndExitPlan: () => chat.rejectAndExitPlan(wikiSid),
    onAnswerFollowup: (questionId, answers) => chat.answerFollowup(wikiSid, questionId, answers),
    onDismissFollowup: () => chat.dismissFollowup(wikiSid),
  }), [chatProps, wikiSid, mode, currentWorkspaceId, chat]);

  return (
    <div
      className={"app" + (boardOpen ? " with-board" : "")}
      style={boardOpen ? ({ "--board-width": `${boardWidth}px` } as CSSProperties) : undefined}
    >
      <Sidebar
        workspaces={workspaces}
        sessions={visibleSessions}
        currentSessionId={currentSessionId}
        sessionStatus={chat.sessionStatus}
        expanded={expanded}
        view={view}
        externalAgentsEnabled={externalAgentsEnabled}
        onViewChange={(nextView) => {
          if (nextView === "agents" && !externalAgentsEnabled) return;
          setView(nextView);
        }}
        onToggleExpand={toggleExpand}
        onNewWorkspace={() => setModal({ open: true, ws: null })}
        onEditWorkspace={(ws) => setModal({ open: true, ws })}
        onDeleteWorkspace={deleteWorkspace}
        onNewSession={newSession}
        onSelectSession={selectSession}
        onRenameSession={renameSession}
        onDeleteSession={deleteSession}
      />
      <main className="main">
        {configLoadState === "retrying" && (
          <div className="gateway-config-notice" role="status">
            <span className="gateway-config-notice__dot" aria-hidden="true" />
            <span>运行环境暂未连接，正在重试…</span>
            <button type="button" onClick={() => setConfigRetryKey((current) => current + 1)}>
              立即重试
            </button>
          </div>
        )}
        {view === "skills" ? (
          <SkillsHub />
        ) : view === "agents" && externalAgentsEnabled ? (
          <AgentsHub
            onAssignAgent={async (agent) => {
              const sid = genId();
              await api.setSessionAgentConfig(
                sid,
                {
                  executor: "external",
                  external: { external_agent_id: agent.id },
                },
                {
                  workspace_id: currentWorkspaceId,
                  title: "新会话",
                },
              );
              setCurrentSessionId(sid);
              setMode("agent");
              setView("chat");
              chat.clearSession(sid);
              setTasks([]);
              await refreshSessions();
            }}
            onAssignTeam={async (team) => {
              const sid = genId();
              await api.setSessionAgentConfig(
                sid,
                {
                  executor: "team",
                  team: { external_team_id: team.id },
                },
                {
                  workspace_id: currentWorkspaceId,
                  title: `${team.name} · 团队任务`,
                },
              );
              setSessionExternalTeams((prev) => ({ ...prev, [sid]: team.id }));
              setSessionTeamTiers((prev) => ({ ...prev, [sid]: "auto" }));
              setExternalTeams((prev) => {
                const rest = prev.filter((item) => item.id !== team.id);
                return [team, ...rest];
              });
              setCurrentSessionId(sid);
              setMode("team");
              setBoardOpen(true);
              setView("chat");
              chat.clearSession(sid);
              setTasks([]);
              await refreshSessions();
            }}
            onStartLeaderChat={async (agent) => {
              const sid = genId();
              const isBuiltin = agent.id === "crew::builtin";
              await api.setSessionAgentConfig(
                sid,
                isBuiltin
                  ? { executor: "builtin" }
                  : { executor: "external", external: { external_agent_id: agent.id } },
                {
                  workspace_id: currentWorkspaceId,
                  title: `与 ${agent.name} 对话`,
                },
              );
              setCurrentSessionId(sid);
              setMode("agent");
              setView("chat");
              setBoardOpen(false);
              chat.clearSession(sid);
              setTasks([]);
              await refreshSessions();
            }}
          />
        ) : view === "wiki" ? (
          <WikiHub
            chatProps={wikiChatProps}
            kbId={wikiKbId}
            onKbChange={setWikiKbId}
            sessionId={wikiAgentSessionId || ""}
            wikiProgress={chat.wikiProgress}
            onNewSession={newWikiSession}
            onSelectSession={selectWikiSession}
            onDeleteSession={deleteWikiSession}
          />
        ) : (
          <>
            {(chat.messages.length > 0 || chat.busy || isCurrentTeamSession) && (
              <TopBar
                title={title}
                connected={chat.connected}
                config={config}
                boardOpen={boardOpen}
                onModelChange={changeModel}
                activeModelId={config?.active_model_id ?? ""}
                onToggleBoard={() => {
                  const next = !boardOpen;
                  setBoardOpen(next);
                  if (next) refreshTasks();
                }}
              />
            )}
            <ChatPanel
              messages={chat.messages}
              busy={chat.busy}
              compactingContext={chat.compactingContext}
              queueHint={chat.queueHint}
              pendingQueue={chat.pendingQueue}
              config={config}
              attachments={attachments}
              onSend={handleSend}
              onAsk={(text) => handleSend(text, [])}
              onStop={() => chat.stop(currentSessionId)}
              onSteer={(text) => chat.steer(currentSessionId, text)}
              planActive={chat.planActive}
              onEnterPlan={() => chat.enterPlan(currentSessionId)}
              onExitPlan={() => chat.exitPlan(currentSessionId)}
              onApprovePlan={() => chat.approvePlan(currentSessionId, mode, currentWorkspaceId)}
              onRejectPlan={() => chat.rejectPlan(currentSessionId)}
              onRejectAndExitPlan={() => chat.rejectAndExitPlan(currentSessionId)}
              followupQuestion={chat.followupQuestion}
              onAnswerFollowup={(questionId, answers) => chat.answerFollowup(currentSessionId, questionId, answers)}
              onDismissFollowup={() => chat.dismissFollowup(currentSessionId)}
              editDraft={editDraft}
              onEditMessage={editMessage}
              onCancelEdit={() => setEditDraft(null)}
              onRemoveFromQueue={(i) => chat.removeFromQueue(currentSessionId, i)}
              onEditQueueItem={(i, q) => chat.editQueueItem(currentSessionId, i, q)}
              onSendQueueItemNow={(id) => chat.sendQueueItemNow(currentSessionId, id)}
              onAttachmentsChange={setAttachments}
              onModelChange={changeModel}
              currentAgentLabel={currentAgentLabel}
              isTeamSession={isCurrentTeamSession}
              teamExecutionTier={sessionTeamTiers[currentSessionId] ?? "auto"}
              onTeamExecutionTierChange={(tier) => {
                setSessionTeamTiers((prev) => ({ ...prev, [currentSessionId]: tier }));
              }}
              teamMembers={currentTeamMembers}
              compactTools={toolsMenuChrome}
              toolCollapseLevel={toolCollapseLevel}
              todos={chat.todos}
              canSelectAgent={canSelectSessionAgent}
              onSelectAgent={bindCurrentSessionAgent}
            />
          </>
        )}
      </main>
      {boardOpen && (
        <TaskBoard
          sessionId={currentSessionId}
          tasks={tasks}
          mode={mode}
          messages={chat.messages}
          currentAgentLabel={currentAgentLabel}
          teamMembers={currentTeamMembers}
          boardWidth={boardWidth}
          onBoardWidthChange={setBoardWidth}
          onJumpToMessage={jumpToMessage}
          onClose={() => setBoardOpen(false)}
          onCancel={async (taskId) => {
            await api.cancelTask(taskId);
            await refreshTasks();
          }}
          onRecover={async (nodeId, action, replacementAssignee) => {
            const result = await api.recoverTeamNode(
              currentSessionId,
              nodeId,
              action,
              replacementAssignee,
            );
            if (!result.ok) throw new Error(result.error || "节点恢复失败");
            await refreshTasks();
          }}
        />
      )}
      {modal.open && (
        <WorkspaceModal
          initial={modal.ws}
          onClose={() => setModal({ open: false, ws: null })}
          onSubmit={submitWorkspace}
        />
      )}
    </div>
  );
}
