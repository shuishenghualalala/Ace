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
  apiMock.probeMcpServer.mockResolvedValue({ ok: true, tools: ['ping', 'echo'], tool_count: 2, error: null });
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

it('probes an HTTP endpoint from the MCP settings form without saving', async () => {
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
  }));

  const result = document.getElementById('mcp-probe-result')!;
  await vi.waitFor(() => expect(result.textContent).toContain('连接成功'));
  expect(result.textContent).toContain('2 个工具');
  expect(result.textContent).toContain('ping, echo');
  expect(apiMock.createMcpServer).not.toHaveBeenCalled();
  expect(apiMock.updateMcpServer).not.toHaveBeenCalled();
});
