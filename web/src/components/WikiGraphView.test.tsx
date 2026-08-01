import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import WikiGraphView from "./WikiGraphView";
import type { WikiPage } from "../types";

vi.mock("../api", () => ({
  api: {
    wikiGraph: vi.fn().mockResolvedValue({
      ok: true,
      graph: {
        nodes: [
          { id: "p1", title: "部署规范", type: "topic" },
          { id: "p2", title: "CI/CD", type: "entity" },
        ],
        edges: [{ source: "p1", target: "p2", relation: "related" }],
      },
    }),
  },
}));

describe("WikiGraphView", () => {
  it("renders loading state synchronously", () => {
    const pages: WikiPage[] = [];
    const html = renderToStaticMarkup(
      <WikiGraphView kbId="default" pages={pages} selectedId={null} onSelectPage={vi.fn()} />,
    );
    expect(html).toContain("加载图谱中");
  });
});
