import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, CandlestickChart, BrainCircuit,
  ListOrdered, History, Database, Settings,
} from 'lucide-react';

const links = [
  { to: '/', icon: LayoutDashboard, label: '仪表盘' },
  { to: '/chart', icon: CandlestickChart, label: '图表分析' },
  { to: '/strategies', icon: BrainCircuit, label: '策略管理' },
  { to: '/positions', icon: ListOrdered, label: '当前持仓' },
  { to: '/trades', icon: History, label: '交易历史' },
  { to: '/coin-pool', icon: Database, label: '选币池' },
  { to: '/settings', icon: Settings, label: '系统设置' },
];

export default function Sidebar() {
  return (
    <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col">
      <div className="p-4 border-b border-gray-800">
        <h1 className="text-lg font-bold text-blue-400">智能对冲马丁</h1>
        <p className="text-xs text-gray-500">Smart Hedge Martin</p>
      </div>
      <nav className="flex-1 p-2 space-y-1">
        {links.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                isActive
                  ? 'bg-blue-600/20 text-blue-400'
                  : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'
              }`
            }
          >
            <Icon size={18} />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="p-3 border-t border-gray-800 text-xs text-gray-600 text-center">
        v0.1.0
      </div>
    </aside>
  );
}
