/**
 * api-service 客户端。默认同源：Next rewrite 把 /api 代理到 FastAPI，本文件
 * 路径零改动。API_BASE 是代理层异常时的直连逃生通道——NEXT_PUBLIC_API_BASE
 * 显式设置（如 http://127.0.0.1:8000）即绕过 rewrite 直连，依赖服务端
 * CORSMiddleware（allow_origins 含 http://localhost:3000）作前置。
 */
import type {
  AnswerRequest,
  AnswerResult,
  SessionCreated,
  SessionState,
  SessionSummary,
  StreamEvent,
} from "./types";
import { parseSSE } from "./sse";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData;
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(isForm ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { error?: string } | null;
    throw new ApiError(res.status, body?.error ?? `请求失败（HTTP ${res.status}）`);
  }
  return (await res.json()) as T;
}

/** 最小韧性（列表/向导够用）：单次超时 + 失败后立即重试一次。
 *  面试页 restore/对账的 15s/30s 分级退避在 useInterview，语义不同档不共用 */
export async function withTimeoutRetry<T>(
  call: (signal: AbortSignal) => Promise<T>,
  timeoutMs: number,
): Promise<T> {
  const attempt = (): Promise<T> => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    return call(ctrl.signal).finally(() => clearTimeout(timer));
  };
  try {
    return await attempt();
  } catch {
    return await attempt();
  }
}

export interface CreateSessionPayload {
  position: string;
  jd?: string;
  resumeText?: string;
  resumeFile?: File;
}

export function createSession(p: CreateSessionPayload, signal?: AbortSignal): Promise<SessionCreated> {
  if (p.resumeFile) {
    const fd = new FormData();
    fd.set("position", p.position);
    if (p.jd) fd.set("jd", p.jd);
    if (p.resumeText) fd.set("resume_text", p.resumeText);
    fd.set("resume_file", p.resumeFile);
    return request("/api/sessions", { method: "POST", body: fd, signal });
  }
  return request("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ position: p.position, jd: p.jd, resume_text: p.resumeText }),
    signal,
  });
}

export function submitAnswer(
  sessionId: string,
  body: AnswerRequest,
  signal?: AbortSignal,
): Promise<AnswerResult> {
  return request(`/api/sessions/${sessionId}/answers`, {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });
}

/**
 * 流式应答：POST /answers/stream，SSE 帧经 parseSSE 逐事件回调。
 * 结局集（枚举完备）：
 * - resolve：流正常收尾，或任一阶段的主动 abort（fetch 段与读流段都
 *   静默——中止总是调用方自己的决定，不产生未处理拒绝）。约束：中止须用
 *   裸 `ctrl.abort()`；`AbortSignal.timeout()`/`abort(reason)` 的拒绝名不是
 *   AbortError，会绕过静默分支按异常穿出。
 * - reject ApiError：流前非 2xx（与 request() 同型，状态码+文案）或流中
 *   error 事件（500）。
 * - reject 其他（TypeError 等）：流中网络断/意外异常。
 * 对账判据是「onEvent 是否已收到 round」，不是 resolve/reject 形态——
 * round 后无论静默收尾还是异常穿出都走 GET-对账。
 * `onActivity`：字节级活性钩子——读循环每收到 chunk 即调，含纯注释片
 *（`: ping` 保活解析出零事件但字节是活的）。idle watchdog 必须挂此处
 * 而非 onEvent：判据是「无字节间隔 45s」，服务端 15s ping 的设计目的
 * 就是桥接事件静默期。
 */
export async function streamAnswer(
  sessionId: string,
  body: AnswerRequest,
  opts: {
    onEvent: (ev: StreamEvent) => void;
    /** 字节级活性（每 chunk 一次，ping 注释片也算）——watchdog 喂活源 */
    onActivity?: () => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  try {
    const res = await fetch(`${API_BASE}/api/sessions/${sessionId}/answers/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: opts.signal,
    });
    if (!res.ok) {
      const errBody = (await res.json().catch(() => null)) as { error?: string } | null;
      throw new ApiError(res.status, errBody?.error ?? `请求失败（HTTP ${res.status}）`);
    }
    if (!res.body) {
      throw new ApiError(500, "流式响应无内容");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        opts.onActivity?.(); // 字节活着（含 ping 注释片）——watchdog 语义
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseSSE(buffer);
        buffer = rest;
        for (const ev of events) {
          if (ev.event === "error") {
            throw new ApiError(500, ev.error);
          }
          opts.onEvent(ev);
        }
      }
      // 收尾不留尾巴：服务端每帧后必发 \n\n（含终局帧），正常关闭时缓冲
      // 必为空；有残片即半截帧，丢弃——由 resolve-未见终局走上层对账
    } finally {
      reader.releaseLock();
    }
  } catch (err) {
    if ((err as { name?: string } | null)?.name === "AbortError") {
      return; // 主动中止（watchdog/卸载，fetch 段或读流段）：静默返回
    }
    throw err;
  }
}

export function getSession(sessionId: string, signal?: AbortSignal): Promise<SessionState> {
  return request(`/api/sessions/${sessionId}`, { signal });
}

export function listSessions(signal?: AbortSignal): Promise<SessionSummary[]> {
  return request("/api/sessions", { signal });
}
