"""第二检索面 vendor 单测：段落切块的区间合同 + 单字段 BM25。
区间合同是本面的溯源根基（source_ref 可核验 = 拿 start:end 切原文必得
段落原文）——所以第一条测试就是逐块回切对账，而不是先测检索。
"""
import pytest

from interviewer.dataflows.bm25f import tokenize  # noqa: F401 —— 锁同一把切词尺
from interviewer.dataflows.candidate_corpus import (
    chunk_texts,
    search_candidate_local,
)
from interviewer.dataflows.interface import CANDIDATE_TOOL_NAME, get_vendor_chain

_RESUME = """工作经历
FNZ Analyst Developer（2023-2027）

项目经验
负责交易系统性能优化，把订单处理延迟从 50ms 降到 8ms。
主导缓存改造，用 Redis 缓存热点行情数据。

教育背景
GWU 计算机硕士"""

_JD = """岗位要求
熟悉 Kafka 等消息队列。

加分项
有低延迟系统经验。"""

_TEXTS = {"resume": _RESUME, "jd": _JD}


# 切块：区间合同与编号

def test_chunk_offsets_slice_to_verbatim_text():
    """区间合同：full_text[start:end] == chunk.text（strip 只削两端，段落
    正文必是原文连续切片——source_ref 的可核验性全押在这条上）。"""
    for source, full in _TEXTS.items():
        for c in chunk_texts(_TEXTS):
            if c.source == source:
                assert full[c.start:c.end] == c.text


def test_chunk_ids_sequential_and_para_labels():
    """编号跨源连续（resume 在前 jd 在后），段序号按源内独立计数。"""
    chunks = chunk_texts(_TEXTS)
    assert [c.id for c in chunks] == [f"cand-{i:04d}" for i in range(1, 6)]
    assert [(c.source, c.para) for c in chunks] == [
        ("resume", 1), ("resume", 2), ("resume", 3), ("jd", 1), ("jd", 2),
    ]


def test_single_newlines_stay_one_paragraph():
    """段内单换行不拆段（空行才是边界）：简历的 bullet 群是一段证据。"""
    first = chunk_texts(_TEXTS)[0]
    assert "工作经历" in first.text and "FNZ" in first.text


def test_empty_and_whitespace_texts_yield_no_chunks():
    assert chunk_texts({}) == []
    assert chunk_texts({"resume": "", "jd": "\n\n   \n\n"}) == []


def test_crlf_text_interval_contract():
    """CRLF 区间合同（2026-11-17 审查补锁）：Windows 简历带 \\r\\n 直达
    切块器（file_loader 字节读不翻译换行，与题库面文本读不对称）——段内
    \\r\\n 原样保留、区间回切逐字节成立；"先归一 \\r 再切"式重构在此当场红
（偏移静默漂移 = source_ref 可核验性失效）。"""
    t = "工作经历\r\nFNZ Analyst\r\n\r\n项目经验\r\n负责性能优化，延迟 50ms 降到 8ms"
    chunks = chunk_texts({"resume": t})
    assert len(chunks) == 2
    for c in chunks:
        assert t[c.start:c.end] == c.text


def test_blank_query_returns_empty():
    """空/全空白 query：tokenize 零词块 → 全部零分 → 空列表（工具层自然
    得哨兵——candidate_tool docstring 明文设计的行为，补锁）。"""
    assert search_candidate_local("", texts=_TEXTS) == []
    assert search_candidate_local("   \n  ", texts=_TEXTS) == []


def test_single_side_texts_ok():
    """texts 只有一侧（jd 缺键）：缺键 = 空文本，另一侧正常出块。"""
    chunks = chunk_texts({"resume": _RESUME})
    assert chunks and all(c.source == "resume" for c in chunks)


# 检索：排序 / 零分不进榜 / exclude / 截断

def test_bm25_single_field_handcomputed():
    """公式手算锁（test_bm25f 同款纪律，2026-11-17 审查补——此前零数值
    断言，公式抄错全绿）：n=2、df=1、avgdl=3——
    tf=1/dl=3：饱和项 = 2.5/(1+1.5×1) = 1.0，得分 = ln(2) = 0.6931471805599453
    tf=2/dl=6：饱和项 = 5/(2+1.5×1.75) = 5/4.625，得分 = ln(2)×5/4.625 ≈ 0.749348
    词块不在段里 → 0 分。"""
    from collections import Counter

    from interviewer.dataflows import candidate_corpus as cc

    assert cc.K1 == 1.5 and cc.B == 0.75          # 与题库面同一套常量（数值锁）
    q, df, n = {"性能"}, Counter({"性能": 1}), 2
    s1 = cc._bm25_single_field(q, Counter({"性能": 1}), df, n, dl=3, avgdl=3.0)
    assert s1 == pytest.approx(0.6931471805599453, rel=1e-6)
    s2 = cc._bm25_single_field(q, Counter({"性能": 2}), df, n, dl=6, avgdl=3.0)
    assert s2 == pytest.approx(0.749348, rel=1e-5)
    miss = cc._bm25_single_field(q, Counter({"缓存": 1}), df, n, dl=3, avgdl=3.0)
    assert miss == 0.0


def test_search_ranks_relevant_paragraph_first():
    hits = search_candidate_local("性能优化 延迟", texts=_TEXTS)
    assert hits and hits[0].id == "cand-0002"
    assert "性能优化" in hits[0].text


def test_search_no_overlap_returns_empty():
    """零分不进榜：query 与全部段落无词块交集 → 空列表（哨兵的原料）。"""
    assert search_candidate_local("量子纠缠", texts=_TEXTS) == []


def test_search_exclude_ids():
    hits = search_candidate_local("性能优化", texts=_TEXTS)
    assert [h.id for h in hits] == ["cand-0002"]
    # 唯一命中被排除 → 空（同 query 无第二落点）
    assert search_candidate_local("性能优化", texts=_TEXTS,
                                  exclude_ids=("cand-0002",)) == []


def test_search_top_k_truncates():
    assert len(search_candidate_local("系统", texts=_TEXTS, top_k=1)) == 1


def test_search_is_deterministic():
    q = "缓存 改造"
    assert (search_candidate_local(q, texts=_TEXTS)
            == search_candidate_local(q, texts=_TEXTS))


# 注册表路由

def test_registered_under_candidate_method():
    """'search_candidate' 方法经注册表可解析到本函数（组装入口零改动可用）。"""
    assert get_vendor_chain(CANDIDATE_TOOL_NAME, "local")[0] is search_candidate_local


def test_tokenize_shared_with_bank_face():
    """两侧同一把切词尺：同一个 bigram 在两面的 token 形状一致（中英混排
    各自成段、汉字 bigram、拉丁整词小写）。"""
    assert tokenize("性能优化 Kafka") == ["性能", "能优", "优化", "kafka"]
