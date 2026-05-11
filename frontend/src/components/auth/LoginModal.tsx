import { useState, FormEvent } from 'react';
import { Lock } from 'lucide-react';
import { useAuthStore } from '../../store/authStore';

export default function LoginModal() {
  const login = useAuthStore((s) => s.login);
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const submit = () => {
    setError('');
    const result = login(password);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setPassword('');
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    submit();
  };

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center p-4">
      <div className="w-full max-w-md bg-gray-900 border border-gray-800 rounded-xl shadow-xl p-8">
        <div className="flex flex-col items-center gap-2 mb-6">
          <div className="p-3 rounded-full bg-blue-600/20 text-blue-400">
            <Lock size={28} />
          </div>
          <h1 className="text-xl font-bold text-gray-100">智能对冲马丁</h1>
          <p className="text-sm text-gray-500 text-center">请输入密码登录</p>
        </div>

        <form onSubmit={onSubmit} className="space-y-4">
          <div>
            <label htmlFor="ui-password" className="sr-only">
              密码
            </label>
            <input
              id="ui-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密码"
              className="w-full px-4 py-3 rounded-lg bg-gray-950 border border-gray-700 text-gray-100 placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-600/50 focus:border-blue-600"
            />
          </div>
          {error && (
            <p className="text-sm text-red-400 bg-red-900/20 rounded-lg px-3 py-2">{error}</p>
          )}
          <button
            type="submit"
            className="w-full py-3 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-medium transition-colors"
          >
            登录
          </button>
        </form>
      </div>
    </div>
  );
}
