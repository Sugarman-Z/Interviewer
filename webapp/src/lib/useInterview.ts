"use client";

/**
 * 面试页状态机：
 *
 *   restore ──GET──▶ await-input ──submit/skip──▶ thinking ──▶ await-input（下一问）
 *                      │                            ├─▶ finished（自然收场 → 跳 /report/[id]）
 *                      │                            └─▶ send-error（composer 锁定，「重试」为唯一出口）
 *   restore-error（GET 失败，原位重试）
 *
 * 幂等纪律：send-error 的重试先 GET 会话详情比对 pending_question.qnum，
 * 已推进则不重发，直接采用服务端状态——避免响应丢失后盲目重发把同一
 * 回答记到下一题上。提交附带 expect_qnum 槽位号（opening 写死字面量）；
 * skip 不本地合成 system 提示行；410 limbo 特判渲染 restore-error 同款卡片。
 *
 * 超时模型：send 走 streamAnswer + 45s idle watchdog + 600s 整轮护栏；
 * retry 的 legacy 重发腿保留 180s 整轮超时（无字节流可看护）；GET 恢复/
 * 对账 30s。restore 与 round 后断流对账 GET 对 409 in-flight 与自身超时
 * 自动退避（15s/30s，累计 ≥180s 才落终态），长回合期间不出现错误卡。
 *
 * 消息合成：POST 响应只带下一问/终局，候选人消息由本地按 mock 同款文案
 * 合成。流式失败映射（判据 = 是否已见 round）：未见 round 的失败 →
 * send-error；round 后断流 → poll-only GET 对账恒不重发；error 事件 →
 * send-error 文案透传（重试卡的 GET 比对负责决定重发）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, getSession, streamAnswer, submitAnswer } from "@/lib/api";
import { typewriterDurationMs } from "@/components/chat/StreamingBubble";
import type {
  AnswerRequest,
  AnswerResult,
  SessionState,
  StreamEvent,
  StreamPhase,
  TranscriptMessage,
} from "@/lib/types";

/** 超时拆分（retry 的 legacy 重发腿专用）：重发 180s——收场轮
 *  ReportWriter 常 40-120s，60s 必误伤，且无字节流可看护只能整轮超时；
 *  GET 恢复/对账 30s（同一常量会拖长死后端时的恢复路径） */
export const SUBMIT_TIMEOUT_MS = 180_000;
export const GET_TIMEOUT_MS = 30_000;

/** 流式看护（仅 send 路径）：idle watchdog 45s 无事件/字节 → abort；
 *  600s 整轮护栏兜「事件不断但回合永不收尾」。abort 后交由 streamAnswer
 *  的静默语义走「round 后 → 对账 / 未见 round → send-error」分支。
 *  约束：中止须裸 abort()（保持静默语义） */
export const IDLE_WATCHDOG_MS = 45_000;
export const OVERALL_GUARD_MS = 600_000;

/** 退避档位：GET 撞 409 in-flight / 自身超时 → 15s 后重试，再败 30s，
 *  累计等待 ≥180s 才落终态错误卡 */
const BACKOFF_FIRST_MS = 15_000;
const BACKOFF_NEXT_MS = 30_000;
const BACKOFF_CAP_MS = 180_000;

/** 退避信号：in-flight 409 或自身超时（AbortError）——其余错误照旧落终态 */
const isBackoffSignal = (e: unknown): boolean =>
  (e instanceof ApiError && e.status === 409) ||
  (e instanceof DOMException && e.name === "AbortError");

/** 累计退避调度：返回本次应等待的毫秒数；累计已达上限返回 null（落终态） */
function nextBackoffDelay(elapsed: { current: number }): number | null {
  if (elapsed.current >= BACKOFF_CAP_MS) return null;
  const delay = elapsed.current === 0 ? BACKOFF_FIRST_MS : BACKOFF_NEXT_MS;
  elapsed.current += delay;
  return delay;
}

export type InterviewStage =
  | { kind: "restore" }
  | { kind: "restore-error"; message: string }
  | { kind: "await-input" }
  | {
      kind: "thinking";
      attempted: AnswerRequest;
      /** delta 累积（方案 A：终局 question 整题权威替换） */
      streamText?: string;
      /** 阶段文案源（已知时 ThinkingBubble 由 phase 驱动并禁定时器） */
      phase?: StreamPhase;
    }
  | { kind: "send-error"; message: string; attempted: AnswerRequest }
  | { kind: "finished" };

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof DOMException && e.name === "AbortError") return "等待超时了——教练那边没有响应";
  return "网络似乎不太稳定，请重试";
}

