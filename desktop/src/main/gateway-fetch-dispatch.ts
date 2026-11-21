type GatewayFetch = (url: string, init: RequestInit) => Promise<Response>;

export async function dispatchRendererGatewayFetch(
  target: URL,
  init: RequestInit,
  scopeIsCurrent: () => boolean,
  startupFetch: GatewayFetch,
): Promise<Response> {
  // Gateway routing sees escaped ASCII decoded. Non-ASCII belongs only to the session ID.
  const route = target.pathname.replace(/%([0-7][0-9a-f])/giu, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
  const goalWrite = init.method?.toUpperCase() === 'POST' && /^\/api\/session\/[^/]+\/goal\/?(?![\s\S])/u.test(route);
  if (!goalWrite) return startupFetch(target.href, init);
  if (!scopeIsCurrent()) throw new Error('目标操作未提交：窗口、账号或网关已切换，请重新读取目标。');
  // Neither a startup retry nor a 307/308 redirect may re-send a Goal write.
  return fetch(target.href, { ...init, redirect: 'error' });
}
