"""工具层单测：BankSearchTool 双面孔契约 + resolve_search_tool 组装入口。
锁的合同：模型面只暴露 query（exclude_topics 硬约束不可见）；run 返回
(markdown, entries)，空命中 = (哨兵, [])，绝不 ("", [])；markdown 首行 [id]
（模型回填 bank_qid 的视觉锚）；exclude_topics 是考点全名精确匹配；
工具名/哨兵与 ADDENDUM prompt 的跨文件锚（改名必须两侧同批）。
"""
from pathlib import Path

import pytest

from interviewer.dataflows import resolve_search_tool
from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.bank_tool import (
    BANK_CORRUPTED,
    NO_RESULTS_AVAILABLE,
    NO_RESULTS_EXCLUDED,
    _format_entry,
)
from interviewer.dataflows.errors import SourceNotConfiguredError
from interviewer.agents.prompts.issue_question import ISSUE_QUESTION_TOOLS_ADDENDUM

_BLOCK = """---
id: bank-t-101
topic: Kafka 高可用
difficulty: 3
tags: kafka, 消息队列

**题目**
三节点集群 leader 宕机时如何保证消息不丢？

**追问树**
- ISR 与 acks 的关系？

**评分锚点**
- 5 分：说清联动
"""

_BLOCK_B = """---
id: bank-t-102
topic: 缓存击穿
difficulty: 4
tags: redis, 缓存

**题目**
热点 key 过期瞬间请求穿透数据库怎么办？
"""


