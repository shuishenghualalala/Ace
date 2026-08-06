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
    // 兼容旧渲染组件的初始投影；真实登录态由主进程 auth:session-state
    // 在启动时同步，不能把这里当作认证来源。
    userInfo: { staffName: '本地账号', staffCode: 'local' },
    isLoggedIn: true,
  },
  'auth',
);
