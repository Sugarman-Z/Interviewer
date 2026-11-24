"""金标评测集生成：规则反推 → scripts/data/bank_rag_goldset.json。

五桶设计（难度分层，混在一起测出的数字谁也解释不了）：
- verbatim：query = topic 原文，测召回下限（引擎连原文都丢是坏了，不是泛化差）；
- keyword：tags/主题词的确定性组合，模拟出题模型真实会发的关键词式查询；
  出生时自检 local rank-1，不达标按候选梯换档（见 _keyword_candidates）；
- paraphrase：人工改写的自然语言问句（PARAPHRASE_QUERIES 常量），测真实
  泛化；只报告不断言（旧子串引擎大面积 miss 曾是换 BM25F 的动机数字）；
- offtopic：远域离题探针（OFFTOPIC_PROBES）——无 gold，任何引擎命中即
  泄漏；dense 阈值漂移导致的哨兵击穿在这里先红。只收远域不收技术相邻
（docker/git 会过线且属已知取舍）；
- synonym：verbatim/keyword 经 data/glossary.yaml 换面生成的变体，gold 不变；
  无 rank-1 出生自检——换面后 miss 正是本桶要量的同义盲区，只报告不断言。

金标条目形状（与 eval_bank_rag.py / tests/test_rag_regression.py 三方合同）：
    {"query": str, "gold_id": str, "acceptable_ids": [str], "query_type": str}
acceptable_ids 规则（确定性）：与 gold 分享 ≥2 个 tag 的其他题（nDCG 增益 1；
gold=3、无关=0）。再生：题库改动后重跑即可（verbatim/keyword 零人工）；
paraphrase 桶是人工的，gold_id 失配当场炸。运行：
PYTHONUTF8=1 python scripts/gen_rag_goldset.py [--corpus ./corpus]
"""
import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from interviewer.dataflows.local_corpus import parse_corpus, search_corpus_local

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# 人工改写桶（测真实泛化；改写措辞别与 topic 原文雷同，否则测的还是循环）。
# 想换/增条目直接改这里——gold_id 与题库失配会在生成时 fail-loud。
PARAPHRASE_QUERIES: list[tuple[str, str]] = [
    ("leader 宕机了怎么保证已确认的消息不丢", "bank-kafka-001"),
    ("先更新数据库还是先删缓存，为什么", "bank-cache-001"),
    ("热点 key 过期瞬间大量请求打到数据库怎么办", "bank-cache-002"),
    ("多轮对话的字段该放图状态还是消息列表", "bank-agent-001"),
    ("agent 跑一半停下来等用户确认再继续怎么做", "bank-agent-003"),
    ("中文检索为什么不能直接按空格分词", "bank-agent-006"),
    ("关键词和向量两路检索的分数怎么合并", "bank-agent-007"),
    ("怎么防止模型谎报题库来源", "bank-agent-009"),
    ("mysql 可重复读下幻读解决了吗", "bank-be-001"),
    ("并发扣库存该用悲观锁还是乐观锁", "bank-be-004"),
    ("多个实例怎么保证定时任务只跑一份", "bank-be-005"),
    ("支付接口被重复调用怎么保证只扣一次钱", "bank-be-008"),
    ("消费组积压了几百万条消息怎么处理", "bank-be-012"),
    ("首屏加载太慢怎么排查优化", "bank-fs-003"),
    ("动画每帧改宽度为什么卡", "bank-fs-004"),
    ("自动化测试各层怎么配比", "bank-fs-008"),
    ("讲一次你处理的线上故障", "bank-proj-003"),
    ("用户反馈出的题不贴简历怎么排查", "bank-agent-016"),
]

# 远域离题探针（offtopic 桶；无 gold）。措辞纪律：全部离 CS/工程远域——
# 技术相邻的题外查询（docker、git rebase 等）实测会过 0.50 线，属于
# DENSE_MIN_COSINE 注释里记录的已知取舍，不进锁。
OFFTOPIC_PROBES: list[str] = [
    "量子纠缠是怎么回事",
    "莫扎特的歌剧有哪些",
    "红烧肉怎么做好吃",
    "光合作用的原理",
    "文艺复兴时期的绘画特点",
    "足球越位规则",
]


def _acceptable_ids(gold, questions) -> list[str]:
    """与 gold 分享 ≥2 个 tag 的其他题（解析顺序，确定性输出）。
    tests/test_rag_regression._expected_acceptable 是本规则的镜像锁——改这里
    必须同批改那边（不同批 = schema 锁炸，这正是锁的用途）。"""
    gold_tags = {t.casefold() for t in gold.tags}
    out = []
    for q in questions:
        if q.id == gold.id:
            continue
        shared = gold_tags & {t.casefold() for t in q.tags}
        if len(shared) >= 2:
            out.append(q.id)
    return out