def _make_tool(tmp_path: Path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK, encoding="utf-8")
    cfg = {"corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    return resolve_search_tool(cfg)


# resolve_search_tool 组装入口

def test_resolve_off_returns_none(tmp_path):
    """corpus_vendors 空串 = 工具路径整体关闭（IssueQuestion 走无工具旧路径的开关）。"""
    assert resolve_search_tool({"corpus_vendors": "", "corpus_dir": str(tmp_path)}) is None


def test_resolve_returns_tool(tmp_path):
    tool = _make_tool(tmp_path)
    assert tool is not None and hasattr(tool, "as_tool") and hasattr(tool, "run")


def test_resolve_unknown_vendor_fails_loud(tmp_path):
    """链名拼错在建图时炸（错误左移），不留给面试进行到一半。"""
    cfg = {"corpus_vendors": "local,不存在的源", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    with pytest.raises(SourceNotConfiguredError):
        resolve_search_tool(cfg)


# 双面孔契约

def test_tool_face_exposes_only_query(tmp_path):
    tool = _make_tool(tmp_path).as_tool()
    assert list(tool.args.keys()) == ["query"]          # exclude_topics 不可见
    assert tool.name == "search_corpus"


def test_run_returns_md_and_entries(tmp_path):
    md, entries = _make_tool(tmp_path).run("Kafka 宕机")
    assert isinstance(md, str) and md.startswith("[bank-t-101]")
    assert [e.id for e in entries] == ["bank-t-101"]
    assert isinstance(entries[0], BankQuestion)


def test_run_empty_returns_sentinel_with_topics(tmp_path):
    """真没有/表述不匹配态：哨兵 + 未考考点清单——模型对照清单
    自行区分"改写重试"与"按材料生成"（代码不可判定，交给清单）。"""
    md, entries = _make_tool(tmp_path).run("量子纠缠")
    assert md.startswith(NO_RESULTS_AVAILABLE) and entries == []
    assert "题库现有考点" in md and "Kafka 高可用" in md


def test_tool_invoke_returns_str_not_tuple(tmp_path):
    out = _make_tool(tmp_path).as_tool().invoke({"query": "Kafka"})
    assert isinstance(out, str) and out.startswith("[bank-t-101]")  # 模型只拿 markdown 半张脸


def test_run_exclude_topics_exact_match(tmp_path):
    """排除是考点全名精确匹配：排除"Kafka 高可用"触发 EXCLUDED 哨兵
    三态：命中过但全被查重吃掉）；排除近似名"Kafka"不影响正常命中。"""
    tool = _make_tool(tmp_path)
    assert tool.run("kafka", exclude_topics=("Kafka 高可用",))[0].startswith(NO_RESULTS_EXCLUDED)
    assert tool.run("kafka", exclude_topics=("Kafka",))[0].startswith("[bank-t-101]")


# markdown 格式（模型逐字消费的"A"）

def test_format_entry_id_first_and_omits_empty_sections():
    q = BankQuestion(id="bank-x-1", topic="测试考点", text="题目原文")
    out = _format_entry(q)
    assert out.startswith("[bank-x-1]")
    assert "题目：题目原文" in out
    assert "追问树" not in out and "评分锚点" not in out and "出处" not in out  # 空段整行省略


def test_format_entry_never_shows_scoring_anchors():
    """锚点最小知识 真问题②）：锚点**存在**也不得出现在模型视野——
    答案钥匙 + 题面允许改编 = 零防线邀请抄录。锚点今天没有评分侧消费方
（Evaluator 只读 followup_hints，IssuedQuestion 无锚点字段），删行
    零影响；W5 校准要用锚点时另铺评分通道，不回这张脸。"""
    q = BankQuestion(id="bank-x-2", topic="测试考点", text="题目原文",
                     scoring_anchors=["答案钥匙原句不应外泄XYZ"])
    out = _format_entry(q)
    assert "评分锚点" not in out
    assert "答案钥匙原句不应外泄XYZ" not in out


# 与 ADDENDUM prompt 的跨文件锚（tool 名/哨兵是纯字符串约定）

def test_addendum_contract_anchor():
    """ADDENDUM 写死了工具名与哨兵的行为——本模块的常量必须与它对得上
 起两个哨兵各持一个锚）。"""
    assert "search_corpus(" in ISSUE_QUESTION_TOOLS_ADDENDUM
    assert "NO_RESULTS_AVAILABLE" in ISSUE_QUESTION_TOOLS_ADDENDUM
    assert "NO_RESULTS_EXCLUDED" in ISSUE_QUESTION_TOOLS_ADDENDUM
    assert "BANK_CORRUPTED" in ISSUE_QUESTION_TOOLS_ADDENDUM    # 三哨兵纪律
    assert NO_RESULTS_AVAILABLE.startswith("NO_RESULTS_AVAILABLE")
    assert NO_RESULTS_EXCLUDED.startswith("NO_RESULTS_EXCLUDED")
    assert BANK_CORRUPTED.startswith("BANK_CORRUPTED")


# 循环期适配器（LoopTool 的 bank 实现）

def test_for_call_binds_exclude_and_fresh_registry(tmp_path):
    """for_call 三合同：查重闭包生效 + 账本全新 + 适配器两副面孔齐。
     三态：exclude 命中 → EXCLUDED 哨兵；二次探测命中的已考考点
    **不进 entries/registry**（模型无从合法引用其编号，报了即编造）。"""
    bound = _make_tool(tmp_path).for_call(exclude_topics=("Kafka 高可用",))
    assert bound.registry == {}                          # 新账本，不带历史
    md, record = bound.execute({"query": "kafka"})
    assert md.startswith(NO_RESULTS_EXCLUDED)            # 闭包生效：已考考点被排除
    assert record is not None and record["empty_hit"] is True
    assert record["empty_state"] == "excluded"
    assert record["excluded_hit_ids"] == ["bank-t-101"]  # 归因走独立字段
    assert record["hit_ids"] == [] and isinstance(record["elapsed_ms"], (int, float))
    assert bound.registry == {}                          # 已考考点不进防幻觉账本


def test_for_call_registry_accumulates_across_calls(tmp_path):
    """累积型对账账本：同轮多次检索的命中都进账（防幻觉盖帽的粮）。"""
    bound = _make_tool(tmp_path).for_call()
    bound.execute({"query": "Kafka"})
    bound.execute({"query": "kafka 集群"})
    assert list(bound.registry) == ["bank-t-101"]


def test_two_for_call_accounts_isolated(tmp_path):
    """单次调用对象合同：两次 for_call 账本必须隔离——复用旧适配器=脏账。"""
    tool = _make_tool(tmp_path)
    b1, b2 = tool.for_call(), tool.for_call()
    b1.execute({"query": "Kafka"})
    assert b1.registry != {} and b2.registry == {}


# 三态哨兵细节

def test_excluded_sentinel_lists_only_remaining_topics(tmp_path):
    """EXCLUDED 态的清单是与已考考点的差集：只列未考考点（已考的不该再被建议）。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK + "\n" + _BLOCK_B, encoding="utf-8")
    tool = resolve_search_tool(
        {"corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    )
    md, entries = tool.run("Kafka 宕机", exclude_topics=("Kafka 高可用",))
    assert md.startswith(NO_RESULTS_EXCLUDED) and entries == []
    assert "缓存击穿" in md          # 未考考点在清单里
    assert "Kafka 高可用" not in md  # 已考考点不在（差集）


def test_topics_line_degrades_to_empty(monkeypatch, tmp_path):
    """清单是增强件：corpus_topics 方法缺席（如未来混合链的未注册方）→
    哨兵本体自洽、不带清单行也不炸检索（fail-open 落点在题库增强，不在检索本体）。"""
    from interviewer.dataflows import interface

    monkeypatch.delitem(interface._VENDOR_METHODS["corpus_topics"], "local")
    tool = _make_tool(tmp_path)
    md, entries = tool.run("量子纠缠")
    assert md == NO_RESULTS_AVAILABLE and entries == []   # 无清单时哨兵即全部内容


def test_topics_line_probes_per_vendor_not_whole_chain(monkeypatch, tmp_path):
    """混合链不被未注册方连坐（TODO C3 原文，2026-11-17 全量审查补锁）：
    链上首个 vendor 未注册 corpus_topics（dense/hybrid 的现实形状——它们
    只注册 search_corpus）时，清单必须由注册了的 local 腿给出。若 _topics_line
    退回整链路由（get_vendor_chain 对首腿抛错整链炸→清单空），本测试红。"""
    from interviewer.dataflows import interface

    def _no_topics_leg(query, *, exclude_topics=(), top_k=5, corpus_dir="./corpus"):
        return []   # 只注册 search_corpus 的腿：检索能答、清单方法缺席

    monkeypatch.setitem(
        interface._VENDOR_METHODS["search_corpus"], "fakeleg", _no_topics_leg
    )
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK, encoding="utf-8")
    tool = resolve_search_tool(
        {"corpus_vendors": "fakeleg,local", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    )

    md, entries = tool.run("量子纠缠")                # 空命中 → AVAILABLE 哨兵 + 清单行
    assert md.startswith(NO_RESULTS_AVAILABLE) and entries == []
    assert "Kafka 高可用" in md                        # 清单由 local 腿答出，没被首腿连坐


# 组卷层后处理开关⑤，2026-11-17）

def test_dedup_skin_swap_yields_excluded_sentinel(monkeypatch, tmp_path):
    """查重 v1.5 端到端（换皮 dodge 场景）：已考"Kafka 高可用"，候选
    "缓存击穿"在 stub 向量里与已考同向（= 同题换皮）→ 被 v1.5 剔除 →
    三态探测判 EXCLUDED（不是 AVAILABLE——库里有题，是被查重吃掉的）。"""
    from interviewer.dataflows import interface

    def _vbt(topics, *, corpus_dir="./corpus"):
        m = {"Kafka 高可用": [1.0, 0.0], "缓存击穿": [1.0, 0.0]}
        return {t: m[t] for t in topics if t in m}

    monkeypatch.setitem(
        interface._VENDOR_METHODS.setdefault("vectors_by_topic", {}), "local", _vbt)
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK + "\n" + _BLOCK_B, encoding="utf-8")
    tool = resolve_search_tool({
        "corpus_vendors": "local", "corpus_dir": str(tmp_path),
        "bank_top_k": 5, "bank_topic_dedup": True,
    })

    md, entries = tool.run("kafka 缓存", exclude_topics=("Kafka 高可用",))
    assert md.startswith(NO_RESULTS_EXCLUDED) and entries == []


def test_dedup_flag_off_keeps_candidate(tmp_path):
    """开关关（配置缺键默认关）→ 同场景下候选正常出题：v1 精确匹配只排
    已考考点本身，零回归是开关的默认态。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK + "\n" + _BLOCK_B, encoding="utf-8")
    tool = resolve_search_tool({
        "corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5,
    })

    md, entries = tool.run("kafka 缓存", exclude_topics=("Kafka 高可用",))
    assert [e.id for e in entries] == ["bank-t-102"]


def test_mmr_flag_wiring_reorders(monkeypatch, tmp_path):
    """bank_mmr 接线：开启 + question_vectors 可用 → 池内按 MMR 重排
（stub 向量把 t-102 拉成与 query 同向 → 它必须登顶）。"""
    from interviewer.dataflows import interface

    def _qv(query, *, ids, corpus_dir="./corpus"):
        return [1.0, 0.0], {"bank-t-101": [0.0, 1.0], "bank-t-102": [1.0, 0.0]}

    monkeypatch.setitem(
        interface._VENDOR_METHODS.setdefault("question_vectors", {}), "local", _qv)
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK + "\n" + _BLOCK_B, encoding="utf-8")
    tool = resolve_search_tool({
        "corpus_vendors": "local", "corpus_dir": str(tmp_path),
        "bank_top_k": 5, "bank_mmr": True,
    })

    md, entries = tool.run("kafka 缓存")
    assert entries and entries[0].id == "bank-t-102"   # MMR 重排生效
    assert sorted(e.id for e in entries) == ["bank-t-101", "bank-t-102"]


