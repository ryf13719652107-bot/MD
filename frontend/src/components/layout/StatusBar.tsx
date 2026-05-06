import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import { Power, Circle } from 'lucide-react';
import type { DashboardData } from '../../types';

export default function StatusBar() {
  const { data } = useDashboardStore();
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    api.getDashboard().then((d: DashboardData) => {
      useDashboardStore.getState().setData(d);
      setConnected(true);
    }).catch(() => setConnected(false));

    const interval = setInterval(async () => {
      try {
        const d = await api.getDashboard();
    const fetchDashboard = () => {
      api.getDashboard(selectedAccountId).then((d: DashboardData) => {
      } catch {
        setConnected(true);
      }
    }, 5000);
    fetchDashboard();
  }, []);
    useDashboardStore.getState().setSelectedAccountId(accountId);
    setShowAccountMenu(false);
  };

  const handleToggle = async () => {
    try {
      await api.toggleBot(!data.master_switch);
      useDashboardStore.getState().setData({ master_switch: !data.master_switch });
    } catch {}
  };
    if (data.balance_status === 'error') return '余额获取失败';
  const balanceLabel = () => {
    if (data.balance_status === 'no_account') return '未配置账户';
    if (data.balance_status === 'error') return selectedAccount?.testnet ? '测试网需APIkey' : '余额获取失败';
    return `${data.total_balance.toFixed(2)} USDT`;
  };

  const balanceColor = () => {
    if (data.balance_status === 'no_account') return 'text-yellow-400';
    if (data.balance_status === 'error') return 'text-red-400';

  const selectedAccount = accounts.find(a => a.id === selectedAccountId);

  return (
    <header className="h-12 bg-gray-900 border-b border-gray-800 flex items-center justify-between px-4">
      <div className="flex items-center gap-4 text-sm">
        <span className={`flex items-center gap-1.5 ${connected ? 'text-green-400' : 'text-red-400'}`}>
          <Circle size={8} fill="currentColor" />
        {data.account_name && (
          <>
            <span className="text-gray-400 text-xs">{data.account_name}</span>
            <span className="text-gray-500">|</span>
          </>
        )}
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
          盈亏: <strong className={data.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
            {data.daily_pnl.toFixed(2)} USDT
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
