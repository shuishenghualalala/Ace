import { useEffect, useState } from "react";
import type { AppConfig, Attachment, ExternalAgent, FollowupQuestion, PendingMessage, Session, Skill, TeamExecutionTier, TeamMemberView, TodoItem, UiMessage } from "../types";
import { api } from "../api";
import MessageList from "./MessageList";
import Composer from "./Composer";
import QueuePanel from "./QueuePanel";
import ScenarioHub from "./ScenarioHub";
import RunningIntro from "./RunningIntro";
import TodoProgressPanel from "./TodoProgressPanel";
import { shouldShowTodoPanel } from "../lib/todoUtils";

export interface Props {
  messages: UiMessage[];
  busy: boolean;
  compactingContext?: boolean;
  queueHint?: string;
  pendingQueue: PendingMessage[];
  config: AppConfig | null;
  attachments: Attachment[];
  onSend: (text: string, attachments: Attachment[], subScenario?: string) => void;
  onStop?: () => void;
  onSteer?: (text: string) => void;
  onRemoveFromQueue?: (index: number) => void;
  onEditQueueItem?: (index: number, newQuery: string) => void;
  onSendQueueItemNow?: (id: string) => void;
  onAttachmentsChange: (attachments: Attachment[]) => void;
  onModelChange?: (modelId: string) => void;
  currentAgentLabel?: Session["agent_label"];
  isTeamSession?: boolean;
  teamExecutionTier?: TeamExecutionTier;
  onTeamExecutionTierChange?: (tier: TeamExecutionTier) => void;
  teamMembers?: TeamMemberView[];
  compactTools?: boolean;
  toolCollapseLevel?: number;
  canSelectAgent?: boolean;
  onSelectAgent?: (agent: ExternalAgent) => void | Promise<void>;
  planActive?: boolean;
  onEnterPlan?: () => void;
  onExitPlan?: () => void;
  onApprovePlan?: () => void;
  onRejectPlan?: () => void;
  onRejectAndExitPlan?: () => void;
  onAsk?: (text: string) => void;
  followupQuestion?: FollowupQuestion | null;
  onAnswerFollowup?: (
    questionId: string,
    answers: { question_id: string; answers: string[] }[],
  ) => boolean | void;
  onDismissFollowup?: () => void;
  todos?: TodoItem[];
  editDraft?: { messageId: string; text: string } | null;
  onEditMessage?: (message: UiMessage) => void;
  onCancelEdit?: () => void;
  /** 附件上传归属（wiki 会话时传入）：透传给 Composer 的上传调用 */
  uploadContext?: { sessionId?: string; kbId?: string };
  /** 传入后回答正文中的 [[Wiki 页面名]] 引用渲染为可点击链接（Wiki 问答场景）。 */
  onWikiLink?: (title: string) => void;
}

const SKILL_ICONS: Record<string, string> = {
  coding: "🖥️",
  writing: "✉️",
};

