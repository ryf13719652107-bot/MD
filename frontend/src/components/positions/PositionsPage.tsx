import { useDashboardStore } from '../../store/dashboardStore';

export default function PositionsPage() {
  const { data } = useDashboardStore();
  const positions = data.exchange_positions || [];
  const totalUsdt = positions.reduce((s: number, p: any) => s + (p.usdt || 0), 0);
  const longUsdt = positions.filter((p: any) => p.side === 'long').reduce((s: number, p: any) => s + (p.usdt || 0), 0);
  const longPct = totalUsdt > 0 ? (longUsdt / totalUsdt * 100) : 0;
  const shortPct = totalUsdt > 0 ? (100 - longPct) : 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-xl font-bold">当前持仓</h2>
        <span className="text-xs text-gray-500">交易所实时数据 · 每 10 秒刷新</span>
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

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">USDT</th>
              <th className="p-3">入场价</th>
              <th className="p-3">当前价</th>
              <th className="p-3">未实现盈亏</th>
              <th className="p-3">盈亏%</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p: any, i: number) => (
              <tr key={i} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="p-3 font-medium font-mono">{p.symbol}</td>
                <td className={`p-3 ${p.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {p.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 font-mono">{p.usdt?.toFixed(2)}</td>
                <td className="p-3 font-mono">{p.entry_price?.toFixed(6)}</td>
                <td className="p-3 font-mono">{p.mark_price?.toFixed(6)}</td>
                <td className={`p-3 font-mono ${(p.unrealized_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {p.unrealized_pnl >= 0 ? '+' : ''}{p.unrealized_pnl?.toFixed(2)} USDT
                </td>
                <td className={`p-3 font-mono ${(p.pnl_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct?.toFixed(2)}%
                </td>
              </tr>
            ))}
            {positions.length === 0 && (
              <tr><td colSpan={7} className="p-8 text-center text-gray-600">暂无持仓</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
