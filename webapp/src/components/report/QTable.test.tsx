// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { TopicResult } from "@/lib/types";
import QTable from "./QTable";

afterEach(cleanup);

const r = (
  qnum: string,
  topic: string,
  scores: [number, number, number],
  extra: Partial<TopicResult> = {},
): TopicResult => ({
  qnum,
  topic,
  is_followup: false,
  skipped: false,
  scores: { technical_depth: scores[0], correctness: scores[1], communication: scores[2] },
  ...extra,
});

/** §10-3：eval_failed 行——scores 整键缺席 */
const unscored = (qnum: string, topic: string): TopicResult => ({
  qnum,
  topic,
  is_followup: false,
  skipped: false,
});

describe("QTable · 未评分行（§10-3）", () => {
  it("eval_failed 行四格占位「未评分」，tfoot 只计已评分行", () => {
    render(
      <QTable
        results={[
          r("1", "并发与线程池", [4, 4, 4]),
          unscored("2", "Redis 缓存一致性"),
          r("3", "MySQL 索引", [2, 2, 2]),
        ]}
        overall={3}
      />,
    );
    // 未评分行的三个维度分格 + 均分格
    expect(screen.getAllByText("未评分")).toHaveLength(4);
    // tfoot：各维 (4+2)/2 = 3.0（×3）+ overall 3.0——分母不含未评分行
    expect(screen.getAllByText("3.0")).toHaveLength(4);
    // 已评分行照常渲染：分数格 4×3；均分列 4.0 / 2.0（题号列另有一个 "2"）
    expect(screen.getAllByText("4")).toHaveLength(3);
    expect(screen.getAllByText("4.0")).toHaveLength(1);
    expect(screen.getAllByText("2.0")).toHaveLength(1);
  });
});
