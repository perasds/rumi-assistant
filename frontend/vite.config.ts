import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  build: mode === 'book' ? { outDir: 'dist-book', rolldownOptions: { input: 'book.html' } } : undefined,
  server: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: { '/api': { target: process.env.RUMI_API_TARGET ?? 'http://127.0.0.1:8000', changeOrigin: false } },
  },
  test: { environment: 'jsdom', restoreMocks: true, unstubGlobals: true },
}))