const now = () => new Date().toISOString();

/** 把一次成功推进合成为下一个 session 状态（含本地 transcript 增量） */
function applyResult(prev: SessionState, attempted: AnswerRequest, result: AnswerResult): SessionState {
  const pending = prev.pending_question;
  const appended: TranscriptMessage[] = [];
  if (attempted.type === "skip") {
    appended.push({
      role: "candidate",
      variant: "skip",
      text: "（跳过了此题）",
      topic: pending?.topic,
      qnum: pending?.qnum,
      at: now(),
    });
    // 不再合成 system 提示行——「已跳过」气泡标注已足够
    //（MessageTurn 对 variant=skip 有专属渲染），合成行使 live 视图、
    // 刷新后 transcript、真后端三方分叉
  } else {
    appended.push({
      role: "candidate",
      variant: "answer",
      text: attempted.text,
      topic: pending?.topic,
      qnum: pending?.qnum,
      at: now(),
    });
  }

  if (result.kind === "question") {
    const q = result.question;
    appended.push({
      role: "interviewer",
      variant: q.variant,
      text: q.text,
      topic: q.topic,
      qnum: q.qnum,
      at: now(),
    });
    // 主答题数只在「答的是主题题」时 +1（opening/追问不计，与引擎口径一致）
    const answered = prev.progress.answered + (pending?.variant === "question" ? 1 : 0);
    return {
      ...prev,
      transcript: [...prev.transcript, ...appended],
      pending_question: q,
      progress: { ...prev.progress, answered },
    };
  }

  // final：自然收场。progress 沿用服务端口径不改写——熔断等提前收场时
  // answered < total 是事实，不虚构答满。
  return {
    ...prev,
    status: "finished",
    transcript: [...prev.transcript, ...appended],
    pending_question: undefined,
    final: {
      overall: result.overall,
      interview_report: result.interview_report,
      topic_results: result.topic_results,
    },
  };
}

/** 与 mock 兜底同款：transcript 末条不含当前待答题时补渲染（qnum === 精确比较） */
function ensurePendingTail(s: SessionState): TranscriptMessage[] {
  const pending = s.pending_question;
  if (s.status !== "awaiting" || !pending) return s.transcript;
  const last = s.transcript.at(-1);
  const ok = last?.role === "interviewer" && last.variant === pending.variant && last.qnum === pending.qnum;
  if (ok) return s.transcript;
  return [
    ...s.transcript,
    { role: "interviewer", variant: pending.variant, text: pending.text, topic: pending.topic, qnum: pending.qnum },
  ];
}

export interface UseInterview {
  stage: InterviewStage;
  session: SessionState | null;
  messages: TranscriptMessage[];
  draft: string;
  setDraft: (v: string) => void;
  /** 发送当前草稿（仅 await-input 有效；空文本不发） */
  submit: () => void;
  /** 跳过当前题（仅 await-input 有效） */
  skip: () => void;
  /** send-error 后的纪律重试：先 GET 比对题号，已推进不重发 */
  retry: () => void;
  /** restore-error 后重试恢复 */
  retryRestore: () => void;
  /**
   * 落定播完窗口：终局已落定（stage 已 await-input、输入已解锁）但真机
   * 形态下 delta 与终局同拍到达，打字机需要播完窗口——窗口内 ChatShell
   * 抑制 transcript 尾行并继续渲染流式气泡；播完自动撤空。事件流闭包
   * 捕获（中间态可能被 React 批掉，不依赖 stage 提交）
   */
  playOutText: string | null;
  /**
   * 播完窗口收拍信号：true = 刚收拍换装（playOutText 刚转 null）——
   * ChatShell 以它让满文本挂载的正式尾行跳过 pop 入场。下一轮 send
   * 复位；reduced-motion 无窗口故恒 false
   */
  playOutJustEnded: boolean;
}

