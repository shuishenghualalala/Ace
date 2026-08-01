/**
 * Skill 单一数据源。
 *
 * composer 的 `/` 补全、工具栏 skills 浮层、技能页都依赖同一份后端技能列表。
 * 安装/卸载后由技能页调用 `invalidateSkills()` 使缓存失效，所有订阅方自动重载。
 */

import { backendApi, type Skill } from '../backend-client';

let cache: Skill[] | null = null;
let promise: Promise<Skill[]> | null = null;
const listeners = new Set<() => void>();

/** 订阅 skill 列表变化；返回取消订阅函数。 */
export function onSkillsChange(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** 使缓存失效并通知所有订阅方。 */
export function invalidateSkills(): void {
  cache = null;
  promise = null;
  listeners.forEach((cb) => cb());
}

/** 异步获取技能列表；结果会被缓存，直到 `invalidateSkills()` 被调用。 */
export async function getSkills(): Promise<Skill[]> {
  if (cache) return cache;
  if (!promise) {
    promise = backendApi.skills().then(
      (list) => {
        cache = list;
        return list;
      },
      (err) => {
        promise = null;
        throw err;
      },
    );
  }
  return promise;
}

/** 同步取当前缓存；未加载时返回空数组。 */
export function getCachedSkills(): Skill[] {
  return cache ?? [];
}

/** 预取技能列表；失败不抛错，由下次 `getSkills()` 重试。 */
export function prefetchSkills(): void {
  if (!cache) {
    void getSkills().catch(() => {
      // 首次未连后端时失败无妨，下次触发再试。
    });
  }
}
