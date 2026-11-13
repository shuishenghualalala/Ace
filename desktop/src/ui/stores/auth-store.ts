/**
 * auth-store：登录态 / 用户信息。凭据只由 Electron 主进程持有。
 */
import { createStore, type Store } from '../reducers/store-bus';
import type { UserInfo } from '../state';

export interface AuthStoreState {
  userInfo: UserInfo | null;
  isLoggedIn: boolean;
}

export const authStore: Store<AuthStoreState> = createStore<AuthStoreState>(
  {
    // 启动时先按未登录处理，等待主进程 auth:get-state 完成同步。
    // 否则首屏可能在 Cookie 尚未恢复前就向 Gateway 发起受保护请求。
    userInfo: null,
    isLoggedIn: false,
  },
  'auth',
);
