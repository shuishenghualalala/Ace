/** Closed renderer-to-main IPC surface. Additions require main/preload review. */
export const IPC_INVOKE_CHANNELS = [
  'window:minimize',
  'window:maximize',
  'window:close',
  'window:isMaximized',
  'inspiration:open-window',
  'inspiration:close-window',
  'inspiration:window-state',
  'app:quit',
  'app:get-version',
  'shell:openExternal',
  'shell:openPath',
  'dialog:saveLocalExport',
  'shell:readTextFile',
  'shell:writeTextFile',
  'shell:readFileBase64',
  'shell:writeFileBase64',
  'shell:pathExists',
  'workspace:directoryInfo',
  'shell:showItemInFolder',
  'shell:listOpenApplications',
  'shell:openPathWith',
  'wiki:openSourceFile',
  'clipboard:writeImage',
  'image:showItemInFolder',
  'dialog:selectFile',
  'dialog:selectFolder',
  'app:get-auto-launch-enabled',
  'app:set-auto-launch-enabled',
  'app:get-close-behavior',
  'app:set-close-behavior',
  'app:get-system-locale',
  'app:renderer-initial-state-ready',
  'security:get-strict-security',
  'security:set-strict-security',
  'update:start-download',
  'update:pause',
  'update:resume',
  'update:retry',
  'update:install-package',
  'update:get-state',
  'feedback:preview',
  'feedback:submit',
  'feedback:cancel',
  'feedback:list',
  'feedback:image',
  'auth:heartbeat',
  'auth:get-state',
  'auth:send-code',
  'auth:login',
  'auth:logout',
  'gateway:fetch',
  'gateway:stream-start',
  'gateway:stream-cancel',
  'gateway:upload',
  'gateway:ensure',
  'gateway:get-status',
  'security:pending',
  'security:set-mode',
  'security:decide',
  'security:capabilities',
  'security:rules',
  'security:set-rule',
  'security:delete-rule',
  'security:audit',
  'security:audit-export',
  'security:audit-purge',
  'security:alerts',
  'security:alert-isolate',
  'security:alert-revoke',
  'security:alert-resolve',
  'security:uac-status',
  'security:enable-uac',
  'security:setup',
  'gateway:retry',
  'gateway-ws:connect',
  'gateway-ws:send',
  'gateway-ws:close',
  'browser-ws:connect',
  'browser-ws:close',
  'browser-view:set-panel',
  'browser-view:hide',
  'browser-view:get-navigation',
  'tray:set-status',
  'release:open-latest-download',
] as const;

export type IpcInvokeChannel = (typeof IPC_INVOKE_CHANNELS)[number];

const IPC_INVOKE_CHANNEL_SET: ReadonlySet<string> = new Set(IPC_INVOKE_CHANNELS);

export function isIpcInvokeChannel(value: string): value is IpcInvokeChannel {
  return IPC_INVOKE_CHANNEL_SET.has(value);
}

export const IPC_MAIN_TO_RENDERER_EVENT_CHANNELS = [
  'window:maximized-changed',
  'inspiration:window-state-changed',
  'gateway:stream-event',
  'gateway-ws:event',
  'browser-ws:event',
  'browser-view:navigation-changed',
  'browser-view:interaction-requested',
  'browser-view:load-failed',
  'browser-view:layout-invalidated',
  'version-update-available',
  'version-update-download-progress',
  'auth:session-state',
  'main:uncaught-error',
  'backend:status',
  'backend:suppress-overlay',
  'tray:activated',
] as const;

export type IpcMainToRendererEventChannel =
  (typeof IPC_MAIN_TO_RENDERER_EVENT_CHANNELS)[number];

const IPC_MAIN_TO_RENDERER_EVENT_CHANNEL_SET: ReadonlySet<string> =
  new Set(IPC_MAIN_TO_RENDERER_EVENT_CHANNELS);

export function isIpcMainToRendererEventChannel(
  value: string,
): value is IpcMainToRendererEventChannel {
  return IPC_MAIN_TO_RENDERER_EVENT_CHANNEL_SET.has(value);
}

export const IPC_RENDERER_TO_MAIN_EVENT_CHANNELS = [
  'inspiration:sticky-close',
] as const;

export type IpcRendererToMainEventChannel =
  (typeof IPC_RENDERER_TO_MAIN_EVENT_CHANNELS)[number];

const IPC_RENDERER_TO_MAIN_EVENT_CHANNEL_SET: ReadonlySet<string> =
  new Set(IPC_RENDERER_TO_MAIN_EVENT_CHANNELS);

export function isIpcRendererToMainEventChannel(
  value: string,
): value is IpcRendererToMainEventChannel {
  return IPC_RENDERER_TO_MAIN_EVENT_CHANNEL_SET.has(value);
}
