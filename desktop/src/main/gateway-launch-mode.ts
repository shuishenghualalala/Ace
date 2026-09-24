import type { AuthUserSnapshot } from '../shared/types';

export type GatewayIdentityMode = 'local' | 'dev';

/** Resolve the Gateway identity used for this Desktop process lifetime. */
export function resolveGatewayIdentityMode(
  isDevLaunch: boolean,
  _jwt?: string | null,
  _userInfo?: AuthUserSnapshot | null,
): GatewayIdentityMode {
  return isDevLaunch ? 'dev' : 'local';
}

/** Return the child-process overrides required by the selected identity mode. */
export function managedGatewayModeEnv(
  mode: GatewayIdentityMode,
  devHome: string,
): Record<string, string> {
  return mode === 'dev'
    ? {
        CREW_GATEWAY_DEV: '1',
        CREW_HOME: devHome,
        // These flags are injected by the trusted main process, never by the
        // renderer or by a URL/localStorage value.  The Gateway reads them as
        // an independent diagnostics capability; gateway_dev_mode remains an
        // authentication concern.
        CREW_OBSERVABILITY_ENABLED: '1',
        CREW_OBSERVABILITY_DEVELOPER_ACCESS: '1',
        CREW_OBSERVABILITY_CAPTURE_PROFILE: 'content_redacted',
      }
    : {
        // A managed fallback in an ordinary launch must not inherit a
        // developer-access setting from a shared config file.  External
        // Gateways are never spawned with this environment and therefore are
        // not modified by Desktop.
        CREW_OBSERVABILITY_DEVELOPER_ACCESS: '0',
        CREW_OBSERVABILITY_CAPTURE_PROFILE: 'metadata',
      };
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
