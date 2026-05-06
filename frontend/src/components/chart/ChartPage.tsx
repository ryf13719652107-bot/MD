import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams } from 'react-router-dom';
import { createChart, ColorType, type IChartApi, type ISeriesApi, type CandlestickData, type LineData, type Time } from 'lightweight-charts';
import { api } from '../../services/api';
import { useMarketStore } from '../../store/marketStore';
import type { KlineData } from '../../types';

const LIMIT = 500;

export default function ChartPage() {
  const { symbol } = useParams();
  const { selectedSymbol, selectedTimeframe, setSelectedSymbol, setSelectedTimeframe } = useMarketStore();
  const mainRef = useRef<HTMLDivElement>(null);
  const rsiRef = useRef<HTMLDivElement>(null);
  const wtRef = useRef<HTMLDivElement>(null);
  const charts = useRef<{ main: IChartApi | null; rsi: IChartApi | null; wt: IChartApi | null }>({ main: null, rsi: null, wt: null });
  const series = useRef<{ candle: ISeriesApi<'Candlestick'> | null; rsi: ISeriesApi<'Line'> | null; wt1: ISeriesApi<'Line'> | null; wt2: ISeriesApi<'Line'> | null }>(
    { candle: null, rsi: null, wt1: null, wt2: null }
  );
  const [rsiVal, setRsiVal] = useState<number | null>(null);
  const [wtVal, setWtVal] = useState<{ wt1: number; wt2: number } | null>(null);
  const [price, setPrice] = useState<string>('-');
  const [symbols, setSymbols] = useState<string[]>([
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'DOTUSDT',
  ]);

  const currentSymbol = symbol || selectedSymbol;

  const loadData = useCallback(async () => {
    if (!series.current.candle) return;
    try {
      const [klines, ticker] = await Promise.all([
        api.getKlines(currentSymbol, selectedTimeframe, LIMIT),
        api.getTicker(currentSymbol),
      ]);
      setPrice(ticker?.last != null ? String(ticker.last) : '-');

      // --- K-line ---
      const candleData: CandlestickData[] = klines.map((k) => ({
        time: (k.time / 1000) as Time, open: k.open, high: k.high, low: k.low, close: k.close,
      }));
      series.current.candle.setData(candleData);

      const closes = klines.map((k) => k.close);
      const highs = klines.map((k) => k.high);
      const lows = klines.map((k) => k.low);

      // --- RSI ---
      const rsiValues = calcRSI(closes, 14);
      setRsiVal(rsiValues.length > 0 ? rsiValues[rsiValues.length - 1] : null);
      if (series.current.rsi) {
        const offset = klines.length - rsiValues.length;
        series.current.rsi.setData(rsiValues.map((v, i) => ({
          time: (klines[i + offset].time / 1000) as Time, value: v,
        })));
      }

      // --- WaveTrend ---
      const wt = calcWaveTrend(highs, lows, closes, 10, 21);
      if (wt) {
        setWtVal({ wt1: wt.wt1[wt.wt1.length - 1], wt2: wt.wt2[wt.wt2.length - 1] });
        if (series.current.wt1 && series.current.wt2) {
          const wtLen = wt.wt1.length;
          const wtOffset = klines.length - wtLen;
          const wt1Data: LineData[] = wt.wt1.map((v, i) => ({
            time: (klines[i + wtOffset].time / 1000) as Time, value: v,
          }));
          const wt2Data: LineData[] = wt.wt2.map((v, i) => ({
            time: (klines[i + wtOffset].time / 1000) as Time, value: v,
          }));
          series.current.wt1.setData(wt1Data);
          series.current.wt2.setData(wt2Data);
        }
      }
    } catch (e) {
      console.error('图表加载失败:', e);
    }
  }, [currentSymbol, selectedTimeframe]);

  // Init charts once
  useEffect(() => {
    if (!mainRef.current || !rsiRef.current || !wtRef.current) return;
    // Clean old
    Object.values(charts.current).forEach((c) => c?.remove());
    Object.keys(series.current).forEach((k) => (series.current as any)[k] = null);

    // Main chart
    charts.current.main = createChart(mainRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#111827' }, textColor: '#9ca3af' },
      grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } },
      width: mainRef.current.clientWidth, height: mainRef.current.clientHeight,
      crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false },
    });
    series.current.candle = charts.current.main.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444', borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });

    // RSI chart
    charts.current.rsi = createChart(rsiRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#111827' }, textColor: '#9ca3af' },
      grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } },
      width: rsiRef.current.clientWidth, height: rsiRef.current.clientHeight,
      crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false },
    });
    series.current.rsi = charts.current.rsi.addLineSeries({ color: '#8b5cf6', lineWidth: 2 });
    series.current.rsi.createPriceLine({ price: 70, color: '#ef4444', lineWidth: 1, lineStyle: 2 });
    series.current.rsi.createPriceLine({ price: 30, color: '#22c55e', lineWidth: 1, lineStyle: 2 });

    // WaveTrend chart
    charts.current.wt = createChart(wtRef.current, {
      layout: { background: { type: ColorType.Solid, color: '#111827' }, textColor: '#9ca3af' },
      grid: { vertLines: { color: '#1f2937' }, horzLines: { color: '#1f2937' } },
      width: wtRef.current.clientWidth, height: wtRef.current.clientHeight,
      crosshair: { mode: 0 }, timeScale: { timeVisible: true, secondsVisible: false },
    });
    series.current.wt1 = charts.current.wt.addLineSeries({ color: '#22c55e', lineWidth: 2 });
    series.current.wt2 = charts.current.wt.addLineSeries({ color: '#ef4444', lineWidth: 2 });
    // WT reference lines
    series.current.wt1.createPriceLine({ price: 60, color: '#ef4444', lineWidth: 1, lineStyle: 2 });
    series.current.wt1.createPriceLine({ price: -60, color: '#22c55e', lineWidth: 1, lineStyle: 2 });

    loadData();

    const handleResize = () => {
      [mainRef, rsiRef, wtRef].forEach((ref, i) => {
        const keys = ['main', 'rsi', 'wt'] as const;
        if (ref.current && charts.current[keys[i]]) {
          charts.current[keys[i]]!.applyOptions({ width: ref.current.clientWidth, height: ref.current.clientHeight });
        }
      });
    };
    window.addEventListener('resize', handleResize);
    return () => {
      window.removeEventListener('resize', handleResize);
      Object.values(charts.current).forEach((c) => c?.remove());
    };
  }, []); // eslint-disable-line

  // Reload on change
  useEffect(() => { loadData(); }, [loadData]);

  return (
    <div className="space-y-2 h-[calc(100vh-56px)] flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between flex-shrink-0">
        <h2 className="text-lg font-bold">图表分析</h2>
        <div className="flex items-center gap-2 text-xs">
          <select value={currentSymbol} onChange={(e) => setSelectedSymbol(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1">
            {symbols.map((s) => (<option key={s} value={s}>{s}</option>))}
          </select>
          <select value={selectedTimeframe} onChange={(e) => setSelectedTimeframe(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded px-2 py-1">
            <option value="1m">1m</option><option value="5m">5m</option>
            <option value="15m">15m</option><option value="1h">1h</option>
          </select>
          <span className="text-gray-300">价格: <strong className="text-white">{price}</strong></span>
          {rsiVal !== null && (
            <span className={rsiVal < 30 ? 'text-green-400' : rsiVal > 70 ? 'text-red-400' : 'text-gray-400'}>
              RSI14: <strong>{rsiVal.toFixed(1)}</strong>
            </span>
          )}
          {wtVal && (
            <span className="text-gray-400">
              WT: <strong className="text-green-400">{wtVal.wt1.toFixed(2)}</strong>
              <span className="text-gray-600">/</span>
              <strong className="text-red-400">{wtVal.wt2.toFixed(2)}</strong>
            </span>
          )}
        </div>
      </div>

      {/* Charts */}
      <div ref={mainRef} className="flex-[5] bg-gray-900 border border-gray-800 rounded-lg min-h-0" />
      <div ref={rsiRef} className="flex-[2] bg-gray-900 border border-gray-800 rounded-lg min-h-0" />
      <div ref={wtRef} className="flex-[2] bg-gray-900 border border-gray-800 rounded-lg min-h-0" />
    </div>
  );
}

// ── RSI ──
function calcRSI(closes: number[], period: number): number[] {
  if (closes.length < period + 1) return [];
  const gains: number[] = [], losses: number[] = [];
  for (let i = 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    gains.push(d > 0 ? d : 0); losses.push(d < 0 ? -d : 0);
  }
  let ag = gains.slice(0, period).reduce((a, b) => a + b) / period;
  let al = losses.slice(0, period).reduce((a, b) => a + b) / period;
  const r: number[] = [al === 0 ? 100 : 100 - 100 / (1 + ag / al)];
  for (let i = period; i < gains.length; i++) {
    ag = (ag * (period - 1) + gains[i]) / period;
    al = (al * (period - 1) + losses[i]) / period;
    r.push(al === 0 ? 100 : 100 - 100 / (1 + ag / al));
  }
  return r;
}

// ── WaveTrend ──
function ema(data: number[], period: number): number[] {
  if (data.length < period) return [];
  const k = 2 / (period + 1);
  const r = [data.slice(0, period).reduce((a, b) => a + b) / period];
  for (let i = period; i < data.length; i++) r.push(data[i] * k + r[r.length - 1] * (1 - k));
  return r;
}

function sma(data: number[], period: number): number[] {
  if (data.length < period) return [];
  const r: number[] = [];
  for (let i = period - 1; i < data.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += data[j];
    r.push(sum / period);
  }
  return r;
}

function calcWaveTrend(highs: number[], lows: number[], closes: number[], chLen: number, avgLen: number) {
  const n = highs.length;
  if (n < chLen + avgLen + 4) return null;
  const hlc3 = highs.map((h, i) => (h + lows[i] + closes[i]) / 3);

  // 1. esa = EMA(HLC3, chLen)
  const esa = ema(hlc3, chLen);
  if (!esa.length) return null;
  const offEsa = hlc3.length - esa.length;

  // 2. d = EMA(|HLC3 - ESA|, chLen)
  const dev = esa.map((_, i) => Math.abs(hlc3[i + offEsa] - esa[i]));
  const d = ema(dev, chLen);
  if (!d.length) return null;
  const offD = esa.length - d.length;

  // 3. ci = (HLC3 - ESA) / (0.015 * d)
  const ci: number[] = [];
  for (let i = 0; i < d.length; i++) {
    const eI = i + offD;
    const hI = eI + offEsa;
    ci.push(d[i] !== 0 ? (hlc3[hI] - esa[eI]) / (0.015 * d[i]) : 0);
  }

  // 4. wt1 = EMA(CI, avgLen)
  const wt1 = ema(ci, avgLen);
  if (!wt1.length) return null;

  // 5. wt2 = SMA(wt1, 4)  — Pine Script exact
  const wt2 = sma(wt1, 4);
  if (!wt2.length) return null;

  const offF = wt1.length - wt2.length;
  return {
    wt1: wt1.slice(offF),
    wt2: wt2,
  };
}
