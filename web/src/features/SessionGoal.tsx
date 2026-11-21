import { useEffect, useRef, useState } from "react";
import { api, ApiError, type SessionGoalAction, type SessionGoalSnapshot } from "../api";

const goalStatus = { active: "进行中", paused: "已暂停", completed: "已完成" };

function goalError(error: unknown): string {
  if (error instanceof ApiError) return error.body?.error || `网关返回 ${error.status}`;
  return "请检查网关连接与版本";
}

export function SessionGoalPanel({ sessionId, initialObjective, onClose }: {
  sessionId: string;
  initialObjective: string;
  onClose: () => void;
}) {
  const [snapshot, setSnapshot] = useState<SessionGoalSnapshot | null>(null);
  const [draft, setDraft] = useState(initialObjective);
  const [editing, setEditing] = useState(Boolean(initialObjective));
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const updating = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    const { signal } = controller;
    void api.sessionGoal(sessionId, signal).then((state) => {
      if (!signal.aborted) setSnapshot(state);
    }).catch((err: unknown) => {
      if (!signal.aborted) setError(`无法读取目标：${goalError(err)}`);
    }).finally(() => {
      if (!signal.aborted) setBusy(false);
    });
    return () => controller.abort();
  }, [sessionId]);

  const apply = async (action: SessionGoalAction) => {
    const signal = request.current?.signal;
    if (!snapshot || !signal || signal.aborted || updating.current) return;
    updating.current = true;
    setBusy(true);
    setError("");
    try {
      const latest = await api.changeSessionGoal(sessionId, {
        action, version: snapshot.version, ...(action === "set" ? { objective: draft.trim() } : {}),
      }, signal);
      if (!signal.aborted) {
        setSnapshot(latest);
        if (action === "set") setEditing(false);
      }
    } catch (err) {
      if (signal.aborted) return;
      if (err instanceof ApiError && err.status === 409) {
        setError("目标已在其他窗口更新。请核对当前状态后再提交。");
        setSnapshot(null);
        const latest = await api.sessionGoal(sessionId, signal).catch(() => null);
        if (!signal.aborted) setSnapshot(latest);
      } else {
        setError(`未能更新目标：${goalError(err)}`);
      }
    } finally {
      updating.current = false;
      if (!signal.aborted) setBusy(false);
    }
  };

  const goal = snapshot?.goal;
  const showEditor = editing || snapshot?.goal === null;
  return (
    <section className="session-goal" aria-label="会话目标" aria-busy={busy}>
      <div className="session-goal__header">
        <strong>会话目标{goal ? ` · ${goalStatus[goal.status]}` : ""}</strong>
        <button type="button" className="toolbar-btn" onClick={onClose}>关闭目标面板</button>
      </div>
      <small>目标随相关消息提供给模型，不自动提交或继续运行。</small>
      {busy && !snapshot && <p role="status">正在读取目标…</p>}
      {error && <p role="alert">{error}</p>}
      {goal && !showEditor && <p className="session-goal__objective">{goal.objective}</p>}
      {showEditor && (
        <form onSubmit={(event) => { event.preventDefault(); if (draft.trim()) void apply("set"); }}>
          <textarea aria-label="目标内容" value={draft} onChange={(event) => setDraft(event.target.value)}
            maxLength={4000} rows={2} placeholder="描述想达成的结果" />
          <button type="submit" className="toolbar-btn" disabled={busy || !snapshot || !draft.trim()}>保存目标</button>
        </form>
      )}
      {goal && !showEditor && (
        <div className="session-goal__actions">
          {goal.status === "active" && <button type="button" className="toolbar-btn" disabled={busy}
            onClick={() => void apply("pause")}>暂停目标</button>}
          {goal.status === "paused" && <button type="button" className="toolbar-btn" disabled={busy}
            onClick={() => void apply("resume")}>恢复目标</button>}
          {goal.status !== "completed" && <button type="button" className="toolbar-btn" disabled={busy}
            onClick={() => void apply("complete")}>标记目标完成</button>}
          <button type="button" className="toolbar-btn" disabled={busy} onClick={() => {
            setDraft(goal.objective);
            setEditing(true);
          }}>设置新目标</button>
        </div>
      )}
    </section>
  );
}
