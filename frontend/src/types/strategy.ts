export interface Strategy {
  id: number;
  account_id: number;
  name: string;
  direction: 'long' | 'short';
  symbol: string | null;
  signal_source: 'rsi' | 'wavetrend' | 'martingale_base';
  rsi_period: number;
  timeframe: string;
  wt_channel_length: number;
  wt_average_length: number;
  wt_ob_level: number;
  wt_os_level: number;
  margin_threshold: number;
  base_qty_type: 'margin_pct' | 'usdt';
  base_qty_value: number;
  rsi_entry_threshold: number;
  price_drop_pct: number;
  price_drop_multiplier: number;
  martingale_mult: number;
  max_layers: number;
  martingale_rsi_enabled: boolean;
  take_profit_pct: number;
  take_profit_limit_order: boolean;
  stop_loss_enabled: boolean;
  stop_loss_pct: number;
  single_symbol_stop_loss_enabled: boolean;
  single_symbol_stop_loss_pct: number;
  slippage_pct: number;
  leverage: number;
  use_coin_pool: boolean;
  coin_pool_source: 'gainers' | 'losers' | 'both';
  coin_pool_refresh_seconds: number;
  coin_pool_fetch_mode: 'immediate' | 'interval' | 'scheduled';
  coin_pool_anchor_hour: number;
  coin_pool_anchor_minute: number;
  coin_pool_top_n: number;
  /** 最低 24h 成交额(USDT)，0=不限制 */
  coin_pool_min_volume_24h: number;
  exclude_tradefi: boolean;
  exclude_delisting: boolean;
  exclude_mainstream: boolean;
  exclude_funding: boolean;
  funding_rate_threshold_pct: number;
  blacklisted_symbols: string[];
  status: 'running' | 'stopped' | 'error';
  started_at: string | null;
  last_rsi: number | null;
  last_signal: string | null;
  last_signal_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface StrategyFormData {
  account_id: number;
  name: string;
  direction: 'long' | 'short';
  symbol?: string;
  signal_source: 'rsi' | 'wavetrend' | 'martingale_base';
  rsi_period: number;
  timeframe: string;
  wt_channel_length: number;
  wt_average_length: number;
  wt_ob_level: number;
  wt_os_level: number;
  margin_threshold: number;
  base_qty_type: 'margin_pct' | 'usdt';
  base_qty_value: number;
  rsi_entry_threshold: number;
  price_drop_pct: number;
  price_drop_multiplier: number;
  martingale_mult: number;
  max_layers: number;
  martingale_rsi_enabled: boolean;
  take_profit_pct: number;
  take_profit_limit_order: boolean;
  stop_loss_enabled: boolean;
  stop_loss_pct: number;
  single_symbol_stop_loss_enabled: boolean;
  single_symbol_stop_loss_pct: number;
  slippage_pct: number;
  leverage: number;
  use_coin_pool: boolean;
  coin_pool_source: 'gainers' | 'losers' | 'both';
  coin_pool_refresh_hours: number;
  coin_pool_fetch_mode: 'immediate' | 'interval' | 'scheduled';
  /** 表单内 HH:mm，提交时拆为 hour + minute */
  coin_pool_anchor_time: string;
  coin_pool_top_n: number;
  coin_pool_min_volume_24h: number;
  exclude_tradefi: boolean;
  exclude_delisting: boolean;
  exclude_mainstream: boolean;
  exclude_funding: boolean;
  funding_rate_threshold_pct: number;
}

export type StrategyApiPayload = Omit<StrategyFormData, 'coin_pool_refresh_hours' | 'coin_pool_anchor_time'> & {
  coin_pool_refresh_seconds: number;
  coin_pool_anchor_hour: number;
  coin_pool_anchor_minute: number;
};

export function formatAnchorTime(hour?: number, minute?: number): string {
  return `${String(hour ?? 8).padStart(2, '0')}:${String(minute ?? 0).padStart(2, '0')}`;
}

export function parseAnchorTime(time: string): { hour: number; minute: number } {
  const [h, m] = time.split(':').map((v) => Number(v));
  return {
    hour: Number.isFinite(h) ? Math.min(23, Math.max(0, h)) : 8,
    minute: Number.isFinite(m) ? Math.min(59, Math.max(0, m)) : 0,
  };
}

export function formatSignalSourceLabel(
  source: Strategy['signal_source'],
  direction: Strategy['direction'],
  rsiEntryThreshold?: number,
): string {
  if (source === 'wavetrend') return 'WaveTrend';
  if (source === 'martingale_base') return '基础马丁';
  return `RSI ${direction === 'long' ? '<' : '>'} ${rsiEntryThreshold ?? ''}`;
}

export function formatSingleSymbolStopLoss(
  enabled?: boolean,
  pct?: number,
): string {
  if (enabled !== true) return '已禁用';
  return `钱包余额 ${pct ?? 10}%（触发后拉黑）`;
}

export function formatCoinPoolRefreshHours(seconds: number): string {
  const hours = seconds / 3600;
  return Number.isInteger(hours) ? `${hours}小时` : `${hours.toFixed(1)}小时`;
}

export function formatCoinPoolFetchMode(
  mode: Strategy['coin_pool_fetch_mode'],
  anchorHour?: number,
  anchorMinute?: number,
): string {
  if (mode === 'scheduled') {
    return `指定时间开选（${formatAnchorTime(anchorHour, anchorMinute)} 起）`;
  }
  if (mode === 'immediate') return '启动时立即抓取';
  return '按间隔开选';
}

export function formatLastSignalText(
  source: Strategy['signal_source'],
  lastSignal: string | null | undefined,
  lastRsi: number | null | undefined,
): string {
  const dir =
    lastSignal === 'long'
      ? '做多'
      : lastSignal === 'short'
        ? '做空'
        : lastSignal === 'neutral'
          ? '无信号'
          : lastSignal ?? '无信号';
  if (source === 'martingale_base') return `基础马丁 → ${dir}`;
  const metric = source === 'wavetrend' ? 'WT1' : 'RSI';
  return `${metric} ${lastRsi ?? '-'} → ${dir}`;
}
