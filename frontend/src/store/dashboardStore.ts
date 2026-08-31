import { create } from 'zustand';
import type { Account, DashboardData, ExchangeId } from '../types';

interface DashboardState {
  data: DashboardData;
  selectedAccountId: number | null;
  accounts: Account[];
  dashboardLoading: boolean;
  _wsTick: number;
  setData: (data: Partial<DashboardData>) => void;
  replaceData: (data: DashboardData) => void;
  resetData: () => void;
  setSelectedAccountId: (id: number | null) => void;
  setAccounts: (accounts: Account[]) => void;
  setDashboardLoading: (v: boolean) => void;
  bumpWsTick: () => void;
}

export const defaultDashboardData: DashboardData = {
  total_balance: 0,
  wallet_balance: 0,
  margin_balance: 0,
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
  total_pnl_long: 0,
  total_pnl_short: 0,
  leverage_multiplier: 0,
  master_switch: false,
  account_name: '',
  balance_status: 'no_account',
  exchange_positions: [],
};

export function selectSelectedAccount(s: DashboardState): Account | undefined {
  return s.accounts.find((a) => a.id === s.selectedAccountId);
}

export function selectSelectedExchange(s: DashboardState): ExchangeId | null {
  return selectSelectedAccount(s)?.exchange ?? null;
}

export const useDashboardStore = create<DashboardState>((set) => ({
  data: { ...defaultDashboardData },
  selectedAccountId: null,
  accounts: [],
  dashboardLoading: false,
  setData: (data) => set((s) => ({ data: { ...s.data, ...data } })),
  replaceData: (data) => set({ data: { ...data }, dashboardLoading: false }),
  resetData: () => set((s) => ({
    data: {
      ...defaultDashboardData,
      exchange_positions: [],
      // 总开关是全局 BotConfig，切账户时不能闪成「已停止」
      master_switch: s.data.master_switch,
    },
    dashboardLoading: true,
  })),
  setSelectedAccountId: (id) => set({ selectedAccountId: id }),
  setAccounts: (accounts) => set({ accounts }),
  setDashboardLoading: (v) => set({ dashboardLoading: v }),
  _wsTick: 0,
  bumpWsTick: () => set((s) => ({ _wsTick: s._wsTick + 1 })),
}));
