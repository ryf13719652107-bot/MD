import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { Trade } from '../../types';
import { TrendingDown, Layers, BarChart3, Activity, Target, Wallet, PiggyBank, Gauge } from 'lucide-react';

function PanelRow({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex justify-between items-baseline gap-3 py-1.5 border-b border-gray-800/80 last:border-0">
      <span className="text-gray-500 text-xs shrink-0">{label}</span>
      <span className={`text-sm font-mono font-medium text-right ${valueClass ?? 'text-gray-200'}`}>{value}</span>
    </div>
  );
}

export default function DashboardPage() {
  const { data, selectedAccountId } = useDashboardStore();
  const [trades, setTrades] = useState<Trade[]>([]);

  useEffect(() => {
    const accountId = selectedAccountId ?? undefined;
    const load = () => api.listTrades({ limit: 5, account_id: accountId }).then((d) => setTrades(d.trades)).catch(() => {});
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  }, [selectedAccountId]);

  const positions = data.exchange_positions || [];

  const totalUsdt = positions.reduce((s: number, p: any) => s + (p.usdt || 0), 0);
  const longUsdt = positions.filter((p: any) => p.side === 'long').reduce((s: number, p: any) => s + (p.usdt || 0), 0);
  const shortUsdt = totalUsdt - longUsdt;
  const longPct = totalUsdt > 0 ? (longUsdt / totalUsdt * 100) : 0;
  const shortPct = totalUsdt > 0 ? (shortUsdt / totalUsdt * 100) : 0;

  const leverageColor = data.leverage_multiplier > 5 ? 'text-red-400' : data.leverage_multiplier > 2 ? 'text-yellow-400' : 'text-green-400';

  const mainStats = [
    { label: '钱包余额', value: `${data.total_balance.toFixed(2)} USDT`, icon: Wallet, color: 'text-blue-400' },
    { label: '可用余额', value: `${data.available_balance.toFixed(2)} USDT`, icon: PiggyBank, color: 'text-green-400' },
    { label: '杠杆倍数', value: `${data.leverage_multiplier.toFixed(2)}x`, icon: Gauge, color: leverageColor },
    {
      label: '未实现盈亏',
      value: `${data.unrealized_pnl >= 0 ? '+' : ''}${data.unrealized_pnl.toFixed(2)} USDT`,
      icon: TrendingDown,
      color: data.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400',
    },
    { label: '活跃策略', value: String(data.active_strategies), icon: Activity, color: 'text-yellow-400' },
    { label: '当前持仓', value: String(data.open_positions), icon: Layers, color: 'text-purple-400' },
  ];

  const fmtPnl = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)} USDT`;

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-bold">仪表盘</h2>

      <div className="flex flex-col xl:flex-row gap-4 items-start">
        <div className="flex-1 min-w-0 space-y-4 w-full">
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
            {mainStats.map(({ label, value, icon: Icon, color }) => (
              <div key={label} className="bg-gray-900 border border-gray-800 rounded-lg p-3">
                <div className="flex items-center gap-2 text-gray-500 text-xs mb-1">
                  <Icon size={14} className={color} />
                  {label}
                </div>
                <div className={`text-lg font-semibold ${color}`}>{value}</div>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-gray-300 mb-3">
                当前持仓
                {totalUsdt > 0 && (
                  <span className="ml-3 text-xs font-normal">
                    <span className="text-green-400">多 {longPct.toFixed(0)}%</span>
                    <span className="text-gray-600 mx-1">|</span>
                    <span className="text-red-400">空 {shortPct.toFixed(0)}%</span>
                    <span className="ml-2 w-24 h-2 bg-gray-700 rounded-full inline-flex overflow-hidden align-middle">
                      <span className="h-full bg-green-500" style={{ width: `${longPct}%` }} />
                      <span className="h-full bg-red-500" style={{ width: `${shortPct}%` }} />
                    </span>
                  </span>
                )}
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-500 text-left">
                      <th className="pb-2">交易对</th>
                      <th className="pb-2">方向</th>
                      <th className="pb-2">USDT</th>
                      <th className="pb-2">入场价</th>
                      <th className="pb-2">盈亏</th>
                      <th className="pb-2">盈亏%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {positions.slice(0, 10).map((p: any, i: number) => (
                      <tr key={i} className="border-t border-gray-800">
                        <td className="py-2 font-mono">{p.symbol}</td>
                        <td className={p.side === 'long' ? 'text-green-400' : 'text-red-400'}>
                          {p.side === 'long' ? '做多' : '做空'}
                        </td>
                        <td className="font-mono">{p.usdt?.toFixed(2)}</td>
                        <td className="font-mono">{p.entry_price?.toFixed(6)}</td>
                        <td className={(p.unrealized_pnl || 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                          {p.unrealized_pnl >= 0 ? '+' : ''}
                          {p.unrealized_pnl?.toFixed(2)}
                        </td>
                        <td className={(p.pnl_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                          {p.pnl_pct >= 0 ? '+' : ''}
                          {p.pnl_pct?.toFixed(1)}%
                        </td>
                      </tr>
                    ))}
                    {positions.length === 0 && (
                      <tr>
                        <td colSpan={6} className="py-4 text-gray-600 text-center">
                          暂无持仓
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
              <h3 className="text-sm font-semibold text-gray-300 mb-3">最近交易</h3>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-500 text-left">
                      <th className="pb-2">时间</th>
                      <th className="pb-2">交易对</th>
                      <th className="pb-2">方向</th>
                      <th className="pb-2">盈亏</th>
                      <th className="pb-2">原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.slice(0, 5).map((t) => (
                      <tr key={t.id} className="border-t border-gray-800">
                        <td className="py-2 text-gray-400">{new Date(t.exit_time).toLocaleTimeString()}</td>
                        <td>{t.symbol}</td>
                        <td className={t.side === 'long' ? 'text-green-400' : 'text-red-400'}>
                          {t.side === 'long' ? '做多' : '做空'}
                        </td>
                        <td className={t.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}>
                          {t.realized_pnl.toFixed(4)}
                        </td>
                        <td className="text-gray-500 text-xs">
                          {t.close_reason === 'take_profit'
                            ? '止盈'
                            : t.close_reason === 'stop_loss'
                              ? '止损'
                              : t.close_reason === 'panic_close'
                                ? '紧急平仓'
                                : t.close_reason === 'sync'
                                  ? '同步平仓'
                                  : t.close_reason === 'margin_stop'
                                    ? '保证金止损'
                                    : t.close_reason === 'manual'
                                      ? '手动平仓'
                                      : t.close_reason}
                        </td>
                      </tr>
                    ))}
                    {trades.length === 0 && (
                      <tr>
                        <td colSpan={5} className="py-4 text-gray-600 text-center">
                          暂无交易记录
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <aside className="w-full xl:w-72 shrink-0 space-y-3">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-2 text-emerald-400/90 text-sm font-semibold mb-2 border-b border-gray-800 pb-2">
              <Target size={16} />
              累计数据
            </div>
            <div>
              <PanelRow
                label="累计已实现"
                value={fmtPnl(data.total_realized_pnl)}
                valueClass={data.total_realized_pnl >= 0 ? 'text-emerald-400' : 'text-orange-400'}
              />
              <PanelRow label="累计交易" value={`${data.total_trades} 笔`} />
              <PanelRow label="累计胜率" value={`${data.total_win_rate_pct.toFixed(1)}%`} valueClass="text-indigo-400" />
              <PanelRow
                label="多单盈亏(累计)"
                value={fmtPnl(data.total_pnl_long)}
                valueClass={data.total_pnl_long >= 0 ? 'text-green-400' : 'text-red-400'}
              />
              <PanelRow
                label="空单盈亏(累计)"
                value={fmtPnl(data.total_pnl_short)}
                valueClass={data.total_pnl_short >= 0 ? 'text-green-400' : 'text-red-400'}
              />
            </div>
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-2 text-cyan-400/90 text-sm font-semibold mb-2 border-b border-gray-800 pb-2">
              <BarChart3 size={16} />
              当日数据
            </div>
            <div>
              <PanelRow
                label="当日盈亏"
                value={fmtPnl(data.daily_pnl)}
                valueClass={data.daily_pnl >= 0 ? 'text-green-400' : 'text-red-400'}
              />
              <PanelRow label="当日交易" value={`${data.daily_trades} 笔`} />
              <PanelRow label="当日胜率" value={`${data.win_rate_pct.toFixed(1)}%`} valueClass="text-blue-400" />
              <PanelRow
                label="当日盈亏/余额"
                value={`${data.daily_pnl_pct >= 0 ? '+' : ''}${data.daily_pnl_pct.toFixed(2)}%`}
                valueClass={data.daily_pnl_pct >= 0 ? 'text-green-400' : 'text-red-400'}
              />
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
