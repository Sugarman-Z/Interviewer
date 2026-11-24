// @vitest-environment jsdom
import { StrictMode } from "react";
import { act, renderHook } from "@testing-library/react";
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

import { useInterview } from "./useInterview";
import { advance as storeAdvance, createSession as storeCreate, getSession as storeGet, toState } from "@/lib/mock/store";
import type { AnswerResult, StreamEvent, TranscriptMessage } from "@/lib/types";

beforeEach(() => {
  pushMock.mockReset();
  getSessionMock.mockReset();
  submitAnswerMock.mockReset();
  streamAnswerMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

/** streamAnswer 桩的 opts 形状（实现侧只依赖这几键） */
type StreamOpts = { onEvent: (e: StreamEvent) => void; onActivity?: () => void; signal?: AbortSignal };
/** 断言用：第三参携带 onEvent 回调 */
const WITH_ON_EVENT = expect.objectContaining({ onEvent: expect.any(Function) });

/** 桩：完整一场——round → stage → 终局，随后 resolve（send 的正常路径） */
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

const Q1: IssuedQuestion = { variant: "question", topic: "并发与线程池", qnum: "1", text: "线程池参数怎么协同？" };
const Q2: IssuedQuestion = { variant: "question", topic: "Redis 缓存一致性", qnum: "2", text: "缓存一致性怎么保证？" };
const F11: IssuedQuestion = { variant: "followup", topic: "扩容时机", qnum: "1.1", text: "想更早扩容怎么调？" };

function state(pending: IssuedQuestion, answered: number, extra: SessionState["transcript"] = []): SessionState {
  return {
    session_id: "s1",
    position: "后端开发工程师",
    status: "awaiting",
    max_questions: 10,
    transcript: [
      { role: "interviewer", variant: "opening", text: "请自我介绍。", at: "t0" },
      { role: "candidate", variant: "answer", text: "五年后端。", at: "t1" },
      ...extra,
      { role: "interviewer", variant: pending.variant, topic: pending.topic, qnum: pending.qnum, text: pending.text, at: "t2" },
    ],
    pending_question: pending,
    progress: { answered, total: 10 },
  };
}

const FINAL = {
  kind: "final" as const,
  overall: 3.7,
  interview_report: "## 表现亮点\n…",
  topic_results: [],
};

describe("useInterview · 恢复", () => {
  it("挂载 GET 成功 → await-input，messages 含当前题兜底", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.messages.at(-1)).toMatchObject({ role: "interviewer", qnum: "1" });
  });

  it("transcript 末条不含当前题时补渲染 pending", async () => {
    const s = state(Q1, 0);
    s.transcript = s.transcript.slice(0, 2); // 服务端只下发已答部分
    getSessionMock.mockResolvedValue(s);
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    expect(result.current.messages.at(-1)).toMatchObject({ role: "interviewer", qnum: "1" });
    //：合成行形状全等锁死——不得回退出伪造 at（空串兜底已删），
    // toEqual 对多余的已定义键失败
    expect(result.current.messages.at(-1)).toEqual({
      role: "interviewer",
      variant: "question",
      text: "线程池参数怎么协同？",
      topic: "并发与线程池",
      qnum: "1",
    });
  });

  it("GET 失败 → restore-error；retryRestore 后恢复", async () => {
    getSessionMock
      .mockRejectedValueOnce(new (await import("@/lib/api")).ApiError(404, "会话不存在或已失效"))
      .mockResolvedValueOnce(state(Q1, 0));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    expect(result.current.stage).toMatchObject({ kind: "restore-error", message: "会话不存在或已失效" });
    await act(async () => {
      result.current.retryRestore();
    });
    expect(result.current.stage.kind).toBe("await-input");
  });

  it("恢复进已收场会话 → finished（不自动跳转，由页面引导）", async () => {
    getSessionMock.mockResolvedValue({ ...state(Q1, 10), status: "finished", pending_question: undefined });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    expect(result.current.stage.kind).toBe("finished");
    expect(pushMock).not.toHaveBeenCalled();
  });
});

