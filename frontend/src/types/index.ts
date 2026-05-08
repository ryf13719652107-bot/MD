export interface Account {
  id: number;
  name: string;
  masked_key: string;
  testnet: boolean;
  hedge_mode: boolean;
  created_at: string;
  updated_at: string;
}

export interface Position {
  id: number;
  strategy_id: number | null;
  account_id: number;
  symbol: string;
  side: 'long' | 'short';
  quantity: number;
  entry_price: number;
  mark_price: number | null;
  unrealized_pnl: number | null;
  layer: number;
  take_profit_price: number | null;
  exchange_order_id: string | null;
  tp_limit_order_id: string | null;
  opened_at: string;
  closed_at: string | null;
}

export interface Trade {
  id: number;
  strategy_id: number | null;
  account_id: number;
  symbol: string;
  side: 'long' | 'short';
  quantity: number;
  entry_price: number;
  exit_price: number;
  realized_pnl: number;
  pnl_pct: number;
  entry_time: string;
  exit_time: string;
  layer: number;
  close_reason: string;
}

export interface DashboardData {
  total_balance: number;
  available_balance: number;
  unrealized_pnl: number;
  unrealized_pnl_long: number;
  unrealized_pnl_short: number;
  daily_pnl: number;
  daily_pnl_long: number;
  daily_pnl_short: number;
  daily_pnl_pct: number;
  active_strategies: number;
  open_positions: number;
  daily_trades: number;
  win_rate_pct: number;
  /** 历史累计已实现盈亏（trades 表） */
  total_realized_pnl: number;
  /** 历史平仓笔数 */
  total_trades: number;
  /** 历史胜率（盈利笔数/总笔数） */
  total_win_rate_pct: number;
  /** 历史累计已实现盈亏 — 多单 */
  total_pnl_long: number;
  /** 历史累计已实现盈亏 — 空单 */
  total_pnl_short: number;
  leverage_multiplier: number;
  master_switch: boolean;
  account_name: string;
  balance_status: string;
  exchange_positions: Array<{
    symbol: string;
    side: string;
    usdt: number;
    contracts?: number;
    entry_price: number;
    mark_price: number;
    unrealized_pnl: number;
    pnl_pct: number;
  }>;
}

export interface CoinPoolEntry {
  id: number;
  symbol: string;
  rank: number;
  price_change_pct: number;
  volume_24h: number | null;
  source: 'gainers' | 'losers';
  added_at: string;
  last_updated: string;
}

export interface KlineData {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}
