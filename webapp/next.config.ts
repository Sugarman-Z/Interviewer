import type { NextConfig } from "next";

// cacheComponents/partialPrefetching（Next 16 CNA 默认开启）已显式关闭：
// 本应用是客户端数据驱动形态，新缓存模型只增加每个动态页/路由的显式声明
// 负担，没有收益。真后端接入后如需服务端缓存再评估开启。
// 依据：node_modules/next/dist/docs/01-app/01-getting-started/08-caching.md
//
// 切流（SPEC-api-service §10-5）：同源 /api 经 dev 代理到 FastAPI。必须用
// beforeFiles 形态——数组/afterFiles 会被同路径 Mock route.ts 吃掉，实测成
// 「向导/列表走 Mock、详情/回答走真后端」的静默混搭。
const nextConfig: NextConfig = {
  // compress: false（T8 e2e 抓到的流式杀手）：dev 代理对客户端声明
  // Accept-Encoding: gzip 的 rewrite 响应做**缓冲式整流压缩**——SSE 全部
  // 事件在流收尾时一次性到达（实测 identity 逐块 5.7-6.7s vs gzip 全量
  // 8.05s 同毫秒），浏览器恒带 gzip 头且 fetch 禁改该头，流式体验会整体
  // 失效。关压缩换逐块透传（本机回环单用户，无带宽代价）
  compress: false,
  experimental: {
    // 外源 rewrite 代理默认 30s 掐长响应（next/dist/esm/server/lib/router-utils/
    // proxy-request.js:22 `proxyTimeout || 30000`；实测 125s 响应在 30.0s 处
    // 500）——提到 190s 盖过前端提交超时 180s（useInterview SUBMIT_TIMEOUT_MS）。
    // 真收场轮 ReportWriter(deep) 常 40-120s，不调此值切流即断（SPEC §10-9
    // 的 API_BASE 直连是备选逃生，本修法优先生效）。流式化后此值只兜
    // 无字节窗口——15s keepalive ping 使活跃流永不空闲超时（SPIKE-3）
    proxyTimeout: 190_000,
  },
  async rewrites() {
    return {
      beforeFiles: [
        {
          source: "/api/:path*",
          destination: "http://127.0.0.1:8000/api/:path*",
        },
      ],
    };
  },
};

export default nextConfig;
