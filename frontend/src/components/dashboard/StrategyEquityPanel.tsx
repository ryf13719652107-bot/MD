import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createChart, ColorType, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp } from 'lightweight-charts';
import { LineChart, RotateCcw } from 'lucide-react';
import { api } from '../../services/api';
import type { EquitySeriesData, EquityViewMode } from '../../types';

const DAY_OPTIONS = [7, 30, 90] as const;

function formatUsd(n: number) {
  const sign = n >= 0 ? '' : '-';
  const v = Math.abs(n);
  return `${sign}$${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export default function StrategyEquityPanel({ accountId }: { accountId: number | null }) {
  const [days, setDays] = useState<(typeof DAY_OPTIONS)[number]>(30);
  const [mode, setMode] = useState<EquityViewMode>('return');
  const [data, setData] = useState<EquitySeriesData | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Area'> | null>(null);

  useEffect(() => {
    if (accountId == null) {
      setData(null);
      return;
    }
    let cancelled = false;
    setData(null);
    setLoading(true);
    setErr(null);
    api
      .getEquitySeries(accountId, days)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: Error) => {
        if (!cancelled) {
          setErr(e.message);
          setData(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accountId, days]);

  const pointsLen = data?.points?.length ?? 0;

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: '#111827' },
        textColor: '#9ca3af',
      },
      grid: {
        vertLines: { color: '#1f2937' },
        horzLines: { color: '#1f2937' },
      },
      timeScale: { borderColor: '#374151' },
      rightPriceScale: { borderColor: '#374151' },
      height: 280,
      width: el.clientWidth,
    });
    const series = chart.addAreaSeries({
      lineColor: '#22c55e',
      topColor: 'rgba(34, 197, 94, 0.42)',
      bottomColor: 'rgba(34, 197, 94, 0.02)',
      lineWidth: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [pointsLen]);

  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart || !data?.points.length) {
      return;
    }

    const pts = data.points;
    const lastPnl = pts[pts.length - 1]?.pnl_usdt ?? 0;
    const green = lastPnl >= 0;
    const line = green ? '#22c55e' : '#ef4444';
    const top = green ? 'rgba(34, 197, 94, 0.42)' : 'rgba(239, 68, 68, 0.35)';
    const bottom = green ? 'rgba(34, 197, 94, 0.02)' : 'rgba(239, 68, 68, 0.02)';

    let chartData: { time: Time; value: number }[];
    if (mode === 'return') {
      chartData = pts.map((p) => ({ time: p.t_unix as UTCTimestamp, value: p.return_pct }));
      series.applyOptions({
        lineColor: line,
        topColor: top,
        bottomColor: bottom,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      });
    } else if (mode === 'balance') {
      chartData = pts.map((p) => ({ time: p.t_unix as UTCTimestamp, value: p.total_usdt }));
      series.applyOptions({
        lineColor: line,
        topColor: top,
        bottomColor: bottom,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      });
    } else {
      chartData = pts.map((p) => ({ time: p.t_unix as UTCTimestamp, value: p.pnl_usdt }));
      series.applyOptions({
        lineColor: line,
        topColor: top,
        bottomColor: bottom,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      });
    }

    series.setData(chartData);
    chart.timeScale().fitContent();
  }, [data, mode]);

  const resetBaseline = async () => {
    if (accountId == null) return;
    if (!window.confirm('将使用当前交易所余额作为新基准，确认重置收益曲线？')) return;
    try {
      await api.resetEquityBaseline(accountId);
      const d = await api.getEquitySeries(accountId, days);
      setData(d);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : '重置失败');
    }
  };

  const s = data?.summary;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-3">
        <div className="flex items-center gap-2 text-gray-200 text-sm font-semibold">
          <LineChart size={18} className="text-emerald-400/90" />
          策略收益
          <span className="text-xs font-normal text-gray-500 ml-1">（余额口径与仪表盘一致：total USDT）</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded-md border border-gray-700 overflow-hidden text-xs">
            {(['return', 'balance', 'pnl'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`px-2.5 py-1.5 transition-colors ${
                  mode === m ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
                }`}
              >
                {m === 'return' ? '回报率' : m === 'balance' ? '余额' : '盈亏'}
              </button>
            ))}
          </div>
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value) as (typeof DAY_OPTIONS)[number])}
            className="text-xs bg-gray-800 border border-gray-700 rounded px-2 py-1.5 text-gray-200"
          >
            {DAY_OPTIONS.map((d) => (
              <option key={d} value={d}>
                {d}D
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={resetBaseline}
            disabled={accountId == null}
            className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded bg-blue-600 text-white disabled:opacity-40 disabled:cursor-not-allowed hover:bg-blue-500"
          >
            <RotateCcw size={14} />
            重置收益
          </button>
        </div>
      </div>

      {accountId == null && (
        <p className="text-gray-500 text-sm py-8 text-center">请在顶部状态栏选择账户后查看收益曲线。</p>
      )}

      {accountId != null && loading && <p className="text-gray-500 text-sm py-4 text-center">加载中…</p>}
      {accountId != null && err && <p className="text-red-400 text-sm py-2">{err}</p>}

      {accountId != null && !loading && s && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-4 text-sm">
            <div>
              <div className="text-gray-500 text-xs mb-0.5">余额</div>
              <div className="font-mono font-medium text-gray-100">{s.total_balance.toLocaleString('en-US', { maximumFractionDigits: 2 })}</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs mb-0.5">盈亏</div>
              <div className={`font-mono font-medium ${s.pnl_usdt >= 0 ? 'text-green-400' : 'text-red-400'}`}>{formatUsd(s.pnl_usdt)}</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs mb-0.5">回报率</div>
              <div className={`font-mono font-medium ${s.return_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {s.return_pct >= 0 ? '+' : ''}
                {s.return_pct.toFixed(2)}%
              </div>
            </div>
            <div>
              <div className="text-gray-500 text-xs mb-0.5">最大回撤</div>
              <div className="font-mono font-medium text-red-400">{s.max_drawdown_pct.toFixed(2)}%</div>
            </div>
            <div>
              <div className="text-gray-500 text-xs mb-0.5">收益/回撤</div>
              <div
                className={`font-mono font-medium ${
                  s.return_pct < 0 || (s.return_drawdown_ratio != null && s.return_drawdown_ratio < 0)
                    ? 'text-red-400'
                    : s.return_pct > 0 || (s.return_drawdown_ratio ?? 0) > 0
                      ? 'text-green-400'
                      : 'text-gray-400'
                }`}
              >
                {s.return_drawdown_ratio != null ? s.return_drawdown_ratio.toFixed(2) : '—'}
              </div>
            </div>
          </div>
          {s.implicit_baseline && (
            <p className="text-xs text-gray-500 mb-2">未手动重置时，默认以窗口内最早快照（或当前余额）为基准。</p>
          )}
          {!data?.points.length ? (
            <p className="text-gray-500 text-sm py-6 text-center">
              尚无小时快照数据；服务启动后会自动写入，整点亦会采集。可先点击「重置收益」固定基准。
            </p>
          ) : (
            <div ref={containerRef} className="w-full h-[280px]" />
          )}
        </>
      )}
    </div>
  );
}
