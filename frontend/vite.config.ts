import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const buildStamp = new Date().toISOString()

export default defineConfig({
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
