"""混合检索单测：RRF 公式手算锁死 + hybrid vendor 组装契约。
RRF 是整个 hybrid 的数学本体，按 test_bm25f 的同款纪律对待：手算 fixture
锁死公式与常数，防止"看起来像 RRF"的悄悄变形（k 改了、rank 从 0 起算了、
平票乱序了，召回数字都会无声漂移）。
"""
import pytest

pytest.importorskip("numpy")   # dense 组可选：不装 numpy/fastembed 的环境整文件跳过
                                #（hybrid 模块级 import dense_corpus → numpy）

import interviewer.dataflows.hybrid as hy
from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.hybrid import LEG_TOP_N, rrf_fuse, search_corpus_hybrid
from interviewer.dataflows.interface import get_vendor_chain


def _q(qid: str) -> BankQuestion:
    return BankQuestion(id=qid, topic=f"考点{qid}", text=f"题面{qid}")


# rrf_fuse：公式与确定性

def test_rrf_formula_handcomputed():
    """手算锁死：两腿 [a,b,c] 与 [b,d]，k=60——
    b = 1/62 + 1/61 ≈ 0.032522（两腿共识，登顶）
    a = 1/61 ≈ 0.016393；d = 1/62 ≈ 0.016129；c = 1/63 ≈ 0.015873"""
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"]], top_k=10)
    assert [i for i, _ in fused] == ["b", "a", "d", "c"]
    scores = dict(fused)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["d"] == pytest.approx(1 / 62)


def test_rrf_tie_breaks_by_first_appearance():
    """平票确定性：x = 1/61+1/62 与 y = 1/62+1/61 同分——按首次出现序
（x 在第一条腿的第 1 名）分先后。同一输入永远同一输出。"""
    fused = rrf_fuse([["x", "y"], ["y", "x"]], top_k=10)
    assert [i for i, _ in fused] == ["x", "y"]
    assert fused[0][1] == pytest.approx(fused[1][1])


def test_rrf_top_k_truncates():
    assert len(rrf_fuse([["a", "b", "c"]], top_k=2)) == 2


def test_rrf_empty_legs_give_empty():
    assert rrf_fuse([[], []], top_k=5) == []


def test_rrf_both_legs_beats_single_leg_first():
    """hybrid 的赢面判别用例：双腿共识（各第 3 名）压过单腿第 1 名——
    k=60 的平滑就是为了让"两条腿都点头"值钱。"""
    fused = rrf_fuse([["a", "b", "both"], ["c", "d", "both"]], top_k=5)
    assert fused[0][0] == "both"


# search_corpus_hybrid：组装契约

@pytest.fixture
def leg_spy(monkeypatch):
    """替身两腿 + 调用记录。local 腿回 [A, B, C]、dense 腿回 [B, D]——
    B 双腿共识应登顶（手算：B=1/62+1/61 > A=1/61 > D=1/62 > C=1/63）。"""
    calls = {"local": [], "dense": []}

    def _local(query, *, exclude_topics=(), top_k=5, corpus_dir="./corpus"):
        calls["local"].append({"query": query, "exclude": exclude_topics, "top_k": top_k})
        return [_q("A"), _q("B"), _q("C")]

    def _dense(query, *, exclude_topics=(), top_k=5, corpus_dir="./corpus"):
        calls["dense"].append({"query": query, "exclude": exclude_topics, "top_k": top_k})
        return [_q("B"), _q("D")]

    monkeypatch.setattr(hy, "search_corpus_local", _local)
    monkeypatch.setattr(hy, "search_corpus_dense", _dense)
    return calls


def test_hybrid_fuses_legs_with_rrf_order(leg_spy):
    hits = search_corpus_hybrid("任意", top_k=3)
    assert [h.id for h in hits] == ["B", "A", "D"]


def test_hybrid_passes_exclude_and_leg_depth(leg_spy):
    """exclude_topics 透传给两条腿（查重硬约束不能在融合层漏掉）；
    腿深 = max(LEG_TOP_N, top_k)——默认腿深 20，top_k 更大时跟着放大。"""
    search_corpus_hybrid("q", exclude_topics=("已考",), top_k=3)
    for leg in ("local", "dense"):
        assert leg_spy[leg][0]["exclude"] == ("已考",)
        assert leg_spy[leg][0]["top_k"] == LEG_TOP_N
    search_corpus_hybrid("q", top_k=LEG_TOP_N + 5)
    assert leg_spy["local"][-1]["top_k"] == LEG_TOP_N + 5


