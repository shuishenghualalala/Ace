/** @vitest-environment happy-dom */
import { createElement, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import type { FeatureEventRegistry } from "../lib/feature-event-dispatcher";
import type { Chunk } from "../types";
import { GatewaySequenceTracker } from "../ws";
import { useChat } from "./useChat";

vi.mock("../api", () => ({
  api: {
    history: vi.fn(async () => []),
    sessionStatus: vi.fn(),
    sessionPlan: vi.fn(async () => ({ active: false, has_plan: false })),
    sessionTodos: vi.fn(async () => ({ todos: [] })),
  },
}));

const sid = "session-1";
const workspace = "default";

function frame(kind: Chunk["kind"], gateway_sequence: number, body: Chunk["body"], gateway_epoch = "old"): Chunk {
  return {
    kind, body, gateway_sequence, gateway_epoch, session_id: sid,
    is_final: kind === "final", sequence: gateway_sequence,
  };
}

class TestWebSocket {
  static OPEN = 1;
  static instances: TestWebSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen?: () => void;
  onclose?: (event: { code: number; reason: string; wasClean: boolean }) => void;
  onmessage?: (event: { data: string }) => void;

  constructor(_url: string) {
    TestWebSocket.instances.push(this);
  }

  open() {
    this.readyState = TestWebSocket.OPEN;
    this.onopen?.();
  }

  send(payload: string) {
    this.sent.push(JSON.parse(payload));
  }

  receive(chunk: Chunk) {
    this.onmessage?.({ data: JSON.stringify(chunk) });
  }

  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000, reason: "test", wasClean: true });
  }
}

const status = (live: "running" | "idle") => ({
  session_id: sid, live, queue_depth: 0, last_status: "", last_error: "",
});
const registry = { dispatch: vi.fn(() => null) } as unknown as FeatureEventRegistry;
const afterFinal = vi.fn();
let chat: ReturnType<typeof useChat>;
let root: Root;

function Probe() {
  chat = useChat(sid, afterFinal, registry);
  return null;
}

beforeEach(async () => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("WebSocket", TestWebSocket);
  TestWebSocket.instances = [];
  root = createRoot(document.createElement("div"));
  await act(async () => root.render(createElement(Probe)));
  await act(async () => TestWebSocket.instances[0].open());
});

