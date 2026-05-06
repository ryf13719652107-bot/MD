import { create } from 'zustand';

interface SettingsState {
  theme: 'dark' | 'light';
  setTheme: (t: 'dark' | 'light') => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  theme: 'dark',
  setTheme: (t) => set({ theme: t }),
}));
