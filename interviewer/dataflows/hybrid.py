"""混合检索 vendor：BM25F 腿 + 稠密腿 → RRF 融合。

融合用 RRF 不用分数相加：两条腿的分数不在一个世界（BM25F 是 idf 加权和、
无上界；cosine 有界 [-1,1]），直接加权求和要对每对引擎调权重，换任何
一个模型就得重调。RRF（Reciprocal Rank Fusion）只消费名次：

    score(d) = Σ_legs  1 / (k + rank_d)      k = 60（原论文值）

名次天然免疫各腿的分数尺度与膨胀（BM25F 涨 10 倍，名次不变）；k=60 压住
第 1 名与第 2 名的差距，让"出现在两条腿"比"单腿第一"更值钱——字面命中
和语义命中互相补位、共同背书。参数锁（同 bm25f 纪律）：RRF_K / LEG_TOP_N
是常量，调它们 = 对着考卷改答案。
"""
from interviewer.dataflows.dense_corpus import (
    question_vectors,
    search_corpus_dense,
    vectors_by_topic,
)
from interviewer.dataflows.interface import register_vendor
from interviewer.dataflows.local_corpus import search_corpus_local

RRF_K = 60          # RRF 平滑常数（原论文值）
LEG_TOP_N = 20      # 每腿进融合的名次深度（TODO 定案：两腿各 top-20）


def rrf_fuse(ranked_ids: list[list[str]], top_k: int, k: int = RRF_K) -> list[tuple[str, float]]:
    """多路"已排序的 id 列表" → RRF 融合排名 [(id, score)]，截 top_k。

    纯函数（test_hybrid 手算锁死公式）。平票按首次出现顺序（第几条腿的
    第几名）——dict 保持插入序 + sorted 稳定排序，同一输入永远同一输出。
    """
    scores: dict[str, float] = {}
    for ranking in ranked_ids:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]


def search_corpus_hybrid(query, *, exclude_topics=(), top_k=5,
                         corpus_dir="./corpus") -> list:
    """hybrid vendor：两腿各取 top-max(LEG_TOP_N, top_k) → RRF → top_k。
    腿抛异常不吞：fastembed 没装、模型下载失败这类事交给 config 链的回退
（route_to_vendor 的职责）。两腿全空 → 融合结果空 → 上层空哨兵照常
    工作（稠密腿的阈值截断先保证"腿空 = 真没有语义相关"）。"""
    fetch_k = max(LEG_TOP_N, top_k)
    legs = [
        search_corpus_local(query, exclude_topics=exclude_topics,
                            top_k=fetch_k, corpus_dir=corpus_dir),
        search_corpus_dense(query, exclude_topics=exclude_topics,
                            top_k=fetch_k, corpus_dir=corpus_dir),
    ]
    fused = rrf_fuse([[h.id for h in leg] for leg in legs], top_k=top_k)
    by_id = {h.id: h for leg in legs for h in leg}
    return [by_id[doc_id] for doc_id, _ in fused]


register_vendor("search_corpus", "hybrid", search_corpus_hybrid)
# 向量面代答：hybrid 的 dense 腿有题向量，组卷层（MMR/查重）
# 按"逐 vendor 探测"找 question_vectors/vectors_by_topic——链串 "hybrid,local"
# 里没有 "dense" 这个名字，不代答的话最主流的混合链永远探测不到向量方法
#（corpus_topics 防的连坐问题在这里换了个形态重现）。
register_vendor("question_vectors", "hybrid", question_vectors)
register_vendor("vectors_by_topic", "hybrid", vectors_by_topic)
