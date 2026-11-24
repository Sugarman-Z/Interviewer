"""staging 隔离测试：`_` 前缀目录在结构上不可见。
锁两层：「staging 永不被 parse」（候选题进不了签发链路，防幻觉纪律延伸
到数据侧）；缓存同口径（staging 编辑不触发指纹变化，否则改候选题会
白白重建索引）。
"""
from pathlib import Path

from interviewer.dataflows.local_corpus import _fingerprint, parse_corpus

_BLOCK_A = """---
id: bank-t-001
topic: Kafka 高可用
difficulty: 3
tags: kafka, 消息队列

**题目**
三节点集群 leader 宕机时如何保证已确认消息不丢？

**追问树**
- ISR 与 acks 的关系？

**评分锚点**
- 5 分：说清联动
"""


def _make_corpus(root: Path) -> str:
    d = root / "project"
    d.mkdir(parents=True)
    (d / "a.md").write_text(_BLOCK_A, encoding="utf-8")
    return str(root)


def test_staging_dir_never_parsed(tmp_path):
    """`_` 前缀目录整体排除：候选题不进 parse，连 id 撞车也不炸——
    staging 对解析器是结构性不可见，不是「先 parse 再过滤」。"""
    corpus = _make_corpus(tmp_path)
    staging = tmp_path / "_staging"
    staging.mkdir()
    (staging / "cand.md").write_text(
        _BLOCK_A.replace("bank-t-001", "bank-t-100"), encoding="utf-8"
    )
    assert [q.id for q in parse_corpus(corpus)] == ["bank-t-001"]

    # 连与正式库重复的 id 都不该炸：staging 里放什么都进不了对账
    (staging / "dup.md").write_text(_BLOCK_A, encoding="utf-8")
    assert [q.id for q in parse_corpus(corpus)] == ["bank-t-001"]


def test_fingerprint_ignores_staging_edits(tmp_path):
    """缓存口径：staging 增删改不动语料指纹——否则人审候选题 = 反复重建索引。"""
    _make_corpus(tmp_path)
    fp_before = _fingerprint(tmp_path)

    staging = tmp_path / "_staging"
    staging.mkdir()
    (staging / "cand.md").write_text(
        "---\nid: bank-t-900\ntopic: 候选考点\n\n**题目**\n候选题面\n", encoding="utf-8"
    )
    assert _fingerprint(tmp_path) == fp_before
