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
  return renderToStaticMarkup(<TeamAgentTurnBubble message={message} />);
}

describe("TeamAgentTurnBubble communication status", () => {
  it("shows the answered status for a direct user Agent mention", () => {
    expect(renderMessage()).toContain("已回答");
  });

  it("does not show a communication badge for ordinary Team messages", () => {
    expect(renderMessage({ communicationKind: "team_submit", communicationStatus: "answered" }))
      .not.toContain("已回答");
  });
});
