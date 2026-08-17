import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { UiMessage } from "../types";
import TeamAgentTurnBubble from "./TeamAgentTurnBubble";

function renderMessage(overrides: Partial<UiMessage> = {}): string {
  const message: UiMessage = {
    id: "mention-answer",
    role: "team_internal",
    text: "我当前使用 K3 模型。",
    agentId: "coder",
    agentName: "coder",
    communicationKind: "user_mention_answer",
    communicationStatus: "answered",
    ...overrides,
  };
  return renderToStaticMarkup(
    <TeamAgentTurnBubble message={message} onRetryMention={() => {}} onCancelMention={() => {}} />,
  );
}

describe("TeamAgentTurnBubble communication status", () => {
  it("shows the answered status for a direct user Agent mention", () => {
    expect(renderMessage()).toContain("已回答");
  });

  it("shows retry for a terminal direct mention when the original prompt is persisted", () => {
    expect(renderMessage({ communicationStatus: "failed", communicationRequestText: "你使用的是什么模型？" }))
      .toContain(">重试<");
  });

  it("does not show retry without the persisted prompt", () => {
    expect(renderMessage({ communicationStatus: "failed" })).not.toContain("重试");
  });

  it("keeps a terminal expired mention actionable and visible", () => {
    const html = renderMessage({
      text: "coder 的回答已超时。",
      communicationStatus: "expired",
      communicationRequestText: "@coder 你现在用什么模型？",
    });
    expect(html).toContain("已超时");
    expect(html).toContain(">重试<");
  });

  it("does not show a communication badge for ordinary Team messages", () => {
    expect(renderMessage({ communicationKind: "team_submit", communicationStatus: "answered" }))
      .not.toContain("已回答");
  });

  it("shows the structured recipient in a Team communication title", () => {
    expect(renderMessage({
      agentRole: "向 kk 征询执行意见与状态",
      mentionFrom: "kk",
      mentionTo: ["leader"],
      mentionIntent: "submit",
    })).toContain("向 leader 征询执行意见与状态");
  });
});
