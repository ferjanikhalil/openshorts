import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend target for the dev proxy. Defaults to the docker-compose service
// name; set VITE_PROXY_TARGET=http://localhost:8000 to run the dev server on
// the host against a backend reachable at localhost (no CORS, same-origin).
const backend = process.env.VITE_PROXY_TARGET || 'http://backend:8000'
const renderer = process.env.VITE_RENDER_TARGET || 'http://renderer:3100'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: [
      'openshorts.app',
      'www.openshorts.app'
    ],
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
        timeout: 0,
        configure: (proxy) => {
          // Handle large file uploads without crashing
          proxy.on('proxyReq', (proxyReq, req) => {
            if (req.headers['content-type']?.includes('multipart/form-data')) {
              proxyReq.setHeader('Transfer-Encoding', 'chunked');
            }
          });
        }
      },
      '/videos': { target: backend, changeOrigin: true, timeout: 0 },
      '/thumbnails': { target: backend, changeOrigin: true, timeout: 0 },
      '/gallery': { target: backend, changeOrigin: true, timeout: 0 },
      '/video': { target: backend, changeOrigin: true, timeout: 0 },
      '/render': { target: renderer, changeOrigin: true, timeout: 0 },
    }
  }
})
