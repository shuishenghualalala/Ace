/** @vitest-environment happy-dom */

import { beforeEach, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  probeMcpServer: vi.fn(),
  mcpServers: vi.fn(),
  cuaDriverStatus: vi.fn(),
  createMcpServer: vi.fn(),
  updateMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  reloadMcpServer: vi.fn(),
}));

vi.mock('../../src/ui/backend-client', () => ({ backendApi: apiMock }));

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.probeMcpServer.mockResolvedValue({
    ok: true, tools: ['ping', 'echo'], tool_count: 2, elapsed_ms: 12, error: null,
  });
  setupForm();
});

function setupForm(): void {
  document.body.innerHTML = `
    <div id="mcp-server-overlay" hidden>
      <h3 id="mcp-server-title"></h3>
      <button type="button" id="mcp-server-close"></button>
      <form id="mcp-server-form">
        <input id="mcp-name" value="remote" />
        <input type="radio" name="mcp-transport" value="stdio" checked />
        <input type="radio" name="mcp-transport" value="http" />
        <input type="radio" name="mcp-transport" value="sse" />
        <div id="mcp-stdio-wrap"><input id="mcp-command" /><input id="mcp-args" /></div>
        <div id="mcp-remote-wrap" hidden><input id="mcp-url" /><textarea id="mcp-headers"></textarea></div>
        <textarea id="mcp-env"></textarea>
      </form>
      <p id="mcp-probe-result" hidden></p>
      <button type="button" id="mcp-server-delete" hidden>删除服务</button>
      <button type="button" id="mcp-server-probe">测试连接</button>
      <button type="button" id="mcp-server-add">添加服务</button>
    </div>`;
}

it('ignores a late probe result after switching transport', async () => {
  vi.resetModules();
  let resolveProbe!: (value: unknown) => void;
  let probeSignal: AbortSignal | undefined;
  apiMock.probeMcpServer.mockImplementation((_payload: unknown, signal?: AbortSignal) => {
    probeSignal = signal;
    return new Promise((resolve) => { resolveProbe = resolve; });
  });
  const { bindMcpPane, openMcpServerModal } = await import('../../src/ui/features/settings-mcp');
  bindMcpPane();
  await openMcpServerModal();

  const http = document.querySelector<HTMLInputElement>('input[name="mcp-transport"][value="http"]')!;
  http.checked = true;
  http.dispatchEvent(new Event('change', { bubbles: true }));
  (document.getElementById('mcp-url') as HTMLInputElement).value = 'https://mcp.example.com/mcp';
  (document.getElementById('mcp-server-probe') as HTMLButtonElement).click();

  const stdio = document.querySelector<HTMLInputElement>('input[name="mcp-transport"][value="stdio"]')!;
  stdio.checked = true;
  stdio.dispatchEvent(new Event('change', { bubbles: true }));
  expect(probeSignal?.aborted).toBe(true);
  resolveProbe({ ok: true, tools: ['ping'], tool_count: 1, elapsed_ms: 5, error: null });
  await new Promise((resolve) => setTimeout(resolve, 0));

  const result = document.getElementById('mcp-probe-result')!;
  expect(result.hidden).toBe(true);
  expect(result.textContent).toBe('');
  const button = document.getElementById('mcp-server-probe') as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  expect(button.textContent).toBe('测试连接');
});

it('probes an HTTP endpoint from the MCP settings form without saving', async () => {
  vi.resetModules();
  const { bindMcpPane, openMcpServerModal } = await import('../../src/ui/features/settings-mcp');
  bindMcpPane();
  await openMcpServerModal();

  const http = document.querySelector<HTMLInputElement>('input[name="mcp-transport"][value="http"]')!;
  http.checked = true;
  http.dispatchEvent(new Event('change', { bubbles: true }));
  (document.getElementById('mcp-url') as HTMLInputElement).value = 'https://mcp.example.com/mcp';
  (document.getElementById('mcp-headers') as HTMLTextAreaElement).value = 'Authorization: Bearer secret';

  (document.getElementById('mcp-server-probe') as HTMLButtonElement).click();
  await vi.waitFor(() => expect(apiMock.probeMcpServer).toHaveBeenCalledWith({
    transport: 'http',
    url: 'https://mcp.example.com/mcp',
    headers: { Authorization: 'Bearer secret' },
  }, expect.anything()));

  const result = document.getElementById('mcp-probe-result')!;
  await vi.waitFor(() => expect(result.textContent).toContain('连接成功'));
  expect(result.textContent).toContain('2 个工具');
  expect(result.textContent).toContain('ping, echo');
  expect(apiMock.createMcpServer).not.toHaveBeenCalled();
  expect(apiMock.updateMcpServer).not.toHaveBeenCalled();

  const stdio = document.querySelector<HTMLInputElement>('input[name="mcp-transport"][value="stdio"]')!;
  stdio.checked = true;
  stdio.dispatchEvent(new Event('change', { bubbles: true }));
  expect(result.hidden).toBe(true);
  expect(result.textContent).toBe('');
});
