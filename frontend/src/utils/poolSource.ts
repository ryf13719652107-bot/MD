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