describe("useInterview · 提交与推进", () => {
  it("提交回答：thinking → 下一问；本地合成候选人消息 + 题面，主答题数 +1，草稿清空", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamSettlesOnce({ kind: "question", question: Q2 });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("核心线程数、队列、拒绝策略协同工作。"));
    await act(async () => {
      result.current.submit();
    });
    expect(streamAnswerMock).toHaveBeenCalledWith(
      "s1",
      { type: "answer", text: expect.stringContaining("核心线程数"), expect_qnum: "1" },
      WITH_ON_EVENT,
    );
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.draft).toBe("");
    const msgs = result.current.messages;
    expect(msgs.at(-2)).toMatchObject({ role: "candidate", variant: "answer", qnum: "1" });
    expect(msgs.at(-1)).toMatchObject({ role: "interviewer", qnum: "2", topic: "Redis 缓存一致性" });
    expect(result.current.session?.progress.answered).toBe(1);
    expect(result.current.session?.pending_question?.qnum).toBe("2");
  });

  it("答追问：answered 不 +1（追问不计入主答题数）", async () => {
    getSessionMock.mockResolvedValue(state(F11, 1));
    streamSettlesOnce({ kind: "question", question: Q2 });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("把核心线程数调大，代价是内存占用。"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.session?.progress.answered).toBe(1);
    expect(result.current.session?.pending_question?.qnum).toBe("2");
  });

  it("跳过：合成跳过气泡 + 下一问（§10-7：不再合成 system 提示行）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamSettlesOnce({ kind: "question", question: Q2 });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    await act(async () => {
      result.current.skip();
    });
    expect(streamAnswerMock).toHaveBeenCalledWith("s1", { type: "skip", expect_qnum: "1" }, WITH_ON_EVENT);
    const msgs = result.current.messages;
    expect(msgs.at(-2)).toMatchObject({ role: "candidate", variant: "skip", qnum: "1" });
    expect(msgs.at(-1)).toMatchObject({ role: "interviewer", qnum: "2" });
    expect(msgs.some((m) => m.role === "system")).toBe(false);
  });

  it("等待期间不可重复提交（thinking 中 submit/skip 均为 no-op）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    let emit!: (e: StreamEvent) => void;
    let release!: () => void;
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          emit = opts.onEvent;
          release = () => {
            emit({ event: "question", kind: "question", question: Q2 });
            resolve();
          };
        }),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("第一遍回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("thinking");
    act(() => result.current.setDraft("第二遍"));
    await act(async () => {
      result.current.submit();
    });
    await act(async () => {
      result.current.skip();
    });
    expect(streamAnswerMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      emit({ event: "round", expect: "1" });
      release();
    });
    expect(result.current.stage.kind).toBe("await-input");
  });

  it("StrictMode 双挂载后成功回调不被丢弃（dev 卡思考态须刷新的回归）", async () => {
    // dev 下 App Router 默认 StrictMode：挂载→模拟卸载（cleanup 置 aliveRef=false）
    // →重挂载。若守卫不复位，settleResult 开头即 return，POST 成功但 UI 永停 thinking。
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamSettlesOnce({ kind: "question", question: Q2 });
    const { result } = renderHook(() => useInterview("s1"), { wrapper: StrictMode });
    await act(async () => {});
    expect(result.current.stage.kind).toBe("await-input");
    act(() => result.current.setDraft("核心线程数、队列、拒绝策略协同工作。"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.session?.pending_question?.qnum).toBe("2");
    expect(result.current.messages.at(-1)).toMatchObject({ role: "interviewer", qnum: "2" });
  });

  it("空草稿不提交", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("   "));
    await act(async () => {
      result.current.submit();
    });
    expect(streamAnswerMock).not.toHaveBeenCalled();
    expect(result.current.stage.kind).toBe("await-input");
  });

  it("自然收场（final 事件）：finished + 跳 /report/[id]", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 9));
    streamSettlesOnce(FINAL);
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("最后一答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("finished");
    expect(result.current.session?.status).toBe("finished");
    expect(result.current.session?.pending_question).toBeUndefined();
    expect(result.current.session?.final?.overall).toBe(3.7);
    // progress 沿用服务端口径，不虚构答满（熔断提前收场 answered < total）
    expect(result.current.session?.progress.answered).toBe(9);
    expect(pushMock).toHaveBeenCalledWith("/report/s1");
  });
});

