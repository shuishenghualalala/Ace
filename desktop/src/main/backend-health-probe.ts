import {
  probeGatewayInstance,
  type GatewayInstanceProbe,
  type GatewayInstanceVerificationOptions,
} from './gateway-instance-auth';

/** BackendHealthMonitor 探针结果：在 GatewayInstanceProbe 之上附带 loop_lag_ms。 */
export interface BackendHealthProbe extends GatewayInstanceProbe {
  /** 仅 health 线程端口路径携带：业务事件循环滞后（毫秒），主端口回退路径不带。 */
  loopLagMs?: number;
}

/**
 * 由主端口推导 health 线程端口（主端口 + 1）。URL 非法或没有显式端口
 * （80/443 默认端口无法推导 +1）时返回 null，调用方直接探测主端口。
 */
export function healthPortUrlFor(baseUrl: string): string | null {
  try {
    const parsed = new URL(baseUrl);
    if (parsed.port === '') return null;
    parsed.port = String(Number(parsed.port) + 1);
    // 只保留 origin + 新端口：探测路径由 probeGatewayInstance 统一补 /api/health。
    return `${parsed.origin}/`;
  } catch {
    return null;
  }
}

/** 包一层 fetchImpl：透传响应，同时从 health 线程端口响应里摘出 loop_lag_ms。 */
function captureLoopLagFetch(
  fetchImpl: typeof fetch,
  onLag: (lagMs: number) => void,
): typeof fetch {
  return (async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    const response = await fetchImpl(input, init);
    if (response.ok) {
      const body = (await response.clone().json().catch(() => null)) as unknown;
      if (body && typeof body === 'object') {
        const lag = (body as Record<string, unknown>).loop_lag_ms;
        if (typeof lag === 'number' && Number.isFinite(lag)) onLag(lag);
      }
    }
    return response;
  }) as typeof fetch;
}

/**
 * 后端健康探测：优先打 health 线程端口（主端口 + 1，与业务事件循环隔离，
 * 循环卡超时仍能感知进程活性并返回 loop_lag_ms）；验证失败（旧 gateway 没有
 * 独立 health 线程，端口不可达或被占用）再回退主端口 /api/health 探测。
 * probeGatewayInstance 原样复用，challenge/proof 逻辑不在这里复制。
 */
export async function probeBackendHealth(
  baseUrl: string,
  options: GatewayInstanceVerificationOptions = {},
): Promise<BackendHealthProbe> {
  const healthUrl = healthPortUrlFor(baseUrl);
  if (healthUrl) {
    let loopLagMs: number | undefined;
    const result = await probeGatewayInstance(healthUrl, {
      ...options,
      fetchImpl: captureLoopLagFetch(options.fetchImpl ?? fetch, (lag) => {
        loopLagMs = lag;
      }),
    });
    if (result.verified) {
      return loopLagMs === undefined ? { ...result } : { ...result, loopLagMs };
    }
  }
  return probeGatewayInstance(baseUrl, options);
}
