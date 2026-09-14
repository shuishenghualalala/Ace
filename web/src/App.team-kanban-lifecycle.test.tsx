/** @vitest-environment happy-dom */
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import {
  KANBAN_FEATURE_ID,
  TEAM_FEATURE_ID,
} from "./lib/featureFlags";
import type { AppConfig, ExternalTeam } from "./types";
import type { FeatureEventRegistry } from "./lib/feature-event-dispatcher";
import {
  appHostState,
  enqueueHostConfig,
  mountAppHost,
  resetAppHostState,
  unmountAppHosts,
} from "./test-utils/appHostLifecycle";

function appConfig(overrides: Partial<AppConfig>): AppConfig {
  return {
    model: "m", has_key: true, base_url: "", active_model_id: "m", models: [],
    wiki: { enabled: true },
    ...overrides,
  };
}

function capability(available: boolean) {
  return { state: "active", available, generation: null };
}

const stubTeam: ExternalTeam = {
  id: "team-1", name: "Team 1", description: "", leader_agent_id: "leader",
  instructions: "", created_at: "", updated_at: "", members: [],
};

vi.mock("./hooks/useChat", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./hooks/useChat")>()),
  useChat: (_sid: string, _done: unknown, registry: FeatureEventRegistry) => {
  appHostState.captured.push(registry);
  return { messages: [], busy: false, queueHint: "", pendingQueue: [], todos: [], compactingContext: false, sessionStatus: {}, connected: true, planActive: false, planReview: null, wikiProgress: {}, followupQuestion: null, forSession: () => ({ messages: [], busy: false, queueHint: "", pendingQueue: [], planActive: false, followupQuestion: null, todos: [], compactingContext: false }), send: vi.fn(), stop: vi.fn(), cancelMention: vi.fn(), steer: vi.fn(), enterPlan: vi.fn(), exitPlan: vi.fn(), approvePlan: vi.fn(), rejectPlan: vi.fn(), rejectAndExitPlan: vi.fn(), answerFollowup: vi.fn(), dismissFollowup: vi.fn(), loadHistory: vi.fn(), clearSession: vi.fn(), seedStatuses: vi.fn(), removeFromQueue: vi.fn(), editQueueItem: vi.fn(), sendQueueItemNow: vi.fn() };
} }));
vi.mock("./hooks/useSessions", () => ({ useSessions: () => ({ sessions: [], refresh: vi.fn() }) }));
vi.mock("./hooks/useWorkspaces", () => ({ useWorkspaces: () => ({ workspaces: [], refresh: vi.fn() }) }));
vi.mock("./components/Sidebar", () => ({ default: ({ onViewChange }: { onViewChange: (view: string) => void }) => (
  <button type="button" data-testid="nav-agents" onClick={() => onViewChange("agents")} />
) }));
vi.mock("./components/AgentsHub", () => ({ default: ({ onAssignTeam }: { onAssignTeam: (team: ExternalTeam) => void }) => (
  <button type="button" data-testid="assign-team" onClick={() => onAssignTeam(stubTeam)} />
) }));
vi.mock("./components/TopBar", () => ({ default: () => null }));
vi.mock("./components/ChatPanel", () => ({ default: () => null }));
vi.mock("./components/SkillsHub", () => ({ default: () => null }));
vi.mock("./components/TaskBoard", () => ({ default: () => <aside data-testid="task-board" /> }));
vi.mock("./components/WorkspaceModal", () => ({ default: () => null }));
vi.mock("./components/Composer", () => ({ teamMemberMentionId: () => "leader" }));
vi.mock("./api", () => ({ api: { config: vi.fn(async () => appHostState.responses.shift() ?? { wiki: { enabled: true } }), sessionsStatus: vi.fn(async () => ({})), externalTeams: vi.fn(async () => []), tasks: vi.fn(async () => []), sessions: vi.fn(async () => []), workspaces: vi.fn(async () => []), switchModel: vi.fn(async () => ({ wiki: { enabled: true } })), setSessionAgentConfig: vi.fn(async () => ({})) }, ApiError: class extends Error { status = 500; } }));

const ctx = { sessionId: "s1", now: 1, startLocalTurn: () => 1, newId: () => "m1", book: { toolMap: new Map(), assistantId: null, turnStartedAt: null, awaitingAssistantAfterTool: false, deltaSpans: [], legacyDeltaText: "", hadTeamInternal: false, fileChanges: [], fileChangeSignatures: {}, prevTurnFileSignature: {} }, messages: [] };

