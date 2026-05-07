import { useEffect, useState, useRef } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Trade } from '../../types';
import { Download, Trash2 } from 'lucide-react';

export default function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const limit = 50;
  const selectedAccountId = useDashboardStore((s) => s.selectedAccountId);
  const loadRef = useRef<() => void>(() => {});

  const load = async () => {
    const data = await api.listTrades({ limit, offset: page * limit, account_id: selectedAccountId ?? undefined });
    setTrades(data.trades);
    setTotal(data.total);
  };
  loadRef.current = load;

  useEffect(() => {
    setPage(0);
  }, [selectedAccountId]);

  useEffect(() => { load(); }, [page, selectedAccountId]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 30000);
    return () => clearInterval(timer);
  }, []);

  const handleDeleteOne = async (id: number) => {
    if (!confirm('确定要删除这条交易记录吗？')) return;
    await api.deleteTrade(id);
    load();
  };

  const handleDeleteAll = async () => {
    if (!confirm('确定要删除全部交易记录吗？此操作不可恢复。')) return;
    await api.deleteAllTrades();
    setPage(0);
    load();
  };

  const exportCsv = () => {
    window.open('/api/trades/export', '_blank');
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">交易历史</h2>
        <div className="flex items-center gap-2">
          <button onClick={handleDeleteAll} className="flex items-center gap-1.5 bg-red-600/20 hover:bg-red-600/40 text-red-400 px-3 py-1.5 rounded-lg text-sm">
            <Trash2 size={16} />
            清空
          </button>
          <button onClick={exportCsv} className="flex items-center gap-1.5 bg-gray-700 hover:bg-gray-600 px-3 py-1.5 rounded-lg text-sm">
            <Download size={16} />
            导出CSV
          </button>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">平仓时间</th>
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">成本(USDT)</th>
              <th className="p-3">入场价</th>
              <th className="p-3">出场价</th>
              <th className="p-3">盈亏</th>
              <th className="p-3">盈亏%</th>
              <th className="p-3">平仓原因</th>
              <th className="p-3 w-10"></th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="p-3 text-gray-400">{new Date(t.exit_time).toLocaleString()}</td>
                <td className="p-3 font-medium">{t.symbol}</td>
                <td className={`p-3 ${t.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {t.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 font-mono">{(t.quantity * t.entry_price).toFixed(2)}</td>
                <td className="p-3">{t.entry_price?.toFixed(8)}</td>
                <td className="p-3">{t.exit_price?.toFixed(8)}</td>
                <td className={`p-3 ${t.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {t.realized_pnl >= 0 ? '+' : ''}{t.realized_pnl.toFixed(4)}
                </td>
                <td className={`p-3 ${t.pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {t.pnl_pct >= 0 ? '+' : ''}{t.pnl_pct.toFixed(2)}%
                </td>
                <td className="p-3">
                  <span className={`px-2 py-0.5 rounded text-xs ${
                    t.close_reason === 'take_profit' ? 'bg-green-600/20 text-green-400' :
                    t.close_reason === 'stop_loss' ? 'bg-red-600/20 text-red-400' :
                    t.close_reason === 'panic_close' ? 'bg-yellow-600/20 text-yellow-400' :
                    t.close_reason === 'sync' ? 'bg-blue-600/20 text-blue-400' :
                    t.close_reason === 'margin_stop' ? 'bg-orange-600/20 text-orange-400' :
                    'bg-gray-700 text-gray-400'
                  }`}>
                    {t.close_reason === 'take_profit' ? '止盈' :
                     t.close_reason === 'stop_loss' ? '止损' :
                     t.close_reason === 'panic_close' ? '紧急平仓' :
                     t.close_reason === 'sync' ? '同步平仓' :
                     t.close_reason === 'margin_stop' ? '保证金止损' :
                     t.close_reason === 'manual' ? '手动平仓' : t.close_reason}
                  </span>
                </td>
                <td className="p-3">
                  <button onClick={() => handleDeleteOne(t.id)} className="p-1 text-gray-500 hover:text-red-400 hover:bg-red-600/20 rounded" title="删除">
                    <Trash2 size={14} />
                  </button>
                </td>
              </tr>
            ))}
            {trades.length === 0 && (
              <tr><td colSpan={10} className="p-8 text-center text-gray-600">暂无交易记录</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {total > limit && (
        <div className="flex items-center justify-center gap-3">
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-3 py-1 bg-gray-800 rounded text-sm disabled:opacity-50"
          >
            上一页
          </button>
          <span className="text-sm text-gray-400">
            第 {page + 1} 页 / 共 {Math.ceil(total / limit)} 页
          </span>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={(page + 1) * limit >= total}
            className="px-3 py-1 bg-gray-800 rounded text-sm disabled:opacity-50"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
}
