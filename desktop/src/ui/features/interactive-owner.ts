import { state } from '../state';

export function interactiveOwnerKey(): string {
  const user = state.isLoggedIn ? state.userInfo : null;
  if (!user || !(user.staffUid || user.uid || user.pid || user.staffCode)) return '';
  return JSON.stringify([user.staffUid, user.uid, user.pid, user.staffCode]);
}
