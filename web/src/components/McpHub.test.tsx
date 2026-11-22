/** @vitest-environment happy-dom */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import McpHub from "./McpHub";

const apiMock = vi.hoisted(() => ({
  mcpServers: vi.fn(),
  createMcpServer: vi.fn(),
  updateMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  reloadMcpServer: vi.fn(),
  probeMcpServer: vi.fn(),
}));

vi.mock("../api", () => ({ api: apiMock }));

let host: HTMLElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("confirm", vi.fn(() => true));
  apiMock.mcpServers.mockResolvedValue({
    ok: true,
    servers: [{
      name: "remote",
      transport: "http",
      connected: true,
      error: "",
      tools: ["ping"],
      config: { url: "https://mcp.example.com/mcp", transport: "http" },
    }],
  });
  apiMock.probeMcpServer.mockResolvedValue({
    ok: true, tools: ["ping", "echo"], tool_count: 2, elapsed_ms: 12, error: null,
  });
  apiMock.createMcpServer.mockResolvedValue({ ok: true, servers: [] });
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
  await act(async () => { root.render(<McpHub />); });
  await act(async () => { await Promise.resolve(); });
}

it("edits an MCP server without echoing masked secrets", async () => {
  apiMock.mcpServers.mockResolvedValue({
    ok: true,
    servers: [{
      name: "remote",
      transport: "http",
      connected: true,
      error: "",
      tools: ["ping"],
      config: {
        url: "https://mcp.example.com/mcp",
        transport: "http",
        headers: { Authorization: "***" },
        env: { API_KEY: "***" },
        auto_connect: false,
      },
    }],
  });
  await render();
  const edit = [...host.querySelectorAll("button")].find((button) => button.textContent === "编辑");
  await act(async () => edit?.click());
  expect(host.querySelector<HTMLInputElement>('input[aria-label="服务名称"]')?.disabled).toBe(true);
  expect(host.querySelector<HTMLInputElement>('input[aria-label="启动时自动连接"]')?.checked).toBe(false);
  expect(host.textContent).toContain("Authorization: ***");
  const url = host.querySelector<HTMLInputElement>('input[aria-label="MCP URL"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(url, "https://mcp.example.com/next");
    url.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const probe = [...host.querySelectorAll("button")].find((button) => button.textContent === "测试连接");
  await act(async () => probe?.click());
  await vi.waitFor(() => expect(apiMock.probeMcpServer).toHaveBeenCalledWith({
    name: "remote",
    transport: "http",
    url: "https://mcp.example.com/next",
    headers: { Authorization: "***" },
    env: { API_KEY: "***" },
    auto_connect: false,
  }, expect.anything()));
  const save = [...host.querySelectorAll("button")].find((button) => button.textContent === "保存");
  await act(async () => save?.click());
  await vi.waitFor(() => expect(apiMock.updateMcpServer).toHaveBeenCalledWith("remote", {
    transport: "http",
    url: "https://mcp.example.com/next",
    headers: { Authorization: "***" },
    env: { API_KEY: "***" },
    auto_connect: false,
  }));
});

it("shows failure before connected when a server reports both", async () => {
  apiMock.mcpServers.mockResolvedValue({
    ok: true,
    servers: [{
      name: "flaky", transport: "http", connected: true, error: "reconnect failed", tools: ["ping"],
      config: { url: "https://mcp.example.com/mcp", transport: "http" },
    }],
  });
  await render();
  expect(host.textContent).toContain("失败");
  expect(host.textContent).not.toContain("已连接");
});

it("shows a load error without claiming the MCP list is empty", async () => {
  apiMock.mcpServers.mockRejectedValue(new Error("需要管理员权限"));
  await render();
  expect(host.textContent).toContain("需要管理员权限");
  expect(host.textContent).not.toContain("暂无 MCP 服务");
});

it("closes the MCP form with Escape", async () => {
  await render();
  const add = [...host.querySelectorAll("button")].find((button) => button.textContent === "添加服务");
  add?.focus();
  await act(async () => add?.click());
  expect(host.querySelector(".mcp-modal")).not.toBeNull();
  expect(document.activeElement).toBe(host.querySelector(".mcp-modal input"));
  await act(async () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  });
  expect(host.querySelector(".mcp-modal")).toBeNull();
  expect(document.activeElement).toBe(add);
});

it("refreshes the MCP server list on demand", async () => {
  await render();
  expect(apiMock.mcpServers).toHaveBeenCalledTimes(1);
  const refresh = [...host.querySelectorAll("button")].find((button) => button.textContent === "刷新");
  await act(async () => refresh?.click());
  await vi.waitFor(() => expect(apiMock.mcpServers).toHaveBeenCalledTimes(2));
});

