"""图状态定义。纪律：不用 messages 通道，问答史走 transcript（普通列表）；
工具循环在节点内局部实现，messages 通道永不引入。

每个可变字段有唯一写者（例外：发言字段谁开口谁写；consecutive_llm_failures
由 IssueQuestion/Evaluator 共写、任一成功清零）：
    pending_question   ← OpeningIntro / IssueQuestion / Evaluator（发言字段）
    last_answer        ← AwaitIntro / AwaitAnswer（resume 值经 interrupt 返回）
    transcript         ← 各角色节点（append_transcript 返回全量新列表）
    current_question / current_topic ← IssueQuestion（唯一写者；done 显式清空）
    followup_state     ← Evaluator / ModuleSummary（重置）/ IssueQuestion（换题重置）
    last_scorecard     ← Evaluator（失败整块覆写 eval_failed dict）
    issued_keys ← IssueQuestion；consecutive_llm_failures ← IssueQuestion + Evaluator
    retrieval_trace    ← IssueQuestion（只落盘不回喂模型）
    topic_results      ← Evaluator（IssueQuestion 出题依据 / 掌握度底座）
    module_report      ← ModuleSummary
    interview_report   ← ReportWriter
"""
from typing import TypedDict

# interrupt 的 Command(resume=...) 载荷类型（第一天就结构化，答案与控制指令不混在纯字符串里）
RESUME_ANSWER = "answer"
RESUME_SKIP = "skip"
RESUME_EXIT = "exit"


class FollowupState(TypedDict):
    """单考点追问子状态。无 reducer，更新必须整块返回新 dict。
    followup_count = 本题已答轮数（换题归零）；total_count = 全场轮数（只增不减，
    仅 Evaluator 里 +1，作时长保险丝）。
    """
    topic: str
    qa_history: str
    followup_count: int
    total_count: int


def fresh_followup_state(topic: str = "", total_count: int = 0) -> FollowupState:
    return {"topic": topic, "qa_history": "", "followup_count": 0, "total_count": total_count}


class InterviewState(TypedDict):
    # 运行元信息（图外入口一次性写入，全程只读）
    session_id: str
    position: str            # 用户声明的岗位——事实锚：不得推断、不得虚构
    candidate_profile: str   # 简历原文/结构化摘要——事实锚（W1 为粘贴原文）
    jd_requirements: str     # JD 原文（可为空）
    past_context: str        # 到期错题 + 历史教训（图外 ReviewSettler 注入，W4 启用）
    # 问答史（产品数据，进报告与落盘；append-only，写者返回全量新列表）
    transcript: list
    # 问答工作区
    pending_question: str    # 面试官最新一问（interrupt 载体）
    last_answer: str
    # 当前题（IssueQuestion 签发；done 收场时显式清空—— 教训）
    current_question: dict   # 当前题（dict：text/topic/source_type/source_ref/...）
    current_topic: str
    # 评估与追问
    followup_state: FollowupState
    last_scorecard: dict
    # 签发与留痕（Phase B 动态架构新增）
    issued_keys: list              # 已签发考点（查重锚；W4 复考时作对照）
    consecutive_llm_failures: int  # 连续 LLM 失败计数（熔断用，任何一次成功清零）
    topic_results: list            # 每题评分留痕（IssueQuestion 出题依据 / W4 掌握度底座）
    retrieval_trace: list          # 检索留痕：{query, hit_ids, elapsed_ms,
                                   # empty_hit} + 循环补的 tool 键，C6 双工具可区分；
                                   # 跨签发累加，只落盘供评测/归因——
                                   # 绝不回喂模型，防它照着自己的检索史演戏）
    # 产出
    module_report: str
    interview_report: str