def test_corrupt_corpus_degrades_to_corrupt_sentinel(tmp_path):
    """题库任何时点解析失败都不阻塞流程——降级
    BANK_CORRUPTED 哨兵（模型转材料出题），留痕带 error 键，registry 不进账
（模型此时报任何 bank_qid 都判编造，防幻觉语义保持）。"""
    tool = _make_tool(tmp_path)                          # 建库（a.md）
    (tmp_path / "project" / "b.md").write_text(_BLOCK, encoding="utf-8")  # 同 id → 坏
    md, entries, probe = tool.run_with_probe("Kafka")
    assert md.startswith(BANK_CORRUPTED) and entries == []
    assert probe["state"] == "corrupt" and probe["detail"]
    bound = tool.for_call()
    out, record = bound.execute({"query": "Kafka"})
    assert out.startswith(BANK_CORRUPTED)
    assert record["error"] == "corpus_parse_failed"
    assert record["hit_ids"] == [] and record["empty_state"] == "corrupt"
    assert bound.registry == {}
    # as_tool 模型面同样降级（直调脚本/测试入口也不被坏库炸）
    assert tool.as_tool().invoke({"query": "Kafka"}).startswith(BANK_CORRUPTED)


def test_corrupt_corpus_self_heals_after_repair(tmp_path):
    """修复即恢复：坏库不缓存（每次检索重解析），把重复 id 修掉后下一次
    检索立刻回到正常命中——降级是每调用级别的，不是进程级开关。"""
    tool = _make_tool(tmp_path)                          # 建库（a.md）
    bad = tmp_path / "project" / "b.md"
    bad.write_text(_BLOCK, encoding="utf-8")             # 同 id → 坏
    assert tool.run("Kafka")[0].startswith(BANK_CORRUPTED)
    bad.write_text(_BLOCK_B, encoding="utf-8")             # 换 id → 修好
    md, entries = tool.run("缓存")
    assert not md.startswith(BANK_CORRUPTED) and entries


