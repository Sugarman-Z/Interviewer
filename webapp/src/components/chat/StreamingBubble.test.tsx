// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import StreamingBubble from "./StreamingBubble";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((q: string) => ({
      matches: reduce && q.includes("prefers-reduced-motion"),
      addEventListener: () => {},
      removeEventListener: () => {},
    })),
  );
}

/** 气泡正文（data-stream 标记定位，不耦合 Transcript 结构——TDD §5-T6
 *  的「data 流式标记」锚）。注：未桩 matchMedia 的用例隐式依赖 jsdom 恒无
 *  matchMedia（组件的 ?. 短路 → 非 reduced），该依赖是确定性的 */
function bubbleText(): string {
  return document.querySelector("[data-stream] p")?.textContent ?? "";
}

describe("StreamingBubble · 方案 A 打字机（tech §6.3）", () => {
  it("逐字严格递增（前 8 tick 每步都前进），≤3s 收敛到全文（短文本 40 字/s 下限档）", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const text = "线程池的核心参数怎么协同？讲讲你的取舍。"; // 21 字
    render(<StreamingBubble text={text} />);
    expect(bubbleText()).toBe(""); // 从零起播
    const lens: number[] = [];
    for (let i = 0; i < 14; i++) {
      act(() => {
        vi.advanceTimersByTime(50);
      });
      lens.push(bubbleText().length);
    }
    for (let i = 1; i < 8; i++) {
      expect(lens[i]).toBeGreaterThan(lens[i - 1]); // 严格递增：逐 tick 停顿的回归在此红
    }
    expect(bubbleText()).toBe(text); // 21 字 ÷ 40 字/s ≈ 0.55s，700ms 必收敛
  });

  it("长文本 80 字/s 上限档：240 字恰 3s 收敛（M9 锚——量级笔误在此红）", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const text = "长".repeat(240);
    render(<StreamingBubble text={text} />);
    act(() => {
      vi.advanceTimersByTime(2_900);
    });
    expect(bubbleText().length).toBeLessThan(240); // 未到点不全显（不对齐 TICK_MS/RATE 精确值）
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(bubbleText()).toBe(text); // 3s 整收敛
  });

  it("超长题面（>240 字）：速率上限拍板——80 字/s 线性，400 字 5s 收敛（T6 评审注记的显式锁）", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const text = "超".repeat(400);
    render(<StreamingBubble text={text} />);
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(bubbleText()).toBe("超".repeat(240)); // 3s 处 = 80 字/s × 3s（≤3s 不变式到此为止）
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(bubbleText()).toBe(text); // 5s 收敛
  });

  it("text 延展（新 delta 到达）从当前可见处继续，不清零重播（累积器复位型实现在此红）", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const first = "第一段题面文本足够长，让中段可见字数远大于每 tick 步长二到三字。"; // 30+ 字
    const { rerender } = render(<StreamingBubble text={first} />);
    act(() => {
      vi.advanceTimersByTime(500); // 40 字/s → ~20 字可见（>> perTick 2）
    });
    const mid = bubbleText();
    expect(mid.length).toBeGreaterThan(10); // 中段确实播了不少
    const extended = first + "后半段追问补充说明继续出现在题面上。";
    rerender(<StreamingBubble text={extended} />);
    expect(bubbleText()).toBe(mid); // 延展瞬间不回退
    act(() => {
      vi.advanceTimersByTime(100);
    });
    const after = bubbleText();
    expect(after.startsWith(mid)).toBe(true); // 从 mid 续播（复位型实现此处回到 2 字 → 红）
    expect(after.length).toBeGreaterThan(mid.length);
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(bubbleText()).toBe(extended);
  });

  it("码点切片：含 emoji/扩展区字符的题面逐帧不出现替换符（U+FFFD）", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const text = "请解释🙂这个符号，以及 𠮷 字的读音。"; // 含代理对
    render(<StreamingBubble text={text} />);
    for (let i = 0; i < 40; i++) {
      act(() => {
        vi.advanceTimersByTime(50);
      });
      expect(bubbleText()).not.toContain("�"); // 每帧切点都在码点边界
    }
    expect(bubbleText()).toBe(text);
  });

  it("reduced-motion：瞬时全显、光标不出现（busy 仍在场——等待未结束）", () => {
    stubReducedMotion(true);
    const text = "prefers-reduced-motion 时不应逐字";
    render(<StreamingBubble text={text} />);
    expect(bubbleText()).toBe(text);
    expect(document.querySelector("[data-stream] p i")).toBeNull(); // 光标不渲染
    expect(document.querySelector('[aria-busy="true"]')).toBeTruthy(); // 等待语义不因动效偏好消失
  });

  it("播报纪律与标记：无 aria-live/role=status；在场恒 aria-busy；光标只在打字中", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const text = "正在流式输出的题面文本内容";
    const { container } = render(<StreamingBubble text={text} />);
    expect(container.querySelector("[aria-live]")).toBeNull(); // 逐字变化不进播报（容器外由 Transcript 保证）
    expect(container.querySelector('[role="status"]')).toBeNull(); // role=status 隐含 polite，同样排除
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy(); // 播完前
    expect(container.querySelector("[data-stream] p i")).toBeTruthy(); // 打字中：光标在场
    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy(); // 播完未落定：busy 仍在（spec 口径=落定才消失，落定即整气泡卸载）
    expect(container.querySelector("[data-stream] p i")).toBeNull(); // 播完：光标撤（不演「还在打字」——断流对账窗口的诚实呈现）
  });

  it("head/follow（播完窗口同构形态，T8 追查二）：chip 在正文前、follow 小头像与正式追问行一致", () => {
    render(<StreamingBubble text="正在逐字的题面" head={<div>Q2 · 缩容触发条件</div>} follow />);
    expect(screen.getByText("Q2 · 缩容触发条件")).toBeTruthy(); // 题面 chip 同构（换装瞬间不新增头部）
    expect(document.querySelector("[data-stream] svg")?.getAttribute("width")).toBe("15"); // follow 小头像
  });
});
