/**
 * Mock 面试剧本引擎（纯逻辑，无 I/O；唯一的时间依赖是消息时间戳字段）。
 *
 * 确定性规则：
 * - 回答 ≥ FOLLOWUP_MIN_CHARS 字且该题配有追问 → 触发同考点追问（一次）；
 *   否则直接换下一道主题题
 * - 跳过 → 记 system 消息 + 低分，不追问，直接换题
 * - 评分按回答长度分档（长=好），1-5 分三维；纯演示口径，不代表真实引擎
 * - 主题题答满 MAX_QUESTIONS 道自然收场，追问不计入题数（与引擎口径一致）
 */
import type {
  AnswerRequest,
  AnswerResult,
  CreateSessionInput,
  DimensionScores,
  FinalPayload,
  IssuedQuestion,
  ScoredTopicResult,
  StreamEvent,
  TopicResult,
  TranscriptMessage,
} from "@/lib/types";

export const MAX_QUESTIONS = 10;
/** 回答达到该字数视为「值得追问」 */
const FOLLOWUP_MIN_CHARS = 30;
const SKIP_SCORES: DimensionScores = { technical_depth: 2, correctness: 2, communication: 2 };

interface MainQuestion {
  topic: string;
  text: string;
  followup?: { topic: string; text: string };
}

const MAIN_QUESTIONS: MainQuestion[] = [
  {
    topic: "并发与线程池",
    text: "先从并发聊起。讲讲线程池的核心参数是怎么协同工作的：任务提交之后，内部的执行流程是怎样的？为什么需要拒绝策略？",
    followup: {
      topic: "扩容时机与代价",
      text: "你提到「队列满了才扩容」——如果想让它更早扩容，参数应该怎么调？把核心线程数设得很大有什么代价？",
    },
  },
  {
    topic: "Redis 缓存一致性",
    text: "如果用 Redis 做缓存，缓存和数据库的一致性你会怎么保证？说说你采用的策略和选择它的理由。",
    followup: {
      topic: "双删失败兜底",
      text: "「延迟双删」的延迟窗口怎么估算？如果第二次删除失败了，有哪些兜底手段？",
    },
  },
  {
    topic: "MySQL 索引",
    text: "联合索引 (a, b, c) 建好之后，哪些查询能用上它？结合你项目里的真实例子说说。",
    followup: {
      topic: "索引失效场景",
      text: "那如果 where 条件里对列做了函数计算，比如 DATE(created_at)，索引还走得通吗？为什么？",
    },
  },
  {
    topic: "网络与 TCP",
    text: "TCP 连接建立阶段如果 SYN-ACK 丢了，客户端和服务端各自会怎么处理？重传机制是怎么工作的？",
  },
  {
    topic: "消息队列",
    text: "如何保证消息不丢、不重？把你知道的完整链路讲一遍，从生产到消费。",
    followup: {
      topic: "幂等与代价",
      text: "消费端幂等如果要做到「恰好一次」的体验？说说这个目标的代价和现实取舍。",
    },
  },
  {
    topic: "分布式锁",
    text: "多个实例同时跑定时任务，怎么保证只有一个执行？你用的方案有什么坑？",
    followup: {
      topic: "看门狗与误删",
      text: "锁续期和误删怎么防？和 ZooKeeper / etcd 这类实现比，取舍在哪里？",
    },
  },
  {
    topic: "系统设计",
    text: "设计一个短链接服务：怎么存、怎么跳转、怎么扛住高并发的读取？",
  },
  {
    topic: "项目复盘",
    text: "讲一次你做过的最难的技术决策：当时的约束是什么、有哪些选项、最后为什么这么选？",
    followup: {
      topic: "决策复盘",
      text: "如果现在重做一次，你会改动其中的哪一步？为什么？",
    },
  },
  {
    topic: "线上排查",
    text: "线上接口突然变慢，你的排查思路是什么？按顺序讲，越具体越好。",
  },
  {
    topic: "收尾与反问",
    text: "技术问题问完了。你有什么想反问我的？以及接下来三个月，你的学习规划是什么？",
  },
];

function openingQuestion(position: string): IssuedQuestion {
  return {
    variant: "opening",
    text: `你好，我是本场「${position}」模拟面试的面试官。整场大约 ${MAX_QUESTIONS} 个问题，每答完一题我会视情况追问或换题。开始之前，请先用一两分钟做个自我介绍：教育背景、最近一段经历，以及你最有把握的方向。`,
  };
}

const CLOSING_TEXT = `好，本场 ${MAX_QUESTIONS} 个问题都问完了，感谢你的时间和坦诚。终场报告已经生成，去看看你的表现吧。`;

export interface MockSession {
  session_id: string;
  position: string;
  created_at: string;
  updated_at: string;
  transcript: TranscriptMessage[];
  results: TopicResult[];
  /** 当前待答题；收场后为 undefined */
  pending_question?: IssuedQuestion;
  /** 已答主题题数（追问不计入） */
  answered_main: number;
  /** 收场后给出 */
  final?: FinalPayload;
}

