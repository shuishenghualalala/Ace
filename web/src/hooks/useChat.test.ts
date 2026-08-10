import { describe, expect, it } from "vitest";
import { mergeStreamingText } from "../lib/agentTurnState";
import {
  applyAssistantTextDelta,
  applyOrderedDelta,
  applyToolChunk,
  isTeamRuntimeStatus,
  normalizeWikiCardPages,
  resolveFinalText,
  type AssistantTextDeltaAccumulator,
  type DeltaAccumulator,
} from "./useChat";

describe("mergeStreamingText", () => {
  it("appends streaming ACP thinking chunks instead of keeping only punctuation", () => {
    let thinking = mergeStreamingText(undefined, "我先检查");
    thinking = mergeStreamingText(thinking, "项目结构");
    thinking = mergeStreamingText(thinking, "。");
    expect(thinking).toBe("我先检查项目结构。");
  });

  it("deduplicates overlapping or cumulative chunks", () => {
    expect(mergeStreamingText("先检查项目", "项目结构")).toBe("先检查项目结构");
  });
});

describe("resolveFinalText", () => {
  it("累积文本已包含 final 文本 → 保留累积全文", () => {
    const acc = "调研报告正文\n\n总结：以上是完整报告";
    const final = "总结：以上是完整报告";
    expect(resolveFinalText(acc, final)).toBe(acc);
  });

  it("多轮累积以 final 文本结尾 → 保留累积", () => {
    const acc = "第一轮说明\n\n第二轮总结";
    const final = "第二轮总结";
    expect(resolveFinalText(acc, final)).toBe(acc);
  });

  it("final 文本比累积更完整 → 用 final 覆盖（丢帧兜底）", () => {
    const acc = "前半截";
    const final = "前半截后半截";
    expect(resolveFinalText(acc, final)).toBe(final);
  });

  it("累积和 final 无包含关系 → 保守保留累积（避免 final 冲掉前面轮次）", () => {
    const acc = "我先查一下资料。";
    const final = "总结";
    expect(resolveFinalText(acc, final)).toBe(acc);
  });

  it("final 与累积长度接近但不包含 → 用 final 纠偏乱序累积", () => {
    expect(resolveFinalText("lohel", "hello")).toBe("hello");
  });

  it("空 final → 保留累积（用户停止）", () => {
    const acc = "已生成的半截内容";
    expect(resolveFinalText(acc, "")).toBe(acc);
  });

  it("累积为空 → 采用 final 文本", () => {
    const final = "完整回复";
    expect(resolveFinalText("", final)).toBe(final);
  });

  it("忽略首尾空白差异", () => {
    const acc = "答案：A\n\n";
    const final = "答案：A";
    expect(resolveFinalText(acc, final)).toBe(acc);
  });
});

describe("isTeamRuntimeStatus", () => {
  it("treats direct leader notices as team runtime status, not chat messages", () => {
    expect(isTeamRuntimeStatus("简单消息由 Leader 直接回复…")).toBe(true);
  });
});

describe("normalizeWikiCardPages", () => {
  it("accepts current pages payload and legacy cards payload", () => {
    const page = { id: "p1", title: "页面一" };

    expect(normalizeWikiCardPages({ pages: [page] })[0]).toMatchObject(page);
    expect(normalizeWikiCardPages({ cards: [page] })[0]).toMatchObject(page);
    expect(normalizeWikiCardPages({ cards: "bad" })).toEqual([]);
  });
});