describe("useInterview · 失败与重试纪律（spec §6）", () => {
  async function toSendError() {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValueOnce(new Error("network down"));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage).toMatchObject({ kind: "send-error" });
    // 保留已输入内容
    expect(result.current.draft).toBe("我的回答内容");
    return result;
  }

  it("发送失败 → send-error，草稿还原", async () => {
    await toSendError();
  });

  it("重试·已推进：GET 发现题号变了 → 不重发，采用服务端状态", async () => {
    const result = await toSendError();
    const fresh = state(Q2, 1, [
      { role: "candidate", variant: "answer", text: "我的回答内容", topic: "并发与线程池", qnum: "1", at: "t15" },
    ]);
    getSessionMock.mockResolvedValue(fresh);
    await act(async () => {
      result.current.retry();
    });
    expect(submitAnswerMock).not.toHaveBeenCalled(); // 首发走流式：legacy 零调用
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.session?.pending_question?.qnum).toBe("2");
    expect(result.current.messages.at(-1)).toMatchObject({ role: "interviewer", qnum: "2" });
    expect(result.current.draft).toBe(""); // 已确认送达，旧回答不残留（防误发到下一题）
  });

  it("重试·未推进：GET 题号未变 → 重发原请求并落地结果", async () => {
    const result = await toSendError();
    getSessionMock.mockResolvedValue(state(Q1, 0)); // 服务端确实没收到
    submitAnswerMock.mockResolvedValueOnce({ kind: "question", question: Q2 });
    await act(async () => {
      result.current.retry();
    });
    expect(submitAnswerMock).toHaveBeenCalledTimes(1); // 唯一一次 legacy 调用 = 重发腿
    expect(submitAnswerMock).toHaveBeenLastCalledWith("s1", { type: "answer", text: "我的回答内容", expect_qnum: "1" }, expect.anything());
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.session?.pending_question?.qnum).toBe("2");
    expect(result.current.draft).toBe(""); // 重发成功后草稿清空
  });

  it("重试·发现已收场：不重发，直接跳报告", async () => {
    const result = await toSendError();
    getSessionMock.mockResolvedValue({ ...state(Q1, 10), status: "finished", pending_question: undefined, final: { overall: 4, interview_report: "…", topic_results: [] } });
    await act(async () => {
      result.current.retry();
    });
    expect(submitAnswerMock).not.toHaveBeenCalled();
    expect(result.current.stage.kind).toBe("finished");
    expect(pushMock).toHaveBeenCalledWith("/report/s1");
  });

  it("重发腿保留 180s 整轮超时（SUBMIT_TIMEOUT_MS 归 retry 专用，tech §6.2）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValueOnce(new Error("network down"));
    submitAnswerMock.mockImplementation(
      (_id: string, _body: unknown, signal?: AbortSignal) =>
        new Promise((_res, rej) => {
          signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
        }),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("会超时的回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage).toMatchObject({ kind: "send-error" });
    getSessionMock.mockResolvedValue(state(Q1, 0)); // 对账未推进 → 重发
    await act(async () => {
      result.current.retry();
    });
    expect(result.current.stage.kind).toBe("thinking");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(result.current.stage.kind).toBe("thinking"); // 收场轮 40-120s，60s 必须还在等
    await act(async () => {
      await vi.advanceTimersByTimeAsync(119_999);
    }); // t=179,999：差 1ms 到点
    expect(result.current.stage.kind).toBe("thinking");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    }); // t=180,000：到点
    expect(result.current.stage).toMatchObject({ kind: "send-error", message: expect.stringContaining("超时") });
  });
});

