// prodai-atlas console — Vite SPA. 운영에서는 Manager 가 embed.FS 로 내장(PRD §16.1), 그 전까지 nginx 정적 서빙.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const managerUrl = process.env.MANAGER_URL ?? "http://127.0.0.1:8080";
const gatewayUrl = process.env.GATEWAY_URL ?? "http://127.0.0.1:8090";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: managerUrl },
      "/health/manager": { target: managerUrl, rewrite: () => "/health" },
      "/gw": { target: gatewayUrl, rewrite: (p) => p.replace(/^\/gw/, "") },
      "/svc": { target: managerUrl },   // Manager 가 플랫폼 서비스로 중계 (ATLAS_SVC_URLS)
    },
  },
});
