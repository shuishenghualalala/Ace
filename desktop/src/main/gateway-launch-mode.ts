export type GatewayIdentityMode = 'local' | 'dev';

/** Resolve the Gateway identity used for this Desktop process lifetime. */
export function resolveGatewayIdentityMode(
  isDevLaunch: boolean,
): GatewayIdentityMode {
  // 开源桌面端不依赖远程账号：普通启动固定使用本地 owner，`--dev`
  // 继续使用隔离的开发 home，避免开发数据污染正常工作区。
  return isDevLaunch ? 'dev' : 'local';
}

/** Return the child-process overrides required by the selected identity mode. */
export function managedGatewayModeEnv(
  mode: GatewayIdentityMode,
  devHome: string,
): Record<string, string> {
  return mode === 'dev'
    ? { CREW_GATEWAY_DEV: '1', CREW_HOME: devHome }
    : {};
}

/**
 * Resolve the CREW_HOME shared by the Desktop verifier and its Gateway.
 * Dev fallback is intentionally isolated, but both processes must read the
 * instance-auth key from that same isolated home.
 */
export function resolveGatewayCrewHome(
  mode: GatewayIdentityMode,
  accountHome: string,
  devHome: string,
): string {
  return mode === 'dev' ? devHome : accountHome;
}

/** Normal local mode may reuse a verified Gateway; dev mode stays isolated. */
export function shouldProbeExternalGateway(mode: GatewayIdentityMode): boolean {
  return mode === 'local';
}
