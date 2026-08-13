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
  /** 传入后回答正文中的 [[Wiki 页面名]] 引用渲染为可点击链接（Wiki 问答场景）。 */
  onWikiLink?: (title: string) => void;
}

// 从消息中提取计时信息。
function extractTiming(
  messages: UiMessage[],
): { startedAt: number | null; fallbackMs: number } {
  for (const m of messages) {
    if (m.turnStartedAt != null)
      return { startedAt: m.turnStartedAt, fallbackMs: m.turnDurationMs ?? 0 };
    if (m.turnDurationMs != null)
      return { startedAt: null, fallbackMs: m.turnDurationMs };
  }
  return { startedAt: null, fallbackMs: 0 };
}

// 微型子组件：只有它每秒 tick 刷新时间显示，避免整个 AgentTurn 树重渲染。
function StreamingDuration({
  isStreaming,
  startedAt,
  fallbackMs,
  processNoun,
}: {
  isStreaming: boolean;
  startedAt: number | null;
  fallbackMs: number;
  processNoun: string;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!isStreaming) return;
    const timer = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [isStreaming]);

  const ms =
    isStreaming && startedAt != null ? Date.now() - startedAt : fallbackMs;

  return (
    <span className="msg__fold-label">
      {processNoun} · {isStreaming ? "处理中" : "已处理"}{" "}
      {formatDuration(ms)}
    </span>
  );
}

export default function AgentTurn({
  messages,
  isStreaming,
  onApprovePlan,
  onRejectPlan,
  onRejectAndExitPlan,
  onWikiLink,
}: Props) {
  const [pinnedOpen, setPinnedOpen] = useState<boolean | null>(null);
  const [viewingPage, setViewingPage] = useState<WikiPage | null>(null);

  const { startedAt, fallbackMs } = useMemo(
    () => extractTiming(messages),
    [messages],
  );

  const toolCount = useMemo(
    () => messages.reduce((n, m) => n + (m.toolCalls?.length ?? 0), 0),
    [messages],
  );
  const commandCount = useMemo(
    () =>
      messages.reduce(
        (n, m) =>
          n +
          (m.toolCalls?.filter(
            (tc) => tc.name === "terminal" || tc.name === "process",
          ).length ?? 0),
        0,
      ),
    [messages],
  );

  // processItems / textParts / fileChanges 只在 messages 变化时重建。
  const { processItems, textParts, fileChanges } = useMemo(() => {
    // 用消息 id 标识当前 turn 的"最终回复"。
    let lastTextMessageId: string | null = null;
    for (let k = messages.length - 1; k >= 0; k -= 1) {
      if (messages[k].role === "assistant" && messages[k].text?.trim()) {
        lastTextMessageId = messages[k].id;
        break;
      }
    }

    const processItems: ProcessTimelineItem[] = [];
    const textParts: ReactNode[] = [];
    const fileChanges = new Map<
      string,
      NonNullable<UiMessage["turnFileChanges"]>[number]
    >();

    messages.forEach((m, i) => {
      for (const file of m.turnFileChanges || [])
        fileChanges.set(file.path, file);
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
        !isStreaming &&
        m.role === "assistant" &&
        !text &&
        Boolean(thinking) &&
        !hasTools &&
        !m.planReview;
      const hasRealThinking =
        Boolean(thinking) &&
        !thinkingLooksLikeOnlyAnswer &&
        (!text || thinking !== text);

      if (hasRealThinking) {
        processItems.push({
          kind: "thinking",
          id: `${m.id}-thinking`,
          content: m.thinking!,
          done: !isStreaming,
        });
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
                <WikiCard
                  key={page.id}
                  page={page}
                  onView={(p) => setViewingPage(p)}
                />
              ))}
            </div>
          </div>,
        );
      }
      const displayText = thinkingLooksLikeOnlyAnswer ? m.thinking : m.text;
      if (displayText) {
        const isIntermediate =
          m.role === "assistant" && m.id !== lastTextMessageId;
        if (isIntermediate) {
          processItems.push({
            kind: "narration",
            id: `${m.id}-narration`,
            content: displayText,
          });
        } else {
          textParts.push(
            <div key={`${m.id}-text`} className="msg__text md-body">
              <MarkdownContent
                content={displayText}
                isStreaming={
                  isStreaming && !thinkingLooksLikeOnlyAnswer
                }
                onWikiLink={onWikiLink}
              />
            </div>,
          );
        }
      } else if (
        isStreaming &&
        m.role === "assistant" &&
        i === messages.length - 1
      ) {
        textParts.push(
          <div
            key={`${m.id}-typing`}
            className="msg__text md-body typing-inline"
          >
            <span />
            <span />
            <span />
          </div>,
        );
      }
    });

    return { processItems, textParts, fileChanges };
  }, [messages, isStreaming, onApprovePlan, onRejectPlan, onRejectAndExitPlan]);

  const defaultOpen = isStreaming;
  const open = pinnedOpen ?? defaultOpen;
  const hasThinkingItem = processItems.some(
    (item) => item.kind === "thinking",
  );
  const processNoun =
    commandCount > 0 && commandCount === toolCount
      ? `运行 ${commandCount} 个命令`
      : toolCount > 0
        ? `使用了 ${toolCount} 个工具`
        : hasThinkingItem
          ? "思考已完成"
          : "处理过程";

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
              setPinnedOpen(
                (e.currentTarget as HTMLDetailsElement).open,
              );
            }}
          >
            <summary className="msg__fold-summary">
              <StreamingDuration
                isStreaming={isStreaming}
                startedAt={startedAt}
                fallbackMs={fallbackMs}
                processNoun={processNoun}
              />
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
              <AgentProcessTimeline
                items={processItems}
                isStreaming={isStreaming}
              />
            </div>
          </details>
        )}
        {textParts}
        <TurnFileChangesCard files={Array.from(fileChanges.values())} />
      </div>
      {viewingPage && (
        <WikiPageView
          page={viewingPage}
          onClose={() => setViewingPage(null)}
        />
      )}
    </div>
  );
}
