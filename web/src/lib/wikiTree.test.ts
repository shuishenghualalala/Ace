import { describe, expect, it } from "vitest";

import type { WikiPage } from "../types";
import { buildFileTree, vaultDocumentLabel, vaultFolderLabel } from "./wikiTree";

function page(id: string, filePath: string): WikiPage {
  return {
    id,
    page_type: filePath.includes("/sources/") ? "source" : "entity",
    title: id,
    file_path: filePath,
    sources: [],
    related: [],
    tags: [],
    created_at: 0,
    updated_at: 0,
    aliases: [],
  };
}

describe("buildFileTree", () => {
  it("只展示 Wiki、来源分类和根文档", () => {
    const root = buildFileTree([
      page("pdf", "wiki/sources/pdfs/pdf.md"),
      page("entity", "wiki/entities/entity.md"),
      page("raw", "raw/pdfs/raw.md"),
      page("internal", ".crew/cache/internal.md"),
    ]);

    expect(root.children.map((node) => (
      node.kind === "page" ? node.page.title : node.name
    ))).toEqual([
      "wiki",
      "Home.md",
      "index.md",
    ]);
    const wiki = root.children[0];
    expect(wiki.kind).toBe("folder");
    if (wiki.kind !== "folder") return;
    const sources = wiki.children.find(
      (node) => node.kind === "folder" && node.name === "sources",
    );
    expect(sources?.kind).toBe("folder");
    if (sources?.kind !== "folder") return;
    expect(sources.children.map((node) => (
      node.kind === "page" ? node.page.title : node.name
    ))).toEqual([
      "articles",
      "pdfs",
      "words",
      "excels",
      "ppts",
      "notes",
      "sessions",
      "images",
      "videos",
      "assets",
    ]);
  });

  it("为文件树目录和根文档提供中文显示名", () => {
    expect(vaultFolderLabel("wiki", "wiki")).toBe("知识库");
    expect(vaultFolderLabel("wiki/entities", "entities")).toBe("关键词");
    expect(vaultFolderLabel("wiki/topics", "topics")).toBe("话题");
    expect(vaultFolderLabel("wiki/sources/articles", "articles")).toBe("链接与网页");
    expect(vaultFolderLabel("wiki/sources/assets", "assets")).toBe("其他附件");
    expect(vaultDocumentLabel("Home.md")).toBe("知识库概览");
    expect(vaultDocumentLabel("index.md")).toBe("知识导航");
  });
});
