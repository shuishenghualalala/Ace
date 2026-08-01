import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { DebugEvent } from "../types";

interface Props {
  sessionId: string;
  title: string;
  onClose: () => void;
}

function formatTime(ts: number): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

function eventTitle(ev: DebugEvent): string {
  if (ev.dir === "request") return `LLM 输入 · ${ev.model ?? ""}`;
  if (ev.dir === "response") return `LLM 输出 · ${ev.model ?? ""}`;
  if (ev.dir === "error") return `LLM 错误 · ${ev.model ?? ""}`;
  if (ev.dir === "user") return "用户输入";
  if (ev.dir === "tool_start") return `工具开始 · ${ev.name ?? ""}`;
  if (ev.dir === "tool_result") return `工具结果 · ${ev.name ?? ""}`;
  return ev.dir;
}

function preview(ev: DebugEvent): string {
  const text = String(ev.text ?? ev.content ?? ev.error ?? "");
  if (text) return text.slice(0, 180);
  if (ev.messages) return `${ev.messages.length} 条 messages`;
  if (ev.tool_calls) return `${ev.tool_calls.length} 个 tool_calls`;
  return "";
}

export default function DebugPanel({ sessionId, title, onClose }: Props) {
  const [events, setEvents] = useState<DebugEvent[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.debugLog(sessionId, 200);
      setEnabled(data.enabled);
      setEvents(data.events);
    } catch {
      setEnabled(false);
      setEvents([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [sessionId]);

  const grouped = useMemo(() => events.slice().sort((a, b) => a.ts - b.ts), [events]);

  return (
    <div className="debug-panel">
      <div className="debug-panel__head">
        <div>
          <div className="debug-panel__title">调试日志</div>
          <div className="debug-panel__sub">{title}</div>
        </div>
        <div className="debug-panel__actions">
          <button className="icon-btn" title="刷新" onClick={load} type="button">↻</button>
          <button className="icon-btn" title="关闭" onClick={onClose} type="button">×</button>
        </div>
      </div>
      <div className="debug-panel__body">
        {!enabled && <div className="debug-panel__empty">runtime.llm_trace 未开启，暂无调试日志。</div>}
        {enabled && loading && <div className="debug-panel__empty">加载中...</div>}
        {enabled && !loading && grouped.length === 0 && (
          <div className="debug-panel__empty">当前会话还没有调试事件。</div>
        )}
        {enabled && !loading && grouped.map((ev, index) => (
          <details className="debug-event" key={`${ev.ts}_${ev.dir}_${index}`}>
            <summary>
              <span className="debug-event__time">{formatTime(ev.ts)}</span>
              <span className="debug-event__title">{eventTitle(ev)}</span>
              <span className="debug-event__preview">{preview(ev)}</span>
            </summary>
            <pre>{JSON.stringify(ev, null, 2)}</pre>
          </details>
        ))}
      </div>
    </div>
  );
}
