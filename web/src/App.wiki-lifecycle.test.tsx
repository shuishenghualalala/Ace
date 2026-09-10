/** @vitest-environment happy-dom */
import { StrictMode, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { AppConfig } from "./types";
import type { FeatureEventRegistry } from "./lib/feature-event-dispatcher";

function appConfig(wikiEnabled: boolean): AppConfig {
  return {
    model: "m", has_key: true, base_url: "", active_model_id: "m", models: [],
    wiki: { enabled: wikiEnabled },
  };
}

const captured: FeatureEventRegistry[] = [];
const responses: AppConfig[] = [];
vi.mock("./hooks/useChat", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./hooks/useChat")>()),
  useChat: (_sid: string, _done: unknown, registry: FeatureEventRegistry) => {
  captured.push(registry);
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
vi.mock("./api", () => ({ api: { config: vi.fn(async () => responses.shift() ?? { wiki: { enabled: true } }), sessionsStatus: vi.fn(async () => ({})), externalTeams: vi.fn(async () => []), tasks: vi.fn(async () => []), sessions: vi.fn(async () => []), workspaces: vi.fn(async () => []), switchModel: vi.fn(async () => ({ wiki: { enabled: true } })), setSessionAgentConfig: vi.fn(async () => ({})) }, ApiError: class extends Error { status = 500; } }));

const ctx = { sessionId: "s1", now: 1, startLocalTurn: () => 1, newId: () => "m1", book: { toolMap: new Map(), assistantId: null, turnStartedAt: null, awaitingAssistantAfterTool: false, deltaSpans: [], legacyDeltaText: "", hadTeamInternal: false, fileChanges: [], fileChangeSignatures: {}, prevTurnFileSignature: {} }, messages: [] };

describe("real App Wiki host lifecycle", () => {
  let roots: ReturnType<typeof createRoot>[] = [];
  beforeEach(() => {
    captured.length = 0; responses.length = 0;
    Object.defineProperty(window, "localStorage", { configurable: true, value: { getItem: () => null, setItem: vi.fn() } });
  });
  afterEach(() => { roots.forEach((r) => r.unmount()); roots = []; });
  it("uses independent registries for two StrictMode App hosts and applies capability loading", async () => {
    responses.push(appConfig(false), appConfig(true));
    const a = document.body.appendChild(document.createElement("div"));
    const b = document.body.appendChild(document.createElement("div"));
    const ra = createRoot(a); const rb = createRoot(b); roots = [ra, rb];
    await act(async () => {
      ra.render(<StrictMode><App /></StrictMode>);
      rb.render(<StrictMode><App /></StrictMode>);
    });
    await vi.waitFor(() => expect(captured.length).toBeGreaterThanOrEqual(2));
    const unique = [...new Set(captured)];
    expect(unique.length).toBeGreaterThanOrEqual(2);
    expect(unique[0]).not.toBe(unique.at(-1));
    expect(unique[0]!.dispatch("wiki", "cards", 1, { pages: [{ id: "p1" }] }, ctx)).toBeNull();
    expect(unique.at(-1)!.dispatch("wiki", "cards", 1, { pages: [{ id: "p1" }] }, ctx)).not.toBeNull();
  });
});
