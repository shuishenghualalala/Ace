import { useState, type ReactNode } from "react";
import type { AgentTurnState } from "../lib/agentTurnState";
import { processSummaryLabel } from "../lib/processDisplay";
import { useProcessTiming } from "../hooks/useProcessTiming";
import AgentAvatarLogo from "./AgentAvatarLogo";
import { AgentProcessFold } from "./AgentProcessTimeline";
import MarkdownContent from "./MarkdownContent";
import { PlanReviewCard } from "./PlanReviewPanel";
import TurnFileChangesCard from "./TurnFileChangesCard";

export interface AgentTurnIdentity {
  name: string;
  badge?: string;
  role?: string;
  tone?: number;
  crewLogo?: boolean;
  teamLogo?: boolean;
  external?: boolean;
}

interface Props {
  identity: AgentTurnIdentity;
  state: AgentTurnState;
  isStreaming: boolean;
  id?: string;
  className?: string;
  bubbleClassName?: string;
  processClassName?: string;
  collapsibleTitle?: string;
  afterContent?: ReactNode;
  nameSuffix?: ReactNode;
  onApprovePlan?: () => void;
  onRejectPlan?: () => void;
  onRejectAndExitPlan?: () => void;
}

export default function AgentTurnBubble({
  identity,
  state,
  isStreaming,
  id,
  className = "",
  bubbleClassName = "",
  processClassName = "",
  collapsibleTitle,
  afterContent,
  nameSuffix,
  onApprovePlan,
  onRejectPlan,
  onRejectAndExitPlan,
}: Props) {
  const [pinnedOpen, setPinnedOpen] = useState<boolean | null>(null);
  const timing = useProcessTiming(state, isStreaming);
  const processNoun = processSummaryLabel({
    isStreaming,
    toolCount: state.toolCount,
    commandCount: state.commandCount,
    hasThinking: state.hasThinking,
  });
  const durationLabel = state.turnDurationMs != null || state.turnStartedAt != null || state.timestamp != null
    ? ` ${timing.label}`
    : "";
  const processTitle = `${processNoun} · ${isStreaming ? "处理中" : "已处理"}${durationLabel}`;
  const content = (
    <>
      <AgentProcessFold
        items={state.processItems}
        label={processTitle}
        isStreaming={isStreaming}
        open={pinnedOpen ?? isStreaming}
        onOpenChange={setPinnedOpen}
        className={processClassName}
      />
      {state.planReviews.map((item) => (
        <PlanReviewCard
          key={item.id}
          review={item.review}
          onApprove={() => onApprovePlan?.()}
          onReject={() => onRejectPlan?.()}
          onRejectAndExit={() => onRejectAndExitPlan?.()}
        />
      ))}
      {state.responses.map((response) => (
        <div key={response.id} className="msg__text md-body">
          <MarkdownContent content={response.content} isStreaming={response.streaming} />
        </div>
      ))}
      <TurnFileChangesCard files={state.fileChanges} />
      {state.showTyping && (
        <div className="msg__text md-body typing-inline">
          <span />
          <span />
          <span />
        </div>
      )}
      {afterContent}
    </>
  );
  const wrappedContent = bubbleClassName
    ? <div className={bubbleClassName}>{content}</div>
    : content;

  return (
    <div
      className={`msg${className ? ` ${className}` : ""}`}
      id={id}
      data-message-id={id?.replace(/^message-/, "")}
    >
      {identity.teamLogo ? (
        <div className="msg__avatar msg__avatar--team team-internal__avatar">
          <span className="session__team-logo" aria-hidden="true"><i /><i /></span>
        </div>
      ) : identity.crewLogo ? (
        <div className="msg__avatar bot team-internal__avatar">
          <AgentAvatarLogo />
        </div>
      ) : (
        <span className={`agent-avatar agent-avatar--message${identity.external ? ` agent-avatar--external agent-provider-tone-${identity.tone ?? 0}` : ` agent-tone-${identity.tone ?? 0}`}`}>
          {identity.badge?.trim() || "?"}
        </span>
      )}
      <div className="msg__body">
        <div className={`msg__name${className.includes("team-internal") ? " team-internal__name" : ""}`}>
          {className.includes("team-internal") ? <strong>{identity.name}</strong> : identity.name}
          {identity.role && <em>{identity.role}</em>}
          {nameSuffix}
        </div>
        {collapsibleTitle ? (
          <details className="team-internal__collapse">
            <summary>{collapsibleTitle}</summary>
            {wrappedContent}
          </details>
        ) : wrappedContent}
      </div>
    </div>
  );
}
