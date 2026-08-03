import type { Account, Position, Trade, DashboardData, CoinPoolEntry, KlineData, EquitySeriesData } from '../types';
import type { Strategy, StrategyFormData } from '../types/strategy';

// Same-origin `/api`: Vite dev server proxies to backend; production is served by FastAPI with API on the same host (also works behind reverse proxy on 80/443).
const BASE = '/api';
const WRITE_TOKEN_KEY = 'martin_write_token';

/** owner 登录密码 → X-Write-Token；与 backend API_WRITE_TOKEN（同 owner 密码）一致 */
function getWriteToken(): string {
  try {
    const fromSession = sessionStorage.getItem(WRITE_TOKEN_KEY);
    if (fromSession?.trim()) return fromSession.trim();
    if (sessionStorage.getItem('martin_ui_role') === 'owner') {
      return (import.meta.env.VITE_UI_OWNER_PASSWORD ?? '').trim();
    }
  } catch {
    /* ignore */
  }
  if (import.meta.env.VITE_UI_AUTH_DISABLED === 'true') {
    return (import.meta.env.VITE_UI_OWNER_PASSWORD ?? '').trim();
  }
  return (import.meta.env.VITE_API_WRITE_TOKEN ?? '').trim();
}

function writeHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json', ...extra };
  const token = getWriteToken();
  if (token) h['X-Write-Token'] = token;
  return h;
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: writeHeaders(options?.headers as Record<string, string> | undefined),
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const d = err.detail;
    const msg =
      typeof d === 'string'
        ? d
        : Array.isArray(d)
          ? d.map((x: any) => (x?.msg ?? JSON.stringify(x))).join('; ')
          : d != null
            ? JSON.stringify(d)
            : res.statusText;
    throw new Error(msg || 'Request failed');
  }
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