def test_corrupt_field_translation_also_degrades(tmp_path):
    """difficulty 非数字（int() 翻译失败）同样降级——2026-11-18 审查抓到的
    逃逸：该形态曾以裸 ValueError 穿出窄捕获（读取切块在收口点之外），
    修复后与重复 id 同走 BANK_CORRUPTED 第四态。"""
    tool = _make_tool(tmp_path)
    bad = tmp_path / "project" / "b.md"
    bad.write_text(_BLOCK.replace("difficulty: 3", "difficulty: 高"), encoding="utf-8")
    md, record = tool.for_call().execute({"query": "Kafka"})
    assert md.startswith(BANK_CORRUPTED)
    assert record["error"] == "corpus_parse_failed"


# 远域整单闸

def _far_gate_tool(tmp_path, on=True):
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK, encoding="utf-8")
    return resolve_search_tool({
        "corpus_vendors": "local", "corpus_dir": str(tmp_path),
        "bank_top_k": 5, "bank_far_gate": on,
    })


def test_far_gate_empties_garbage_hits(monkeypatch, tmp_path):
    """闸开 + s1 低于阈值：BM25 的垃圾命中整批转 AVAILABLE 哨兵（模型侧
    行为与 state=none 零差异——同一个哨兵、同一份考点清单附件），留痕
    state=far_gated 单列；命中不进 entries/registry（防幻觉语义保持：
    模型没见过的 id 报了即编造）。"""
    from interviewer.dataflows import relevance_gate

    tool = _far_gate_tool(tmp_path)
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", lambda q, c: 0.2)
    md, entries, probe = tool.run_with_probe("Kafka")
    assert md.startswith(NO_RESULTS_AVAILABLE) and entries == []
    assert probe["state"] == "far_gated"
    assert "题库现有考点" in md                 # 清单附件同走空哨兵合同
    bound = tool.for_call()
    out, record = bound.execute({"query": "Kafka"})
    assert out.startswith(NO_RESULTS_AVAILABLE)
    assert record["empty_state"] == "far_gated" and record["hit_ids"] == []
    assert bound.registry == {}


