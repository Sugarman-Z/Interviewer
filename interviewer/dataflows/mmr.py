"""组卷多样性：贪心 MMR，从检索池选覆盖不同考点的 top-k（公式见 mmr_select）。

题向量逐 vendor 探测 question_vectors，缺席/异常则原序截断返回（fail-open）。
错题本复考要"同考点相似"的题，届时关 bank_mmr。依赖 numpy，bank_tool 惰性导入。
"""
import logging

import numpy as np

from interviewer.dataflows.dense_corpus import unit_vec
from interviewer.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

MMR_LAMBDA = 0.7   # 相关:多样 = 7:3；调整需 coverage/recall 数据支撑，test_mmr 数值锁定
MMR_POOL_N = 20    # 组卷池深：先检索 top-20 再选 top-k；池浅于 top-k 时 MMR 没得选


def mmr_select(query_vec, cand_ids: list[str], cand_vecs: dict,
               *, top_k: int, lam: float = MMR_LAMBDA) -> list[str]:
    """贪心 MMR 选 top_k 个 id（纯函数，test_mmr 手算锁定公式）。

    cand_ids 输入序即平票决胜序（严格大于才换人，同分先到先得）。
    cand_vecs 缺 id → KeyError 原样穿出，由调用方按 fail-open 降级。
    """
    if top_k <= 0 or not cand_ids:
        return []
    qv = unit_vec(query_vec)
    units = {i: unit_vec(cand_vecs[i]) for i in cand_ids}
    rel = {i: float(qv @ units[i]) for i in cand_ids}
    selected: list[str] = []
    remaining = list(cand_ids)
    while remaining and len(selected) < top_k:
        best, best_score = None, None
        for i in remaining:
            penalty = max((float(units[i] @ units[s]) for s in selected), default=0.0)
            score = lam * rel[i] - (1.0 - lam) * penalty
            if best_score is None or score > best_score:
                best, best_score = i, score
        selected.append(best)
        remaining.remove(best)
    return selected


def diversify_hits(query: str, hits: list, *, corpus_dir: str, vendors: str,
                   top_k: int, lam: float = MMR_LAMBDA) -> list:
    """组卷出口：检索池 hits → MMR → 恰好 top_k 条。

    题向量逐 vendor 探测 question_vectors，首个注册的腿答，混合链不被
    未注册的腿拖累。缺席/异常一律原序截断返回——上游为开 MMR 放大了
    取题深度，必须交回恰好 top_k 条。
    """
    ids = [h.id for h in hits]
    for name in (v.strip() for v in vendors.split(",")):
        if not name:
            continue
        try:
            qv, vecs = route_to_vendor(
                "question_vectors", name, query, ids=ids, corpus_dir=corpus_dir)
            order = mmr_select(qv, ids, vecs, top_k=top_k, lam=lam)
            by_id = {h.id: h for h in hits}
            return [by_id[i] for i in order]
        except Exception:  # noqa: BLE001 多样性缺席可降级，检索本体不在此路径
            logger.debug("question_vectors 路由失败（vendor=%s），组卷回落检索原序", name)
            continue
    return hits[:top_k]
