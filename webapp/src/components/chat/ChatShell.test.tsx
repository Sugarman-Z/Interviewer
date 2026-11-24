// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { IssuedQuestion, SessionState } from "@/lib/types";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const getSessionMock = vi.fn();
const submitAnswerMock = vi.fn();
const streamAnswerMock = vi.fn();
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
  submitAnswer: (id: string, body: unknown, signal?: AbortSignal) => submitAnswerMock(id, body, signal),
  streamAnswer: (id: string, body: unknown, opts: unknown) => streamAnswerMock(id, body, opts),
}));

import ChatShell from "./ChatShell";
import type { AnswerResult, StreamEvent } from "@/lib/types";

/** streamAnswer 桩的 opts 形状 */
type StreamOpts = { onEvent: (e: StreamEvent) => void; onActivity?: () => void; signal?: AbortSignal };

/** 桩：完整一场——round → stage → 终局，随后 resolve */
function streamSettlesOnce(result: AnswerResult) {
  streamAnswerMock.mockImplementationOnce(async (_id: string, _body: unknown, opts: StreamOpts) => {
    opts.onEvent({ event: "round", expect: "1" });
    opts.onEvent({ event: "stage", phase: result.kind === "final" ? "reporting" : "issuing" });
    if (result.kind === "question") {
      opts.onEvent({ event: "question", kind: "question", question: result.question });
    } else {
      opts.onEvent({
        event: "final",
        kind: "final",
        overall: result.overall,
        interview_report: result.interview_report,
        topic_results: result.topic_results,
      });
    }
  });
}

afterEach(cleanup);

const Q2: IssuedQuestion = { variant: "question", topic: "Redis 缓存一致性", qnum: "2", text: "缓存一致性怎么保证？" };

function awaitingState(): SessionState {
  return {
    session_id: "s1",
    position: "后端开发工程师",
    status: "awaiting",
    max_questions: 10,
    transcript: [
      { role: "interviewer", variant: "opening", text: "请先做个自我介绍。", at: "2026-11-18T10:00:00Z" },
      { role: "candidate", variant: "answer", text: "五年后端。", at: "2026-11-18T10:01:00Z" },
      {
        role: "interviewer",
        variant: "question",
        topic: "并发与线程池",
        qnum: "1",
        text: "线程池核心参数怎么协同？",
        at: "2026-11-18T10:02:00Z",
      },
      {
        role: "interviewer",
        variant: "followup",
        topic: "扩容时机与代价",
        qnum: "1.1",
        text: "想更早扩容，参数怎么调？",
        at: "2026-11-18T10:03:00Z",
      },
    ],
    pending_question: { variant: "followup", topic: "扩容时机与代价", qnum: "1.1", text: "想更早扩容，参数怎么调？" },
    progress: { answered: 1, total: 10 },
  };
}

beforeEach(() => {
  getSessionMock.mockReset();
  submitAnswerMock.mockReset();
  streamAnswerMock.mockReset();
  pushMock.mockReset();
});

