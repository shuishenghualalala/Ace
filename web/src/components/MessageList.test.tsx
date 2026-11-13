import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { FollowupQuestion, UiMessage } from "../types";
import MessageList from "./MessageList";

describe("MessageList followup origin", () => {
  it("renders the asking team agent identity on followup cards", () => {
    const followupQuestion: FollowupQuestion = {
      question_id: "q1",
      title: "Leader 选择团队执行模式",
      origin: {
        type: "team",
        agent_id: "leader",
        agent_name: "Leader",
      },
      questions: [{
        id: "team_execution_tier",
        question: "请选择执行模式",
        options: [{
          label: "standard",
          value: "standard",
          description: "默认模式：开发 2048 小游戏并完成验证。",
        }],
        multiSelect: false,
      }],
    };

    const html = renderToStaticMarkup(
      <MessageList
        messages={[]}
        busy={false}
        followupQuestion={followupQuestion}
        teamMembers={[{
          agentId: "leader",
          name: "hh",
          role: "leader",
          isLeader: true,
          tone: 2,
        }]}
      />,
    );

    expect(html).toContain("agent-avatar--message");
    expect(html).toContain("agent-tone-2");
    expect(html).toContain("hh 正在询问");
    expect(html).toContain("standard");
    expect(html).toContain("默认模式：开发 2048 小游戏并完成验证。");
  });
});

describe("MessageList agent bubble routing", () => {
  it("renders team internal messages with the member bubble", () => {
    const html = renderToStaticMarkup(
      <MessageList
        messages={[
          { id: "team_1", role: "team_internal", text: "@kk 开始执行", agentId: "kk" },
          { id: "team_2", role: "team_internal", text: "kk 已完成", agentId: "kk" },
        ]}
        busy={false}
        currentAgentLabel={{ name: "Team", provider: "team" }}
        teamMembers={[{
          agentId: "kk",
          name: "kk",
          role: "developer",
          isLeader: false,
          tone: 1,
        }]}
      />,
    );

    expect(html).toContain("<strong>@kk</strong> 开始执行");
    expect(html).toContain("kk 已完成");
    expect(html).toContain("team-internal");
    expect(html).not.toContain("Crew 正在处理");
  });

  it("routes ACP sessions through the shared AgentTurnBubble", () => {
    const messages: UiMessage[] = [{
      id: "acp_1",
      role: "assistant",
      text: "检查完成。",
      thinking: "先检查项目结构。",
      turnStartedAt: new Date("2026-07-10T10:00:00Z").getTime(),
      turnDurationMs: 8000,
      toolCalls: [{
        toolCallId: "read_1",
        name: "Read",
        args: "{\"path\":\"README.md\"}",
        status: "done",
        startedAt: 0,
      }],
    }];

    const html = renderToStaticMarkup(
      <MessageList
        messages={messages}
        busy={false}
        currentAgentLabel={{ name: "Kimi", provider: "kimi" }}
      />,
    );

    expect(html).toContain("Kimi");
    expect(html).toContain("agent-avatar--external");
    expect(html).toContain(">K</span>");
    expect(html).toContain("思考已完成");
    expect(html).toContain("读取 README.md");
    expect(html).toContain("检查完成。");
  });

  it("keeps built-in Crew sessions on the original AgentTurn", () => {
    const html = renderToStaticMarkup(
      <MessageList
        messages={[{ id: "crew_1", role: "assistant", text: "Crew 原路径" }]}
        busy={false}
        currentAgentLabel={{ name: "Crew", provider: "crew" }}
      />,
    );

    expect(html).toContain("msg-agent-logo");
    expect(html).toContain("Crew 原路径");
    expect(html).not.toContain("agent-avatar--message");
  });
});
