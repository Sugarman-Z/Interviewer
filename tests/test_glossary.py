"""术语词表单测：加载校验 fail-loud + 变体规则 + 题库同步锁。
题库同步锁是本模块的核心测试：promote 硬闸以词表为基准拒绝未知 tag，
基准本身必须先对现行库成立——题库加 tag 不入表，这里当场红。
"""
from pathlib import Path

import pytest

from interviewer.dataflows.glossary import Glossary, load_glossary
from interviewer.dataflows.local_corpus import parse_corpus

REPO = Path(__file__).resolve().parents[1]


def _real() -> Glossary:
    return load_glossary(REPO / "data" / "glossary.yaml")


# 加载与校验

def test_load_real_glossary_covers_all_bank_tags():
    """题库↔词表同步锁：现行库全部 tag 都在表（promote 硬闸的基准先成立）。"""
    gl = _real()
    questions = parse_corpus(REPO / "corpus")
    unknown = gl.unknown_tags(sorted({t for q in questions for t in q.tags}))
    assert unknown == [], f"tag 未入词表（先在 data/glossary.yaml 注册）：{unknown}"


def test_covers_is_casefold_and_alias_aware():
    gl = _real()
    assert gl.covers("Kafka") and gl.covers("消息队列") and gl.covers("MQ")
    assert gl.covers("gc") and gl.covers("GC")          # 垃圾回收的别名
    assert not gl.covers("不存在的面")


def test_unknown_tags_preserves_order_and_dedups():
    gl = Glossary(faces={"a": ["a"]}, surfaces={"a": "a"})
    assert gl.unknown_tags(["x", "x", "a", "y"]) == ["x", "y"]


def test_load_fail_loud_on_bad_shapes(tmp_path):
    def _yaml(name, text):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        return p

    with pytest.raises(ValueError, match="entries"):
        load_glossary(_yaml("g1.yaml", "foo: 1"))
    with pytest.raises(ValueError, match="entries"):
        load_glossary(_yaml("g2.yaml", "entries: []"))
    with pytest.raises(ValueError, match="aliases"):
        load_glossary(_yaml("g3.yaml", "entries:\n- canonical: a\n  aliases: [1]"))
    # 同一个面挂两个 canonical：归一会随机指向，必须当场炸
    with pytest.raises(ValueError, match="同时挂"):
        load_glossary(_yaml("g4.yaml",
                            "entries:\n- canonical: a\n  aliases: [x]\n"
                            "- canonical: b\n  aliases: [x]"))
    # 同一 canonical 两条：后条静默覆盖前条的换面池（faces 覆盖、surfaces
    # 残留），变体生成单向残缺零报警——表内自相矛盾的漏网形态（审查抓到）
    with pytest.raises(ValueError, match="第二条"):
        load_glossary(_yaml("g5.yaml",
                            "entries:\n- canonical: a\n  aliases: [x]\n"
                            "- canonical: a\n  aliases: [y]"))
    # 键名拼错（alias: 单数）：静默当无别名 = 词表面悄悄丢一半，当场炸
    with pytest.raises(ValueError, match="未知键"):
        load_glossary(_yaml("g6.yaml", "entries:\n- canonical: a\n  alias: [x]"))


# 变体规则（确定性 + 护栏）

def test_variant_swaps_alias_and_canonical():
    gl = _real()
    assert "js 闭包" in gl.variant_queries("javascript 闭包")
    assert "javascript 闭包" in gl.variant_queries("js 闭包")


def test_variant_chinese_to_english():
    vs = _real().variant_queries("消息队列 积压 处理")
    assert "message queue 积压 处理" in vs
    assert "mq 积压 处理" in vs


def test_variant_one_swap_at_a_time():
    """一次只换一个面（控 precision）：两个可换点的 query 产出两个单换
    变体，不产出双重替换。"""
    vs = _real().variant_queries("js 消息队列")
    assert "javascript 消息队列" in vs
    assert "js message queue" in vs
    assert "javascript message queue" not in vs


