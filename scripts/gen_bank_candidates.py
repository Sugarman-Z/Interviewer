"""LLM 出题脚本：分配表 + 现有题 few-shot → corpus/_staging/。
可重复的管道入口（扩容/补缺口：lint 覆盖矩阵找空洞 → 写分配表 spec.json
→ 跑这里 → 人审 → promote_staging 入库）。

结构安全（staging 隔离）：输出路径强制落在 `_` 前缀目录下，写错直接拒绝
执行——防线在代码不在自觉；local_corpus 的 glob 同批排除 `_` 目录。

用法（真 API 消耗 token；离线测试走 tests/test_gen_bank_candidates.py 注入假 LLM）：
    PYTHONUTF8=1 python scripts/gen_bank_candidates.py \
        --spec scripts/data/bank_spec_example.json --title "Agent RAG 与检索"

spec 形状（UTF-8 JSON 数组，一元素一题；hint 可省）：
    [{"id": "bank-agent-021", "topic": "文档切块策略", "difficulty": 3,
      "tags": ["rag", "知识库"], "hint": "chunk 大小/重叠/结构感知"}]
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.bank_writer import write_file
from interviewer.default_config import get_config
from interviewer.llm import create_llm_client

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = REPO_ROOT / "corpus" / "_staging"

# few-shot 样板题（kafka-001=技术题样板、agent-001=工程判断题样板；
# id 硬编码可接受：这两个 id 是题库的格式基石，被删时本脚本应大声失败）
EXEMPLAR_IDS = ["bank-kafka-001", "bank-agent-001"]

RULES = """【硬性规则】
1. 题面 text：2-4 句、60~140 字，场景式发问、直接对候选人说话；禁止「请简述/请解释 X」式背诵题；题面不得包含答案要点。
2. 追问树 followup_tree：恰好 3 条，每条 15~45 字的独立问句，递进：实现/配置细节 → 边界情况 → 反例或取舍。
3. 评分锚点 scoring_anchors：恰好 3 条，分别以「5 分：」「3 分：」「1 分：」开头，每条 20~60 字。5 分=机制联动+边界/窗口说得精确；3 分=概念正确但说不清边界；1 分=表面化/想当然。
4. 全部中文（技术名词保留英文原文），中文标点（，。：？），任何字段不得出现 "null" 字样。
5. 同批各题的题面开头句式必须多样化。"""


class QuestionDraft(BaseModel):
    """结构化输出载体：method=function_calling（2026-09-16 冒烟定案，
    json_schema 经智谱端点 0/3 全炸——见 PROJECT_MEMORY）。"""

    id: str
    topic: str
    difficulty: int = Field(ge=1, le=5)
    tags: list[str]
    text: str
    followup_tree: list[str]
    scoring_anchors: list[str]
    source: str


class QuestionList(BaseModel):
    questions: list[QuestionDraft]


def _exemplars() -> list[BankQuestion]:
    from interviewer.dataflows.local_corpus import parse_corpus

    by_id = {q.id: q for q in parse_corpus(REPO_ROOT / "corpus")}
    missing = [i for i in EXEMPLAR_IDS if i not in by_id]
    if missing:
        raise ValueError(f"few-shot 样板题不在题库里：{missing}（格式基石被删？先恢复样板）")
    return [by_id[i] for i in EXEMPLAR_IDS]


def build_prompt(spec: list[dict[str, Any]], exemplars: list[BankQuestion]) -> str:
    blocks = "\n\n".join(
        f"### 样例（{q.id}）\n" + "\n".join(
            [f"topic: {q.topic}", f"difficulty: {q.difficulty}", f"tags: {', '.join(q.tags)}",
             "**题目**", q.text, "**追问树**", *[f"- {h}" for h in q.followup_tree],
             "**评分锚点**", *[f"- {a}" for a in q.scoring_anchors]]
        ) for q in exemplars
    )
    return (
        "你是资深技术面试官，在为「AI 模拟面试教练」扩充题库。按分配表逐条出题，"
        "一题不多一题不少，id 严格照抄。\n\n"
        f"【分配表】（id/topic/difficulty/tags 照抄不改；hint 是出题角度提示）\n"
        f"{json.dumps(spec, ensure_ascii=False, indent=1)}\n\n"
        f"【格式样例】（字段语义以此为准）\n{blocks}\n\n{RULES}\n\n"
        "返回 questions 数组，每题含 id/topic/difficulty/tags/text/followup_tree/"
        "scoring_anchors/source 八字段；source 一律填「LLM 草拟·」加今天日期（YYYY-MM-DD）。"
    )


def generate_candidates(llm: Any, spec: list[dict[str, Any]], today: str) -> list[BankQuestion]:
    """llm：有 with_structured_output 的客户端（create_llm_client 产物）。

    today 由调用方传入（可测试性：测试不依赖系统日期）；返回值已按 spec
    对齐校验——数量或 id 对不上直接 raise，宁可不写文件也不写半批。
    """
    if not hasattr(llm, "with_structured_output"):
        raise TypeError(
            "LLM 客户端不支持 with_structured_output（mock 模式？）——"
            "出题需要真模型：配置 ZHIPU_API_KEY 后重跑，或走离线批量管道"
        )
    prompt = build_prompt(spec, _exemplars())
    structured = llm.with_structured_output(QuestionList, method="function_calling")
    result = structured.invoke(prompt)
    drafts = result.questions

    want_ids = [s["id"] for s in spec]
    got_ids = [d.id for d in drafts]
    if sorted(got_ids) != sorted(want_ids):
        raise ValueError(f"生成结果与分配表不对齐：\n  期望 {want_ids}\n  实得 {got_ids}")

    return [
        BankQuestion(
            id=d.id, topic=d.topic, difficulty=d.difficulty, tags=d.tags,
            text=d.text, followup_tree=d.followup_tree, scoring_anchors=d.scoring_anchors,
            source=d.source or f"LLM 草拟·{today}", module="project",
        )
        for d in drafts
    ]


def write_staging(filename: str, title: str, questions: list[BankQuestion]) -> Path:
    """写出 staging 文件。防线：直接父目录必须 `_` 前缀——否则拒绝执行
（staging 隔离在代码里，不靠自觉；test_gen_bank_candidates 锁死）。"""
    target = (STAGING_DIR / filename).resolve()
    if not target.parent.name.startswith("_"):
        raise ValueError(f"出题产物只能写进 _ 前缀目录（staging 隔离），拒绝写入：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_file(target, title, questions)
    return target


def main(argv: list[str] | None = None) -> int:
    from datetime import date

    ap = argparse.ArgumentParser(description="LLM 出题 → corpus/_staging/（人审后才可入库）")
    ap.add_argument("--spec", required=True, help="分配表 JSON 路径（形状见模块头注释）")
    ap.add_argument("--out", required=True, help="staging 文件名，如 agent-rag-与检索.md")
    ap.add_argument("--title", default="", help="文件头标题（缺省用文件名去扩展名）")
    args = ap.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    if not spec:
        print("分配表为空", file=sys.stderr)
        return 1

    llm = create_llm_client("quick", get_config())
    questions = generate_candidates(llm, spec, date.today().isoformat())
    target = write_staging(args.out, args.title or Path(args.out).stem, questions)

    print(f"{len(questions)} 题已写入 {target}")
    print("下一步：人审（直接编辑该文件，删/改不要的块）→ "
          "PYTHONUTF8=1 python scripts/promote_staging.py "
          f"{Path(args.out).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
