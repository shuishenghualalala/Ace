import { Fragment, useEffect, useRef, useState } from "react";
import type { AppConfig, Attachment, ExternalAgent, Session, Skill, TeamExecutionTier, TeamMemberView, UserAgentMention } from "../types";
import { api } from "../api";
import { externalAgentsAvailable } from "../lib/featureFlags";
import AttachmentList from "./AttachmentList";
import ExternalAgentAvatar from "./ExternalAgentAvatar";

interface Props {
  config: AppConfig | null;
  busy: boolean;
  attachments: Attachment[];
  onSend: (text: string, attachments: Attachment[], subScenario?: string, userMentions?: UserAgentMention[]) => void;
  onStop?: () => void;
  onSteer?: (text: string) => void;
  onAttachmentsChange: (attachments: Attachment[]) => void;
  onModelChange?: (modelId: string) => void;
  /** 当前会话绑定的模型 id（会话级切换；未传时回退全局 active） */
  activeModelId?: string;
  activeModelLabel?: string;
  isTeamSession?: boolean;
  teamMembers?: TeamMemberView[];
  teamExecutionTier?: TeamExecutionTier;
  onTeamExecutionTierChange?: (tier: TeamExecutionTier) => void;
  currentAgentLabel?: Session["agent_label"];
  canSelectAgent?: boolean;
  onSelectAgent?: (agent: ExternalAgent) => void | Promise<void>;
  compact?: boolean;
  compactTools?: boolean;
  toolCollapseLevel?: number;
  planActive?: boolean;
  onEnterPlan?: () => void;
  onExitPlan?: () => void;
  /** 场景化推荐：预填文本（nonce 变化即注入）+ 场景 chip + 携带的细分玩法 id */
  prefillText?: string;
  prefillNonce?: number;
  scenarioChip?: { label: string; onClear: () => void } | null;
  subScenario?: string;
  editDraft?: { messageId: string; text: string } | null;
  onCancelEdit?: () => void;
  /** 附件上传归属（wiki 会话时传入）：后端把附件收入对应知识库 */
  uploadContext?: { sessionId?: string; kbId?: string };
}

export function teamMemberMentionId(
  member: Pick<TeamMemberView, "agentId" | "isLeader">,
): string {
  return member.isLeader ? "leader" : member.agentId?.trim() || "";
}

export function formatTeamMentionToken(member: Pick<TeamMemberView, "agentId" | "mentionId" | "name">): string {
  const label = member.name.trim().replace(/\s+/g, " ");
  return `@${label || member.mentionId || member.agentId || "Agent"}`;
}

