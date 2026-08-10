import { useEffect, useRef } from "react";
import type { FollowupQuestion, Session, TeamMemberView, UiMessage } from "../types";
import { groupMessagesIntoTurns } from "../lib/chatTurns";
import { buildAgentTurnState } from "../lib/agentTurnState";
import AgentTurn from "./AgentTurn";
import AgentTurnBubble from "./AgentTurnBubble";
import MessageItem from "./MessageItem";
import FollowupQuestionCard from "./FollowupQuestionCard";
import AgentAvatarLogo from "./AgentAvatarLogo";
import { externalAgentInitial, externalAgentTone } from "./ExternalAgentAvatar";

interface Props {
  messages: UiMessage[];
  busy: boolean;
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
  onEditMessage?: (message: UiMessage) => void;
  teamMembers?: TeamMemberView[];
  showEmptyState?: boolean;
  currentAgentLabel?: Session["agent_label"];
}

export default function MessageList({
  messages,
  busy,
  onApprovePlan,
  onRejectPlan,
  onRejectAndExitPlan,
  onAsk,
  followupQuestion,
  onAnswerFollowup,
  onDismissFollowup,
  onEditMessage,
  teamMembers,
  showEmptyState = true,
  currentAgentLabel,
}: Props) {
  const messagesRef = useRef<HTMLDivElement>(null);
  const followOutputRef = useRef(true);
  const previousMessageCountRef = useRef(messages.length);
  const scrollFrameRef = useRef<number | null>(null);
  const turns = groupMessagesIntoTurns(messages);
  // 流式中的回合 = 最后一个回合（仅当它本身是 agent 回合时）。
  // 不能用「最后一个 agent 回合」：发第二条消息时新回合尚未产生 assistant，
  // 会错误地把已结束的上一回合标记为流式，导致新消息无 loading、计时错位。
  const lastIdx = turns.length - 1;
  const activeAgentTurnIdx =
    busy && lastIdx >= 0 && turns[lastIdx].kind === "agent" ? lastIdx : -1;
  const latestUserMessageId = !busy
    ? [...messages].reverse().find((msg) => msg.role === "user")?.id
    : undefined;

  useEffect(() => {
    const appendedMessage = messages.length > previousMessageCountRef.current;
    const lastMessage = messages[messages.length - 1];
    if (appendedMessage && lastMessage?.role === "user") {
      followOutputRef.current = true;
    }
    previousMessageCountRef.current = messages.length;

    const container = messagesRef.current;
    if (container && followOutputRef.current) {
      if (scrollFrameRef.current != null) cancelAnimationFrame(scrollFrameRef.current);
      scrollFrameRef.current = requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
        scrollFrameRef.current = null;
      });
    }
    return () => {
      if (scrollFrameRef.current != null) {
        cancelAnimationFrame(scrollFrameRef.current);
        scrollFrameRef.current = null;
      }
    };
  }, [messages, busy, followupQuestion]);

  const followupOrigin = followupQuestion?.origin;
  const followupMember = followupOrigin
    ? (
        (followupOrigin.agent_id ? teamMembers?.find((member) => member.agentId === followupOrigin.agent_id) : undefined)
        || (followupOrigin.agent_name ? teamMembers?.find((member) => member.name === followupOrigin.agent_name) : undefined)
        || (followupOrigin.agent_id === "leader" ? teamMembers?.find((member) => member.isLeader) : undefined)
      )
    : undefined;
  const followupName = followupMember?.name || followupOrigin?.agent_name || "Crew";
  const followupTone = followupMember?.tone ?? 0;
  const followupAgentId = String(followupOrigin?.agent_id || followupMember?.agentId || "").trim();
  const followupIsCrew = followupAgentId === "crew::builtin";
  const currentProvider = String(currentAgentLabel?.provider || "crew").trim().toLowerCase();
  const isAcpSession = currentProvider !== "crew" && currentProvider !== "team";

  return (
    <div
      className="messages"
      ref={messagesRef}
      onScroll={(event) => {
        const container = event.currentTarget;
        const distanceToBottom =
          container.scrollHeight - container.scrollTop - container.clientHeight;
        followOutputRef.current = distanceToBottom <= 48;
      }}
    >
      <div className="messages__inner">
        {showEmptyState && messages.length === 0 && !busy && (
          <div className="empty">
            <h2>开始一段对话</h2>
            <div>单 Agent 直接执行任务；切到 Team 模式可组建多智能体协同。</div>
          </div>
        )}
        {turns.map((turn, idx) =>
          turn.kind === "user" ? (
            <MessageItem
              key={turn.message.id}
              msg={turn.message}
              isStreaming={
                busy
                && turn.message.role === "team_internal"
                && turn.message.eventType === "team_stream"
              }
              canEdit={turn.message.id === latestUserMessageId}
              onEdit={onEditMessage}
              teamMembers={teamMembers}
            />
          ) : isAcpSession ? (
            <AgentTurnBubble
              key={turn.turnId}
              identity={{
                name: currentAgentLabel?.name || currentAgentLabel?.provider || "Agent",
                badge: externalAgentInitial({
                  provider: currentAgentLabel?.provider || "external",
                  display_badge: currentAgentLabel?.display_badge,
                }),
                tone: externalAgentTone(currentAgentLabel?.provider || "external"),
                external: true,
              }}
              state={buildAgentTurnState(turn.messages, busy && idx === activeAgentTurnIdx)}
              isStreaming={busy && idx === activeAgentTurnIdx}
              onApprovePlan={onApprovePlan}
              onRejectPlan={onRejectPlan}
              onRejectAndExitPlan={onRejectAndExitPlan}
            />
          ) : (
            <AgentTurn
              key={turn.turnId}
              messages={turn.messages}
              isStreaming={busy && idx === activeAgentTurnIdx}
              onApprovePlan={onApprovePlan}
              onRejectPlan={onRejectPlan}
              onRejectAndExitPlan={onRejectAndExitPlan}
              onAsk={onAsk}
            />
          ),
        )}
        {followupQuestion && (
          <div className="msg followup-msg">
            {followupMember && !followupIsCrew ? (
              <span className={`agent-avatar agent-avatar--message agent-tone-${followupTone}`}>
                {followupName.slice(0, 1).toUpperCase()}
              </span>
            ) : (
              <div className="msg__avatar bot">
                <AgentAvatarLogo />
              </div>
            )}
            <div className="msg__body">
              <div className="msg__name">
                {followupName === "Crew" ? "Crew" : `${followupName} 正在询问`}
              </div>
              <FollowupQuestionCard
                question={followupQuestion}
                onSubmit={(answers) => onAnswerFollowup?.(followupQuestion.question_id, answers)}
                onDismiss={onDismissFollowup}
              />
            </div>
          </div>
        )}
        {busy && messages.length > 0 && messages[messages.length - 1].role === "user" && (
          <div className="typing">
            <span /> <span /> <span />
          </div>
        )}
      </div>
    </div>
  );
}
