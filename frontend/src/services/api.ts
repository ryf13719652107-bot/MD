import type { Account, Position, Trade, DashboardData, CoinPoolEntry, KlineData } from '../types';
import type { Strategy, StrategyFormData } from '../types/strategy';

const BASE = '/api';

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  if (res.status === 204) return undefined as unknown as T;
  return res.json() as Promise<T>;
}

export const api = {
  // Accounts
  createAccount: (data: { name: string; api_key: string; api_secret: string; testnet: boolean; hedge_mode: boolean }): Promise<Account> =>
    request<Account>('/accounts', { method: 'POST', body: JSON.stringify(data) }),
  listAccounts: (): Promise<Account[]> => request<Account[]>('/accounts'),
  deleteAccount: (id: number): Promise<void> => request<void>(`/accounts/${id}`, { method: 'DELETE' }),

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
  panicCloseStrategy: (id: number): Promise<{ closed: number; errors: string[] }> =>
    request(`/strategies/${id}/panic-close`, { method: 'POST' }),
  getStrategyLogs: (id: number, limit?: number): Promise<{ time: string; level: string; message: string }[]> =>
    request(`/strategies/${id}/logs${limit ? `?limit=${limit}` : ''}`),
  getExchangePositions: (id: number): Promise<{ symbol: string; side: string; usdt: number; entry_price: number; mark_price: number; unrealized_pnl: number; pnl_pct: number }[]> =>
    request(`/strategies/${id}/exchange-positions`),

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
  listTrades: (params?: { strategy_id?: number; symbol?: string; account_id?: number; limit?: number; offset?: number }): Promise<{ trades: Trade[]; total: number }> => {
    const qs = new URLSearchParams();
    if (params?.strategy_id) qs.set('strategy_id', String(params.strategy_id));
    if (params?.symbol) qs.set('symbol', params.symbol);
    if (params?.account_id != null) qs.set('account_id', String(params.account_id));
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    const q = qs.toString();
    return request(`/trades${q ? `?${q}` : ''}`);
  },
  deleteTrade: (id: number): Promise<void> => request(`/trades/${id}`, { method: 'DELETE' }),
  deleteAllTrades: (): Promise<void> => request('/trades', { method: 'DELETE' }),

  // Dashboard
  getDashboard: (accountId?: number): Promise<DashboardData> =>
    request<DashboardData>(`/dashboard${accountId ? `?account_id=${accountId}` : ''}`),

  // Klines
  getKlines: (symbol: string, timeframe = '1m', limit = 200): Promise<KlineData[]> =>
    request<KlineData[]>(`/klines?symbol=${symbol}&timeframe=${timeframe}&limit=${limit}`),

  // Ticker
  getTicker: (symbol: string): Promise<{ symbol: string; last: number; change_pct: number; high_24h: number; low_24h: number; volume_24h: number }> =>
    request(`/ticker?symbol=${symbol}`),

  // Coin pool
  getCoinPool: (source?: string): Promise<CoinPoolEntry[]> =>
    request<CoinPoolEntry[]>(`/coin-pool${source ? `?source=${source}` : ''}`),
  refreshCoinPool: (): Promise<{ status: string; message: string }> =>
    request('/coin-pool/refresh', { method: 'POST' }),
  getCoinPoolConfig: (): Promise<{ refresh_interval_seconds: number; pool_source: string; max_symbols: number }> =>
    request('/coin-pool/config'),
  updateCoinPoolConfig: (data: any): Promise<{ refresh_interval_seconds: number; pool_source: string; max_symbols: number }> =>
    request('/coin-pool/config', { method: 'PUT', body: JSON.stringify(data) }),
  testFetchCoinPool: (): Promise<{ success: boolean; count: number; data: any[]; message: string }> =>
    request('/coin-pool/test-fetch', { method: 'POST' }),

  // Bot toggle
  toggleBot: (enabled: boolean): Promise<{ master_switch: boolean }> =>
    request('/bot/toggle', { method: 'POST', body: JSON.stringify({ enabled }) }),
};
