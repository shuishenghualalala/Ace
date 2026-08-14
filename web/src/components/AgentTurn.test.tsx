import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { UiMessage } from "../types";
import AgentTurn from "./AgentTurn";

function renderHtml(messages: UiMessage[], isStreaming = false): string {
  return renderToStaticMarkup(
    <AgentTurn messages={messages} isStreaming={isStreaming} />,
  );
}

function extractTimeline(html: string): string {
  const match = html.match(
    /<div class="process-timeline">([\s\S]*?)<\/div>\s*<\/div>\s*<\/details>/,
  );
  return match?.[1] ?? "";
}

function extractBodyText(html: string): string {
  // 移除时间线部分，取 body 里剩下的 msg__text
  return html.replace(
    /<div class="process-timeline">[\s\S]*?<\/div>\s*<\/div>\s*<\/details>/,
    "",
  );
}

describe("AgentTurn", () => {
  it("单条 assistant text 渲染为正文，不进入时间线", () => {
    const messages: UiMessage[] = [
      { id: "a1", role: "assistant", text: "这是最终回复。" },
    ];
    const html = renderHtml(messages);
    expect(html).toContain("这是最终回复。");
    expect(html).not.toContain("process-timeline__item--narration");
  });

  it("多条 assistant text 时，非最后一条作为 narration 进入时间线，最后一条留在正文", () => {
    const messages: UiMessage[] = [
      { id: "a1", role: "assistant", text: "我先查一下。" },
      {
        id: "a2",
        role: "assistant",
        text: "",
        toolCalls: [{ toolCallId: "tc1", name: "file_read", args: "{}", status: "done", startedAt: 0 }],
      },
      { id: "a3", role: "assistant", text: "这是最终回复。" },
    ];
    const html = renderHtml(messages);
    const timeline = extractTimeline(html);
    const body = extractBodyText(html);

    expect(timeline).toContain("我先查一下。");
    expect(timeline).toContain("process-timeline__item--narration");

    expect(body).toContain("这是最终回复。");
    expect(timeline).not.toContain("这是最终回复。");
  });

  it("流式中当前最后一条 assistant text 仍渲染为正文", () => {
    const messages: UiMessage[] = [
      { id: "a1", role: "assistant", text: "中间步骤说明。" },
      { id: "a2", role: "assistant", text: "正在输出最终回复…" },
    ];
    const html = renderHtml(messages, true);
    const timeline = extractTimeline(html);
    const body = extractBodyText(html);

    expect(timeline).toContain("中间步骤说明。");
    expect(body).toContain("正在输出最终回复…");
  });

  it("外部 Agent 回合复用文件改动卡并禁用已删除文件", () => {
    const html = renderHtml([{
      id: "external-1",
      role: "assistant",
      text: "修改完成。",
      turnFileChanges: [
        { path: "/work/app.ts", name: "app.ts", added: 12, removed: 2, status: "modified" },
        { path: "/work/old.ts", name: "old.ts", added: 0, removed: 4, status: "deleted" },
      ],
    }]);

    expect(html).toContain("已编辑 2 个文件");
    expect(html).toContain("app.ts");
    expect(html).toContain("old.ts");
    expect(html).toContain("修改");
    expect(html).toContain("删除");
    expect(html).toContain('data-file-status="deleted"');
    expect(html).toContain("disabled");
  });

});
