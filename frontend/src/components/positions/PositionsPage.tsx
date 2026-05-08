import { useEffect, useState, useRef, useMemo } from 'react';
import { api } from '../../services/api';
import { useDashboardStore } from '../../store/dashboardStore';
import type { DashboardData, Position } from '../../types';
import { Check, Minus } from 'lucide-react';

type ExchangePos = DashboardData['exchange_positions'][number];

type DisplayRow = {
  key: string;
  symbol: string;
  side: 'long' | 'short';
  notional_usdt: number;
  entry_price: number;
  mark_price: number | null;
  unrealized_pnl: number;
  layer: string;
  take_profit_label: string;
  tp_has_order: boolean;
  tp_target_only: boolean; // 有止盈目标价但未挂限价单（如挂单失败、或仅市价止盈）
  opened_at_label: string;
  exchange_only: boolean;
};

function buildRows(dbPositions: Position[], exchangePositions: ExchangePos[]): DisplayRow[] {
  const norm = (s: string) => s.replace(/\//g, '').replace(':USDT', '').toUpperCase();
  if (exchangePositions.length > 0) {
    return exchangePositions.map((ep) => {
      const sym = norm(ep.symbol);
      const side = (ep.side || '').toLowerCase() as 'long' | 'short';
      const match = dbPositions.filter(
        (p) => norm(p.symbol) === sym && p.side === side,
      );
      let layer = '-';
      if (match.length === 1) layer = `L${match[0].layer}`;
      else if (match.length > 1) {
        const layers = [...new Set(match.map((m) => m.layer))].sort((a, b) => a - b);
        layer = `L${layers[0]}-L${layers[layers.length - 1]}（${match.length}层）`;
      }
      const tp = match.find((m) => m.take_profit_price != null)?.take_profit_price;
      const tpId = match.some((m) => !!m.tp_limit_order_id);
      const opened = match
        .map((m) => m.opened_at)
        .filter(Boolean)
        .sort()[0];
      const hasTpPrice = tp != null;
      return {
        key: `${sym}-${side}`,
        symbol: ep.symbol,
        side,
        notional_usdt: typeof ep.usdt === 'number' ? ep.usdt : 0,
        entry_price: ep.entry_price,
        mark_price: ep.mark_price,
        unrealized_pnl: ep.unrealized_pnl,
        layer,
        take_profit_label: hasTpPrice ? tp!.toFixed(8) : '-',
        tp_has_order: tpId,
        tp_target_only: hasTpPrice && !tpId,
        opened_at_label: opened ? new Date(opened as string).toLocaleString() : '-',
        exchange_only: match.length === 0,
      };
    });
  }
  if (dbPositions.length > 0) {
    return dbPositions.map((p) => {
      const px = p.mark_price ?? p.entry_price;
      return {
        key: String(p.id),
        symbol: p.symbol,
        side: p.side,
        notional_usdt: px * p.quantity,
        entry_price: p.entry_price,
        mark_price: p.mark_price ?? null,
        unrealized_pnl: p.unrealized_pnl ?? 0,
        layer: `L${p.layer}`,
        take_profit_label: p.take_profit_price != null ? p.take_profit_price.toFixed(8) : '-',
        tp_has_order: !!p.tp_limit_order_id,
        tp_target_only: p.take_profit_price != null && !p.tp_limit_order_id,
        opened_at_label: p.opened_at ? new Date(p.opened_at).toLocaleString() : '-',
        exchange_only: false,
      };
    });
  }
  return [];
}

export default function PositionsPage() {
  const { selectedAccountId } = useDashboardStore();
  const [dbPositions, setDbPositions] = useState<Position[]>([]);
  const [exchangePositions, setExchangePositions] = useState<ExchangePos[]>([]);
  const loadRef = useRef<() => void>(() => {});

  const load = async () => {
    const acc = selectedAccountId ?? undefined;
    try {
      const [positions, dash] = await Promise.all([
        api.listPositions({ account_id: acc }),
        api.getDashboard(acc),
      ]);
      setDbPositions(positions);
      setExchangePositions(dash.exchange_positions || []);
      useDashboardStore.getState().setData(dash);
    } catch {
      try {
        const positions = await api.listPositions({ account_id: acc });
        setDbPositions(positions);
      } catch {
        setDbPositions([]);
      }
    }
  };
  loadRef.current = load;

  useEffect(() => {
    load();
  }, [selectedAccountId]);

  useEffect(() => {
    const timer = setInterval(() => loadRef.current(), 30000);
    return () => clearInterval(timer);
  }, []);

  const rows = useMemo(
    () => buildRows(dbPositions, exchangePositions),
    [dbPositions, exchangePositions],
  );

  const hasExchangeHint = exchangePositions.length > 0 && dbPositions.length === 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-3">
        <h2 className="text-xl font-bold">当前持仓</h2>
        <span className="text-xs text-gray-500">
          与顶部「持仓」一致：来自交易所；层数 / 止盈 / 限价单来自本地策略库（若有）· 每 30 秒刷新
        </span>
      </div>

      {hasExchangeHint && (
        <p className="text-xs text-amber-500/90 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          交易所有持仓，但本地数据库暂无对应未平仓记录。可能是数据未同步、部署过新库或仓位非本机器人开立。机器人仍可交易；必要时可对账或等待同步。
        </p>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-gray-500 text-left border-b border-gray-800">
              <th className="p-3">交易对</th>
              <th className="p-3">方向</th>
              <th className="p-3">名义(USDT)</th>
              <th className="p-3">层数</th>
              <th className="p-3">入场价</th>
              <th className="p-3">当前价</th>
              <th className="p-3">止盈价</th>
              <th className="p-3">限价止盈</th>
              <th className="p-3">未实现盈亏</th>
              <th className="p-3">开仓时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.key}
                className="border-b border-gray-800/50 hover:bg-gray-800/30"
              >
                <td className="p-3 font-medium font-mono">
                  {row.symbol}
                  {row.exchange_only && (
                    <span className="ml-1.5 text-[10px] text-gray-500 align-middle">仅交易所</span>
                  )}
                </td>
                <td className={`p-3 ${row.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                  {row.side === 'long' ? '做多' : '做空'}
                </td>
                <td className="p-3 font-mono text-gray-200">{row.notional_usdt.toFixed(2)}</td>
                <td className="p-3 text-gray-400">{row.layer}</td>
                <td className="p-3 font-mono">{row.entry_price?.toFixed(8)}</td>
                <td className="p-3 font-mono">{row.mark_price != null ? row.mark_price.toFixed(8) : '-'}</td>
                <td className="p-3 font-mono text-cyan-400">{row.take_profit_label}</td>
                <td className="p-3">
                  {row.tp_has_order ? (
                    <span className="inline-flex items-center gap-1 text-green-400" title="已挂限价止盈单">
                      <Check size={16} strokeWidth={2.5} />
                      <span className="text-xs">已挂单</span>
                    </span>
                  ) : row.tp_target_only ? (
                    <span className="inline-flex items-center gap-1 text-amber-400/90" title="有止盈目标价，当前无未完成限价单（可能为市价止盈或未挂成）">
                      <Minus size={16} />
                      <span className="text-xs">未挂单</span>
                    </span>
                  ) : (
                    <span className="text-gray-600 text-xs">-</span>
                  )}
                </td>
                <td className={`p-3 font-mono ${row.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {row.unrealized_pnl >= 0 ? '+' : ''}
                  {row.unrealized_pnl.toFixed(2)} USDT
                </td>
                <td className="p-3 text-gray-500 text-xs">{row.opened_at_label}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={10} className="p-8 text-center text-gray-600">
                  暂无持仓
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