export default function Composer({
  config,
  busy,
  attachments,
  onSend,
  onStop,
  onSteer,
  onAttachmentsChange,
  onModelChange,
  activeModelId,
  activeModelLabel,
  isTeamSession = false,
  teamMembers,
  teamExecutionTier = "auto",
  onTeamExecutionTierChange,
  currentAgentLabel,
  canSelectAgent = false,
  onSelectAgent,
  compact,
  compactTools = false,
  toolCollapseLevel = compactTools ? 1 : 0,
  planActive,
  onEnterPlan,
  onExitPlan,
  prefillText,
  prefillNonce,
  scenarioChip,
  subScenario,
  editDraft,
  onCancelEdit,
  uploadContext,
}: Props) {
  const externalAgentsEnabled = externalAgentsAvailable(config);
  const [text, setText] = useState("");
  const [atOpen, setAtOpen] = useState(false);
  const [atResults, setAtResults] = useState<
    { text: string; display: string; meta: string; type: string; agentMention?: UserAgentMention }[]
  >([]);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillQuery, setSkillQuery] = useState("");
  const [agentsOpen, setAgentsOpen] = useState(false);
  const [agents, setAgents] = useState<ExternalAgent[]>([]);
  const [agentBusy, setAgentBusy] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [teamModeOpen, setTeamModeOpen] = useState(false);
  const [craftOpen, setCraftOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const atIndexRef = useRef(-1);
  const agentMentionsRef = useRef<Record<string, UserAgentMention>>({});
  const craftRef = useRef<HTMLDivElement>(null);
  const skillsRef = useRef<HTMLDivElement>(null);
  const agentsRef = useRef<HTMLDivElement>(null);
  const modelRef = useRef<HTMLDivElement>(null);
  const teamModeRef = useRef<HTMLDivElement>(null);
  const toolsRef = useRef<HTMLDivElement>(null);
  const composingRef = useRef(false);
  // Safari 中 compositionend 在 keydown 之前触发，用此标记捕获"刚结束合成后的 Enter"
  const justComposedRef = useRef(false);

  useEffect(() => {
    if (!editDraft) return;
    setText(editDraft.text);
    window.setTimeout(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(editDraft.text.length, editDraft.text.length);
    }, 0);
  }, [editDraft]);

  // 点击工具弹窗外部时关闭
  useEffect(() => {
    if (!craftOpen && !skillsOpen && !agentsOpen && !modelOpen && !teamModeOpen && !toolsOpen) return;
    const handler = (e: MouseEvent) => {
      if (toolsOpen && toolsRef.current && !toolsRef.current.contains(e.target as Node)) {
        setToolsOpen(false);
      }
      if (craftOpen && craftRef.current && !craftRef.current.contains(e.target as Node)) {
        setCraftOpen(false);
      }
      if (skillsOpen && skillsRef.current && !skillsRef.current.contains(e.target as Node)) {
        setSkillsOpen(false);
        setSkillQuery("");
      }
      if (agentsOpen && agentsRef.current && !agentsRef.current.contains(e.target as Node)) {
        setAgentsOpen(false);
      }
      if (modelOpen && modelRef.current && !modelRef.current.contains(e.target as Node)) {
        setModelOpen(false);
      }
      if (teamModeOpen && teamModeRef.current && !teamModeRef.current.contains(e.target as Node)) {
        setTeamModeOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [craftOpen, skillsOpen, agentsOpen, modelOpen, teamModeOpen, toolsOpen]);

  useEffect(() => {
    if (externalAgentsEnabled) return;
    setAgentsOpen(false);
    setAgents([]);
  }, [externalAgentsEnabled]);

  const toggleTools = () => {
    const next = !toolsOpen;
    setToolsOpen(next);
    if (next) {
      if (skills.length === 0) api.skills().then(setSkills).catch(() => setSkills([]));
      if (externalAgentsEnabled && agents.length === 0) {
        api.externalAgents().then(setAgents).catch(() => setAgents([]));
      }
    } else {
      setSkillQuery("");
    }
  };

  const toggleSkills = () => {
    if (!skillsOpen && skills.length === 0) {
      api.skills().then(setSkills).catch(() => setSkills([]));
    }
    if (skillsOpen) setSkillQuery("");
    setSkillsOpen(!skillsOpen);
  };

  const selectSkill = (slug: string) => {
    const prefix = `/${slug} `;
    setText((prev) => (prev.trim() ? `${prefix}${prev}` : prefix));
    setSkillQuery("");
    setSkillsOpen(false);
  };

  const filteredSkills = skills.filter((skill) => {
    const query = skillQuery.trim().toLowerCase();
    if (!query) return true;
    return [
      skill.slug,
      skill.name,
      skill.display_name,
      skill.description,
      skill.description_zh,
      skill.category,
    ].some((value) => value?.toLowerCase().includes(query));
  });

  const toggleAgents = () => {
    if (!externalAgentsEnabled) return;
    if (!agentsOpen && agents.length === 0) {
      api.externalAgents().then(setAgents).catch(() => setAgents([]));
    }
    setAgentsOpen((v) => !v);
  };

  const selectAgent = async (agent: ExternalAgent) => {
    if (!externalAgentsEnabled || !canSelectAgent || !onSelectAgent) return;
    setAgentBusy(true);
    try {
      await onSelectAgent(agent);
      setAgentsOpen(false);
    } finally {
      setAgentBusy(false);
    }
  };

  // 场景化推荐：nonce 变化时把预填文本注入输入框（用户可继续编辑）
  useEffect(() => {
    if (prefillNonce === undefined) return;
    setText(prefillText ?? "");
  }, [prefillNonce]);

  const submit = () => {
    const t = text.trim();
    // 忙时也允许发送：后端会按会话排队，前端显示「正在队列中」卡片
    if (!t && attachments.length === 0) return;
    const userMentions = Object.entries(agentMentionsRef.current)
      .filter(([token]) => new RegExp(`(^|\\s)${token.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}(?=\\s|$)`).test(t))
      .map(([, mention]) => mention);
    onSend(t, attachments, subScenario, userMentions.length > 0 ? userMentions : undefined);
    setText("");
    agentMentionsRef.current = {};
    onCancelEdit?.();
    onAttachmentsChange([]);
  };

  // 实时引导：把当前输入注入运行中的回复（不打断），随后清空输入框
  const doSteer = () => {
    const t = text.trim();
    if (!t) return;
    onSteer?.(t);
    setText("");
    onCancelEdit?.();
  };

  /** 上传 File 列表并追加到 attachments（复用：文件选择 / 粘贴 / 拖拽）。并行上传。 */
  const uploadFiles = async (files: File[]) => {
    const results = await Promise.allSettled(
      files.map(async (file) => {
        const content = await readFileAsBase64(file);
        const result = await api.upload(file.name, content, uploadContext);
        return {
          id: result.id,
          name: result.name,
          path: result.path,
          type: result.type as Attachment["type"],
          previewUrl: result.previewUrl,
        };
      }),
    );
    const newAtts: Attachment[] = [...attachments];
    for (const r of results) {
      if (r.status === "fulfilled") {
        newAtts.push(r.value);
      } else {
        console.error("上传失败:", r.reason);
      }
    }
    onAttachmentsChange(newAtts);
  };

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    await uploadFiles(Array.from(files));
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  /** 粘贴图像：从剪贴板读取 image/* 文件 */
  const handlePaste = async (e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imageFiles: File[] = [];
    for (const item of Array.from(items)) {
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) imageFiles.push(file);
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault(); // 阻止默认粘贴行为（避免 textarea 插入乱码）
      await uploadFiles(imageFiles);
    }
  };

  /** 拖拽：允许 drop */
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };

  /** 拖拽释放：上传拖入的文件 */
  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) {
      await uploadFiles(files);
    }
  };

  const removeAttachment = (id: string) => {
    onAttachmentsChange(attachments.filter((a) => a.id !== id));
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 【第1层】合成阶段内的 keydown 直接忽略
    if (e.nativeEvent.isComposing || composingRef.current) return;
    // 【第2层】Chrome: IME 事件中 keyCode 为 229，即使 isComposing 已为 false 也能检测到
    if ((e.nativeEvent as KeyboardEvent).keyCode === 229) return;

    // 【第3层】Safari 兼容：Safari 中 compositionend 先于 keydown 触发，
    // 此时 isComposing=false 且 keyCode!=229，需用 justComposedRef 捕获确认选字的 Enter
    if (justComposedRef.current && e.key === "Enter") return;

    if (atOpen && atResults.length > 0 && (e.key === "Tab" || (e.key === "Enter" && atOpen))) {
      e.preventDefault();
      selectAtItem(atResults[0]);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
      return;
    }
    if (e.key === "Escape" && atOpen) {
      setAtOpen(false);
      return;
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setText(val);

    const cursorPos = e.target.selectionStart;
    const textBeforeCursor = val.slice(0, cursorPos);
    const atMatch = textBeforeCursor.match(/@([^\s]*)$/);
    if (atMatch) {
      atIndexRef.current = cursorPos - atMatch[0].length;
      setAtOpen(true);
      const query = atMatch[1];
      const teamResults = isTeamSession
        ? (teamMembers ?? [])
          .filter((member) => teamMemberMentionId(member))
          .filter((member) => {
            const needle = query.trim().toLowerCase();
            if (!needle) return true;
            return [teamMemberMentionId(member), member.agentId, member.name, member.role]
              .filter(Boolean)
              .some((value) => value!.toLowerCase().includes(needle));
          })
          .map((member) => ({
            text: formatTeamMentionToken(member),
            display: member.name,
            meta: member.isLeader ? `Leader · ${member.role}` : member.role,
            type: "agent",
            agentMention: {
              kind: "team_member" as const,
              member_id: teamMemberMentionId(member),
            },
          }))
        : [];
      api.complete(query)
        .then((files) => setAtResults([...teamResults, ...files]))
        .catch(() => setAtResults(teamResults));
    } else {
      setAtOpen(false);
    }
  };

  const selectAtItem = (item: { text: string; display: string; meta: string; type: string; agentMention?: UserAgentMention }) => {
    const before = text.slice(0, atIndexRef.current);
    const atMatchLen = text.slice(before.length).match(/^@[^\s]*/)?.[0].length || 0;
    const after = text.slice(before.length + atMatchLen);
    const newText = `${before}${item.text} `;
    setText(newText + after);
    setAtOpen(false);

    if (item.type === "agent" && item.agentMention) {
      agentMentionsRef.current[item.text] = item.agentMention;
      return;
    }

    if (item.type === "file" || item.type === "image") {
      const pathMeta = item.meta;
      onAttachmentsChange([
        ...attachments,
        {
          id: `ref_${Date.now()}`,
          name: item.display,
          path: pathMeta,
          type: item.type === "image" ? "image" : "file",
        },
      ]);
    }
  };

  const collapseLevel = compactTools ? Math.max(1, toolCollapseLevel) : 0;
  const composerClassName = [
    "composer",
    compact ? "composer--compact" : "",
    collapseLevel > 0 ? "composer--tools-compact" : "",
    collapseLevel >= 2 ? "composer--tools-model" : "",
    collapseLevel >= 3 ? "composer--tools-all" : "",
  ].filter(Boolean).join(" ");

  return (
    <div
      className={composerClassName}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {editDraft && !busy && (
        <div className="composer-edit-banner">
          <span className="composer-edit-banner__dot" />
          <span className="composer-edit-banner__text">正在修改上一条消息，发送后将继续执行未完成任务</span>
          <button type="button" className="composer-edit-banner__cancel" onClick={onCancelEdit}>
            取消
          </button>
        </div>
      )}
      <div className="composer__box">
        <AttachmentList attachments={attachments} onRemove={removeAttachment} />
        {scenarioChip && (
          <div className="composer__scenario-chip">
            <span className="composer__scenario-chip-icon">📑</span>
            <span>{scenarioChip.label}</span>
            <button type="button" className="composer__scenario-chip-clear" onClick={scenarioChip.onClear} title="清除场景">
              ✕
            </button>
          </div>
        )}
        <div className="composer__input-row">
          <button
            className="composer__attach"
            onClick={() => fileInputRef.current?.click()}
            title="添加附件"
            type="button"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: "none" }}
            onChange={handleFileSelect}
          />
          <textarea
            ref={textareaRef}
            rows={compact ? 1 : 2}
            placeholder="输入消息..."
            value={text}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onCompositionStart={() => (composingRef.current = true)}
            onCompositionEnd={() => {
              composingRef.current = false;
              // Safari 中 compositionend 在 keydown 之前触发，标记"刚结束合成"
              // 用 setTimeout(0) 重置：确保只在同一宏任务周期内生效
              // Chrome 中 compositionend 后的下一个用户 Enter 是不同宏任务，不会被误拦截
              justComposedRef.current = true;
              setTimeout(() => (justComposedRef.current = false), 0);
            }}
          />
        </div>
        {atOpen && atResults.length > 0 && (
          <div className="at-popover">
            {atResults.map((r, i) => {
              const category = r.type === "agent" ? "团队成员" : "文件";
              const previous = atResults[i - 1];
              const previousCategory = previous?.type === "agent" ? "团队成员" : "文件";
              return (
                <Fragment key={`${r.type}-${r.text}-${i}`}>
                  {category !== previousCategory && (
                    <div className="at-popover__section">{category}</div>
                  )}
                  <button
                    className="at-popover__item"
                    onClick={() => selectAtItem(r)}
                    type="button"
                  >
                    <span className={`at-popover__icon${r.type === "agent" ? " at-popover__icon--agent" : ""}`}>
                      {r.type === "agent" ? "◉" : r.type === "folder" ? "📁" : r.type === "image" ? "🖼️" : "📄"}
                    </span>
                    <span className="at-popover__display">{r.display}</span>
                    <span className="at-popover__meta">{r.meta}</span>
                  </button>
                </Fragment>
              );
            })}
          </div>
        )}
        <div className="composer__toolbar">
          <div className="composer__toolbar-left">
            {compactTools && (
              <div className="tools-picker" ref={toolsRef}>
                <button
                  className={"toolbar-btn toolbar-btn--icon" + (toolsOpen ? " toolbar-btn--active" : "")}
                  onClick={toggleTools}
                  title="更多工具"
                  type="button"
                >
                  +
                </button>
                {toolsOpen && (
                  <div className="tools-popover">
                    {collapseLevel >= 3 && (
                      <div className="tools-popover__group">
                        <span className="tools-popover__title">Craft</span>
                        <button
                          className={"tools-popover__item" + (!planActive ? " is-active" : "")}
                          onClick={() => {
                            if (planActive) onExitPlan?.();
                            setToolsOpen(false);
                          }}
                          type="button"
                        >
                          Craft
                        </button>
                        <button
                          className={"tools-popover__item" + (planActive ? " is-active" : "")}
                          onClick={() => {
                            onEnterPlan?.();
                            setToolsOpen(false);
                          }}
                          type="button"
                        >
                          Plan
                        </button>
                      </div>
                    )}
                    {collapseLevel >= 2 && config && config.models.length > 0 && (
                      <div className="tools-popover__group">
                        <span className="tools-popover__title">Model</span>
                        {config.models.map((m) => (
                          <button
                            key={m.id}
                            className={"tools-popover__item" + (m.id === config.active_model_id ? " is-active" : "")}
                            onClick={() => {
                              onModelChange?.(m.id);
                              setToolsOpen(false);
                            }}
                            type="button"
                          >
                            {m.name}
                          </button>
                        ))}
                      </div>
                    )}
                    {collapseLevel >= 3 && (
                      <div className="tools-popover__group">
                        <span className="tools-popover__title">Skills</span>
                        {skills.slice(0, 6).map((skill) => (
                          <button
                            key={skill.slug}
                            className="tools-popover__item"
                            onClick={() => {
                              selectSkill(skill.slug);
                              setToolsOpen(false);
                            }}
                            type="button"
                          >
                            /{skill.slug}
                          </button>
                        ))}
                        {skills.length === 0 && <span className="tools-popover__empty">暂无 Skills</span>}
                      </div>
                    )}
                    {externalAgentsEnabled && (
                      <div className="tools-popover__group">
                        <span className="tools-popover__title">外援</span>
                        {!canSelectAgent && (
                          <span className="tools-popover__empty">
                            {currentAgentLabel && currentAgentLabel.provider !== "crew"
                              ? `已绑定 ${currentAgentLabel.name}`
                              : "当前会话不可切换"}
                          </span>
                        )}
                        {agents.slice(0, 8).map((agent) => (
                          <button
                            key={agent.id}
                            className="tools-popover__item"
                            disabled={!canSelectAgent || agentBusy}
                            onClick={() => {
                              selectAgent(agent);
                              setToolsOpen(false);
                            }}
                            type="button"
                          >
                            {agent.name}
                          </button>
                        ))}
                        {agents.length === 0 && <span className="tools-popover__empty">暂无外援</span>}
                      </div>
                    )}
                    <div className="tools-popover__group">
                      <span className="tools-popover__title">Folder</span>
                      <button
                        className="tools-popover__item"
                        onClick={() => setToolsOpen(false)}
                        type="button"
                      >
                        选择文件夹
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
            {isTeamSession && (
              <>
                <div className="team-mode-picker" ref={teamModeRef}>
                  <button
                    className={"toolbar-btn" + (teamModeOpen ? " toolbar-btn--active" : "")}
                    onClick={() => setTeamModeOpen((value) => !value)}
                    title="选择团队执行模式"
                    type="button"
                  >
                    <span>Mode</span>
                    <span className="team-mode-popover__name">{teamExecutionTier}</span>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="m6 9 6 6 6-6"/>
                    </svg>
                  </button>
                  {teamModeOpen && (
                    <div className="team-mode-popover">
                      {(["auto", "fast", "standard", "ai"] as TeamExecutionTier[]).map((tier) => (
                        <button
                          key={tier}
                          className={`team-mode-popover__item${teamExecutionTier === tier ? " is-active" : ""}`}
                          onClick={() => {
                            onTeamExecutionTierChange?.(tier);
                            setTeamModeOpen(false);
                          }}
                          type="button"
                        >
                          <span className="team-mode-popover__name">{tier}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div className="toolbar-divider" />
              </>
            )}
            <div className="craft-picker" ref={craftRef}>
              <button
                className={"toolbar-btn" + (craftOpen || planActive ? " toolbar-btn--active" : "")}
                onClick={() => setCraftOpen((v) => !v)}
                title="Craft"
                type="button"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/>
                  <path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/>
                  <path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>
                </svg>
                {planActive ? "Plan" : "Craft"}
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m6 9 6 6 6-6"/>
                </svg>
              </button>
              {craftOpen && (
                <div className="craft-popover">
                  <button
                    className={"craft-popover__item" + (!planActive ? " craft-popover__item--active" : "")}
                    onClick={() => {
                      if (planActive) onExitPlan?.();
                      setCraftOpen(false);
                    }}
                    type="button"
                  >
                    <span className="craft-popover__label">Craft</span>
                  </button>
                  <button
                    className={"craft-popover__item" + (planActive ? " craft-popover__item--active" : "")}
                    onClick={() => {
                      onEnterPlan?.();
                      setCraftOpen(false);
                    }}
                    type="button"
                  >
                    <span className="craft-popover__label">Plan</span>
                  </button>
                </div>
              )}
            </div>
            <div className="toolbar-divider" />
            <div className="model-picker" ref={modelRef}>
              <button
                className={"toolbar-btn" + (modelOpen ? " toolbar-btn--active" : "")}
                onClick={() => setModelOpen((v) => !v)}
                title="选择模型"
                type="button"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2a10 10 0 1 0 10 10 4 4 0 0 1-5-5 4 4 0 0 1-5-5"/>
                  <path d="M8.5 8.5v.01"/>
                  <path d="M16 15.5v.01"/>
                  <path d="M12 12v.01"/>
                  <path d="M11 17v.01"/>
                  <path d="M7 14v.01"/>
                </svg>
                <span className="model-picker__label">{activeModelLabel || config?.model || "未配置模型"}</span>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m6 9 6 6 6-6"/>
                </svg>
              </button>
              {modelOpen && config && config.models.length > 0 && (
                <div className="model-popover">
                  {config.models.map((m) => (
                    <button
                      key={m.id}
                      className={"model-popover__item" + (m.id === (activeModelId || config.active_model_id) ? " model-popover__item--active" : "")}
                      onClick={() => { onModelChange?.(m.id); setModelOpen(false); }}
                      type="button"
                    >
                      {m.name}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="skills-picker" ref={skillsRef}>
              <button
                className={"toolbar-btn" + (skillsOpen ? " toolbar-btn--active" : "")}
                title="Skills"
                onClick={toggleSkills}
                type="button"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
                </svg>
                Skills
              </button>
              {skillsOpen && (
                <div className="skills-popover">
                  <div className="skills-popover__search">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <circle cx="11" cy="11" r="8"/>
                      <path d="m21 21-4.35-4.35"/>
                    </svg>
                    <input
                      autoFocus
                      placeholder="搜索 Skills"
                      value={skillQuery}
                      onChange={(event) => setSkillQuery(event.target.value)}
                    />
                  </div>
                  <div className="skills-popover__list">
                    {skills.length === 0 ? (
                      <span className="skills-popover__empty">暂无可用 Skills</span>
                    ) : filteredSkills.length === 0 ? (
                      <span className="skills-popover__empty">没有匹配的 Skill</span>
                    ) : (
                      filteredSkills.map((s) => (
                        <button
                          key={s.slug}
                          className="skills-popover__item"
                          onClick={() => selectSkill(s.slug)}
                          type="button"
                        >
                          <span className="skills-popover__name">/{s.slug}</span>
                          <span className="skills-popover__desc">{s.display_name || s.name}</span>
                          {s.source === "user" && <span className="skills-popover__badge">自定义</span>}
                        </button>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>
            {externalAgentsEnabled && <div className="agents-picker" ref={agentsRef}>
              <button
                className={"toolbar-btn" + (agentsOpen ? " toolbar-btn--active" : "")}
                title={currentAgentLabel && currentAgentLabel.provider !== "crew" ? `当前外援：${currentAgentLabel.name}` : "外援"}
                onClick={toggleAgents}
                type="button"
              >
                <ExternalAgentAvatar agent={currentAgentLabel || { provider: "external" }} size="compact" />
                外援
              </button>
              {agentsOpen && (
                <div className="skills-popover agents-popover">
                  {!canSelectAgent && (
                    <span className="skills-popover__empty">
                      {currentAgentLabel && currentAgentLabel.provider !== "crew"
                        ? `当前会话已绑定 ${currentAgentLabel.name}`
                        : "当前会话已有消息，不能切换外援"}
                    </span>
                  )}
                  {agents.length === 0 ? (
                    <span className="skills-popover__empty">暂无可用外援</span>
                  ) : (
                    agents.map((agent) => (
                      <button
                        key={agent.id}
                        className="skills-popover__item"
                        disabled={!canSelectAgent || agentBusy}
                        onClick={() => selectAgent(agent)}
                        type="button"
                      >
                        <ExternalAgentAvatar agent={agent} size="compact" />
                        <span className="skills-popover__name">{agent.provider}</span>
                        <span className="skills-popover__desc">{agent.name}</span>
                        {agent.model && <span className="skills-popover__badge">{agent.model}</span>}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>}
          </div>
          <div className="composer__toolbar-right">
            <button className="toolbar-btn toolbar-btn--folder" title="选择文件夹">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>
              </svg>
              选择文件夹
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="m6 9 6 6 6-6"/>
              </svg>
            </button>
            {busy ? (
              <>
                <button
                  className="composer__send composer__send--steer"
                  onClick={doSteer}
                  disabled={!text.trim()}
                  title="插入指令到当前回复（不打断生成）"
                  type="button"
                >
                  {/* 闪电：实时引导 steer */}
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                  </svg>
                </button>
                <button
                  className="composer__send composer__send--stop"
                  onClick={onStop}
                  title="停止生成（保留已生成的内容）"
                  type="button"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <rect x="6" y="6" width="12" height="12" rx="2"/>
                  </svg>
                </button>
              </>
            ) : (
              <button
                className="composer__send"
                onClick={submit}
                title="发送"
                type="button"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="22" y1="2" x2="11" y2="13"/>
                  <polygon points="22 2 15 22 11 13 2 9 22 2"/>
                </svg>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      const base64 = result.split(",")[1] || result;
      resolve(base64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
