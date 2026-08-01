import { useState } from "react";
import type { ToolCallInfo } from "../types";
import { toolDisplayTitle } from "../lib/processDisplay";
import type { ProcessTimelineItem } from "../lib/agentTurnState";
import MarkdownContent from "./MarkdownContent";

export type { ProcessTimelineItem } from "../lib/agentTurnState";

interface Props {
  items: ProcessTimelineItem[];
  isStreaming?: boolean;
}

interface FoldProps extends Props {
  label: string;
  className?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

function prettyBlock(value?: string): string {
  if (!value) return "";
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function toolIconClass(tool: ToolCallInfo): string {
  if (tool.status === "error") return "process-timeline__icon--error";
  if (tool.status === "running" || tool.status === "generating") return "process-timeline__icon--running";
  return "";
}

function ProcessIcon({ kind, className = "" }: { kind: "thinking" | "tool" | "status" | "error"; className?: string }) {
  if (kind === "thinking") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9 18h6" />
        <path d="M10 22h4" />
        <path d="M8.2 14.3a6 6 0 1 1 7.6 0c-.7.5-1.1 1.2-1.3 2H9.5c-.2-.8-.6-1.5-1.3-2Z" />
      </svg>
    );
  }
  if (kind === "error") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v6" />
        <path d="M12 17h.01" />
      </svg>
    );
  }
  if (kind === "status") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v4l3 2" />
      </svg>
    );
  }
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="4" y="5" width="16" height="14" rx="2" />
      <path d="m8 9 3 3-3 3" />
      <path d="M13 15h3" />
    </svg>
  );
}

function ToolTimelineItem({ tool }: { tool: ToolCallInfo }) {
  const [open, setOpen] = useState(false);
  const request = prettyBlock(tool.args);
  const response = prettyBlock(tool.result);
  const hasDetail = Boolean(request || response);

  return (
    <div className="process-timeline__item">
      <div className={`process-timeline__icon ${toolIconClass(tool)}`}>
        <ProcessIcon kind="tool" />
      </div>
      <div className="process-timeline__content">
        <button
          className="process-timeline__row"
          type="button"
          onClick={() => hasDetail && setOpen((value) => !value)}
        >
          <span className="process-timeline__title">{toolDisplayTitle(tool)}</span>
          {hasDetail && (
            <span className={`process-timeline__chevron ${open ? "process-timeline__chevron--open" : ""}`}>
              ›
            </span>
          )}
        </button>
        {open && hasDetail && (
          <div className="process-timeline__detail">
            {request && (
              <section className="process-code-block">
                <div className="process-code-block__title">Request</div>
                <pre>{request}</pre>
              </section>
            )}
            {response && (
              <section className="process-code-block">
                <div className="process-code-block__title">Response</div>
                <pre>{response}</pre>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ThinkingTimelineItem({
  item,
  isStreaming,
}: {
  item: Extract<ProcessTimelineItem, { kind: "thinking" }>;
  isStreaming?: boolean;
}) {
  const [open, setOpen] = useState(!item.done);
  const hasDetail = Boolean(item.content.trim());
  return (
    <div className="process-timeline__item">
      <div className="process-timeline__icon">
        <ProcessIcon kind="thinking" />
      </div>
      <div className="process-timeline__content">
        <button
          className="process-timeline__row"
          type="button"
          onClick={() => hasDetail && setOpen((value) => !value)}
        >
          <span className="process-timeline__title">{item.done ? "思考已完成" : "思考中"}</span>
          {hasDetail && (
            <span className={`process-timeline__chevron ${open ? "process-timeline__chevron--open" : ""}`}>
              ›
            </span>
          )}
        </button>
        {open && hasDetail && (
          <div className="process-timeline__thinking msg__text md-body">
            <MarkdownContent content={item.content} isStreaming={isStreaming} />
          </div>
        )}
      </div>
    </div>
  );
}

function NarrationTimelineItem({ content, isStreaming }: { content: string; isStreaming?: boolean }) {
  return (
    <div className="process-timeline__item process-timeline__item--narration">
      <div className="process-timeline__icon process-timeline__icon--ghost" aria-hidden="true" />
      <div className="process-timeline__narration msg__text md-body">
        <MarkdownContent content={content} isStreaming={isStreaming} />
      </div>
    </div>
  );
}

export default function AgentProcessTimeline({ items, isStreaming }: Props) {
  return (
    <div className="process-timeline">
      {items.map((item) => {
        if (item.kind === "tool") return <ToolTimelineItem key={item.id} tool={item.tool} />;
        if (item.kind === "thinking") {
          return <ThinkingTimelineItem key={item.id} item={item} isStreaming={isStreaming} />;
        }
        if (item.kind === "narration") {
          return <NarrationTimelineItem key={item.id} content={item.content} isStreaming={isStreaming} />;
        }
        return (
          <div key={item.id} className="process-timeline__item">
            <div className={`process-timeline__icon ${item.kind === "error" ? "process-timeline__icon--error" : ""}`}>
              <ProcessIcon kind={item.kind} />
            </div>
            <div className={`process-timeline__content ${item.kind === "error" ? "process-timeline__error" : ""}`}>
              {item.text}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function AgentProcessFold({
  items,
  label,
  isStreaming,
  className = "",
  open,
  onOpenChange,
}: FoldProps) {
  if (items.length === 0) return null;
  return (
    <details
      className={`msg__foldable${className ? ` ${className}` : ""}`}
      open={open}
    >
      <summary
        className="msg__fold-summary"
        onClick={(event) => {
          // onToggle also fires when React changes the controlled `open` prop.
          // Only a real summary activation should pin the user's preference.
          const details = event.currentTarget.parentElement as HTMLDetailsElement | null;
          if (details) onOpenChange?.(!details.open);
        }}
      >
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
        <AgentProcessTimeline items={items} isStreaming={isStreaming} />
      </div>
    </details>
  );
}
