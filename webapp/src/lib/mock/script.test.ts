import { describe, expect, it } from "vitest";
import { advanceSession, buildFinal, MAX_QUESTIONS, newSession } from "./script";
import type { AnswerResult, TopicResult } from "@/lib/types";

const LONG = "线程池的核心参数包括核心线程数、最大线程数、空闲回收时间和工作队列，任务提交后先建核心线程，满了入队，队列满了再扩到最大线程数，最后触发拒绝策略让上游感知过载。";
const SHORT = "不太确定，先跳过这个细节。"; // < 30 字，不触发追问

const answer = (text: string) => ({ type: "answer" as const, text });
const SKIP = { type: "skip" as const };

function asQuestion(r: AnswerResult) {
  if (r.kind !== "question") throw new Error(`expected question, got ${r.kind}`);
  return r.question;
}

function asFinal(r: AnswerResult) {
  if (r.kind !== "final") throw new Error(`expected final, got ${r.kind}`);
  return r;
}

/** 全短答打完一场（不触发追问，最快路径） */
function playShortAll() {
  let s = newSession({ position: "后端开发工程师" });
  s = advanceSession(s, answer(SHORT)).session;
  let last: AnswerResult | null = null;
  while (!s.final) {
    const r = advanceSession(s, answer(SHORT));
    s = r.session;
    last = r.result;
  }
  return { session: s, last: last! };
}

describe("newSession", () => {
  it("首问是开场自我介绍（variant=opening），transcript 仅一条", () => {
    const s = newSession({ position: "后端开发工程师" });
    expect(s.pending_question?.variant).toBe("opening");
    expect(s.transcript).toHaveLength(1);
    expect(s.transcript[0]).toMatchObject({ role: "interviewer", variant: "opening" });
    expect(s.answered_main).toBe(0);
    expect(s.results).toHaveLength(0);
  });
});

describe("advanceSession · 推进", () => {
  it("自我介绍答完进入第一道主题题（qnum=1），自我介绍不评分", () => {
    const { session, result } = advanceSession(newSession({ position: "后端" }), answer(LONG));
    const q = asQuestion(result);
    expect(q.variant).toBe("question");
    expect(q.qnum).toBe("1");
    expect(q.topic).toBeTruthy();
    expect(session.results).toHaveLength(0);
    expect(session.answered_main).toBe(0);
  });

  it("长回答（≥30 字）在配有追问的题上触发追问 qnum=n.1，且不推进主题题数", () => {
    const s = advanceSession(newSession({ position: "后端" }), answer(LONG)).session; // → Q1
    const { session, result } = advanceSession(s, answer(LONG)); // 答 Q1 → 追问 1.1
    const q = asQuestion(result);
    expect(q.variant).toBe("followup");
    expect(q.qnum).toBe("1.1");
    expect(session.answered_main).toBe(1);
  });

  it("追问答完进入下一道主题题", () => {
    let s = advanceSession(newSession({ position: "后端" }), answer(LONG)).session;
    s = advanceSession(s, answer(LONG)).session; // → 追问 1.1
    const { result, session } = advanceSession(s, answer(LONG)); // 答追问 → Q2
    const q = asQuestion(result);
    expect(q.variant).toBe("question");
    expect(q.qnum).toBe("2");
    expect(session.answered_main).toBe(1);
  });

  it("短回答（<30 字）直接换题，不追问", () => {
    const s = advanceSession(newSession({ position: "后端" }), answer(LONG)).session;
    const { result, session } = advanceSession(s, answer(SHORT)); // 答 Q1（短）
    const q = asQuestion(result);
    expect(q.variant).toBe("question");
    expect(q.qnum).toBe("2");
    expect(session.results.filter((r) => r.is_followup)).toHaveLength(0);
  });

  it("跳过：候选人记 skip 变体 + system 消息，低分落账，不追问直接换题", () => {
    const s = advanceSession(newSession({ position: "后端" }), answer(LONG)).session;
    const { result, session } = advanceSession(s, SKIP);
    expect(asQuestion(result).qnum).toBe("2");
    const skipped = session.results.find((r) => r.qnum === "1");
    expect(skipped).toMatchObject({ skipped: true, is_followup: false });
    expect(skipped?.scores).toEqual({ technical_depth: 2, correctness: 2, communication: 2 });
    const tail = session.transcript.slice(-3).map((m) => `${m.role}/${m.variant}`);
    expect(tail).toContain("candidate/skip");
    expect(tail).toContain("system/system");
  });

  it("开场跳过：不评分，直接进 Q1", () => {
    const { result, session } = advanceSession(newSession({ position: "后端" }), SKIP);
    expect(asQuestion(result).qnum).toBe("1");
    expect(session.results).toHaveLength(0);
  });
});

