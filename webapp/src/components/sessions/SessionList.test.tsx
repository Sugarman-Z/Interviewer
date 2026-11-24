// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionSummary } from "@/lib/types";

const listSessionsMock = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  // 真实 withTimeoutRetry 进测试（ 重试语义要真跑），只桩网络函数
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    listSessions: (signal?: AbortSignal) => listSessionsMock(signal),
  };
});

import SessionList from "./SessionList";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const HOUR = 3_600_000;

function mk(partial: Partial<SessionSummary>): SessionSummary {
  return {
    session_id: "x",
    position: "某岗位",
    started_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    status: "ongoing",
    progress: { answered: 4, total: 10 },
    ...partial,
  };
}

/** 四条种子：进行中 / 已完成×2（分低与高）/ 已中断（追问挂起态） */
function seedList(): SessionSummary[] {
  return [
    mk({ session_id: "m-ong", position: "算法工程师", status: "ongoing", pending_qnum: "5", progress: { answered: 4, total: 10 } }),
    mk({ session_id: "m-fin", position: "后端开发工程师", status: "finished", overall: 2.9, progress: { answered: 10, total: 10 } }),
    mk({ session_id: "m-int", position: "数据分析师", status: "interrupted", pending_qnum: "2.1", progress: { answered: 2, total: 10 } }),
    mk({ session_id: "m-fi2", position: "前端开发工程师", status: "finished", overall: 4.3, progress: { answered: 10, total: 10 } }),
  ];
}

/** 岗位名在行内唯一（状态文案与筛选 chip 重名，一律以岗位定位行） */
function rowOf(pos: string): HTMLElement | null {
  return screen.getByText(pos).closest("article");
}

beforeEach(() => {
  listSessionsMock.mockReset();
});

describe("SessionList · 渲染", () => {
  it("三态行：岗位、状态标签、meta 由载荷派生；头部共 N 场", async () => {
    listSessionsMock.mockResolvedValue(seedList());
    const { container } = render(<SessionList />);

    expect(await screen.findByText("算法工程师")).toBeTruthy();
    expect(screen.getByText("后端开发工程师")).toBeTruthy();
    expect(screen.getByText("数据分析师")).toBeTruthy();
    expect(screen.getByText("前端开发工程师")).toBeTruthy();
    expect(container.querySelectorAll("article")).toHaveLength(4);

    // 状态标签在行内（chip 按钮同名，故按行断言）
    expect(rowOf("算法工程师")?.textContent).toContain("进行中");
    expect(rowOf("数据分析师")?.textContent).toContain("已中断");
    expect(screen.getByText("共 4 场 · 未完成的随时接着练")).toBeTruthy();

    // meta 口径：挂起 → 待答题号（追问 2.1 归属第 2 题，不得 answered+1）；完成 → 题数
    expect(screen.getByText(/第 5 题 \/ 共约 10 题 · 开始于/)).toBeTruthy();
    expect(screen.getByText(/第 2 题 \/ 共约 10 题 · 开始于/)).toBeTruthy();
    expect(screen.getAllByText(/^10 题 · /)).toHaveLength(2);
  });

  it("呼吸点只出现在进行中行（i 元素），其余行没有", async () => {
    listSessionsMock.mockResolvedValue(seedList());
    render(<SessionList />);
    await screen.findByText("算法工程师");

    expect(rowOf("算法工程师")?.querySelector("i")).toBeTruthy();
    expect(rowOf("后端开发工程师")?.querySelector("i")).toBeNull();
    expect(rowOf("数据分析师")?.querySelector("i")).toBeNull();
  });

  it("已完成行显示 5 分制分数砖；挂起行没有", async () => {
    listSessionsMock.mockResolvedValue(seedList());
    render(<SessionList />);
    await screen.findByText("后端开发工程师");

    expect(screen.getByLabelText("综合评分 2.9 / 5")).toBeTruthy();
    expect(screen.getByLabelText("综合评分 4.3 / 5")).toBeTruthy();
    expect(screen.queryAllByLabelText(/综合评分/)).toHaveLength(2);
  });

  it("动作：挂起态（进行中/已中断）→ 继续面试；已完成 → 查看报告", async () => {
    listSessionsMock.mockResolvedValue(seedList());
    render(<SessionList />);
    await screen.findByText("算法工程师");

    expect(rowOf("算法工程师")?.querySelector('a[href="/interview/m-ong"]')?.textContent).toContain("继续面试");
    expect(rowOf("数据分析师")?.querySelector('a[href="/interview/m-int"]')?.textContent).toContain("继续面试");
    expect(rowOf("后端开发工程师")?.querySelector('a[href="/report/m-fin"]')?.textContent).toContain("查看报告");

    // 已完成行没有面试入口，挂起行没有报告入口
    expect(rowOf("后端开发工程师")?.querySelector('a[href^="/interview/"]')).toBeNull();
    expect(rowOf("算法工程师")?.querySelector('a[href^="/report/"]')).toBeNull();
  });
});

