import { create } from 'zustand';

export type UiRole = 'owner' | 'guest';

const STORAGE_KEY = 'martin_ui_role';

function authDisabled(): boolean {
  return import.meta.env.VITE_UI_AUTH_DISABLED === 'true';
}

function readStoredRole(): UiRole | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw === 'owner' || raw === 'guest') return raw;
  } catch {
    /* ignore */
  }
  return null;
}

function initialRole(): UiRole | null {
  if (authDisabled()) return 'owner';
  return readStoredRole();
}

type AuthState = {
  role: UiRole | null;
  login: (password: string) => { ok: true } | { ok: false; error: string };
  logout: () => void;
};

export const useAuthStore = create<AuthState>((set) => ({
  role: initialRole(),

  login: (password: string) => {
    if (authDisabled()) {
      set({ role: 'owner' });
      return { ok: true };
    }
    const ownerPwd = (import.meta.env.VITE_UI_OWNER_PASSWORD ?? '').trim();
    const guestPwd = (import.meta.env.VITE_UI_GUEST_PASSWORD ?? '').trim();
    const input = password.trim();
    if (ownerPwd === '' && guestPwd === '') {
      return {
        ok: false,
        error:
          '未注入登录密码：请在 frontend 目录配置 .env.local 后重新执行 npm run build，并部署新的 dist',
      };
    }
    if (ownerPwd !== '' && input === ownerPwd) {
      try {
        sessionStorage.setItem(STORAGE_KEY, 'owner');
      } catch {
        /* ignore */
      }
      set({ role: 'owner' });
      return { ok: true };
    }
    if (guestPwd !== '' && input === guestPwd) {
      try {
        sessionStorage.setItem(STORAGE_KEY, 'guest');
      } catch {
        /* ignore */
      }
      set({ role: 'guest' });
      return { ok: true };
    }
    return { ok: false, error: '密码错误' };
  },

  logout: () => {
    if (authDisabled()) return;
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    set({ role: null });
  },
}));
