"""RAG 回归锁（离线）：金标 schema 锁 + verbatim/keyword 召回锁。
三个测试各锁一层（与 gen_rag_goldset.py / eval_bank_rag.py 三方同源，
检索参数一致：exclude_topics=()、top_k=10——锁与尺子量的必须是同一件事）：
- schema 锁：金标文件与题库同步。增删题（bank_size/verbatim 一题一条）与
  原地改动（verbatim query=topic 原文、acceptable 按现行 tags 重算）都锁；
- recall@1 锁：verbatim+keyword 桶在 local 引擎上必须第一名命中。这是锁
  不是统计——金标出生时逐条自检过 rank-1，这里断言的是它保持命中：引擎
  换心/题库改写让它掉出第一名都该炸；
- paraphrase 桶：只报告不断言（真实泛化的数字在当前规模下是轶事），
  当前值见 scripts/data/bank_rag_eval_*.json。
"""
import json
from pathlib import Path

from interviewer.dataflows.glossary import load_glossary
from interviewer.dataflows.local_corpus import parse_corpus, search_corpus_local

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "corpus"
GOLDSET_PATH = REPO_ROOT / "scripts" / "data" / "bank_rag_goldset.json"


def _goldset() -> dict:
    return json.loads(GOLDSET_PATH.read_text(encoding="utf-8"))


def _rank(query: str, gold_id: str) -> int | None:
    hits = search_corpus_local(query, exclude_topics=(), top_k=10, corpus_dir=str(CORPUS_DIR))
    ids = [h.id for h in hits]
    return ids.index(gold_id) + 1 if gold_id in ids else None


def _expected_acceptable(gold_id: str, questions) -> list[str]:
    """独立重算 acceptable 规则（共享 ≥2 tag，解析顺序）。

    刻意复刻 gen_rag_goldset._acceptable_ids 而不是 import 它：锁的意义就是
    拿第二份实现互相对账——gen 的规则改了而这里没跟，本函数算出的期望值
    与金标失配 → schema 锁炸，强制两侧同批改。
    """
    gold_tags = {t.casefold() for t in next(q for q in questions if q.id == gold_id).tags}
    return [
        q.id for q in questions
        if q.id != gold_id and len(gold_tags & {t.casefold() for t in q.tags}) >= 2
    ]


def test_goldset_schema_lock():
    """金标与题库同步 + 条目形状四键锁死（三方合同：gen/eval/本测试）。"""
    questions = parse_corpus(CORPUS_DIR)
    bank = {q.id: q for q in questions}
    gs = _goldset()

    assert gs["bank_size"] == len(bank), (
        f"金标 bank_size={gs['bank_size']} ≠ 题库 {len(bank)} 题——"
        f"题库改动了，先重跑 scripts/gen_rag_goldset.py"
    )
    items = gs["items"]
    verbatim = [i for i in items if i["query_type"] == "verbatim"]
    assert len(verbatim) == len(bank), "verbatim 桶必须一题一条（少 = 生成规则坏了）"

    seen_verbatim = set()
    for it in items:
        assert set(it.keys()) == {"query", "gold_id", "acceptable_ids", "query_type"}
        assert it["query_type"] in {"verbatim", "keyword", "paraphrase", "offtopic", "synonym"}
        assert it["query"].strip(), "空 query 进了金标"
        if it["query_type"] == "offtopic":
        # 远域离题探针：无 gold、无 acceptable——判它的尺
            # 是 eval 的泄漏率（命中即泄漏），不是名次
            assert it["gold_id"] is None and it["acceptable_ids"] == []
            continue
        assert it["gold_id"] in bank, f"gold_id {it['gold_id']!r} 不在题库（先再生金标）"
        for aid in it["acceptable_ids"]:
            assert aid in bank and aid != it["gold_id"]
        if it["query_type"] == "verbatim":
            assert it["gold_id"] not in seen_verbatim, "verbatim 桶同一题出现两次"
            seen_verbatim.add(it["gold_id"])
            # 题库原地改 topic（不增删题、id 不变）时 bank_size/id 锁全看不见——
            # 这条是 verbatim 桶定义本身（query=topic 原文）的唯一守卫
            assert it["query"] == bank[it["gold_id"]].topic, (
                f"{it['gold_id']} 的 verbatim query 与题库 topic 失配"
                f"（{it['query']!r} ≠ {bank[it['gold_id']].topic!r}）——题库原地改动后需再生金标"
            )
        # 同理：tag 原地改动（C4 的 lint tag 归一就是这种形态）会让冻结的
        # acceptable_ids 失配、nDCG 按过期集合打分——按现行 tags 独立重算核对
        assert it["acceptable_ids"] == _expected_acceptable(it["gold_id"], questions), (
            f"{it['gold_id']} 的 acceptable_ids 与现行 tag 规则失配——题库 tag 改动后需再生金标"
        )


