import type { WikiPage, WikiPageType } from "../types";

import type { WikiIconName } from "../components/WikiIcon";

export interface WikiTreeFolder {
  kind: "folder";
  name: string;
  path: string;
  children: WikiTreeNode[];
}

export interface WikiTreePage {
  kind: "page";
  page: WikiPage;
  path: string;
}

export interface WikiTreeDocument {
  kind: "document";
  name: "Home.md" | "index.md";
  path: string;
}

export type WikiTreeNode = WikiTreeFolder | WikiTreePage | WikiTreeDocument;

const VISIBLE_VAULT_FOLDERS = [
  "wiki/entities",
  "wiki/topics",
  "wiki/sources",
  "wiki/sources/articles",
  "wiki/sources/pdfs",
  "wiki/sources/words",
  "wiki/sources/excels",
  "wiki/sources/ppts",
  "wiki/sources/notes",
  "wiki/sources/sessions",
  "wiki/sources/images",
  "wiki/sources/videos",
  "wiki/sources/assets",
  "wiki/comparisons",
  "wiki/synthesis",
] as const;

const FOLDER_ORDER = new Map<string, number>(
  VISIBLE_VAULT_FOLDERS.map((path, index) => [path, index]),
);

const TYPE_ORDER: Record<WikiPageType, number> = {
  entity: 0,
  topic: 1,
  source: 2,
  comparison: 3,
  synthesis: 4,
};

export function buildFileTree(pages: WikiPage[]): WikiTreeFolder {
  const root: WikiTreeFolder = { kind: "folder", name: "", path: "", children: [] };

  const ensureFolder = (folderPath: string): WikiTreeFolder => {
    const parts = folderPath.split("/").filter(Boolean);
    let current = root;
    for (let index = 0; index < parts.length; index++) {
      const name = parts[index];
      const path = parts.slice(0, index + 1).join("/");
      let folder = current.children.find(
        (node): node is WikiTreeFolder => node.kind === "folder" && node.name === name,
      );
      if (!folder) {
        folder = { kind: "folder", name, path, children: [] };
        current.children.push(folder);
      }
      current = folder;
    }
    return current;
  };

  for (const path of VISIBLE_VAULT_FOLDERS) ensureFolder(path);
  root.children.push(
    { kind: "document", name: "Home.md", path: "Home.md" },
    { kind: "document", name: "index.md", path: "index.md" },
  );

  for (const page of pages) {
    const parts = (page.file_path || "")
      .split("/")
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts[0] !== "wiki") continue;
    let current = root;

    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const isFile = i === parts.length - 1;
      const pathSoFar = parts.slice(0, i + 1).join("/");

      if (isFile) {
        current.children.push({ kind: "page", page, path: pathSoFar });
      } else {
        let folder = current.children.find(
          (n): n is WikiTreeFolder => n.kind === "folder" && n.name === name,
        );
        if (!folder) {
          folder = { kind: "folder", name, path: pathSoFar, children: [] };
          current.children.push(folder);
        }
        current = folder;
      }
    }
  }

  sortTree(root);
  return root;
}

function sortTree(node: WikiTreeFolder): void {
  node.children.sort((a, b) => {
    if (a.kind === "folder" && b.kind !== "folder") return -1;
    if (a.kind !== "folder" && b.kind === "folder") return 1;
    if (a.kind === "folder" && b.kind === "folder") {
      const orderA = FOLDER_ORDER.get(a.path) ?? 999;
      const orderB = FOLDER_ORDER.get(b.path) ?? 999;
      if (orderA !== orderB) return orderA - orderB;
      return a.name.localeCompare(b.name, "zh-CN");
    }
    if (a.kind === "document" || b.kind === "document") {
      const leafA = a as WikiTreePage | WikiTreeDocument;
      const leafB = b as WikiTreePage | WikiTreeDocument;
      const nameA = leafA.kind === "document" ? leafA.name : leafA.page.title;
      const nameB = leafB.kind === "document" ? leafB.name : leafB.page.title;
      return nameA.localeCompare(nameB, "en");
    }
    const pa = a as WikiTreePage;
    const pb = b as WikiTreePage;
    const orderA = TYPE_ORDER[pa.page.page_type] ?? 99;
    const orderB = TYPE_ORDER[pb.page.page_type] ?? 99;
    if (orderA !== orderB) return orderA - orderB;
    return pa.page.title.localeCompare(pb.page.title, "zh-CN");
  });
  for (const child of node.children) {
    if (child.kind === "folder") sortTree(child);
  }
}

