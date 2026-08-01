import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AgentProcessTimeline from "./AgentProcessTimeline";

describe("AgentProcessTimeline", () => {
  it("preprocesses streaming narration markdown", () => {
    const content = [
      "1. **触发条件** — 记忆复盘",
      "2. **F2. Fork 机制 — 复制子 Agent 独立复盘",
    ].join("\n");
    const html = renderToStaticMarkup(
      <AgentProcessTimeline
        isStreaming
        items={[{ kind: "narration", id: "n1", content }]}
      />,
    );
    expect(html).toContain("<strong>F2. Fork 机制");
    expect(html).not.toContain("**F2. Fork");
  });

  it("renders generating tool labels as active process items", () => {
    const html = renderToStaticMarkup(
      <AgentProcessTimeline
        items={[{
          kind: "tool",
          id: "w1",
          tool: {
            toolCallId: "w1",
            name: "file_write",
            uiLabel: "正在写入 /tmp/demo.html",
            args: JSON.stringify({ path: "/tmp/demo.html" }),
            status: "generating",
            startedAt: 0,
          },
        }]}
      />,
    );
    expect(html).toContain("正在写入 /tmp/demo.html");
    expect(html).toContain("process-timeline__icon--running");
  });
});
