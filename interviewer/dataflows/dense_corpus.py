"""稠密检索 vendor：bge-small-zh-v1.5 语义向量 + numpy 点积。

与 BM25F 的对照：BM25F 量"字面重叠"（命中清单可数可解释，天花板在改写
措辞的查询上）；稠密检索量"语义接近"（"宕机""挂了""故障转移"向量相邻，
哪怕零字面共享；代价是跑模型 + 无可解释账目）。200 题规模不装 chromadb：
numpy 一次矩阵点积就是全库打分（毫秒级），向量库的 ANN 索引在这个量级是
负资产——答案写在 eval 表的 latency 列里。

与空哨兵契约的和解：向量检索"永远有最邻近"，而三态哨兵建立在"空命中"
上——和解线 = cosine 阈值截断，低于 DENSE_MIN_COSINE 的命中按"没有"
处理；阈值用真实题库校准（金标查询对 gold 的最低分 vs 离题查询的最高分）。
fastembed 只在本模块惰性导入（装不装不影响 local 链路）。
"""
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.interface import register_vendor

logger = logging.getLogger(__name__)

# 参数锁（同 bm25f.py 的纪律：小库上调参 = 对着考卷改答案）
MODEL_NAME = "BAAI/bge-small-zh-v1.5"   # 512 维中文嵌入，ONNX CPU 单查询毫秒级
DENSE_MIN_COSINE = 0.50                 # 哨兵和解线。2026-11-16 真模型校准（50 题 +
                                        # 118 条金标 + 6 条离题探针）：gold-cosine 最低
                                        # 0.389（全是 keyword 桶短查询，local 主场）、
                                        # 离题 max 0.483——0.50 让离题泄漏归零，代价是
                                        # 误裁 3 条 keyword gold（dense 单列可见，hybrid
                                        # 里 local 腿全兜住）。哨兵是正确性不变量，
                                        # 排序质量是统计量：正确性优先。C4 扩到 200 题
                                        # 后随金标重校（离题 max 只会更挤）。
                                        # 诚实边界（同日实测）：零泄漏只对**远域**探针
                                        # 成立（金标 offtopic 桶 6 条，eval 回归锁定）；
                                        # 题库外的技术相邻查询（docker 0.572 / 快排
                                        # 0.556 / git rebase 0.506）会过线——bge 眼里
                                        # "都是技术文本"本身算语义相近。哨兵保证范围 =
                                        # 远域；技术相邻的泄漏数字记在 eval 表，200 题
                                        # 重议时与阈值上调一并决定
CACHE_DIR = "./data/fastembed"          # 模型权重缓存（root data/ 已 gitignore；
                                        # fastembed 默认落 Temp 目录，Windows 清理
                                        # 会吃掉权重导致静默重下，必须显式指路）
INDEX_ROOT = "./data/dense_index"       # numpy 索引根：每模型一个子目录（指纹隔离）


def embed_text(q: BankQuestion) -> str:
    """题 → 嵌入文本：topic + tags + text + 追问树。追问树装着主文没有的
    子考点（检索"acks 参数"靠它有落点）；评分锚点不进——那是答案钥匙，
    嵌进去等于让"答案长什么样"参与决定"这道题被检到"。"""
    parts = [q.topic, " ".join(q.tags), q.text, *q.followup_tree]
    return "\n".join(p for p in parts if p and p.strip())


# 编码器 seam：真模型 / 测试假模型各一个 adapter，DenseIndex 只认接口


class FastembedEncoder:
    """fastembed 薄封装（真模型 adapter）。passage/query 两侧分工不自己拼：
    bge 系模型查询侧要加指令前缀、文档侧不加——搞反是稠密检索最隐蔽的
    翻车点（不报错，只是召回悄悄变差），交给库按模型卡处理。向量两侧
    都归一化成单位长度，之后的点积就是 cosine。model_name 参数化：
    relevance_gate 用同一封装跑 jina 索引，两套索引按 model_id 隔离。
    """

    def __init__(self, model_name: str = MODEL_NAME):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "fastembed 未安装（稠密腿依赖）：pip install fastembed"
            ) from exc
        self.model_id = model_name   # 进索引目录名与 meta 双校验（换模型 = 新集合）
        self._model = TextEmbedding(model_name=model_name, cache_dir=CACHE_DIR)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """文档侧批量嵌入 → (n, dim) 单位向量。空列表 → (0, 1) 空矩阵。"""
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        return _unit_rows(np.asarray(
            [v.astype(np.float32) for v in self._model.passage_embed(texts)]
        ))

    def embed_query(self, text: str) -> np.ndarray:
        """查询侧单条嵌入 → (dim,) 单位向量。"""
        return _unit_rows(np.asarray(
            [next(iter(self._model.query_embed([text]))).astype(np.float32)]
        ))[0]


