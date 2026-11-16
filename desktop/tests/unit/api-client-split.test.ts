import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  backendApi,
  workApi,
  officeApi,
  notificationApi,
  BackendChatSocket,
  type BackendSession,
  type WikiPage,
  type WorkItem,
  type ToolInfo,
  type BackendConfig,
} from '../../src/ui/backend-client';
import { readJsonResponse, getJSON, gatewayFetch } from '../../src/ui/api/transport';

describe('backend-client split facade', () => {
  it('exports the same public objects as before the split', () => {
    expect(backendApi).toBeDefined();
    expect(workApi).toBeDefined();
    expect(officeApi).toBeDefined();
    expect(notificationApi).toBeDefined();
    expect(BackendChatSocket).toBeDefined();
    expect(typeof BackendChatSocket).toBe('function');
  });

  it('assembles backendApi from feature modules without key collisions', () => {
    // 抽查代表性方法存在且为函数
    expect(typeof backendApi.sessions).toBe('function');
    expect(typeof backendApi.config).toBe('function');
    expect(typeof backendApi.tools).toBe('function');
    expect(typeof backendApi.wikiPages).toBe('function');
    expect(typeof backendApi.dynamicKanbanBoard).toBe('function');
    expect(typeof backendApi.systemMetrics).toBe('function');
    expect(typeof backendApi.plugins).toBe('function');
    expect(typeof backendApi.mcpServers).toBe('function');
    expect(typeof backendApi.upload).toBe('function');
  });

  it('keeps backendApi method identity stable (same reference across re-imports)', async () => {
    const mod = await import('../../src/ui/backend-client');
    expect(mod.backendApi.sessions).toBe(backendApi.sessions);
    expect(mod.backendApi.wikiPages).toBe(backendApi.wikiPages);
    expect(mod.backendApi.config).toBe(backendApi.config);
  });

  it('preserves workApi / officeApi / notificationApi method surface', () => {
    expect(typeof workApi.history).toBe('function');
    expect(typeof workApi.listItems).toBe('function');
    expect(typeof officeApi.mailLatest).toBe('function');
    expect(typeof officeApi.todoFetch).toBe('function');
    expect(typeof officeApi.scheduleSearch).toBe('function');
    expect(typeof officeApi.meetingPending).toBe('function');
    expect(typeof notificationApi.list).toBe('function');
    expect(typeof notificationApi.markAllRead).toBe('function');
  });

  it('still exports key types as type-only imports', () => {
    // 类型编译期存在即足够；这里用 as 做轻量引用同一性断言
    const _t: ToolInfo = { name: 'x', toolset: 'y' };
    const _w: WorkItem = {
      item_id: '1',
      owner_account_id: 'a',
      title: 't',
      version: 1,
      created_at: 0,
      updated_at: 0,
    };
    const _c: BackendConfig = {
      model: 'm',
      has_key: true,
      base_url: 'http://localhost:8000',
      active_model_id: 'm',
      models: [],
    };
    expect(_t).toBeDefined();
    expect(_w).toBeDefined();
    expect(_c).toBeDefined();
  });
});

describe('shared transport error parsing', () => {
  function createMemoryStorage(): Storage {
    const data = new Map<string, string>();
    return {
      get length() { return data.size; },
      clear: () => data.clear(),
      getItem: (key: string) => (data.has(key) ? data.get(key)! : null),
      key: (index: number) => Array.from(data.keys())[index] ?? null,
      removeItem: (key: string) => { data.delete(key); },
      setItem: (key: string, value: string) => { data.set(key, String(value)); },
    } as Storage;
  }

  beforeEach(() => {
    Object.defineProperty(globalThis, 'localStorage', {
      value: createMemoryStorage(),
      configurable: true,
      writable: true,
    });
    if (typeof window === 'undefined') {
      Object.defineProperty(globalThis, 'window', {
        value: globalThis,
        configurable: true,
        writable: true,
      });
    }
  });

  it('readJsonResponse extracts JSON error body message', async () => {
    const res = new Response(JSON.stringify({ error: 'custom backend error' }), {
      status: 400,
      statusText: 'Bad Request',
      headers: { 'Content-Type': 'application/json' },
    });
    await expect(readJsonResponse(res, '/api/test')).rejects.toThrow('custom backend error');
  });

  it('readJsonResponse falls back to status code when body is not JSON', async () => {
    const res = new Response('<html>error</html>', {
      status: 500,
      statusText: 'Internal Server Error',
    });
    await expect(readJsonResponse(res, '/api/test')).rejects.toThrow('500 /api/test');
  });

  it('readJsonResponse rejects HTML ok responses as missing endpoint', async () => {
    const res = new Response('<!doctype html><html></html>', {
      status: 200,
      headers: { 'Content-Type': 'text/html' },
    });
    await expect(readJsonResponse(res, '/api/test')).rejects.toThrow('当前服务未提供接口 /api/test');
  });

  it('getJSON delegates to gatewayFetch and readJsonResponse', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));
    const result = await getJSON<{ ok: boolean }>('/api/health');
    expect(result).toEqual({ ok: true });
    expect(gatewayFetch).toBeDefined();
    vi.unstubAllGlobals();
  });
});
