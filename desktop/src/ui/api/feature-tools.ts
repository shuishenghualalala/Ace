/**
 * 工具 / MCP / CUA 客户端：toolsets、tools、场景、MCP server 管理、CUA Driver 安装。
 */
import { getJSON, jsonBody } from './transport';

/** /api/tools 返回的单个工具行（外援等工具选择器用）。 */
export interface ToolInfo {
  name: string;
  toolset: string;
  display_name?: string;
}

export interface SubScenario {
  id: string;
  title: string;
  query: string;
}

export interface Scenario {
  id: string;
  title: string;
  icon?: string;
  description?: string;
  category?: string;
  items: SubScenario[];
}

// ── MCP Server 管理（/api/mcp/servers，admin-only）──

/** MCP server 传输类型。 */
export type McpTransport = 'stdio' | 'http' | 'sse' | 'unknown';

/** MCP server 运行时配置（密钥类 env 已脱敏为 ***）。 */
export interface McpServerConfig {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  transport?: McpTransport;
  headers?: Record<string, string>;
}

/** 后端 /api/mcp/servers 返回的单个 server 行。 */
export interface McpServerRow {
  name: string;
  transport: McpTransport;
  connected: boolean;
  error: string;
  tools: string[];
  config: McpServerConfig;
}

/** 新增/编辑 MCP server 的 payload（前端表单 → 后端）。 */
export interface McpServerPayload {
  name?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  transport?: McpTransport;
  headers?: Record<string, string>;
}

// ── CUA Driver（Computer Use）一键安装（/api/mcp/cua-driver/*，登录用户可用）──

/** CUA Driver 安装任务的某个步骤进度。 */
export interface CuaSetupStep {
  name: string;
  status: string; // pending / running / success / failed / skipped
  message: string;
  ts: number;
}

/** GET /api/mcp/cua-driver/setup/{task_id} 返回的安装进度。 */
export interface CuaSetupProgress {
  task_id: string;
  platform: string;
  status: string; // pending / running / success / failed / cancelled
  started_at: number;
  finished_at: number | null;
  steps: CuaSetupStep[];
  log: string[];
  error: string | null;
}

/** GET /api/mcp/cua-driver/status 返回的当前安装/运行状态。 */
export interface CuaDriverStatus {
  ok: boolean;
  installed: boolean;
  binary: string | null;
  version: string;
  daemon_running: boolean;
  mcp_enabled: boolean;
  tools_registered: string[];
}

export const toolsApi = {
  toolsets: () => getJSON<string[]>('/api/toolsets'),
  tools: () => getJSON<ToolInfo[]>('/api/tools'),

  scenarios: (count = 4) => getJSON<Scenario[]>(`/api/scenarios?count=${count}`),
  scenarioIntroLines: (count = 8) => getJSON<string[]>(`/api/scenarios/intro-lines?count=${count}`),
  scenarioLoadingStatuses: (count = 8) => getJSON<string[]>(`/api/scenarios/loading-status?count=${count}`),

  // ── MCP Server 管理（/api/mcp/servers，admin-only）──

  mcpServers: () =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>('/api/mcp/servers'),
  createMcpServer: (payload: McpServerPayload) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>('/api/mcp/servers', {
      method: 'POST',
      ...jsonBody(payload),
    }),
  updateMcpServer: (name: string, payload: McpServerPayload) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: 'PUT',
      ...jsonBody(payload),
    }),
  deleteMcpServer: (name: string) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  reloadMcpServer: (name: string) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>(`/api/mcp/servers/${encodeURIComponent(name)}/reload`, {
      method: 'POST',
    }),

  // ── CUA Driver（Computer Use）一键安装（/api/mcp/cua-driver/*，登录用户可用）──

  cuaDriverStatus: () => getJSON<CuaDriverStatus>('/api/mcp/cua-driver/status'),
  cuaDriverSetup: (opts?: { force_reinstall?: boolean; start_daemon?: boolean }) =>
    getJSON<{ ok: boolean; task_id: string; status: string }>('/api/mcp/cua-driver/setup', {
      method: 'POST',
      ...jsonBody(opts ?? {}),
    }),
  cuaDriverSetupStatus: (taskId: string) =>
    getJSON<CuaSetupProgress & { ok: boolean }>(
      `/api/mcp/cua-driver/setup/${encodeURIComponent(taskId)}`,
    ),
  cuaDriverCancel: (taskId: string) =>
    getJSON<{ ok: boolean; task_id: string; status: string }>(
      `/api/mcp/cua-driver/setup/${encodeURIComponent(taskId)}/cancel`,
      { method: 'POST' },
    ),
};
