"""稠密检索 vendor 单测：全离线（FakeEncoder 顶替真模型）。
FakeEncoder 的"语义"：文本 → bm25f.tokenize 的 token 集合 → 每个 token 一个
md5 种子的伪随机基向量 → 求和归一。共享 token 的文本 cosine 高——语义
可控、完全确定性的假模型，让逻辑测试（建索引/三级缓存/阈值截断/空哨兵/
模型指纹/回退链）不碰网络也不碰 ONNX。真模型的数字归 eval 脚本的
ablation 表，不进 pytest（测试全离线纪律）。
"""
import hashlib

import pytest

# numpy 是 dense 可选组的依赖：未装的环境（只装 [dev]）本文件整体跳过，
# "不装 dense 全部测试仍绿"的契约因此成立
pytest.importorskip("numpy")
import numpy as np

import interviewer.dataflows.dense_corpus as dc
import interviewer.dataflows.local_corpus  # noqa: F401 —— 注册 local（回退链测试用）
from interviewer.dataflows.bm25f import tokenize
from interviewer.dataflows.interface import route_to_vendor

DIM = 512


class FakeEncoder:
    """确定性伪嵌入器（dc.FastembedEncoder 的同接口替身）。

    几何：文本 → token 集合 → 每个 token 一个 md5 种子的**单位**伪随机基向量
    → 求和。共享 s 个 token 的 query/doc 的 cosine 期望 = s/√(n_q·n_doc)，
    非共享对的交叉噪声 std = 1/√DIM——基向量单位化 + 512 维把噪声压到
    ±0.05 以下，"共享 token → 高分"的信号才立得住（不归一化的 64 维版本
    实测翻车：零重叠的题靠噪声过阈值、真命中的题被噪声反超）。

    model_id 不是摆设：它进索引目录名与 meta 校验——"换模型 = 新集合"
    的合同就靠它测（两台"模型"的索引绝不互读）。embed_documents_calls
    计数供缓存测试断言"没有重算"。
    """

    def __init__(self, model_id="fake-64", dim=DIM):
        self.model_id = model_id
        self.dim = dim
        self.embed_documents_calls = 0

    def _basis(self, token: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "little")
        v = np.random.default_rng(seed).standard_normal(self.dim)
        return v / np.linalg.norm(v)

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim)
        for tok in set(tokenize(text)):
            v += self._basis(tok)
        norm = np.linalg.norm(v)
        return v / norm if norm else v   # 零向量保持零：cosine=0，天然落阈值下

    def embed_documents(self, texts):
        self.embed_documents_calls += 1
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.stack([self._vec(t).astype(np.float32) for t in texts])

    def embed_query(self, text):
        return self._vec(text).astype(np.float32)


# 测试语料（格式同 test_bank_corpus；A 带"只存在于追问树"的子考点）

_BLOCK_A = """---
id: bank-t-001
topic: Kafka 高可用
difficulty: 3
tags: kafka, 消息队列
source: 测试语料

**题目**
三节点集群 leader 宕机时如何保证已确认的消息不丢？

**追问树**
- ISR 与 acks 的关系？

**评分锚点**
- 5 分：说清联动
"""

_BLOCK_B = """---
id: bank-t-002
topic: 缓存击穿
difficulty: 4
tags: redis, 缓存

**题目**
热点 key 过期瞬间请求穿透数据库怎么办？
"""


def _make_corpus(root) -> str:
    d = root / "project"
    d.mkdir(parents=True)
    (d / "a.md").write_text(_BLOCK_A + "\n" + _BLOCK_B, encoding="utf-8")
    return str(root)


@pytest.fixture
def dense_env(tmp_path, monkeypatch):
    """离线测试环境：INDEX_ROOT 指到 tmp（不写 repo 的 data/）、编码器换 Fake。
    search_corpus_dense 全路径可测——不碰 ./data、不碰 fastembed。

    DENSE_MIN_COSINE 一并钉到假模型的自洽值：真 bge 的相关对 cosine 0.8+、
    阈值 0.45 是按真模型分布校准的；伪嵌入的几何是 s/√(n_q·n_doc)——
    部分重叠 0.2-0.4、噪声 ±0.05。阈值属于真模型世界，fixture 给假世界
    定自己的标尺（0.10）：共享 token 的查询稳过线、零重叠的稳落线。
    """
    corpus = _make_corpus(tmp_path)
    enc = FakeEncoder()
    monkeypatch.setattr(dc, "INDEX_ROOT", str(tmp_path / "dense_index"))
    monkeypatch.setattr(dc, "_get_encoder", lambda *a, **k: enc)
    monkeypatch.setattr(dc, "DENSE_MIN_COSINE", 0.10)
    return corpus, enc


