/**
 * Mock 会话的进程内存储 + 种子数据。
 * 挂在 globalThis 上以在 dev 热重载间存活；重启即清空（Mock 定位，不做持久化）。
 * 种子会话直接用剧本引擎「打」出来再回拨时间戳，保证与真实推进路径同构。
 */
import type {
  AnswerRequest,
  AnswerResult,
  CreateSessionInput,
  SessionState,
  SessionSummary,
} from "@/lib/types";
import { advanceSession, MAX_QUESTIONS, newSession, type MockSession } from "./script";

/** 超过该时长没有动作的 ongoing 会话在列表里显示为「已中断」 */
const INTERRUPTED_AFTER_MS = 60 * 60 * 1000;

interface Store {
  sessions: Map<string, MockSession>;
}
const g = globalThis as unknown as { __peimianMockStore?: Store };
const store: Store = (g.__peimianMockStore ??= { sessions: new Map() });

export function sleepMockDelay(): Promise<void> {
  const raw = process.env.MOCK_DELAY_MS;
  const n = raw === undefined || raw === "" ? undefined : Number(raw);
  // 非数字（如 "13s"）回落默认值而不是静默归零
  const ms = n === undefined || !Number.isFinite(n) ? 1500 : Math.max(0, n);
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 同会话串行化：并发/重试的重复提交直接 409，防止一条回答记到没见过的题上
const inflight = new Set<string>();

export function tryAcquire(id: string): boolean {
  if (inflight.has(id)) return false;
  inflight.add(id);
  return true;
}

export function release(id: string): void {
  inflight.delete(id);
}

export function createSession(input: CreateSessionInput): MockSession {
  const s = newSession(input);
  store.sessions.set(s.session_id, s);
  return s;
}

export function getSession(id: string): MockSession | undefined {
  return store.sessions.get(id);
}

/** 会话不存在 / 已结束时抛错，由路由层翻译成 404 / 409 */
export function advance(id: string, req: AnswerRequest): AnswerResult {
  const s = store.sessions.get(id);
  if (!s) throw new MockNotFound();
  if (!s.pending_question) throw new MockFinished();
  const { session, result } = advanceSession(s, req);
  store.sessions.set(id, session);
  return result;
}

export class MockNotFound extends Error {}
export class MockFinished extends Error {}

export function toState(s: MockSession): SessionState {
  return {
    session_id: s.session_id,
    position: s.position,
    status: s.final ? "finished" : "awaiting",
    max_questions: MAX_QUESTIONS,
    transcript: s.transcript,
    pending_question: s.pending_question,
    progress: { answered: s.answered_main, total: MAX_QUESTIONS },
    final: s.final,
  };
}

export function listSummaries(): SessionSummary[] {
  const now = Date.now();
  return [...store.sessions.values()]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((s) => {
      const status: SessionSummary["status"] = s.final
        ? "finished"
        : now - Date.parse(s.updated_at) > INTERRUPTED_AFTER_MS
          ? "interrupted"
          : "ongoing";
      return {
        session_id: s.session_id,
        position: s.position,
        started_at: s.created_at,
        status,
        progress: { answered: s.answered_main, total: MAX_QUESTIONS },
        pending_qnum: s.pending_question?.qnum,
        overall: s.final?.overall,
      };
    });
}

// 种子会话

const SEED_WORDS =
  "在项目里我们采用的是先更新数据库再删除缓存的策略，配合延迟双删覆盖读请求回填旧值的窗口；读未命中时回源加互斥锁防击穿，过期时间加随机抖动防集中失效，线上跑了两个季度没有出现明显不一致。";

function repeatTo(len: number): string {
  const base = SEED_WORDS.repeat(Math.ceil(len / SEED_WORDS.length) + 1);
  return base.slice(0, Math.max(8, len));
}

/** 用剧本引擎真实推进出一场（或半场）面试 */
function playInto(input: CreateSessionInput, lengths: number[], stopAfterMains?: number): MockSession {
  let s = newSession(input);
  s = advanceSession(s, { type: "answer", text: repeatTo(120) }).session; // 自我介绍
  while (!s.final && (stopAfterMains === undefined || s.answered_main < stopAfterMains)) {
    s = advanceSession(s, { type: "answer", text: repeatTo(lengths[s.answered_main % lengths.length]) }).session;
  }
  return s;
}

function backdate(s: MockSession, createdAgoMs: number, updatedAgoMs: number): void {
  const now = Date.now();
  s.created_at = new Date(now - createdAgoMs).toISOString();
  s.updated_at = new Date(now - updatedAgoMs).toISOString();
  // transcript 的时间戳不逐条回拨（演示数据，无人逐条核对时刻）
}

const DAY = 24 * 60 * 60 * 1000;
const MIN = 60 * 1000;

function seedDemoSessions(): void {
  const seeds: MockSession[] = [
    playInto({ position: "后端开发工程师" }, [90, 200, 25, 150, 45, 120, 15, 180, 60, 100]),
    playInto({ position: "前端开发工程师" }, [40, 160, 70, 30, 110, 20, 140, 55, 95, 35]),
    playInto({ position: "算法工程师" }, [130, 60, 170], 4),
    playInto({ position: "数据分析师" }, [80, 30], 2),
  ];
  backdate(seeds[0], 2 * DAY + 40 * MIN, 2 * DAY);
  backdate(seeds[1], 3 * DAY + 20 * MIN, 3 * DAY);
  backdate(seeds[2], 50 * MIN, 12 * MIN); // ongoing
  backdate(seeds[3], 3 * DAY, 3 * DAY - 30 * MIN); // interrupted（超 1 小时无动作）
  for (const s of seeds) store.sessions.set(s.session_id, s);
}

// 种子化放在文件末尾执行：seedDemoSessions 依赖上面的 const 常量，
// 提前调用会踩 TDZ（Cannot access before initialization）
if (store.sessions.size === 0) seedDemoSessions();
