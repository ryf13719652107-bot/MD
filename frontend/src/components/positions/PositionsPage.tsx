import { useEffect, useState, useRef } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Position } from '../../types';

export default function PositionsPage() {
  const { data, selectedAccountId } = useDashboardStore();
  const exchangePositions = data.exchange_positions || [];
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

  const totalUsdt = exchangePositions.reduce((s: number, p: any) => s + (p.usdt || 0), 0);
  const longUsdt = exchangePositions.filter((p: any) => p.side === 'long').reduce((s: number, p: any) => s + (p.usdt || 0), 0);
  const longPct = totalUsdt > 0 ? (longUsdt / totalUsdt * 100) : 0;
  const shortPct = totalUsdt > 0 ? (100 - longPct) : 0;

  const exchangeMap = new Map<string, any>();
  for (const ep of exchangePositions) {
    exchangeMap.set(`${ep.symbol}_${ep.side}`, ep);
  }

  const seen = new Set<string>();
  const merged: any[] = dbPositions.map((dp) => {
    const key = `${dp.symbol}_${dp.side}`;
    seen.add(key);
    const ep = exchangeMap.get(key);
    return {
      ...dp,
      usdt: ep?.usdt,
      mark_price: ep?.mark_price ?? dp.mark_price,
      unrealized_pnl: ep?.unrealized_pnl ?? dp.unrealized_pnl,
      pnl_pct: ep?.pnl_pct,
    };
  });

  for (const ep of exchangePositions) {
    const key = `${ep.symbol}_${ep.side}`;
    if (!seen.has(key)) {
      merged.push({
        symbol: ep.symbol,
        side: ep.side,
        quantity: 0,
        entry_price: ep.entry_price,
        mark_price: ep.mark_price,
        unrealized_pnl: ep.unrealized_pnl,
        pnl_pct: ep.pnl_pct,
        usdt: ep.usdt,
        layer: 0,
        opened_at: '',
        take_profit_price: null,
        tp_limit_order_id: null,
      });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-xl font-bold">当前持仓</h2>
        <span className="text-xs text-gray-500">DB + 交易所数据 · 每 30 秒刷新</span>
        {totalUsdt > 0 && (
          <span className="text-xs">
            <span className="text-green-400">多 {longPct.toFixed(0)}%</span>
            <span className="text-gray-600 mx-1">|</span>
            <span className="text-red-400">空 {shortPct.toFixed(0)}%</span>
            <span className="ml-2 w-20 h-1.5 bg-gray-700 rounded-full inline-flex overflow-hidden align-middle">
              <span className="h-full bg-green-500" style={{width: `${longPct}%`}}></span>
              <span className="h-full bg-red-500" style={{width: `${shortPct}%`}}></span>
            </span>
          </span>
        )}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">层数</th>
              <th className="p-3">USDT</th>
              <th className="p-3">入场价</th>
              <th className="p-3">当前价</th>
              <th className="p-3">止盈价</th>
              <th className="p-3">限价单</th>
              <th className="p-3">未实现盈亏</th>
              <th className="p-3">盈亏%</th>
              <th className="p-3">开仓时间</th>
            </tr>
          </thead>
          <tbody>
            {merged.map((p, i) => (
              <tr key={p.id || i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="p-3 font-medium font-mono">{p.symbol}</td>
                <td className={`p-3 ${p.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {p.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 text-gray-400">{p.layer != null ? `L${p.layer}` : '-'}</td>
                <td className="p-3 font-mono">{p.usdt?.toFixed(2)}</td>
                <td className="p-3 font-mono">{p.entry_price?.toFixed(8)}</td>
                <td className="p-3 font-mono">{p.mark_price?.toFixed(8)}</td>
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
                <td className={`p-3 font-mono ${(p.pnl_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {(p.pnl_pct || 0) >= 0 ? '+' : ''}{(p.pnl_pct || 0).toFixed(2)}%
                </td>
                <td className="p-3 text-gray-500 text-xs">{p.opened_at ? new Date(p.opened_at).toLocaleString() : '-'}</td>
              </tr>
            ))}
            {merged.length === 0 && (
              <tr><td colSpan={11} className="p-8 text-center text-gray-600">暂无持仓</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
