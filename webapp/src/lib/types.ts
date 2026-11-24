/**
 * web-ui ↔ api-service 的 HTTP 契约类型。
 * 单一定义点：Mock 路由与真后端都以本文件为准，不许另起一份。
 */

// 消息与题目

export type InterviewerVariant = "opening" | "question" | "followup" | "closing";
export type CandidateVariant = "answer" | "skip";
export type TranscriptRole = "interviewer" | "candidate" | "system";
export type TranscriptVariant = InterviewerVariant | CandidateVariant | "system";

export interface IssuedQuestion {
  variant: InterviewerVariant;
  text: string;
  /** 主题题与追问的考点（opening / closing 可缺省） */
  topic?: string;
  /**
   * 题号：主题题 "1"…"10"，追问 "3.1"（主题号 + "." + 序）。
   * 报告页判断「题 N 被追问过」必须精确匹配 `qnum === `${N}.1``（带点号），
   * 禁用 startsWith——"10" 会被 "1" 前缀误配。
   */
  qnum?: string;
}

export interface TranscriptMessage {
  role: TranscriptRole;
  variant: TranscriptVariant;
  text: string;
  topic?: string;
  qnum?: string;
 /** ISO 8601；可选——引擎无每条时间戳，服务端不下发该键，前端不渲染 */
  at?: string;
}

// 创建会话

export interface CreateSessionInput {
  /** 必填，空值后端 400 */
  position: string;
  jd?: string;
  /** 线上字段名，与其余 wire 字段同为 snake_case（api.ts 客户端侧做映射） */
  resume_text?: string;
}

export interface SessionCreated {
  session_id: string;
  max_questions: number;
  /** 首问 = 开场自我介绍（variant="opening"） */
  question: IssuedQuestion;
}

// 回答

/**
 * 幂等守护槽位号（可选）：取值口径写死
 * `pending_question?.variant === "opening" ? "opening" : pending_question?.qnum`
 * ——禁用直填 qnum（opening 轮 qnum 为 undefined，JSON.stringify 丢键后
 * 服务端按缺字段静默跳过校验，幂等守护恰在开场轮失效）。Mock 路由忽略
 * 该键（向后兼容）
 */
export type AnswerRequest =
  | { type: "answer"; text: string; expect_qnum?: string }
  | { type: "skip"; expect_qnum?: string };

export interface DimensionScores {
  /** 各 1-5 分，与引擎评分维度同名同口径 */
  technical_depth: number;
  correctness: number;
  communication: number;
}

export interface TopicResult {
  /** 与 IssuedQuestion.qnum 同口径；判断「被追问过」用 `qnum === `${N}.1`` 精确匹配 */
  qnum: string;
  topic: string;
  is_followup: boolean;
  skipped: boolean;
 /** 可选：eval_failed 条目整键缺席——报告页显示「未评分」，不进聚合（禁 `?? 0`） */
  scores?: DimensionScores;
}

/** scores 在场的 TopicResult——均分/聚合口径的入参形状（「仅计非空条目」） */
export type ScoredTopicResult = TopicResult & { scores: DimensionScores };

export interface FinalPayload {
 /** 三维均值的 5 分制总分，一位小数（：不用百分制） */
  overall: number;
  /** 总评正文（markdown） */
  interview_report: string;
  topic_results: TopicResult[];
}

export type AnswerResult =
  | { kind: "question"; question: IssuedQuestion }
  | ({ kind: "final" } & FinalPayload);

// 流式事件

/** thinking 阶段文案源（opening 轮 issuing 由服务端同帧直发） */
export type StreamPhase = "evaluating" | "issuing" | "reporting";

/**
 * SSE 事件判别联合。data 载荷自描述（服务端帧恒带 event 键）；
 * 终局两事件复用 AnswerResult 的两臂（kind 判别，Extract 直取）——
 * 单一定义点纪律：不改动 AnswerResult、不复制其字段。
 */
export type StreamEvent =
  | { event: "round"; expect: string }
  | { event: "stage"; phase: StreamPhase }
  | { event: "delta"; text: string }
  | ({ event: "question" } & Extract<AnswerResult, { kind: "question" }>)
  | ({ event: "final" } & Extract<AnswerResult, { kind: "final" }>)
  | { event: "error"; error: string };

// 会话状态

export interface SessionProgress {
  /** 已答主题题数（追问不计入，与引擎 max_questions 口径一致） */
  answered: number;
  total: number;
}

export interface SessionState {
  session_id: string;
  position: string;
  status: "awaiting" | "finished";
  max_questions: number;
  /**
   * 约定：awaiting 态末条 interviewer 消息与 pending_question 重复下发
   *（服务端对违例 fail-loud）；ChatShell 的末条补渲染兜底保留为纯展示
   * 容错，不承担契约职责。
   */
  transcript: TranscriptMessage[];
  /** awaiting 时当前待答的题（刷新/断线恢复用） */
  pending_question?: IssuedQuestion;
  progress: SessionProgress;
  /** status === "finished" 时给出 */
  final?: FinalPayload;
}

/** 会话列表三态（，自方向 A 嫁接） */
export type SessionStatus = "ongoing" | "finished" | "interrupted";

export interface SessionSummary {
  session_id: string;
  position: string;
  started_at: string;
  status: SessionStatus;
  progress: SessionProgress;
  /**
   * awaiting 时当前待答题号（与 IssuedQuestion.qnum 同口径：opening 无题号，
   * 追问 "3.1" 归属第 3 题）。列表「第 N 题」必须与面试页头部同源取题号，
   * 不得用 answered+1 推导——追问挂起时会差一题。
   */
  pending_qnum?: string;
  /** finished 时给出 */
  overall?: number;
}

// 错误

export interface ApiErrorBody {
  /** 面向用户的中文提示 */
  error: string;
}
