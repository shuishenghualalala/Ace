import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式把 /api 与 /ws 代理到后端网关，构建产物输出到 dist 由网关托管。
// 网关端口解析顺序：
//   1. VITE_GATEWAY_PORT / GATEWAY_PORT 环境变量（显式指定优先）；
//   2. 网关发现文件 {CREW_HOME}/run/gateway.json——网关启动时写入实际监听端口，
//      读取后仍会探测 /api/health 确认存活（容忍崩溃残留的旧文件）；
//   3. 都不可用则回落到 8000（保持历史默认，请求失败由页面报错）。
const DEFAULT_GATEWAY_PORT = 8000;
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function crewHome(): string {
  const val = (process.env.CREW_HOME || "").trim();
  if (val) return isAbsolute(val) ? val : join(process.env.HOME || "~", val);
  return join(repoRoot, ".Crew");
}

async function probeGateway(port: number): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
      signal: AbortSignal.timeout(800),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function portFromDiscoveryFile(): Promise<number | null> {
  const file = join(crewHome(), "run", "gateway.json");
  try {
    if (!existsSync(file)) return null;
    const { port } = JSON.parse(readFileSync(file, "utf-8"));
    if (Number.isInteger(port) && port > 0 && port < 65536 && (await probeGateway(port))) {
      return port;
    }
  } catch {
    // 文件损坏/不可读按无发现文件处理
  }
  return null;
}

async function resolveGatewayPort(): Promise<number> {
  const envPort = Number(process.env.VITE_GATEWAY_PORT || process.env.GATEWAY_PORT);
  if (Number.isInteger(envPort) && envPort > 0 && envPort < 65536) return envPort;
  const discovered = await portFromDiscoveryFile();
  if (discovered !== null) return discovered;
  return DEFAULT_GATEWAY_PORT;
}

export default defineConfig(async () => {
  const gatewayPort = await resolveGatewayPort();
  const httpTarget = `http://127.0.0.1:${gatewayPort}`;
  console.log(`[vite] proxy /api,/ws -> ${httpTarget}`);
  return {
    plugins: [react()],
    build: { outDir: "dist" },
    server: {
      port: 5173,
      proxy: {
        "/api": { target: httpTarget, changeOrigin: true },
        "/ws": { target: httpTarget.replace(/^http/, "ws"), ws: true },
      },
    },
  };
});
