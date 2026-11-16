/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { AppConfig } from "./types";
import type { FeatureEventRegistry } from "./lib/feature-event-dispatcher";
import {
  appHostState,
  enqueueHostConfig,
  mountAppHost,
  resetAppHostState,
  unmountAppHosts,
} from "./test-utils/appHostLifecycle";

function appConfig(wikiEnabled: boolean): AppConfig {
  return {
    model: "m", has_key: true, base_url: "", active_model_id: "m", models: [],
    wiki: { enabled: wikiEnabled },
  };
}

vi.mock("./hooks/useChat", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./hooks/useChat")>()),
  useChat: (_sid: string, _done: unknown, registry: FeatureEventRegistry) => {
  appHostState.captured.push(registry);
  return { messages: [], busy: false, queueHint: "", pendingQueue: [], todos: [], compactingContext: false, sessionStatus: {}, connected: true, planActive: false, planReview: null, wikiProgress: {}, followupQuestion: null, forSession: () => ({ messages: [], busy: false, queueHint: "", pendingQueue: [], planActive: false, followupQuestion: null, todos: [], compactingContext: false }), send: vi.fn(), stop: vi.fn(), cancelMention: vi.fn(), steer: vi.fn(), enterPlan: vi.fn(), exitPlan: vi.fn(), approvePlan: vi.fn(), rejectPlan: vi.fn(), rejectAndExitPlan: vi.fn(), answerFollowup: vi.fn(), dismissFollowup: vi.fn(), loadHistory: vi.fn(), clearSession: vi.fn(), seedStatuses: vi.fn(), removeFromQueue: vi.fn(), editQueueItem: vi.fn(), sendQueueItemNow: vi.fn() };
} }));
vi.mock("./hooks/useSessions", () => ({ useSessions: () => ({ sessions: [], refresh: vi.fn() }) }));
vi.mock("./hooks/useWorkspaces", () => ({ useWorkspaces: () => ({ workspaces: [], refresh: vi.fn() }) }));
vi.mock("./components/Sidebar", () => ({ default: () => null }));
vi.mock("./components/TopBar", () => ({ default: () => null }));
vi.mock("./components/ChatPanel", () => ({ default: () => null }));
vi.mock("./components/SkillsHub", () => ({ default: () => null }));
vi.mock("./components/TaskBoard", () => ({ default: () => null }));
vi.mock("./components/WorkspaceModal", () => ({ default: () => null }));
vi.mock("./components/Composer", () => ({ teamMemberMentionId: () => "leader" }));
vi.mock("./api", () => ({ api: { config: vi.fn(async () => appHostState.responses.shift() ?? { wiki: { enabled: true } }), sessionsStatus: vi.fn(async () => ({})), externalTeams: vi.fn(async () => []), tasks: vi.fn(async () => []), sessions: vi.fn(async () => []), workspaces: vi.fn(async () => []), switchModel: vi.fn(async () => ({ wiki: { enabled: true } })), setSessionAgentConfig: vi.fn(async () => ({})) }, ApiError: class extends Error { status = 500; } }));

const ctx = { sessionId: "s1", now: 1, startLocalTurn: () => 1, newId: () => "m1", book: { toolMap: new Map(), assistantId: null, turnStartedAt: null, awaitingAssistantAfterTool: false, deltaSpans: [], legacyDeltaText: "", hadTeamInternal: false, fileChanges: [], fileChangeSignatures: {}, prevTurnFileSignature: {} }, messages: [] };

let warnSpy: MockInstance;

describe("real App Wiki host lifecycle", () => {
  beforeEach(() => {
    resetAppHostState();
    Object.defineProperty(window, "localStorage", { configurable: true, value: { getItem: () => null, setItem: vi.fn() } });
    warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => { unmountAppHosts(); warnSpy.mockRestore(); });

  it("applies each host's loaded wiki config to its committed registry", async () => {
    enqueueHostConfig(appConfig(false));
    const disabled = await mountAppHost();
    enqueueHostConfig(appConfig(true));
    const enabled = await mountAppHost();

    // 两个宿主各自持有独立的 registry 实例（而非 StrictMode 丢弃的渲染调用实例）
    expect(disabled.registry).not.toBe(enabled.registry);

    // 关闭的宿主：生效 config 是 enabled=false，handler 已从 commit 的 registry 上卸载；
    // null 必须来自"未注册"（伴随 unhandled 告警），而不是捕到了无 handler 的杂散实例。
    await vi.waitFor(() => {
      warnSpy.mockClear();
      expect(disabled.registry.dispatch("wiki", "cards", 1, { pages: [{ id: "p1" }] }, ctx)).toBeNull();
      expect(warnSpy).toHaveBeenCalledWith("[feature-event] unhandled event: feature=wiki event=cards version=1");
    });

    // 开启的宿主：同一断言路径下 handler 在位，正常产出 effect。
    expect(enabled.registry.dispatch("wiki", "cards", 1, { pages: [{ id: "p1" }] }, ctx)).not.toBeNull();
  });
});
