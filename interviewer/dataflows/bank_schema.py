"""题库条目模型：corpus markdown 题块的解析目标形态。
消费方：local_corpus.parse_corpus 的产物 / search_corpus_local 的返回元素 /
签发回填注册表的值（IssueQuestion 拿 bank_qid 对账，命中用库内原值盖帽）。
"""
from pydantic import BaseModel, Field


class BankQuestion(BaseModel):
    """必填/默认的分界 = "缺了这条题还敢不敢用"（fail-loud/fail-open 在字段级的体现）。

    id/topic/text 缺 = 条目废了必须炸；追问树/评分锚点缺 = 条目弱了但还能用。
    """

    # 防幻觉对账锚：必须是 str——模型回传的 bank_qid 永远是字符串（"bank-kafka-001"），
    # 这里用 int 会在盖帽对账时制造 int/str 错位这一类最阴的 bug。
    # 命名约定 <域>-<主题>-<序号>：自描述、可 grep、跨文件不撞。
    id: str
    topic: str          # 考点锚：签发查重（issued_keys）与同考点追问共用
    text: str           # 题目原文（模型签发时可改编措辞，但 topic 锚由代码盖帽回库内原值）
    module: str = "project"
    difficulty: int = Field(ge=1, le=5, default=3)   # 手写笔误（如 7）由约束当场拦下
    tags: list[str] = Field(default_factory=list)
    followup_tree: list[str] = Field(default_factory=list)    # 追问树：E1 起由 Evaluator 按轮次消费（经 _stamp_bank 进 current_question.followup_hints）
    scoring_anchors: list[str] = Field(default_factory=list)  # 真评分上线后的 rubric 原料
    source: str = ""    # 出处（人读）；签发回填时进 source_ref