it("filters MCP servers by name and tool", async () => {
  apiMock.mcpServers.mockResolvedValue({
    ok: true,
    servers: [
      {
        name: "remote", transport: "http", connected: true, error: "", tools: ["ping"],
        config: { url: "https://mcp.example.com/mcp", transport: "http" },
      },
      {
        name: "local", transport: "stdio", connected: false, error: "", tools: ["echo"],
        config: { command: "echo", transport: "stdio" },
      },
    ],
  });
  await render();
  expect(host.textContent).toContain("remote");
  expect(host.textContent).toContain("local");
  const search = host.querySelector<HTMLInputElement>('input[aria-label="搜索 MCP 服务"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(search, "ping");
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(host.textContent).toContain("remote");
  expect(host.textContent).not.toContain("local");
});

it("clears the probe result when switching to stdio", async () => {
  await render();
  const add = [...host.querySelectorAll("button")].find((button) => button.textContent === "添加服务");
  await act(async () => add?.click());
  const transport = host.querySelector<HTMLSelectElement>('select[aria-label="传输类型"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(transport, "http");
    transport.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const url = host.querySelector<HTMLInputElement>('input[aria-label="MCP URL"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(url, "https://mcp.example.com/new");
    url.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const probe = [...host.querySelectorAll("button")].find((button) => button.textContent === "测试连接");
  await act(async () => probe?.click());
  await vi.waitFor(() => expect(host.textContent).toContain("连接成功"));
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(transport, "stdio");
    transport.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(host.textContent).not.toContain("连接成功");
  expect(host.textContent).toContain("测试连接不会执行命令");
});

it("ignores a late probe result after the transport changes", async () => {
  let resolveProbe!: (value: unknown) => void;
  let probeSignal: AbortSignal | undefined;
  apiMock.probeMcpServer.mockImplementation((_payload: unknown, signal?: AbortSignal) => {
    probeSignal = signal;
    return new Promise((resolve) => { resolveProbe = resolve; });
  });
  await render();
  const add = [...host.querySelectorAll("button")].find((button) => button.textContent === "添加服务");
  await act(async () => add?.click());
  const transport = host.querySelector<HTMLSelectElement>('select[aria-label="传输类型"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(transport, "http");
    transport.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const url = host.querySelector<HTMLInputElement>('input[aria-label="MCP URL"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(url, "https://mcp.example.com/new");
    url.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const probe = [...host.querySelectorAll("button")].find((button) => button.textContent === "测试连接");
  await act(async () => probe?.click());
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(transport, "stdio");
    transport.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(probeSignal?.aborted).toBe(true);
  await act(async () => {
    resolveProbe({ ok: true, tools: ["ping"], tool_count: 1, elapsed_ms: 5, error: null });
  });
  expect(host.textContent).not.toContain("连接成功");
});

it("lists MCP servers and probes an HTTP endpoint before saving", async () => {
  await render();
  expect(host.textContent).toContain("remote");
  expect(host.textContent).toContain("已连接");
  expect(host.textContent).toContain("ping");

  const add = [...host.querySelectorAll("button")].find((button) => button.textContent === "添加服务");
  await act(async () => add?.click());

  const name = host.querySelector<HTMLInputElement>('input[aria-label="服务名称"]')!;
  const transport = host.querySelector<HTMLSelectElement>('select[aria-label="传输类型"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(name, "probe-target");
    name.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    setter?.call(transport, "http");
    transport.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const url = host.querySelector<HTMLInputElement>('input[aria-label="MCP URL"]')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(url, "https://mcp.example.com/new");
    url.dispatchEvent(new Event("input", { bubbles: true }));
  });

  const probe = [...host.querySelectorAll("button")].find((button) => button.textContent === "测试连接");
  await act(async () => probe?.click());
  await vi.waitFor(() => expect(apiMock.probeMcpServer).toHaveBeenCalled());
  expect(apiMock.probeMcpServer).toHaveBeenCalledWith({
    transport: "http",
    url: "https://mcp.example.com/new",
    auto_connect: true,
  }, expect.anything());
  await vi.waitFor(() => expect(host.textContent).toContain("连接成功（12 ms）"));

  const autoConnect = host.querySelector<HTMLInputElement>('input[aria-label="启动时自动连接"]')!;
  await act(async () => autoConnect.click());
  const save = [...host.querySelectorAll("button")].find((button) => button.textContent === "保存");
  await act(async () => save?.click());
  await vi.waitFor(() => expect(apiMock.createMcpServer).toHaveBeenCalled());
  expect(apiMock.createMcpServer).toHaveBeenCalledWith({
    transport: "http",
    url: "https://mcp.example.com/new",
    name: "probe-target",
    auto_connect: false,
  });
});