describe("useInterview · expect_qnum 附带（§10-4）", () => {
  const OPENING: IssuedQuestion = { variant: "opening", text: "请先做个自我介绍。" };

  it("opening 轮写死字面量 \"opening\"（qnum undefined 直填会丢键）", async () => {
    getSessionMock.mockResolvedValue(state(OPENING, 0));
    streamSettlesOnce({ kind: "question", question: Q1 });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("自我介绍：后端工程师。"));
    await act(async () => {
      result.current.submit();
    });
    expect(streamAnswerMock).toHaveBeenCalledWith(
      "s1",
      { type: "answer", text: "自我介绍：后端工程师。", expect_qnum: "opening" },
      WITH_ON_EVENT,
    );
  });

  it("追问轮带 \"N.k\" 槽位号；skip 同样附带", async () => {
    getSessionMock.mockResolvedValue(state(F11, 1));
    streamSettlesOnce({ kind: "question", question: Q2 });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    await act(async () => {
      result.current.skip();
    });
    expect(streamAnswerMock).toHaveBeenCalledWith(
      "s1",
      { type: "skip", expect_qnum: "1.1" },
      WITH_ON_EVENT,
    );
  });
});

describe("useInterview · skip 后 live 与刷新视图一致（§10-7）", () => {
  it("对同一 Mock 剧本：live 合成行 == 刷新 transcript 去掉 system 展示行", async () => {
    const ms = storeCreate({ position: "后端开发" });
    storeAdvance(ms.session_id, { type: "answer", text: "自我介绍：后端工程师，五年经验。" });
    const pre = toState(storeGet(ms.session_id)!);
    const r = storeAdvance(ms.session_id, { type: "skip" });
    const refreshed = toState(storeGet(ms.session_id)!);

    getSessionMock.mockResolvedValue(pre);
    streamSettlesOnce(r);
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    await act(async () => {
      result.current.skip();
    });

    // 剥除传输层生成值 at（ ② 同口径），显式挑选其余字段
    const stripAt = (rows: TranscriptMessage[]) =>
      rows.map(({ role, variant, text, topic, qnum }) => ({ role, variant, text, topic, qnum }));
    expect(stripAt(result.current.messages)).toEqual(
      stripAt(refreshed.transcript.filter((m) => m.role !== "system")),
    );
  });
});

