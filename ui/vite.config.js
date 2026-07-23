import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://api:8001'
  const marqoProxyTarget = env.VITE_MARQO_PROXY_TARGET || 'http://marqo:8882'
  // Production/subpath deploy: VITE_BASE=/docs-pipeline/
  // Local `npm run dev` defaults to '/' unless VITE_BASE is set in ui/.env
  const base = env.VITE_BASE || '/'

  return {
    base,
    plugins: [tailwindcss(), react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      host: '0.0.0.0',
      port: 3000,
      allowedHosts: true,
      watch: {
        usePolling: true,
        interval: 1000,
      },
      proxy: {
        '/api': {
          target: apiProxyTarget,
          changeOrigin: true,
          rewrite: (requestPath) => requestPath.replace(/^\/api/, '')
        },
        '/marqo': {
          target: marqoProxyTarget,
          changeOrigin: true,
          rewrite: (requestPath) => requestPath.replace(/^\/marqo/, '')
        }
      }
    }
  }
})
