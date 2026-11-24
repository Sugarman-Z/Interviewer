"""BM25F 内核测试。手算 fixture 全部算死——公式参数错了不是"分数低一点"，
是红。
"""
import math

import pytest

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.bm25f import (
    B,
    FIELD_WEIGHTS,
    K1,
    bm25f_score,
    build_stats,
    doc_fields,
    tokenize,
)


def _q(**over):
    base = dict(
        id="q1",
        topic="Kafka 高可用",
        text="三节点集群 leader 宕机",
        tags=["kafka", "消息队列"],
    )
    base.update(over)
    return BankQuestion(**base)


# tokenize 三规则

def test_tokenize_cjk_bigram():
    assert tokenize("高可用") == ["高可", "可用"]
    assert tokenize("宕机") == ["宕机"]               # 恰好一个 bigram


def test_tokenize_single_cjk_char_kept():
    assert tokenize("慢") == ["慢"]                   # 单字符段保留该字


def test_tokenize_latin_run_casefolded():
    assert tokenize("Kafka 集群") == ["kafka", "集群"]
    assert tokenize("ISR/acks CI/CD") == ["isr", "acks", "ci", "cd"]  # 标点切段


def test_tokenize_keeps_duplicates():
    """tokenize 本身不去重；去重契约在调用方（set）——query 侧唯一 term 求和。"""
    assert tokenize("测试 测试") == ["测试", "测试"]


def test_doc_fields_tags_space_joined():
    """tags 空格拼接：拼接缝不产跨 tag 伪 bigram（"队缓"类永不出现）。"""
    f = doc_fields(_q())
    assert f["tags"] == "kafka 消息队列"
    assert tokenize(f["tags"]) == ["kafka", "消息", "息队", "队列"]


# build_stats：df 文档级 + avgdl guard

def test_build_stats_df_is_document_level():
    """df 量"多少题提到过这个词"：term 出现在任一字段该题计 1 次。"""
    stats = build_stats([
        _q(),
        _q(id="q2", topic="缓存击穿", text="热点 key 过期", tags=["redis"]),
    ])
    assert stats["n"] == 2
    assert stats["df"]["kafka"] == 1                  # 只在 q1 的 tags
    assert stats["df"]["集群"] == 1                   # 只在 q1 的 text
    assert stats["avgdl"]["text"] > 0


def test_build_stats_avgdl_zero_no_crash():
    """除零守卫：全库 tags 皆空 → avgdl=0，长度比取 0（sat(0)=0），不炸——
    其他字段的命中照常得分（不被空字段的 0 avgdl 连坐）。"""
    stats = build_stats([_q(tags=[]), _q(id="q2", topic="缓存", text="穿透", tags=[])])
    assert stats["avgdl"]["tags"] == 0
    s = bm25f_score({"kafka"}, stats["docs"]["q1"], stats)   # kafka 命中 topic
    assert s > 0


# bm25f_score：手算锁死

def test_score_text_field_hand_computed():
    """手算①："宕机"只出现在 text（tf=1，len=avgdl → 长度比 1）。"""
    stats = build_stats([_q()])
    s = bm25f_score({"宕机"}, stats["docs"]["q1"], stats)
    idf = math.log((1 - 1 + 0.5) / (1 + 0.5) + 1)     # = ln(4/3) ≈ 0.2877
    sat_text = 1.0 * 1.0 * (K1 + 1) / (1.0 * 1.0 + K1 * (1 - B + B * 1.0))  # = 2.5/2.5
    assert s == pytest.approx(idf * sat_text, rel=1e-3)


def test_score_topic_field_hand_computed():
    """手算②："高可"只出现在 topic（tf=1，len=avgdl=3 → 长度比 1）——
    字段权重进饱和函数内：sat = W·tf·(k1+1) / (W·tf + k1·norm)。"""
    stats = build_stats([_q()])
    s = bm25f_score({"高可"}, stats["docs"]["q1"], stats)
    idf = math.log(4 / 3)                             # df=1, N=1
    w = FIELD_WEIGHTS["topic"]
    sat_topic = w * 1.0 * (K1 + 1) / (w * 1.0 + K1 * 1.0)   # = 7.5/4.5 ≈ 1.6667
    assert s == pytest.approx(idf * sat_topic, rel=1e-3)


def test_field_weight_ordering():
    """判别用例：同一 term 分别只出现在 topic / tags / text 时，得分
    topic > tags > text（字段权重 3/2/1 的可见效果）。三题各字段等长
（长度比全为 1）——否则长度归一会混进对比：tags 比平均长 3 倍的
    题会被罚到 text 命中之下（本测试初版正是这么红的，那是归一在干活）。"""
    q_topic = _q(id="a", topic="雪崩", text="甲", tags=["备用"])
    q_tags = _q(id="b", topic="乙", text="丙", tags=["雪崩"])
    q_text = _q(id="c", topic="丁", text="雪崩", tags=["备用"])
    stats = build_stats([q_topic, q_tags, q_text])
    s = {qid: bm25f_score({"雪崩"}, stats["docs"][qid], stats) for qid in ("a", "b", "c")}
    assert s["a"] > s["b"] > s["c"]


def test_params_locked():
    """参数锁：50 题上扫参 = overfitting 表演，k1/b/权重锁死不动（TODO 定案）。"""
    assert K1 == 1.5 and B == 0.75
    assert FIELD_WEIGHTS == {"topic": 3.0, "tags": 2.0, "text": 1.0}
