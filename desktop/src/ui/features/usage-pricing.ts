/**
 * 使用统计 · 成本定价配置
 *
 * 持久化：localStorage `crew.usage.pricing.v1`
 *
 * 与 usage-tracker 的关系：tracker 里的 lookupPrice() 优先查这里（按 modelId 精确匹配），
 * 找不到再回落到内置默认表。这样用户在面板里改了价格，所有未来回合的 cost 估算立刻生效。
 */

export interface PricingRow {
  id: string;
  modelId: string;
  /** USD per 1M tokens，方便与官方价格比对。 */
  inputPerMillion: number;
  outputPerMillion: number;
  cacheReadPerMillion: number;
  cacheWritePerMillion: number;
}

export const PRICING_KEY = 'crew.usage.pricing.v1';

/** 内置默认价格表（与 usage-tracker.ts 里的 PRICE_TABLE 同步） */
export const PRICING_DEFAULT: PricingRow[] = [
  { id: 'p_minimax',      modelId: 'minimax-latest',    inputPerMillion: 2.00,  outputPerMillion: 6.00,  cacheReadPerMillion: 0,     cacheWritePerMillion: 0 },
  { id: 'p_sonnet46',     modelId: 'claude-sonnet-4-6', inputPerMillion: 3.00,  outputPerMillion: 15.00, cacheReadPerMillion: 0.30,  cacheWritePerMillion: 3.75 },
  { id: 'p_haiku45',      modelId: 'claude-haiku-4-5',  inputPerMillion: 0.80,  outputPerMillion: 4.00,  cacheReadPerMillion: 0.08,  cacheWritePerMillion: 0 },
  { id: 'p_gpt4o_mini',   modelId: 'gpt-4o-mini',       inputPerMillion: 0.15,  outputPerMillion: 0.60,  cacheReadPerMillion: 0.075, cacheWritePerMillion: 0 },
  { id: 'p_gpt4o',        modelId: 'gpt-4o',            inputPerMillion: 2.50,  outputPerMillion: 10.00, cacheReadPerMillion: 1.25,  cacheWritePerMillion: 0 },
  { id: 'p_qwen_long',    modelId: 'qwen-long',         inputPerMillion: 0.50,  outputPerMillion: 2.00,  cacheReadPerMillion: 0,     cacheWritePerMillion: 0 },
];

let cache: PricingRow[] | null = null;
const listeners = new Set<() => void>();

function loadFromStorage(): PricingRow[] {
  try {
    const raw = localStorage.getItem(PRICING_KEY);
    if (!raw) return PRICING_DEFAULT.map((r) => ({ ...r }));
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return PRICING_DEFAULT.map((r) => ({ ...r }));
    // 合并默认：用户表里没有的默认项补回来（升级加新模型时）
    const byId = new Map<string, PricingRow>();
    for (const r of parsed) {
      if (r && typeof r === 'object' && r.id) byId.set(r.id, r as PricingRow);
    }
    const merged: PricingRow[] = [];
    for (const d of PRICING_DEFAULT) {
      merged.push(byId.get(d.id) ?? { ...d });
    }
    for (const r of byId.values()) {
      if (!PRICING_DEFAULT.find((d) => d.id === r.id)) merged.push(r);
    }
    return merged;
  } catch {
    return PRICING_DEFAULT.map((r) => ({ ...r }));
  }
}

function saveToStorage(rows: PricingRow[]): void {
  try {
    localStorage.setItem(PRICING_KEY, JSON.stringify(rows));
  } catch {
    /* quota */
  }
}

function getAll(): PricingRow[] {
  if (cache === null) cache = loadFromStorage();
  return cache;
}

function setAll(next: PricingRow[]): void {
  cache = next;
  saveToStorage(next);
  listeners.forEach((fn) => fn());
}

export function getPricingRows(): PricingRow[] {
  return getAll().slice();
}

export function upsertPricingRow(row: PricingRow): void {
  const all = getAll().slice();
  const idx = all.findIndex((r) => r.id === row.id);
  if (idx >= 0) all[idx] = row;
  else all.push(row);
  setAll(all);
}

export function deletePricingRow(id: string): void {
  const all = getAll().filter((r) => r.id !== id);
  setAll(all);
}

export function resetPricing(): void {
  setAll(PRICING_DEFAULT.map((r) => ({ ...r })));
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 供 usage-tracker 调用：按 modelId 查定价，找不到返回 null（落到默认表） */
export function lookupPricing(modelId: string): PricingRow | null {
  return getAll().find((r) => r.modelId === modelId) ?? null;
}
