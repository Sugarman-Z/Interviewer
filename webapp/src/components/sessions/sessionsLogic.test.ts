import { describe, expect, it } from "vitest";
import type { SessionSummary } from "@/lib/types";
import { FILTERS, STATUS_LABEL, filterSessions, formatStartedAt, metaLine } from "./sessionsLogic";

function mk(partial: Partial<SessionSummary>): SessionSummary {
  return {
    session_id: "s1",
    position: "后端开发工程师",
    started_at: new Date(2026, 10, 18, 9, 0).toISOString(),
    status: "ongoing",
    progress: { answered: 4, total: 10 },
    ...partial,
  };
}

describe("filterSessions", () => {
  const list = [
    mk({ session_id: "a", status: "ongoing" }),
    mk({ session_id: "b", status: "finished", overall: 2.9 }),
    mk({ session_id: "c", status: "interrupted" }),
    mk({ session_id: "d", status: "finished", overall: 4.3 }),
  ];

  it("all 原样返回（顺序信任服务端）", () => {
    expect(filterSessions(list, "all")).toEqual(list);
  });

  it.each([
    ["ongoing", ["a"]],
    ["finished", ["b", "d"]],
    ["interrupted", ["c"]],
  ] as const)("按状态过滤：%s", (key, ids) => {
    expect(filterSessions(list, key).map((sv) => sv.session_id)).toEqual(ids);
  });

  it("FILTERS 覆盖三态 + 全部，标签齐备", () => {
    expect(FILTERS.map((f) => f.key)).toEqual(["all", "ongoing", "finished", "interrupted"]);
    for (const f of FILTERS) {
      if (f.key !== "all") expect(STATUS_LABEL[f.key]).toBe(f.label);
    }
  });
});

describe("formatStartedAt", () => {
  it("同日 → 今天 HH:mm", () => {
    expect(formatStartedAt(new Date(2026, 10, 18, 14, 30).toISOString(), new Date(2026, 10, 18, 16, 0))).toBe(
      "今天 14:30",
    );
  });

  it("跨午夜不到 24h 也算昨天（23:50 → 次日 00:10）", () => {
    expect(formatStartedAt(new Date(2026, 10, 17, 23, 50).toISOString(), new Date(2026, 10, 18, 0, 10))).toBe(
      "昨天 23:50",
    );
  });

  it("今年更早 → M月D日；跨年 → 带年份", () => {
    expect(formatStartedAt(new Date(2026, 9, 14).toISOString(), new Date(2026, 10, 18))).toBe("10月14日");
    expect(formatStartedAt(new Date(2025, 11, 31).toISOString(), new Date(2026, 0, 2))).toBe("2025年12月31日");
  });

  it("非法 ISO → 空串（渲染侧自然吞掉）", () => {
    expect(formatStartedAt("not-a-date")).toBe("");
  });
});

describe("metaLine", () => {
  it("挂起态：第 N 题与面试页头部同口径（pending_qnum 优先）", () => {
    const sv = mk({
      status: "ongoing",
      pending_qnum: "5",
      started_at: new Date(2026, 10, 18, 9, 0).toISOString(),
    });
    expect(metaLine(sv, new Date(2026, 10, 18, 12, 0))).toBe(
      "第 5 题 / 共约 10 题 · 开始于 今天 09:00",
    );
  });

  it("追问挂起：「3.1」归属第 3 题，不得显示 answered+1（差一题回归）", () => {
    const sv = mk({ status: "interrupted", pending_qnum: "2.1", progress: { answered: 2, total: 10 } });
    expect(metaLine(sv, new Date())).toContain("第 2 题");
    expect(metaLine(sv, new Date())).not.toContain("第 3 题");
  });

  it("pending_qnum 缺省时回退 answered+1（防御旧后端/开场无题号）", () => {
    const sv = mk({ status: "interrupted", progress: { answered: 2, total: 10 } });
    expect(metaLine(sv, new Date())).toMatch(/^第 3 题 \/ 共约 10 题 · 开始于 /);
  });

  it("finished：只报题数与时间，分数交给 sscore 砖", () => {
    const sv = mk({ status: "finished", progress: { answered: 10, total: 10 }, overall: 2.9 });
    expect(metaLine(sv, new Date(2026, 10, 18, 12, 0))).toBe("10 题 · 今天 09:00");
    expect(metaLine(sv, new Date(2026, 10, 18, 12, 0))).not.toContain("2.9");
  });

  it("answered 追平 total 时钳制在 total（防御异常态不显示第 11 题）", () => {
    const sv = mk({ pending_qnum: "10", progress: { answered: 10, total: 10 } });
    expect(metaLine(sv, new Date())).toContain("第 10 题");
  });
});
