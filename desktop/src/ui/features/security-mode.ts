import type { BackendConfig } from '../backend-client';
import { state } from '../state';

export interface SecurityCapabilities {
  platform?: string;
  helper_present?: boolean;
  filesystem_sandbox?: boolean;
  managed_network?: boolean;
  local_binding_control?: boolean;
  detail?: string;
}

/** 安全模块总开关：后端 /api/config 返回 security.enabled，Ace 默认关闭。 */
export function securityModuleEnabled(config: BackendConfig | null | undefined = state.config): boolean {
  return config?.security?.enabled === true;
}

/** 配置加载完成后通知安全入口同步可用状态。 */
export function syncSecurityModuleFeatureUi(): void {
  window.dispatchEvent(new CustomEvent('security:config-change'));
}

/** 订阅安全模块配置变更，handler 会立即执行一次以同步当前状态。 */
export function bindSecurityModuleFeatureUi(onChange: (enabled: boolean) => void): () => void {
  const handler = (): void => onChange(securityModuleEnabled());
  window.addEventListener('security:config-change', handler);
  handler();
  return () => window.removeEventListener('security:config-change', handler);
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
