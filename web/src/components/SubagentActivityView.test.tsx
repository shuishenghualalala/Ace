import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SubagentActivityPanel } from "./SubagentActivityView";

describe("SubagentActivityPanel", () => {
  it("renders a compact read-only activity list", () => {
    const html = renderToStaticMarkup(
      <SubagentActivityPanel
        activities={[
          { id: "child-1", label: "Explore", provider: "inprocess-spawn", status: "running" },
          { id: "child-2", label: "Verification", status: "completed" },
        ]}
      />,
    );

    expect(html).toContain("子智能体活动");
    expect(html).toContain("Explore");
    expect(html).toContain("运行中");
    expect(html).toContain("Verification");
    expect(html).toContain("已完成");
    expect(html).toContain("subagent-activity__row--running");
    expect(html).toContain("aria-label=\"子智能体活动\"");
  });

  it("does not add layout when the current session has no subagents", () => {
    expect(renderToStaticMarkup(<SubagentActivityPanel activities={[]} />)).toBe("");
  });

  it("exposes continuable message and interrupt actions", () => {
    const html = renderToStaticMarkup(
      <SubagentActivityPanel
        activities={[{ id: "child-1", label: "Worker", status: "running" }]}
        expandedId="child-1"
        draft="继续检查"
        onToggleMessage={() => undefined}
        onDraftChange={() => undefined}
        onSendMessage={() => undefined}
        onInterrupt={() => undefined}
      />,
    );

    expect(html).toContain("发送");
    expect(html).toContain("中断");
    expect(html).toContain("继续检查");
    expect(html).toContain("发送消息给Worker");
  });
});
