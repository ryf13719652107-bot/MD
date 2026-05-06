import { useEffect, useState, useCallback } from 'react';
import { api } from '../../services/api';
import type { CoinPoolEntry } from '../../types';
import { RefreshCw, TrendingUp, TrendingDown, FlaskConical } from 'lucide-react';

export default function CoinPoolPage() {
  const [coins, setCoins] = useState<CoinPoolEntry[]>([]);
  const [source, setSource] = useState<string>('');
  const [config, setConfig] = useState({ refresh_interval_seconds: 3600, pool_source: 'both', max_symbols: 20 });
  const [testResult, setTestResult] = useState<{ success: boolean; message: string; data: any[] } | null>(null);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    const [c, cfg] = await Promise.all([
      api.getCoinPool(source || undefined),
      api.getCoinPoolConfig(),
    ]);
    setCoins(c);
    setConfig(cfg);
  }, [source]);

  useEffect(() => { load(); }, [load]);

  const handleRefresh = async () => {
    try {
      const result = await api.refreshCoinPool();
      setTestResult({
        success: result.status === 'ok',
        message: result.message,
        data: [],
      });
    } catch (e: any) {
      setTestResult({ success: false, message: `请求异常: ${e.message}`, data: [] });
    }
    load();
  };

  const handleConfigUpdate = async () => {
    await api.updateCoinPoolConfig(config);
    load();
  };

  const handleTestFetch = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.testFetchCoinPool();
      setTestResult(result);
    } catch (e: any) {
      setTestResult({ success: false, message: `请求失败: ${e.message}`, data: [] });
    }
    setTesting(false);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-bold">选币池</h2>
        <div className="flex items-center gap-2">
          <button
            onClick={handleTestFetch}
            disabled={testing}
            className="flex items-center gap-1.5 bg-purple-600 hover:bg-purple-700 disabled:opacity-50 px-3 py-1.5 rounded-lg text-sm"
          >
            <FlaskConical size={16} />
            {testing ? '测试中...' : '测试抓取'}
          </button>
          <button
            onClick={handleRefresh}
            className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg text-sm"
          >
            <RefreshCw size={16} />
            刷新
          </button>
        </div>
      </div>

      {testResult && (
        <div className={`border rounded-lg p-4 ${testResult.success ? 'bg-green-900/20 border-green-700' : 'bg-red-900/20 border-red-700'}`}>
          <div className="flex items-center justify-between mb-2">
            <h3 className={`font-semibold ${testResult.success ? 'text-green-400' : 'text-red-400'}`}>
              {testResult.success ? '测试成功' : '测试失败'}
            </h3>
            <button onClick={() => setTestResult(null)} className="text-gray-500 hover:text-gray-300">关闭</button>
          </div>
          <p className="text-sm text-gray-300 mb-2">{testResult.message}</p>
          {testResult.data.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 text-left">
                    <th className="p-1">排名</th>
                    <th className="p-1">交易对</th>
                    <th className="p-1">来源</th>
                    <th className="p-1">涨跌幅</th>
                    <th className="p-1">24h成交量</th>
                  </tr>
                </thead>
                <tbody>
                  {testResult.data.map((item: any, i: number) => (
                    <tr key={i} className="border-t border-gray-700/50">
                      <td className="p-1 text-gray-400">{item.rank}</td>
                      <td className="p-1 font-medium">{item.symbol}</td>
                      <td className={`p-1 ${item.source === 'gainers' ? 'text-green-400' : 'text-red-400'}`}>
                        {item.source === 'gainers' ? '涨幅榜' : '跌幅榜'}
                      </td>
                      <td className={`p-1 ${item.price_change_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                        {item.price_change_pct >= 0 ? '+' : ''}{item.price_change_pct.toFixed(2)}%
                      </td>
                      <td className="p-1 text-gray-400">{item.volume_24h ? (item.volume_24h / 1e6).toFixed(1) + 'M' : '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <div className="flex items-center gap-4 mb-4 flex-wrap">
          <div>
            <label className="text-xs text-gray-400 block mb-1">榜单筛选</label>
            <select value={source} onChange={(e) => setSource(e.target.value)} className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm">
              <option value="">全部</option>
              <option value="gainers">涨幅榜</option>
              <option value="losers">跌幅榜</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">刷新间隔(秒)</label>
            <input
              type="number" value={config.refresh_interval_seconds}
              onChange={(e) => setConfig({ ...config, refresh_interval_seconds: parseInt(e.target.value) || 3600 })}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-24"
            />
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">榜单来源</label>
            <select value={config.pool_source} onChange={(e) => setConfig({ ...config, pool_source: e.target.value })} className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm">
              <option value="both">两者</option>
              <option value="gainers">仅涨幅榜</option>
              <option value="losers">仅跌幅榜</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-400 block mb-1">最大数量</label>
            <input
              type="number" value={config.max_symbols}
              onChange={(e) => setConfig({ ...config, max_symbols: parseInt(e.target.value) || 20 })}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-1.5 text-sm w-20"
            />
          </div>
          <button onClick={handleConfigUpdate} className="mt-4 px-3 py-1.5 bg-green-600 hover:bg-green-700 rounded text-sm">保存配置</button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-500 text-left border-b border-gray-800">
                <th className="p-2">排名</th>
                <th className="p-2">交易对</th>
                <th className="p-2">来源</th>
                <th className="p-2">涨跌幅</th>
                <th className="p-2">24h成交量</th>
                <th className="p-2">加入时间</th>
              </tr>
            </thead>
            <tbody>
              {coins.map((c) => (
                <tr key={c.id} className="border-b border-gray-800/50">
                  <td className="p-2 text-gray-400">#{c.rank}</td>
                  <td className="p-2 font-medium">{c.symbol}</td>
                  <td className="p-2">
                    <span className={`flex items-center gap-1 ${c.source === 'gainers' ? 'text-green-400' : 'text-red-400'}`}>
                      {c.source === 'gainers' ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
                      {c.source === 'gainers' ? '涨幅榜' : '跌幅榜'}
                    </span>
                  </td>
                  <td className={`p-2 ${c.price_change_pct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {c.price_change_pct >= 0 ? '+' : ''}{c.price_change_pct.toFixed(2)}%
                  </td>
                  <td className="p-2 text-gray-400">{c.volume_24h ? (c.volume_24h / 1e6).toFixed(1) + 'M' : '-'}</td>
                  <td className="p-2 text-gray-500 text-xs">{new Date(c.added_at).toLocaleString()}</td>
                </tr>
              ))}
              {coins.length === 0 && (
                <tr><td colSpan={6} className="p-8 text-center text-gray-600">选币池为空，请点击刷新获取涨跌幅数据</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
