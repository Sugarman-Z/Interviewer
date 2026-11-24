// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChatHeader from "./ChatHeader";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ChatHeader · 进度与每题计时器", () => {
  it("岗位、题号、进度上限全部来自后端数据", () => {
    render(<ChatHeader position="数据分析师" current={3} max={8} finished={false} qnum="3" />);
    expect(screen.getByText("数据分析师")).toBeTruthy();
    expect(screen.getByText("第 3 题")).toBeTruthy();
    expect(screen.getByText(/共约 8 题/)).toBeTruthy();
  });

  it("计时器走秒（等宽 mm:ss），换题归零", () => {
    vi.useFakeTimers();
    const { rerender } = render(<ChatHeader position="后端" current={1} max={10} finished={false} qnum="1" />);
    expect(screen.getByText("00:00")).toBeTruthy();
    act(() => {
      vi.advanceTimersByTime(65_000);
    });
    expect(screen.getByText("01:05")).toBeTruthy();

    // 换题归零
    rerender(<ChatHeader position="后端" current={2} max={10} finished={false} qnum="2" />);
    expect(screen.getByText("00:00")).toBeTruthy();
  });

  it("已收场：显示已收场，无计时器", () => {
    render(<ChatHeader position="后端" current={10} max={10} finished />);
    expect(screen.getByText("已收场")).toBeTruthy();
    expect(screen.queryByText(/^\d{2}:\d{2}$/)).toBeNull();
  });
});
