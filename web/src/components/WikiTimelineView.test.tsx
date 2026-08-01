import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import WikiTimelineView from "./WikiTimelineView";
import type { WikiPage } from "../types";

function makePage(id: string, title: string, updatedAt: number): WikiPage {
  return {
    id,
    page_type: "topic",
    title,
    content: "内容",
    file_path: "",
    sources: [],
    related: [],
    tags: [],
    created_at: updatedAt,
    updated_at: updatedAt,
    aliases: [],
  };
}

describe("WikiTimelineView", () => {
  const now = new Date();
  const todayTs = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 10).getTime() / 1000;
  const yesterdayTs = todayTs - 86400;

  it("groups pages by date", () => {
    const pages: WikiPage[] = [
      makePage("p1", "今日页面", todayTs),
      makePage("p2", "昨日页面", yesterdayTs),
    ];

    const html = renderToStaticMarkup(
      <WikiTimelineView
        pages={pages}
        selectedId={null}
        selectedIds={new Set()}
        highlightedIds={new Set()}
        onSelectPage={vi.fn()}
        onToggleSelect={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(html).toContain("今天");
    expect(html).toContain("昨天");
    expect(html).toContain("今日页面");
    expect(html).toContain("昨日页面");
  });

  it("shows empty hint when no pages", () => {
    const html = renderToStaticMarkup(
      <WikiTimelineView
        pages={[]}
        selectedId={null}
        selectedIds={new Set()}
        highlightedIds={new Set()}
        onSelectPage={vi.fn()}
        onToggleSelect={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(html).toContain("还没有 Wiki 页面");
  });
});
