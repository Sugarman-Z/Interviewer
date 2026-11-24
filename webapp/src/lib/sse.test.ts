/**
 * 流式解析与封装测试。
 *
 * 两层各司其职：parseSSE 无状态纯函数（跨 chunk 断帧由 rest 回传解决），
 * streamAnswer 只做传输与两段论错误映射（流前非 2xx → ApiError 同型、
 * 流中 error 事件 → ApiError(500)、abort 静默返回不产生未处理拒绝）。
 * fetch 经 vi.stubGlobal 桩化（真 Response + ReadableStream，Node 环境原生）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, streamAnswer } from "./api";
import { parseSSE } from "./sse";
import type { StreamEvent } from "./types";

const encoder = new TextEncoder();

function frame(event: StreamEvent): string {
  return `event: ${event.event}\ndata: ${JSON.stringify(event)}\n\n`;
}

describe("parseSSE · 帧解析纯函数", () => {
  it("单 chunk 多事件：一次全部吐出，rest 为空", () => {
    const text = frame({ event: "round", expect: "opening" }) +
      frame({ event: "stage", phase: "issuing" }) +
      frame({ event: "delta", text: "介绍一下你自己" });
    const { events, rest } = parseSSE(text);
    expect(events).toEqual([
      { event: "round", expect: "opening" },
      { event: "stage", phase: "issuing" },
      { event: "delta", text: "介绍一下你自己" },
    ]);
    expect(rest).toBe("");
  });

  it("跨 chunk 断帧：半截帧留 rest，与下一片拼前缀后完整吐出", () => {
    const text = frame({ event: "round", expect: "opening" }) +
      frame({ event: "question", kind: "question", question: { variant: "question", text: "下一问" } });
    const cut = frame({ event: "round", expect: "opening" }).length + 20; // 落在第二帧中间
    const r1 = parseSSE(text.slice(0, cut));
    expect(r1.events).toEqual([{ event: "round", expect: "opening" }]);
    expect(r1.rest).not.toBe("");
    const r2 = parseSSE(r1.rest + text.slice(cut));
    expect(r2.events).toEqual([
      {
        event: "question",
        kind: "question",
        question: { variant: "question", text: "下一问" },
      },
    ]);
    expect(r2.rest).toBe("");
  });

  it("data: 多行以换行连接（规范语义）", () => {
    const text = 'event: delta\ndata: {"event":\ndata: "delta","text":"x"}\n\n';
    const { events } = parseSSE(text);
    expect(events).toEqual([{ event: "delta", text: "x" }]);
  });

  it("注释行与无 data 字段的帧不派发（: ping 保活帧静默丢弃）", () => {
    const text =
      ": ping\n\n" +
      frame({ event: "round", expect: "1" }) +
      ": keepalive\n\n" +
      "event: novalue\n\n";
    const { events, rest } = parseSSE(text);
    expect(events).toEqual([{ event: "round", expect: "1" }]);
    expect(rest).toBe("");
  });

  it("CRLF 容忍：\\r\\n 行终止与帧分隔等价", () => {
    const text = 'event: stage\r\ndata: {"event":"stage","phase":"evaluating"}\r\n\r\n';
    const { events } = parseSSE(text);
    expect(events).toEqual([{ event: "stage", phase: "evaluating" }]);
  });

  it("event: 行缺省仍按 data 载荷派发（规范实现；本题 wire 恒有名）", () => {
    const text = 'data: {"event":"delta","text":"无 event 行"}\n\n';
    const { events } = parseSSE(text);
    expect(events).toEqual([{ event: "delta", text: "无 event 行" }]);
  });

  it("坏 data 帧整帧丢弃，同批好帧照发（合同外输入加固，评审修正）", () => {
    const text =
      'event: round\ndata: {"event":"round","expect":"1"}\n\n' +
      "data: oops（代理注入的明文）\n\n" +
      'event: stage\ndata: {"event":"stage","phase":"issuing"}\n\n';
    const { events } = parseSSE(text);
    expect(events).toEqual([
      { event: "round", expect: "1" },
      { event: "stage", phase: "issuing" },
    ]);
  });
});

describe("streamAnswer · fetch 封装（两段论错误映射）", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubStream(chunks: string[]): void {
    stubStreamBytes(chunks.map((s) => encoder.encode(s)));
  }

  function stubStreamBytes(chunks: Uint8Array[]): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              for (const b of chunks) c.enqueue(b);
              c.close();
            },
          }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        ),
      ),
    );
  }

  it("读全流逐事件回调并正常收尾；fetch 参数锚（URL/method/body/signal 透传）", async () => {
    const signal = new AbortController().signal;
    stubStream([
      'event: round\ndata: {"event":"round","expect":"1"}\n\n',
      'event: stage\ndata: {"event":"stage","phase":"evaluating"}\n\n',
      `event: question\ndata: ${JSON.stringify({
        event: "question",
        kind: "question",
        question: { variant: "question", text: "技术选型讲讲？", qnum: "2" },
      })}\n\n`,
    ]);
    const seen: StreamEvent[] = [];
    await streamAnswer(
      "s1",
      { type: "answer", text: "标准回答", expect_qnum: "1" },
      { onEvent: (e) => seen.push(e), signal },
    );
    expect(seen.map((e) => e.event)).toEqual(["round", "stage", "question"]);
    expect(seen[2]).toEqual({
      event: "question",
      kind: "question",
      question: { variant: "question", text: "技术选型讲讲？", qnum: "2" },
    });
    const fetchMock = vi.mocked(fetch);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(
      "/api/sessions/s1/answers/stream",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "answer", text: "标准回答", expect_qnum: "1" }),
        signal,
      },
    );
  });

  it("多字节 UTF-8 被 chunk 边界劈开：字节级分片仍完整还原（decoder stream 锚）", async () => {
    const text = 'event: delta\ndata: {"event":"delta","text":"题面整块中文到达"}\n\n';
    const bytes = encoder.encode(text);
    // 劈点落在多字节字符中间：前缀（含"题"）字节长度 +1 → 劈进"面"的第 2 字节
    const cut = encoder.encode(text.slice(0, text.indexOf("题面") + 1)).length + 1;
    stubStreamBytes([bytes.slice(0, cut), bytes.slice(cut)]);
    const seen: StreamEvent[] = [];
    await streamAnswer("s1", { type: "answer", text: "x" }, { onEvent: (e) => seen.push(e) });
    expect(seen).toEqual([{ event: "delta", text: "题面整块中文到达" }]); // U+FFFD 乱码即红
  });

  it("收尾残片（半截帧无分隔符）丢弃不派发：resolve 即未见终局的上层对账信号", async () => {
    stubStream([
      'event: round\ndata: {"event":"round","expect":"1"}\n\n',
      'event: delta\ndata: {"event":"delta","te',
    ]);
    const seen: string[] = [];
    await streamAnswer("s1", { type: "answer", text: "x" }, { onEvent: (e) => seen.push(e.event) });
    expect(seen).toEqual(["round"]);
  });

  it("onActivity 字节级活性：纯 ping 注释片零事件但活性到达（watchdog 喂活源）", async () => {
    stubStream([
      'event: round\ndata: {"event":"round","expect":"1"}\n\n',
      ": ping\n\n",
      ": ping\n\n",
    ]);
    const seen: string[] = [];
    let activity = 0;
    await streamAnswer("s1", { type: "answer", text: "x" }, {
      onEvent: (e) => seen.push(e.event),
      onActivity: () => {
        activity += 1;
      },
    });
    expect(seen).toEqual(["round"]); // ping 片不派发事件
    expect(activity).toBe(3); // 每个 chunk 都算活性（1 事件片 + 2 ping 片）
  });

  it("chunk 边界落在帧中间：拼接后不丢事件", async () => {
    const text = 'event: delta\ndata: {"event":"delta","text":"整块题面一次到达"}\n\n';
    stubStream([text.slice(0, 21), text.slice(21)]);
    const seen: StreamEvent[] = [];
    await streamAnswer("s1", { type: "answer", text: "x" }, { onEvent: (e) => seen.push(e) });
    expect(seen).toEqual([{ event: "delta", text: "整块题面一次到达" }]);
  });

  it("流前 409/503 JSON → ApiError(状态码, 文案)，与 request() 同型", async () => {
    const cases = [
      [409, "本回合仍在进行中，请稍候"],
      [503, "流式输出暂不可用，本场将使用整轮等待模式"],
    ] as const;
    for (const [status, message] of cases) {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => new Response(JSON.stringify({ error: message }), { status })),
      );
      const err = await streamAnswer("s1", { type: "answer", text: "x" }, { onEvent: () => {} }).catch(
        (e: unknown) => e,
      );
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(status);
      expect((err as ApiError).message).toBe(message);
    }
  });

  it("流中 error 事件 → ApiError(500, 文案)；此前事件已回调", async () => {
    stubStream([
      'event: round\ndata: {"event":"round","expect":"1"}\n\n',
      `event: error\ndata: ${JSON.stringify({
        event: "error",
        error: "面试官暂时掉线了，请稍后刷新查看本场状态",
      })}\n\n`,
    ]);
    const seen: string[] = [];
    const err = await streamAnswer("s1", { type: "answer", text: "x" }, {
      onEvent: (e) => seen.push(e.event),
    }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
    expect((err as ApiError).message).toBe("面试官暂时掉线了，请稍后刷新查看本场状态");
    expect(seen).toEqual(["round"]);
  });

  it("abort：读流中被中止 → 静默返回不产生拒绝", async () => {
    const ctrl = new AbortController();
    let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(c) {
              streamController = c;
              c.enqueue(encoder.encode('event: round\ndata: {"event":"round","expect":"1"}\n\n'));
              // 不 close：模拟长回合静默段，读循环挂在第二个 read 上
            },
          }),
          { status: 200 },
        ),
      ),
    );
    const seen: string[] = [];
    const pending = streamAnswer("s1", { type: "answer", text: "x" }, {
      onEvent: (e) => seen.push(e.event),
      signal: ctrl.signal,
    });
    await new Promise((r) => setTimeout(r, 0)); // 让首帧送达读循环
    ctrl.abort();
    streamController!.error(new DOMException("Aborted", "AbortError"));
    await expect(pending).resolves.toBeUndefined();
    expect(seen).toEqual(["round"]);
  });

  it("abort：fetch 阶段（响应头到达前）被中止 → 同样静默返回（评审修正的两段合同）", async () => {
    const ctrl = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: unknown, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            // 永不 resolve：模拟服务端 withhold 响应头（T3 await-first）+
            // 单引擎线程被其他会长回合占住的排队窗口
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("Aborted", "AbortError"));
            });
          }),
      ),
    );
    const seen: string[] = [];
    const pending = streamAnswer("s1", { type: "answer", text: "x" }, {
      onEvent: (e) => seen.push(e.event),
      signal: ctrl.signal,
    });
    await new Promise((r) => setTimeout(r, 0));
    ctrl.abort();
    await expect(pending).resolves.toBeUndefined();
    expect(seen).toEqual([]);
  });
});
