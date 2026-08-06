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
    // 登录逻辑已移除：Ace 恒为已登录的本地账号，账户面板直接展示，
    // 不再因 userInfo 为 null 恒显「尚未登录」。身份由后端 local 模式 loopback 放行。
    userInfo: { staffName: '本地账号', staffCode: 'local' },
    isLoggedIn: true,
  },
  'auth',
);