describe("advanceSession · 收场", () => {
  it("打满 10 道主题题自然收场：返回 final，transcript 以 closing 结尾", () => {
    const { session, last } = playShortAll();
    const f = asFinal(last);
    expect(session.pending_question).toBeUndefined();
    expect(session.final?.overall).toBe(f.overall);
    expect(session.transcript.at(-1)).toMatchObject({ role: "interviewer", variant: "closing" });
    expect(session.transcript.filter((m) => m.variant === "question")).toHaveLength(MAX_QUESTIONS);
  });

  it("final：topic_results 覆盖全部题，overall 在 1-5 且一位小数，报告正文含关键节", () => {
    const { last } = playShortAll();
    const f = asFinal(last);
    expect(f.topic_results).toHaveLength(MAX_QUESTIONS);
    expect(f.overall).toBeGreaterThanOrEqual(1);
    expect(f.overall).toBeLessThanOrEqual(5);
    expect(Number.isInteger(f.overall * 10)).toBe(true);
    expect(f.interview_report).toContain("综合评分");
    expect(f.interview_report).toContain("## 表现亮点");
    expect(f.interview_report).toContain("## 下一场建议");
  });

  it("全 long 打法：配有追问的题各产生一条追问结果（10 主 + 追问）", () => {
    let s = newSession({ position: "后端" });
    s = advanceSession(s, answer(LONG)).session;
    let last: AnswerResult | null = null;
    while (!s.final) {
      const r = advanceSession(s, answer(LONG));
      s = r.session;
      last = r.result;
    }
    const f = asFinal(last!);
    const followups = f.topic_results.filter((r) => r.is_followup);
    expect(followups.length).toBeGreaterThan(0);
    expect(f.topic_results).toHaveLength(MAX_QUESTIONS + followups.length);
    expect(f.interview_report).toMatch(/含 \d+ 次追问/);
  });

  it("已收场的会话再推进抛错（路由层翻成 409）", () => {
    const { session } = playShortAll();
    expect(() => advanceSession(session, answer(SHORT))).toThrow();
  });
});

