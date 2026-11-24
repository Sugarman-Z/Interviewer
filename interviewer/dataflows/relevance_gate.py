"""远域整单闸：查询对全库最高语义分低于阈值 → 命中整批转 NO_RESULTS_AVAILABLE 哨兵。

只闸整单不逐条——keyword 查询的 dense 绝对分天然低，逐条过滤会误伤 BM25 主场。
τ=0.38 校准于探针 40×金标 780（误伤 9/780）；近域相邻缺题不覆盖。
"""
GATE_MODEL = "jinaai/jina-embeddings-v2-base-zh"  # fastembed 不支持 bge-base-zh，用 base 档替身
GATE_MIN_COSINE = 0.38   # 题库/模型变更后需重校；曲线见 scripts/data/leak_gate_2x2_2026-11-18.json


def gate_max_cosine(query: str, corpus_dir: str = "./corpus") -> float | None:
    """查询对全库的最高 cosine；None = 闸不可用（依赖缺席/空库），调用方按
    fail-open 处理（不加闸）。只捕 ImportError，其余异常照炸；dense_corpus
    惰性导入，local-only 环境 import 本模块不炸。"""
    try:
        from interviewer.dataflows.dense_corpus import _get_index
        index = _get_index(corpus_dir, model_name=GATE_MODEL)
    except ImportError:
        return None
    if index.encoder is None or not index.questions:
        return None
    sims = index.matrix @ index.encoder.embed_query(query)
    return float(sims.max()) if sims.size else None


def should_gate_empty(query: str, corpus_dir: str = "./corpus") -> bool:
    """整单闸判定：True = 远域查询，命中应整批转空哨兵。"""
    s1 = gate_max_cosine(query, corpus_dir)
    return s1 is not None and s1 < GATE_MIN_COSINE
