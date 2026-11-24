// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TranscriptMessage } from "@/lib/types";
import Transcript from "./Transcript";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const m = (msg: Omit<TranscriptMessage, "at">): TranscriptMessage => ({ ...msg, at: "2026-11-18T10:00:00Z" });

/** 覆盖全部 role × variant 组合的一条流 */
const FLOW: TranscriptMessage[] = [
  m({ role: "interviewer", variant: "opening", text: "请先做个自我介绍。" }),
  m({ role: "candidate", variant: "answer", text: "五年后端，主写订单中心。" }),
  m({ role: "system", variant: "system", text: "面试继续" }),
  m({
    role: "interviewer",
    variant: "question",
    topic: "Redis 缓存一致性",
    qnum: "2",
    text: "缓存和数据库的一致性怎么保证？",
  }),
  m({ role: "candidate", variant: "skip", text: "（跳过了此题）", topic: "Redis 缓存一致性", qnum: "2" }),
  m({ role: "system", variant: "system", text: "已跳过此题 · 我们换一道" }),
  m({
    role: "interviewer",
    variant: "followup",
    topic: "索引失效场景",
    qnum: "3.1",
    text: "对列做函数计算，索引还走得通吗？",
  }),
  m({ role: "interviewer", variant: "closing", text: "本场问题问完了，感谢你的时间。" }),
];

describe("Transcript · 四变体渲染", () => {
  it("开场/收场有静态标签，主题题带 Q 编号 + 考点 chip", () => {
    render(<Transcript messages={FLOW} />);
    expect(screen.getByText("开场 · 自我介绍")).toBeTruthy();
    expect(screen.getByText("Q2 · Redis 缓存一致性")).toBeTruthy();
    expect(screen.getByText("收场")).toBeTruthy();
  });

  it("追问：徽标「追问」+「同考点 · 主题」绿 chip，与主题题的呈现不同", () => {
    render(<Transcript messages={FLOW} />);
    expect(screen.getByText("追问")).toBeTruthy();
    expect(screen.getByText("同考点 · 索引失效场景")).toBeTruthy();
    // 主题题 chip 不带「同考点」前缀，两者可区分
    expect(screen.queryByText(/同考点 · Redis/)).toBeNull();
  });

  it("候选人回答右气泡、跳过弱化气泡、系统居中条", () => {
    render(<Transcript messages={FLOW} />);
    expect(screen.getByText("五年后端，主写订单中心。")).toBeTruthy();
    expect(screen.getByText("已跳过此题")).toBeTruthy(); // 跳过气泡（系统条文本不同，唯一命中）
    expect(screen.getByText("已跳过此题 · 我们换一道")).toBeTruthy();
    expect(screen.getByText("面试继续")).toBeTruthy();
  });

  it("候选人行带「我」字圆头像（answer/skip 两形态一致），读屏不可见（纯装饰）", () => {
    render(<Transcript messages={FLOW} />);
    const avas = screen.getAllByText("我");
    expect(avas.length).toBe(2); // FLOW 含 1 answer + 1 skip，行行有头像
    avas.forEach((a) => expect(a.getAttribute("aria-hidden")).toBe("true"));
  });

  it("容器带 role=log 语义", () => {
    render(<Transcript messages={FLOW} />);
    expect(screen.getByRole("log", { name: "面试消息流" })).toBeTruthy();
  });

  it("footer 槽位在播报容器（role=log）**之外**、同一滚动流内（T6 评审拍板 + T8 追查二：打字位 = 展示位）", () => {
    render(<Transcript messages={FLOW} footer={<div>THINKING-FOOTER</div>} />);
    const log = screen.getByRole("log", { name: "面试消息流" });
    expect(log.textContent).not.toContain("THINKING-FOOTER"); // 不在播报容器内
    const footer = screen.getByText("THINKING-FOOTER");
    expect(footer.parentElement?.parentElement).toBe(log.parentElement); // .tlFooter 与 .tlLog 同为 .tl 子节点——同一滚动流的尾位
  });
});

