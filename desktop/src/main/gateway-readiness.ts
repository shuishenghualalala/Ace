import type { GatewayInstanceProbe, GatewayStartupReadiness } from './gateway-instance-auth';

export interface DesktopGatewayReadiness {
  status: 'starting' | 'ready' | 'failed';
  generation: number;
  errorCode?: string;
}

export class GatewayStartupError extends Error {
  constructor(readonly code: string) {
    super(`Gateway startup failed (${code})`);
    this.name = 'GatewayStartupError';
  }
}

export function readinessFromMainProbe(
  probe: GatewayInstanceProbe, managed: boolean,
): GatewayStartupReadiness | null | undefined {
  if (!probe.verified) return undefined;
  if (probe.readiness) return probe.readiness;
  // A previously installed standalone Gateway may implement the old health
  // endpoint. An owned process is launched from the same release as Desktop.
  if (!managed) return null;
  throw new GatewayStartupError('GATEWAY_READINESS_UNAVAILABLE');
}

export class GatewayReadinessTracker {
  private state: DesktopGatewayReadiness = { status: 'starting', generation: 0 };

  get snapshot(): DesktopGatewayReadiness {
    return this.state;
  }

  reset(generation: number): void {
    this.state = { status: 'starting', generation };
  }

  observe(generation: number, status: DesktopGatewayReadiness['status'], errorCode?: string): boolean {
    if (generation !== this.state.generation || this.state.status === 'failed') return false;
    if (this.state.status === status && this.state.errorCode === errorCode) return false;
    if (this.state.status === 'ready' && status === 'starting') return false;
    this.state = { status, generation, ...(errorCode ? { errorCode } : {}) };
    return true;
  }
}
