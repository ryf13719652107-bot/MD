import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard, CandlestickChart, BrainCircuit,
  ListOrdered, History, Database, Settings,
  LogOut,
} from 'lucide-react';
import { useAuthStore } from '../../store/authStore';

const links = [
  { to: '/', icon: LayoutDashboard, label: '仪表盘', guestAllowed: true },
  { to: '/chart', icon: CandlestickChart, label: '图表分析', guestAllowed: false },
  { to: '/strategies', icon: BrainCircuit, label: '策略管理', guestAllowed: false },
  { to: '/positions', icon: ListOrdered, label: '当前持仓', guestAllowed: true },
  { to: '/trades', icon: History, label: '交易历史', guestAllowed: true },
  { to: '/coin-pool', icon: Database, label: '选币池', guestAllowed: false },
  { to: '/settings', icon: Settings, label: '系统设置', guestAllowed: false },
];

export default function Sidebar() {
  const role = useAuthStore((s) => s.role);
  const logout = useAuthStore((s) => s.logout);
  const authOff = import.meta.env.VITE_UI_AUTH_DISABLED === 'true';

  return (
    <aside className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col">
      <div className="p-4 border-b border-gray-800">
        <h1 className="text-lg font-bold text-blue-400">智能对冲马丁</h1>
        <p className="text-xs text-gray-500">Smart Hedge Martin</p>
      </div>

      <nav className="flex-1 p-2 space-y-1">
        {links.map(({ to, icon: Icon, label, guestAllowed }) => {
          const locked = role === 'guest' && !guestAllowed;
          if (locked) {
            return (
              <div
                key={to}
                title="需要主人权限"
                className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm text-gray-600 cursor-not-allowed pointer-events-none select-none"
              >
                <Icon size={18} />
                {label}
              </div>
            );
          }
          return (
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
          );
        })}
      </nav>

      <div className="p-3 border-t border-gray-800 space-y-2">
        {!authOff && (
          <button
            type="button"
            onClick={() => logout()}
            className="w-full flex items-center justify-center gap-2 py-2 rounded-lg text-xs text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
          >
            <LogOut size={14} />
            退出登录
          </button>
        )}
        <div className="text-xs text-gray-600 text-center">v0.1.0</div>
      </div>
    </aside>
  );
}
