import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import MarkdownContent from "./MarkdownContent";

function makeLongContent(blocks: number, blockSize = 500): string {
  return Array.from({ length: blocks }, (_, i) => `## 段落 ${i + 1}\n\n${"x".repeat(blockSize)}`).join("\n\n");
}

describe("MarkdownContent", () => {
  it("preprocesses incomplete markdown while streaming", () => {
    const html = renderToStaticMarkup(<MarkdownContent content="hello **world" isStreaming />);
    expect(html).toContain("<strong>world</strong>");
  });

  it("preprocesses incomplete bold marker inside ordered list items while streaming", () => {
    const content = [
      "1. **触发条件** — 记忆复盘",
      "2. **F2. Fork 机制 — 复制子 Agent 独立复盘",
    ].join("\n");
    const html = renderToStaticMarkup(<MarkdownContent content={content} isStreaming />);
    expect(html).toContain("<strong>F2. Fork 机制");
    expect(html).not.toContain("**F2. Fork");
  });

  it("does not preprocess incomplete markdown after streaming ends", () => {
    const html = renderToStaticMarkup(<MarkdownContent content="hello **world" />);
    expect(html).not.toContain("<strong>world</strong>");
  });

  it("renders full content by default", () => {
    const content = makeLongContent(10, 500);
    const html = renderToStaticMarkup(<MarkdownContent content={content} />);
    expect(html).toContain("段落 1");
    expect(html).toContain("段落 10");
    expect(html).not.toContain("md-sentinel");
  });

  it("folds long content and renders only first blocks when fold is enabled", () => {
    const content = makeLongContent(20, 500);
    const html = renderToStaticMarkup(<MarkdownContent content={content} fold initialThreshold={1000} />);
    expect(html).toContain("段落 1");
    // 总长约 10000+，首屏阈值 1000，应该只渲染前 1-2 段
    expect(html).not.toContain("段落 20");
    expect(html).toContain("md-sentinel");
  });

  it("does not fold short content even when fold is enabled", () => {
    const content = "## Hello\n\nworld";
    const html = renderToStaticMarkup(<MarkdownContent content={content} fold initialThreshold={4000} />);
    expect(html).toContain("Hello");
    expect(html).toContain("world");
    expect(html).not.toContain("md-sentinel");
  });

  it("does not fold while streaming", () => {
    const content = makeLongContent(20, 500);
    const html = renderToStaticMarkup(
      <MarkdownContent content={content} fold isStreaming initialThreshold={1000} />
    );
    expect(html).toContain("段落 1");
    expect(html).toContain("段落 20");
    expect(html).not.toContain("md-sentinel");
  });

  it("renders structured mention links as badges", () => {
    const html = renderToStaticMarkup(
      <MarkdownContent content="请 [@kk](mention://member/kk) 和 [@all](mention://team/all) 看一下" />,
    );

    expect(html).toContain("md-mention md-mention--member");
    expect(html).toContain("data-mention-id=\"kk\"");
    expect(html).toContain("md-mention md-mention--team");
    expect(html).not.toContain("href=\"mention://");
  });

  it("renders GFM table correctly when folded", () => {
    const blocks: string[] = [];
    for (let i = 0; i < 30; i++) {
      blocks.push(`## 段落 ${i + 1}\n\n${"x".repeat(400)}`);
    }
    // 在块之间插入表格：表格内部是单换行，块之间是双换行。
    blocks.splice(
      5,
      0,
      "| 名称 | 值 |\n|---|---|\n| a | 1 |\n| b | 2 |",
    );
    const content = blocks.join("\n\n");
    const html = renderToStaticMarkup(
      <MarkdownContent content={content} fold initialThreshold={3000} />
    );
    expect(html).toContain("<table>");
    expect(html).toContain("名称");
    expect(html).toContain("a</td>");
  });

  it("keeps list continuity across blocks when folded", () => {
    const content = "## 前\n\n- a\n- b\n\n## 后\n\n- c";
    const html = renderToStaticMarkup(<MarkdownContent content={content} fold initialThreshold={100} />);
    expect(html).toContain("<ul>");
    expect(html).toContain("<li>a</li>");
    expect(html).toContain("<li>b</li>");
    expect(html).toContain("<li>c</li>");
  });

  it("keeps fenced code block intact when folded", () => {
    const content = [
      "## 前",
      "",
      "```js",
      "a();",
      "```",
      "",
      "## 后",
      "",
      "after",
    ].join("\n");
    const html = renderToStaticMarkup(<MarkdownContent content={content} fold initialThreshold={30} />);
    expect(html).toContain("<code");
    expect(html).toContain("language-js");
    expect(html).toContain("a</span>();");
  });
});