def test_hybrid_empty_legs_return_empty(leg_spy, monkeypatch):
    """空哨兵契约在 hybrid 层的延续：两腿全空 → 空列表（上层三态探测照常）。
    dense 腿的阈值截断先保证了"腿空 = 没有语义相关"，hybrid 不另设防线。"""
    monkeypatch.setattr(hy, "search_corpus_local", lambda *a, **k: [])
    monkeypatch.setattr(hy, "search_corpus_dense", lambda *a, **k: [])
    assert search_corpus_hybrid("任意") == []


def test_hybrid_leg_error_propagates(leg_spy, monkeypatch):
    """TODO 红线：腿抛异常不在 vendor 内部吞——交给 config 链的回退
（route_to_vendor 职责）。这里吞了，"hybrid 不可用退 local"就永远
    不会被触发，fail-loud 被软化成 fail-silent。"""
    def _boom(*a, **k):
        raise RuntimeError("fastembed 没装")

    monkeypatch.setattr(hy, "search_corpus_dense", _boom)
    with pytest.raises(RuntimeError, match="fastembed"):
        search_corpus_hybrid("任意")


def test_hybrid_registered_as_vendor():
    """注册表路由：'hybrid' 经 config 链可解析到本函数。"""
    assert get_vendor_chain("search_corpus", "hybrid")[0] is search_corpus_hybrid


def test_hybrid_single_leg_survives_threshold_cutoff(leg_spy, monkeypatch):
    """单腿为空的生产主路径（2026-11-16 审查补锁）：dense 腿被阈值截成空、
    local 腿非空 → hybrid 输出 local 名次。若被误改成"两腿都非空才算命中"，
    bank_tool 会把"库里有题"误诊成"库真没有"（AVAILABLE 哨兵误发）。"""
    monkeypatch.setattr(hy, "search_corpus_dense", lambda *a, **k: [])
    hits = search_corpus_hybrid("任意", top_k=3)
    assert [h.id for h in hits] == ["A", "B", "C"]


def test_resolve_search_tool_wires_dense_and_hybrid():
    """组装入口的 import 副作用分支（2026-11-16 审查补锁）：链名 dense/hybrid
    的显式 import 被删/拼错时这里当场红，而不是用户配置后建图才炸
（"import 了 bank_tool 没 import vendor"是踩过的坑，见 resolve_search_tool
    注释）。空串 = 整体关闭的老开关一并锁住。"""
    from interviewer.dataflows import resolve_search_tool

    tool = resolve_search_tool({
        "corpus_vendors": "hybrid,local", "corpus_dir": "./corpus", "bank_top_k": 5,
    })
    assert tool is not None
    assert resolve_search_tool({
        "corpus_vendors": "", "corpus_dir": "./corpus", "bank_top_k": 5,
    }) is None


def test_leg_constants_locked():
    """腿深/k 的数值锁（test_bm25f.test_params_locked 同款纪律，2026-11-17
    全量审查补）："腿深 20 / k=60" 是 spec 常量——此前测试全用符号自证
（== LEG_TOP_N），把腿深改成 5 照样全绿。k=60 已由手算分数间接锁死，
    这里再显式双锁（引用常量注释/eval 重议时这行就是基准）。"""
    assert LEG_TOP_N == 20
    assert hy.RRF_K == 60


def test_hybrid_registers_vector_face():
    """混合链的向量面代答⑤）：链串 "hybrid,local" 里没有 "dense"
    这个名字——hybrid 不代答 question_vectors/vectors_by_topic 的话，最
    主流的混合链组卷层（MMR/查重）永远探测不到题向量。"""
    from interviewer.dataflows.dense_corpus import question_vectors, vectors_by_topic
    from interviewer.dataflows.interface import get_vendor_chain

    assert get_vendor_chain("question_vectors", "hybrid")[0] is question_vectors
    assert get_vendor_chain("vectors_by_topic", "hybrid")[0] is vectors_by_topic
