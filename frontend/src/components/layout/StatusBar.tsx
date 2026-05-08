import { useEffect, useState, useRef, useCallback } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import { useWebSocket } from '../../hooks/useWebSocket';
import { Power, Circle, ChevronDown } from 'lucide-react';
import type { DashboardData, Account } from '../../types';

export default function StatusBar() {
  const { data } = useDashboardStore();
  const [connected, setConnected] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
  const [showAccountMenu, setShowAccountMenu] = useState(false);
  const fetchRef = useRef<() => void>(() => {});

  // Connect dashboard WS to trigger re-fetch on snapshot
  useWebSocket('dashboard', useCallback((msg: any) => {
    if (msg.type === 'snapshot') {
      fetchRef.current();
    }
  }, []));

  useEffect(() => {
    api.listAccounts().then((accs) => {
      setAccounts(accs);
      const saved = localStorage.getItem('selected_account_id');
      const initialId = (saved && accs.find(a => a.id === Number(saved)))
        ? Number(saved)
        : (accs.length > 0 ? accs[0].id : null);
      setSelectedAccountId(initialId);
      if (initialId) useDashboardStore.getState().setSelectedAccountId(initialId);
    });
  }, []);

  useEffect(() => {
    if (selectedAccountId == null) return;

    const fetchDashboard = () => {
      api.getDashboard(selectedAccountId).then((d: DashboardData) => {
        useDashboardStore.getState().setData(d);
        setConnected(true);
      }).catch(() => {
        setConnected(false);
        useDashboardStore.getState().setData({ exchange_positions: [] });
      });
    };
    fetchRef.current = fetchDashboard;

    fetchDashboard();
    const interval = setInterval(fetchDashboard, 30000);
    return () => clearInterval(interval);
  }, [selectedAccountId]);

  const handleAccountSelect = (accountId: number) => {
    setSelectedAccountId(accountId);
    localStorage.setItem('selected_account_id', String(accountId));
    useDashboardStore.getState().setSelectedAccountId(accountId);
    setShowAccountMenu(false);
  };

  const handleToggle = async () => {
    try {
      await api.toggleBot(!data.master_switch);
      useDashboardStore.getState().setData({ master_switch: !data.master_switch });
    } catch {}
  };

  const balanceLabel = () => {
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
                    <span>{account.name}</span>
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
          余额: <strong className={balanceColor()}>{balanceLabel()}</strong>
        </span>
        <span className="text-gray-500">|</span>
        <span className="text-gray-300">
          策略: <strong>{data.active_strategies}</strong>
        </span>
        <span className="text-gray-300">
          持仓: <strong>{data.open_positions}</strong>
        </span>
        <span className="text-gray-300">
          当日
          <strong className={data.daily_pnl >= 0 ? 'text-green-400 ml-1' : 'text-red-400 ml-1'}>
            {data.daily_pnl.toFixed(2)}
          </strong>
          <span className="text-gray-500 text-xs ml-1">累计已实现</span>
          <strong className={data.total_realized_pnl >= 0 ? 'text-emerald-400 ml-1' : 'text-orange-400 ml-1'}>
            {data.total_realized_pnl.toFixed(2)}
          </strong>
          <span className="text-gray-500 text-xs ml-1">胜率</span>
          <strong className="text-indigo-400 ml-1">{data.total_win_rate_pct.toFixed(1)}%</strong>
          <span className="text-gray-500 text-xs">({data.total_trades}笔)</span>
          <span className="mx-1">多单盈亏</span>
          <strong className={data.daily_pnl_long >= 0 ? 'text-green-400' : 'text-red-400'}>
            {data.daily_pnl_long.toFixed(2)}
          </strong>
          <span className="mx-1">空单盈亏</span>
          <strong className={data.daily_pnl_short >= 0 ? 'text-green-400' : 'text-red-400'}>
            {data.daily_pnl_short.toFixed(2)}
          </strong>
        </span>
      </div>

      {showAccountMenu && (
        <div
          className="fixed inset-0 z-40"
          onClick={() => setShowAccountMenu(false)}
        />
      )}

      <button
        onClick={handleToggle}
        className={`flex items-center gap-1.5 px-3 py-1 rounded text-sm font-medium transition-colors ${
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
