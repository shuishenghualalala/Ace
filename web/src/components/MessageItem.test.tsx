import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { UiMessage } from "../types";
import MessageItem from "./MessageItem";

describe("MessageItem team process timeline", () => {
  it("dedupes team tool start/done pairs and renders an agent-style process label", () => {
    const msg: UiMessage = {
      id: "team_1",
      role: "team_internal",
      text: "已完成团队消息读取。",
      agentId: "agent_hh",
      agentName: "hh",
      agentRole: "leader",
      toolCalls: [
        {
          toolCallId: "tool_read",
          name: "team_read_messages",
          uiLabel: "读取团队消息",
          args: "{\"limit\":5}",
          status: "running",
          startedAt: 0,
        },
        {
          toolCallId: "tool_read",
          name: "team_read_messages",
          uiLabel: "读取团队消息",
          args: "{\"limit\":5}",
          result: "{\"ok\":true}",
          status: "done",
          startedAt: 0,
          duration: 120,
        },
        {
          toolCallId: "tool_memory",
          name: "memory",
          uiLabel: "记忆 search",
          args: "{\"query\":\"成员\"}",
          status: "running",
          startedAt: 0,
        },
        {
          toolCallId: "tool_memory",
          name: "memory",
          uiLabel: "记忆 search",
          args: "{\"query\":\"成员\"}",
          result: "{\"success\":true}",
          status: "done",
          startedAt: 0,
          duration: 80,
        },
      ],
    };

    const html = renderToStaticMarkup(<MessageItem msg={msg} />);

    expect(html).toContain("使用了 2 个工具 · 已处理");
    expect(html).not.toContain("使用了 4 个工具");
    expect(html).toContain("process-timeline");
    expect(html).toContain("读取团队消息");
    expect(html).toContain("记忆 search");
  });

  it("shows real timing and localized ACP tool titles in team process bubbles", () => {
    const msg: UiMessage = {
      id: "team_timed",
      role: "team_internal",
      text: "已完成。",
      agentId: "kk",
      agentName: "kk",
      turnStartedAt: new Date("2026-07-10T04:00:07Z").getTime(),
      turnDurationMs: 267000,
      timestamp: new Date("2026-07-10T04:04:34Z").getTime(),
      toolCalls: [{
        toolCallId: "tool_write",
        name: "Write",
        args: "{\"path\":\"game.js\"}",
        result: "Wrote 123 bytes",
        status: "done",
        startedAt: 0,
        duration: 120,
      }],
    };

    const html = renderToStaticMarkup(<MessageItem msg={msg} />);

    expect(html).toContain("使用了 1 个工具 · 已处理 4m 27s");
    expect(html).toMatch(/\d{2}:\d{2}:\d{2}/);
    expect(html).not.toMatch(/\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2}/);
    expect(html).toContain("写入 game.js");
  });

  it("highlights plain mention fallback after Chinese punctuation", () => {
    const msg: UiMessage = {
      id: "team_mention",
      role: "team_internal",
      text: "审阅未通过，@kk 请继续修订。",
      agentId: "leader",
      agentName: "leader",
      isLeader: true,
    };

    const html = renderToStaticMarkup(<MessageItem msg={msg} />);

    expect(html).toContain("<strong>@kk</strong>");
  });

  it("keeps a live team timeline open and marks thinking as running", () => {
    const msg: UiMessage = {
      id: "team_live",
      role: "team_internal",
      text: "正在检查",
      eventType: "team_stream",
      agentId: "kk",
      thinking: "先读取当前文件。",
      toolCalls: [{
        toolCallId: "tool_1",
        name: "terminal",
        uiLabel: "运行命令",
        status: "running",
        startedAt: 0,
      }],
    };

    const html = renderToStaticMarkup(<MessageItem msg={msg} isStreaming />);

    expect(html).toContain("<details");
    expect(html).toContain("open");
    expect(html).toContain("处理中");
    expect(html).toContain("思考中");
  });

  it("renders crew::builtin team messages with the built-in Crew identity", () => {
    const msg: UiMessage = {
      id: "team_crew",
      role: "team_internal",
      text: "测试方案已完成。",
      agentId: "crew::builtin",
      agentName: "Crew",
      agentRole: "测试方案：小游戏",
    };

    const html = renderToStaticMarkup(<MessageItem msg={msg} />);

    expect(html).toContain("msg-agent-logo");
    expect(html).toContain("<strong>Crew</strong>");
    expect(html).not.toContain("agent-avatar--message");
  });
});
