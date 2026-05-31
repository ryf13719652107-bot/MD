/** 将 USDT 成交额格式化为「亿」「万」中文单位 */
export function formatUsdtVolume(value: number | null | undefined): string {
  if (value == null || value <= 0 || !Number.isFinite(value)) return '-';

  const YI = 1e8;
  const WAN = 1e4;

  if (value >= YI) {
    return trimTrailingZeros(value / YI, 1) + '亿';
  }
  if (value >= WAN) {
    return trimTrailingZeros(value / WAN, 1) + '万';
  }
  return Math.round(value).toLocaleString('zh-CN');
}

function trimTrailingZeros(n: number, decimals: number): string {
  return n
    .toFixed(decimals)
    .replace(/\.0+$/, '')
    .replace(/(\.\d)0$/, '$1');
}

/** 最近结算资金费率(%) 展示 */
export function formatFundingRatePct(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(4)}%`;
}

export function fundingRateColorClass(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return 'text-gray-500';
  if (value > 0) return 'text-orange-400';
  if (value < 0) return 'text-cyan-400';
  return 'text-gray-400';
}