function msg(m: Omit<TranscriptMessage, "at">): TranscriptMessage {
  return { ...m, at: new Date().toISOString() };
}

function newId(): string {
  return `m-${Math.random().toString(36).slice(2, 8)}${Date.now().toString(36)}`;
}

function scoreByLength(len: number): DimensionScores {
  const clamp = (n: number) => Math.max(1, Math.min(5, n));
  return {
    technical_depth: clamp(2 + Math.floor(len / 80)),
    correctness: clamp(2 + Math.floor(len / 100)),
    communication: clamp(2 + Math.floor(len / 60)),
  };
}

export function newSession(input: CreateSessionInput): MockSession {
  const now = new Date().toISOString();
  const opening = openingQuestion(input.position);
  return {
    session_id: newId(),
    position: input.position,
    created_at: now,
    updated_at: now,
    transcript: [msg({ role: "interviewer", variant: "opening", text: opening.text })],
    results: [],
    pending_question: opening,
    answered_main: 0,
  };
}

/** 推进一回合：落账候选人回答，返回下一问或终局。纯函数（返回新对象）。*/
export function advanceSession(
  s: MockSession,
  req: AnswerRequest,
): { session: MockSession; result: AnswerResult } {
  if (!s.pending_question) throw new Error("advanceSession: session already finished");
  const pending = s.pending_question;
  const session: MockSession = {
    ...s,
    transcript: [...s.transcript],
    results: [...s.results],
  };
  const push = (m: Omit<TranscriptMessage, "at">) => session.transcript.push(msg(m));

  // 1) 候选人侧落账
  if (req.type === "skip") {
    push({
      role: "candidate",
      variant: "skip",
      text: "（跳过了此题）",
      topic: pending.topic,
      qnum: pending.qnum,
    });
    push({ role: "system", variant: "system", text: "已跳过此题 · 我们换一道" });
  } else {
    push({
      role: "candidate",
      variant: "answer",
      text: req.text,
      topic: pending.topic,
      qnum: pending.qnum,
    });
  }

  // 2) 评分落账（开场自我介绍不评分，与引擎口径一致）
  if (pending.variant === "question" || pending.variant === "followup") {
    const skipped = req.type === "skip";
    const scores = skipped ? SKIP_SCORES : scoreByLength(req.type === "answer" ? req.text.length : 0);
    session.results.push({
      qnum: pending.qnum ?? "",
      topic: pending.topic ?? "",
      is_followup: pending.variant === "followup",
      skipped,
      scores,
    });
    if (pending.variant === "question") session.answered_main += 1;
  }

  // 3) 决定下一跳
  const answerLen = req.type === "answer" ? req.text.length : 0;
  if (pending.variant === "question" && req.type === "answer" && answerLen >= FOLLOWUP_MIN_CHARS) {
    const main = MAIN_QUESTIONS[session.answered_main - 1];
    if (main?.followup) {
      const fq: IssuedQuestion = {
        variant: "followup",
        text: main.followup.text,
        topic: main.followup.topic,
        qnum: `${session.answered_main}.1`,
      };
      push({ role: "interviewer", variant: "followup", text: fq.text, topic: fq.topic, qnum: fq.qnum });
      session.pending_question = fq;
      session.updated_at = new Date().toISOString();
      return { session, result: { kind: "question", question: fq } };
    }
  }
  return nextStep(session);
}

function nextStep(session: MockSession): { session: MockSession; result: AnswerResult } {
  if (session.answered_main >= MAX_QUESTIONS) {
    const final = buildFinal(session.results, session.position);
    session.transcript.push(
      msg({ role: "interviewer", variant: "closing", text: CLOSING_TEXT }),
    );
    session.pending_question = undefined;
    session.final = final;
    session.updated_at = new Date().toISOString();
    return { session, result: { kind: "final", ...final } };
  }
  const main = MAIN_QUESTIONS[session.answered_main];
  const q: IssuedQuestion = {
    variant: "question",
    text: main.text,
    topic: main.topic,
    qnum: String(session.answered_main + 1),
  };
  session.transcript.push(msg({ role: "interviewer", variant: "question", text: q.text, topic: q.topic, qnum: q.qnum }));
  session.pending_question = q;
  session.updated_at = new Date().toISOString();
  return { session, result: { kind: "question", question: q } };
}

function resultMean(r: ScoredTopicResult): number {
  return (
    (r.scores.technical_depth + r.scores.correctness + r.scores.communication) / 3
  );
}

// 流式剧本（Mock /answers/stream 的纯逻辑核）

/** 题面分块：交替 2-3 字（确定性——同题面恒同分块，测试可对拍） */
export function fragment(text: string): string[] {
  const chunks: string[] = [];
  let size = 2;
  for (let i = 0; i < text.length; ) {
    chunks.push(text.slice(i, i + size));
    i += size;
    size = size === 2 ? 3 : 2;
  }
  return chunks;
}

