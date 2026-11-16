/**
 * 真实 <App> 宿主生命周期测试工具（StrictMode 专用）。
 *
 * StrictMode 会让每个宿主发生两次额外动作，测试必须同时应对：
 * 1. 双渲染：每次渲染调用都会执行 useMemo 工厂、新建 FeatureEventRegistry，
 *   只有最后一次渲染调用的实例会被 commit —— 因此不能从捕获列表里取
 *   第一个 registry，只能取 mountAppHost 冲刷后的最后一个（即 commit 的那个）。
 * 2. 双加载：config effect 挂载→清理→重挂载，第一次 fetch 的响应在
 *   cancelled 清理后被丢弃 —— 因此每个宿主要入队两份相同 config，
 *   第一份喂给被丢弃的加载，第二份才是生效配置。
 */
import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import App from "../App";
import type { FeatureEventRegistry } from "../lib/feature-event-dispatcher";
import type { AppConfig } from "../types";

/** 各测试文件的 useChat / api.config mock 通过此对象接入共享状态。 */
export const appHostState = {
  /** useChat mock 按渲染顺序推入每个宿主的 registry（含被丢弃的渲染调用）。 */
  captured: [] as FeatureEventRegistry[],
  /** api.config mock 的响应队列。 */
  responses: [] as AppConfig[],
};

/** 为一个宿主入队两份相同 config：第一份被 StrictMode 丢弃的加载消费。 */
export function enqueueHostConfig(...configs: AppConfig[]): void {
  for (const config of configs) appHostState.responses.push(config, config);
}

export function resetAppHostState(): void {
  appHostState.captured.length = 0;
  appHostState.responses.length = 0;
}

const roots: Root[] = [];

/** 挂一个 StrictMode App 宿主，冲掉 config 微任务链，返回最终 commit 的 registry。 */
export async function mountAppHost(): Promise<{
  registry: FeatureEventRegistry;
  container: HTMLDivElement;
}> {
  const container = document.body.appendChild(document.createElement("div"));
  const root = createRoot(container);
  roots.push(root);
  await act(async () => {
    root.render(<StrictMode><App /></StrictMode>);
  });
  await act(async () => { await Promise.resolve(); });
  await act(async () => { await Promise.resolve(); });
  return { registry: appHostState.captured.at(-1)!, container };
}

export function unmountAppHosts(): void {
  roots.forEach((root) => root.unmount());
  roots.length = 0;
}
