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

describe("Web Goal turn ownership", () => {
  it("queues ordinary text behind a streamed Goal and reconciles both turns before sending it", async () => {
    const goalId = "goal_0123456789abcdef0123456789abcdef";
    vi.mocked(api.sessionStatus).mockResolvedValueOnce(status("idle"));
    vi.mocked(api.history).mockResolvedValueOnce([
      { role: "user", content: "Earlier ordinary request", request_id: "normal" },
      { role: "assistant", content: "Earlier result", request_id: "normal" },
      { role: "user", content: "推进已保存的会话目标。", request_id: goalId },
      { role: "assistant", content: "Goal result", request_id: goalId },
    ]);
    const socket = TestWebSocket.instances[0];
    const goalFrame = (kind: Chunk["kind"], seq: number, body: Chunk["body"]) =>
      socket.receive({ ...frame(kind, seq, body), request_id: goalId });
    await act(async () => goalFrame("status", 1, { message: "Goal runtime admitted" }));
    expect(chat.busy).toBe(true);
    await act(async () => chat.send("Leave this ordinary message queued", sid, "agent", workspace));
    expect(chat.pendingQueue.map((item) => item.query)).toEqual(["Leave this ordinary message queued"]);
    expect(socket.sent.some((payload) => payload.query === "Leave this ordinary message queued")).toBe(false);
    await act(async () => goalFrame("delta", 2, { text: "Goal result" }));
    expect(chat.messages.at(-1)).toMatchObject({ text: "Goal result", requestId: goalId });
    await act(async () => goalFrame("final", 3, { text: "Goal result" }));
    expect(api.history).toHaveBeenCalledWith(sid);
    expect(chat.messages.map((message) => message.text)).toEqual([
      "Earlier ordinary request", "Earlier result", "推进已保存的会话目标。", "Goal result", "Leave this ordinary message queued",
    ]);
    expect(socket.sent.filter((payload) => payload.query === "Leave this ordinary message queued")).toHaveLength(1);
  });

  it("leaves the already visible Goal reply intact if the canonical read fails", async () => {
    const goalId = "goal_0123456789abcdef0123456789abcdef";
    vi.mocked(api.sessionStatus).mockResolvedValueOnce(status("idle"));
    vi.mocked(api.history).mockRejectedValueOnce(new Error("fixture temporarily unavailable"));
    const socket = TestWebSocket.instances[0];
    await act(async () => {
      socket.receive({ ...frame("delta", 1, { text: "Visible goal result" }), request_id: goalId });
      socket.receive({ ...frame("final", 2, { text: "Visible goal result" }), request_id: goalId });
    });
    expect(chat.busy).toBe(false);
    expect(api.history).toHaveBeenCalledWith(sid);
    expect(chat.messages).toMatchObject([{ text: "Visible goal result", requestId: goalId }]);
  });
});

