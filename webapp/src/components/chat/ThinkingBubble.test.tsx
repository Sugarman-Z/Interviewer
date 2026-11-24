// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ThinkingBubble from "./ThinkingBubble";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ThinkingBubble · 四级等待文案（spec §5.4）", () => {
  it("0-8s：正在评估；8s：准备下一题；30s：安抚文案", () => {
    vi.useFakeTimers();
    render(<ThinkingBubble />);
    expect(screen.getByText("正在评估你的回答…")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(8_000);
    });
    expect(screen.getByText("正在准备下一个问题…")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(22_000); // 累计 30s
    });
    expect(screen.getByText(/面试官思考较久/)).toBeTruthy();
  });

  it("带 role=status 播报语义", () => {
    render(<ThinkingBubble />);
    expect(screen.getByRole("status")).toBeTruthy();
  });
});

describe("ThinkingBubble · phase 驱动（T6/US2-2 流式态）", () => {
  it("三档 phase 文案（US2-1 词汇表）+ 定时自切禁用——30s/60s 都不切", () => {
    vi.useFakeTimers();
    const { rerender } = render(<ThinkingBubble phase="evaluating" />);
    expect(screen.getByText("正在评估你的回答…")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(screen.getByText("正在评估你的回答…")).toBeTruthy(); // 定时不切（US2-2 负向）

    rerender(<ThinkingBubble phase="issuing" />);
    expect(screen.getByText("正在组织下一个问题…")).toBeTruthy(); // US2-1 原文措辞
    rerender(<ThinkingBubble phase="reporting" />);
    expect(screen.getByText("正在生成终场报告…")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText("正在生成终场报告…")).toBeTruthy(); // 报告段 40-120s 定时不扰
  });
});
