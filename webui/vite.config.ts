import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 构建产物直接放进 Python 包里（src/jianshanagent/admin/static/），由机器人自己的 HTTP 服务提供。
// 开发时 `npm run dev` 起前端，接口转发给本机跑着的机器人（JIANSHANAGENT_ADMIN_API 可以改地址）。
const api = process.env.JIANSHANAGENT_ADMIN_API ?? "http://127.0.0.1:18090";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  build: {
    outDir: path.resolve(import.meta.dirname, "../src/jianshanagent/admin/static"),
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: { proxy: { "/api": { target: api, changeOrigin: false } } },
});
