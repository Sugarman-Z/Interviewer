"""组卷多样性单测：MMR 公式手算锁死 + diversify_hits 组装契约。
手算 fixture（单位向量，2 维足够判别）：
    q  = [1, 0]
    A  = [0.95, 0.3122]    rel=0.95 —— 检索第 1 名
    A2 = [0.93, 0.3676]    rel=0.93，sim(A,A2)≈0.998 —— A 的近重复，检索第 2 名
    B  = [0.91, -0.4146]   rel=0.91，sim(A,B)≈0.735 —— 不同方向，检索第 3 名
λ=0.7 第二轮：A2 = 0.7×0.93 − 0.3×0.998 ≈ 0.3515；
              B  = 0.7×0.91 − 0.3×0.735 ≈ 0.4165 —— B 胜出（近重复被换下）。
"""
import pytest

pytest.importorskip("numpy")   # dense 组可选：不装 numpy/fastembed 整文件跳过
import numpy as np

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.mmr import (
    MMR_LAMBDA,
    MMR_POOL_N,
    diversify_hits,
    mmr_select,
)

Q = [1.0, 0.0]
A = [0.95, 0.3122]
A2 = [0.93, 0.3676]
B = [0.91, -0.4146]
VECS = {"a": A, "a2": A2, "b": B}


def _q(qid: str) -> BankQuestion:
    return BankQuestion(id=qid, topic=f"考点{qid}", text=f"题面{qid}")


# mmr_select：公式与确定性（手算锁）

def test_mmr_displaces_near_duplicate():
    """判别用例（模块 docstring 的手算）：第二名是第一名的近重复时，
    MMR 用第三名换下它——检索原序 [a, a2, b] → top-2 = [a, b]。"""
    out = mmr_select(Q, ["a", "a2", "b"], VECS, top_k=2)
    assert out == ["a", "b"]


def test_mmr_full_order_recovers_demoted_at_k3():
    """k=3 全取：被降位的近重复排最后（[a, b, a2]），不丢题只让位。"""
    out = mmr_select(Q, ["a", "a2", "b"], VECS, top_k=3)
    assert out == ["a", "b", "a2"]


def test_mmr_first_pick_is_pure_relevance():
    """首选拍纯 relevance（惩罚项为空）：与 query 同向者第一。"""
    out = mmr_select(Q, ["x", "y"], {"x": [0.0, 1.0], "y": [1.0, 0.0]}, top_k=1)
    assert out == ["y"]


def test_mmr_tie_breaks_by_input_order():
    """平票按输入序（严格大于才换人）：同分候选先入者胜，输出确定。"""
    out = mmr_select(Q, ["p", "q"], {"p": [0.6, 0.8], "q": [0.6, 0.8]}, top_k=1)
    assert out == ["p"]


def test_mmr_truncates_and_empty():
    assert mmr_select(Q, [], {}, top_k=5) == []
    assert mmr_select(Q, ["a"], VECS, top_k=0) == []
    assert mmr_select(Q, ["a"], VECS, top_k=9) == ["a"]


def test_mmr_accepts_non_unit_inputs():
    """防御性归一：非单位输入先归一再算（2 倍长度的 a 与 a 同分）。"""
    out = mmr_select(Q, ["a", "b"], {"a": [2 * x for x in A], "b": [0.0, 5.0]}, top_k=2)
    assert out[0] == "a"


def test_constants_locked():
    """λ/池深数值锁（test_bm25f.test_params_locked 同款纪律）：
    λ=0.7、池深 20 都是 spec 常量——符号自证防不住常量被改。"""
    assert MMR_LAMBDA == 0.7
    assert MMR_POOL_N == 20


# diversify_hits：组装与降级

def test_diversify_reorders_hits(monkeypatch, tmp_path):
    """向量方法可用 → 按手算口径重排（近重复让位）。"""
    from interviewer.dataflows import interface

    def _qv(query, *, ids, corpus_dir="./corpus"):
        return np.array(Q), {"a": A, "a2": A2, "b": B}

    monkeypatch.setitem(
        interface._VENDOR_METHODS.setdefault("question_vectors", {}), "local", _qv)
    hits = [_q("a"), _q("a2"), _q("b")]
    out = diversify_hits("任意", hits, corpus_dir=str(tmp_path), vendors="local", top_k=2)
    assert [h.id for h in out] == ["a", "b"]


def test_diversify_no_provider_truncates(tmp_path):
    """向量方法全链缺席 → 原序**截断**恰好 top_k 条：上游为开 MMR 放大了
    取题深度（池深 20），把 20 条漏给模型等于把池子当考卷。"""
    hits = [_q(f"h{i}") for i in range(20)]
    out = diversify_hits("任意", hits, corpus_dir=str(tmp_path), vendors="local", top_k=5)
    assert [h.id for h in out] == [f"h{i}" for i in range(5)]


def test_diversify_provider_error_falls_through_to_next_leg(monkeypatch, tmp_path):
    """首腿抛错 → 试下一腿（混合链不被未注册方连坐的同款逐家探测）。"""
    from interviewer.dataflows import interface

    def _boom(*a, **k):
        raise RuntimeError("腿炸")

    def _ok(query, *, ids, corpus_dir="./corpus"):
        return np.array(Q), {i: np.array(A) for i in ids}

    m = interface._VENDOR_METHODS.setdefault("question_vectors", {})
    monkeypatch.setitem(m, "badleg", _boom)
    monkeypatch.setitem(m, "goodleg", _ok)
    hits = [_q("x1"), _q("x2")]
    out = diversify_hits("任意", hits, corpus_dir=str(tmp_path),
                         vendors="badleg,goodleg", top_k=2)
    assert len(out) == 2
