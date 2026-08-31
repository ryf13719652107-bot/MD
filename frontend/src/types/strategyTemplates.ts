import type { StrategyFormData } from './strategy';

/** 用户自建参数模板；套用时不改名称 / 账户 */
export interface StrategyParamTemplate {
  id: number;
  name: string;
  patch: Partial<StrategyFormData>;
  created_at: string;
  updated_at: string;
}

const IDENTITY_KEYS = new Set(['account_id', 'name']);

export function snapshotTemplatePatch(data: StrategyFormData): Partial<StrategyFormData> {
  const out: Partial<StrategyFormData> = {};
  for (const [key, value] of Object.entries(data)) {
    if (IDENTITY_KEYS.has(key)) continue;
    (out as Record<string, unknown>)[key] = value;
  }
  return out;
}