let warnSpy: MockInstance;

describe("real App team/kanban host lifecycle", () => {
  beforeEach(() => {
    resetAppHostState();
    Object.defineProperty(window, "localStorage", { configurable: true, value: { getItem: () => null, setItem: vi.fn() } });
    warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => { unmountAppHosts(); warnSpy.mockRestore(); });

  it("installs team handlers only while the product.team capability is available", async () => {
    enqueueHostConfig(
      appConfig({ feature_capabilities: { [TEAM_FEATURE_ID]: capability(false) } }),
      appConfig({ feature_capabilities: { [TEAM_FEATURE_ID]: capability(true) } }),
    );
    const off = await mountAppHost();
    const on = await mountAppHost();

    await vi.waitFor(() => expect(
      off.registry.dispatch("team", "internal_message", 1, { text: "hi" }, ctx),
    ).toBeNull());
    warnSpy.mockClear();
    expect(off.registry.dispatch("team", "internal_message", 1, { text: "hi" }, ctx)).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith("[feature-event] unhandled event: feature=team event=internal_message version=1");

    const effect = on.registry.dispatch("team", "internal_message", 1, { text: "hi" }, ctx);
    expect(effect).not.toBeNull();
    expect(effect?.hadTeamInternal).toBe(true);
    expect(effect?.statusHint).toBe("running");
    expect(effect?.queueHint).toBe("");
  });

  it("distinguishes registered-null from unregistered-null for kanban workflow_progress", async () => {
    enqueueHostConfig(
      appConfig({ feature_capabilities: { [KANBAN_FEATURE_ID]: capability(false) } }),
      appConfig({ feature_capabilities: { [KANBAN_FEATURE_ID]: capability(true) } }),
    );
    const off = await mountAppHost();
    const on = await mountAppHost();

    await vi.waitFor(() => {
      warnSpy.mockClear();
      expect(off.registry.dispatch("kanban", "workflow_progress", 1, {}, ctx)).toBeNull();
      expect(warnSpy).toHaveBeenCalledWith("[feature-event] unhandled event: feature=kanban event=workflow_progress version=1");
    });

    warnSpy.mockClear();
    expect(on.registry.dispatch("kanban", "workflow_progress", 1, {}, ctx)).toBeNull();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("keeps team and kanban handlers working for legacy configs without feature_capabilities", async () => {
    enqueueHostConfig(appConfig({}));
    const { registry } = await mountAppHost();

    const effect = registry.dispatch("team", "internal_message", 1, { text: "hi" }, ctx);
    expect(effect).not.toBeNull();
    expect(effect?.hadTeamInternal).toBe(true);

    warnSpy.mockClear();
    expect(registry.dispatch("kanban", "workflow_progress", 1, {}, ctx)).toBeNull();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it("hides the TaskBoard for team sessions while product.team is unavailable", async () => {
    enqueueHostConfig(appConfig({ feature_capabilities: { [TEAM_FEATURE_ID]: capability(false) } }));
    const { container } = await mountAppHost();

    await act(async () => { container.querySelector<HTMLButtonElement>('[data-testid="nav-agents"]')!.click(); });
    await act(async () => { container.querySelector<HTMLButtonElement>('[data-testid="assign-team"]')!.click(); });
    await act(async () => {});

    // 能力关闭时即使会话仍停留在 team 模式（boardOpen 被置回 false），也不渲染看板根节点
    expect(container.querySelector('[data-testid="task-board"]')).toBeNull();
  });

  it("keeps the TaskBoard for team sessions while product.team is available", async () => {
    enqueueHostConfig(appConfig({ feature_capabilities: { [TEAM_FEATURE_ID]: capability(true) } }));
    const { container } = await mountAppHost();

    await act(async () => { container.querySelector<HTMLButtonElement>('[data-testid="nav-agents"]')!.click(); });
    await act(async () => { container.querySelector<HTMLButtonElement>('[data-testid="assign-team"]')!.click(); });
    await act(async () => {});

    expect(container.querySelector('[data-testid="task-board"]')).not.toBeNull();
  });
});

describe("team/kanban feature capability rules", () => {
  // 规则分支的完整表格见 src/lib/featureFlags.test.ts；这里只钉死后端 feature id 字面量。
  it("exposes the backend feature ids", () => {
    expect(TEAM_FEATURE_ID).toBe("product.team");
    expect(KANBAN_FEATURE_ID).toBe("product.dynamic-kanban");
  });
});