def _keyword_candidates(q) -> list[str]:
    """keyword 桶候选梯（从宽到严，第一个被 local 判 rank-1 的胜出）。
    为什么是梯不是单条规则：单看 tags 可能撞不出唯一性（同 tag 兄弟题平分
    后难度高的反超），需逐级加主题尾词收窄。规则全部确定性。"""
    topic_tokens = q.topic.split()
    topic_tail = topic_tokens[-1] if topic_tokens else ""
    topic_longest = max(topic_tokens, key=len) if topic_tokens else ""
    cands = []
    if q.tags:
        cands.append(" ".join(q.tags))
    if q.tags and topic_tail:
        cands.append(f"{q.tags[0]} {topic_tail}")
        cands.append(f"{' '.join(q.tags)} {topic_tail}")
    if topic_longest:
        cands.append(topic_longest)
    seen, out = set(), []
    for c in cands:
        # 与 topic 原文雷同的候选不算 keyword（那是 verbatim 桶的事）
        if c.casefold() != q.topic.casefold() and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _rank_of(query: str, gold_id: str, corpus_dir: str) -> int | None:
    """local 引擎下 gold 的名次（None=不在 top-10）。gen 与回归锁用同一
    参数（exclude_topics=()、top_k=10），保证"出生时 rank-1"与"锁断言
    rank-1"量的是同一件事。"""
    hits = search_corpus_local(query, exclude_topics=(), top_k=10, corpus_dir=corpus_dir)
    ids = [h.id for h in hits]
    return ids.index(gold_id) + 1 if gold_id in ids else None


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 RAG 金标评测集（四桶规则反推+人工桶）")
    ap.add_argument("--corpus", default=str(REPO_ROOT / "corpus"))
    ap.add_argument("--out", default=str(SCRIPT_DIR / "data" / "bank_rag_goldset.json"))
    ap.add_argument("--glossary", default=str(REPO_ROOT / "data" / "glossary.yaml"),
                    help="术语词表（synonym 桶的原料；传空串跳过 synonym 桶）")
    args = ap.parse_args()

    glossary = None
    if args.glossary:
        from interviewer.dataflows.glossary import load_glossary
        glossary = load_glossary(args.glossary)   # 坏表 fail-loud（对账基准不许半载）

    questions = parse_corpus(args.corpus)
    if not questions:
        print(f"题库为空：{args.corpus}——金标无从反推，先入库再跑", file=sys.stderr)
        return 1
    by_id = {q.id: q for q in questions}

    items = []
    ladder_usage: Counter[str] = Counter()
    unsolved: list[tuple[str, str]] = []  # (gold_id, 最好成绩描述)

    for q in questions:
        # verbatim：topic 原文，一题一条
        rank = _rank_of(q.topic, q.id, args.corpus)
        if rank != 1:
            # 召回下限破了：引擎连自己的 topic 原文都排不到第一，后面全没意义
            print(f"verbatim 自检失败：{q.id}｜topic={q.topic!r}｜rank={rank}", file=sys.stderr)
            return 1
        items.append({
            "query": q.topic,
            "gold_id": q.id,
            "acceptable_ids": _acceptable_ids(q, questions),
            "query_type": "verbatim",
        })

        # keyword：候选梯 + rank-1 自检
        best = None
        for i, cand in enumerate(_keyword_candidates(q), start=1):
            if _rank_of(cand, q.id, args.corpus) == 1:
                best = (f"rule-{i}", cand)
                break
        if best is None:
            # 不硬塞：塞进去 = 回归锁出生即红。大声报告，让 C3/C4 接手
            cands = _keyword_candidates(q)
            detail = "; ".join(f"{c!r}→rank {_rank_of(c, q.id, args.corpus)}" for c in cands) or "（无候选：tags 与 topic 均空）"
            unsolved.append((q.id, detail))
        else:
            ladder_usage[best[0]] += 1
            items.append({
                "query": best[1],
                "gold_id": q.id,
                "acceptable_ids": _acceptable_ids(q, questions),
                "query_type": "keyword",
            })

    # paraphrase：人工改写，gold 失配当场炸
    for query, gold_id in PARAPHRASE_QUERIES:
        if gold_id not in by_id:
            print(
                f"paraphrase 金标失配：{gold_id!r} 不在题库里（题库改动了？改 PARAPHRASE_QUERIES）",
                file=sys.stderr,
            )
            return 1
        items.append({
            "query": query,
            "gold_id": gold_id,
            "acceptable_ids": _acceptable_ids(by_id[gold_id], questions),
            "query_type": "paraphrase",
        })

    # offtopic：远域离题探针（无 gold，命中即泄漏；eval 判决用）
    for query in OFFTOPIC_PROBES:
        items.append({
            "query": query,
            "gold_id": None,
            "acceptable_ids": [],
            "query_type": "offtopic",
        })

    # synonym：词表换面变体，只对有 gold 的前两桶生成）
    # 变体与原 query 同 gold/acceptable；跨桶去重（verbatim 的变体可能
    # 恰好等于别题的 keyword query——那是别题自己的条目，不重复收）。
    if glossary is not None:
        seen_queries = {i["query"].casefold() for i in items}
        for it in [i for i in items if i["query_type"] in ("verbatim", "keyword")]:
            for v in glossary.variant_queries(it["query"]):
                if v.casefold() in seen_queries:
                    continue
                seen_queries.add(v.casefold())
                items.append({
                    "query": v,
                    "gold_id": it["gold_id"],
                    "acceptable_ids": it["acceptable_ids"],
                    "query_type": "synonym",
                })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": date.today().isoformat(),
        "bank_size": len(questions),
        "buckets": {b: sum(1 for i in items if i["query_type"] == b)
                    for b in ("verbatim", "keyword", "paraphrase", "offtopic", "synonym")},
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"题库 {len(questions)} 题 → 金标 {len(items)} 条 → {out}")
    print(f"分桶：{payload['buckets']}")
    print(f"keyword 候选梯用量：{dict(ladder_usage)}")
    if glossary is None:
        print("synonym 桶未生成（--glossary 传空串）")
    if unsolved:
        print(f"\n⚠ {len(unsolved)} 题的 keyword 查询 local 排不到第一，已剔除出金标（C3 换心动机）：")
        for qid, detail in unsolved:
            print(f"  {qid}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
