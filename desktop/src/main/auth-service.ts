/**
 * 认证服务（Ace email / remote 租户认证）。
 *
 * 委托 DesktopAuthSession（cookie-based，`crew_auth_session`），与 Gateway 的
 * `_remote_account_from_cookie` 直接兼容。LoginNewService 保留为主进程统一门面，
 * 旧调用点（getSessionInfo / heartbeat / setStrictSecurityEnabled …）继续可用。
 *
 * 适配说明：HEAD 的 index.ts 旧调用点用 `getJWTToken()` + `Authorization: Bearer`
 * 鉴权，但 Gateway 实际只读 `crew_auth_session` cookie。这里把 `getJWTToken()`
 * 适配为返回 cookie 串（仅用于真值判断与兼容旧 Bearer 行；Gateway 忽略 Bearer），
 * 真正的鉴权由 `gatewayIdentityHeaders()` 返回的 `Cookie` 头完成。远程已登录时
 * 二者同时非空，旧代码里 `if (!jwt || !identityHeaders) return 401` 的判断仍然成立。
 */
import type { AuthStateSnapshot, AuthUserSnapshot, VersionUpdatePayload } from '../shared/types';
import { desktopAuthSession } from './auth-session';

export type VersionUpdateCallback = (payload: VersionUpdatePayload) => void;
export type SessionExpiredCallback = () => void | Promise<void>;

export class LoginNewService {
  setGatewayProofProvider(
    provider: (method: string, pathname: string, body: string) => string,
  ): void {
    desktopAuthSession.setGatewayProofProvider(provider);
  }

  /** 当前认证态（mode / configured / providerId / isLoggedIn / user）。 */
  getState(): AuthStateSnapshot {
    return desktopAuthSession.state();
  }

  /** 远程/email 模式下作为请求 Cookie 头的值（`crew_auth_session=…`）；local 模式返回空串。 */
  cookieHeader(): string {
    return desktopAuthSession.cookieHeader();
  }

  /** 当前 owner 账号 ID（browser owner / gateway identity）。 */
  ownerAccountId(): string | null {
    return desktopAuthSession.ownerAccountId();
  }

  /** 兼容旧调用：返回 { isLoggedIn, userInfo }。userInfo 现为 AuthUserSnapshot。 */
  getSessionInfo(): { isLoggedIn: boolean; userInfo: AuthUserSnapshot | null } {
    const state = desktopAuthSession.state();
    return { isLoggedIn: state.isLoggedIn, userInfo: state.user };
  }

  /**
   * 适配旧调用：返回 cookie 串（真值 = 远程已登录）。Gateway 不读 Bearer，
   * 鉴权靠 gatewayIdentityHeaders() 返回的 Cookie 头；此处返回值仅用于旧真值判断
   * 与被 Gateway 忽略的 `Authorization: Bearer` 行。
   */
  getJWTToken(): string | null {
    const cookie = desktopAuthSession.cookieHeader();
    return cookie || null;
  }

  async refreshConfig(baseUrl: string): Promise<AuthStateSnapshot> {
    return desktopAuthSession.refreshConfig(baseUrl);
  }

  async loginWithEmail(baseUrl: string, email: string): Promise<Record<string, unknown>> {
    return desktopAuthSession.loginWithEmail(baseUrl, email);
  }

  async login(baseUrl: string, phoneNumber: string, code: string): Promise<Record<string, unknown>> {
    return desktopAuthSession.login(baseUrl, phoneNumber, code);
  }

  async sendCode(baseUrl: string, phoneNumber: string): Promise<Record<string, unknown>> {
    return desktopAuthSession.sendCode(baseUrl, phoneNumber);
  }

  async logout(baseUrl: string): Promise<Record<string, unknown>> {
    return desktopAuthSession.logout(baseUrl);
  }

  // -- 生命周期钩子（保留签名供主进程调用；行为为空，真正逻辑在 IPC handler 侧）--
  setStrictSecurityEnabled(enabled: boolean): void {
    if (!enabled) throw new Error('strict security cannot be disabled');
  }
  setSessionExpiredHandler(_cb: SessionExpiredCallback | null): void {}
  setVersionUpdateHandler(_cb: VersionUpdateCallback | null): void {}
  async heartbeat(_version?: string): Promise<{ success: boolean }> {
    return { success: true };
  }
  stopHeartbeat(): void {}
  clearJWTToken(): void {}
}

export const loginNewServiceInstance = new LoginNewService();
