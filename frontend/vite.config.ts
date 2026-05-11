import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const buildStamp = new Date().toISOString()

export default defineConfig({
  // 固定从本目录加载 .env / .env.local，避免从仓库根目录执行命令时读不到变量
  envDir: __dirname,
  define: {
    __FRONTEND_BUILD_STAMP__: JSON.stringify(buildStamp),
  },
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://8.211.153.248:8000',
      '/ws': {
        target: 'ws://8.211.153.248:8000',
        ws: true,
      },
    },
  },
})
