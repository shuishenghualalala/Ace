/**
 * Renderer-side login gates for local desktop workflows.
 *
 * Ace has no login screen — the local desktop is always usable, so these
 * guards are permanent no-ops that report logged-in.
 */
// Ace 开源桩：无登录墙，恒为已登录。
export function isRendererLoggedIn(): boolean {
  return true;
}

export function requireRendererLogin(_message = '请先登录'): boolean {
  return true;
}