# 检索契约

def test_dense_shared_tokens_rank_first(dense_env):
    """共享 token 的题排第一：伪嵌入的"语义"= token 重叠，行为对齐直觉。"""
    corpus, _ = dense_env
    hits = dc.search_corpus_dense("kafka 宕机", corpus_dir=corpus)
    assert [h.id for h in hits] == ["bank-t-001"]


def test_dense_followup_tree_participates_in_embedding(dense_env):
    """追问树进嵌入面（TODO 字段取舍）：isr/acks 只存在于 A 的追问树——
    查它必须命中 A。评分锚点不进嵌入面（union 反例归 eval 数字管）。"""
    corpus, _ = dense_env
    hits = dc.search_corpus_dense("ISR 与 acks 的配置", corpus_dir=corpus)
    assert hits and hits[0].id == "bank-t-001"


def test_dense_below_threshold_returns_empty(dense_env):
    """空哨兵和解线（TODO 点名的冲突）：稠密"永远有最邻近"，但离题 query
    的最邻近低于 DENSE_MIN_COSINE → 截断为空 → NO_RESULTS_AVAILABLE
    哨兵照常触发。这条不断言，下游三态探测就整个失真。"""
    corpus, _ = dense_env
    assert dc.search_corpus_dense("量子纠缠 佯谬", corpus_dir=corpus) == []


def test_dense_exclude_topics(dense_env):
    """签发查重硬约束：已考考点在打分前出局（与 local 同语义）。"""
    corpus, _ = dense_env
    assert dc.search_corpus_dense("kafka", exclude_topics=["Kafka 高可用"],
                                  corpus_dir=corpus) == []


def test_dense_missing_dir_fail_open(dense_env, tmp_path):
    """决策④同款：目录缺席返回空而非崩溃（题库增强非承诺）。"""
    assert dc.search_corpus_dense("任意", corpus_dir=str(tmp_path / "没有")) == []


def test_dense_bad_corpus_raises_not_cached(tmp_path, monkeypatch):
    """坏库 fail-loud 且不缓存短路（BankIndex 同款纪律：第二次调用要重新炸）。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "bad.md").write_text(
        "---\nid: bank-bad\ntopic: 坏块\ndifficulty: 9\n\n**题目**\n越界\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dc, "INDEX_ROOT", str(tmp_path / "idx"))
    monkeypatch.setattr(dc, "_get_encoder", lambda *a, **k: FakeEncoder())
    for _ in range(2):
        with pytest.raises(ValueError, match="坏块"):
            dc.search_corpus_dense("任意", corpus_dir=str(tmp_path))


# 三级缓存：内存 → 磁盘 → 现算

def test_dense_disk_index_reused_across_memory_cache_clear(dense_env):
    """磁盘层：清掉内存缓存后仍不重算嵌入（从 npz 载入）——
    "跨进程持久化"的单进程模拟。"""
    corpus, enc = dense_env
    dc.search_corpus_dense("kafka", corpus_dir=corpus)
    assert enc.embed_documents_calls == 1
    index_dir = dc._index_dir(dc.INDEX_ROOT, enc)
    assert (index_dir / "index.npz").exists() and (index_dir / "meta.json").exists()

    dc._INDEX_CACHE.clear()
    dc.search_corpus_dense("kafka", corpus_dir=corpus)
    assert enc.embed_documents_calls == 1   # 没有重算：磁盘索引接住了


def test_dense_corpus_change_rebuilds_index(dense_env, tmp_path):
    """指纹层：题库改动（新文件）→ 指纹变 → 重建嵌入，新题立即可见