describe("SessionList · 筛选", () => {
  it("chips 切换过滤行集，aria-pressed 跟随", async () => {
    listSessionsMock.mockResolvedValue(seedList());
    const { container } = render(<SessionList />);
    await screen.findByText("算法工程师");

    const chip = (label: string) => screen.getByRole("button", { name: label });
    expect(chip("全部").getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(chip("已完成"));
    expect(chip("已完成").getAttribute("aria-pressed")).toBe("true");
    expect(chip("全部").getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelectorAll("article")).toHaveLength(2);
    expect(screen.queryByText("算法工程师")).toBeNull();

    fireEvent.click(chip("进行中"));
    expect(container.querySelectorAll("article")).toHaveLength(1);
    expect(screen.getByText(/第 5 题 \/ 共约 10 题 · 开始于/)).toBeTruthy();

    fireEvent.click(chip("全部"));
    expect(container.querySelectorAll("article")).toHaveLength(4);
  });

  it("筛选到空：轻提示，不触发「还没有面试」空态", async () => {
    listSessionsMock.mockResolvedValue([mk({ session_id: "m-ong", status: "ongoing" })]);
    render(<SessionList />);
    await screen.findByText("某岗位");

    fireEvent.click(screen.getByRole("button", { name: "已完成" }));
    expect(screen.getByText("没有已完成的会话")).toBeTruthy();
    expect(screen.queryByText(/还没有面试/)).toBeNull();
  });
});

describe("SessionList · 空态与加载失败", () => {
  it("全空：还没有面试，来一场 + 开始新面试（头部与空态各一），不渲染筛选", async () => {
    listSessionsMock.mockResolvedValue([]);
    render(<SessionList />);

    expect(await screen.findByText(/还没有面试，来一场/)).toBeTruthy();
    const cta = screen.getAllByRole("link", { name: /开始新面试/ });
    expect(cta).toHaveLength(2);
    expect(cta.every((a) => a.getAttribute("href") === "/")).toBe(true);
    expect(screen.queryByRole("button", { name: "全部" })).toBeNull();
  });

  it("失败：人话报错 + 重试恢复（自动重试消耗两次后仍败才给错误卡）", async () => {
    const ApiErrorCtor = (await import("@/lib/api")).ApiError;
    listSessionsMock
      .mockRejectedValueOnce(new ApiErrorCtor(503, "服务暂时不可用"))
      .mockRejectedValueOnce(new ApiErrorCtor(503, "服务暂时不可用"))
      .mockResolvedValueOnce(seedList());
    render(<SessionList />);

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("服务暂时不可用")).toBeTruthy();
    expect(listSessionsMock).toHaveBeenCalledTimes(2); // §10-6：自动重试了一次
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getByText("共 4 场 · 未完成的随时接着练")).toBeTruthy());
  });

  it("§10-6 最小韧性：首次失败自动重试一次即恢复，不出现错误卡", async () => {
    listSessionsMock.mockRejectedValueOnce(new Error("network down")).mockResolvedValueOnce(seedList());
    render(<SessionList />);

    expect(await screen.findByText("共 4 场 · 未完成的随时接着练")).toBeTruthy();
    expect(listSessionsMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("§10-6 超时半边：30s 挂起即 abort 并带新 signal 重试，两次超时才落错误卡", async () => {
    vi.useFakeTimers();
    listSessionsMock.mockImplementation(
      (_signal: AbortSignal) =>
        new Promise((_res, rej) => {
          _signal.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
        }),
    );
    render(<SessionList />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(listSessionsMock).toHaveBeenCalledTimes(2); // 第一次超时 → 换新 signal 立即重试
    expect(screen.queryByRole("alert")).toBeNull(); // 仍在加载，不落错误卡
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(screen.getByRole("alert")).toBeTruthy(); // 两次超时 → 错误卡（AbortError 走通用文案）
    expect(screen.getByText("会话列表加载失败，请重试")).toBeTruthy();
  });

  it("加载中文案（h1 先行渲染）", () => {
    listSessionsMock.mockReturnValue(new Promise(() => {}));
    render(<SessionList />);
    expect(screen.getByText("正在取回你的面试记录…")).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1, name: "历史会话" })).toBeTruthy();
  });
});
