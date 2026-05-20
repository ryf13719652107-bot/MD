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