def test_recall_at_1_locked_for_verbatim_and_keyword():
    """锁：verbatim+keyword 桶在 local 上 recall@1 == 1.0（第一名命中）。"""
    for it in _goldset()["items"]:
        if it["query_type"] not in ("verbatim", "keyword"):
            continue
        rank = _rank(it["query"], it["gold_id"])
        assert rank == 1, (
            f"[{it['query_type']}] {it['gold_id']} 掉出第一名（rank={rank}）"
            f"｜query={it['query']!r}——引擎/题库改动造成回归，或金标需再生"
        )


def test_paraphrase_bucket_report_only():
    """paraphrase 桶只报告不断言（50 题规模下是轶事；200 题后再议阈值）。"""
    items = [i for i in _goldset()["items"] if i["query_type"] == "paraphrase"]
    assert items, "paraphrase 桶空了——gen 脚本的 PARAPHRASE_QUERIES 被清空？"

    hits1 = sum(1 for i in items if _rank(i["query"], i["gold_id"]) == 1)
    hits5 = sum(1 for i in items if (r := _rank(i["query"], i["gold_id"])) is not None and r <= 5)
    print(
        f"\nparaphrase 桶（只报告不断言）：recall@1={hits1}/{len(items)}"
        f"｜recall@5={hits5}/{len(items)}"
    )


def test_synonym_bucket_report_only():
    """synonym 桶 词表换面变体）只报告不断言——换面 miss 正是
    本桶要量的同义盲区，不是回归。完整性由 schema 锁的通用路径保证
（gold 在库 + acceptable 重算对账）；这里补桶身份检查：变体必须
    与某条 verbatim/keyword 同 gold（换面不换靶）。"""
    items = _goldset()["items"]
    syn = [i for i in items if i["query_type"] == "synonym"]
    assert syn, "synonym 桶空了——词表没生成任何变体（aliases 全空或桶被关？）"
    # 桶身份的强形式（2026-11-17 审查修正：原 "gold in source_golds" 是恒真式
    # ——verbatim 一题一条，source_golds 恒等于全库 id；mutation 把 210 条
    # gold 换错全套照绿）：每条变体必须能从**同 gold** 的源 query
    #（verbatim/keyword）经词表 variant_queries 复推出来——gold 换错、
    # 源换错、词表变体规则改了没再生金标，都在这里炸。
    gl = load_glossary(REPO_ROOT / "data" / "glossary.yaml")
    derivable: dict[str, set[str]] = {}
    for i in items:
        if i["query_type"] in ("verbatim", "keyword"):
            derivable.setdefault(i["gold_id"], set()).update(
                gl.variant_queries(i["query"]))
    for i in syn:
        assert i["query"] in derivable.get(i["gold_id"], ()), (
            f"变体 {i['query']!r} 无法从 gold={i['gold_id']} 的源 query 复推——"
            f"生成侧 gold/源被换错，或词表变体规则改动后未再生金标"
        )

    hits1 = sum(1 for i in syn if _rank(i["query"], i["gold_id"]) == 1)
    hits5 = sum(1 for i in syn if (r := _rank(i["query"], i["gold_id"])) is not None and r <= 5)
    print(
        f"\nsynonym 桶（只报告不断言）：recall@1={hits1}/{len(syn)}"
        f"｜recall@5={hits5}/{len(syn)}——同义盲区的量化，词表扩展的依据"
    )