describe("buildFinal", () => {
  it("空结果不炸，overall 落在界内", () => {
    const f = buildFinal([], "测试岗位");
    expect(f.overall).toBeGreaterThanOrEqual(0);
    expect(f.topic_results).toHaveLength(0);
  });

  it("低分结果进入薄弱环节文案", () => {
    const results: TopicResult[] = [
      { qnum: "1", topic: "A", is_followup: false, skipped: false, scores: { technical_depth: 2, correctness: 2, communication: 2 } },
      { qnum: "2", topic: "B", is_followup: false, skipped: false, scores: { technical_depth: 5, correctness: 5, communication: 5 } },
    ];
    const f = buildFinal(results, "后端");
    expect(f.interview_report).toContain("**B**");
    expect(f.interview_report).toContain("**A**");
  });

  it("全低分场次：亮点区给兜底文案，不与薄弱区自相矛盾", () => {
    const results: TopicResult[] = [1, 2, 3].map((n) => ({
      qnum: String(n),
      topic: `考点${n}`,
      is_followup: false,
      skipped: n === 2,
      scores: { technical_depth: 2, correctness: 2, communication: 2 },
    }));
    const f = buildFinal(results, "后端");
    expect(f.interview_report).toContain("没有突出的亮点题");
    expect(f.interview_report).not.toContain("发挥稳定");
  });

  it("亮点与薄弱互斥：同一题不同时出现在两节", () => {
    const results: TopicResult[] = [1, 2, 3, 4, 5].map((n) => ({
      qnum: String(n),
      topic: `考点${n}`,
      is_followup: false,
      skipped: false,
      scores:
        n <= 2
          ? { technical_depth: 5, correctness: 5, communication: 5 }
          : { technical_depth: 2, correctness: 3, communication: 2 },
    }));
    const f = buildFinal(results, "后端");
    const highlights = f.interview_report.split("## 薄弱环节")[0];
    const weak = f.interview_report.split("## 薄弱环节")[1] ?? "";
    for (const n of [1, 2]) {
      expect(highlights).toContain(`**考点${n}**`);
      expect(weak).not.toContain(`**考点${n}**`);
    }
  });

  it("§10-3：未评分行不进 overall 聚合与亮点/薄弱，但保留在 topic_results", () => {
    const results: TopicResult[] = [
      { qnum: "1", topic: "并发与线程池", is_followup: false, skipped: false, scores: { technical_depth: 4, correctness: 4, communication: 5 } },
      { qnum: "2", topic: "Redis 缓存一致性", is_followup: false, skipped: false },
      { qnum: "3", topic: "MySQL 索引", is_followup: false, skipped: true, scores: { technical_depth: 2, correctness: 2, communication: 2 } },
    ];
    const f = buildFinal(results, "后端");
    expect(f.overall).toBe(3.2); // (13/3 + 2) / 2 ≈ 3.17 → 3.2，未评分行不进分子分母
    expect(f.topic_results).toHaveLength(3); // 未评分行原样下发
    expect(f.interview_report).not.toContain("**Redis 缓存一致性**"); // 无均分，不进亮点/薄弱两节
  });
});


// T7：流式事件序双端共锚（docs/contract/stream-events.sample.json）

import { readFileSync } from "node:fs";
import { fragment, sseFrame, streamScript } from "./script";
import type { AnswerRequest, StreamEvent } from "@/lib/types";

const CONTRACT = JSON.parse(
  readFileSync(new URL("../../../../docs/contract/stream-events.sample.json", import.meta.url), "utf8"),
) as { scenarios: { name: string; pattern: string[] }[] };
const PATTERNS = Object.fromEntries(CONTRACT.scenarios.map((s) => [s.name, s.pattern]));

/** 事件序 → 合同记号（连续 delta 折叠为 delta*，与 pytest 侧 _to_pattern 同规则） */
function toPattern(events: StreamEvent[]): string[] {
  const tokens: string[] = [];
  for (const ev of events) {
    const tok = ev.event === "stage" ? `stage:${ev.phase}` : ev.event;
    if (tok === "delta" && tokens.at(-1) === "delta*") continue;
    tokens.push(tok === "delta" ? "delta*" : tok);
  }
  return tokens;
}

/** 一回合的完整流式侧写：推进 + 事件序（槽位取推进前待答题） */
function streamRound(s: ReturnType<typeof newSession>, req: AnswerRequest) {
  const pending = s.pending_question!;
  const slot = pending.variant === "opening" ? "opening" : pending.qnum!;
  const r = advanceSession(s, req);
  return { session: r.session, result: r.result, events: streamScript(slot, r.result) };
}