describe("Web Gateway recovery", () => {
  it("splits both tabs at the accepted question exactly once, even without a tool result", async () => {
    let peer!: ReturnType<typeof useChat>;
    function Peer() { peer = useChat(sid, afterFinal, registry); return null; }
    const peerRoot = createRoot(document.createElement("div"));
    await act(async () => peerRoot.render(createElement(Peer)));
    const [sender, observer] = TestWebSocket.instances;
    const send = (socket: TestWebSocket, kind: Chunk["kind"], seq: number, body: Chunk["body"], request = "turn") =>
      socket.receive({ ...frame(kind, seq, body), request_id: request });
    const question = { question_id: "q", questions: [{ id: "choice", question: "Pick", options: ["red"] }] };
    const resolution = { status: "resolved", question_id: "q", answer_history_id: "followup:q", answer_text: "Chosen red" };
    try {
      await act(async () => observer.open());
      await act(async () => {
        for (const socket of [sender, observer]) {
          send(socket, "delta", 1, { text: "before" });
          send(socket, "tool", 2, { phase: "start", name: "ask_followup_question", tool_call_id: "ask" });
        }
        send(sender, "followup_question", 3, question);
      });
      await act(async () => {
        expect(chat.answerFollowup(sid, "q", [{ question_id: "choice", answers: ["red"] }])).toBe(true);
        send(observer, "followup_question", 3, question);
        for (const socket of [sender, observer]) {
          send(socket, "followup_question", 4, resolution);
          send(socket, "delta", 5, { text: "after" });
          send(socket, "followup_question", 6, { ...question, question_id: "new" });
          send(socket, "followup_question", 7, resolution);
          send(socket, "followup_question", 8, { ...resolution, question_id: "new", answer_history_id: "followup:new" }, "old-turn");
          send(socket, "delta", 9, { text: " suffix" });
        }
      });
      for (const tab of [chat, peer]) {
        expect(tab.messages.map(message => [message.role, message.text])).toEqual([
          ["assistant", "before"], ["user", "Chosen red"], ["assistant", "after suffix"],
        ]);
        expect(tab.messages[1].id).toBe("followup:q");
        expect(tab.messages[0].toolCalls?.map(call => call.toolCallId)).toEqual(["ask"]);
        expect(tab.messages[2].toolCalls?.length ?? 0).toBe(0);
        expect(tab.followupQuestion?.question_id).toBe("new");
      }
      await act(async () => { expect(chat.answerFollowup(sid, "q", [])).toBe(false); });
      expect(sender.sent.filter(payload => payload.action === "followup_answer")).toHaveLength(1);
    } finally { await act(async () => peerRoot.unmount()); }
  });
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


  it("rebuilds the active transcript across tools and thinking, then continues from the cut", async () => {
    const socket = TestWebSocket.instances[0];
    expect(socket.sent.at(-1)).toMatchObject({ action: "subscribe", gateway_snapshots: true });
    const attachment = { id: "upload", name: "note.txt", type: "file" } as const;
    await act(async () => chat.send("previous prompt", sid, "agent", workspace));
    await act(async () => socket.receive(frame("final", 1, { text: "completed response" })));
    await act(async () => chat.send("current prompt", sid, "agent", workspace, [attachment]));
    await act(async () => socket.receive(frame("delta", 2, { text: "partial from old socket" })));
    await act(async () => chat.send("queued prompt", sid, "agent", workspace, [attachment]));

    const requestId = "active";
    const turnFrame = (kind: Chunk["kind"], seq: number, body: Chunk["body"]) =>
      ({ ...frame(kind, seq, body), request_id: requestId });
    const startedAt = 1_700_000_000;
    const snapshot = frame("gateway_resync", 30, {
      reason: "cursor_expired",
      floor: 19,
      turn_snapshot: {
        request_id: requestId,
        started_at: startedAt,
        history: {
          items: [
            { role: "user", content: "previous prompt" },
            { role: "assistant", content: "completed response" },
            { role: "user", content: "current prompt" },
          ],
          has_more: false,
          next_before: null,
        },
        frames: [
          turnFrame("thinking", 5, { text: "first thought" }),
          { ...turnFrame("delta", 9, { text: "first segment", delta_start: 2, delta_end: 4 }), sequence: 4 },
          turnFrame("tool", 11, { phase: "generating", tool_call_id: "one", name: "read" }),
          turnFrame("tool", 12, { phase: "start", tool_call_id: "one", args: "note.txt" }),
          turnFrame("tool", 13, { phase: "result", tool_call_id: "one", detail: "read result" }),
          turnFrame("thinking", 14, { text: "second thought " }),
          turnFrame("thinking", 15, { text: "continues" }),
          { ...turnFrame("delta", 17, { text: "second segment", delta_start: 11, delta_end: 12 }), sequence: 12 },
          turnFrame("tool", 18, { phase: "start", tool_call_id: "two", name: "edit", args: "note.txt" }),
          turnFrame("tool", 20, { phase: "result", tool_call_id: "two", detail: "edit result" }),
          turnFrame("status", 22, { message: "Reviewing the result" }),
          turnFrame("todo_updated", 23, { todos: [{ id: "todo", content: "Review", status: "in_progress" }] }),
          turnFrame("file_changes", 25, { files: [{ path: "note.txt", status: "modified", added: 1 }] }),
          turnFrame("thinking", 28, { text: "third thought" }),
          { ...turnFrame("delta", 30, { text: "third", delta_start: 22, delta_end: 23 }), sequence: 23 },
        ],
      },
    });
    await act(async () => socket.receive(snapshot));

    expect(chat.messages.map((message) => [message.role, message.text])).toEqual([
      ["user", "previous prompt"],
      ["assistant", "completed response"],
      ["user", "current prompt"],
      ["assistant", "first segment"],
      ["assistant", "second segment"],
      ["status", "Reviewing the result"],
      ["assistant", "third"],
    ]);
    expect(chat.messages[2].attachments).toEqual([attachment]);
    expect(chat.messages[3]).toMatchObject({
      thinking: "first thought",
      turnStartedAt: startedAt * 1000,
      toolCalls: [{ toolCallId: "one", args: "note.txt", result: "read result", status: "done" }],
    });
    expect(chat.messages[4]).toMatchObject({
      thinking: "second thought continues",
      toolCalls: [{ toolCallId: "two", args: "note.txt", result: "edit result", status: "done" }],
    });
    expect(chat.messages[6].thinking).toBe("third thought");
    expect(chat.todos).toEqual([{ id: "todo", content: "Review", status: "in_progress" }]);
    expect(chat.pendingQueue.map((message) => message.query)).toEqual(["queued prompt"]);
    expect(chat.recoveryNotice).toBe("");
    expect(chat.busy).toBe(true);
    expect(afterFinal).toHaveBeenCalledTimes(1);
    expect(registry.dispatch).not.toHaveBeenCalled();

    await act(async () => {
      await chat.loadHistory(sid);
      socket.receive(turnFrame("delta", 30, { text: "duplicate at cut" }));
      socket.receive({ ...turnFrame("delta", 31, { text: " segment" }), sequence: 24 });
      socket.receive(turnFrame("delta", 31, { text: "duplicate after cut" }));
      socket.receive(snapshot);
    });
    expect(chat.messages[6].text).toBe("third segment");
    expect(api.sessionStatus).not.toHaveBeenCalled();
    expect(api.history).not.toHaveBeenCalled();

    await act(async () => {
      socket.receive(turnFrame("final", 32, { text: "third segment" }));
      socket.receive(turnFrame("final", 32, { text: "duplicate final" }));
    });
    expect(chat.messages[6]).toMatchObject({
      text: "third segment",
      turnFileChanges: [{ path: "note.txt", status: "modified", added: 1 }],
    });
    expect(chat.messages.at(-1)).toMatchObject({ role: "user", text: "queued prompt", attachments: [attachment] });
    expect(afterFinal).toHaveBeenCalledTimes(2);
    expect(chat.pendingQueue).toEqual([]);
    expect(socket.sent.filter((payload) => payload.query === "queued prompt")).toHaveLength(1);
  });

  it("keeps the visible prefix and polls when a snapshot contains a foreign active frame", async () => {
    vi.mocked(api.sessionStatus).mockResolvedValue(status("running"));
    const socket = TestWebSocket.instances[0];
    await act(async () => chat.send("current prompt", sid, "agent", workspace));
    await act(async () => socket.receive(frame("delta", 1, { text: "local prefix" })));
    await act(async () => socket.receive(frame("gateway_resync", 3, {
      reason: "cursor_expired",
      floor: 2,
      turn_snapshot: {
        request_id: "active",
        started_at: 1_700_000_000,
        history: { items: [{ role: "user", content: "current prompt" }], has_more: false, next_before: null },
        frames: [{ ...frame("delta", 2, { text: "other request" }), request_id: "foreign" }],
      },
    })));
    expect(chat.messages.map((message) => message.text)).toEqual(["current prompt", "local prefix"]);
    expect(chat.recoveryNotice).toContain("回复仍在生成");
    expect(api.sessionStatus).toHaveBeenCalledTimes(1);
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

describe("Web turn outcomes", () => {
  it("keeps a partial reply and its authoritative result when history replaces the live view", async () => {
    const ws = TestWebSocket.instances[0];
    await act(async () => chat.send("show a partial result", sid, "agent", workspace));
    const requestId = chat.activeRequestId!;
    const reply = "The prefix was generated";
    const outcome = { status: "partial", reason: "output_limit", retryable: true };
    await act(async () => {
      ws.receive({ ...frame("delta", 1, { text: reply }), request_id: requestId });
      ws.receive({ ...frame("final", 2, { text: reply, outcome }), request_id: requestId });
    });
    const expectPartial = () => {
      expect(chat.messages.filter((message) => message.text === reply)).toHaveLength(1);
      expect(chat.messages.filter((message) => message.turnOutcome)).toEqual([
        expect.objectContaining({ requestId, turnOutcome: "partial", text: expect.stringContaining("回复未完成") }),
      ]);
    };
    expectPartial();

    vi.mocked(api.history).mockResolvedValueOnce([
      { role: "user", content: "show a partial result", request_id: requestId },
      { role: "assistant", content: reply, request_id: requestId, outcome },
    ]);
    vi.mocked(api.sessionStatus).mockResolvedValueOnce({ ...status("idle"), last_status: "partial", last_outcome: { ...outcome, request_id: requestId } });
    await act(async () => chat.loadHistory(sid));
    expectPartial();
  });

  it("keeps an earlier failure with its request while a later successful reply adds no notice", async () => {
    const ws = TestWebSocket.instances[0];
    const failure = { status: "failed", message: "Model rejected the credentials" };
    await act(async () => chat.send("first request", sid, "agent", workspace));
    const oldRequest = chat.activeRequestId!;
    await act(async () => {
      ws.receive({ ...frame("error", 1, { message: failure.message, outcome: failure }), request_id: oldRequest });
    });
    expect(chat.messages.filter((message) => message.text.includes(failure.message))).toHaveLength(1);

    await act(async () => chat.send("second request", sid, "agent", workspace));
    const nextRequest = chat.activeRequestId!;
    await act(async () => {
      ws.receive({ ...frame("final", 2, { text: "Normal reply", outcome: { status: "completed" } }), request_id: nextRequest });
    });

    vi.mocked(api.history).mockResolvedValueOnce([
      { role: "user", content: "first request", request_id: oldRequest, outcome: failure },
      { role: "user", content: "second request", request_id: nextRequest },
      { role: "assistant", content: "Normal reply", request_id: nextRequest, outcome: { status: "completed" } },
    ]);
    vi.mocked(api.sessionStatus).mockResolvedValueOnce({ ...status("idle"), last_status: "completed" });
    await act(async () => chat.loadHistory(sid));

    expect(chat.messages.map((message) => [message.text, message.requestId, message.turnOutcome])).toEqual([
      ["first request", oldRequest, undefined],
      ["本轮执行失败。Model rejected the credentials", oldRequest, "failed"],
      ["second request", nextRequest, undefined],
      ["Normal reply", nextRequest, undefined],
    ]);
  });
});

describe("Web request-scoped stop", () => {
  it("binds a pre-token stop to the clicked round, preserves queued messages and keeps an unconfirmed tool visible", async () => {
    const ws = TestWebSocket.instances[0];
    const deliver = (kind: Chunk["kind"], request_id: string, body: Chunk["body"], terminal = false) =>
      ws.receive({ kind, request_id, session_id: sid, body, sequence: 0, is_final: terminal });
    await act(async () => {
      deliver("send_receipt_capability", "", { version: 1, request_stop: 1 });
      chat.send("one", sid, "agent", workspace);
    });
    const first = chat.activeRequestId!;
    expect(first).toBeTruthy();
    expect(ws.sent.at(-1)).toMatchObject({ query: "one", request_id: first });
    const stopFirst = () => chat.stop(sid, first);
    await act(async () => { chat.send("two", sid, "agent", workspace); stopFirst(); });
    expect(ws.sent.at(-1)).toEqual({ action: "stop", session_id: sid, request_id: first });
    await act(async () => {
      deliver("status", "control", { stop: { request_id: first, state: "requested" } });
      deliver("tool", first, { phase: "error", tool_call_id: "t", result_metadata: { code: "tool_exit_unconfirmed", details: { exit_confirmed: false } } });
      deliver("error", first, { message: "cancelled", outcome: { status: "stopped" } }, true);
    });
    const second = chat.activeRequestId!;
    expect(ws.sent.at(-1)).toMatchObject({ query: "two", request_id: second });
    expect(second).not.toBe(first);
    expect(chat.messages.some((m) => m.text.includes("实际退出尚未确认"))).toBe(true);
    await act(async () => {
      stopFirst();
      deliver("final", first, { text: "late" }, true);
      deliver("status", "control", { stop: { request_id: first, state: "not_active" } });
    });
    expect(chat.busy).toBe(true);
    expect(chat.activeRequestId).toBe(second);
    expect(chat.messages.some((m) => m.text === "late")).toBe(false);
    expect(ws.sent.filter((f) => f.action === "stop")).toHaveLength(1);
  });

  it("does not send a wildcard to an older or disconnected gateway", async () => {
    const ws = TestWebSocket.instances[0];
    await act(async () => chat.send("one", sid, "agent", workspace));
    const first = chat.activeRequestId!;
    await act(async () => chat.stop(sid, first));
    expect(ws.sent.some((f) => f.action === "stop")).toBe(false);
    expect(chat.messages.some((m) => m.text.includes("尚未确认停止"))).toBe(true);
    expect(chat.busy).toBe(true);
  });
});
