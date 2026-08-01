import { useState } from "react";

interface Props {
  text: string;
  canEdit?: boolean;
  onEdit?: () => void;
}

export default function MessageActions({ text, canEdit = false, onEdit }: Props) {
  const [copied, setCopied] = useState(false);
  if (!text && !canEdit) return null;

  async function copy() {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="msg__action-stack" aria-label="消息操作">
      {text && (
        <button
          type="button"
          className={`msg__action-btn msg__copy-btn ${copied ? "is-copied" : ""}`}
          onClick={copy}
          title={copied ? "已复制" : "复制"}
          aria-label={copied ? "已复制" : "复制消息"}
        >
          {copied ? (
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M20 6 9 17l-5-5" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="8" y="8" width="11" height="11" rx="2" />
              <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
            </svg>
          )}
        </button>
      )}
      {canEdit && (
        <button
          type="button"
          className="msg__action-btn msg__edit-btn"
          onClick={onEdit}
          title="修改后重新发送"
          aria-label="修改后重新发送"
        >
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
        </button>
      )}
    </div>
  );
}
