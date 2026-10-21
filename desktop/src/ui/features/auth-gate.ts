/** Renderer-side login gates shared by features that call the Gateway. */
import { notify } from '../state';
import { authStore } from '../stores/auth-store';

export function isRendererLoggedIn(): boolean {
  return authStore.get().isLoggedIn;
}

export function requireRendererLogin(message = '请先登录'): boolean {
  if (isRendererLoggedIn()) return true;
  if (message) notify(message);
  return false;
}
