import { Fragment, useEffect, useMemo, useState } from "react";
import type { SubagentListResponse, UiMessage } from "../types";
import { api } from "../api";
import {
  mergeSubagentActivities,
  subagentStatusLabel,
  type SubagentActivity,
} from "../lib/subagentActivity";

const REFRESH_INTERVAL_MS = 2000;
const MAX_VISIBLE_ACTIVITIES = 6;

interface Props {
  sessionId?: string;
  messages: UiMessage[];
  busy: boolean;
}

export interface SubagentActivityPanelProps {
  activities: SubagentActivity[];
  expandedId?: string | null;
  draft?: string;
  actionId?: string | null;
  actionError?: string;
  onToggleMessage?: (id: string) => void;
  onDraftChange?: (value: string) => void;
  onSendMessage?: (id: string) => void;
  onInterrupt?: (id: string) => void;
}

export function SubagentActivityPanel({
  activities,
  expandedId = null,
  draft = "",
  actionId = null,
  actionError = "",
  onToggleMessage,
  onDraftChange,
  onSendMessage,
  onInterrupt,
}: SubagentActivityPanelProps) {
  if (activities.length === 0) return null;

  const visible = activities.slice(0, MAX_VISIBLE_ACTIVITIES);
  const running = activities.filter((activity) => activity.status === "running").length;
  const summary = running > 0 ? `${running} 个运行中` : `${activities.length} 个记录`;

  return (
    <section className="subagent-activity" aria-label="子智能体活动">
      <div className="subagent-activity__header">
        <span className="subagent-activity__icon" aria-hidden="true">✦</span>
        <strong>子智能体活动</strong>
        <span className="subagent-activity__summary">{summary}</span>
      </div>
      <div className="subagent-activity__list" role="list">
        {visible.map((activity) => (
          <Fragment key={activity.id}>
            <div
              className={`subagent-activity__row subagent-activity__row--${activity.status}`}
              role="listitem"
              aria-label={`${activity.label}，${subagentStatusLabel(activity.status)}`}
            >
              <span className="subagent-activity__dot" aria-hidden="true" />
              <span className="subagent-activity__label" title={activity.label}>{activity.label}</span>
              {(activity.provider || activity.lifecycle) && (
                <span className="subagent-activity__meta" title={activity.provider || activity.lifecycle}>
                  {activity.provider || activity.lifecycle}
                </span>
              )}
              <span className="subagent-activity__status">{subagentStatusLabel(activity.status)}</span>
              {onToggleMessage && ["running", "ready", "idle"].includes(activity.status) && (
                <button
                  type="button"
                  className="subagent-activity__action"
                  onClick={() => onToggleMessage(activity.id)}
                  disabled={actionId === activity.id}
                >
                  发送
                </button>
              )}
              {onInterrupt && activity.status === "running" && (
                <button
                  type="button"
                  className="subagent-activity__action subagent-activity__action--danger"
                  onClick={() => onInterrupt(activity.id)}
                  disabled={actionId === activity.id}
                >
                  中断
                </button>
              )}
            </div>
            {expandedId === activity.id && onDraftChange && onSendMessage && (
              <form
                className="subagent-activity__composer"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (draft.trim()) onSendMessage(activity.id);
                }}
              >
                <input
                  value={draft}
                  onChange={(event) => onDraftChange(event.target.value)}
                  placeholder="给子智能体发送消息…"
                  aria-label={`发送消息给${activity.label}`}
                  disabled={actionId === activity.id}
                />
                <button type="submit" disabled={!draft.trim() || actionId === activity.id}>
                  {actionId === activity.id ? "发送中…" : "发送"}
                </button>
              </form>
            )}
          </Fragment>
        ))}
      </div>
      {actionError && <div className="subagent-activity__error" role="alert">{actionError}</div>}
      {activities.length > visible.length && (
        <div className="subagent-activity__more">还有 {activities.length - visible.length} 个记录</div>
      )}
    </section>
  );
}

export default function SubagentActivityView({ sessionId, messages, busy }: Props) {
  const [response, setResponse] = useState<SubagentListResponse | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [actionId, setActionId] = useState<string | null>(null);
  const [actionError, setActionError] = useState("");
  const [refreshNonce, setRefreshNonce] = useState(0);
  const lifecycleKey = messages
    .filter((message) => message.subagentId)
    .map((message) => `${message.subagentId}:${message.subagentState}:${message.subagentStopReason || ""}`)
    .join("|");

  useEffect(() => {
    if (!sessionId) {
      setResponse(null);
      return;
    }
    let cancelled = false;

    const refresh = async () => {
      try {
        const next = await api.subagents(sessionId);
        if (!cancelled) setResponse(next);
      } catch {
        if (!cancelled) setResponse(null);
      }
    };

    void refresh();
    const timer = window.setInterval(() => void refresh(), busy ? REFRESH_INTERVAL_MS : 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [sessionId, busy, lifecycleKey, refreshNonce]);

  const activities = useMemo(
    () => mergeSubagentActivities(response, messages),
    [response, messages],
  );

  const toggleMessage = (id: string) => {
    setActionError("");
    setExpandedId((current) => current === id ? null : id);
  };

  const sendMessage = async (id: string) => {
    if (!sessionId || !draft.trim()) return;
    setActionId(id);
    setActionError("");
    try {
      await api.sendSubagentMessage(id, sessionId, draft.trim(), true);
      setDraft("");
      setExpandedId(null);
      setRefreshNonce((value) => value + 1);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "发送消息失败");
    } finally {
      setActionId(null);
    }
  };

  const interrupt = async (id: string) => {
    if (!sessionId) return;
    setActionId(id);
    setActionError("");
    try {
      await api.interruptSubagent(id, sessionId);
      setRefreshNonce((value) => value + 1);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "中断子智能体失败");
    } finally {
      setActionId(null);
    }
  };

  return (
    <SubagentActivityPanel
      activities={activities}
      expandedId={expandedId}
      draft={draft}
      actionId={actionId}
      actionError={actionError}
      onToggleMessage={sessionId ? toggleMessage : undefined}
      onDraftChange={setDraft}
      onSendMessage={sessionId ? sendMessage : undefined}
      onInterrupt={sessionId ? interrupt : undefined}
    />
  );
}
