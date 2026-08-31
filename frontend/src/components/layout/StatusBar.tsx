import { useEffect, useState, useRef, useCallback } from 'react';
import { api } from '../../services/api';
import { defaultDashboardData, useDashboardStore } from '../../store/dashboardStore';
import { useAuthStore } from '../../store/authStore';
import { useWebSocket } from '../../hooks/useWebSocket';
import { Power, Circle, ChevronDown } from 'lucide-react';
import type { DashboardData } from '../../types';

export default function StatusBar() {
  const data = useDashboardStore((s) => s.data);
  const accounts = useDashboardStore((s) => s.accounts);
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);
  const dashboardLoading = useDashboardStore((s) => s.dashboardLoading);
  const role = useAuthStore((s) => s.role);
  const guest = role === 'guest';
  const [connected, setConnected] = useState(false);
  const [showAccountMenu, setShowAccountMenu] = useState(false);
  const fetchRef = useRef<() => void>(() => {});
  const fetchGen = useRef(0);

  // Connect dashboard WS to trigger re-fetch on snapshot
  useWebSocket('dashboard', useCallback((msg: any) => {
    if (msg.type === 'snapshot') {
      fetchRef.current();
    }
  }, []));

  useEffect(() => {
    api.listAccounts().then((accs) => {
      useDashboardStore.getState().setAccounts(accs);
      const saved = localStorage.getItem('selected_account_id');
      const initialId = (saved && accs.find((a) => a.id === Number(saved)))
        ? Number(saved)
        : (accs.length > 0 ? accs[0].id : null);
      useDashboardStore.getState().setSelectedAccountId(initialId);
    });
  }, []);

  useEffect(() => {
    if (selectedAccountId == null) {
      useDashboardStore.getState().setDashboardLoading(false);
      return;
    }

    const gen = ++fetchGen.current;
    const fetchDashboard = (showLoading: boolean) => {
      if (showLoading) useDashboardStore.getState().setDashboardLoading(true);
      const accId = selectedAccountId;
      api.getDashboard(accId).then((d: DashboardData) => {
        if (gen !== fetchGen.current) return;
        if (useDashboardStore.getState().selectedAccountId !== accId) return;
        useDashboardStore.getState().replaceData(d);
        setConnected(true);
      }).catch(() => {
        if (gen !== fetchGen.current) return;
        if (useDashboardStore.getState().selectedAccountId !== accId) return;
        setConnected(false);
        const prevSwitch = useDashboardStore.getState().data.master_switch;
        useDashboardStore.getState().replaceData({
          ...defaultDashboardData,
          exchange_positions: [],
          balance_status: 'error',
          master_switch: prevSwitch,
        });
      });
    };
    fetchRef.current = () => fetchDashboard(false);

    fetchDashboard(true);
    const interval = setInterval(() => fetchDashboard(false), 60000);
    return () => clearInterval(interval);
  }, [selectedAccountId]);

  const handleAccountSelect = (accountId: number) => {
    setShowAccountMenu(false);
    if (accountId === selectedAccountId) return;
    fetchGen.current += 1;
    localStorage.setItem('selected_account_id', String(accountId));
    const store = useDashboardStore.getState();
    store.resetData();
    store.setSelectedAccountId(accountId);
  };

  const handleToggle = async () => {
    try {
      await api.toggleBot(!data.master_switch);
      useDashboardStore.getState().setData({ master_switch: !data.master_switch });
    } catch {}
  };

  const balanceLabel = () => {
    if (dashboardLoading) return '加载中…';
    if (data.balance_status === 'no_account') return '未配置账户';
    if (data.balance_status === 'error') return '余额获取失败';
    return `${data.total_balance.toFixed(2)} USDT`;
  };

  const balanceColor = () => {
    if (data.balance_status === 'no_account') return 'text-yellow-400';
    if (data.balance_status === 'error') return 'text-red-400';
    return 'text-green-400';
  };

  const selectedAccount = accounts.find(a => a.id === selectedAccountId);

  return (
    <header className="h-12 bg-gray-900 border-b border-gray-800 flex items-center justify-between px-4">
      <div className="flex items-center gap-4 text-sm">
        <span className={`flex items-center gap-1.5 ${connected ? 'text-green-400' : 'text-red-400'}`}>
          <Circle size={8} fill="currentColor" />
          {connected ? '已连接' : '未连接'}
        </span>
        <span className="text-gray-500">|</span>

        {accounts.length > 0 && (
          <div className="relative">
            <button
              onClick={() => setShowAccountMenu(!showAccountMenu)}
              className="flex items-center gap-1.5 text-gray-300 hover:text-white transition-colors"
            >
              <span className="text-xs">
                {selectedAccount ? (
                  <>
                    {selectedAccount.name}
                    <span className={`ml-1.5 px-1.5 py-0.5 rounded text-[10px] ${
                      selectedAccount.exchange === 'gate'
                        ? 'bg-teal-600/20 text-teal-300'
                        : 'bg-amber-600/20 text-amber-300'
                    }`}>
                      {selectedAccount.exchange === 'gate' ? 'GATE' : '币安'}
                    </span>
                    <span className={`ml-1 px-1.5 py-0.5 rounded text-[10px] ${
                      selectedAccount.testnet
                        ? 'bg-yellow-600/20 text-yellow-400'
                        : 'bg-green-600/20 text-green-400'
                    }`}>
                      {selectedAccount.testnet ? '测试网' : '实盘'}
                    </span>
                  </>
                ) : '选择账户'}
              </span>
              <ChevronDown size={14} />
            </button>

            {showAccountMenu && (
              <div className="absolute top-full left-0 mt-1 w-48 bg-gray-800 border border-gray-700 rounded-lg shadow-lg z-50 py-1">
                {accounts.map((account) => (
                  <button
                    key={account.id}
                    onClick={() => handleAccountSelect(account.id)}
                    className={`w-full text-left px-3 py-2 text-xs hover:bg-gray-700 transition-colors flex items-center justify-between ${
                      selectedAccountId === account.id ? 'bg-gray-700/50' : ''
                    }`}
                  >
                    <span className="flex items-center gap-1">
                      <span>{account.name}</span>
                      <span className={`px-1 py-0.5 rounded text-[10px] ${
                        account.exchange === 'gate'
                          ? 'bg-teal-600/20 text-teal-300'
                          : 'bg-amber-600/20 text-amber-300'
                      }`}>
                        {account.exchange === 'gate' ? 'GATE' : '币安'}
                      </span>
                    </span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                      account.testnet
                        ? 'bg-yellow-600/20 text-yellow-400'
                        : 'bg-green-600/20 text-green-400'
                    }`}>
                      {account.testnet ? '测试' : '实盘'}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        <span className="text-gray-500">|</span>
        <span className="text-gray-300">
          余额: <strong className={dashboardLoading ? 'text-gray-400' : balanceColor()}>{balanceLabel()}</strong>
        </span>
        <span className="text-gray-500">|</span>
        <span className="text-gray-300">
          策略: <strong>{dashboardLoading ? '—' : data.active_strategies}</strong>
        </span>
        <span className="text-gray-300">
          持仓: <strong>{dashboardLoading ? '—' : data.open_positions}</strong>
        </span>
        <span className="text-gray-300 hidden lg:inline">
          {dashboardLoading ? (
            <span className="text-gray-500">切换账户中…</span>
          ) : (
            <>
          当日
          <strong className={data.daily_pnl >= 0 ? 'text-green-400 ml-1' : 'text-red-400 ml-1'}>
            {data.daily_pnl.toFixed(2)}
          </strong>
          <span className="text-gray-500 text-xs ml-1">累计已实现</span>
          <strong className={data.total_realized_pnl >= 0 ? 'text-emerald-400 ml-1' : 'text-orange-400 ml-1'}>
            {data.total_realized_pnl.toFixed(2)}
          </strong>
          <span className="text-gray-500 text-xs ml-1">累计胜率</span>
          <strong className="text-indigo-400 ml-1">{data.total_win_rate_pct.toFixed(1)}%</strong>
          <span className="text-gray-500 text-xs">({data.total_trades}笔)</span>
          <span className="mx-1 text-gray-500 text-xs">多单(累计)</span>
          <strong className={data.total_pnl_long >= 0 ? 'text-green-400' : 'text-red-400'}>
            {data.total_pnl_long.toFixed(2)}
          </strong>
          <span className="mx-1 text-gray-500 text-xs">空单(累计)</span>
          <strong className={data.total_pnl_short >= 0 ? 'text-green-400' : 'text-red-400'}>
            {data.total_pnl_short.toFixed(2)}
          </strong>
            </>
          )}
        </span>
      </div>

      {showAccountMenu && (
        <div
          className="fixed inset-0 z-40"
          onClick={() => setShowAccountMenu(false)}
        />
      )}

      <button
        type="button"
        onClick={guest || dashboardLoading ? undefined : handleToggle}
        disabled={guest || dashboardLoading}
        title={guest ? '访客模式无法切换总开关' : undefined}
        className={`flex items-center gap-1.5 px-3 py-1 rounded text-sm font-medium transition-colors ${
          guest ? 'opacity-50 cursor-not-allowed' : ''
        } ${
          data.master_switch
            ? 'bg-green-600/20 text-green-400 hover:bg-green-600/30'
            : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
        }`}
      >
        <Power size={14} />
        {data.master_switch ? '运行中' : '已停止'}
      </button>
    </header>
  );
}
