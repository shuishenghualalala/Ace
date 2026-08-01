import type { AppConfig } from "../types";

/** 只有 Gateway 明确返回 false 时才关闭外援；加载中或暂时断连不降级为关闭。 */
export function externalAgentsAvailable(config: AppConfig | null): boolean {
  return config?.external_agents?.enabled !== false;
}
