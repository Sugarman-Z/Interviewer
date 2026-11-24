"""查重 v1.5：整题向量的近重复剔除——"同题换皮"进不了候选面。
v1（vendor 层 exclude_topics）是考点全名精确匹配，任何改写都绕得过；
v1.5 用整题向量补：命中题与任一已考题 cosine ≥ DEDUP_MIN_COSINE → 视为
同一题的换皮，从候选剔除（空位由池内后续补位，取题深度已放大到池深）。

校准教训（真模型实测，bge-small，200 题库）：topic 字符串嵌入不可用——
超短文本上编码退化成"结构向量"，"Redo 与 Binlog"↔"XSS 与 CSRF" cosine
1.000 而真改写对只有 0.811，两坨分布重叠，不存在能分开的阈值。整题向量
（topic+tags+题面+追问树，与索引同口径）分布健康：200 题全对 max=0.833、
p99=0.710。阈值定 0.90：高于全库 max 一档余量、低于同题换皮的必然区间
（同一道题两种措辞 cosine 0.95+）——给题库翻倍与考点改写 dodge 当安全
网，当前 200 题 0 触发。

诚实边界：映射不回索引行的自创考点 → 本层跳过，靠 v1 兜底；0.83-0.90
区间的"相关不同题"不该被抓（过度合并会把两个合法考点错当一题）。
依赖 numpy（dense 可选组）；被 bank_tool 惰性导入，同 mmr.py。
"""
import logging

import numpy as np

from interviewer.dataflows.dense_corpus import unit_vec
from interviewer.dataflows.interface import route_to_vendor

logger = logging.getLogger(__name__)

DEDUP_MIN_COSINE = 0.90   # 校准记录见模块头；改它必须带新的分布数据
                          #（test_dedup 的边界测试锁死 0.90）


def filter_near_duplicates(hits: list, exclude_topics, *,
                           corpus_dir: str, vendors: str) -> list:
    """剔除与已考考点近重复的命中（题向量经 vectors_by_topic 逐 vendor
    探测：dense 注册、hybrid 代答、local 链查不到即降级）。fail-open：
    方法缺席/腿异常 → 原样返回（回落精确匹配 v1）。exclude_topics 空 →
    零成本直通；自创考点映射不回索引 → 只比对映射得上的部分。"""
    issued = [t for t in dict.fromkeys(exclude_topics) if t]   # 去重保序
    if not issued or not hits:
        return hits
    cand_topics = [h.topic for h in hits]
    for name in (v.strip() for v in vendors.split(",")):
        if not name:
            continue
        try:
            base = route_to_vendor("vectors_by_topic", name, issued, corpus_dir=corpus_dir)
            cand = route_to_vendor("vectors_by_topic", name, cand_topics, corpus_dir=corpus_dir)
        except Exception:  # noqa: BLE001 —— 查重增强缺席可降级；检索本体不在这条路径上
            logger.debug("vectors_by_topic 路由失败（vendor=%s），查重回落精确匹配", name)
            continue
        base_vecs = [unit_vec(v) for v in base.values()]
        if not base_vecs:
            continue   # 已考串全映射不回索引：该腿无能为力，试下家
        keep, dropped = [], []
        for h in hits:
            cv = cand.get(h.topic)
            if cv is None:
                keep.append(h)   # 命中映射不回向量（罕见）：不该因缺向量被丢
                continue
            u = unit_vec(cv)
            if any(float(bv @ u) >= DEDUP_MIN_COSINE for bv in base_vecs):
                dropped.append(h)
            else:
                keep.append(h)
        if dropped:
            logger.info("查重 v1.5 剔除 %d 条近重复命中：%s",
                        len(dropped), [h.id for h in dropped])
        return keep
    return hits
