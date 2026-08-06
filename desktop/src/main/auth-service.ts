/**
 * 认证服务（Ace 开源桩）。
 *
 * Ace 无远程 SSO：本地桌面直接可用，恒为「已登录、无远程凭据」状态。
 * 仅保留 main 进程实际调用的方法签名，移除全部远程鉴权 / 心跳 / 验证码网络逻辑
 * 与内部鉴权主机引用。
 *
 * 调用方（主进程）仍用 loginNewServiceInstance.getSessionInfo() 读取登录态，
 * 恒返回 isLoggedIn=true，使 renderer 无需登录墙即可使用。
 */
import type { UserInfoSnapshot, VersionUpdatePayload } from '../shared/types';

export type VersionUpdateCallback = (payload: VersionUpdatePayload) => void;
export type SessionExpiredCallback = () => void | Promise<void>;

/**
 * 登录服务（桩）：所有方法为空实现 / 恒定返回。
 * 保留 main/index.ts 调用的方法签名，行为上等价于「始终已登录、无凭据、无网络」。
 */
export class LoginNewService {
  /** Ace 恒已登录；本地桌面无账号概念，userInfo 为空。 */
  getSessionInfo(): { isLoggedIn: boolean; userInfo: UserInfoSnapshot | null } {
    return { isLoggedIn: true, userInfo: null };
  }

  getJWTToken(): string | null {
    return null;
  }

  // Ace 无远程 SSO：严格安全约束由本地偏好（desktop-prefs）+ gateway 启动 env
  // （ACE_STRICT_SECURITY）+ gateway 回收重启强制，无需远程下发。此方法保留签名
  // 供主进程在 toggle / 启动时调用，行为上为空实现（真正的生效逻辑在 IPC handler
  // 的 saveStrictSecurityPreference + recycleGatewayForSecurityChange 里）。
  setStrictSecurityEnabled(_enabled: boolean): void {}

  setSessionExpiredHandler(_cb: SessionExpiredCallback | null): void {}

  setVersionUpdateHandler(_cb: VersionUpdateCallback | null): void {}

  async heartbeat(_version?: string): Promise<{ success: boolean }> {
    return { success: true };
  }

  stopHeartbeat(): void {}

  clearJWTToken(): void {}
}

export const loginNewServiceInstance = new LoginNewService();
