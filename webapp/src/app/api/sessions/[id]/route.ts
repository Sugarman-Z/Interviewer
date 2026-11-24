/**
 * GET /api/sessions/[id] —— 会话详情（Mock）：transcript 回放 + 当前待答题，
 * 前端刷新/断线恢复的唯一数据源。
 */
import { NextResponse, type NextRequest } from "next/server";
import { getSession, toState } from "@/lib/mock/store";

export async function GET(
  _req: NextRequest,
  ctx: RouteContext<"/api/sessions/[id]">,
) {
  const { id } = await ctx.params;
  const s = getSession(id);
  if (!s) {
    return NextResponse.json({ error: "会话不存在或已失效" }, { status: 404 });
  }
  return NextResponse.json(toState(s));
}
