import { backendApi } from '../backend-client';
import { mountRenderer } from '../app';
import type { RendererAdapter } from '../adapters/renderer-adapter';
import type { AuthStateSnapshot } from '../../shared/types';
import { createApplicationShell } from '../layouts/application-shell';
import {
  FIXTURE_MARKER,
  selectFixture,
  type VisualFixture,
} from './fixtures';

type GatewayEventListener = (event: unknown) => void;

function createFixtureBridge(fixture: VisualFixture): Window['Crew'] {
  const listeners = new Set<GatewayEventListener>();
  const timers = new Set<ReturnType<typeof setTimeout>>();

  const clearTimers = (): void => {
    for (const timer of timers) clearTimeout(timer);
    timers.clear();
  };

  const isOpenEvent = (event: unknown): boolean =>
    typeof event === 'object'
    && event !== null
    && (event as { type?: unknown }).type === 'open';

  const scheduleEvents = (
    include: (event: unknown) => boolean,
    transform: (event: unknown) => unknown = (event) => event,
  ): void => {
    for (const item of fixture.events) {
      if (!include(item.event)) continue;
      const timer = setTimeout(() => {
        timers.delete(timer);
        const event = transform(item.event);
        for (const listener of listeners) listener(event);
      }, item.afterMs);
      timers.add(timer);
    }
  };

  const authState: AuthStateSnapshot = {
    mode: 'local',
    configured: true,
    providerId: 'fixture',
    isLoggedIn: fixture.auth === 'authenticated',
    user: fixture.auth === 'authenticated'
      ? {
          userId: fixture.owner.key,
          phoneNumber: '',
          displayName: fixture.owner.displayName,
        }
      : null,
  };

  const bindOutboundRequest = (
    event: unknown,
    requestId: string,
    sessionId: string,
  ): unknown => {
    if (typeof event !== 'object' || event === null) return event;
    const gatewayEvent = event as { type?: unknown; data?: unknown };
    if (gatewayEvent.type !== 'message' || typeof gatewayEvent.data !== 'string') return event;
    const frame = JSON.parse(gatewayEvent.data) as Record<string, unknown>;
    return {
      ...gatewayEvent,
      data: JSON.stringify({
        ...frame,
        request_id: requestId,
        session_id: sessionId,
      }),
    };
  };

  const bridge = {
    windowMinimize: async () => undefined,
    windowMaximize: async () => undefined,
    windowClose: async () => undefined,
    workspaceDirectoryInfo: async (workspaceId: string) => ({
      exists: workspaceId !== 'finance-workspace',
      canonicalPath: workspaceId === 'finance-workspace' ? null : `/Projects/${workspaceId}`,
    }),
    selectFolder: async () => null,
    getAppVersion: async () => '0.0.0-fixture',
    getAutoLaunchEnabled: async () => ({ enabled: false }),
    getCloseBehavior: async () => ({ closeBehavior: 'tray' as const }),
    getFeedbackList: async () => ({
      success: true,
      list: [{
        id: 7,
        staffCode: 'MW-001',
        title: '分页与详情展示',
        description: '提交记录需要支持完整分页，并让详情内容更容易阅读。',
        status: 'PROCESSING' as const,
        adminReply: '已收到，正在核查。',
        createdAt: new Date(fixture.now - 3_600_000).toISOString(),
        updatedAt: new Date(fixture.now - 1_800_000).toISOString(),
      }],
      total: 42,
    }),
    securityCapabilities: async () => ({
      ok: true,
      body: {
        platform: 'win32',
        helper_present: true,
        filesystem_sandbox: true,
        managed_network: true,
        local_binding_control: true,
        detail: 'Windows 受管执行环境已就绪',
      },
    }),
    securityRules: async () => ({
      ok: true,
      body: {
        rules: [{
          rule_id: 'fixture-rule',
          decision: 'allow',
          argv_prefix: ['git', 'status'],
          cwd: 'D:/Projects/Crew',
          enabled: true,
        }],
      },
    }),
    securityAudit: async () => ({
      ok: true,
      body: {
        total: 1,
        events: [{
          timestamp: Math.floor(fixture.now / 1000) - 90,
          action_type: 'exec',
          decision: 'allow',
          sandbox_backend: 'windows',
        }],
      },
    }),
    ensureGateway: async () => ({
      baseUrl: 'http://fixture.invalid',
      managed: false,
    }),
    rendererInitialStateReady: async () => ({ ok: true as const }),
    authGetState: async () => ({ ok: true, state: authState }),
    authSendCode: async () => ({ ok: false, error: '视觉预览不支持发送验证码' }),
    authLogin: async () => ({ ok: false, error: '视觉预览不支持登录' }),
    authLogout: async () => ({ ok: true }),
    onBackendStatus: (
      listener: (status: { connected: boolean; components: Record<string, never> }) => void,
    ) => {
      let active = true;
      queueMicrotask(() => {
        if (active) {
          listener({
            connected: fixture.backend === 'connected',
            components: {},
          });
        }
      });
      return () => { active = false; };
    },
    onSessionState: (listener: (state: AuthStateSnapshot) => void) => {
      queueMicrotask(() => listener(authState));
    },
    gatewayFetch: async (url: string, init?: { method?: string }) => {
      const path = new URL(url, 'http://fixture.invalid').pathname;
      const method = String(init?.method || 'GET').toUpperCase();
      const response = fixture.responses[`${method} ${path}`] ?? fixture.responses[path];
      return {
        status: response?.status ?? (response ? 200 : 404),
        statusText: response ? 'Fixture response' : 'Fixture route not found',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(response?.body ?? { error: `No fixture for ${path}` }),
      };
    },
    gatewayWsConnect: async () => {
      clearTimers();
      scheduleEvents(isOpenEvent);
      return { ok: true };
    },
    gatewayWsSend: async (payload: unknown) => {
      if (
        typeof payload === 'object'
        && payload !== null
      ) {
        const outbound = payload as {
          query?: unknown;
          request_id?: unknown;
          session_id?: unknown;
        };
        if (
          typeof outbound.query === 'string'
          && typeof outbound.request_id === 'string'
          && typeof outbound.session_id === 'string'
        ) {
          scheduleEvents(
            (event) => !isOpenEvent(event),
            (event) => bindOutboundRequest(event, outbound.request_id as string, outbound.session_id as string),
          );
        }
      }
      return { ok: true };
    },
    gatewayWsClose: async () => {
      clearTimers();
      return { ok: true };
    },
    onGatewayWsEvent: (listener: GatewayEventListener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };

  return bridge as unknown as Window['Crew'];
}

export function createFixtureAdapter(fixture: VisualFixture): RendererAdapter {
  return {
    bridge: createFixtureBridge(fixture),
    backend: backendApi,
    now: () => fixture.now,
  };
}

export function startFixtureRenderer(
  root: HTMLElement = document.body,
  search: string = window.location.search,
): () => void {
  const fixture = selectFixture(search);
  const adapter = createFixtureAdapter(fixture);
  Object.defineProperty(window, 'Crew', {
    configurable: true,
    value: adapter.bridge,
  });
  root.dataset.fixtureId = fixture.id;
  root.dataset.fixtureMarker = FIXTURE_MARKER;
  Object.assign(window, { __mwCreateApplicationShell: createApplicationShell });

  const disposeRenderer = mountRenderer(root, adapter);
  return () => {
    disposeRenderer();
    void adapter.bridge.gatewayWsClose();
    root.removeAttribute('data-fixture-id');
    root.removeAttribute('data-fixture-marker');
    Reflect.deleteProperty(window, '__mwCreateApplicationShell');
  };
}
