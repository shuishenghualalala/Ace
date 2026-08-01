/**
 * 会话模式重置工具（纯状态，不依赖 DOM）。
 * 供 cron 新建会话等场景把全局 composer 模式切回智能体默认。
 */
import { state } from '../state';

/** 把全局模式重置为智能体默认（agent + craft）。 */
export function resetToAgentMode(): void {
  state.mode = 'agent';
  state.composerMode = 'craft';
}