describe("ChatShell · 恢复（spec §5.5）", () => {
  it("挂载即 GET；awaiting 会话重建 transcript 与当前题，顶栏岗位/题号来自后端", async () => {
    getSessionMock.mockResolvedValue(awaitingState());
    render(<ChatShell sessionId="s1" />);

    expect(getSessionMock).toHaveBeenCalledWith("s1", expect.anything());
    expect(await screen.findByText(/Q1 · 并发与线程池/)).toBeTruthy();
    expect(screen.getByText("追问")).toBeTruthy();
    expect(screen.getByText("同考点 · 扩容时机与代价")).toBeTruthy();
    expect(screen.getByText("后端开发工程师")).toBeTruthy();
    // 拍板 a 的机器闸：演示模式标注不回潮（切流后内容来自真后端）
    expect(screen.queryByText(/演示模式/)).toBeNull();
    // pending 是 1.1 → 顶栏显示第 1 题；上限来自 max_questions，不硬编码
    expect(screen.getByText("第 1 题")).toBeTruthy();
    expect(screen.getByText(/共约 10 题/)).toBeTruthy();
    // 计时器与真输入区在场；恢复完成后输入框自动聚焦（ 续面即答）。
    // 计时器每秒走字：并行负载下断言可能跨过 1s 刻度，匹配分钟内任意秒值防 flakes
    expect(screen.getByText(/^00:\d{2}$/)).toBeTruthy();
    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    expect(ta).toBeTruthy();
    // 并行负载下焦点效应可能晚一拍到达，waitFor 防 flakes（同文件计时器断言的同款纪律）
    await waitFor(() => expect(document.activeElement).toBe(ta));
  });

  it("已结束会话：引导去报告页，链接指向 /report/[id]，不渲染输入区", async () => {
    getSessionMock.mockResolvedValue({
      ...awaitingState(),
      status: "finished",
      pending_question: undefined,
      progress: { answered: 10, total: 10 },
    });
    render(<ChatShell sessionId="s9" />);

    const link = await screen.findByRole("link", { name: "查看终场报告" });
    expect(link.getAttribute("href")).toBe("/report/s9");
    expect(screen.getByText("已收场")).toBeTruthy();
    expect(screen.queryByLabelText("回答输入框")).toBeNull();
  });

  it("加载失败原位报错，重试成功后恢复", async () => {
    getSessionMock
      .mockRejectedValueOnce(new Error("network down")) // 非 ApiError → 通用文案
      .mockResolvedValueOnce(awaitingState());
    render(<ChatShell sessionId="s404" />);

    expect(await screen.findByText(/网络似乎不太稳定/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText(/Q1 · 并发与线程池/)).toBeTruthy();
  });

  it("后端 404 的人话文案透传", async () => {
    getSessionMock.mockRejectedValue(new (await import("@/lib/api")).ApiError(404, "会话不存在或已失效"));
    render(<ChatShell sessionId="s404" />);
    expect(await screen.findByText("会话不存在或已失效")).toBeTruthy();
  });

  it("兜底：transcript 末条不含当前待答题时补渲染 pending_question", async () => {
    const state = awaitingState();
    state.transcript = state.transcript.filter((m) => m.variant !== "followup");
    getSessionMock.mockResolvedValue(state);
    render(<ChatShell sessionId="s1" />);
    expect(await screen.findByText("想更早扩容，参数怎么调？")).toBeTruthy();
    expect(screen.getByText("同考点 · 扩容时机与代价")).toBeTruthy();
  });
});

describe("ChatShell · 提交回路（spec §5.3）", () => {
  it("输入回答 Enter 发送：等待气泡出现 → 下一问渲染，输入区恢复", async () => {
    let releaseStream!: () => void;
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          opts.onEvent({ event: "round", expect: "1.1" });
          opts.onEvent({ event: "stage", phase: "evaluating" });
          releaseStream = () => {
            opts.onEvent({ event: "question", kind: "question", question: Q2 });
            resolve();
          };
        }),
    );
    getSessionMock.mockResolvedValue(awaitingState());
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);

    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "把核心线程数调大并改用 SynchronousQueue。" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    // 等待态：气泡文案 + composer 锁定
    expect(await screen.findByText("正在评估你的回答…")).toBeTruthy();
    expect((screen.getByLabelText("回答输入框") as HTMLTextAreaElement).disabled).toBeTruthy();
    expect((screen.getByRole("button", { name: "跳过此题" }) as HTMLButtonElement).disabled).toBeTruthy();

    releaseStream();
    expect(await screen.findByText(/Q2 · Redis 缓存一致性/)).toBeTruthy();
    expect((screen.getByLabelText("回答输入框") as HTMLTextAreaElement).disabled).toBeFalsy();
    // 本地合成的候选人消息在流里
    expect(screen.getByText("把核心线程数调大并改用 SynchronousQueue。")).toBeTruthy();
  });

  it("发送失败：横幅报错 + 重试（先核对进度）入口在场，composer 锁定、草稿保留展示", async () => {
    streamAnswerMock.mockRejectedValueOnce(new Error("boom"));
    getSessionMock.mockResolvedValue(awaitingState());
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);

    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "我的回答" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(await screen.findByText(/网络似乎不太稳定/)).toBeTruthy();
    expect(screen.getByText(/已保留在输入框/)).toBeTruthy();
    // 错误态锁定：重试是唯一出口，不出现可点但哑掉的发送/跳过
    expect((screen.getByLabelText("回答输入框") as HTMLTextAreaElement).disabled).toBeTruthy();
    expect((screen.getByRole("button", { name: "跳过此题" }) as HTMLButtonElement).disabled).toBeTruthy();
    expect((screen.getByLabelText("回答输入框") as HTMLTextAreaElement).value).toBe("我的回答");
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("跳过失败：横幅副文案不提「回答已保留」（skip 无文本）", async () => {
    streamAnswerMock.mockRejectedValueOnce(new Error("boom"));
    getSessionMock.mockResolvedValue(awaitingState());
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);
    fireEvent.click(screen.getByRole("button", { name: "跳过此题" }));
    expect(await screen.findByText(/网络似乎不太稳定/)).toBeTruthy();
    expect(screen.queryByText(/已保留在输入框/)).toBeNull();
  });

  it("跳过后不再合成 system 提示行（§10-7）；「已跳过」气泡标注在场", async () => {
    getSessionMock.mockResolvedValue(awaitingState());
    streamSettlesOnce({ kind: "question", question: Q2 });
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);
    fireEvent.click(screen.getByRole("button", { name: "跳过此题" }));
    expect(await screen.findByText("已跳过此题")).toBeTruthy();
    expect(screen.queryByText("已跳过此题 · 我们换一道")).toBeNull();
    expect(await screen.findByText(/Q2 · Redis 缓存一致性/)).toBeTruthy();
  });

  it("提交撞 410 limbo：restore-error 同款卡片——有返回会话列表出口、无「核对进度」副文案（§10-8）", async () => {
    streamAnswerMock.mockRejectedValueOnce(
      new (await import("@/lib/api")).ApiError(410, "这场面试的记录不完整（引擎中断），无法恢复"),
    );
    getSessionMock.mockResolvedValue(awaitingState());
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);
    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "我的回答" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(await screen.findByText(/记录不完整/)).toBeTruthy();
    const back = screen.getByRole("link", { name: "返回会话列表" });
    expect(back.getAttribute("href")).toBe("/sessions");
    expect(screen.queryByText(/核对进度/)).toBeNull(); // send-error 副文案不出现
  });

  it("纯键盘完成一场面试（T8 验收）：Enter 作答、焦点不丢、键盘可达、收场跳报告", async () => {
    const FINAL = {
      kind: "final" as const,
      overall: 4.2,
      interview_report: "## 总评\n表现不错。",
      topic_results: [],
    };
    getSessionMock.mockResolvedValue(awaitingState());
    streamSettlesOnce({ kind: "question", question: Q2 });
    streamSettlesOnce(FINAL);
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);

    // sr-only h1 与键盘可聚焦的消息流；键盘焦点锚在
    // 播报语义元素 log 上（读屏播报「面试消息流」，方向键滚动最近滚动祖先
    // .tl——追查二拆分后 tabIndex 随语义走），滚动容器不再设 tab stop
    expect(screen.getByRole("heading", { level: 1, name: "后端开发工程师 · 模拟面试" })).toBeTruthy();
    expect(screen.getByRole("log").getAttribute("tabIndex")).toBe("0");
    expect(screen.getByRole("log").parentElement?.getAttribute("tabIndex")).toBeNull();

    // 第一题：键盘作答（Enter 发送），下一题到达后焦点回到输入框
    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    ta.focus();
    fireEvent.change(ta, { target: { value: "核心线程数按 CPU 核数与 IO 等待比例定。" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(await screen.findByText(/Q2 · Redis 缓存一致性/)).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("回答输入框"));

    // 第二题：跳过。button 的 Enter/Space 激活是浏览器原生行为，jsdom 不模拟，
    // 组件侧的可测部分是按钮可聚焦（原生元素默认可 Tab），此处以 click 代表激活。
    const skip = screen.getByRole("button", { name: "跳过此题" }) as HTMLButtonElement;
    skip.focus();
    expect(document.activeElement).toBe(skip); // 确认非 disabled、可进入焦点
    fireEvent.click(skip);

    // 收场：自动跳报告
    expect(await screen.findByText(/教练把终场报告准备好了/)).toBeTruthy();
    expect(pushMock).toHaveBeenCalledWith("/report/s1");
  });
});

