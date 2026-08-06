/**
 * Renderer-side login gates for local desktop workflows.
 *
 * The login wall and main-process Gateway enforce email/remote auth. These
 * legacy call sites remain no-ops until their individual flows are migrated.
 */
export function isRendererLoggedIn(): boolean {
  return true;
}

export function requireRendererLogin(_message = '请先登录'): boolean {
  return true;
}
