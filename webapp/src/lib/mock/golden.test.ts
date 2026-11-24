/**
 * golden 双锚双端共锚——vitest 侧。
 *
 * 与 pytest 的 tests/test_web_golden.py 共锚 docs/contract/ 下同两份样本：
 * 改任一样本字段，两端测试同时红（防「双绿但各测各的」）。
 *
 * - 状态锚：真 Mock store 走「创建 → 答开场 → q1 待答」半场，toState 输出
 *   剥 session_id/at 后与样本全等（文本源自本目录 script.ts 常量——
 *   pytest 侧以同款文本喂 gateway.session_view）
 * - 聚合锚：buildFinal.overall + reportLogic.dimAverages 对样本输入同值
 *（输入行是 wire + engine 字段并集，两端各取所需）
 * - skip 语义不进双端全等，此处独立断言 Mock 侧行为
 *
 * 注意：确定性种子（固定时间 + spy Math.random）使每次 createSession 生成
 * 同一 session_id——共享 store 里后建的会话覆盖先建的。本文件各用例都在
 * 下一次创建前消费完自己的会话，串行下无碰撞。
 */
import { readFileSync } from "node:fs";

import { describe, expect, it, vi } from "vitest";

import type { SessionState, TopicResult, TranscriptMessage } from "@/lib/types";
import { dimAverages } from "@/components/report/reportLogic";
import { buildFinal } from "./script";
import { advance, createSession, getSession, toState } from "./store";

const readSample = (name: string): unknown =>
  JSON.parse(
    readFileSync(new URL(`../../../../docs/contract/${name}`, import.meta.url), "utf8"),
  );

/** §8 ② 规范化：剥传输层生成值。置 undefined 而非删键——toEqual 把
 *  undefined 属性视同键缺席（vitest 同 Jest 语义），两侧口径一致 */
const normalize = (state: SessionState): unknown => ({
  ...state,
  session_id: undefined,
  transcript: state.transcript.map((m: TranscriptMessage) => ({ ...m, at: undefined })),
});

const ANSWER_TEXT =
  "自我介绍：后端工程师，五年后端经验，主要做高并发服务与中间件，最有把握的方向是分布式系统。";

describe("状态锚：Mock store 半场输出 == session-state.sample.json", () => {
  it("创建 → 答开场 → q1 待答，awaiting 视图全等", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-20T12:00:00Z"));
    vi.spyOn(Math, "random").mockReturnValue(0.42);
    try {
      const s = createSession({ position: "后端开发" });
      advance(s.session_id, { type: "answer", text: ANSWER_TEXT });
      const state = toState(getSession(s.session_id)!);
      expect(normalize(state)).toEqual(readSample("session-state.sample.json"));
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });
});

interface AggCase {
  name: string;
  topic_results: unknown[];
  expected: {
    overall: number;
    dim_averages: {
      technical_depth: number;
      correctness: number;
      communication: number;
    };
  };
}

describe("聚合锚：buildFinal.overall + dimAverages == final-aggregation.sample.json", () => {
  it("每个用例双值全等", () => {
    const { cases } = readSample("final-aggregation.sample.json") as { cases: AggCase[] };
    expect(cases.length).toBeGreaterThanOrEqual(3); // 用例集缩水即红
    for (const c of cases) {
      const rows = c.topic_results as unknown as TopicResult[];
      expect(buildFinal(rows, "后端开发").overall, c.name).toBe(c.expected.overall);
      expect(dimAverages(rows), c.name).toEqual(c.expected.dim_averages);
    }
  });
});

describe("skip 语义专项（§8 ④：不进双端全等，独立断言）", () => {
  it("skip 轮：candidate 行 variant=skip + system 提示行 + 2/2/2 落账 + 直接换题", () => {
    const s = createSession({ position: "后端开发" });
    advance(s.session_id, { type: "answer", text: "自我介绍：后端工程师。" });
    const result = advance(s.session_id, { type: "skip" });

    const state = toState(getSession(s.session_id)!);
    const rows = state.transcript;
    // 行序：opening、开场回答、q1、skip 行、system 提示行
    expect(rows[3]).toMatchObject({
      role: "candidate",
      variant: "skip",
      text: "（跳过了此题）",
      topic: "并发与线程池",
      qnum: "1",
    });
    // system 提示行是 Mock 侧展示逻辑（：gateway 不复刻，真后端无此行）
    expect(rows[4]).toMatchObject({
      role: "system",
      variant: "system",
      text: "已跳过此题 · 我们换一道",
    });
    // 不追问，直接换下一道主题题
    expect(result.kind).toBe("question");
    expect(state.pending_question?.qnum).toBe("2");
    expect(state.pending_question?.topic).toBe("Redis 缓存一致性");
    // 低分落账（skip 不走长度分档，固定 2/2/2）
    expect(getSession(s.session_id)!.results).toEqual([
      {
        qnum: "1",
        topic: "并发与线程池",
        is_followup: false,
        skipped: true,
        scores: { technical_depth: 2, correctness: 2, communication: 2 },
      },
    ]);
  });
});
