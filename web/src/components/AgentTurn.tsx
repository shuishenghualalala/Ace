import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { UiMessage, WikiPage } from "../types";
import { formatDuration } from "../lib/formatDuration";
import AgentAvatarLogo from "./AgentAvatarLogo";
import MarkdownContent from "./MarkdownContent";
import { PlanReviewCard } from "./PlanReviewPanel";
import AgentProcessTimeline, { type ProcessTimelineItem } from "./AgentProcessTimeline";
import WikiCard from "./WikiCard";
import WikiPageView from "./WikiPageView";
import TurnFileChangesCard from "./TurnFileChangesCard";

interface Props {
  messages: UiMessage[];
  /** 该回合是否仍在接收流式分片 */
  isStreaming: boolean;
  onApprovePlan?: () => void;
  onRejectPlan?: () => void;
  onRejectAndExitPlan?: () => void;
  onAsk?: (text: string) => void;
}

function computeTurnDurationMs(messages: UiMessage[], isStreaming: boolean): number {
  const timedMessage = messages.find(
    (m) => m.turnDurationMs != null || m.turnStartedAt != null,
  );
  if (!timedMessage) return 0;
  if (isStreaming && timedMessage.turnStartedAt != null) {
    return Date.now() - timedMessage.turnStartedAt;
  }
  return timedMessage.turnDurationMs ?? 0;
}