（mtime 缓存最经典的坑"改了不生效"，与 BankIndex 同一条锁）。"""
    corpus, enc = dense_env
    dc.search_corpus_dense("kafka", corpus_dir=corpus)
    assert enc.embed_documents_calls == 1

    (tmp_path / "project" / "b.md").write_text(
        "---\nid: bank-t-003\ntopic: 消息丢失\n\n**题目**\n消息丢了怎么办\n",
        encoding="utf-8",
    )
    hits = dc.search_corpus_dense("消息丢失", corpus_dir=corpus)
    assert enc.embed_documents_calls == 2                  # 指纹变 → 重算
    assert any(q.id == "bank-t-003" for q in hits)


def test_dense_model_change_uses_new_collection(dense_env, tmp_path, monkeypatch):
    """模型指纹（TODO 红线：换模型 = 新集合，绝不混写）：换 encoder 后
    不读旧模型的 npz（重算），且两套索引各在各的目录里。"""
    corpus, enc_a = dense_env
    dc.search_corpus_dense("kafka", corpus_dir=corpus)

    enc_b = FakeEncoder(model_id="fake-other")
    monkeypatch.setattr(dc, "_get_encoder", lambda *a, **k: enc_b)
    dc.search_corpus_dense("kafka", corpus_dir=corpus)
    assert enc_b.embed_documents_calls == 1                # 没混读 A 的索引

    dirs = {p.name for p in (tmp_path / "dense_index").iterdir()}
    assert dirs == {"fake-64", "fake-other"}


def test_dense_memory_cache_hit_skips_disk_read(dense_env):
    """内存层：同指纹重复检索不重算、不重读盘（计数与磁盘 mtime 双证）。"""
    corpus, enc = dense_env
    dc.search_corpus_dense("kafka", corpus_dir=corpus)
    npz = dc._index_dir(dc.INDEX_ROOT, enc) / "index.npz"
    first_mtime = npz.stat().st_mtime_ns
    dc.search_corpus_dense("kafka 宕机", corpus_dir=corpus)
    assert enc.embed_documents_calls == 1
    assert npz.stat().st_mtime_ns == first_mtime           # 没重写


# vendor 注册与回退链（"删稠密走 local 仍全绿"的验收条款）

def test_dense_registered_and_routable(dense_env):
    corpus, _ = dense_env
    hits = route_to_vendor("search_corpus", "dense", "kafka", corpus_dir=corpus)
    assert hits[0].id == "bank-t-001"


def test_dense_failure_falls_back_to_local(dense_env, monkeypatch):
    """验收条款：稠密腿不可用（fastembed 没装 / 模型下载失败）→ 抛错穿透
    vendor（不内部吞）→ route_to_vendor 回退链把 local 的结果接住。"""
    corpus, _ = dense_env

    def _no_fastembed(*a, **k):
        raise ImportError("fastembed 未安装（稠密腿依赖）")

    monkeypatch.setattr(dc, "_get_encoder", _no_fastembed)
    hits = route_to_vendor("search_corpus", "dense,local", "kafka 宕机", corpus_dir=corpus)
    assert [h.id for h in hits] == ["bank-t-001"]


def test_dense_missing_dir_beats_missing_dependency(tmp_path, monkeypatch):
    """缺目录 fail-open 优先于依赖缺席（2026-11-16 审查修正：编码器原在
    目录检查之前被求值——fastembed 没装时缺目录抛 ImportError 而非返回空）。"""
    def _no(*a, **k):
        raise ImportError("fastembed 未安装（稠密腿依赖）")

    monkeypatch.setattr(dc, "_get_encoder", _no)
    assert dc.search_corpus_dense("任意", corpus_dir=str(tmp_path / "没有")) == []


# 审查修正的回归锁（2026-11-16 三轴审查）

def test_dense_corrupt_npz_self_heals(dense_env):
    """磁盘索引损坏（截断 = 最常见损坏形态）→ 不炸、静默重建。修正前
    BadZipFile/EOFError 家族逃出 _load_index 的异常网：每次调用重炸且
    不自愈，删文件前稠密腿整体瘫痪。"""
    corpus, enc = dense_env
    dc.search_corpus_dense("kafka", corpus_dir=corpus)
    npz = dc._index_dir(dc.INDEX_ROOT, enc) / "index.npz"
    raw = npz.read_bytes()
    npz.write_bytes(raw[: len(raw) // 2])            # 截断半个 zip
    dc._INDEX_CACHE.clear()

    hits = dc.search_corpus_dense("kafka", corpus_dir=corpus)   # 不抛 = 修复本体
    assert hits and hits[0].id == "bank-t-001"
    assert enc.embed_documents_calls == 2                      # 重建（重算一次）
    assert npz.read_bytes() != raw[: len(raw) // 2]            # 坏文件被替换


def test_dense_empty_corpus_disk_roundtrip(tmp_path, monkeypatch):
    """空语料（目录在、零可解析 md）的磁盘索引可载入。修正前 meta 写
    dim=0 与落盘矩阵 (0,1) 自相矛盾 → 校验恒失败 → 每个新进程都重建重写。"""
    (tmp_path / "project").mkdir()
    enc = FakeEncoder()
    monkeypatch.setattr(dc, "INDEX_ROOT", str(tmp_path / "idx"))
    monkeypatch.setattr(dc, "_get_encoder", lambda *a, **k: enc)
    monkeypatch.setattr(dc, "DENSE_MIN_COSINE", 0.10)

    assert dc.search_corpus_dense("任意", corpus_dir=str(tmp_path)) == []
    assert enc.embed_documents_calls == 1
    dc._INDEX_CACHE.clear()
    assert dc.search_corpus_dense("任意", corpus_dir=str(tmp_path)) == []
    assert enc.embed_documents_calls == 1              # 第二次从磁盘载入，没重算
