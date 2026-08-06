export interface SecurityCapabilities {
  platform?: string;
  helper_present?: boolean;
  filesystem_sandbox?: boolean;
  managed_network?: boolean;
  local_binding_control?: boolean;
  detail?: string;
}

/** Gateway uses Python's "windows"; Electron uses Node's "win32". */
export function isWindowsPlatform(platform?: string): boolean {
  return !platform || platform === 'windows' || platform === 'win32';
}

export function formatCapabilitySummary(capability: SecurityCapabilities): string {
  if (!capability.helper_present) return `未启用原生沙箱 · ${capability.detail ?? '运行组件缺失'}`;
  const enabled = capability.filesystem_sandbox && capability.managed_network;
  return `${enabled ? '原生沙箱已启用' : '沙箱配置不完整'} · ${capability.detail ?? capability.platform ?? ''}`;
}
