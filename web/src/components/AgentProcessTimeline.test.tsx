import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AgentProcessTimeline from "./AgentProcessTimeline";

describe("AgentProcessTimeline", () => {
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