def _unit_rows(m: np.ndarray) -> np.ndarray:
    """行归一化。零向量保持零（归一它会变 nan）——零向量 cosine=0，
    自然落在阈值之下被丢弃，正是"无语义重叠"想要的语义。"""
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms > 0, norms, 1.0)


def unit_vec(v) -> np.ndarray:
    """单向量归一（零向量保持零）。mmr/dedup 与索引行共用的归一合同——
    只写这一处，防止两套归一口径漂移。"""
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


_ENCODER_CACHE: dict[str, object] = {}


def _get_encoder(model_name: str = MODEL_NAME):
    """编码器单例（模型加载秒级，绝不能每次检索付一遍）。按模型名分槽——
    远域闸（relevance_gate）与本模块主力 bge-small 各持一个单例。"""
    if model_name not in _ENCODER_CACHE:
        _ENCODER_CACHE[model_name] = FastembedEncoder(model_name)
    return _ENCODER_CACHE[model_name]


# 索引本体

class DenseIndex:
    """题库向量索引：parse 结果 + 行对齐的单位向量矩阵。

    行序 = parse_corpus 顺序（sorted glob，确定性）——ids 与 questions
    按位置对齐是全部持久化/缓存合同的地基，载入时逐 id 核对。
    """

    def __init__(self, questions, matrix: np.ndarray, fingerprint: tuple, encoder):
        self.questions = questions
        self.matrix = matrix
        self.fingerprint = fingerprint
        self.encoder = encoder

    def search(self, query: str, *, exclude_topics=(), top_k: int = 5) -> list[BankQuestion]:
        """cosine top-k，带两条硬过滤：exclude_topics（签发查重，同 local 的
        打分前过滤语义）与 DENSE_MIN_COSINE（空哨兵和解线）。"""
        if not self.questions:
            return []
        qv = self.encoder.embed_query(query)
        rows = [i for i, q in enumerate(self.questions) if q.topic not in exclude_topics]
        if not rows:
            return []
        sims = self.matrix[rows] @ qv
        # 稳定排序 + 行序决胜：cosine 并列时按 parse 顺序，输出确定可复现
        order = sorted(range(len(rows)), key=lambda j: (-sims[j], j))
        out: list[BankQuestion] = []
        for j in order[:top_k]:
            if sims[j] < DENSE_MIN_COSINE:
                break   # 已按分降序，第一个低于阈值 = 后面全低
            out.append(self.questions[rows[j]])
        return out


def _index_dir(index_root: str, encoder) -> Path:
    """索引目录 = <root>/<model_id 安全化>。换模型 = 新目录，绝不混写
（TODO 红线：512 维和别的维数矩阵混载会在点积时炸，同名不同模型
    静默混写更阴——目录名先隔离，meta 里再校验一道）。"""
    return Path(index_root) / encoder.model_id.replace("/", "__")