describe("useInterview · GET 退避（§10-6：409/超时不落终态）", () => {
  it("restore GET 连续 409：15s/30s 两档退避，期间停留加载态，回合结束后恢复", async () => {
    vi.useFakeTimers();
    const inflight = new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试");
    getSessionMock
      .mockRejectedValueOnce(inflight)
      .mockRejectedValueOnce(inflight)
      .mockResolvedValueOnce(state(Q1, 0))
      .mockRejectedValue(inflight); // 不会被消费
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    expect(result.current.stage.kind).toBe("restore");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    }); // 第一档 15s
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    expect(result.current.stage.kind).toBe("restore"); // 不落终态

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    }); // 只到 t=30：第二档是 30s，尚未到点
    expect(getSessionMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    }); // t=45：第二档到点，GET 恢复
    expect(getSessionMock).toHaveBeenCalledTimes(3);
    expect(result.current.stage.kind).toBe("await-input");
  });

  it("GET 自身超时（AbortError）同样退避重试", async () => {
    vi.useFakeTimers();
    getSessionMock
      .mockImplementationOnce(
        (_id: string, signal?: AbortSignal) =>
          new Promise((_res, rej) => {
            signal?.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
          }),
      )
      .mockResolvedValueOnce(state(Q1, 0));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    }); // GET_TIMEOUT_MS 到点
    expect(getSessionMock).toHaveBeenCalledTimes(1); // 先等退避窗
    expect(result.current.stage.kind).toBe("restore"); // 不落 restore-error
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    expect(result.current.stage.kind).toBe("await-input");
  });

  it("退避累计 ≥180s 仍 409 才落 restore-error（15+30×6=195s，共 8 次尝试）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockRejectedValue(
      new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试"),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000 + 30_000 * 6);
    });
    expect(getSessionMock).toHaveBeenCalledTimes(8);
    expect(result.current.stage.kind).toBe("restore-error");
  });

  it("非退避信号（如 404）不退避，直接落 restore-error", async () => {
    getSessionMock.mockRejectedValue(new (await import("@/lib/api")).ApiError(404, "会话不存在或已失效"));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    expect(result.current.stage.kind).toBe("restore-error");
    expect(getSessionMock).toHaveBeenCalledTimes(1);
  });

  it("重试内对账 GET 撞 409：退避期间保持 thinking，恢复后采用服务端状态不重发", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValueOnce(new Error("network down"));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("send-error");

    // 原 POST 其实已到服务端、回合进行中：对账 GET 409 → 退避 → 恢复
    getSessionMock.mockReset();
    getSessionMock
      .mockRejectedValueOnce(new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试"))
      .mockResolvedValue(state(Q2, 1));
    await act(async () => {
      result.current.retry();
    });
    expect(result.current.stage.kind).toBe("thinking");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(result.current.stage.kind).toBe("await-input");
    expect(submitAnswerMock).not.toHaveBeenCalled(); // 退避期间从未重发（首发是流式）
    expect(result.current.session?.pending_question?.qnum).toBe("2");
  });

  it("重试内对账 GET 持续 409：30s 第二档 + 累计 ≥180s 落回 send-error（全程不重发）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValueOnce(new Error("network down"));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("send-error");

    getSessionMock.mockReset();
    getSessionMock.mockRejectedValue(
      new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试"),
    );
    await act(async () => {
      result.current.retry();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    }); // 第一档到点，重试已发出
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    }); // t=30：retry 路径第二档是 30s，未到点
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000 * 5 + 15_000);
    }); // 至 t=195：cap 耗尽
    expect(result.current.stage).toMatchObject({
      kind: "send-error",
      message: "上一条回答还在处理中，请稍候再试",
    });
    expect(submitAnswerMock).not.toHaveBeenCalled(); // cap 耗尽也从未重发（首发是流式）
  });

  it("退避等待期内卸载：时钟再走也不发出后续 GET（restore 侧守卫）", async () => {
    vi.useFakeTimers();
    const inflight = new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试");
    getSessionMock.mockRejectedValue(inflight);
    const { unmount } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    }); // 一次退避重试已发出
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    act(() => {
      unmount();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000);
    });
    expect(getSessionMock).toHaveBeenCalledTimes(2);
  });

  it("重试退避等待期内卸载：时钟再走也不发出后续对账 GET（retry 侧守卫）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValueOnce(new Error("network down"));
    const { result, unmount } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("send-error");
    getSessionMock.mockReset();
    getSessionMock.mockRejectedValue(
      new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试"),
    );
    await act(async () => {
      result.current.retry();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    act(() => {
      unmount();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(45_000);
    });
    expect(getSessionMock).toHaveBeenCalledTimes(2);
  });
});

describe("useInterview · 410 limbo 特判（§10-8）", () => {
  it("流前撞 410 → restore-error 卡片态（非 send-error 死循环）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValue(
      new (await import("@/lib/api")).ApiError(410, "这场面试的记录不完整（引擎中断），无法恢复"),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage).toMatchObject({
      kind: "restore-error",
      message: "这场面试的记录不完整（引擎中断），无法恢复",
    });
  });
});

