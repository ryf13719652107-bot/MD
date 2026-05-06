import { create } from 'zustand';

interface TickerData {
  symbol: string;
  last: number;
  change_pct: number;
  volume: number;
}

interface MarketState {
  selectedSymbol: string;
  selectedTimeframe: string;
  tickers: Record<string, TickerData>;
  setSelectedSymbol: (s: string) => void;
  setSelectedTimeframe: (t: string) => void;
  updateTicker: (t: TickerData) => void;
}

export const useMarketStore = create<MarketState>((set) => ({
  selectedSymbol: 'BTCUSDT',
  selectedTimeframe: '1m',
  tickers: {},
  setSelectedSymbol: (s) => set({ selectedSymbol: s }),
  setSelectedTimeframe: (t) => set({ selectedTimeframe: t }),
  updateTicker: (t) =>
    set((s) => ({
      tickers: { ...s.tickers, [t.symbol]: t },
    })),
}));
