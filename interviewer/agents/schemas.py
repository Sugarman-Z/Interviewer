"""pydantic 模型：Question（题库/兜底题载体）、Scorecard（评分卡）、
IssuedQuestion（IssueQuestion 动态签发的结构化载荷）。"""
from typing import Literal

from pydantic import BaseModel, Field

SourceType = Literal["resume", "jd", "bank", "generated", "review"]

# 评分三维的唯一定义点（原 evaluator.py 与 issue_question.py 各一份 _DIMS +
# flow_logic 字面第三遍——加第四维从"同批改 5 文件"变"改这一处"）
SCORE_DIMS = ("technical_depth", "correctness", "communication")


class Question(BaseModel):
    """一道面试题。纪律：source_type/source_ref 缺失的题不得进入列表（防幻觉的载体）。"""

    module: str = "project"          # 所属面试模块（W1 仅 project）
    topic: str                       # 考点（同考点追问的锚）
    text: str                        # 主问题
    source_type: SourceType = "generated"
    source_ref: str = ""             # 简历 bullet 原文 / JD 条款 / 题库 id / 生成说明
    followup_hints: list[str] = Field(default_factory=list)  # 追问方向（E1 起由 Evaluator 按轮次消费；题库 followup_tree 经 _stamp_bank 进来）


class Scorecard(BaseModel):
    """三维评分 + action 裁决 + 追问句，扁平结构（弱模型遵约率高）。

    action 缺省 "advance"：漏填与显式 advance 不可区分，方向安全（漏填不追问），
    与 score_parser 的 eval_failed 分层。followup_text 仅 action="followup" 时有效。
    """

    technical_depth: int = Field(ge=1, le=5)
    correctness: int = Field(ge=1, le=5)
    communication: int = Field(ge=1, le=5)
    evidence: dict[str, str] = Field(default_factory=dict)   # 每维必须引用候选人原话
    weak_points: list[str] = Field(default_factory=list)     # 错题本 pending 候选（≤3 条）
    action: Literal["followup", "advance"] = "advance"       # 裁决：同考点追问 / 换题
    followup_text: str = ""                                  # 追问句（仅 action=followup 时有效）


class IssuedQuestion(BaseModel):
    """单次签发载荷（扁平结构，弱模型遵约率高）。

    done=true 时 topic/text 允许占位（空值业务校验在节点内）；reason 进报告。
    """

    done: bool = False
    reason: str = ""
    module: str = "project"
    topic: str
    text: str
    source_type: SourceType = "generated"
    source_ref: str = ""
    followup_hints: list[str] = Field(default_factory=list)
    # bank_qid 对账凭据：不在检索账本 = 编造命中，按签发失败处理。
    # description 进 function calling 参数 schema，无工具路径也可见（但不应填）
    bank_qid: str | None = Field(
        default=None,
        # 措辞注意：不能写"否则填 null"——真 API 实测模型会把 null 当四个字母的
        # 字符串抄进去（2026-09-20 熔断事故），代码侧另有规范化兜底（_stamp_bank）
        description="仅在采用题库命中时填写（命中条目 id，如 bank-kafka-001）；否则省略该字段",
    )