def _save_index(index_dir: Path, index: DenseIndex) -> None:
    """落盘 meta.json + index.npz（tmp + os.replace 原子替换——写一半断电
    不留半个索引）。写失败只告警不炸：持久化是加速件，检索本体还有内存态。"""
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
        npz_tmp = index_dir / "index.npz.tmp"
        with open(npz_tmp, "wb") as f:
            np.savez(f, matrix=index.matrix, ids=np.array([q.id for q in index.questions]))
        os.replace(npz_tmp, index_dir / "index.npz")
        meta_tmp = index_dir / "meta.json.tmp"
        meta_tmp.write_text(json.dumps({
            "model_id": index.encoder.model_id,
            # shape[1] 而非 size 判空：空语料矩阵是 (0,1)，写 dim=0 会与
            # 载入侧的形状校验自相矛盾（空索引永远载不回来，每次重建重写）
            "dim": int(index.matrix.shape[1]) if index.matrix.ndim == 2 else 0,
            "fingerprint": [[p, m, s] for p, m, s in index.fingerprint],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(meta_tmp, index_dir / "meta.json")
    except OSError:
        logger.warning("稠密索引落盘失败（%s）：本次仅用内存态", index_dir, exc_info=True)


def _load_index(index_dir: Path, questions, fingerprint: tuple, encoder):
    """磁盘索引载入；任何不符（无文件/模型变/指纹变/id 错位/文件坏）→ None
    让调用方重建。全部判空而非报错：磁盘缓存本来就是"可信则用"的加速件。"""
    try:
        meta = json.loads((index_dir / "meta.json").read_text(encoding="utf-8"))
        if meta.get("model_id") != encoder.model_id:
            return None
        if tuple(tuple(x) for x in meta.get("fingerprint", [])) != fingerprint:
            return None
        data = np.load(index_dir / "index.npz", allow_pickle=False)
        ids = [str(x) for x in data["ids"]]
        if ids != [q.id for q in questions]:
            return None   # 行序与题库错位（理论不可达：指纹已同；防线照设）
        matrix = data["matrix"].astype(np.float32)
        if matrix.shape != (len(ids), int(meta.get("dim", matrix.shape[1]))):
            return None
        return DenseIndex(questions, matrix, fingerprint, encoder)
    # npz 的坏法家族（2026-11-16 审查实测）：非 zip 垃圾字节→ValueError、
    # zip 魔数在但结构坏/被截断→BadZipFile、0 字节→EOFError。磁盘缓存是
    # 加速件：任何坏法都落到 None 重建，绝不让半个坏文件把检索卡死
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
        return None


# 指纹复用 local_corpus 的 _fingerprint（不另写一份）：两套索引的失效条件
# 必须对同一份语料同时触发——两份各自漂移的指纹 = "BM25F 见了新题、稠密
# 还在旧题上检索"这类最阴的缓存 bug。私有名跨模块引用是刻意的（合同共享）。
from interviewer.dataflows.local_corpus import _fingerprint, parse_corpus  # noqa: E402

# 模块级内存缓存：key = (normcase 路径, model_id) → (指纹, DenseIndex)。
# 与 BankIndex 同款纪律：指纹不符即重建；坏库（parse raise）不缓存短路。
_INDEX_CACHE: dict[tuple, tuple] = {}


def _get_index(corpus_dir, model_name: str = MODEL_NAME) -> DenseIndex:
    """缓存入口（内存 → 磁盘 → 现算，三级）。model_name 参数化：默认主力
    bge-small，relevance_gate 传 jina——索引目录与缓存键都带 model_id，
    两套索引物理隔离。编码器在目录检查之后才取用：题库缺席（fail-open）
    时不碰 fastembed，没装依赖的环境里缺目录该返回空列表而非 ImportError；
    空索引的 encoder 槽位为 None，search 对空 questions 首行即返回。
    """
    root = Path(corpus_dir)
    if not root.is_dir():
        # 决策④同款 fail-open：目录缺席返回空索引且**不缓存**（随时可能被建出来）
        return DenseIndex([], np.zeros((0, 1), dtype=np.float32), (), None)
    encoder = _get_encoder(model_name)
    key = (os.path.normcase(str(root.resolve())), encoder.model_id)
    fp = _fingerprint(root)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == fp:
        return cached[1]
    questions = parse_corpus(corpus_dir)   # 坏块/重复 id 在此 raise，不缓存
    index = _load_index(_index_dir(INDEX_ROOT, encoder), questions, fp, encoder)
    if index is None:
        matrix = encoder.embed_documents([embed_text(q) for q in questions])
        index = DenseIndex(questions, matrix, fp, encoder)
        _save_index(_index_dir(INDEX_ROOT, encoder), index)
    _INDEX_CACHE[key] = (fp, index)
    return index


def search_corpus_dense(query, *, exclude_topics=(), top_k=5,
                        corpus_dir="./corpus") -> list[BankQuestion]:
    """稠密检索 vendor（签名与 search_corpus_local 全同，注册表内可互换）。"""
    return _get_index(corpus_dir).search(
        query, exclude_topics=exclude_topics, top_k=top_k
    )


# 注册表第二/三方法（组卷层的粮；corpus_topics 同款机制）

def question_vectors(query: str, *, ids: list[str], corpus_dir="./corpus"):
    """(查询向量, {id: 题向量})——MMR 的粮。题向量取索引行（与建库同一份
    嵌入，绝无另算一套的口径分叉）；id 不在索引 → KeyError、空索引 →
    ValueError，都交调用方按 fail-open 降级。"""
    index = _get_index(corpus_dir)
    if index.encoder is None:
        raise ValueError(f"语料无稠密索引：{corpus_dir}")
    qv = index.encoder.embed_query(query)
    row_of = {q.id: i for i, q in enumerate(index.questions)}
    return qv, {qid: index.matrix[row_of[qid]] for qid in ids}


def vectors_by_topic(topics: list[str], *, corpus_dir="./corpus") -> dict:
    """考点串 → 整题向量（查重 v1.5 的粮）。考点按全名精确匹配回索引行
（issued_keys 原串就是 canonical topic）；映射不上的串（generated 自创
    考点/题库外措辞）直接省略，调用方见缺即知管不了。空索引同上
    ValueError。"整题向量而非 topic 串"的教训见 dedup.py 头注释。"""
    index = _get_index(corpus_dir)
    if index.encoder is None:
        raise ValueError(f"语料无稠密索引：{corpus_dir}")
    row_of = {q.topic: i for i, q in enumerate(index.questions)}
    return {t: index.matrix[row_of[t]] for t in topics if t in row_of}


register_vendor("search_corpus", "dense", search_corpus_dense)
register_vendor("question_vectors", "dense", question_vectors)
register_vendor("vectors_by_topic", "dense", vectors_by_topic)
