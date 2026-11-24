/**
 * POST /api/sessions/[id]/answers —— 提交回答或跳过（Mock）
 * 返回下一问（question）或终局（final）；延迟模拟引擎评分+出题的耗时。
 */
import { NextResponse, type NextRequest } from "next/server";
import {
  advance,
  MockFinished,
  MockNotFound,
  release,
  sleepMockDelay,
  tryAcquire,
} from "@/lib/mock/store";
import type { AnswerRequest } from "@/lib/types";

export async function POST(
  req: NextRequest,
  ctx: RouteContext<"/api/sessions/[id]/answers">,
) {
  const { id } = await ctx.params;
  const body = (await req.json().catch(() => null)) as AnswerRequest | null;
  const valid =
    body !== null &&
    ((body.type === "answer" && typeof body.text === "string" && body.text.trim().length > 0) ||
      body.type === "skip");
  if (!valid) {
    return NextResponse.json({ error: "请求格式不正确：需要 {type:\"answer\",text} 或 {type:\"skip\"}" }, { status: 400 });
  }
  // 占锁在延迟之前：并发/重试的重复提交在等待期就被挡下
  if (!tryAcquire(id)) {
    return NextResponse.json({ error: "上一条回答还在处理中，请稍候再试" }, { status: 409 });
  }
  try {
    const normalized: AnswerRequest =
      body.type === "answer" ? { type: "answer", text: body.text.trim() } : { type: "skip" };
    await sleepMockDelay();
    const result = advance(id, normalized);
    return NextResponse.json(result);
  } catch (e) {
    if (e instanceof MockNotFound) {
      return NextResponse.json({ error: "会话不存在或已失效" }, { status: 404 });
    }
    if (e instanceof MockFinished) {
      return NextResponse.json({ error: "本场面试已结束" }, { status: 409 });
    }
    throw e;
  } finally {
    release(id);
  }
}
