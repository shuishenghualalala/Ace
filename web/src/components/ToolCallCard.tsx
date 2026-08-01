import { useState } from "react";
import type { ToolCallInfo } from "../types";
import { formatToolResultDisplay } from "../utils/toolResult";
import { toolDisplayTitle } from "../lib/processDisplay";

interface Props {
  tool: ToolCallInfo;
}

function RunningIcon() {
  return (
    <svg
      className="tool-card__spinner"
      viewBox="0 0 24 24"
      width="12"
      height="12"
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="9"
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        strokeDasharray="40 20"
        strokeLinecap="round"
      />
    </svg>
  );
}

export default function ToolCallCard({ tool }: Props) {
  const [open, setOpen] = useState(false);

  const isActive = tool.status === "running" || tool.status === "generating";

  const statusClass =
    isActive
      ? "tool-card__icon--running"
      : tool.status === "error"
        ? "tool-card__icon--error"
        : "tool-card__icon--done";

  const statusIcon = isActive ? <RunningIcon /> : tool.status === "error" ? "✗" : "✓";

  const hasDetail = Boolean(tool.args || tool.result);

  return (
    <div className={`tool-card ${open ? "tool-card--open" : ""}`}>
      <button
        className="tool-card__header"
        onClick={() => hasDetail && setOpen(!open)}
        type="button"
      >
        <span className={`tool-card__icon ${statusClass}`}>{statusIcon}</span>
        <span className="tool-card__name">{toolDisplayTitle(tool)}</span>
        {tool.duration != null && (
          <span className="tool-card__duration">{tool.duration}ms</span>
        )}
        {hasDetail && (
          <span className={`tool-card__caret ${open ? "tool-card__caret--open" : ""}`}>
            ›
          </span>
        )}
      </button>
      {open && (
        <div className="tool-card__body">
          {tool.args && (
            <div className="tool-card__section">
              <div className="tool-card__label">参数</div>
              <pre className="tool-card__pre">{tool.args}</pre>
            </div>
          )}
          {tool.result && (
            <div className="tool-card__section">
              <div className="tool-card__label">结果</div>
              <pre className="tool-card__pre">{formatToolResultDisplay(tool.name, tool.result)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