export const api = {
  // Accounts
  createAccount: (data: {
    name: string;
    api_key: string;
    api_secret: string;
    exchange?: 'binance' | 'gate';
    testnet: boolean;
    hedge_mode: boolean;
  }): Promise<Account> =>
    request<Account>('/accounts', { method: 'POST', body: JSON.stringify(data) }),
  listAccounts: (): Promise<Account[]> => request<Account[]>('/accounts'),
  deleteAccount: (id: number): Promise<void> => request<void>(`/accounts/${id}`, { method: 'DELETE' }),
  purgeSpamAccounts: (): Promise<{ deleted_count: number; deleted: { id: number; name: string }[] }> =>
    request('/accounts/purge-spam', { method: 'POST' }),

  // Strategies
  createStrategy: (data: StrategyFormData): Promise<Strategy> =>
    request<Strategy>('/strategies', { method: 'POST', body: JSON.stringify(data) }),
  listStrategies: (status?: string, accountId?: number): Promise<Strategy[]> => {
    const qs = new URLSearchParams();
    if (status) qs.set('status', status);
    if (accountId != null) qs.set('account_id', String(accountId));
    const q = qs.toString();
    return request<Strategy[]>(`/strategies${q ? `?${q}` : ''}`);
  },
  getStrategy: (id: number): Promise<Strategy> => request<Strategy>(`/strategies/${id}`),
  updateStrategy: (id: number, data: StrategyFormData): Promise<Strategy> =>
    request<Strategy>(`/strategies/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteStrategy: (id: number): Promise<void> => request<void>(`/strategies/${id}`, { method: 'DELETE' }),
  startStrategy: (id: number): Promise<{ status: string }> =>
    request(`/strategies/${id}/start`, { method: 'POST' }),
  stopStrategy: (id: number): Promise<{ status: string }> =>
    request(`/strategies/${id}/stop`, { method: 'POST' }),
  panicCloseStrategy: (id: number): Promise<{ closed: number; failed: number; results: Array<{ symbol: string; side: string; status: string; exit_price?: number; error?: string }> }> =>
    request(`/strategies/${id}/panic-close`, { method: 'POST' }),
  getStrategyLogs: (id: number, limit?: number): Promise<{ time: string; level: string; message: string }[]> =>
    request(`/strategies/${id}/logs${limit ? `?limit=${limit}` : ''}`),
  getExchangePositions: (id: number): Promise<{ symbol: string; side: string; usdt: number; entry_price: number; mark_price: number; unrealized_pnl: number; pnl_pct: number }[]> =>
    request(`/strategies/${id}/exchange-positions`),
  addStrategyBlacklistSymbol: (id: number, symbol: string): Promise<Strategy> =>
    request<Strategy>(`/strategies/${id}/blacklist`, {
      method: 'POST',
      body: JSON.stringify({ symbol }),
    }),
  removeStrategyBlacklistSymbol: (id: number, symbol: string): Promise<Strategy> =>
    request<Strategy>(`/strategies/${id}/blacklist/${encodeURIComponent(symbol)}`, {
      method: 'DELETE',
    }),

  // Positions
  listPositions: (params?: { strategy_id?: number; symbol?: string; account_id?: number }): Promise<Position[]> => {
    const qs = new URLSearchParams();
    if (params?.strategy_id) qs.set('strategy_id', String(params.strategy_id));
    if (params?.symbol) qs.set('symbol', params.symbol);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    const q = qs.toString();
    return request<Position[]>(`/positions${q ? `?${q}` : ''}`);
  },
  closePosition: (id: number): Promise<{ status: string }> =>
    request(`/positions/${id}/close`, { method: 'POST' }),

  // Trades
  listTrades: (params?: {
    strategy_id?: number;
    symbol?: string;
    side?: 'long' | 'short';
    account_id?: number;
    limit?: number;
    offset?: number;
  }): Promise<{ trades: Trade[]; total: number }> => {
    const qs = new URLSearchParams();
    if (params?.strategy_id) qs.set('strategy_id', String(params.strategy_id));
    if (params?.symbol?.trim()) qs.set('symbol', params.symbol.trim());
    if (params?.side) qs.set('side', params.side);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    if (params?.limit != null && params.limit > 0) qs.set('limit', String(params.limit));
    if (params?.offset != null && params.offset >= 0) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return request(`/trades${q ? `?${q}` : ''}`);
  },
  deleteTrade: (id: number): Promise<void> => request(`/trades/${id}`, { method: 'DELETE' }),
  deleteAllTrades: (accountId: number): Promise<void> =>
    request(`/trades?account_id=${accountId}`, { method: 'DELETE' }),
  restoreTrades: (accountId: number): Promise<{ restored: number; skipped: number; total: number; account_id: number; invalid?: number }> =>
    request(`/trades/restore?account_id=${accountId}`, { method: 'POST' }),
  getBackupStats: (accountId: number): Promise<{ count: number; size_bytes: number; path: string; account_id: number }> =>
    request(`/trades/backup-stats?account_id=${accountId}`),

  // Dashboard
  getDashboard: (accountId?: number): Promise<DashboardData> =>
    request<DashboardData>(`/dashboard${accountId ? `?account_id=${accountId}` : ''}`),

  getEquitySeries: (accountId: number, days = 30): Promise<EquitySeriesData> =>
    request<EquitySeriesData>(`/equity/series?account_id=${accountId}&days=${days}`),
  resetEquityBaseline: (
    accountId: number,
  ): Promise<{ ok: boolean; deleted_snapshots: number; message: string }> =>
    request(`/equity/baseline-reset?account_id=${accountId}`, { method: 'POST' }),

  // Klines
  getKlines: (symbol: string, timeframe = '1m', limit = 200): Promise<KlineData[]> =>
    request<KlineData[]>(`/klines?symbol=${symbol}&timeframe=${timeframe}&limit=${limit}`),

  // Ticker
  getTicker: (symbol: string): Promise<{ symbol: string; last: number; change_pct: number; high_24h: number; low_24h: number; volume_24h: number }> =>
    request(`/ticker?symbol=${symbol}`),

  // Coin pool
  getCoinPool: (source?: string, strategyId?: number, exchange?: string): Promise<CoinPoolEntry[]> => {
    const params = new URLSearchParams();
    if (source) params.set('source', source);
    if (strategyId != null) params.set('strategy_id', String(strategyId));
    if (exchange) params.set('exchange', exchange);
    const query = params.toString();
    return request<CoinPoolEntry[]>(`/coin-pool${query ? `?${query}` : ''}`);
  },
  /** 与调度器一致：按该策略成交量 / top_n / TradFi 过滤后的可开仓列表 */
  getStrategyEffectiveCoinPool: (strategyId: number): Promise<CoinPoolEntry[]> =>
    request<CoinPoolEntry[]>(`/strategies/${strategyId}/effective-coin-pool`),
  refreshCoinPool: (exchange?: string): Promise<{ status: string; message: string }> => {
    const q = exchange ? `?exchange=${encodeURIComponent(exchange)}` : '';
    return request(`/coin-pool/refresh${q}`, { method: 'POST' });
  },
  getCoinPoolConfig: (exchange?: string): Promise<{ refresh_interval_seconds: number; pool_source: string; max_symbols: number }> => {
    const q = exchange ? `?exchange=${encodeURIComponent(exchange)}` : '';
    return request(`/coin-pool/config${q}`);
  },
  updateCoinPoolConfig: (
    data: any,
    exchange?: string,
  ): Promise<{ refresh_interval_seconds: number; pool_source: string; max_symbols: number }> => {
    const q = exchange ? `?exchange=${encodeURIComponent(exchange)}` : '';
    return request(`/coin-pool/config${q}`, { method: 'PUT', body: JSON.stringify(data) });
  },
  testFetchCoinPool: (exchange?: string): Promise<{ success: boolean; count: number; data: any[]; message: string }> => {
    const q = exchange ? `?exchange=${encodeURIComponent(exchange)}` : '';
    return request(`/coin-pool/test-fetch${q}`, { method: 'POST' });
  },

  // Bot toggle
  toggleBot: (enabled: boolean): Promise<{ master_switch: boolean }> =>
    request('/bot/toggle', { method: 'POST', body: JSON.stringify({ enabled }) }),

  // Wick spike log stats
  analyzeWickStats: (params?: {
    progress_min?: number;
    list_limit?: number;
    include_rotated?: boolean;
    enrich_opens?: boolean;
    max_enrich?: number;
  }): Promise<WickStatsAnalysis> => {
    const qs = new URLSearchParams();
    if (params?.progress_min != null) qs.set('progress_min', String(params.progress_min));
    if (params?.list_limit != null) qs.set('list_limit', String(params.list_limit));
    if (params?.include_rotated != null) qs.set('include_rotated', String(params.include_rotated));
    if (params?.enrich_opens != null) qs.set('enrich_opens', String(params.enrich_opens));
    if (params?.max_enrich != null) qs.set('max_enrich', String(params.max_enrich));
    const q = qs.toString();
    return request<WickStatsAnalysis>(`/wick-stats/analyze${q ? `?${q}` : ''}`);
  },
};

export type WickStatsSummary = {
  n: number;
  min?: number;
  p25?: number;
  p50?: number;
  p75?: number;
  p90?: number;
  max?: number;
  mean?: number;
};

export type WickStatsDeepRow = {
  ts: string;
  strategy_id: number;
  symbol: string;
  direction: string;
  direction_zh?: string;
  progress: number;
  vol_x: number;
  need_x: number;
  vol_shortfall: number;
  tip_gap_pct: number | null;
  px: number;
  open: number;
  ext: number;
};

export type WickStatsOpenRow = {
  ts: string;
  strategy_id: number;
  symbol: string;
  side: string;
  side_zh: string;
  entry_px: number | null;
  bar_open: number | null;
  tip_gap_at_trigger_pct: number | null;
  final_tip_gap_pct: number | null;
  wick_range_pct: number | null;
  capture_ratio: number | null;
  trade_id: number | null;
  realized_pnl: number | null;
  pnl_pct: number | null;
  close_reason: string | null;
  layer: number | null;
  matched: boolean;
  kline_ok: boolean;
  note: string;
};

export type WickStatsPnlBucket = {
  bucket: string;
  n: number;
  win_rate?: number;
  avg_pnl_pct?: number;
  median_pnl_pct?: number;
  avg_final_tip_gap_pct?: number;
};

export type WickStatsOpenQuality = {
  n: number;
  matched_trades?: number;
  kline_ok?: number;
  error?: string;
  final_tip_gap?: WickStatsSummary;
  capture_ratio?: WickStatsSummary;
  pnl_pct?: WickStatsSummary;
  pnl_by_tip_bucket?: WickStatsPnlBucket[];
  rows?: WickStatsOpenRow[];
  text?: string;
};

export type WickStatsAnalysis = {
  ok: boolean;
  error?: string | null;
  log_files?: string[];
  near_miss_total: number;
  entry_total: number;
  opened_total?: number;
  trigger_total?: number;
  block_reasons: Record<string, number>;
  tip_gap_opened?: WickStatsSummary;
  tip_gap_trigger?: WickStatsSummary;
  trade_age_ms?: WickStatsSummary;
  detect_to_lock_ms?: WickStatsSummary;
  open_api_db_ms?: WickStatsSummary;
  speed_labels?: Record<string, string>;
  vol_blocked_deep?: {
    progress_min: number;
    count: number;
    listed: number;
    vol_summary: WickStatsSummary;
    shortfall_summary: WickStatsSummary;
    progress_summary: WickStatsSummary;
    counterfactual: {
      need_5_pass: number;
      need_4_5_pass: number;
      total: number;
    };
    rows: WickStatsDeepRow[];
  };
  open_quality?: WickStatsOpenQuality | null;
  text: string;
};