describe("ChatShell · 流式呈现（SPEC-streaming §5-T6）", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /** 受控流：emit 逐事件投喂（可中途断言），done 终结 */
  function controlledStream() {
    let emit!: (e: StreamEvent) => void;
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          emit = (e: StreamEvent) => {
            if (e.event === "question" || e.event === "final") {
              opts.onEvent(e);
              resolve();
            } else {
              opts.onEvent(e);
            }
          };
        }),
    );
    return (e: StreamEvent) => emit(e);
  }

  it("phase 接线：stage(issuing) 切「正在组织下一个问题…」（US2-1，经 ChatShell 全链）", async () => {
    getSessionMock.mockResolvedValue(awaitingState());
    const emit = controlledStream();
    render(<ChatShell sessionId="s1" />);
    await screen.findByText(/Q1 · 并发与线程池/);
    fireEvent.click(screen.getByRole("button", { name: "跳过此题" }));
    act(() => {
      emit({ event: "round", expect: "1.1" });
      emit({ event: "stage", phase: "evaluating" });
    });
    expect(await screen.findByText("正在评估你的回答…")).toBeTruthy();
    expect(document.querySelector("[data-stream]")).toBeNull(); // 互斥另一半：无 delta 时流式气泡不在场（T6 评审补）
    act(() => {
      emit({ event: "stage", phase: "issuing" });
    });
    expect(await screen.findByText("正在组织下一个问题…")).toBeTruthy();
    act(() => {
      emit({ event: "question", kind: "question", question: Q2 });
    });
    await act(async () => {});
    expect(await screen.findByText(/Q2 · Redis 缓存一致性/)).toBeTruthy();
  });

  it("streamText 非空 → 流式气泡（打字机），ThinkingBubble 不出现（防双渲染）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(awaitingState());
    const emit = controlledStream();
    render(<ChatShell sessionId="s1" />);
    await act(async () => {}); // fake timers 下 findBy 不前进：微任务冲刷 + 同步断言
    fireEvent.click(screen.getByRole("button", { name: "跳过此题" }));
    act(() => {
      emit({ event: "round", expect: "1.1" });
      emit({ event: "stage", phase: "issuing" });
      emit({ event: "delta", text: "追问：想更早扩容该怎么调参？" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150); // 打字机播到中段
    });
    expect(document.querySelector("[data-stream] p")?.textContent).toMatch(/^追问：/); // 气泡部分题面已可见（data-stream 定位，不耦合 Transcript 结构）
    expect(screen.queryByText("正在组织下一个问题…")).toBeNull(); // ThinkingBubble 退场
    expect(screen.queryByText("正在评估你的回答…")).toBeNull(); // 防双渲染负向
    act(() => {
      emit({ event: "question", kind: "question", question: Q2 });
    });
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700); // 推进播完窗口（14 字 ≈ 0.5s）
    });
    expect(screen.getByText(/Q2 · Redis 缓存一致性/)).toBeTruthy();
  });

  it("真机形态（delta 与终局同拍到达）：落定后输入即时解锁、气泡继续播完、播毕才放行 pending 尾行（T8 人工走查修正）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(awaitingState());
    const emit = controlledStream();
    render(<ChatShell sessionId="s1" />);
    await act(async () => {});
    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "我的长回答" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    const TEXT = "下一题的完整题面文本，真机上与终局事件同一拍到达的样子。"; // 27 码点 → 40 字/s ≈ 0.68s
    act(() => {
      // 同一拍：真机 args 原子落地 → 图收尾 → question，事件间隔 ~0ms
      emit({ event: "round", expect: "1.1" });
      emit({ event: "stage", phase: "issuing" });
      emit({ event: "delta", text: TEXT });
      emit({ event: "question", kind: "question", question: Q2 });
    });
    await act(async () => {});
    // 落定即时：输入解锁（不等播完）
    const ta2 = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    expect(ta2.disabled).toBeFalsy();
    // 播完窗口内：气泡在逐字、pending 尾行被抑制（防双文本源）；流式行与
    // 正式行同构——题面 chip 在场（T8 追查二：终局已到，元数据即知），且
    // 位于播报容器之外、同一滚动流尾位（打字位 = 展示位）
    expect(screen.queryByText("缓存一致性怎么保证？")).toBeNull();
    expect(screen.getByText("Q2 · Redis 缓存一致性")).toBeTruthy();
    const streamRow = document.querySelector("[data-stream]");
    expect(streamRow).toBeTruthy();
    const log = screen.getByRole("log", { name: "面试消息流" });
    expect(streamRow?.closest('[role="log"]')).toBeNull();
    expect(streamRow?.parentElement?.parentElement).toBe(log.parentElement);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(document.querySelector("[data-stream] p")?.textContent?.length ?? 0).toBeGreaterThan(0);
    // 播毕：气泡撤、尾行放行（整题权威行）；换装行静默入场（同位同文，
    // data-nopop 在场——pop 动画只属于首次出现的内容）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(document.querySelector("[data-stream]")).toBeNull();
    const tail = screen.getByText("缓存一致性怎么保证？");
    expect(tail).toBeTruthy();
    expect(tail.closest("[data-nopop]")).toBeTruthy();
  });

  it("无 streamText 的落定（skip 换发零 delta 等）：无播完窗口，尾行即时（负向）", async () => {
    getSessionMock.mockResolvedValue(awaitingState());
    const emit = controlledStream();
    render(<ChatShell sessionId="s1" />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "跳过此题" }));
    act(() => {
      emit({ event: "round", expect: "1.1" });
      emit({ event: "stage", phase: "evaluating" });
      emit({ event: "stage", phase: "issuing" });
      emit({ event: "question", kind: "question", question: Q2 });
    });
    await act(async () => {});
    expect(document.querySelector("[data-stream]")).toBeNull(); // 从未有流式气泡
    const tail = screen.getByText("缓存一致性怎么保证？"); // 尾行即时在场
    expect(tail.closest("[data-nopop]")).toBeNull(); // 非换装：正常 pop 入场
  });

  it("追问轮播完窗口：流式行带追问同构（徽标 + 同考点 chip + 小头像），播毕换装为正式追问行", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(awaitingState());
    const emit = controlledStream();
    render(<ChatShell sessionId="s1" />);
    await act(async () => {});
    const ta = screen.getByLabelText("回答输入框") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "先答限流再谈降级。" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    // 同拍形态（真机）：delta 与终局 followup 同一拍
    const FOLLOW: IssuedQuestion = { variant: "followup", topic: "缩容触发条件", qnum: "1.2", text: "那缩容呢？" };
    act(() => {
      emit({ event: "round", expect: "1.1" });
      emit({ event: "delta", text: "那缩容呢？" });
      emit({ event: "question", kind: "question", question: FOLLOW });
    });
    await act(async () => {});
    // 窗口内：追问徽标 ×2（旧 1.1 行 + 流式行同构），同考点 chip 为新题专属，
    // follow 形态三锚：小头像宽 15、行级 follow class、（缩进/连线经 CSS 同源）
    expect(screen.getAllByText("追问").length).toBe(2);
    expect(screen.getByText("同考点 · 缩容触发条件")).toBeTruthy();
    expect(document.querySelector("[data-stream] svg")?.getAttribute("width")).toBe("15"); // follow 小头像
    expect(document.querySelector("[data-stream]")?.className).toContain("follow"); // 追问缩进/连线的 class 半边
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000); // 5 码点 ≈ 0.13s + 0.15s，早已播完
    });
    expect(document.querySelector("[data-stream]")).toBeNull();
    expect(screen.getByText("那缩容呢？")).toBeTruthy(); // 正式追问行放行
  });
});