def test_far_gate_passes_when_relevant(monkeypatch, tmp_path):
    """s1 ≥ 阈值 → 命中清单原样放行：整单闸不动清单——keyword/synonym 的
    dense 绝对分天然低，逐条过滤会砍 local 的 BM25 主场（这是选    整单闸而不是逐条闸的原因，实验数据见 leak_gate_2x2_2026-11-18.json）。"""
    from interviewer.dataflows import relevance_gate

    tool = _far_gate_tool(tmp_path)
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", lambda q, c: 0.9)
    md, entries = tool.run("Kafka")
    assert md.startswith("[bank-t-101]") and [e.id for e in entries] == ["bank-t-101"]


def test_far_gate_fails_open_and_flag_off(monkeypatch, tmp_path):
    """闸不可用（fastembed/numpy 缺席 → None）fail-open 放行；配置缺键
（老配置字典）闸关且根本不碰打分器——增强件缺席不炸检索本体。"""
    from interviewer.dataflows import relevance_gate

    tool = _far_gate_tool(tmp_path)             # 闸开
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", lambda q, c: None)
    assert tool.run("Kafka")[0].startswith("[bank-t-101]")

    def _must_not_be_called(q, c):
        raise AssertionError("闸关着不该碰打分器")

    plain = resolve_search_tool({                 # 同库、配置不带 bank_far_gate → 关
        "corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5})
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", _must_not_be_called)
    assert plain.run("Kafka")[0].startswith("[bank-t-101]")


def test_mmr_no_vectors_truncates_to_topk(tmp_path):
    """开关开但向量方法缺席（local 链常态）→ 原序截断：取题深度已放大到
    池深，交回模型的必须仍是 bank_top_k 条以内——池子漏出去就是 bug。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK + "\n" + _BLOCK_B, encoding="utf-8")
    tool = resolve_search_tool({
        "corpus_vendors": "local", "corpus_dir": str(tmp_path),
        "bank_top_k": 5, "bank_mmr": True, "bank_topic_dedup": True,
    })

    md, entries = tool.run("kafka 缓存")
    assert sorted(e.id for e in entries) == ["bank-t-101", "bank-t-102"]
    assert len(entries) <= 5