def test_variant_latin_boundary_guard():
    """拉丁面词边界护栏："os"/"js" 不得切中更长单词内部。"""
    gl = _real()
    assert gl.variant_queries("postgres") == []          # os ⊂ postgres 不触发
    vs = gl.variant_queries("json 数据结构")
    assert "json data structure" in vs
    assert not any("javascript" in v for v in vs)        # js ⊂ json 不触发


def test_latin_surface_adjacent_to_cjk_still_hits():
    """边界字符只认 [A-Za-z0-9]：汉字不是边界字符。拉丁/混合面紧贴汉字
    必须照常命中（审查抓到的漏配：裸 str.isalnum() 对汉字恒 True，把
    "api设计怎么做"里的 "api设计"、"js闭包原理"里的 "js" 整面判无界——
    LLM 检索串天然无空格混排，mine 的 suggested_tags 正吃这类 query）。"""
    gl = _real()
    assert any(surface == "api设计" for surface, _ in gl.surface_hits("api设计怎么做"))
    assert "javascript闭包原理" in gl.variant_queries("js闭包原理")
    assert gl.variant_queries("gc调优策略")          # gc 紧贴"调"也命中


def test_query_side_casefold_hit_path():
    """query 侧 casefold 的命中路径（审查 mutation 实证：去掉 query.casefold()
    全套 288 测试照绿，但真库 40+ 个 topic 含大写面——'RAG 失败模式分析'
    'GC 调优' 全部当场失明）。"""
    vs = _real().variant_queries("GC 调优策略")
    assert "垃圾回收 调优策略" in vs
    assert "garbage collection 调优策略" in vs


def test_before_side_boundary_guard():
    """护栏两侧都要查的 before 侧专属锁：以 os/js 结尾的普通英文词（macros）
    尾部不得被当缩写切走（审查 mutation：去掉 before 侧检查后
    'macros' 产出 'macr操作系统' 垃圾变体灌进金标）。"""
    gl = _real()
    assert gl.variant_queries("macros") == []
    assert gl.variant_queries("my macros") == []


def test_replace_skipped_when_casefold_changes_length():
    """casefold 非等长字符（ß→ss）使 pos 与原串错位：长度不等时放弃该
    换面点（审查复现：'straße kafka 集群' 产出 'kkafka' 乱码变体静默
    进金标）——宁少产变体，不产错位变体。"""
    gl = Glossary(
        faces={"kafka": ["kafka", "kafka streams"]},
        surfaces={"kafka": "kafka", "kafka streams": "kafka"},
    )
    assert gl.variant_queries("straße kafka 集群") == []          # 错位防护
    assert "strasse kafka streams 集群" in gl.variant_queries("strasse kafka 集群")


def test_variant_no_surface_returns_empty():
    assert _real().variant_queries("量子纠缠") == []


def test_chinese_surface_hits_without_space_boundary():
    """汉字 str.isalnum() 恒 True 的坑（测试抓过的真 bug）：中文面后跟
    汉字必须照样命中——词边界护栏只属于拉丁面，对中文面是无义概念。"""
    gl = _real()
    vs = gl.variant_queries("垃圾回收的调优策略")   # 「垃圾回收」后跟「的」
    assert "gc的调优策略" in vs
    assert "garbage collection的调优策略" in vs


def test_variant_longest_surface_first_with_subface():
    """长面优先：'缓存雪崩' 与其子面 '缓存' 是两个独立换面点，各产变体。"""
    gl = Glossary(
        faces={"缓存雪崩": ["缓存雪崩", "cache stampede"], "缓存": ["缓存", "cache"]},
        surfaces={"缓存雪崩": "缓存雪崩", "cache stampede": "缓存雪崩",
                  "缓存": "缓存", "cache": "缓存"},
    )
    vs = gl.variant_queries("缓存雪崩怎么办")
    assert "cache stampede怎么办" in vs
    assert "cache雪崩怎么办" in vs