describe("applyOrderedDelta", () => {
  const empty = (): DeltaAccumulator => ({ deltaSpans: [], legacyDeltaText: "" });

  it("按 delta_start/delta_end 排序重建文本", () => {
    const state = empty();
    expect(applyOrderedDelta(state, { body: { delta_start: 2, delta_end: 2 }, sequence: 2 }, "world")).toBe("world");
    expect(applyOrderedDelta(state, { body: { delta_start: 1, delta_end: 1 }, sequence: 1 }, "hello ")).toBe("hello world");
  });

  it("合并帧覆盖已收到的单帧，避免重复文本", () => {
    const state = empty();
    expect(applyOrderedDelta(state, { body: { delta_start: 1, delta_end: 1 }, sequence: 1 }, "hel")).toBe("hel");
    expect(applyOrderedDelta(state, { body: { delta_start: 1, delta_end: 2 }, sequence: 2 }, "hello ")).toBe("hello ");
    expect(applyOrderedDelta(state, { body: { delta_start: 3, delta_end: 3 }, sequence: 3 }, "world")).toBe("hello world");
  });

  it("重复帧不重复追加", () => {
    const state = empty();
    expect(applyOrderedDelta(state, { body: { delta_start: 1, delta_end: 2 }, sequence: 2 }, "hello")).toBe("hello");
    expect(applyOrderedDelta(state, { body: { delta_start: 1, delta_end: 1 }, sequence: 1 }, "he")).toBe("hello");
  });

  it("旧帧无序号时保持到达顺序追加", () => {
    const state = empty();
    expect(applyOrderedDelta(state, { body: {}, sequence: 0 }, "a")).toBe("a");
    expect(applyOrderedDelta(state, { body: {}, sequence: 0 }, "b")).toBe("ab");
  });
});

describe("applyAssistantTextDelta", () => {
  it("工具结果后的下一段文本重置 delta 累积，用于新 assistant 分段", () => {
    const state: AssistantTextDeltaAccumulator = {
      deltaSpans: [],
      legacyDeltaText: "",
      awaitingAssistantAfterTool: false,
    };

    expect(applyAssistantTextDelta(
      state,
      { body: { delta_start: 1, delta_end: 1 }, sequence: 1 },
      "好的，我来为你做一个关于",
    )).toBe("好的，我来为你做一个关于");

    // 模拟 tool result 后进入 awaitingAssistantAfterTool；下一段文本应成为新消息的起点，
    // 否则中间过程回复会被合并到正文末尾，无法回到时间线里。
    state.awaitingAssistantAfterTool = true;

    expect(applyAssistantTextDelta(
      state,
      { body: { delta_start: 4, delta_end: 4 }, sequence: 4 },
      "世界杯的通用模板PPT。",
    )).toBe("世界杯的通用模板PPT。");
    expect(state.awaitingAssistantAfterTool).toBe(false);
  });
});

describe("applyToolChunk", () => {
  const id = () => "tool-1";

  it("generating 阶段创建 generating 状态条目", () => {
    const map = new Map();
    const now = 1000;
    const next = applyToolChunk(map, { phase: "generating", tool_call_id: "t1", name: "file_write", args: '{"path":"a.txt"}' }, now, id);
    const t = next.get("t1")!;
    expect(t.status).toBe("generating");
    expect(t.name).toBe("file_write");
    expect(t.startedAt).toBe(now);
    expect(t.duration).toBeUndefined();
  });

  it("generating → start 保留 startedAt 并切为 running", () => {
    let map = new Map();
    map = applyToolChunk(map, { phase: "generating", tool_call_id: "t1" }, 1000, id);
    map = applyToolChunk(map, { phase: "start", tool_call_id: "t1", name: "file_write" }, 1500, id);
    const t = map.get("t1")!;
    expect(t.status).toBe("running");
    expect(t.startedAt).toBe(1000);
  });

  it("start → result 计算完整 duration", () => {
    let map = new Map();
    map = applyToolChunk(map, { phase: "start", tool_call_id: "t1", name: "file_write" }, 1000, id);
    map = applyToolChunk(map, { phase: "result", tool_call_id: "t1", detail: "ok" }, 2500, id);
    const t = map.get("t1")!;
    expect(t.status).toBe("done");
    expect(t.duration).toBe(1500);
    expect(t.result).toBe("ok");
  });

  it("result 阶段若 toolMap 不存在，创建 done 且 duration 为 0", () => {
    const map = applyToolChunk(new Map(), { phase: "result", tool_call_id: "t2", name: "terminal", detail: "x" }, 1000, id);
    const t = map.get("t2")!;
    expect(t.status).toBe("done");
    expect(t.duration).toBe(0);
  });

  it("error 阶段状态为 error 并保留已有 args", () => {
    let map = new Map();
    map = applyToolChunk(map, { phase: "generating", tool_call_id: "t1", args: '{"x":1}' }, 1000, id);
    map = applyToolChunk(map, { phase: "error", tool_call_id: "t1", detail: "fail" }, 1200, id);
    const t = map.get("t1")!;
    expect(t.status).toBe("error");
    expect(t.args).toBe('{"x":1}');
    expect(t.duration).toBe(200);
  });
});