describe("streamScript · 事件序读表对拍（T7 双锚 vitest 侧）", () => {
 /** 与 pytest 侧对称的通用断言：round 载荷槽位 + delta 拼接 ⊆ 题面 */
  function assertRoundCommon(events: StreamEvent[], slot: string) {
    expect(events[0]).toEqual({ event: "round", expect: slot });
    const deltas = events.filter((e) => e.event === "delta").map((e) => (e as { text: string }).text).join("");
    const terminal = events.at(-1);
    if (terminal?.event === "question") {
      expect((terminal as { question: { text: string } }).question.text).toContain(deltas);
    }
  }

  it("opening：round → issuing 同帧直发 → 逐字 → question（无 evaluating）", () => {
    const s = newSession({ position: "后端开发工程师" });
    const { events, result } = streamRound(s, answer(LONG));
    expect(toPattern(events)).toEqual(PATTERNS.opening);
    assertRoundCommon(events, "opening");
    // 终局对拍：question 事件剥 event 键 == advanceSession 产物
    expect(events.at(-1)).toEqual({ event: "question", kind: "question", question: asQuestion(result) });
  });

  it("main：短答不触发追问 → evaluating + issuing 两段 stage", () => {
    const s = advanceSession(newSession({ position: "后端开发工程师" }), answer(LONG)).session; // 过开场
    const { events } = streamRound(s, answer(SHORT));
    expect(toPattern(events)).toEqual(PATTERNS.main);
    assertRoundCommon(events, "1"); // 开场已答，本题 qnum=1
  });

  it("followup：长答触发追问 → evaluating 直接接逐字（无 issuing）", () => {
    const s = advanceSession(newSession({ position: "后端开发工程师" }), answer(LONG)).session;
    const { events, result } = streamRound(s, answer(LONG));
    expect(asQuestion(result).variant).toBe("followup");
    expect(toPattern(events)).toEqual(PATTERNS.followup);
    assertRoundCommon(events, "1");
  });

  it("skip：与主问轮同形（评估照跑 + 换发）", () => {
    const s = advanceSession(newSession({ position: "后端开发工程师" }), answer(LONG)).session;
    const { events } = streamRound(s, SKIP);
    expect(toPattern(events)).toEqual(PATTERNS.skip);
    assertRoundCommon(events, "1");
  });

  it("final：最后主问答完 → evaluating + reporting → final（报告正文不流式）", () => {
    let s = newSession({ position: "后端开发工程师" });
    s = advanceSession(s, answer(SHORT)).session; // 开场
    while (!s.final && s.answered_main < MAX_QUESTIONS - 1) {
      s = advanceSession(s, answer(SHORT)).session;
    }
    const { events, result } = streamRound(s, answer(SHORT));
    expect(result.kind).toBe("final");
    expect(toPattern(events)).toEqual(PATTERNS.final);
    assertRoundCommon(events, String(MAX_QUESTIONS)); // 最后一题主问
    expect(events.at(-1)).toEqual({ event: "final", ...asFinal(result) }); // 展开对拍：字段跟随不枚举
  });

  it("帧编码：event 行 == data 载荷的 event 键（与 pytest _parse_sse 同款锚）；分块拼接还原全文", () => {
    for (const line of sseFrame({ event: "round", expect: "1" }).split("\n")) {
      if (line.startsWith("event: ")) {
        expect(line).toBe("event: round");
      }
    }
    expect(JSON.parse(sseFrame({ event: "stage", phase: "issuing" }).split("data: ")[1])).toEqual({
      event: "stage",
      phase: "issuing",
    });
    const text = "介绍一下你自己，包括教育背景与最近一段经历。";
    expect(fragment(text).join("")).toBe(text); // 分块无损
    expect(fragment(text).every((c) => c.length <= 3)).toBe(true); // 2-3 字粒度
  });

  it("fused/no_chunks 为服务端专属形态：合同表在册、Mock 不可达（口径注记）", () => {
    expect(PATTERNS.fused_followup).toBeDefined();
    expect(PATTERNS.no_chunks).toBeDefined();
    // Mock 恒有 delta 且无追问预算——这两行只由 pytest 侧锁（表内 _note 注明同族推导）
  });
});
