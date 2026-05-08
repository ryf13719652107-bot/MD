import { useEffect, useState, useRef } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Position } from '../../types';

export default function PositionsPage() {
  const { selectedAccountId } = useDashboardStore();
  const [dbPositions, setDbPositions] = useState<Position[]>([]);
  const loadRef = useRef<() => void>(() => {});

  const load = async () => {
    try {
      const positions = await api.listPositions({ account_id: selectedAccountId ?? undefined });
      setDbPositions(positions);
    } catch {}
  };
  loadRef.current = load;

  useEffect(() => { load(); }, [selectedAccountId]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 30000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-xl font-bold">当前持仓</h2>
        <span className="text-xs text-gray-500">仅显示策略持仓 · 每 30 秒刷新</span>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">层数</th>
              <th className="p-3">入场价</th>
              <th className="p-3">当前价</th>
              <th className="p-3">止盈价</th>
              <th className="p-3">限价单</th>
              <th className="p-3">未实现盈亏</th>
              <th className="p-3">开仓时间</th>
            </tr>
          </thead>
          <tbody>
            {dbPositions.map((p) => (
              <tr key={p.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="p-3 font-medium font-mono">{p.symbol}</td>
                <td className={`p-3 ${p.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {p.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 text-gray-400">L{p.layer}</td>
                <td className="p-3 font-mono">{p.entry_price?.toFixed(8)}</td>
                <td className="p-3 font-mono">{p.mark_price?.toFixed(8) || '-'}</td>
                <td className="p-3 font-mono text-cyan-400">{p.take_profit_price?.toFixed(8) || '-'}</td>
                <td className="p-3">
                  {p.tp_limit_order_id ? (
                    <span className="px-1.5 py-0.5 rounded text-xs bg-blue-600/20 text-blue-400">已挂单</span>
                  ) : (
                    <span className="text-gray-600 text-xs">-</span>
                  )}
                </td>
                <td className={`p-3 font-mono ${(p.unrealized_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {(p.unrealized_pnl || 0) >= 0 ? '+' : ''}{(p.unrealized_pnl || 0).toFixed(2)} USDT
                </td>
                <td className="p-3 text-gray-500 text-xs">{p.opened_at ? new Date(p.opened_at).toLocaleString() : '-'}</td>
              </tr>
            ))}
            {dbPositions.length === 0 && (
              <tr><td colSpan={9} className="p-8 text-center text-gray-600">暂无持仓</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
