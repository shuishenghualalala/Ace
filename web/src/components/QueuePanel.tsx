import { useState } from "react";
import type { PendingMessage } from "../types";

interface Props {
  queue: PendingMessage[];
  queueHint?: string;
  busy?: boolean;
  onRemove: (index: number) => void;
  onEdit: (index: number, newQuery: string) => void;
  onSendNow?: (id: string) => void;
}

export default function QueuePanel({ queue, queueHint, busy = false, onRemove, onEdit, onSendNow }: Props) {
  const [expanded, setExpanded] = useState(true);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editValue, setEditValue] = useState("");

  if (queue.length === 0) return null;

  const startEdit = (index: number, text: string) => {
    setEditingIndex(index);
    setEditValue(text);
  };

  const cancelEdit = () => {
    setEditingIndex(null);
    setEditValue("");
  };

  const saveEdit = (index: number) => {
    const val = editValue.trim();
    if (val) onEdit(index, val);
    setEditingIndex(null);
    setEditValue("");
  };

  return (
    <div className="queue-panel">
      <button
        className="queue-panel__header"
        onClick={() => setExpanded((v) => !v)}
        type="button"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={"queue-panel__caret" + (expanded ? " queue-panel__caret--open" : "")}
        >
          <path d="m9 18 6-6-6-6" />
        </svg>
        <span className="queue-panel__title">
          队列 ({queue.length})
        </span>
        {queueHint && (
          <span className="queue-panel__hint">{queueHint}</span>
        )}
      </button>

      {expanded && (
        <div className="queue-panel__body">
          {queue.map((item, index) => (
            <div key={item.id} className="queue-panel__item">
              {editingIndex === index ? (
                <input
                  className="queue-panel__input"
                  value={editValue}
                  autoFocus
                  onChange={(e) => setEditValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      saveEdit(index);
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      cancelEdit();
                    }
                  }}
                  onBlur={() => saveEdit(index)}
                />
              ) : (
                <span className="queue-panel__text" title={item.query}>
                  {item.query}
                </span>
              )}
              <div className="queue-panel__actions">
                <button
                  className="queue-panel__act"
                  title="立即发送"
                  disabled={busy || editingIndex === index}
                  onClick={() => onSendNow?.(item.id)}
                  type="button"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 19V5"/>
                    <path d="m5 12 7-7 7 7"/>
                  </svg>
                </button>
                <button
                  className="queue-panel__act"
                  title="编辑"
                  onClick={() => startEdit(index, item.query)}
                  type="button"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                  </svg>
                </button>
                <button
                  className="queue-panel__act"
                  title="删除"
                  onClick={() => onRemove(index)}
                  type="button"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 6h18"/>
                    <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                    <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
