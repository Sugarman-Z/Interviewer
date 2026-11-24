/**
 * POST /api/sessions/[id]/answers/stream —— 流式应答（Mock）
 * 事件序 = script.streamScript 纯函数（与真后端同合同，
 * docs/contract/stream-events.sample.json 单一定义点）；路由只负责锁、
 * 编码与节奏：round/stage 即发，delta 间隔 5ms，终局后收流。
 * 流前失败（400/404/409）普通 JSON——advance 在开流前完成，天然两段论。
 * 无 handler 级测试：与三个既有 Mock route 同形态（vitest 直调 store，
 * 事件序由 script.test.ts 读表锁）。
 */
import { type NextRequest } from "next/server";
import {
  advance,
  getSession,
  MockFinished,
  MockNotFound,
  release,
  tryAcquire,
} from "@/lib/mock/store";
import { sseFrame, streamScript } from "@/lib/mock/script";
import type { AnswerRequest } from "@/lib/types";

const DELTA_GAP_MS = 5;

export async function POST(
  req: NextRequest,
  ctx: RouteContext<"/api/sessions/[id]/answers/stream">,
) {
  const { id } = await ctx.params;
  const body = (await req.json().catch(() => null)) as AnswerRequest | null;
  const valid =
    body !== null &&
    ((body.type === "answer" && typeof body.text === "string" && body.text.trim().length > 0) ||
      body.type === "skip");
  if (!valid) {
    return Response.json({ error: "请求格式不正确：需要 {type:\"answer\",text} 或 {type:\"skip\"}" }, { status: 400 });
  }
  if (!tryAcquire(id)) {
    return Response.json({ error: "上一条回答还在处理中，请稍候再试" }, { status: 409 });
  }
  let events;
  try {
    // round 载荷的槽位号：取推进前的待答题（opening 字面量口径与真后端一致）
    const pending = getSession(id)?.pending_question;
    if (!pending) {
      const known = getSession(id) !== undefined;
      throw known ? new MockFinished() : new MockNotFound();
    }
    // 非 opening 的待答题恒带 qnum（main/followup 都有题号）
    const slotExpect = pending.variant === "opening" ? "opening" : pending.qnum!;
    const normalized: AnswerRequest =
      body.type === "answer" ? { type: "answer", text: body.text.trim() } : { type: "skip" };
    const result = advance(id, normalized);
    events = streamScript(slotExpect, result);
  } catch (e) {
    release(id);
    if (e instanceof MockNotFound) {
      return Response.json({ error: "会话不存在或已失效" }, { status: 404 });
    }
    if (e instanceof MockFinished) {
      return Response.json({ error: "本场面试已结束" }, { status: 409 });
    }
    throw e;
  }
  // 成功路径：锁活到流收尾（finally 提前放会把回放期的 in-flight 409
  // 窗口塌缩到 0，第二发可穿透 double-advance）。事件序已定格（advance
  // 完成），回放只是节奏——「锁寿命 = 回合寿命」：正常收尾（含客户端
  // 断开 cancel）才放
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      try {
        for (const ev of events) {
          controller.enqueue(encoder.encode(sseFrame(ev)));
          if (ev.event === "delta") await new Promise((r) => setTimeout(r, DELTA_GAP_MS));
        }
        controller.close();
      } finally {
        release(id);
      }
    },
    cancel() {
      release(id);
    },
  });
  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      // 与真后端 wire 三头对称（pytest 侧正向锁；Mock 不过 nginx 但合同同形）
      "x-accel-buffering": "no",
    },
  });
}