afterEach(async () => {
  await act(async () => root.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Web Gateway recovery", () => {
  it("subscribes with the old epoch, holds the visible prefix, then replaces it from persisted history", async () => {
    vi.mocked(api.sessionStatus)
      .mockResolvedValueOnce(status("running"))
      .mockResolvedValueOnce(status("idle"));
    vi.mocked(api.history).mockResolvedValueOnce([{ role: "assistant", content: "persisted full response" }]);
    const old = TestWebSocket.instances[0];
    expect(old.sent.at(-1)).not.toHaveProperty("gateway_cursors");
    await act(async () => chat.send("first prompt", sid, "agent", workspace));
    await act(async () => {
      old.receive(frame("delta", 2005, { text: "visible prefix" }));
      old.receive(frame("followup_question", 2006, { question_id: "old prompt", title: "Old prompt" }));
    });
    await act(async () => chat.send("local queued text", sid, "agent", workspace));
    await act(async () => {
      old.close();
      await vi.advanceTimersByTimeAsync(1500);
    });

    const current = TestWebSocket.instances[1];
    await act(async () => current.open());
    expect(current.sent.at(-1)).toMatchObject({
      action: "subscribe",
      last_gateway_sequences: { [sid]: 2006 },
      gateway_cursors: { [sid]: { epoch: "old", sequence: 2006 } },
    });
    await act(async () => current.receive(frame("gateway_resync", 0, {
      reason: "epoch_changed", floor: 1,
      pending_followups: [frame("followup_question", 0, { question_id: "pending", title: "Pending" }, "new")],
    }, "new")));
    expect(chat.followupQuestion?.question_id).toBe("pending");
    expect(chat.recoveryNotice).toContain("回复仍在生成");
    expect(chat.messages.some((message) => message.text === "visible prefix")).toBe(true);

    await act(async () => {
      old.receive(frame("final", 2007, { text: "old socket" }));
      current.receive(frame("delta", 1, { text: "an incomplete suffix" }, "new"));
    });
    expect(chat.messages.map((message) => message.text)).not.toContain("an incomplete suffix");
    expect(chat.messages.map((message) => message.text)).not.toContain("old socket");
    expect(api.history).not.toHaveBeenCalled();

    await act(async () => {
      current.receive(frame("followup_question", 2, { question_id: "pending", status: "resolved" }, "new"));
      current.receive(frame("final", 3, { text: "transient final" }, "new"));
      current.receive(frame("final", 3, { text: "duplicated final" }, "new"));
    });
    expect(api.history).toHaveBeenCalledTimes(1);
    expect(chat.messages.map((message) => message.text)).toEqual(["persisted full response", "local queued text"]);
    expect(chat.followupQuestion).toBeNull();
    expect(chat.pendingQueue).toEqual([]);
    expect(current.sent.filter((payload) => payload.query === "local queued text")).toHaveLength(1);
    expect(afterFinal).not.toHaveBeenCalled();
  });

  it("discards history from an older reset after a new epoch supersedes it", async () => {
    vi.mocked(api.sessionStatus).mockResolvedValue(status("idle"));
    let resolveOldHistory!: (items: Awaited<ReturnType<typeof api.history>>) => void;
    vi.mocked(api.history)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOldHistory = resolve; }))
      .mockResolvedValueOnce([{ role: "assistant", content: "new authoritative history" }]);
    const socket = TestWebSocket.instances[0];

    await act(async () => socket.receive(frame("gateway_resync", 100, { reason: "cursor_expired", floor: 50 })));
    expect(api.history).toHaveBeenCalledTimes(1);
    await act(async () => socket.receive(frame("gateway_resync", 0, { reason: "epoch_changed", floor: 1 }, "new")));
    expect(chat.messages.map((message) => message.text)).toEqual(["new authoritative history"]);

    await act(async () => resolveOldHistory([{ role: "assistant", content: "stale history" }]));
    expect(chat.messages.map((message) => message.text)).toEqual(["new authoritative history"]);
  });

  it("does not apply an idle history response after a newer request starts", async () => {
    vi.mocked(api.sessionStatus)
      .mockResolvedValueOnce(status("idle"))
      .mockResolvedValueOnce(status("running"))
      .mockResolvedValueOnce(status("idle"));
    let resolveOldHistory!: (items: Awaited<ReturnType<typeof api.history>>) => void;
    vi.mocked(api.history)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOldHistory = resolve; }))
      .mockResolvedValueOnce([{ role: "assistant", content: "new result" }]);
    const socket = TestWebSocket.instances[0];
    await act(async () => socket.receive(frame("gateway_resync", 100, { reason: "cursor_expired", floor: 50 })));
    await act(async () => {
      socket.receive(frame("delta", 101, { text: "new active turn" }));
      resolveOldHistory([{ role: "assistant", content: "old result" }]);
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(chat.recoveryNotice).toContain("回复仍在生成");
    expect(chat.messages).toEqual([]);
    await act(async () => socket.receive(frame("final", 102, { text: "new result" })));
    expect(chat.messages.map((message) => message.text)).toEqual(["new result"]);
  });

  it.each(["followup", "plan"])("keeps the local queue while a %s requires user input", async (blockedBy) => {
    vi.mocked(api.sessionStatus).mockResolvedValueOnce(status("idle"));
    vi.mocked(api.history).mockResolvedValueOnce([]);
    if (blockedBy === "plan") {
      vi.mocked(api.sessionPlan).mockResolvedValueOnce({
        session_id: sid, active: true, has_plan: true, awaiting_approval: true,
        status: "pending", plan: "Pending plan", plan_file: "",
      });
    }
    const socket = TestWebSocket.instances[0];
    await act(async () => chat.send("first prompt", sid, "agent", workspace));
    await act(async () => socket.receive(frame("delta", 98, { text: "prefix" })));
    await act(async () => chat.send("waiting prompt", sid, "agent", workspace));
    await act(async () => socket.receive(frame("gateway_resync", 100, {
      reason: "cursor_expired", floor: 50,
      ...(blockedBy === "followup" && {
        pending_followups: [frame("followup_question", 99, { question_id: "pending" })],
      }),
    })));
    expect(chat.pendingQueue).toEqual([expect.objectContaining({ query: "waiting prompt" })]);
    expect(socket.sent.some((payload) => payload.query === "waiting prompt")).toBe(false);
    expect(afterFinal).not.toHaveBeenCalled();
  });

  it("keeps an already-visible question during same-epoch recovery until idle is verified", async () => {
    vi.mocked(api.sessionStatus)
      .mockResolvedValueOnce(status("running"))
      .mockResolvedValueOnce(status("idle"));
    vi.mocked(api.history).mockResolvedValueOnce([]);
    const socket = TestWebSocket.instances[0];
    await act(async () => socket.receive(frame("followup_question", 98, { question_id: "possibly pending" })));
    await act(async () => socket.receive(frame("gateway_resync", 100, { reason: "cursor_expired", floor: 50 })));
    expect(chat.followupQuestion?.question_id).toBe("possibly pending");
    await act(async () => socket.receive(frame("final", 101, { text: "done" })));
    expect(chat.followupQuestion).toBeNull();
  });

  it("accepts a cursor-ahead reset below the old deduplication floor", () => {
    const tracker = new GatewaySequenceTracker();
    for (let n = 2; n <= 2010; n += 1) tracker.accept(sid, frame("delta", n, {}));
    expect(tracker.accept(sid, frame("delta", 1, {}))).toBe(true);
    expect(tracker.reset(sid, { epoch: "old", sequence: 7 })).toBe(true);
    expect(tracker.reset(sid, { epoch: "old", sequence: 7 })).toBe(false);
    expect(tracker.accept(sid, frame("delta", 7, {}))).toBe(false);
    expect(tracker.accept(sid, frame("delta", 9, {}))).toBe(true);
    expect(tracker.accept(sid, frame("delta", 8, {}))).toBe(true);
    expect(tracker.accept(sid, frame("delta", 9, {}))).toBe(false);
  });
});
