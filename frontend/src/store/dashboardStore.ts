import { create } from 'zustand';
import type { DashboardData } from '../types';

interface DashboardState {
  data: DashboardData;
  selectedAccountId: number | null;
  _wsTick: number;
  setData: (data: Partial<DashboardData>) => void;
  setSelectedAccountId: (id: number | null) => void;
  bumpWsTick: () => void;
}

const defaultData: DashboardData = {
  total_balance: 0,
  available_balance: 0,
  unrealized_pnl: 0,
  unrealized_pnl_long: 0,
  unrealized_pnl_short: 0,
  daily_pnl: 0,
  daily_pnl_long: 0,
  daily_pnl_short: 0,
  daily_pnl_pct: 0,
  active_strategies: 0,
  open_positions: 0,
  daily_trades: 0,
  win_rate_pct: 0,
  total_realized_pnl: 0,
  total_trades: 0,
  total_win_rate_pct: 0,
  leverage_multiplier: 0,
  master_switch: false,
  account_name: '',
  balance_status: 'no_account',
  exchange_positions: [],
};

export const useDashboardStore = create<DashboardState>((set) => ({
  data: { ...defaultData },
  selectedAccountId: null,
  setData: (data) => set((s) => ({ data: { ...s.data, ...data } })),
  setSelectedAccountId: (id) => set({ selectedAccountId: id }),
  _wsTick: 0,
  bumpWsTick: () => set((s) => ({ _wsTick: s._wsTick + 1 })),
}));
