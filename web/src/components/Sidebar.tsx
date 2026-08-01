import { useState } from "react";
import type { Session, Workspace } from "../types";
import type { SessionStatus } from "../hooks/useChat";

export type SidebarView = "chat" | "agents" | "skills" | "wiki";

const STATUS_LABEL: Record<SessionStatus, string> = {
  idle: "空闲",
  running: "运行中",
  queued: "排队中",
  error: "出错",
};

function formatTimeAgo(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 0) return "刚刚";
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}天前`;
  return `${Math.floor(diff / 604800)}周前`;
}

function sessionAgentName(session: Session): string {
  return session.agent_label?.name?.trim() || "Crew";
}

function sessionAgentProvider(session: Session): string {
  return session.agent_label?.provider?.trim().toLowerCase() || "crew";
}

function sessionRuntimeInitial(session: Session): string {
  return session.agent_label?.display_badge?.trim() || "?";
}

function sessionRuntimeClass(session: Session): string {
  const provider = sessionAgentProvider(session);
  if (provider === "team") return "team";
  return provider === "crew" ? "crew" : "external";
}

function sessionFullLabel(session: Session): string {
  const title = session.title || "新会话";
  if (sessionAgentProvider(session) === "team") return `team-${sessionAgentName(session)}-${title}`;
  if (sessionAgentProvider(session) === "crew") return title;
  return `${sessionAgentProvider(session)}-${sessionAgentName(session)}-${title}`;
}

interface Props {
  workspaces: Workspace[];
  sessions: Session[];
  currentSessionId: string;
  sessionStatus: Record<string, SessionStatus>;
  expanded: Set<string>;
  view: SidebarView;
  externalAgentsEnabled: boolean;
  onViewChange: (v: SidebarView) => void;
  onToggleExpand: (wsId: string) => void;
  onNewWorkspace: () => void;
  onEditWorkspace: (ws: Workspace) => void;
  onDeleteWorkspace: (wsId: string) => void;
  onNewSession: (wsId: string) => void;
  onSelectSession: (s: Session) => void;
  onRenameSession: (id: string, title: string) => void;
  onDeleteSession: (id: string) => void;
}

export default function Sidebar(props: Props) {
  const {
    workspaces,
    sessions,
    currentSessionId,
    sessionStatus,
    expanded,
    view,
    externalAgentsEnabled,
    onViewChange,
    onToggleExpand,
    onNewWorkspace,
    onEditWorkspace,
    onDeleteWorkspace,
    onNewSession,
    onSelectSession,
    onRenameSession,
    onDeleteSession,
  } = props;
  const [q, setQ] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");

  const sessionsOf = (wsId: string) =>
    sessions.filter((s) => s.workspace_id === wsId && (!q || s.title.includes(q)));

  return (
    <aside className="sidebar">
      {/* Logo */}
      <div className="sidebar__brand">
        <div className="sidebar__logo">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2L2 7l10 5 10-5-10-5z"/>
            <path d="M2 17l10 5 10-5"/>
            <path d="M2 12l10 5 10-5"/>
          </svg>
        </div>
        <div className="sidebar__name">Crew</div>
        <div className="sidebar__ver">v0.1.0</div>
      </div>

      {/* Search */}
      <div className="sidebar__search-wrap">
        <div className="sidebar__search">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8"/>
            <path d="m21 21-4.35-4.35"/>
          </svg>
          <input
            placeholder="搜索任务"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <button className="sidebar__search-btn" title="筛选">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="21" y1="10" x2="3" y2="10"/>
            <line x1="21" y1="6" x2="3" y2="6"/>
            <line x1="21" y1="14" x2="3" y2="14"/>
            <line x1="21" y1="18" x2="3" y2="18"/>
          </svg>
        </button>
      </div>

      {/* New Task Button */}
      <div className="sidebar__pad">
        <button className="btn-new" onClick={() => onNewSession("default")}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/>
            <line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          新建任务
        </button>
      </div>

      {/* Nav Menu */}
      <nav className="sidebar__nav">
        <div
          className={"nav-item" + (view === "skills" ? " active" : "")}
          onClick={() => onViewChange("skills")}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>
          </svg>
          <span>技能</span>
        </div>
        {externalAgentsEnabled && (
          <div
            className={"nav-item" + (view === "agents" ? " active" : "")}
            onClick={() => onViewChange("agents")}
          >
            <svg className="nav-agent-logo sidebar-agent-logo" width="16" height="16" viewBox="3 3 18 18" aria-hidden="true">
              <path className="nav-agent-logo__blob" d="M5.2 13.2c0-4.5 2.9-6.9 6.8-6.9 4.5 0 7 2.8 7 6.2 0 3.8-2.5 5.5-7.2 5.5-4.3 0-6.6-1.4-6.6-4.8Z"/>
              <path className="nav-agent-logo__cap" d="M9 6.7c.7-1.1 1.7-1.7 3.1-1.7 1.3 0 2.3.5 3 1.5"/>
              <path className="nav-agent-logo__shine nav-agent-logo__shine--left" d="M9.6 10.8v1.9"/>
              <path className="nav-agent-logo__shine nav-agent-logo__shine--right" d="M14.4 10.8v1.9"/>
              <path className="nav-agent-logo__pixel" d="M18.8 8.2h1.5M19.55 7.45v1.5"/>
            </svg>
            <span>外援</span>
          </div>
        )}
        <div
          className={"nav-item" + (view === "wiki" ? " active" : "")}
          onClick={() => onViewChange("wiki")}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
          </svg>
          <span>Wiki</span>
        </div>
        <div className="nav-item nav-item--disabled">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <polyline points="12 6 12 12 16 14"/>
          </svg>
          <span>自动化</span>
        </div>
      </nav>

      {/* Section Title */}
      <div className="sidebar__section-header">
        <span className="sidebar__section-sub">工作空间</span>
        <button className="sidebar__section-btn" title="新建工作空间" onClick={onNewWorkspace}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/>
            <line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        </button>
      </div>

      {/* Workspace List */}
      <div className="ws-list">
        {workspaces.map((ws) => {
          const open = expanded.has(ws.id);
          const items = sessionsOf(ws.id);
          return (
            <div className="ws" key={ws.id}>
              <div className="ws__head" onClick={() => onToggleExpand(ws.id)}>
                <span className="ws__caret">{open ? (
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                ) : (
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
                )}</span>
                <span className="ws__name" title={ws.description || ws.name}>
                  {ws.name}
                </span>
                <div className="ws__actions">
                  <button
                    className="ws__act"
                    title="新建会话"
                    onClick={(e) => {
                      e.stopPropagation();
                      onNewSession(ws.id);
                    }}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="12" y1="5" x2="12" y2="19"/>
                      <line x1="5" y1="12" x2="19" y2="12"/>
                    </svg>
                  </button>
                  <button
                    className="ws__act"
                    title="编辑空间"
                    onClick={(e) => {
                      e.stopPropagation();
                      onEditWorkspace(ws);
                    }}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                    </svg>
                  </button>
                  {ws.id !== "default" && (
                    <button
                      className="ws__act"
                      title="删除空间"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteWorkspace(ws.id);
                      }}
                    >
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M18 6 6 18"/>
                        <path d="m6 6 12 12"/>
                      </svg>
                    </button>
                  )}
                </div>
              </div>

              {open && (
                <div className="ws__sessions">
                  {items.length === 0 && <div className="ws__empty">暂无会话</div>}
                  {items.map((s) => {
                    const isEditing = editingId === s.session_id;
                    const timeLabel = formatTimeAgo(s.created_at && s.created_at > 0 ? s.created_at : s.updated_at);
                    const status: SessionStatus = sessionStatus[s.session_id] ?? "idle";
                    const fullLabel = sessionFullLabel(s);
                    const isCrewSession = sessionAgentProvider(s) === "crew";
                    const isTeamSession = sessionAgentProvider(s) === "team";
                    return (
                      <div
                        key={s.session_id}
                        className={"session" + (s.session_id === currentSessionId ? " active" : "")}
                        data-agent-label={isCrewSession ? undefined : fullLabel}
                        onClick={() => {
                          if (!isEditing) onSelectSession(s);
                        }}
                      >
                        <svg className="session__check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M20 6 9 17l-5-5"/>
                        </svg>
                        {isTeamSession ? (
                          <span
                            className="session__team-logo"
                            title={fullLabel}
                            aria-label={`团队会话：${fullLabel}`}
                          >
                            <i />
                            <i />
                          </span>
                        ) : !isCrewSession && (
                          <span
                            className={`session__agent-badge session__agent-badge--${sessionRuntimeClass(s)}`}
                            title={fullLabel}
                            aria-label={`当前外援：${fullLabel}`}
                          >
                            {sessionRuntimeInitial(s)}
                          </span>
                        )}
                        {isEditing ? (
                          <input
                            className="session__input"
                            autoFocus
                            value={editingTitle}
                            onChange={(e) => setEditingTitle(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                const t = editingTitle.trim();
                                if (t && t !== s.title) onRenameSession(s.session_id, t);
                                setEditingId(null);
                              } else if (e.key === "Escape") {
                                setEditingId(null);
                              }
                            }}
                            onBlur={() => {
                              const t = editingTitle.trim();
                              if (t && t !== s.title) onRenameSession(s.session_id, t);
                              setEditingId(null);
                            }}
                            onClick={(e) => e.stopPropagation()}
                          />
                        ) : (
                          <span className="session__title" title={fullLabel}>
                            <span className="session__title-text">{s.title || "新会话"}</span>
                          </span>
                        )}
                        <span className="session__time" title={s.created_at ? new Date(s.created_at * 1000).toLocaleString() : undefined}>{timeLabel}</span>
                        <span className={`session__dot session__dot--${status}`} title={STATUS_LABEL[status]} />
                        {!isEditing && (
                          <button
                            className="session__rename"
                            title="重命名"
                            onClick={(e) => {
                              e.stopPropagation();
                              setEditingId(s.session_id);
                              setEditingTitle(s.title || "新会话");
                            }}
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                              <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                            </svg>
                          </button>
                        )}
                        <button
                          className="session__del"
                          title="删除会话"
                          onClick={(e) => {
                            e.stopPropagation();
                            onDeleteSession(s.session_id);
                          }}
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M18 6 6 18"/>
                            <path d="m6 6 12 12"/>
                          </svg>
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Footer */}
      <div className="sidebar__foot">
        <div className="avatar">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/>
            <circle cx="12" cy="7" r="4"/>
          </svg>
        </div>
        <span>本地用户</span>
        <svg className="sidebar__foot-arrow" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="m9 18 6-6-6-6"/>
        </svg>
      </div>
    </aside>
  );
}
