import type { AuthUserSnapshot } from '../shared/types';

export type GatewayIdentityMode = 'local' | 'dev';

/** Resolve the Gateway identity used for this Desktop process lifetime. */
export function resolveGatewayIdentityMode(
  _isDevLaunch: boolean,
  _jwt?: string | null,
  _userInfo?: AuthUserSnapshot | null,
): GatewayIdentityMode {
  // Ace has one local identity. `--dev` controls renderer/build behavior only;
  // isolating Gateway state here prevents developers from reusing a manually
  // started local Gateway and silently drops their normal model configuration.
  return 'local';
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
 * 统一指向真实 crew home（config 的 ~/.Crew），避免 dev 模式把数据隔离到空目录。
 */
export function resolveGatewayCrewHome(
  mode: GatewayIdentityMode,
  accountHome: string,
  devHome: string,
): string {
  return mode === 'dev' ? devHome : accountHome;
}

/** Only an account-mode Desktop may reuse a Gateway with unknown dev settings. */
export function shouldProbeExternalGateway(mode: GatewayIdentityMode): boolean {
  return mode === 'local';
}