export function useInterview(sessionId: string): UseInterview {
  const router = useRouter();
  const [stage, setStage] = useState<InterviewStage>({ kind: "restore" });
  const [session, setSession] = useState<SessionState | null>(null);
  const [draft, setDraft] = useState("");
  const [restoreNonce, setRestoreNonce] = useState(0);

  // 卸载守卫：迟到的响应不再 setState / router.push（离开即离开）。
  // body 里必须复位：dev 的 StrictMode 挂载时会「模拟卸载→重挂载」跑一遍
  // cleanup，useRef 只初始化一次，不复位则 aliveRef 永远为 false，
  // settleResult 全部静默丢弃——症状即「答题后卡思考态，刷新才见下一题」。
  const aliveRef = useRef(true);
  // 在途请求的控制器：卸载时立即 abort，不等超时自然结束
  const abortRef = useRef<AbortController | null>(null);
  // 落定播完窗口的撤下定时器（卸载清理）
  const playOutTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [playOutText, setPlayOutText] = useState<string | null>(null);
  // 播完窗口收拍信号：true = 刚从窗口收拍换装——ChatShell 让满文本的
  // 正式尾行静默挂载（跳过 pop 入场，同位同文不再闪）。下一轮 send 复位
  const [playOutJustEnded, setPlayOutJustEnded] = useState(false);

  /** 给请求挂超时（AbortController + 定时器，fake timers 可控），并登记便于卸载 abort */
  const timed = useCallback(<T,>(call: (signal: AbortSignal) => Promise<T>, timeoutMs: number): Promise<T> => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    return call(ctrl.signal).finally(() => {
      clearTimeout(timer);
      if (abortRef.current === ctrl) abortRef.current = null;
    });
  }, []);

  useEffect(
    () => {
      aliveRef.current = true;
      return () => {
        aliveRef.current = false;
        abortRef.current?.abort();
        if (playOutTimerRef.current) clearTimeout(playOutTimerRef.current);
      };
    },
    [],
  );

  const adopt = useCallback((s: SessionState) => {
    setSession(s);
    setStage(s.status === "finished" ? { kind: "finished" } : { kind: "await-input" });
  }, []);

  // 挂载/重试恢复（页面上 key={sessionId} 保证换会话必重挂载）。
  // GET 撞 409 in-flight / 自身超时 → 自动退避（15s/30s，累计 ≥180s 才落
  // restore-error），期间停留在 restore 加载态——长回合进行中刷新页面
  // 不该看到错误卡
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const elapsed = { current: 0 };
    const attempt = () => {
      timed((sig) => getSession(sessionId, sig), GET_TIMEOUT_MS).then(
        (s) => {
          if (alive) adopt(s);
        },
        (e: unknown) => {
          if (!alive) return;
          const delay = isBackoffSignal(e) ? nextBackoffDelay(elapsed) : null;
          if (delay !== null) {
            timer = setTimeout(() => {
              if (alive) attempt();
            }, delay);
            return;
          }
          setStage({ kind: "restore-error", message: errText(e) });
        },
      );
    };
    attempt();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [sessionId, restoreNonce, adopt, timed]);

  const retryRestore = useCallback(() => setRestoreNonce((n) => n + 1), []);

  /** 成功结果落地 */
  const settleResult = useCallback(
    (prev: SessionState, attempted: AnswerRequest, result: AnswerResult) => {
      if (!aliveRef.current) return;
      setSession(applyResult(prev, attempted, result));
      if (result.kind === "final") {
        setStage({ kind: "finished" });
        router.push(`/report/${sessionId}`);
      } else {
        setStage({ kind: "await-input" });
      }
    },
    [router, sessionId],
  );

  /** 失败落地：send-error（草稿仅在 answer 时还原展示；composer 已锁定不可编辑）。
   *  410 limbo 特判——落 restore-error 同款卡片（有「返回会话列表」出口、
   *  无「核对进度」副文案）；send-error 横幅的唯一按钮「重试」对 limbo
   *  永不成功，是死循环 UI */
  const failResult = useCallback((attempted: AnswerRequest, e: unknown) => {
    if (!aliveRef.current) return;
    if (e instanceof ApiError && e.status === 410) {
      setStage({ kind: "restore-error", message: errText(e) });
      return;
    }
    if (attempted.type === "answer") setDraft(attempted.text);
    setStage({ kind: "send-error", message: errText(e), attempted });
  }, []);

  /** round 后断流的对账：GET 退避轮询（15s/30s/累计 ≥180s 纪律），200 即
   *  采用服务端状态——round 已证回合被服务端接受，恒不重发。与 retry()
   *  的 poll 刻意不同：那是 send-error 卡的出口，带「GET 比对题号决定
   *  是否 legacy 重发」一腿 */
  const reconcile = useCallback(
    (attempted: AnswerRequest) => {
      const elapsed = { current: 0 };
      const poll = () => {
        timed((sig) => getSession(sessionId, sig), GET_TIMEOUT_MS).then(
          (fresh) => {
            if (!aliveRef.current) return;
            if (fresh.status === "finished") {
              setSession(fresh);
              setStage({ kind: "finished" });
              router.push(`/report/${sessionId}`);
              return;
            }
            // 对账发现回合未落定（引擎在本回合内失败、error 事件随断流
            // 丢失——checkpoint 未动、qnum 未推进）：采用服务端状态但还原
            // 草稿，对齐 error 事件可见路径的失败语义（评审 nit 采纳；
            // 槽位口径与 expect_qnum 同源，opening 用字面量）
            const slotOf = (s: SessionState) =>
              s.pending_question?.variant === "opening" ? "opening" : s.pending_question?.qnum;
            if (slotOf(fresh) === attempted.expect_qnum && attempted.type === "answer") {
              setDraft(attempted.text);
            }
            adopt(fresh); // 回合已落定，整态采用（含 transcript/进度）
          },
          (e: unknown) => {
            if (!aliveRef.current) return;
            const delay = isBackoffSignal(e) ? nextBackoffDelay(elapsed) : null;
            if (delay !== null) {
              setTimeout(() => {
                if (aliveRef.current) poll();
              }, delay);
              return;
            }
            failResult(attempted, e);
          },
        );
      };
      poll();
    },
    [sessionId, adopt, failResult, router, timed],
  );

  const send = useCallback(
    (req: AnswerRequest) => {
      if (stage.kind !== "await-input" || !session) return; // 等待期间不可重复提交
      const pending = session.pending_question;
      // §10-4 幂等守护槽位号：opening 写死字面量（其 qnum 为 undefined，
      // 直填会被 JSON.stringify 丢键 → 服务端按缺字段静默跳过校验）
      const body: AnswerRequest = {
        ...req,
        expect_qnum: pending?.variant === "opening" ? "opening" : pending?.qnum,
      };
      setStage({ kind: "thinking", attempted: body });
      setPlayOutJustEnded(false); // 上轮收拍信号只服务上轮换装（T8 追查二）
      if (req.type === "answer") setDraft("");

      const ctrl = new AbortController();
      abortRef.current = ctrl;
      let idleTimer: ReturnType<typeof setTimeout> | null = setTimeout(
        () => ctrl.abort(),
        IDLE_WATCHDOG_MS,
      );
      const bumpIdle = () => {
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(() => ctrl.abort(), IDLE_WATCHDOG_MS);
      };
      const guardTimer = setTimeout(() => ctrl.abort(), OVERALL_GUARD_MS);

      const seenRound = { current: false };
      const settled = { current: false };
      const streamAcc = { text: "" }; // delta 累积（闭包捕获——终局同拍到达时
      // 中间的 thinking{streamText} 可能被 React 批掉，播完窗口只能从这里取）

      const onEvent = (ev: StreamEvent) => {
        if (!aliveRef.current || settled.current) return;
        if (ev.event === "round") {
          seenRound.current = true;
        } else if (ev.event === "stage") {
          setStage((s) => (s.kind === "thinking" ? { ...s, phase: ev.phase } : s));
        } else if (ev.event === "delta") {
          streamAcc.text += ev.text;
          setStage((s) =>
            s.kind === "thinking" ? { ...s, streamText: streamAcc.text } : s,
          );
        } else if (ev.event === "question") {
          // 终局（question/final）：整题权威，经现行 settleResult 落地；
          // 曾有 delta 则开落定播完窗口（真机 delta 与终局同拍——打字机
          // 的播放窗口在前端补）。reduced-motion 用户瞬时全显：无窗口，
          // 尾行即时
          settled.current = true;
          const reduced =
            typeof window !== "undefined" &&
            window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
          if (streamAcc.text && !reduced) {
            setPlayOutText(streamAcc.text);
            if (playOutTimerRef.current) clearTimeout(playOutTimerRef.current);
            playOutTimerRef.current = setTimeout(() => {
              setPlayOutText(null);
              setPlayOutJustEnded(true); // 收拍：换装行静默入场信号
            }, typewriterDurationMs(Array.from(streamAcc.text).length) + 150);
          }
          settleResult(session, body, { kind: "question", question: ev.question });
        } else if (ev.event === "final") {
          settled.current = true;
          settleResult(session, body, {
            kind: "final",
            overall: ev.overall,
            interview_report: ev.interview_report,
            topic_results: ev.topic_results,
          });
        }
        // error 事件不进本回调：streamAnswer 在封装层转 ApiError(500) 抛出，
        // 落进下面的 rejection 分支
      };

      streamAnswer(sessionId, body, {
        onEvent,
        // watchdog 挂字节级活性：`: ping` 保活解析出零事件但字节活着——
        // 判据是「无字节间隔 45s」，事件级判据会把报告段 40-120s 的健康
        // 流当死掐掉
        onActivity: bumpIdle,
        signal: ctrl.signal,
      }).then(
        () => {
          // resolve = 正常收尾（终局已落地）或主动 abort（watchdog/护栏/卸载）
          if (settled.current || !aliveRef.current) return;
          if (seenRound.current) {
            reconcile(body); // round 后断流 → 对账，恒不重发
          } else {
            // 未见 round：提交未证实被接受 → 等同现行提交失败（超时/断流文案）
            failResult(body, new DOMException("stream cut", "AbortError"));
          }
        },
        (e: unknown) => {
          if (!aliveRef.current) return;
          // error 事件（ApiError 500）→ send-error 文案透传（回合未落定，
          // 重试卡的 GET 比对负责后续）；round 后网络断（TypeError 等非
          // ApiError）→ 对账。流前 4xx（含强降 503/410）天然未见 round
          if (seenRound.current && !(e instanceof ApiError)) {
            reconcile(body);
            return;
          }
          failResult(body, e);
        },
      ).finally(() => {
        clearTimeout(guardTimer);
        if (idleTimer) clearTimeout(idleTimer);
        if (abortRef.current === ctrl) abortRef.current = null;
      });
    },
    [stage, session, sessionId, settleResult, failResult, reconcile],
  );

  const submit = useCallback(() => {
    if (!draft.trim()) return;
    send({ type: "answer", text: draft.trim() });
  }, [draft, send]);

  const skip = useCallback(() => send({ type: "skip" }), [send]);

  const retry = useCallback(() => {
    if (stage.kind !== "send-error" || !session) return;
    const { attempted } = stage;
    const answeredQnum = session.pending_question?.qnum; // 发送时的待答题号
    setStage({ kind: "thinking", attempted });
    // §10-6：对账 GET 撞 409 in-flight / 自身超时 → 退避重试（等待文案即
    // thinking），累计 ≥180s 才回 send-error；期间不重发答案
    const elapsed = { current: 0 };
    const poll = () => {
      timed((sig) => getSession(sessionId, sig), GET_TIMEOUT_MS).then(
        (fresh) => {
          if (!aliveRef.current) return;
          // 重试纪律：比对题号（=== 精确匹配），已推进或已收场则不重发
          const advanced = fresh.status === "finished" || fresh.pending_question?.qnum !== answeredQnum;
          if (advanced) {
            if (fresh.status === "finished") {
              setSession(fresh);
              setStage({ kind: "finished" });
              router.push(`/report/${sessionId}`);
            } else {
              adopt(fresh); // 响应其实到了，采用服务端状态
            }
            if (attempted.type === "answer") setDraft(""); // 已确认送达，清掉旧回答防误发到下一题
            return;
          }
          timed((sig) => submitAnswer(sessionId, attempted, sig), SUBMIT_TIMEOUT_MS).then(
            (result) => {
              settleResult(fresh, attempted, result);
              if (attempted.type === "answer") setDraft(""); // 重发成功同样清空
            },
            (e: unknown) => failResult(attempted, e),
          );
        },
        (e: unknown) => {
          if (!aliveRef.current) return;
          const delay = isBackoffSignal(e) ? nextBackoffDelay(elapsed) : null;
          if (delay !== null) {
            setTimeout(() => {
              if (aliveRef.current) poll();
            }, delay);
            return;
          }
          failResult(attempted, e);
        },
      );
    };
    poll();
  }, [stage, session, sessionId, adopt, settleResult, failResult, router, timed]);

  return {
    stage,
    session,
    messages: session ? ensurePendingTail(session) : [],
    draft,
    setDraft,
    submit,
    skip,
    retry,
    retryRestore,
    playOutText,
    playOutJustEnded,
  };
}