describe("useInterview · 流式状态机（SPEC-streaming §5-T5）", () => {
  /** 手动步进桩：emit 逐事件投喂（每步独立 act 可观察中间态），release 收尾 */
  function scriptedStream() {
    let emit!: (e: StreamEvent) => void;
    let release!: () => void;
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          emit = opts.onEvent;
          release = () => {
            opts.onEvent({ event: "question", kind: "question", question: Q2 });
            resolve();
          };
        }),
    );
    return { emit: (e: StreamEvent) => emit(e), release: () => release() };
  }

  it("delta 逐字累积 + phase 切换 + 终局对拍（与 legacy POST 路径同形）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    const script = scriptedStream();
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("逐字逐句的回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage).toMatchObject({ kind: "thinking" });
    expect("streamText" in result.current.stage).toBe(false); // delta 前无键
    await act(async () => {
      script.emit({ event: "round", expect: "1" });
    });
    await act(async () => {
      script.emit({ event: "stage", phase: "evaluating" });
    });
    expect(result.current.stage).toMatchObject({ kind: "thinking", phase: "evaluating" });
    await act(async () => {
      script.emit({ event: "delta", text: "线程池" });
    });
    expect(result.current.stage).toMatchObject({ kind: "thinking", streamText: "线程池" });
    await act(async () => {
      script.emit({ event: "delta", text: "参数" });
    });
    await act(async () => {
      script.emit({ event: "stage", phase: "issuing" }); // phase 切换不清 streamText
    });
    expect(result.current.stage).toMatchObject({
      kind: "thinking",
      phase: "issuing",
      streamText: "线程池参数",
    });
    await act(async () => {
      script.emit({ event: "delta", text: "怎么协同" });
      script.release();
    });
    // 终局对拍：applyResult 零改动 → 与 POST 路径产物同形
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.draft).toBe("");
    expect(result.current.messages.at(-2)).toMatchObject({ role: "candidate", variant: "answer", qnum: "1" });
    expect(result.current.messages.at(-1)).toMatchObject({ role: "interviewer", qnum: "2", topic: "Redis 缓存一致性" });
    expect(result.current.session?.progress.answered).toBe(1);
    expect(result.current.session?.pending_question?.qnum).toBe("2");
  });

  it("播完窗口收拍给 playOutJustEnded（换装行静默入场信号），下一轮 send 复位（T8 追查二）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    const script = scriptedStream();
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("窗口收拍信号"));
    await act(async () => {
      result.current.submit();
    });
    await act(async () => {
      script.emit({ event: "round", expect: "1" });
      script.emit({ event: "delta", text: "题面" });
      script.release(); // 终局与 delta 同拍（真机形态）
    });
    expect(result.current.playOutText).toBe("题面"); // 窗口开启
    expect(result.current.playOutJustEnded).toBe(false); // 未收拍
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300); // 2 码点 ≈ 50ms + 150ms 余量
    });
    expect(result.current.playOutText).toBeNull(); // 收拍：窗口撤下
    expect(result.current.playOutJustEnded).toBe(true); // 换装信号
    scriptedStream(); // 第二轮的流式桩（Once 语义：一桩一回合）
    act(() => result.current.setDraft("下一轮回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.playOutJustEnded).toBe(false); // 新一轮开跑即复位
  });

  it("round 后断流（静默收尾形态）→ GET 对账退避，恒不重发（US3-1/M4 锚）", async () => {
    vi.useFakeTimers();
    getSessionMock
      .mockResolvedValueOnce(state(Q1, 0)) // restore
      .mockRejectedValueOnce(new (await import("@/lib/api")).ApiError(409, "上一条回答还在处理中，请稍候再试")) // 对账第 1 发：回合在途
      .mockResolvedValue(state(Q2, 1)); // 对账第 2 发：已落定
    streamAnswerMock.mockImplementationOnce(async (_id: string, _body: unknown, opts: StreamOpts) => {
      opts.onEvent({ event: "round", expect: "1" });
      // 终局未见即收尾：连接被掐的服务端可见形态
    });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("thinking"); // 对账中保持 thinking
    expect(getSessionMock).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.session?.pending_question?.qnum).toBe("2");
    expect(streamAnswerMock).toHaveBeenCalledTimes(1); // 断流不重发流式
    expect(submitAnswerMock).not.toHaveBeenCalled(); // 更不走 legacy（M4）
  });

  it("round 后断流（rejection 形态，TypeError）→ 同走对账（T4 评审补）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockImplementationOnce(async (_id: string, _body: unknown, opts: StreamOpts) => {
      opts.onEvent({ event: "round", expect: "1" });
      throw new TypeError("network changed");
    });
    getSessionMock.mockResolvedValue(state(Q2, 1));
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("await-input"); // 对账 200 即采用
    expect(result.current.session?.pending_question?.qnum).toBe("2");
    expect(submitAnswerMock).not.toHaveBeenCalled();
  });

  it("error 事件（ApiError 500）→ send-error 文案透传，不走对账（回合未落定）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockImplementationOnce(async (_id: string, _body: unknown, opts: StreamOpts) => {
      opts.onEvent({ event: "round", expect: "1" });
      throw new (await import("@/lib/api")).ApiError(500, "面试官暂时掉线了，请稍后刷新查看本场状态");
    });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage).toMatchObject({
      kind: "send-error",
      message: "面试官暂时掉线了，请稍后刷新查看本场状态",
    });
    expect(result.current.draft).toBe("我的回答内容"); // 重发素材保留
    getSessionMock.mockReset();
    await act(async () => {});
    expect(getSessionMock).not.toHaveBeenCalled(); // error 事件 ≠ 断流：不进对账
  });

  it("idle watchdog：44,999ms 无事件仍在等，45,000ms abort → send-error 超时文案（M5 锚）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          opts.signal?.addEventListener("abort", () => resolve()); // 复刻静默中止
        }),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("会超时的回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("thinking");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(44_999);
    });
    expect(result.current.stage.kind).toBe("thinking"); // M5 变异（45s→4.5s）在此已红
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.stage).toMatchObject({
      kind: "send-error",
      message: expect.stringContaining("超时"),
    });
    expect(result.current.draft).toBe("会超时的回答");
  });

  it("600s 整轮护栏：字节活着（ping 喂 onActivity）但回合永不收尾 → 到点 abort 走对账", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          opts.onEvent({ event: "round", expect: "1" });
          // 报告段真实形态：零事件、只有 15s ping（onActivity 字节级活性）
          const iv = setInterval(() => opts.onActivity?.(), 15_000);
          opts.signal?.addEventListener("abort", () => {
            clearInterval(iv);
            resolve();
          });
        }),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("护栏测试回答"));
    await act(async () => {
      result.current.submit();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(599_000);
    });
    expect(result.current.stage.kind).toBe("thinking"); // ping 喂着 watchdog；guard 未到
    expect(result.current.stage).not.toHaveProperty("streamText"); // 全程零 delta
    getSessionMock.mockReset();
    getSessionMock.mockResolvedValue(state(Q2, 1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000); // t=600s：护栏到点 → 对账
    });
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.session?.pending_question?.qnum).toBe("2");
    expect(submitAnswerMock).not.toHaveBeenCalled();
  });

  it("ping 健在无事件：45s 不咬（字节级判据，事件级实现在此红）；停 ping 45s 才咬（T5 评审 important 修正的锚）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    let stopPings!: () => void;
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>((resolve) => {
          const iv = setInterval(() => opts.onActivity?.(), 15_000); // 服务端 ping
          stopPings = () => {
            clearInterval(iv);
          };
          opts.signal?.addEventListener("abort", () => {
            clearInterval(iv);
            resolve();
          });
        }),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("报告段只有 ping 的回答"));
    await act(async () => {
      result.current.submit();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000); // 8 个 ping 已过：远超旧事件级判据的 45s
    });
    expect(result.current.stage.kind).toBe("thinking"); // 字节活着，不咬
    act(() => {
      stopPings(); // 真静默：连接死
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(44_999);
    });
    expect(result.current.stage.kind).toBe("thinking");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.stage).toMatchObject({
      kind: "send-error",
      message: expect.stringContaining("超时"),
    });
    expect(result.current.draft).toBe("报告段只有 ping 的回答");
  });

  it("round 后断流且对账发现已收场：直接跳报告（评审补：finished 分支零覆盖）", async () => {
    getSessionMock
      .mockResolvedValueOnce(state(Q1, 0)) // restore
      .mockResolvedValue({
        ...state(Q1, 10),
        status: "finished",
        pending_question: undefined,
        final: { overall: 4, interview_report: "## 总评", topic_results: [] },
      });
    streamAnswerMock.mockImplementationOnce(async (_id: string, _body: unknown, opts: StreamOpts) => {
      opts.onEvent({ event: "round", expect: "1" });
    });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("最后一答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("finished");
    expect(result.current.session?.final?.overall).toBe(4);
    expect(pushMock).toHaveBeenCalledWith("/report/s1");
    expect(submitAnswerMock).not.toHaveBeenCalled();
  });

  it("对账发现回合未落定（qnum 未推进）：采用服务端状态但还原草稿（评审 nit 采纳）", async () => {
    getSessionMock
      .mockResolvedValueOnce(state(Q1, 0)) // restore
      .mockResolvedValue(state(Q1, 0)); // 对账：引擎失败，题号未动
    streamAnswerMock.mockImplementationOnce(async (_id: string, _body: unknown, opts: StreamOpts) => {
      opts.onEvent({ event: "round", expect: "1" });
    });
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("引擎会炸的回答"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage.kind).toBe("await-input"); // 采用服务端状态
    expect(result.current.session?.pending_question?.qnum).toBe("1");
    expect(result.current.draft).toBe("引擎会炸的回答"); // 草稿还原，答案不蒸发
    expect(submitAnswerMock).not.toHaveBeenCalled();
  });

  it("流式不可用注入（M3 形态二）：503 流前 → send-error → retry 经 legacy 重发成功（对拍）", async () => {
    getSessionMock.mockResolvedValue(state(Q1, 0));
    streamAnswerMock.mockRejectedValueOnce(
      new (await import("@/lib/api")).ApiError(503, "流式输出暂不可用，本场将使用整轮等待模式"),
    );
    const { result } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("我的回答内容"));
    await act(async () => {
      result.current.submit();
    });
    expect(result.current.stage).toMatchObject({
      kind: "send-error",
      message: "流式输出暂不可用，本场将使用整轮等待模式",
    });
    // 重试：GET 未推进 → legacy POST /answers 重发
    getSessionMock.mockResolvedValue(state(Q1, 0));
    submitAnswerMock.mockResolvedValueOnce({ kind: "question", question: Q2 });
    await act(async () => {
      result.current.retry();
    });
    expect(submitAnswerMock).toHaveBeenCalledTimes(1);
    expect(submitAnswerMock).toHaveBeenLastCalledWith(
      "s1",
      { type: "answer", text: "我的回答内容", expect_qnum: "1" },
      expect.anything(),
    );
    // 对拍：重发产物与流式/POST 正常路径同形
    expect(result.current.stage.kind).toBe("await-input");
    expect(result.current.messages.at(-2)).toMatchObject({ role: "candidate", variant: "answer", qnum: "1" });
    expect(result.current.messages.at(-1)).toMatchObject({ role: "interviewer", qnum: "2" });
    expect(result.current.session?.progress.answered).toBe(1);
  });

  it("卸载中止流并清定时器：signal 已 aborted、迟到事件不 setState、时钟再走不发对账 GET（M8 锚）", async () => {
    vi.useFakeTimers();
    getSessionMock.mockResolvedValue(state(Q1, 0));
    let emit!: (e: StreamEvent) => void;
    let capturedSignal: AbortSignal | undefined;
    streamAnswerMock.mockImplementationOnce(
      (_id: string, _body: unknown, opts: StreamOpts) =>
        new Promise<void>(() => {
          emit = opts.onEvent; // 永不收尾：卸载是唯一出路
          capturedSignal = opts.signal;
        }),
    );
    const { result, unmount } = renderHook(() => useInterview("s1"));
    await act(async () => {});
    act(() => result.current.setDraft("中途卸载的回答"));
    await act(async () => {
      result.current.submit();
    });
    await act(async () => {
      emit({ event: "round", expect: "1" });
    });
    await act(async () => {
      emit({ event: "delta", text: "半" });
    });
    expect(result.current.stage).toMatchObject({ kind: "thinking", streamText: "半" });
    act(() => {
      unmount();
    });
    expect(capturedSignal?.aborted).toBe(true); // 卸载真的触发 abort（M8）
    getSessionMock.mockReset();
    await act(async () => {
      emit({ event: "delta", text: "截" }); // 迟到 delta：不炸、不落地
      emit({ event: "question", kind: "question", question: Q2 }); // 迟到终局：不落地
    });
    expect(result.current.stage).toMatchObject({ kind: "thinking", streamText: "半" });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(getSessionMock).not.toHaveBeenCalled(); // 无对账、无任何后续请求
  });
});
