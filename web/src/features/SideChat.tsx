import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import ChatPanel from "../components/ChatPanel";
import type { useChat } from "../hooks/useChat";
import type { AppConfig, Attachment } from "../types";

type Chat = ReturnType<typeof useChat>;

type SideChatState = { parentId: string } & (
  | { status: "opening" }
  | { status: "failed"; message: string }
  | { status: "ready"; sessionId: string; workspaceId: string }
);

export function useSideChat(parentId: string | null, loadHistory: Chat["loadHistory"]) {
  const [pane, setPane] = useState<SideChatState | null>(null);
  const request = useRef<AbortController | null>(null);
  const close = useCallback(() => {
    request.current?.abort();
    request.current = null;
    setPane(null);
  }, []);

  useLayoutEffect(() => {
    setPane((current) => current?.parentId === parentId ? current : null);
    return () => {
      request.current?.abort();
      request.current = null;
    };
  }, [parentId]);

  const open = useCallback(() => {
    if (!parentId || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const { signal } = controller;
    setPane({ parentId, status: "opening" });
    void (async () => {
      try {
        const child = await api.createSideChat(parentId, signal);
        if (signal.aborted) return;
        if (child.source_session_id !== parentId || !child.session_id || child.session_id === parentId || !child.workspace_id) {
          throw new Error("网关未返回有效的侧边会话");
        }
        await loadHistory(child.session_id);
        if (signal.aborted) return;
        setPane({ parentId, status: "ready", sessionId: child.session_id, workspaceId: child.workspace_id });
      } catch (error) {
        if (signal.aborted) return;
        request.current = null;
        const message = error instanceof ApiError
          ? error.body?.error || (error.status === 404 ? "找不到可创建侧边聊天的主会话" : `网关返回 ${error.status}`)
          : error instanceof Error ? error.message : "请重试";
        setPane({ parentId, status: "failed", message });
      }
    })();
  }, [parentId, loadHistory]);

  return { pane: pane?.parentId === parentId ? pane : null, open, close };
}

export function SideChatPane({ pane, chat, config, onClose, onRetry }: {
  pane: SideChatState;
  chat: Chat;
  config: AppConfig | null;
  onClose: () => void;
  onRetry: () => void;
}) {
  const readyId = pane.status === "ready" ? pane.sessionId : null;
  const root = useRef<HTMLElement>(null);
  useEffect(() => {
    if (readyId) root.current?.querySelector("textarea")?.focus();
  }, [readyId]);

  return (
    <aside className="side-chat" aria-label="侧边聊天" ref={root}>
      <header className="side-chat__header">
        <div><strong>侧边聊天</strong><span>从主会话的已保存历史分出，消息独立保存</span></div>
        <button className="side-chat__close" type="button" aria-label="关闭侧边聊天" onClick={onClose}>×</button>
      </header>
      {pane.status === "opening" && <p className="side-chat__notice" role="status">正在打开侧边聊天…</p>}
      {pane.status === "failed" && (
        <div className="side-chat__notice">
          <p role="alert">无法打开侧边聊天：{pane.message}</p>
          <button type="button" onClick={onRetry}>重试</button>
        </div>
      )}
      {pane.status === "ready" && <SideChatConversation key={pane.sessionId} pane={pane} chat={chat} config={config} />}
    </aside>
  );
}

function SideChatConversation({ pane, chat, config }: {
  pane: Extract<SideChatState, { status: "ready" }>;
  chat: Chat;
  config: AppConfig | null;
}) {
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const { sessionId, workspaceId } = pane;
  const conversation = chat.forSession(sessionId);
  return (
    <ChatPanel
      {...conversation}
      sessionId={sessionId}
      config={config}
      attachments={attachments}
      onAttachmentsChange={setAttachments}
      uploadContext={{ sessionId }}
      onSend={(query, files) => chat.send(query, sessionId, "agent", workspaceId, files)}
      onStop={() => chat.stop(sessionId, conversation.activeRequestId)}
      onRemoveFromQueue={(index) => chat.removeFromQueue(sessionId, index)}
      onEditQueueItem={(index, query) => chat.editQueueItem(sessionId, index, query)}
      onSendQueueItemNow={(id) => chat.sendQueueItemNow(sessionId, id)}
      onEnterPlan={() => chat.enterPlan(sessionId)}
      onExitPlan={() => chat.exitPlan(sessionId)}
      onPlanDecision={(action, review) => chat.decidePlan(action, review, sessionId, "agent", workspaceId)}
      onAnswerFollowup={(id, answers) => chat.answerFollowup(sessionId, id, answers)}
      onDismissFollowup={() => chat.dismissFollowup(sessionId)}
      embedded
      compactTools
    />
  );
}
