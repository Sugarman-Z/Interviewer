"""pydantic wire 模型（逐字段对齐 webapp/src/lib/types.ts）。
TranscriptMessage 不建模 at（引擎无每条时间戳）、TopicResult.scores 可选
（eval_failed 条目引擎给空 dict，伪造分数违反引擎纪律）。所有响应过
response_model 强制校验后下发。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

InterviewerVariant = Literal["opening", "question", "followup", "closing"]
CandidateVariant = Literal["answer", "skip"]
TranscriptRole = Literal["interviewer", "candidate", "system"]
TranscriptVariant = Literal[
    "opening", "question", "followup", "closing", "answer", "skip", "system"
]


class DimensionScores(BaseModel):
    """三维 1-5 分，与引擎 SCORE_DIMS 同名同口径（types.ts DimensionScores）。"""

    technical_depth: int
    correctness: int
    communication: int


class IssuedQuestion(BaseModel):
    variant: InterviewerVariant
    text: str
    topic: str | None = None
    qnum: str | None = None


class TranscriptMessage(BaseModel):
    role: TranscriptRole
    variant: TranscriptVariant
    text: str
    topic: str | None = None
    qnum: str | None = None
    # at 整键不建模：服务端不下传该键


class CreateSessionInput(BaseModel):
    """创建请求的 JSON 形状（multipart 字段同名）。宽松构造：
    岗位空/超长等业务闸在 routes 层按 出 400，不走 422。"""

    position: str = ""
    jd: str = ""
    resume_text: str = ""


class SessionCreated(BaseModel):
    session_id: str
    max_questions: int
    question: IssuedQuestion


class TopicResult(BaseModel):
    qnum: str
    topic: str
    is_followup: bool
    skipped: bool
    scores: DimensionScores | None = None  #：eval_failed 空分不下发


class FinalPayload(BaseModel):
    overall: float
    interview_report: str
    topic_results: list[TopicResult]


class QuestionResult(BaseModel):
    kind: Literal["question"] = "question"
    question: IssuedQuestion


class FinalResult(FinalPayload):
    kind: Literal["final"] = "final"


AnswerResult = QuestionResult | FinalResult


class SessionProgress(BaseModel):
    answered: int
    total: int


class SessionState(BaseModel):
    session_id: str
    position: str
    status: Literal["awaiting", "finished"]
    max_questions: int
    transcript: list[TranscriptMessage]
    pending_question: IssuedQuestion | None = None
    progress: SessionProgress
    final: FinalPayload | None = None


class SessionSummary(BaseModel):
    session_id: str
    position: str
    started_at: str
    status: Literal["ongoing", "finished", "interrupted"]
    progress: SessionProgress
    pending_qnum: str | None = None
    overall: float | None = None