export function ancestorPaths(filePath: string): string[] {
  const parts = (filePath || "")
    .split("/")
    .map((s) => s.trim())
    .filter(Boolean);
  const paths: string[] = [];
  for (let i = 1; i < parts.length; i++) {
    paths.push(parts.slice(0, i).join("/"));
  }
  return paths;
}

export function collectPageIds(node: WikiTreeNode): string[] {
  if (node.kind === "document") return [];
  if (node.kind === "page") return [node.page.id];
  return node.children.flatMap(collectPageIds);
}

export const TYPE_META: Record<
  WikiPageType,
  { label: string; icon: WikiIconName; shortLabel: string }
> = {
  source: { label: "来源摘要", icon: "source", shortLabel: "摘要" },
  entity: { label: "关键词", icon: "entity", shortLabel: "关键词" },
  topic: { label: "话题", icon: "topic", shortLabel: "话题" },
  comparison: { label: "对比分析", icon: "topic", shortLabel: "对比" },
  synthesis: { label: "综合报告", icon: "topic", shortLabel: "综合" },
};

const VAULT_FOLDER_LABELS: Record<string, string> = {
  wiki: "知识库",
  "wiki/entities": "关键词",
  "wiki/topics": "话题",
  "wiki/sources": "来源摘要",
  "wiki/sources/articles": "链接与网页",
  "wiki/sources/pdfs": "PDF",
  "wiki/sources/words": "Word 文档",
  "wiki/sources/excels": "表格",
  "wiki/sources/ppts": "演示文稿",
  "wiki/sources/notes": "笔记",
  "wiki/sources/sessions": "会话",
  "wiki/sources/images": "图片",
  "wiki/sources/videos": "视频",
  "wiki/sources/assets": "其他附件",
  "wiki/comparisons": "对比",
  "wiki/synthesis": "综合报告",
};

export function vaultFolderLabel(path: string, fallback: string): string {
  return VAULT_FOLDER_LABELS[path] ?? fallback;
}

export function vaultDocumentLabel(name: "Home.md" | "index.md"): string {
  return name === "Home.md" ? "知识库概览" : "知识导航";
}

export interface TypeGroup {
  type: WikiPageType;
  label: string;
  icon: WikiIconName;
  pages: WikiPage[];
}

export interface TagGroup {
  tag: string;
  pages: WikiPage[];
}

export function groupByType(pages: WikiPage[]): TypeGroup[] {
  const map = new Map<WikiPageType, WikiPage[]>();
  for (const page of pages) {
    const list = map.get(page.page_type) || [];
    list.push(page);
    map.set(page.page_type, list);
  }
  const groups: TypeGroup[] = [];
  for (const type of Object.keys(TYPE_ORDER) as WikiPageType[]) {
    const list = map.get(type);
    if (!list || list.length === 0) continue;
    list.sort((a, b) => b.updated_at - a.updated_at);
    const meta = TYPE_META[type];
    groups.push({ type, label: meta.label, icon: meta.icon, pages: list });
  }
  return groups;
}

export function groupByTags(pages: WikiPage[]): TagGroup[] {
  const map = new Map<string, WikiPage[]>();
  const untagged: WikiPage[] = [];
  for (const page of pages) {
    if (page.tags.length === 0) {
      untagged.push(page);
    } else {
      for (const tag of page.tags) {
        const list = map.get(tag) || [];
        list.push(page);
        map.set(tag, list);
      }
    }
  }
  const groups: TagGroup[] = [];
  const tags = [...map.keys()].sort((a, b) => a.localeCompare(b, "zh-CN"));
  for (const tag of tags) {
    const list = map.get(tag)!;
    list.sort((a, b) => b.updated_at - a.updated_at);
    groups.push({ tag, pages: list });
  }
  if (untagged.length > 0) {
    untagged.sort((a, b) => b.updated_at - a.updated_at);
    groups.push({ tag: "未标签", pages: untagged });
  }
  return groups;
}

export function sortByUpdatedAt(pages: WikiPage[]): WikiPage[] {
  return [...pages].sort((a, b) => b.updated_at - a.updated_at);
}

/** 从页面摘要或内容中提取简短描述。 */
export function summaryOf(page: WikiPage, maxLen: number = 140): string {
  const text = (page.summary || page.content || "").trim().replace(/\s+/g, " ").slice(0, maxLen);
  return text || "（无内容摘要）";
}
