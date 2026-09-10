import { describe, expect, it } from "vitest";
import { FeatureEventRegistry } from "../lib/feature-event-dispatcher";
import { installWikiFeatureHandlers } from "./WikiFeature";
import { installKanbanFeatureHandlers, installTeamFeatureHandlers } from "./feature-events";
import type { Bookkeeping, FeatureEventContext } from "../lib/feature-event-dispatcher";
import type { UiMessage, WikiPage } from "../types";

function makeCtx(book?: Partial<Bookkeeping>, messages: UiMessage[] = []): FeatureEventContext {
  return {
    sessionId: "s1",
    now: 1_000_000,
    startLocalTurn: () => 1_000_000,
    newId: () => `id_${++makeCtx.idSeq}`,
    book: {
      toolMap: new Map(),
      assistantId: null,
      turnStartedAt: null,
      awaitingAssistantAfterTool: false,
      deltaSpans: [],
      legacyDeltaText: "",
      hadTeamInternal: false,
      fileChanges: [],
      fileChangeSignatures: {},
      prevTurnFileSignature: {},
      ...book,
    },
    messages,
  };
}
makeCtx.idSeq = 0;

describe("installWikiFeatureHandlers", () => {
  it("wiki cards handler appends an assistant message when none exists", () => {
    const registry = new FeatureEventRegistry();
    installWikiFeatureHandlers(registry);
    const ctx = makeCtx();
    const page: WikiPage = {
      id: "p1",
      page_type: "topic",
      title: "Test Page",
      file_path: "test.md",
      sources: [],
      related: [],
      tags: [],
      created_at: 1,
      updated_at: 1,
      aliases: [],
    };

    const effect = registry.dispatch("wiki", "cards", 1, { pages: [page] }, ctx);

    expect(effect).not.toBeNull();
    expect(ctx.book.assistantId).toBe("id_1");
    const next = effect!.messages!([{ id: "old", role: "user", text: "hi" }]);
    expect(next).toHaveLength(2);
    expect(next[1].role).toBe("assistant");
    expect(next[1].wikiCards).toEqual([page]);
  });

  it("wiki cards handler patches existing assistant message", () => {
    const registry = new FeatureEventRegistry();
    installWikiFeatureHandlers(registry);
    const ctx = makeCtx({ assistantId: "aid_1" });
    const page: WikiPage = {
      id: "p1",
      page_type: "topic",
      title: "Test Page",
      file_path: "test.md",
      sources: [],
      related: [],
      tags: [],
      created_at: 1,
      updated_at: 1,
      aliases: [],
    };

    const effect = registry.dispatch("wiki", "cards", 1, { pages: [page] }, ctx);

    expect(effect).not.toBeNull();
    const existing: UiMessage = { id: "aid_1", role: "assistant", text: "hello" };
    const next = effect!.messages!([existing, { id: "other", role: "assistant", text: "x" }]);
    expect(next[0].wikiCards).toEqual([page]);
    expect(next[1].wikiCards).toBeUndefined();
  });

  it("wiki cards handler ignores empty pages", () => {
    const registry = new FeatureEventRegistry();
    installWikiFeatureHandlers(registry);
    const ctx = makeCtx();
    const effect = registry.dispatch("wiki", "cards", 1, { pages: [] }, ctx);
    expect(effect).toBeNull();
  });

  it("wiki ingest progress handler returns normalized progress", () => {
    const registry = new FeatureEventRegistry();
    installWikiFeatureHandlers(registry);
    const ctx = makeCtx();

    const effect = registry.dispatch("wiki", "ingest_progress", 1, {
      stage: "parsing",
      percent: 42,
      label: "解析中",
      source_id: "src_1",
      error: "oops",
    }, ctx);

    expect(effect?.wikiProgress).toEqual({
      stage: "parsing",
      percent: 42,
      label: "解析中",
      source_id: "src_1",
      session_id: "s1",
      error: "oops",
      detail: undefined,
    });
  });

  it("wiki changed handler returns changes payload", () => {
    const registry = new FeatureEventRegistry();
    installWikiFeatureHandlers(registry);
    const ctx = makeCtx();

    const effect = registry.dispatch("wiki", "changed", 1, { changes: ["p1"] }, ctx);

    expect(effect?.wikiChanged).toEqual(["p1"]);
  });
});

describe("installTeamFeatureHandlers", () => {
  it("team internal handler returns merge effect with running status", () => {
    const registry = new FeatureEventRegistry();
    installTeamFeatureHandlers(registry);
    const ctx = makeCtx();

    const effect = registry.dispatch("team", "internal_message", 1, {
      text: "节点完成",
      agent_id: "agent_1",
      agent_name: "Coder",
      event_type: "team_stream",
      node_id: "build",
    }, ctx);

    expect(effect).not.toBeNull();
    expect(effect?.statusHint).toBe("running");
    expect(effect?.queueHint).toBe("");
    expect(effect?.hadTeamInternal).toBe(true);
    const existing: UiMessage[] = [];
    const next = effect!.messages!(existing);
    expect(next).toHaveLength(1);
    expect(next[0].role).toBe("team_internal");
    expect(next[0].text).toBe("节点完成");
    expect(next[0].agentId).toBe("agent_1");
  });

  it("team internal handler honors append option", () => {
    const registry = new FeatureEventRegistry();
    installTeamFeatureHandlers(registry);
    const ctx = makeCtx();

    const effect = registry.dispatch("team", "internal_message", 1, {
      text: "第一段",
      agent_id: "agent_1",
      event_type: "team_stream",
      node_id: "build",
      source_session_id: "parent::turn::req1",
      append: true,
    }, ctx);

    const existing: UiMessage[] = [
      {
        id: "prev",
        role: "team_internal",
        text: "节点完成",
        agentId: "agent_1",
        eventType: "team_stream",
        nodeId: "build",
        sourceSessionId: "parent::turn::req1",
        displayMode: "stream",
      },
    ];
    const next = effect!.messages!(existing);
    expect(next).toHaveLength(1);
    expect(next[0].text).toBe("节点完成第一段");
  });
});

describe("installKanbanFeatureHandlers", () => {
  it("registers a no-op handler for workflow_progress compat", () => {
    const registry = new FeatureEventRegistry();
    installKanbanFeatureHandlers(registry);
    const ctx = makeCtx();

    const effect = registry.dispatch("kanban", "workflow_progress", 1, { percent: 50 }, ctx);

    expect(effect).toBeNull();
  });
});
