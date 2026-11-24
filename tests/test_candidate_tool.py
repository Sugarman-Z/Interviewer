"""第二检索面工具层单测：双面孔契约 + 账本 + 跨文件 prompt 锚。
与 test_bank_tool 的分工对称：bank 面的工具合同在那边，candidate 面在
这边——两副工具共享 LoopTool seam，但各自的哨兵/账本/格式是独立合同。
"""
from interviewer.agents.prompts.issue_question import ISSUE_QUESTION_CANDIDATE_ADDENDUM
from interviewer.dataflows import resolve_candidate_tool
from interviewer.dataflows.candidate_tool import (
    CANDIDATE_NO_RESULTS,
    CandidateSearchTool,
    _format_chunk,
)
from interviewer.dataflows.candidate_corpus import CandidateChunk, chunk_texts
from interviewer.dataflows.interface import CANDIDATE_TOOL_NAME

_RESUME = """项目经验
负责交易系统性能优化，把订单处理延迟从 50ms 降到 8ms。

教育背景
GWU 计算机硕士"""

_JD = """岗位要求
熟悉 Kafka 等消息队列。"""

_CFG = {"candidate_search_enabled": True, "candidate_vendors": "local",
        "candidate_top_k": 3}
_TEXTS = {"resume": _RESUME, "jd": _JD}


def _tool():
    return CandidateSearchTool(_CFG, _TEXTS)


# 组装入口

def test_resolve_candidate_tool_off_returns_none():
    assert resolve_candidate_tool({"candidate_search_enabled": False}) is None
    assert resolve_candidate_tool({}) is None   # 缺席 = 关（存在性即开关）


def test_resolve_candidate_tool_on_returns_factory():
    factory = resolve_candidate_tool(_CFG)
    bound = factory(_TEXTS)
    assert hasattr(bound, "as_tool") and hasattr(bound, "execute")
    assert bound.registry == {}   # 新账本随行：跨签发复用 = 脏账


def test_resolve_unknown_candidate_vendor_fails_loud():
    import pytest
    from interviewer.dataflows.errors import SourceNotConfiguredError
    with pytest.raises(SourceNotConfiguredError):
        resolve_candidate_tool(dict(_CFG, candidate_vendors="不存在的源"))


# 双面孔契约

def test_tool_face_exposes_only_query_and_name():
    tool = _tool().as_tool()
    assert tool.name == CANDIDATE_TOOL_NAME
    assert list(tool.args.keys()) == ["query"]   # texts/exclude_ids 不可见


def test_run_returns_md_with_annotation_and_verbatim():
    md, chunks = _tool().run("性能优化")
    assert chunks and chunks[0].id == "cand-0001"
    assert md.startswith("[cand-0001] 简历第1段")
    assert "字符 " in md.splitlines()[0]           # 标注行带可核验区间
    assert "负责交易系统性能优化" in md             # 段落原文逐字（引用证据）


def test_run_empty_returns_sentinel():
    md, chunks = _tool().run("量子纠缠")
    assert md == CANDIDATE_NO_RESULTS and chunks == []


def test_jd_chunks_labeled_separately():
    md, _ = _tool().run("Kafka")
    assert "JD第1段" in md


def test_as_tool_invoke_returns_str():
    out = _tool().as_tool().invoke({"query": "Kafka"})
    assert isinstance(out, str) and out.startswith("[cand-")


# 循环期适配器：账本与留痕

def test_execute_record_shape_and_registry_accumulates():
    bound = _tool().for_call()
    md, rec = bound.execute({"query": "性能优化"})
    assert rec["query"] == "性能优化"
    assert rec["hit_ids"] == ["cand-0001"]
    assert rec["empty_hit"] is False and rec["elapsed_ms"] >= 0
    assert set(bound.registry) == {"cand-0001"}
    # 累积型：第二次检索并入同一账本（节点出环后统一对账）
    bound.execute({"query": "Kafka"})
    assert set(bound.registry) == {"cand-0001", "cand-0003"}


def test_execute_empty_hit_recorded():
    _, rec = _tool().for_call().execute({"query": "量子纠缠"})
    assert rec["empty_hit"] is True and rec["hit_ids"] == []


def test_format_chunk_contract():
    c = CandidateChunk(id="cand-0042", source="jd", para=3, start=10, end=99,
                       text="某 JD 条款原文")
    out = _format_chunk(c)
    assert out.startswith("[cand-0042] JD第3段（字符 10-99）\n某 JD 条款原文")


# 与 prompt ADDENDUM 的跨文件锚（tool 名/哨兵/编号前缀是字符串约定）

def test_addendum_contract_anchor():
    """ADDENDUM 写死了工具名、空哨兵与编号前缀的用法——三者在两侧必须对得
    上（test_bank_tool.test_addendum_contract_anchor 的 candidate 版）。"""
    assert CANDIDATE_TOOL_NAME in ISSUE_QUESTION_CANDIDATE_ADDENDUM
    assert CANDIDATE_NO_RESULTS.split("：")[0] in ISSUE_QUESTION_CANDIDATE_ADDENDUM
    assert "cand-XXXX" in ISSUE_QUESTION_CANDIDATE_ADDENDUM


def test_factory_yields_fresh_ledger_per_call():
    """脏账防线（bank 面 for_call 同款纪律）：工厂每次调用产出独立账本——
    上一轮检索喂进的段落不得混入下一轮对账表（跨签发复用 = 脏账）。"""
    f = resolve_candidate_tool(_CFG)
    b1, b2 = f(_TEXTS), f(_TEXTS)
    b1.execute({"query": "性能优化"})
    assert b1.registry and not b2.registry


def test_chunk_ids_shape_matches_sanitize_regex():
    r"""issue_question._sanitize_candidate_refs 用 cand-\d{4,} 对账——切块器
    产出的 id 必须逐个落在该形状内（编号位数升级 = 两侧同批改）。形状锁
    import 被锁的正则本体断言（2026-11-17 审查：本地复制正则 = 自证，
    单改一侧照绿）。"""
    from interviewer.agents.issue_question import _CAND_ID_RE

    assert _CAND_ID_RE.pattern == r"cand-\d{4,}"   # 位数下限 4、上界开放
    for c in chunk_texts(_TEXTS):
        assert _CAND_ID_RE.fullmatch(c.id)
