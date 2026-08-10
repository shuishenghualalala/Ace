import { describe, expect, it } from "vitest";

import type { WikiOpenTab } from "./wikiTabs";
import { closeTab, openTab, tabKey } from "./wikiTabs";

const page = (id: string): WikiOpenTab => ({ kind: "page", id });
const doc = (name: "Home.md" | "index.md"): WikiOpenTab => ({ kind: "doc", name });

describe("tabKey", () => {
  it("按 kind 生成稳定标识", () => {
    expect(tabKey(page("abc"))).toBe("page:abc");
    expect(tabKey(doc("Home.md"))).toBe("doc:Home.md");
    expect(tabKey(doc("index.md"))).toBe("doc:index.md");
  });
});

describe("openTab", () => {
  it("新 Tab 追加到末尾", () => {
    const { tabs, changed } = openTab([page("a")], page("b"));
    expect(changed).toBe(true);
    expect(tabs.map(tabKey)).toEqual(["page:a", "page:b"]);
  });

  it("已存在的 Tab 不改动顺序", () => {
    const prev = [page("a"), page("b")];
    const { tabs, changed } = openTab(prev, page("a"));
    expect(changed).toBe(false);
    expect(tabs).toBe(prev);
  });

  it("page 与 doc 按 tabKey 区分", () => {
    const { tabs } = openTab([page("Home.md")], doc("Home.md"));
    expect(tabs.map(tabKey)).toEqual(["page:Home.md", "doc:Home.md"]);
  });

  it("不修改入参数组", () => {
    const prev = [page("a")];
    openTab(prev, page("b"));
    expect(prev).toHaveLength(1);
  });
});

describe("closeTab", () => {
  it("关闭激活 Tab 时优先激活右侧相邻 Tab", () => {
    const tabs = [page("a"), page("b"), page("c")];
    const { tabs: next, nextActiveKey } = closeTab(tabs, "page:b", "page:b");
    expect(next.map(tabKey)).toEqual(["page:a", "page:c"]);
    expect(nextActiveKey).toBe("page:c");
  });

  it("关闭最后一个激活 Tab 时退到左侧相邻 Tab", () => {
    const tabs = [page("a"), page("b")];
    const { tabs: next, nextActiveKey } = closeTab(tabs, "page:b", "page:b");
    expect(next.map(tabKey)).toEqual(["page:a"]);
    expect(nextActiveKey).toBe("page:a");
  });

  it("关闭仅剩的激活 Tab 后 nextActiveKey 为 null", () => {
    const { tabs, nextActiveKey } = closeTab([doc("Home.md")], "doc:Home.md", "doc:Home.md");
    expect(tabs).toEqual([]);
    expect(nextActiveKey).toBeNull();
  });

  it("关闭非激活 Tab 时保持当前激活", () => {
    const tabs = [page("a"), page("b"), doc("index.md")];
    const { tabs: next, nextActiveKey } = closeTab(tabs, "page:a", "doc:index.md");
    expect(next.map(tabKey)).toEqual(["page:b", "doc:index.md"]);
    expect(nextActiveKey).toBe("doc:index.md");
  });

  it("关闭不存在的 Tab 原样返回", () => {
    const tabs = [page("a")];
    const { tabs: next, nextActiveKey } = closeTab(tabs, "page:x", "page:a");
    expect(next).toBe(tabs);
    expect(nextActiveKey).toBe("page:a");
  });
});
