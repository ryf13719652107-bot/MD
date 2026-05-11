/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_UI_OWNER_PASSWORD?: string;
  readonly VITE_UI_GUEST_PASSWORD?: string;
  readonly VITE_UI_AUTH_DISABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare const __FRONTEND_BUILD_STAMP__: string
