import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // Load .env so VITE_* vars are available inside this config file itself.
  const env = loadEnv(mode, process.cwd(), '')

  const apiBase = env.VITE_API_BASE_URL || 'http://localhost:5000'
  const devPort = parseInt(env.VITE_PORT || '5173', 10)

  return {
    plugins: [react()],

    server: {
      port: devPort,
      // Proxy all backend routes through the dev server so the browser never
      // makes cross-origin requests during development.  Flask's CORS config
      // only needs to allow http://localhost:5173 (the Vite origin).
      proxy: {
        '/command':      { target: apiBase, changeOrigin: true },
        '/system-state': { target: apiBase, changeOrigin: true },
        '/health':       { target: apiBase, changeOrigin: true },
        '/confirm':      { target: apiBase, changeOrigin: true },
      },
    },

    build: {
      outDir:    'dist',
      sourcemap: true,
    },
  }
})
