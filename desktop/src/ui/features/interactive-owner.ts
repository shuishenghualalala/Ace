import { authStore } from '../stores/auth-store';

export function interactiveOwnerKey(): string {
  const auth = authStore.get();
  const user = auth.isLoggedIn ? auth.userInfo : null;
  if (!user || !(user.staffUid || user.uid || user.pid || user.staffCode)) return '';
  return JSON.stringify([user.staffUid, user.uid, user.pid, user.staffCode]);
}