export default function AgentTurn({ messages, isStreaming, onApprovePlan, onRejectPlan, onRejectAndExitPlan }: Props) {
  const [pinnedOpen, setPinnedOpen] = useState<boolean | null>(null);
  const [viewingPage, setViewingPage] = useState<WikiPage | null>(null);
  // 流式中每秒 tick 一次，让「处理中 XXs」真正实时跳动；
  // 否则 durationMs 只在收到新分片时重算，长工具执行期间会冻住。
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!isStreaming) return;
    const timer = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [isStreaming]);

  const durationMs = useMemo(
    () => computeTurnDurationMs(messages, isStreaming),
    [messages, isStreaming],
  );

  // 本回合累计运行的工具数（toolMap 按回合聚合写进 assistant 消息）。
  const toolCount = messages.reduce((n, m) => n + (m.toolCalls?.length ?? 0), 0);
  const commandCount = messages.reduce(
    (n, m) => n + (m.toolCalls?.filter((tc) => tc.name === "terminal" || tc.name === "process").length ?? 0),
    0,
  );

  // 用消息 id（而非数组下标）标识当前 turn 的“最终回复”。
  // 流式中下标会漂移，若之前已渲染在 body 里的节点被 React 当成同 key
  // 节点搬进 <details> 时间线，会出现文字截断/错位。
  let lastTextMessageId: string | null = null;
  for (let k = messages.length - 1; k >= 0; k -= 1) {
    if (messages[k].role === "assistant" && messages[k].text?.trim()) {
      lastTextMessageId = messages[k].id;
      break;
    }
  }

  const processItems: ProcessTimelineItem[] = [];
  const textParts: ReactNode[] = [];
  const fileChanges = new Map<string, NonNullable<UiMessage["turnFileChanges"]>[number]>();

  messages.forEach((m, i) => {
    for (const file of m.turnFileChanges || []) fileChanges.set(file.path, file);
    if (m.role === "status") {
      processItems.push({ kind: "status", id: m.id, text: m.text });
      return;
    }
    if (m.role === "error") {
      processItems.push({ kind: "error", id: m.id, text: m.text });
      return;
    }
    const text = m.text?.trim() ?? "";
    const thinking = m.thinking?.trim() ?? "";
    const hasTools = Boolean(m.toolCalls && m.toolCalls.length > 0);
    const thinkingLooksLikeOnlyAnswer =
      !isStreaming && m.role === "assistant" && !text && Boolean(thinking) && !hasTools && !m.planReview;
    const hasRealThinking =
      Boolean(thinking) && !thinkingLooksLikeOnlyAnswer && (!text || thinking !== text);

    if (hasRealThinking) {
      processItems.push({ kind: "thinking", id: `${m.id}-thinking`, content: m.thinking!, done: !isStreaming });
    }
    if (m.toolCalls && m.toolCalls.length > 0) {
      m.toolCalls.forEach((tool) => {
        processItems.push({ kind: "tool", id: tool.toolCallId, tool });
      });
    }
    if (m.planReview) {
      textParts.push(
        <PlanReviewCard
          key={`${m.id}-plan`}
          review={m.planReview}
          onApprove={() => onApprovePlan?.()}
          onReject={() => onRejectPlan?.()}
          onRejectAndExit={() => onRejectAndExitPlan?.()}
        />,
      );
    }
    if (m.wikiCards && m.wikiCards.length > 0) {
      textParts.push(
        <div key={`${m.id}-wiki`} className="wiki-cards-panel">
          <div className="wiki-cards-panel__title">Wiki 结果</div>
          <div className="wiki-cards-panel__grid">
            {m.wikiCards.map((page) => (
              <WikiCard key={page.id} page={page} onView={(p) => setViewingPage(p)} />
            ))}
          </div>
        </div>,
      );
    }
    const displayText = thinkingLooksLikeOnlyAnswer ? m.thinking : m.text;
    if (displayText) {
      const isIntermediate = m.role === "assistant" && m.id !== lastTextMessageId;
      if (isIntermediate) {
        // 中间回复作为时间线的一项（无图标），让虚线连续穿过；
        // 字体沿用正文样式（黑色、正文字号）。
        // key 与 body 最终回复不同，避免 React 同 key 跨父容器移动 DOM。
        processItems.push({ kind: "narration", id: `${m.id}-narration`, content: displayText });
      } else {
        textParts.push(
          <div key={`${m.id}-text`} className="msg__text md-body">
            <MarkdownContent content={displayText} isStreaming={isStreaming && !thinkingLooksLikeOnlyAnswer} />
          </div>,
        );
      }
    } else if (isStreaming && m.role === "assistant" && i === messages.length - 1) {
      textParts.push(
        <div key={`${m.id}-typing`} className="msg__text md-body typing-inline">
          <span />
          <span />
          <span />
        </div>,
      );
    }
  });

  const defaultOpen = isStreaming;
  const open = pinnedOpen ?? defaultOpen;
  const hasThinkingItem = processItems.some((item) => item.kind === "thinking");
  const processNoun = commandCount > 0 && commandCount === toolCount
    ? `运行 ${commandCount} 个命令`
    : toolCount > 0
      ? `使用了 ${toolCount} 个工具`
      : hasThinkingItem
        ? "思考已完成"
        : "处理过程";
  const label = `${processNoun} · ${isStreaming ? "处理中" : "已处理"} ${formatDuration(durationMs)}`;

  return (
    <div className="msg">
      <div className="msg__avatar bot">
        <AgentAvatarLogo />
      </div>
      <div className="msg__body">
        <div className="msg__name">Crew</div>
        {processItems.length > 0 && (
          <details
            className="msg__foldable"
            open={open}
            onToggle={(e) => {
              setPinnedOpen((e.currentTarget as HTMLDetailsElement).open);
            }}
          >
            <summary className="msg__fold-summary">
              <span className="msg__fold-label">{label}</span>
              <svg
                className="msg__fold-caret"
                viewBox="0 0 24 24"
                width="12"
                height="12"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.4"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <polyline points="9 6 15 12 9 18" />
              </svg>
            </summary>
            <div className="msg__fold-content">
              <AgentProcessTimeline items={processItems} isStreaming={isStreaming} />
            </div>
          </details>
        )}
        {textParts}
        <TurnFileChangesCard files={Array.from(fileChanges.values())} />
      </div>
      {viewingPage && <WikiPageView page={viewingPage} onClose={() => setViewingPage(null)} />}
    </div>
  );
}
