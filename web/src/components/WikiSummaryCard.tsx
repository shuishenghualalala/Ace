import type { WikiSummary } from "../types";

interface Props {
  summary: WikiSummary;
  onAsk?: (text: string) => void;
}

const SUGGESTIONS = [
  "核心观点是什么？",
  "有哪些关键人物或实体？",
  "主要概念有哪些？",
];

export default function WikiSummaryCard({ summary, onAsk }: Props) {
  return (
    <div className="wiki-summary-card">
      <div className="wiki-summary-card__header">
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
        </svg>
        <span>知识库概览</span>
        {summary.status === "generating" && (
          <span className="wiki-summary-card__badge">生成中</span>
        )}
      </div>
      <div className="wiki-summary-card__body">{summary.summary}</div>
      {summary.status === "ready" && (
        <div className="wiki-summary-card__suggestions">
          {SUGGESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              className="wiki-summary-card__chip"
              onClick={() => onAsk?.(q)}
            >
              {q}
            </button>
          ))}
        </div>
      )}
      {(summary.page_count != null || summary.source_count != null) && (
        <div className="wiki-summary-card__meta">
          {summary.page_count != null && (
            <span>{summary.page_count} 个页面</span>
          )}
          {summary.source_count != null && (
            <span>{summary.source_count} 个来源</span>
          )}
        </div>
      )}
    </div>
  );
}
