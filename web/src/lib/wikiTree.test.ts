import { describe, expect, it } from "vitest";

import type { WikiPage } from "../types";
import { buildFileTree, findPageByTitle, splitHomeQuestions, vaultDocumentLabel, vaultFolderLabel } from "./wikiTree";

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

describe("splitHomeQuestions", () => {
  const home = [
    "# 知识库概览",
    "",
    "这是导读。",
    "",
    "## 推荐问题",
    "",
    "- Wiki 里有哪些关于 Karpathy 的页面？",
    "- 人机分工原则的核心结论是什么？",
    "1. 如何上传本地文档到知识库？",
    "",
    "## 知识地图",
    "",
    "- [[人机分工原则]]",
    "",
  ].join("\n");

  it("把 Home.md 拆成 前文 / 推荐问题 / 后文", () => {
    const sections = splitHomeQuestions(home);
    expect(sections).not.toBeNull();
    expect(sections!.before).toContain("这是导读。");
    expect(sections!.before).not.toContain("推荐问题");
    expect(sections!.questions).toEqual([
      "Wiki 里有哪些关于 Karpathy 的页面？",
      "人机分工原则的核心结论是什么？",
      "如何上传本地文档到知识库？",
    ]);
    expect(sections!.after).toContain("## 知识地图");
  });

  it("没有推荐问题小节或小节为空时返回 null", () => {
    expect(splitHomeQuestions("# 概览\n\n没有小节。\n")).toBeNull();
    expect(splitHomeQuestions("## 推荐问题\n\n## 知识地图\n")).toBeNull();
  });
});

describe("findPageByTitle", () => {
  it("按标题或别名精确匹配（大小写不敏感）", () => {
    const pages = [
      { ...page("a", "wiki/entities/a.md"), title: "人机分工原则", aliases: ["人机分工", "Curation"] },
      { ...page("b", "wiki/topics/b.md"), title: "LLM Wiki 是什么" },
    ];
    expect(findPageByTitle(pages, "人机分工原则")?.id).toBe("a");
    expect(findPageByTitle(pages, " 人机分工 ")?.id).toBe("a");
    expect(findPageByTitle(pages, "curation")?.id).toBe("a");
    expect(findPageByTitle(pages, "不存在的页面")).toBeUndefined();
    expect(findPageByTitle(pages, "")).toBeUndefined();
  });
});
