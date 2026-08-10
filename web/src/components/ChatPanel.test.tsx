import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import ChatPanel from "./ChatPanel";

const noop = () => {};

describe("ChatPanel team empty session", () => {
  it("keeps an empty Team session in chat without the single-agent empty state", () => {
    const html = renderToStaticMarkup(
      <ChatPanel
        messages={[]}
        busy={false}
        pendingQueue={[]}
        config={null}
        attachments={[]}
        onSend={noop}
        onAttachmentsChange={noop}
        isTeamSession
      />,
    );

    expect(html).not.toContain("Claw Your Ideas Into Reality");
    expect(html).not.toContain("开始一段对话");
    expect(html).not.toContain("单 Agent 直接执行任务");
  });
});
