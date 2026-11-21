import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type SessionGoalAction, type SessionGoalRun, type SessionGoalSnapshot } from "../api";

const goalStatus = { active: "已启用", paused: "已暂停", completed: "已完成" };
const runLabels: Readonly<Record<string, string>> = {
  accepted: "本轮已接收",
  running: "正在推进本轮",
  stopping: "正在停止本轮",
  finished: "本轮已结束",
  stopped: "本轮已停止",
  interrupted: "本轮已中断",
  failed: "本轮推进失败",
};
const runDetails: Readonly<Record<string, string>> = {
  gateway_restarted: "网关重启后未重放本轮。",
  goal_deadline: "本轮达到运行时限。",
  goal_session_busy: "已有普通对话或后台任务。",
  goal_plan_active: "会话正在使用计划模式。",
  paused_before_dispatch: "目标在执行前已暂停。",
  goal_identity_lost: "原会话或网关已变化。",
};
const verificationLabels: Readonly<Record<string, string>> = {
  running: "正在验收目标",
  passed: "目标验收通过",
  incomplete: "证据不足，目标未完成",
  unconfirmed: "未能确认目标达成",
  interrupted: "本次目标验收已中断",
};
const verificationDetails: Readonly<Record<string, string>> = {
  gateway_restarted: "网关重启后未重试本次验收。",
  goal_verification_timeout: "验收模型未在时限内返回。",
  goal_evidence_changed: "验收时的会话证据已变化。",
  goal_verification_no_evidence: "没有找到本轮可供验收的已保存回复。",
  goal_verification_bad_citations: "无法核实验收模型给出的引文。",
  goal_plan_active: "会话正在使用计划模式。",
};
const POLL_MS = 2_000;
const POLL_LIMIT = 90;

const isLive = (run?: SessionGoalRun) => run?.state === "accepted" || run?.state === "running" || run?.state === "stopping";
const isUnsettled = (run?: SessionGoalRun) => run != null && !["finished", "stopped", "interrupted", "failed"].includes(run.state);
const knownText = (values: Readonly<Record<string, string>>, key: string) => Object.prototype.hasOwnProperty.call(values, key) ? values[key] : "";

function currentVerification(snapshot: SessionGoalSnapshot | null) {
  const verification = snapshot?.verification;
  const run = snapshot?.run;
  return verification && run?.state === "finished" && run.request_id === verification.step_request_id ? verification : undefined;
}

function observedRequest(snapshot: SessionGoalSnapshot | null) {
  const verification = currentVerification(snapshot);
  if (verification?.state === "running") return `verify:${verification.request_id}`;
  const run = snapshot?.run;
  return run && isLive(run) ? `step:${run.request_id}` : undefined;
}

function goalError(error: unknown): string {
  if (error instanceof ApiError) return error.body?.error || `网关返回 ${error.status}`;
  return "请检查网关连接与版本";
}

type Props = { sessionId: string; initialObjective: string; onClose: () => void };

export function SessionGoalPanel(props: Props) {
  return <ScopedGoalPanel key={props.sessionId} {...props} />;
}

