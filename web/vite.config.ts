import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式把 /api 与 /ws 代理到后端网关（:8000），构建产物输出到 dist 由网关托管。
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
