// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FinalPayload, SessionState, TopicResult } from "@/lib/types";

const getSessionMock = vi.fn();
vi.mock("@/lib/api", () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
    ) {
      super(message);
    }
  },
  getSession: (id: string, signal?: AbortSignal) => getSessionMock(id, signal),
}));

import ReportShell from "./ReportShell";

afterEach(cleanup);

const RESULTS: TopicResult[] = [
  { qnum: "1", topic: "并发与线程池", is_followup: false, skipped: false, scores: { technical_depth: 4, correctness: 4, communication: 4 } },
  { qnum: "1.1", topic: "扩容时机与代价", is_followup: true, skipped: false, scores: { technical_depth: 2, correctness: 2, communication: 3 } },
  { qnum: "2", topic: "Redis 缓存一致性", is_followup: false, skipped: true, scores: { technical_depth: 2, correctness: 2, communication: 2 } },
  { qnum: "3", topic: "系统设计", is_followup: false, skipped: false, scores: { technical_depth: 3, correctness: 4, communication: 3 } },
];

const FINAL: FinalPayload = {
  overall: 2.9,
  interview_report:
    "本场针对「后端开发工程师」岗位共 3 题（含 1 次追问），综合评分 **2.9 / 5**。\n\n## 表现亮点\n- **并发与线程池**（4.0 分）：回答完整。\n\n## 薄弱环节\n- **Redis 缓存一致性**（2.0 分）：建议重整。",
  topic_results: RESULTS,
};

function finishedSession(): SessionState {
  return {
    session_id: "s9",
    position: "后端开发工程师",
    status: "finished",
    max_questions: 10,
    transcript: [],
    progress: { answered: 3, total: 10 },
    final: FINAL,
  };
}

beforeEach(() => {
  getSessionMock.mockReset();
});

describe("ReportShell · 已收场", () => {
  it("数据全部来自 final 载荷：圆环 5 分制、等级、markdown 章节渲染", async () => {
    getSessionMock.mockResolvedValue(finishedSession());
    render(<ReportShell sessionId="s9" />);

    expect(await screen.findByRole("img", { name: "综合评分 2.9 / 5，合格" })).toBeTruthy();
    expect(screen.getByText("合格")).toBeTruthy();
    expect(screen.getByText(/3 题 · 含 1 次追问/)).toBeTruthy();
    // markdown：h2 章节与加粗考点（考点名同时出现在表格考点列，共两处）
    expect(screen.getByRole("heading", { level: 2, name: "表现亮点" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2, name: "薄弱环节" })).toBeTruthy();
    expect(screen.getAllByText("并发与线程池")).toHaveLength(2);
  });

  it("逐题表：行数=载荷长度、追问/跳过徽标、tfoot 维度均分与总分", async () => {
    getSessionMock.mockResolvedValue(finishedSession());
    const { container } = render(<ReportShell sessionId="s9" />);
    await screen.findByRole("img", { name: /综合评分/ });

    const rows = container.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(RESULTS.length);
    expect(rows[1].id).toBe("q-1.1"); // 追问行可回链
    expect(screen.getByText("追问")).toBeTruthy();
    expect(screen.getByText("已跳过")).toBeTruthy();

    // 移动端降级的维度标签：每个 td[data-d] 必须携带自己的列身份（不能是笼统「得分」）
    const labels = Array.from(rows[0].querySelectorAll("td[data-d]")).map((td) =>
      td.getAttribute("data-d"),
    );
    expect(labels).toEqual(["题号", "考点", "技术深度", "正确性", "表达", "均分"]);

    const tfoot = container.querySelector("tfoot");
    expect(tfoot?.textContent).toContain("2.8"); // 技术深度均值 (4+2+2+3)/4
    expect(tfoot?.textContent).toContain("2.9"); // 总分沿用载荷
  });

  it("薄弱点卡：回链题号锚点指向表格行；全强时给兜底文案", async () => {
    getSessionMock.mockResolvedValue(finishedSession());
    render(<ReportShell sessionId="s9" />);
    await screen.findByRole("img", { name: /综合评分/ });

    const link2 = screen.getByRole("link", { name: "2" });
    expect(link2.getAttribute("href")).toBe("#q-2");
    expect(screen.getAllByText(/点击可回到对应行/)).toHaveLength(3); // 三张薄弱卡（1.1/2/3）

    // 全强兜底
    getSessionMock.mockResolvedValue({
      ...finishedSession(),
      final: { ...FINAL, overall: 4.2, topic_results: RESULTS.slice(0, 1) },
    });
    cleanup();
    render(<ReportShell sessionId="s9" />);
    expect(await screen.findByText(/没有明显短板/)).toBeTruthy();
  });

  it("操作：再来一场带岗位回向导预填；返回列表", async () => {
    getSessionMock.mockResolvedValue(finishedSession());
    render(<ReportShell sessionId="s9" />);
    const again = await screen.findByRole("link", { name: /再来一场/ });
    expect(again.getAttribute("href")).toContain("/?position=");
    expect(decodeURIComponent(again.getAttribute("href") ?? "")).toContain("后端开发工程师");
    expect(screen.getByRole("link", { name: "返回会话列表" }).getAttribute("href")).toBe("/sessions");
  });
});

describe("ReportShell · 状态分支", () => {
  it("未收场：引导继续面试", async () => {
    getSessionMock.mockResolvedValue({
      ...finishedSession(),
      status: "awaiting",
      final: undefined,
      pending_question: { variant: "question", qnum: "4", topic: "网络", text: "…" },
    });
    render(<ReportShell sessionId="s1" />);
    const link = await screen.findByRole("link", { name: "继续面试" });
    expect(link.getAttribute("href")).toBe("/interview/s1");
    expect(screen.queryByRole("img", { name: /综合评分/ })).toBeNull();
  });

  it("加载失败：人话文案 + 重试恢复", async () => {
    getSessionMock
      .mockRejectedValueOnce(new (await import("@/lib/api")).ApiError(404, "会话不存在或已失效"))
      .mockResolvedValueOnce(finishedSession());
    render(<ReportShell sessionId="s404" />);
    expect(await screen.findByText("会话不存在或已失效")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("img", { name: /综合评分/ })).toBeTruthy();
  });
});