function ScopedGoalPanel({ sessionId, initialObjective, onClose }: Props) {
  const [snapshot, setSnapshot] = useState<SessionGoalSnapshot | null>(null);
  const [draft, setDraft] = useState(initialObjective);
  const [editing, setEditing] = useState(Boolean(initialObjective));
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const scope = useRef<AbortController | null>(null);
  const observer = useRef<AbortController | null>(null);
  const locked = useRef(true);
  const run = snapshot?.run;
  const goal = snapshot?.goal;
  const verification = currentVerification(snapshot);
  const observedId = observedRequest(snapshot);
  const operationBusy = isUnsettled(run) || verification?.state === "running";
  const canVerify = snapshot?.supports_verify === true && goal?.status === "active" && run?.state === "finished";

  const read = useCallback(async (signal: AbortSignal, notice = "") => {
    try {
      const latest = await api.sessionGoal(sessionId, signal);
      if (!signal.aborted) {
        setSnapshot(latest);
        setNeedsRefresh(false);
        setError(notice);
      }
    } catch (err) {
      if (!signal.aborted) {
        setNeedsRefresh(true);
        setError(`${notice ? `${notice} ` : ""}无法读取目标：${goalError(err)}`);
      }
    }
  }, [sessionId]);

  const unlock = (signal: AbortSignal) => {
    if (signal.aborted) return;
    locked.current = false;
    setBusy(false);
  };

  useEffect(() => {
    const controller = new AbortController();
    scope.current = controller;
    locked.current = true;
    setBusy(true);
    void read(controller.signal).finally(() => unlock(controller.signal));
    return () => controller.abort();
  }, [read]);

  useEffect(() => {
    if (!observedId || busy || needsRefresh) return;
    const controller = new AbortController();
    observer.current = controller;
    const { signal } = controller;
    let attempts = 0;
    let timer: number;
    const tick = async () => {
      if (signal.aborted) return;
      try {
        const latest = await api.sessionGoal(sessionId, signal);
        if (signal.aborted) return;
        setSnapshot(latest);
        if (observedRequest(latest) !== observedId) return;
        if (++attempts < POLL_LIMIT) timer = window.setTimeout(() => void tick(), POLL_MS);
        else {
          setNeedsRefresh(true);
          setError("尚未确认目标操作结束，请重新读取目标。");
        }
      } catch (err) {
        if (!signal.aborted) {
          setNeedsRefresh(true);
          setError(`无法确认目标操作状态：${goalError(err)}。请重新读取目标。`);
        }
      }
    };
    timer = window.setTimeout(() => void tick(), POLL_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [observedId, busy, needsRefresh, sessionId]);

  const startRequest = () => {
    const signal = scope.current?.signal;
    if (!signal || signal.aborted || locked.current) return;
    // Fence a read already in flight before a versioned write can resolve.
    observer.current?.abort();
    locked.current = true;
    setBusy(true);
    setError("");
    return signal;
  };

  const apply = async (action: SessionGoalAction) => {
    if (!snapshot || needsRefresh || (operationBusy && action !== "pause") || (action === "verify" && !canVerify)) return;
    const signal = startRequest();
    if (!signal) return;
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
        const reason = err.body?.code;
        const notice = !reason || reason === "goal_version_conflict"
          ? "目标或会话已更新，请核对当前状态后再提交。"
          : `未能更新目标：${goalError(err)}`;
        await read(signal, notice);
      } else {
        setNeedsRefresh(true);
        setError(`无法确认目标更新：${goalError(err)}。请重新读取目标，不会自动重试。`);
      }
    } finally {
      unlock(signal);
    }
  };

  const showEditor = editing || snapshot?.goal === null;
  const disabled = busy || needsRefresh;
  const canAdvance = snapshot?.supports_advance ?? Boolean(run);
  const reason = verification ? knownText(verificationDetails, verification.code) : "";
  const tooLong = Array.from(draft.trim()).length > 4000;
  return (
    <section className="session-goal" aria-label="会话目标" aria-busy={busy}>
      <div className="session-goal__header">
        <strong>会话目标{goal ? ` · ${goalStatus[goal.status]}` : ""}</strong>
        <div className="session-goal__actions">
          <button type="button" className="toolbar-btn" disabled={busy} onClick={() => {
            const signal = startRequest();
            if (signal) void read(signal).finally(() => unlock(signal));
          }}>重新读取目标</button>
          <button type="button" className="toolbar-btn" onClick={onClose}>关闭目标面板</button>
        </div>
      </div>
      <small>{canAdvance ? "每次只推进一轮，不调用工具。本轮结束不代表目标达成。"
        : "目标随相关消息提供给模型，保存和恢复不自动提交回合。"}
        {snapshot?.supports_verify === true && "验收只核对已保存的会话证据，不检查外部实际状态。"}
        {canAdvance && "关闭面板只停止观察。"}</small>
      {busy && !snapshot && <p role="status">正在读取目标…</p>}
      {error && <p role="alert">{error}</p>}
      {run && <p role="status" data-goal-run={run.request_id}>
        {needsRefresh && "上次确认："}{knownText(runLabels, run.state) || "本轮状态待确认"}
        {knownText(runDetails, run.code) && ` · ${knownText(runDetails, run.code)}`}
      </p>}
      {verification && <p role="status" data-goal-verification={verification.request_id}>
        {needsRefresh && "上次确认："}{knownText(verificationLabels, verification.state) || "验收状态待确认"}
        {reason && ` · ${reason}`}
      </p>}
      {goal && <p className="session-goal__objective">{goal.objective}</p>}
      {showEditor && (
        <form onSubmit={(event) => { event.preventDefault(); if (draft.trim() && !tooLong) void apply("set"); }}>
          <textarea aria-label="目标内容" aria-invalid={tooLong} value={draft} readOnly={busy}
            onChange={(event) => setDraft(event.target.value)} maxLength={8000} rows={2} placeholder="描述想达成的结果" />
          {tooLong && <small role="alert">目标最多 4000 个字符。</small>}
          <div className="session-goal__actions">
            <button type="submit" className="toolbar-btn" disabled={disabled || operationBusy || !snapshot || !draft.trim() || tooLong}>保存目标</button>
            {goal && <button type="button" className="toolbar-btn" disabled={busy} onClick={() => setEditing(false)}>取消编辑</button>}
          </div>
        </form>
      )}
      {goal && (
        <div className="session-goal__actions">
          {goal.status === "active" && canAdvance && <button type="button" className="toolbar-btn" disabled={disabled || operationBusy || showEditor}
            onClick={() => void apply("advance")}>推进一轮</button>}
          {canVerify && <button type="button" className="toolbar-btn" disabled={disabled || operationBusy || showEditor}
            onClick={() => void apply("verify")}>验收目标</button>}
          {goal.status === "active" && (!showEditor || operationBusy) && <button type="button" className="toolbar-btn" disabled={disabled}
            onClick={() => void apply("pause")}>暂停目标</button>}
          {goal.status === "paused" && !showEditor && <button type="button" className="toolbar-btn" disabled={disabled || operationBusy}
            onClick={() => void apply("resume")}>恢复目标</button>}
          {goal.status !== "completed" && !showEditor && <button type="button" className="toolbar-btn" disabled={disabled || operationBusy}
            onClick={() => void apply("complete")}>标记目标完成</button>}
          {!showEditor && <button type="button" className="toolbar-btn" disabled={disabled || operationBusy} onClick={() => {
            setDraft(goal.objective);
            setEditing(true);
          }}>设置新目标</button>}
        </div>
      )}
    </section>
  );
}
