import { createElement, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import WikiHub from "../components/WikiHub";
import type { Props as ChatPanelProps } from "../components/ChatPanel";
import { api } from "../api";
import type { Attachment, Mode, Session } from "../types";
import type { useChat } from "../hooks/useChat";
import { UiPageRegistry, type UiPageContribution } from "../lib/ui-feature-registry";

type WikiAgentSessionBinding = { kbId: string; sessionId: string };

export function resolveWikiAgentSessionId(
  binding: WikiAgentSessionBinding | null,
  kbId: string,
): string | null {
  return binding?.kbId === kbId ? binding.sessionId : null;
}

type WikiChatController = ReturnType<typeof useChat>;

export interface WikiFeatureProps {
  baseChatProps: ChatPanelProps;
  chat: WikiChatController;
  mode: Mode;
  workspaceId: string;
  currentAgentLabel?: Session["agent_label"];
  pendingWikiLinkTitle?: string | null;
  onPendingWikiLinkHandled?: () => void;
  store?: WikiFeatureStore;
}

// 页面组件会卸载，但 Wiki feature 的选择语义属于安装期，跨页保留。
export interface WikiFeatureStore {
  kbId: string;
}

export function createWikiFeatureStore(): WikiFeatureStore {
  return { kbId: "default" };
}

export interface WikiPageContext {
  wikiEnabled: boolean;
  props: Omit<WikiFeatureProps, "store"> & { store: WikiFeatureStore };
}

export type WikiPageContribution = UiPageContribution<string, WikiPageContext, ReactNode>;
export type WikiPageRegistry = UiPageRegistry<string, WikiPageContext, ReactNode>;

export function installWikiPageContribution(registry: WikiPageRegistry): () => void {
  return registry.register({
    id: "wiki",
    isAvailable: ({ wikiEnabled }) => wikiEnabled,
    render: ({ props }) => createElement(WikiFeature, props),
  });
}

export default function WikiFeature({
  baseChatProps,
  chat,
  mode,
  workspaceId,
  currentAgentLabel,
  pendingWikiLinkTitle,
  onPendingWikiLinkHandled,
  store,
}: WikiFeatureProps) {
  const [ownedStore] = useState(createWikiFeatureStore);
  const installationStore = store || ownedStore;
  const [kbId, setKbId] = useState(installationStore.kbId);
  const [binding, setBinding] = useState<WikiAgentSessionBinding | null>(
    () => null,
  );
  const sessionId = resolveWikiAgentSessionId(binding, kbId) || "";
  const chatRef = useRef(chat);
  chatRef.current = chat;
  const operationRef = useRef(0);
  const mountedRef = useRef(true);
  useEffect(() => () => {
    mountedRef.current = false;
    operationRef.current += 1;
  }, []);

  useEffect(() => {
    installationStore.kbId = kbId;
  }, [installationStore, kbId, binding]);

  useEffect(() => {
    let active = true;
    const operation = ++operationRef.current;
    setBinding(null);
    api.wikiAgentSession(kbId).then(({ session_id }) => {
        if (!active || operation !== operationRef.current) return;
        const next = { kbId, sessionId: session_id };
        setBinding(next);
        chatRef.current.loadHistory(session_id);
      }).catch(() => {});
    return () => { active = false; };
  }, [installationStore, kbId]);

  const handleNewSession = useCallback(async () => {
    const operation = ++operationRef.current;
    const requestKbId = kbId;
    const { session_id } = await api.wikiAgentSession(requestKbId, { forceNew: true });
    if (!mountedRef.current || operation !== operationRef.current || installationStore.kbId !== requestKbId) return;
    const next = { kbId, sessionId: session_id };
    setBinding(next);
    chatRef.current.loadHistory(session_id);
  }, [installationStore, kbId]);

  const handleSelectSession = useCallback((nextSessionId: string) => {
    if (!nextSessionId || nextSessionId === sessionId) return;
    ++operationRef.current;
    const next = { kbId, sessionId: nextSessionId };
    setBinding(next);
    chatRef.current.loadHistory(nextSessionId);
  }, [installationStore, kbId, sessionId]);

  const handleDeleteSession = useCallback(async (deletedSessionId: string) => {
    const operation = ++operationRef.current;
    const requestKbId = kbId;
    await api.deleteSession(deletedSessionId);
    chat.clearSession(deletedSessionId);
    if (!mountedRef.current || deletedSessionId !== sessionId || operation !== operationRef.current || installationStore.kbId !== requestKbId) return;
    const { session_id } = await api.wikiAgentSession(requestKbId);
    if (!mountedRef.current || operation !== operationRef.current || installationStore.kbId !== requestKbId) return;
    const next = { kbId, sessionId: session_id };
    setBinding(next);
    chatRef.current.loadHistory(session_id);
  }, [installationStore, chat, kbId, sessionId]);

  const wikiChatProps = useMemo<ChatPanelProps>(() => ({
    ...baseChatProps,
    ...chat.forSession(sessionId),
    currentAgentLabel,
    uploadContext: { sessionId, kbId },
    onSend: (text: string, attachments: Attachment[]) => {
      if (!sessionId) return;
      chat.send(text, sessionId, mode, workspaceId, attachments, { wikiKbId: kbId });
    },
    onAsk: (text: string) => {
      if (!sessionId) return;
      chat.send(text, sessionId, mode, workspaceId, [], { wikiKbId: kbId });
    },
    onStop: () => chat.stop(sessionId),
    onSteer: (text) => chat.steer(sessionId, text),
    onRemoveFromQueue: (i) => chat.removeFromQueue(sessionId, i),
    onEditQueueItem: (i, q) => chat.editQueueItem(sessionId, i, q),
    onSendQueueItemNow: (id) => chat.sendQueueItemNow(sessionId, id),
    onEnterPlan: () => chat.enterPlan(sessionId),
    onExitPlan: () => chat.exitPlan(sessionId),
    onApprovePlan: () => chat.approvePlan(sessionId, mode, workspaceId),
    onRejectPlan: () => chat.rejectPlan(sessionId),
    onRejectAndExitPlan: () => chat.rejectAndExitPlan(sessionId),
    onAnswerFollowup: (questionId, answers) => chat.answerFollowup(sessionId, questionId, answers),
    onDismissFollowup: () => chat.dismissFollowup(sessionId),
  }), [baseChatProps, chat, currentAgentLabel, kbId, mode, sessionId, workspaceId]);

  return (
    <WikiHub
      chatProps={wikiChatProps}
      kbId={kbId}
      onKbChange={setKbId}
      sessionId={sessionId}
      wikiProgress={chat.wikiProgress}
      onNewSession={handleNewSession}
      onSelectSession={handleSelectSession}
      onDeleteSession={handleDeleteSession}
      pendingWikiLinkTitle={pendingWikiLinkTitle}
      onPendingWikiLinkHandled={onPendingWikiLinkHandled}
    />
  );
}
