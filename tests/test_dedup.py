""" 查重 v1.5 单测：整题向量近重复剔除 + 降级契约。

向量方法用 stub 注册（plain list 即可——dedup 侧 unit_vec 接受 array-like），
真模型的分布校准归 dedup.py 头注释记录的实测，不进 pytest。
"""
import math

import pytest

pytest.importorskip("numpy")   # dense 组可选：不装 numpy/fastembed 整文件跳过

from interviewer.dataflows.dedup import DEDUP_MIN_COSINE, filter_near_duplicates
from interviewer.dataflows.bank_schema import BankQuestion

U0, U1 = [1.0, 0.0], [0.0, 1.0]     # 一对正交单位向量


def _q(qid: str, topic: str) -> BankQuestion:
    return BankQuestion(id=qid, topic=topic, text=f"题面{qid}")


def _register_vectors(monkeypatch, mapping):
    """把 vectors_by_topic stub 注册到 local 名下（monkeypatch 收尾还原）。"""
    from interviewer.dataflows import interface

    def _vbt(topics, *, corpus_dir="./corpus"):
        return {t: mapping[t] for t in topics if t in mapping}

    monkeypatch.setitem(
        interface._VENDOR_METHODS.setdefault("vectors_by_topic", {}), "local", _vbt)


def test_near_duplicate_dropped_far_kept(monkeypatch, tmp_path):
    """已考"Kafka 高可用"（U0）：同向的换皮题剔除，正交的不同题保留。"""
    _register_vectors(monkeypatch, {
        "Kafka 高可用": U0, "Kafka 的高可用": U0, "缓存击穿": U1})
    hits = [_q("bank-a", "Kafka 的高可用"), _q("bank-b", "缓存击穿")]
    out = filter_near_duplicates(hits, ("Kafka 高可用",),
                                 corpus_dir=str(tmp_path), vendors="local")
    assert [h.id for h in out] == ["bank-b"]


def test_threshold_boundary(monkeypatch, tmp_path):
    """边界：cos 恰好 ≥ 阈值算换皮，差 0.01 不算。阈值本身锁死 0.90——
    改它必须带新的分布数据（dedup.py 头注释的校准记录）。"""
    assert DEDUP_MIN_COSINE == 0.90
    hi = [DEDUP_MIN_COSINE, math.sqrt(1.0 - DEDUP_MIN_COSINE ** 2)]
    lo = [DEDUP_MIN_COSINE - 0.01, math.sqrt(1.0 - (DEDUP_MIN_COSINE - 0.01) ** 2)]
    _register_vectors(monkeypatch, {"已考": U0, "线上": hi, "线下": lo})
    hits = [_q("bank-hi", "线上"), _q("bank-lo", "线下")]
    out = filter_near_duplicates(hits, ("已考",),
                                 corpus_dir=str(tmp_path), vendors="local")
    assert [h.id for h in out] == ["bank-lo"]


def test_empty_issued_is_free_pass(monkeypatch, tmp_path):
    """exclude 空（首题）→ 零嵌入成本直通：向量方法都不该被摸到。"""
    calls = []
    from interviewer.dataflows import interface

    def _spy(topics, *, corpus_dir="./corpus"):
        calls.append(topics)
        return {}

    monkeypatch.setitem(
        interface._VENDOR_METHODS.setdefault("vectors_by_topic", {}), "local", _spy)
    hits = [_q("bank-a", "任意")]
    assert filter_near_duplicates(hits, (), corpus_dir=str(tmp_path), vendors="local") == hits
    assert calls == []


def test_unmappable_issued_skips_vendor(monkeypatch, tmp_path):
    """已考考点映射不回索引（generated 自创串）→ 该腿答不了，原样返回——
    v1 精确匹配仍在 vendor 层兜底，v1.5 缺席不降级成误删。"""
    _register_vectors(monkeypatch, {"缓存击穿": U1})
    hits = [_q("bank-a", "缓存击穿")]
    out = filter_near_duplicates(hits, ("模型自创考点",),
                                 corpus_dir=str(tmp_path), vendors="local")
    assert out == hits


def test_no_provider_fail_open(tmp_path):
    """vectors_by_topic 全链未注册（local-only 生产链的常态）→ 原样返回。"""
    hits = [_q("bank-a", "任意")]
    out = filter_near_duplicates(hits, ("已考",),
                                 corpus_dir=str(tmp_path), vendors="local")
    assert out == hits