describe("Transcript · 自动滚动暂停（T5）", () => {
  /** jsdom 无布局：手工定义滚动几何，模拟上翻/回底 */
  function geom(el: HTMLElement, scrollHeight: number, clientHeight: number) {
    Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
    Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
  }

  /** 滚动容器是 .tl（role=log 的 .tlLog 之内的行不再承载滚动几何） */
  function scrollerOf(): HTMLDivElement {
    return screen.getByRole("log", { name: "面试消息流" }).parentElement as HTMLDivElement;
  }

  it("上翻（距底 >80px）出现「回到底部」，滚回底部后消失", () => {
    render(<Transcript messages={FLOW} />);
    const scroller = scrollerOf();
    geom(scroller, 1000, 400);

    scroller.scrollTop = 100; // 距底 500px
    fireEvent.scroll(scroller);
    expect(screen.getByRole("button", { name: "回到底部 ↓" })).toBeTruthy();

    scroller.scrollTop = 600; // 距底 0
    fireEvent.scroll(scroller);
    expect(screen.queryByRole("button", { name: "回到底部 ↓" })).toBeNull();
  });

  it("点击「回到底部」回到最近底部并隐藏按钮", () => {
    render(<Transcript messages={FLOW} />);
    const scroller = scrollerOf();
    geom(scroller, 1000, 400);
    scroller.scrollTop = 100;
    fireEvent.scroll(scroller);
    fireEvent.click(screen.getByRole("button", { name: "回到底部 ↓" }));
    expect(scroller.scrollTop).toBe(1000);
    expect(screen.queryByRole("button", { name: "回到底部 ↓" })).toBeNull();
  });

  it("流式行长高自动跟底（贴底时）；上翻滚远后不拽回——含吞噬窗口期 pausedRef 未置位的形态（几何判据，ResizeObserver，T8 追查二）", () => {
    const fires: Array<() => void> = [];
    const targets: Array<Element> = [];
    class FakeRO {
      constructor(cb: ResizeObserverCallback) {
        fires.push(() => cb([], null as unknown as ResizeObserver));
      }
      observe(t: Element) {
        targets.push(t);
      }
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", FakeRO as unknown as typeof ResizeObserver);
    render(<Transcript messages={FLOW} footer={<div>THINKING-FOOTER</div>} />);
    // 观察目标是 footer 槽（观察错元素——如 log 本体——的实现在此红）
    expect(targets[0]).toBe(screen.getByText("THINKING-FOOTER").parentElement);
    const scroller = scrollerOf();
    geom(scroller, 1_650, 400);
    scroller.scrollTop = 1_200; // 距底 50px：贴底用户遇打字长高一行的量级
    act(() => fires[0]());
    expect(scroller.scrollTop).toBe(1_650); // 跟底

    scroller.scrollTop = 100; // 上翻读历史（距底 1,150px）
    fireEvent.scroll(scroller);
    act(() => fires[0]());
    expect(scroller.scrollTop).toBe(100); // 滚远不跟

    scroller.scrollTop = 300; // 吞噬窗口期的上翻：scroll 事件被吞、pausedRef
    // 未置位——几何判据仍按实际距底（950px > 阈值）拒绝拽回
    act(() => fires[0]());
    expect(scroller.scrollTop).toBe(300);
  });

  it("noPop 挂载拍锁定：noAnimLast 翻回 false 后换装行不摘标记（直连 prop 的摘锁变异在此红，M10）", () => {
    const { rerender } = render(<Transcript messages={FLOW.slice(0, 6)} noAnimLast={false} />);
    // 收拍换装拍：尾行（FLOW[6] 追问行）以 noAnimLast=true 挂载（满文本静默入场）
    rerender(<Transcript messages={FLOW.slice(0, 7)} noAnimLast />);
    expect(screen.getByText("对列做函数计算，索引还走得通吗？").closest("[data-nopop]")).toBeTruthy();
    // 下一轮开跑：noAnimLast 翻回 false——挂载拍锁定的行不摘标记（直连 prop
    // 实现此刻摘掉 data-nopop、pop 动画从头补播，上一题整行再淡入一次）
    rerender(<Transcript messages={FLOW.slice(0, 7)} noAnimLast={false} />);
    expect(screen.getByText("对列做函数计算，索引还走得通吗？").closest("[data-nopop]")).toBeTruthy();
  });
});
