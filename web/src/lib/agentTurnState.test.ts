import { describe, expect, it } from "vitest";
import {
  buildAgentTurnState,
  emptyAgentTurnState,
  mergeStreamingText,
  reduceAgentTurnEvent,
} from "./agentTurnState";

describe("reduceAgentTurnEvent", () => {
  it("merges streaming thinking and tool lifecycle updates", () => {
    let state = emptyAgentTurnState();
    state = reduceAgentTurnEvent(state, {
      type: "thinking",
      id: "thinking",
      content: "先检查",
      done: false,
    });
    state = reduceAgentTurnEvent(state, {
      type: "thinking",
      id: "thinking",
      content: "检查项目结构",
      done: false,
    });
    state = reduceAgentTurnEvent(state, {
      type: "tool",
      id: "tool_1",
      tool: { toolCallId: "tool_1", name: "Read", status: "running", startedAt: 10 },
    });
    state = reduceAgentTurnEvent(state, {
      type: "tool",
      id: "tool_1",
      tool: { toolCallId: "tool_1", name: "Read", status: "done", result: "ok", startedAt: 10 },
    });

    expect(state.processItems).toHaveLength(2);
    expect(state.processItems[0]).toMatchObject({ kind: "thinking", content: "先检查项目结构" });
    expect(state.processItems[1]).toMatchObject({ kind: "tool", tool: { status: "done", result: "ok" } });
    expect(state.toolCount).toBe(1);
  });
});

describe("buildAgentTurnState", () => {
  it("projects the same persisted Team message into timeline and response data", () => {
    const state = buildAgentTurnState([{
      id: "team_1",
      role: "team_internal",
      text: "节点完成",
      thinking: "核对结果",
      toolCalls: [
        { toolCallId: "t1", name: "terminal", status: "running", startedAt: 0 },
        { toolCallId: "t1", name: "terminal", status: "done", result: "ok", startedAt: 0 },
      ],
    }], false);

    expect(state.responses[0].content).toBe("节点完成");
    expect(state.hasThinking).toBe(true);
    expect(state.toolCount).toBe(1);
    expect(state.commandCount).toBe(1);
  });
});

describe("mergeStreamingText", () => {
  it("accepts cumulative snapshots without duplicating content", () => {
    expect(mergeStreamingText("正在检查", "正在检查项目")).toBe("正在检查项目");
  });
});
