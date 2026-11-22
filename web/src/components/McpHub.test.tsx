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
  });
  await vi.waitFor(() => expect(host.textContent).toContain("连接成功（12 ms）"));

  const save = [...host.querySelectorAll("button")].find((button) => button.textContent === "保存");
  await act(async () => save?.click());
  await vi.waitFor(() => expect(apiMock.createMcpServer).toHaveBeenCalled());
  expect(apiMock.createMcpServer).toHaveBeenCalledWith({
    transport: "http",
    url: "https://mcp.example.com/new",
    name: "probe-target",
  });
});