export default function ChatPanel({
  messages,
  busy,
  compactingContext = false,
  queueHint,
  pendingQueue,
  config,
  attachments,
  onSend,
  onStop,
  onSteer,
  onRemoveFromQueue,
  onEditQueueItem,
  onSendQueueItemNow,
  onAttachmentsChange,
  onModelChange,
  currentAgentLabel,
  isTeamSession,
  teamExecutionTier,
  onTeamExecutionTierChange,
  teamMembers,
  compactTools,
  toolCollapseLevel,
  canSelectAgent,
  onSelectAgent,
  planActive,
  onEnterPlan,
  onExitPlan,
  onApprovePlan,
  onRejectPlan,
  onRejectAndExitPlan,
  onAsk,
  followupQuestion,
  onAnswerFollowup,
  onDismissFollowup,
  editDraft,
  onEditMessage,
  onCancelEdit,
  todos = [],
  uploadContext,
  onWikiLink,
}: Props) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const isEmpty = messages.length === 0 && !busy && !followupQuestion && !isTeamSession;
  // 场景化推荐：预填文本 + nonce（触发注入）+ 当前激活的场景 chip
  const [prefillText, setPrefillText] = useState("");
  const [prefillNonce, setPrefillNonce] = useState(0);
  const [scenarioChip, setScenarioChip] = useState<{ label: string; subId: string } | null>(null);

  useEffect(() => {
    api.skills()
      .then((items) => setSkills(items.filter((skill) => skill.featured)))
      .catch(() => setSkills([]));
  }, []);

  return (
    <div className="chat-panel">
      {isEmpty ? (
        <div className="welcome">
          {/* Mascot */}
          <div className="welcome__mascot">
            <svg viewBox="0 0 200 200" width="180" height="180" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
              {/* Robot cat wireframe - more rounded and cute */}
              {/* Outer head shape */}
              <ellipse cx="100" cy="88" rx="52" ry="46" />
              {/* Inner head detail */}
              <ellipse cx="100" cy="88" rx="44" ry="38" />

              {/* Left ear */}
              <path d="M58 58 Q52 35 58 22 Q70 30 78 48" />
              <path d="M62 52 Q58 38 62 28 Q68 34 72 46" />
              {/* Right ear */}
              <path d="M142 58 Q148 35 142 22 Q130 30 122 48" />
              <path d="M138 52 Q142 38 138 28 Q132 34 128 46" />

              {/* Antenna */}
              <line x1="100" y1="42" x2="100" y2="15" />
              <circle cx="100" cy="11" r="5" />
              <circle cx="100" cy="11" r="2" fill="currentColor" />

              {/* Left eye - large round */}
              <circle cx="78" cy="82" r="14" />
              <circle cx="78" cy="82" r="10" />
              <circle cx="78" cy="82" r="5" fill="currentColor" />
              <circle cx="82" cy="78" r="2" fill="white" />

              {/* Right eye */}
              <circle cx="122" cy="82" r="14" />
              <circle cx="122" cy="82" r="10" />
              <circle cx="122" cy="82" r="5" fill="currentColor" />
              <circle cx="126" cy="78" r="2" fill="white" />

              {/* Nose */}
              <ellipse cx="100" cy="98" rx="4" ry="3" fill="currentColor" />

              {/* Mouth - cute W shape */}
              <path d="M88 108 Q94 114 100 108 Q106 114 112 108" />
              <path d="M94 104 Q100 108 106 104" />

              {/* Whiskers */}
              <line x1="48" y1="90" x2="30" y2="86" />
              <line x1="48" y1="96" x2="28" y2="96" />
              <line x1="48" y1="102" x2="30" y2="106" />
              <line x1="152" y1="90" x2="170" y2="86" />
              <line x1="152" y1="96" x2="172" y2="96" />
              <line x1="152" y1="102" x2="170" y2="106" />

              {/* Headphones band */}
              <path d="M52 82 Q48 60 65 48" />
              <path d="M148 82 Q152 60 135 48" />
              {/* Left headphone */}
              <rect x="46" y="74" width="12" height="22" rx="5" />
              <rect x="48" y="78" width="8" height="14" rx="3" />
              {/* Right headphone */}
              <rect x="142" y="74" width="12" height="22" rx="5" />
              <rect x="144" y="78" width="8" height="14" rx="3" />

              {/* Body - rounded rectangle */}
              <rect x="68" y="128" width="64" height="50" rx="18" />
              {/* Body grid pattern */}
              <line x1="68" y1="148" x2="132" y2="148" />
              <line x1="68" y1="165" x2="132" y2="165" />
              <line x1="90" y1="128" x2="90" y2="178" />
              <line x1="110" y1="128" x2="110" y2="178" />
              {/* Body center detail */}
              <circle cx="100" cy="156" r="6" />

              {/* Left arm */}
              <path d="M68 145 Q52 158 48 172" />
              <ellipse cx="46" cy="176" rx="7" ry="5" transform="rotate(-20 46 176)" />
              {/* Right arm */}
              <path d="M132 145 Q148 158 152 172" />
              <ellipse cx="154" cy="176" rx="7" ry="5" transform="rotate(20 154 176)" />

              {/* Left leg */}
              <ellipse cx="85" cy="182" rx="14" ry="9" />
              <ellipse cx="85" cy="182" rx="8" ry="5" />
              {/* Right leg */}
              <ellipse cx="115" cy="182" rx="14" ry="9" />
              <ellipse cx="115" cy="182" rx="8" ry="5" />

              {/* Tail */}
              <path d="M132 165 Q155 170 158 155 Q160 145 152 148" />
            </svg>
          </div>

          <h1 className="welcome__title">Claw Your Ideas Into Reality</h1>
          <p className="welcome__subtitle">Triggered Anywhere, Completed Locally</p>

          {/* Skill Tags */}
          {skills.length > 0 && (
            <div className="welcome__skills">
              {skills.map((s) => (
                <button
                  key={s.slug}
                  className="skill-tag"
                  title={s.description}
                  onClick={() => onSend(`/${s.slug}`, [])}
                >
                  <span className="skill-tag__icon">{SKILL_ICONS[s.slug] ?? "⚡"}</span>
                  <span>{s.display_name || s.name}</span>
                </button>
              ))}
            </div>
          )}

          {/* 场景化推荐 */}
          <ScenarioHub
            onPick={(sub, parent) => {
              setPrefillText(sub.query);
              setPrefillNonce((n) => n + 1);
              setScenarioChip({ label: parent.title, subId: sub.id });
            }}
          />

          {/* Input Area */}
          <div className="welcome__input-wrap">
            <Composer
              config={config}
              busy={busy}
              attachments={attachments}
              onSend={(text, atts, subScenario) => {
                onSend(text, atts, subScenario);
                setScenarioChip(null);
              }}
              onStop={onStop}
              onAttachmentsChange={onAttachmentsChange}
              onModelChange={onModelChange}
              currentAgentLabel={currentAgentLabel}
              canSelectAgent={canSelectAgent}
              onSelectAgent={onSelectAgent}
              compactTools={compactTools}
              toolCollapseLevel={toolCollapseLevel}
              planActive={planActive}
              onEnterPlan={onEnterPlan}
              onExitPlan={onExitPlan}
              prefillText={prefillText}
              prefillNonce={prefillNonce}
              scenarioChip={scenarioChip ? { label: scenarioChip.label, onClear: () => setScenarioChip(null) } : null}
              subScenario={scenarioChip?.subId}
              editDraft={editDraft}
              onCancelEdit={onCancelEdit}
              uploadContext={uploadContext}
              compact
            />
          </div>
          <p className="welcome__footer">内容由 AI 生成，请核实重要信息。</p>
        </div>
      ) : (
        <>
          <MessageList
            messages={messages}
            busy={busy}
            onApprovePlan={onApprovePlan}
            onRejectPlan={onRejectPlan}
            onRejectAndExitPlan={onRejectAndExitPlan}
            onAsk={onAsk}
            followupQuestion={followupQuestion}
            onAnswerFollowup={onAnswerFollowup}
            onDismissFollowup={onDismissFollowup}
            onEditMessage={onEditMessage}
            teamMembers={teamMembers}
            showEmptyState={!isTeamSession}
            currentAgentLabel={currentAgentLabel}
            onWikiLink={onWikiLink}
          />
          <QueuePanel
            queue={pendingQueue}
            queueHint={queueHint}
            busy={busy}
            onRemove={(i) => onRemoveFromQueue?.(i)}
            onEdit={(i, q) => onEditQueueItem?.(i, q)}
            onSendNow={(id) => onSendQueueItemNow?.(id)}
          />
          {busy && !compactingContext && <RunningIntro />}
          {compactingContext && (
            <div className="context-compaction-notice" role="status" aria-live="polite">
              <svg className="nav-agent-logo context-compaction-notice__logo" width="18" height="18" viewBox="3 3 18 18" aria-hidden="true">
                <path className="nav-agent-logo__blob" d="M5.2 13.2c0-4.5 2.9-6.9 6.8-6.9 4.5 0 7 2.8 7 6.2 0 3.8-2.5 5.5-7.2 5.5-4.3 0-6.6-1.4-6.6-4.8Z" />
                <path className="nav-agent-logo__cap" d="M9 6.7c.7-1.1 1.7-1.7 3.1-1.7 1.3 0 2.3.5 3 1.5" />
                <path className="nav-agent-logo__shine" d="M9.6 10.8v1.9M14.4 10.8v1.9" />
                <path className="nav-agent-logo__pixel" d="M18.8 8.2h1.5M19.55 7.45v1.5" />
              </svg>
              <strong>正在压缩上下文……</strong>
              <span>如果多次压缩上下文，Agent 的能力会受到影响，建议开启新对话～</span>
            </div>
          )}
          {shouldShowTodoPanel(todos) && (
            <TodoProgressPanel todos={todos} />
          )}
          <Composer
            config={config}
            busy={busy}
            attachments={attachments}
            onSend={onSend}
            onStop={onStop}
            onSteer={onSteer}
            onAttachmentsChange={onAttachmentsChange}
            onModelChange={onModelChange}
            currentAgentLabel={currentAgentLabel}
            isTeamSession={isTeamSession}
            teamExecutionTier={teamExecutionTier}
            onTeamExecutionTierChange={onTeamExecutionTierChange}
            canSelectAgent={canSelectAgent}
            onSelectAgent={onSelectAgent}
            compactTools={compactTools}
            toolCollapseLevel={toolCollapseLevel}
            planActive={planActive}
            onEnterPlan={onEnterPlan}
            onExitPlan={onExitPlan}
            editDraft={editDraft}
            onCancelEdit={onCancelEdit}
            uploadContext={uploadContext}
          />
        </>
      )}
    </div>
  );
}
