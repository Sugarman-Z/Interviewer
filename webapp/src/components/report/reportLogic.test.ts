import { describe, expect, it } from "vitest";
import type { ScoredTopicResult, TopicResult } from "@/lib/types";
import { dimAverages, gradeOf, mean, weakPoints } from "./reportLogic";

const r = (
  qnum: string,
  topic: string,
  scores: [number, number, number],
  extra: Partial<TopicResult> = {},
): ScoredTopicResult => ({
  qnum,
  topic,
  is_followup: false,
  skipped: false,
  ...extra,
  scores: { technical_depth: scores[0], correctness: scores[1], communication: scores[2] },
});

/** §10-3：eval_failed 行——scores 整键缺席 */
const unscored = (qnum: string, topic: string, extra: Partial<TopicResult> = {}): TopicResult => ({
  qnum,
  topic,
  is_followup: false,
  skipped: false,
  ...extra,
});

describe("mean / dimAverages", () => {
  it("三维均分各自取全体均值并保留一位小数", () => {
    const avgs = dimAverages([
      r("1", "A", [4, 4, 4]),
      r("2", "B", [2, 2, 3]),
      r("3", "C", [3, 4, 3]),
    ]);
    expect(avgs).toEqual({ technical_depth: 3, correctness: 3.3, communication: 3.3 });
  });

  it("空结果不炸（除零防护）", () => {
    expect(dimAverages([])).toEqual({ technical_depth: 0, correctness: 0, communication: 0 });
  });

  it("eval_failed 行不进聚合：分子分母同时剔除（§10-3）", () => {
    const avgs = dimAverages([
      r("1", "A", [4, 4, 5]),
      unscored("2", "B"),
      r("3", "C", [2, 2, 2], { skipped: true }),
    ]);
    expect(avgs).toEqual({ technical_depth: 3, correctness: 3, communication: 3.5 });
  });

  it("全部未评分 → 0（除零防护延续，不伪造分数）", () => {
    expect(dimAverages([unscored("1", "A"), unscored("2", "B")])).toEqual({
      technical_depth: 0,
      correctness: 0,
      communication: 0,
    });
  });
});

describe("gradeOf · 等级文案", () => {
  it("4.5/3.5/2.5 三档分界", () => {
    expect(gradeOf(4.5)).toBe("优秀");
    expect(gradeOf(4.4)).toBe("良好");
    expect(gradeOf(3.5)).toBe("良好");
    expect(gradeOf(3.4)).toBe("合格");
    expect(gradeOf(2.5)).toBe("合格");
    expect(gradeOf(2.4)).toBe("待提高");
  });
});

describe("weakPoints · 薄弱点归并", () => {
  it("均分 <4 才入选，弱者在前；同考点多题归并题号", () => {
    const weak = weakPoints([
      r("1", "并发与线程池", [4, 4, 4]), // 4.0 强，不入
      r("5", "消息队列", [2, 3, 3]), // 2.7
      r("5.1", "消息队列", [2, 2, 3], { is_followup: true }), // 2.3，同考点归并
      r("6", "分布式锁", [2, 2, 2], { skipped: true }), // 2.0 最弱
    ]);
    expect(weak.map((w) => w.topic)).toEqual(["分布式锁", "消息队列"]);
    expect(weak[1].qnums).toEqual(["5", "5.1"]);
    expect(weak[0].skipped).toBe(true);
    expect(weak[1].mean).toBe(2.5); // 两组均值再取并保留一位：((2.7+2.3)/2)
  });

  it("全强场次返回空数组", () => {
    expect(weakPoints([r("1", "A", [4, 4, 4]), r("2", "B", [5, 5, 5])])).toEqual([]);
  });

  it("未评分行无均分，不参与薄弱点归并（§10-3）", () => {
    const weak = weakPoints([unscored("2", "Redis 缓存一致性"), r("5", "消息队列", [2, 3, 3])]);
    expect(weak.map((w) => w.topic)).toEqual(["消息队列"]);
  });
});

describe("mean", () => {
  it("追问均分", () => {
    expect(mean(r("1.1", "X", [2, 2, 3]))).toBeCloseTo(7 / 3, 5);
  });
});
