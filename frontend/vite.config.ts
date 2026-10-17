import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：JS API（ElMessage 等）由 AutoImport 注入，并带出对应样式
    AutoImport({
      imports: ['vue', 'vue-router', 'pinia'],
      resolvers: [ElementPlusResolver()],
      dts: 'src/auto-imports.d.ts',
      eslintrc: { enabled: false },
    }),
    // 组件按需引入：模板里写 <el-button> 即自动注册并注入样式，避免全量打包
    Components({
      resolvers: [ElementPlusResolver()],
      dts: 'src/components.d.ts',
      dirs: [],
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 开发环境把 /api 透传给本地后端，避免跨域
    proxy: {
      '/api': {
        // 写 127.0.0.1 而不是 localhost：后端 `uvicorn --host 127.0.0.1` 只监听 IPv4，
        // 而 Node 17+ 解析 localhost 时按系统返回顺序、可能先试 ::1 —— 表现是
        // 「浏览器能打开页面，但所有接口 500/ECONNREFUSED」，且和后端日志对不上。
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
})
