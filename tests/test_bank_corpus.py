"""题库数据层单测：parser 失败语义四决策 + 检索打分契约 + vendor 注册路由。
检索测试用 tmp_path 自建小语料而非 corpus/ 真实种子——种子会持续加题，
锁死其内容会让测试随内容演化而碎；真实语料只做"全部可解析"冒烟。

判别用例思想：冒烟 query 恰好整词命中会掩盖"整词相等 vs 部分命中"的实现
缺口——测试里必须有"宕机"（只出现在正文）、"消息"（只出现在 tag）这类
能在强弱实现间做区分的用例；换 BM25F 后命中单位从"子串"变成"相邻双字
块"，判别意图不变。
"""
from pathlib import Path

import pytest

from interviewer.dataflows.errors import CorpusParseError, SourceNotConfiguredError
from interviewer.dataflows.interface import get_vendor_chain, route_to_vendor
from interviewer.dataflows.local_corpus import parse_corpus, search_corpus_local, topics_of

# 测试语料（内容永固定；B 块故意不带追问树，锁"可选小节默认空"）

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

**评分锚点**
- 3 分：两种方案
"""


def _make_corpus(root: Path) -> str:
    d = root / "project"
    d.mkdir(parents=True)
    (d / "a.md").write_text(
        "# 文件头：解析器应跳过（全角冒号不匹配块头）\n" + _BLOCK_A + "\n" + _BLOCK_B,
        encoding="utf-8",
    )
    return str(root)


# parser：失败语义四决策

def test_parse_roundtrip(tmp_path):
    qs = parse_corpus(_make_corpus(tmp_path))
    assert [q.id for q in qs] == ["bank-t-001", "bank-t-002"]  # 文件头被跳过
    a, b = qs
    assert a.module == "project"                  # 从父目录推导
    assert a.tags == ["kafka", "消息队列"]         # 逗号分词
    assert a.difficulty == 3
    assert a.followup_tree == ["ISR 与 acks 的关系？"]  # 弹头剥离
    assert "宕机" in a.text
    assert b.followup_tree == []                  # 可选小节缺省为空列表


def test_parse_bad_block_raises_with_location(tmp_path):
    """决策②：坏块必须炸，且报错带文件路径 + id，3 秒定位。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "bad.md").write_text(
        "---\nid: bank-bad-001\ntopic: 坏块\ndifficulty: 9\n\n**题目**\n难度越界\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"(?s)bad\.md.*bank-bad-001.*difficulty"):  # (?s)：报错跨行，. 要能过换行
        parse_corpus(tmp_path)


def test_parse_duplicate_id_raises(tmp_path):
    """决策③：id 是  防幻觉对账锚，跨文件重复必须炸。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "x.md").write_text(_BLOCK_A, encoding="utf-8")
    (d / "y.md").write_text(_BLOCK_A, encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        parse_corpus(tmp_path)


def test_parse_bad_field_translation_raises_corpus_parse_error(tmp_path):
    """解析失败全家一个类型（2026-11-18 审查逃逸修复）：difficulty 非数字
（int() 翻译失败）曾以裸 ValueError 穿出——窄捕获不认，题库一坏面试
    照炸（difficulty: 9 走 pydantic 校验会降级、difficulty: 高 却炸全场，
    同类手误两种结局）。现在与坏块/重复 id 同走 CorpusParseError 路由。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(
        "---\nid: bank-bad-002\ntopic: 难度手滑\ndifficulty: 高\n\n**题目**\n难度写成了汉字\n",
        encoding="utf-8",
    )
    with pytest.raises(CorpusParseError, match="翻译失败"):
        parse_corpus(tmp_path)


def test_parse_non_utf8_file_raises_corpus_parse_error(tmp_path):
    """中文 Windows 编辑器把题库存成 GBK/ANSI 同属解析失败——
    UnicodeDecodeError 也收口成 CorpusParseError（同上审查修复）。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_bytes(
        "---\nid: bank-gbk-001\ntopic: 编码坏档\ndifficulty: 3\n\n**题目**\n乱码存档测试\n".encode("gbk")
    )
    with pytest.raises(CorpusParseError, match="UTF-8"):
        parse_corpus(tmp_path)


def test_route_prefers_corpus_parse_error_over_first_error(tmp_path):
    """回退链选错策略（2026-11-18 审查确认）：dense 首链 + fastembed 缺席
    的部署里，坏库的 CorpusParseError 曾被先发的 ImportError 遮蔽——降级
    失效、面试崩溃。现在数据损坏立即短路重抛，不被依赖错顶掉。"""
    from interviewer.dataflows import interface

    def _no_dep(*a, **k):
        raise ImportError("fastembed 未安装（稠密腿依赖）")

    def _corrupt(*a, **k):
        raise CorpusParseError("题库 id 重复：x")

    interface.register_vendor("search_corpus", "fake_no_dep", _no_dep)
    interface.register_vendor("search_corpus", "fake_corrupt", _corrupt)
    try:
        with pytest.raises(CorpusParseError, match="重复"):
            interface.route_to_vendor(
                "search_corpus", "fake_no_dep,fake_corrupt", "q",
                corpus_dir=str(tmp_path))
    finally:
        interface._VENDOR_METHODS["search_corpus"].pop("fake_no_dep", None)
        interface._VENDOR_METHODS["search_corpus"].pop("fake_corrupt", None)


def test_parse_missing_dir_returns_empty(tmp_path):
    """决策④：题库缺席合法（fail-open），返回空而非崩溃。"""
    assert parse_corpus(tmp_path / "不存在") == []


def test_parse_real_seeds_smoke():
    """真实种子语料全量可解析。内容会增长，只锁"五个种子 id 都在"，不锁总数。"""
    ids = {q.id for q in parse_corpus("corpus")}
    assert {
        "bank-kafka-001", "bank-cache-001", "bank-cache-002",
        "bank-proj-001", "bank-proj-002",
    } <= ids


# search：打分契约

def test_search_topic_hit_ranks_first(tmp_path):
    corpus = _make_corpus(tmp_path)
    assert search_corpus_local("Kafka 高可用", corpus_dir=corpus)[0].id == "bank-t-001"


def test_search_text_substring_hit(tmp_path):
    """判别用例①：token「宕机」只出现在正文——必须靠子串包含命中，整词相等版在此必挂。"""
    corpus = _make_corpus(tmp_path)
    assert [q.id for q in search_corpus_local("宕机", corpus_dir=corpus)] == ["bank-t-001"]


def test_search_tag_partial_hit(tmp_path):
    """判别用例②：token「消息」只子串命中 tag「消息队列」——tags 必须逐个做子串判断。"""
    corpus = _make_corpus(tmp_path)
    assert [q.id for q in search_corpus_local("消息", corpus_dir=corpus)] == ["bank-t-001"]


def test_search_exclude_topics(tmp_path):
    """硬查重：已考考点在打分前出局 工具闭包传 issued_keys 的契约基础）。"""
    corpus = _make_corpus(tmp_path)
    assert [q.id for q in search_corpus_local("redis", corpus_dir=corpus)] == ["bank-t-002"]
    assert search_corpus_local("redis", exclude_topics=["缓存击穿"], corpus_dir=corpus) == []


def test_search_zero_score_dropped(tmp_path):
    corpus = _make_corpus(tmp_path)
    assert search_corpus_local("量子纠缠", corpus_dir=corpus) == []


def test_search_casefold_both_sides(tmp_path):
    corpus = _make_corpus(tmp_path)
    assert [q.id for q in search_corpus_local("KAFKA", corpus_dir=corpus)] == ["bank-t-001"]


def test_search_top_k(tmp_path):
    corpus = _make_corpus(tmp_path)
    assert len(search_corpus_local("Kafka redis 缓存", top_k=1, corpus_dir=corpus)) == 1


# vendor 注册与路由

def test_vendor_registered_and_routable(tmp_path):
    corpus = _make_corpus(tmp_path)
    assert get_vendor_chain("search_corpus", "local") == [search_corpus_local]
    hits = route_to_vendor("search_corpus", "local", "Kafka", corpus_dir=corpus)
    assert hits[0].id == "bank-t-001"


def test_vendor_unknown_name_fails_loud():
    with pytest.raises(SourceNotConfiguredError):
        get_vendor_chain("search_corpus", "不存在的源")


# topics_of 与第二方法注册 三态哨兵的数据源）

def test_topics_of_lists_all_topics(tmp_path):
    """考点清单 = 全部题的 topic，顺序与解析顺序一致（单行清单的原料）。"""
    assert topics_of(_make_corpus(tmp_path)) == ["Kafka 高可用", "缓存击穿"]


def test_corpus_topics_registered_and_routable(tmp_path):
    """第二方法走同一注册表路由——bank_tool 的清单探测经 route_to_vendor 到这里。"""
    hits = route_to_vendor("corpus_topics", "local", corpus_dir=_make_corpus(tmp_path))
    assert hits == ["Kafka 高可用", "缓存击穿"]


# 换心批：BM25F 行为 + BankIndex 缓存

def test_search_full_sentence_query(tmp_path):
    """整句中文 query：不再要求"考点关键词"，自然语句靠 bigram 重叠命中——
    "保证消息不丢"与题面共享 保证/证消/消息/息不/不丢 五个块。"""
    corpus = _make_corpus(tmp_path)
    hits = search_corpus_local(
        "三节点集群的 leader 挂了怎么保证消息不丢", corpus_dir=corpus
    )
    assert hits and hits[0].id == "bank-t-001"


def test_topic_exact_match_passthrough_beats_tag_heavy_rival(tmp_path):
    """直通判别用例（审计反例的缩微版）：query 与考点全等时无条件第一——
    竞品 tags 里塞着 query 的核心词也压不过。bigram 粒度下竞品靠
    "状态/态管/管理"三字段命中本来会反超约 20%，直通兜底。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(
        "---\nid: bank-t-201\ntopic: Agent 状态管理\ndifficulty: 3\n"
        "tags: langgraph\n\n**题目**\n状态怎么维护\n",
        encoding="utf-8",
    )
    (d / "b.md").write_text(
        "---\nid: bank-t-202\ntopic: 前端状态管理\ndifficulty: 3\n"
        "tags: 状态管理, 前端\n\n**题目**\n前端状态与全局管理\n",
        encoding="utf-8",
    )
    hits = search_corpus_local("Agent 状态管理", corpus_dir=str(tmp_path))
    assert hits[0].id == "bank-t-201"


def test_index_cache_reuses_until_fingerprint_changes(tmp_path, monkeypatch):
    """缓存合同：指纹不变 → 复用（零 parse）；新增文件 → 指纹变 → 重建，
    新题立即可见（mtime 缓存最常见的坑就是"改了不生效"，此处锁死）。"""
    import interviewer.dataflows.local_corpus as lc

    corpus = _make_corpus(tmp_path)
    assert search_corpus_local("宕机", corpus_dir=corpus)[0].id == "bank-t-001"

    calls = {"n": 0}
    real_parse = lc.parse_corpus

    def _counting(corpus_dir):
        calls["n"] += 1
        return real_parse(corpus_dir)

    monkeypatch.setattr(lc, "parse_corpus", _counting)
    search_corpus_local("宕机", corpus_dir=corpus)          # 指纹未变 → 缓存命中
    assert calls["n"] == 0

    (tmp_path / "project" / "b.md").write_text(
        "---\nid: bank-t-003\ntopic: 消息丢失\n\n**题目**\n消息丢了怎么办\n",
        encoding="utf-8",
    )
    hits = search_corpus_local("消息丢失", corpus_dir=corpus)
    assert calls["n"] >= 1                                  # 新文件 → 重 parse
    assert any(q.id == "bank-t-003" for q in hits)


def test_bad_corpus_not_cached_fails_loud(tmp_path):
    """坏库不缓存短路 fail-loud：修好之前每次检索都重新 parse、重新炸
（若缓存了失败，第二次会静默返回空——那是最阴的退化）。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "bad.md").write_text(
        "---\nid: bank-bad\ntopic: 坏块\ndifficulty: 9\n\n**题目**\n越界\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="坏块"):
        search_corpus_local("任意", corpus_dir=str(tmp_path))
    with pytest.raises(ValueError, match="坏块"):
        search_corpus_local("任意", corpus_dir=str(tmp_path))
