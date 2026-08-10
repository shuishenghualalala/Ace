export interface SecurityCapabilities {
  platform?: string;
  helper_present?: boolean;
  filesystem_sandbox?: boolean;
  managed_network?: boolean;
  local_binding_control?: boolean;
  detail?: string;
}

/** Gateway uses Python names; Electron uses Node names. Unknown stays unknown. */
export function isWindowsPlatform(platform?: string): boolean {
  return platform === 'windows' || platform === 'win32';
}

export function isMacOSPlatform(platform?: string): boolean {
  return platform === 'darwin' || platform === 'macos';
}

export function detectedRuntimePlatform(capabilityPlatform?: string): string | undefined {
  const reported = capabilityPlatform?.trim().toLowerCase();
  if (reported) return reported;
  if (typeof window === 'undefined') return undefined;
  const runtimePlatform = window.Crew?.runtimePlatform;
  return typeof runtimePlatform === 'string' && runtimePlatform.trim()
    ? runtimePlatform.trim().toLowerCase()
    : undefined;
}

export function formatCapabilitySummary(capability: SecurityCapabilities): string {
  if (!capability.helper_present) return `未启用原生沙箱 · ${capability.detail ?? '运行组件缺失'}`;
  const enabled = capability.filesystem_sandbox && capability.managed_network;
  return `${enabled ? '原生沙箱已启用' : '沙箱配置不完整'} · ${capability.detail ?? '等待能力检测'}`;
}
