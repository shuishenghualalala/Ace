/** @vitest-environment happy-dom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SkillsHub from "./SkillsHub";

const apiMock = vi.hoisted(() => ({
  skillStore: vi.fn(),
  skillSources: vi.fn(),
  addSkillSource: vi.fn(),
  removeSkillSource: vi.fn(),
  refreshSkillSource: vi.fn(),
  importSkillDirectory: vi.fn(),
  installSkill: vi.fn(),
  uninstallSkill: vi.fn(),
}));

vi.mock("../api", () => ({ api: apiMock }));

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("confirm", vi.fn(() => true));
  apiMock.skillStore.mockResolvedValue({ installed: [], optional: [] });
  apiMock.skillSources.mockResolvedValue({ sources: [] });
  apiMock.addSkillSource.mockResolvedValue({ ok: true, source: { path: "D:\\team-skills", label: "team-skills" } });
  apiMock.removeSkillSource.mockResolvedValue({ ok: true });
  apiMock.refreshSkillSource.mockResolvedValue({
    ok: true,
    source: { kind: "url", url: "https://skills.example.com/manifest.json", label: "Remote" },
  });
  apiMock.importSkillDirectory.mockResolvedValue({ ok: true, slug: "prepared-skill" });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function render(): Promise<void> {
  await act(async () => {
    root.render(<SkillsHub />);
  });
  await act(async () => { await Promise.resolve(); });
}

it("registers and removes a local skill market source", async () => {
  const market = { path: "D:\\team-skills", label: "team-skills" };
  apiMock.skillSources
    .mockResolvedValueOnce({ sources: [] })
    .mockResolvedValue({ sources: [market] });

  await render();
  expect(host.textContent).toContain("尚未登记市场目录");

  const input = host.querySelector<HTMLInputElement>('input[aria-label="市场源"]');
  expect(input).not.toBeNull();
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, "D:\\team-skills");
    input?.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    host.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await vi.waitFor(() => expect(apiMock.addSkillSource).toHaveBeenCalledWith("D:\\team-skills"));
  await vi.waitFor(() => expect(host.textContent).toContain("team-skills"));
  expect(host.textContent).toContain("D:\\team-skills");

  const remove = [...host.querySelectorAll("button")].find((button) => button.textContent === "移除");
  await act(async () => remove?.click());
  await vi.waitFor(() => expect(apiMock.removeSkillSource).toHaveBeenCalledWith(market.path));
});

it("refreshes a remote HTTPS source", async () => {
  const remote = {
    kind: "url" as const,
    url: "https://skills.example.com/manifest.json",
    label: "Remote",
    available: false,
    skill_count: 0,
  };
  apiMock.skillSources.mockResolvedValue({ sources: [remote] });
  await render();
  expect(host.textContent).toContain("缓存不可用");
  const refresh = [...host.querySelectorAll("button")].find((button) => button.textContent === "刷新");
  await act(async () => refresh?.click());
  await vi.waitFor(() => expect(apiMock.refreshSkillSource).toHaveBeenCalledWith(remote.url));
  expect(host.textContent).toContain("Remote");
});

it("imports a prepared skill directory", async () => {
  await render();
  const input = host.querySelector<HTMLInputElement>('input[aria-label="技能目录路径"]');
  expect(input).not.toBeNull();
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, "D:\\prepared-skill");
    input?.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    input?.closest("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await vi.waitFor(() => expect(apiMock.importSkillDirectory).toHaveBeenCalledWith("D:\\prepared-skill"));
  expect(host.textContent).toContain("尚未登记市场目录");
});

it("shows an error when market sources cannot be loaded", async () => {
  apiMock.skillSources.mockRejectedValue(new Error("市场源接口不可用"));
  await render();
  await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toContain("市场源接口不可用"));
  expect(host.textContent).not.toContain("尚未登记市场目录");
});

it("shows the Gateway error when a source is refused", async () => {
  apiMock.addSkillSource.mockRejectedValue(new Error("该市场源已登记"));
  await render();
  const input = host.querySelector<HTMLInputElement>('input[aria-label="市场源"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, "D:\\duplicate");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    host.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
  await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toContain("该市场源已登记"));
  expect(host.textContent).toContain("尚未登记市场目录");
});
