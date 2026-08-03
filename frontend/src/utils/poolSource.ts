export function poolSourceLabel(source: string): string {
  switch (source) {
    case 'gainers':
      return '涨幅榜';
    case 'losers':
      return '跌幅榜';
    case 'both':
      return '涨幅+跌幅';
    default:
      return source;
  }
}

export function poolSourceRankLabel(source: string): string {
  switch (source) {
    case 'gainers':
      return '涨幅榜前';
    case 'losers':
      return '跌幅榜前';
    case 'both':
      return '涨跌各前';
    default:
      return '榜单前';
  }
}

/** both 时 top_n 为每侧名次，合计最多 2N */
export function poolTopNHint(source: string, topN: number): string {
  if (source === 'both') {
    return `涨幅前${topN}+跌幅前${topN}（合计最多${topN * 2}）`;
  }
  return `${poolSourceRankLabel(source)}${topN}名`;
}

export function poolSourceBadgeClass(source: string): string {
  if (source === 'gainers') return 'bg-green-600/20 text-green-400';
  if (source === 'losers') return 'bg-red-600/20 text-red-400';
  return 'bg-gray-600/20 text-gray-400';
}

export function poolSourceTextClass(source: string): string {
  if (source === 'gainers') return 'text-green-400';
  if (source === 'losers') return 'text-red-400';
  return 'text-gray-400';
}