/** SSE 帧编码：与服务端 routes.py _sse_encode 同形（event 行 + 自描述
 *  data 载荷；mock 侧补一条 event 行 == data.event 的对齐锚——pytest 侧
 *  _parse_sse 有同款断言） */
export function sseFrame(ev: StreamEvent): string {
  return `event: ${ev.event}\ndata: ${JSON.stringify(ev)}\n\n`;
}

/**
 * 一回合的流式事件序（纯函数：不碰 store，路由负责锁与延时）。
 * 形态判别与 T3 服务端同规则：开场轮直发 issuing（无 evaluating）；
 * 追问句产自评估调用（evaluating 直接接 delta，无 issuing）；主问/skip
 * 两段 stage；终场轮 reporting（报告正文不流式）。fused/no_chunks 形态
 * Mock 不可达（无追问预算、恒有 chunk）——合同表里那两行是服务端专属，
 * 由 pytest 侧锁。
 */
export function streamScript(slotExpect: string, result: AnswerResult): StreamEvent[] {
  const events: StreamEvent[] = [{ event: "round", expect: slotExpect }];
  const pushDeltas = (text: string) => {
    for (const piece of fragment(text)) events.push({ event: "delta", text: piece });
  };
  if (slotExpect === "opening") {
    events.push({ event: "stage", phase: "issuing" });
  } else if (result.kind === "final") {
    events.push({ event: "stage", phase: "evaluating" });
    events.push({ event: "stage", phase: "reporting" });
  } else if (result.question.variant === "followup") {
    events.push({ event: "stage", phase: "evaluating" });
  } else {
    events.push({ event: "stage", phase: "evaluating" });
    events.push({ event: "stage", phase: "issuing" });
  }
  if (result.kind === "question") {
    pushDeltas(result.question.text);
    events.push({ event: "question", kind: "question", question: result.question });
  } else {
  // 展开构造：FinalPayload 未来加可选字段自动跟随，与
    // legacy 路由/真后端 model_dump 同构——枚举会让三方静默分叉
    events.push({ event: "final", ...result });
  }
  return events;
}

export function buildFinal(results: TopicResult[], position: string): FinalPayload {
  //：未评分行（eval_failed——wire 形态整键缺席 / engine 形态空 dict）
  // 不进聚合与亮点/薄弱排序，但仍在 topic_results 原样下发（报告页显示
  // 「未评分」）；overall 半值进位
  const scoredResults = results.filter(
    (r): r is ScoredTopicResult => r.scores !== undefined && Object.keys(r.scores).length > 0,
  );
  const means = scoredResults.map(resultMean);
  const overall = Math.round((means.reduce((a, b) => a + b, 0) / Math.max(1, means.length)) * 10) / 10;
  const byMean = [...scoredResults].sort((a, b) => resultMean(b) - resultMean(a));
  // 亮点/薄弱对称过滤（≥4 / <4），保证两节互斥且全低分场次不自相矛盾
  const top = byMean.filter((r) => resultMean(r) >= 4).slice(0, 2);
  const bottom = byMean.filter((r) => resultMean(r) < 4).slice(-2).reverse();
  const followups = results.filter((r) => r.is_followup).length;
  const skipped = results.filter((r) => r.skipped).length;

  const line = (r: ScoredTopicResult, strong: string, weak: string) =>
    `- **${r.topic}**（${resultMean(r).toFixed(1)} 分）：${resultMean(r) >= 4 ? strong : weak}`;

  const sections: string[] = [];
  sections.push(
    `本场针对「${position}」岗位共 ${results.length - followups} 题（含 ${followups} 次追问），综合评分 **${overall.toFixed(1)} / 5**。`,
  );
  sections.push(
    top.length > 0
      ? ["## 表现亮点", ...top.map((r) => line(r, "回答完整，追问之下依然站得住。", "发挥稳定，继续保持。"))].join("\n")
      : "## 表现亮点\n本场没有突出的亮点题，先把基础考点答稳，把回答长度和结构练起来。",
  );
  sections.push(
    bottom.length > 0
      ? ["## 薄弱环节", ...bottom.map((r) => line(r, "", "建议按「正常路径 → 失败路径 → 兜底与监控」三段重新整理这个考点。"))].join("\n")
      : "## 薄弱环节\n本场没有明显短板，下一场可以加大难度或换个岗位域。",
  );
  sections.push(
    [
      "## 下一场建议",
      skipped > 0
        ? `本场有 ${skipped} 题跳过——先把这些考点补上再来一场，分数会更接近真实水平。`
        : "优先补薄弱环节对应的考点；表达上保持「先结论、再依据、后例子」的节奏。",
    ].join("\n"),
  );
  return { overall, interview_report: sections.join("\n\n"), topic_results: results };
}
