"""工具循环单测：两条腿 + 军规 + 分派防御。
FakeLLM 剧本法（ScriptedLLM）：逐拍回放预设 AIMessage，同时记录每拍收到
的 messages——"模型第二拍真的看到了检索结果"（RAG 的 A）靠这份记录断言。
工具以 LoopTool 适配器入环（tool.for_call() 造）；registry 从 bound.registry
读（累积账本归工具），trace 从返回值读（循环补 "tool" 键，顺序 = 执行顺序）；
新增分派防御五连：空 tools / 缺两副面孔 / 重名 / 撞 schema 类名 / 双工具同环。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from interviewer.agents.schemas import IssuedQuestion
from interviewer.agents.utils.structured import invoke_structured_with_tools
from interviewer.dataflows import resolve_search_tool
from interviewer.dataflows.bank_tool import NO_RESULTS_AVAILABLE

_BLOCK = """---
id: bank-t-101
topic: Kafka 高可用
difficulty: 3
tags: kafka, 消息队列

**题目**
三节点集群 leader 宕机时如何保证消息不丢？
"""


def _make_tool(tmp_path: Path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK, encoding="utf-8")
    return resolve_search_tool(
        {"corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    )


class ScriptedLLM:
    """按剧本逐拍回放；bind_tools 透传；记录每拍入参 messages。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.seen.append(list(messages))
        return self.script.pop(0)


def _issue_args(**over):
    base = {
        "done": False, "reason": "t", "module": "project", "topic": "Kafka 高可用",
        "text": "题面", "source_type": "bank", "source_ref": "t", "followup_hints": [],
    }
    base.update(over)
    return base


def _search(query, cid="c1"):
    return AIMessage(content="", tool_calls=[
        {"name": "search_corpus", "args": {"query": query}, "id": cid}])

def _schema(args):
    return AIMessage(content="", tool_calls=[
        {"name": "IssuedQuestion", "args": args, "id": "c2"}])

def _tool_messages(invoke_messages):
    return [m for m in invoke_messages if isinstance(m, ToolMessage)]


# 腿①：查库 → 填表交卷

def test_leg1_search_then_schema_tool(tmp_path):
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_search("Kafka 宕机"), _schema(_issue_args())])
    bound = tool.for_call()
    result, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [bound])
    assert isinstance(result, IssuedQuestion) and result.topic == "Kafka 高可用"
    assert list(bound.registry) == ["bank-t-101"]        # 防幻觉账本有粮
    assert len(llm.seen) == 2                       # 一查一答，恰好 2 次调用
    assert len(llm.bound_tools) == 2                # 菜单上两样：检索工具 + schema


def test_retrieval_results_injected_back(tmp_path):
    """RAG 的 A：第二拍入参里必须带着第一拍检索的 ToolMessage（markdown 首行 [id]）。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_search("Kafka 宕机"), _schema(_issue_args())])
    invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    fed = _tool_messages(llm.seen[1])
    assert fed and fed[0].content.startswith("[bank-t-101]")


# 腿②：直接文本作答

def test_leg2_direct_content(tmp_path):
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([AIMessage(content=json.dumps(_issue_args(topic="技术选型"), ensure_ascii=False))])
    bound = tool.for_call()
    result, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [bound])
    assert isinstance(result, IssuedQuestion) and result.topic == "技术选型"
    assert len(llm.seen) == 1 and bound.registry == {}  # 不检索不多花钱、账本为空
    assert trace == []


def test_unparseable_content_returns_none(tmp_path):
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([AIMessage(content="我不会出题")])
    result, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert result is None and trace == []              # (None, []) 交外层梯子，不 raise


# 军规与异常路况

def test_bad_schema_args_recover_next_round(tmp_path):
    """模型填表走神（缺必填）→ 记 None 继续，下一轮正常交卷——不 raise。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_schema({"done": False}), _schema(_issue_args())])
    result, _ = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert isinstance(result, IssuedQuestion) and len(llm.seen) == 2


def test_transport_error_degrades_to_none(tmp_path):
    """传输层异常降级（2026-11-17 全量审查 medium）：429/超时在 client 退避
    重试耗尽后从 bound.invoke 抛出 → (None, trace) 交调用方降级梯（兜底池/
    熔断），绝不炸——与另两条结构化通道（freetext 两腿全 catch→None）对齐。
    此前这里原样穿出，一次 API 抖动能炸死整场面试。"""
    tool = _make_tool(tmp_path)

    class _TransportBoom(ScriptedLLM):
        def invoke(self, messages, config=None, **kwargs):
            self.seen.append(list(messages))
            raise RuntimeError("429 RateLimit: client 重试耗尽")

    llm = _TransportBoom([])
    result, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert result is None and trace == []   # 与腿②解析失败同点会合，不 raise


def test_bad_schema_submission_still_gets_tool_message(tmp_path):
    """军规①协议级锁定：交卷腿校验失败也必须回 ToolMessage——悬空票号会让下一轮
    请求直接协议错（400）。ScriptedLLM 不走网络看不见协议层，只能从回喂消息的
    形状上锁；回喂内容 = 校验错误本身，模型可据以修正重交（同未知工具腿）。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_schema({"done": False}), _schema(_issue_args())])
    result, _ = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert isinstance(result, IssuedQuestion)              # 走神后仍能恢复交卷
    replies = _tool_messages(llm.seen[1])
    assert any(
        m.tool_call_id == "c2" and m.content.startswith("交卷校验失败") for m in replies
    )


def test_unknown_tool_gets_reply_and_continues(tmp_path):
    """军规①：幻觉菜必须回话（悬空票号=下次请求协议错），模型可继续。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([
        AIMessage(content="", tool_calls=[{"name": "hack", "args": {}, "id": "x"}]),
        _schema(_issue_args()),
    ])
    result, _ = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert isinstance(result, IssuedQuestion)
    assert any(m.content.startswith("未知工具：hack") for m in _tool_messages(llm.seen[1]))


def test_empty_query_feeds_sentinel_back(tmp_path):
    """空 query 放行：自然得到哨兵并回喂——错误也是对话的一部分，模型自会修正。
    ：真没有态哨兵行首前缀不变（考点清单是追加的第二行）。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_search(""), _schema(_issue_args())])
    result, _ = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert isinstance(result, IssuedQuestion)
    assert _tool_messages(llm.seen[1])[0].content.startswith(NO_RESULTS_AVAILABLE)


def test_rounds_exhausted_returns_none_with_trace(tmp_path):
    """保险丝：模型无限逛菜单 → 轮数尽出环，content 空 → (None, 留痕)。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_search("Kafka"), _search("Kafka 集群")])
    bound = tool.for_call()
    result, trace = invoke_structured_with_tools(
        llm, IssuedQuestion, "p", [bound], max_rounds=2
    )
    assert result is None and len(llm.seen) == 2
    assert list(bound.registry) == ["bank-t-101"]          # 查过的账仍在——外层报告可用
    assert [r["query"] for r in trace] == ["Kafka", "Kafka 集群"]  # 留痕顺序 = 执行顺序


# 调用数硬顶：独立于轮数保险丝）

def test_max_calls_cap_replies_without_executing(tmp_path):
    """硬顶：达顶后的 tool_call 不执行但必回话（军规①），模型可继续交卷；
    被拒调用不进 trace（留痕只记执行）。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([
        AIMessage(content="", tool_calls=[
            {"name": "search_corpus", "args": {"query": "Kafka"}, "id": "c1"},
            {"name": "search_corpus", "args": {"query": "Kafka 集群"}, "id": "c2"},
        ]),
        _schema(_issue_args()),
    ])
    bound = tool.for_call()
    result, trace = invoke_structured_with_tools(
        llm, IssuedQuestion, "p", [bound], max_calls=1
    )
    assert isinstance(result, IssuedQuestion)              # 触顶后模型仍能交卷
    replies = _tool_messages(llm.seen[1])
    assert len(replies) == 2                               # 两个 tool_call 都有回话
    assert replies[0].content.startswith("[bank-t-101]")   # 第一个执行了
    assert "上限" in replies[1].content                     # 第二个被拒但有话
    assert len(trace) == 1                                 # 被拒调用不进留痕


def test_max_calls_none_means_uncapped(tmp_path):
    """默认 None = 不限（向后兼容）：同轮两个调用都执行。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([
        AIMessage(content="", tool_calls=[
            {"name": "search_corpus", "args": {"query": "Kafka"}, "id": "c1"},
            {"name": "search_corpus", "args": {"query": "Kafka 集群"}, "id": "c2"},
        ]),
        _schema(_issue_args()),
    ])
    _, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert len(trace) == 2


# ：检索留痕（trace 三要素：命中/空命中/没检索）

def test_trace_records_hit(tmp_path):
    """每次检索执行留痕一条：tool / query / hit_ids / elapsed_ms / empty_hit。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_search("Kafka 宕机"), _schema(_issue_args())])
    _, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert len(trace) == 1
    rec = trace[0]
    assert rec["tool"] == "search_corpus"                 # 循环补的名字键
    assert rec["query"] == "Kafka 宕机"
    assert rec["hit_ids"] == ["bank-t-101"]
    assert rec["empty_hit"] is False
    assert isinstance(rec["elapsed_ms"], (int, float)) and rec["elapsed_ms"] >= 0


def test_trace_marks_empty_hit(tmp_path):
    """空命中（哨兵回喂的那次检索）：empty_hit=True、hit_ids 空——与命中可区分。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([_search("不存在的考点"), _schema(_issue_args())])
    _, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert trace[0]["empty_hit"] is True and trace[0]["hit_ids"] == []


def test_trace_empty_when_no_search(tmp_path):
    """腿②不检索：trace 为空列表——"根本没调检索"与"查了没有"在留痕层面可区分。"""
    tool = _make_tool(tmp_path)
    llm = ScriptedLLM([AIMessage(content=json.dumps(_issue_args(), ensure_ascii=False))])
    _, trace = invoke_structured_with_tools(llm, IssuedQuestion, "p", [tool.for_call()])
    assert trace == []


# OCP 分派防御（错误左移到绑定期，llm 都不用给）

def test_empty_tools_rejected():
    with pytest.raises(ValueError, match="为空"):
        invoke_structured_with_tools(None, IssuedQuestion, "p", [])


def test_tool_missing_faces_rejected():
    class NotATool:  # 缺 as_tool/execute 两副面孔
        pass

    with pytest.raises(TypeError, match="两副面孔"):
        invoke_structured_with_tools(None, IssuedQuestion, "p", [NotATool()])


def test_duplicate_tool_names_fail_loud(tmp_path):
    """两个同名工具 = 模型看到两份同名菜单，静默选边只会产出难归因的行为。"""
    tool = _make_tool(tmp_path)
    with pytest.raises(ValueError, match="重名"):
        invoke_structured_with_tools(
            None, IssuedQuestion, "p", [tool.for_call(), tool.for_call()]
        )


def test_tool_name_clashing_schema_class_fails_loud():
    """schema 类名是交卷腿的保留名——工具撞它必炸，不做优先级仲裁。"""
    fake = SimpleNamespace(
        as_tool=lambda: SimpleNamespace(name="IssuedQuestion"),
        execute=lambda args: ("x", None),
    )
    with pytest.raises(ValueError, match="相撞"):
        invoke_structured_with_tools(None, IssuedQuestion, "p", [fake])


def test_two_tools_dispatch_independently(tmp_path):
    """OCP 验收雏形：第二个工具入环零改循环——两把账各记各的，留痕带执行序。"""
    tool = _make_tool(tmp_path)

    class FakeCandBound:
        """第二把假工具：名字不同、账本形状不同（段落检索的 C6 预演）。"""

        def __init__(self):
            self.calls = []

        def as_tool(self):
            return SimpleNamespace(name="search_candidate")

        def execute(self, args):
            self.calls.append(args.get("query"))
            return "candidate 命中内容", {"query": args.get("query"),
                                          "hit_ids": ["cand-0001"]}

    cand = FakeCandBound()
    llm = ScriptedLLM([
        AIMessage(content="", tool_calls=[
            {"name": "search_corpus", "args": {"query": "Kafka 宕机"}, "id": "c1"},
            {"name": "search_candidate", "args": {"query": "简历"}, "id": "c2"},
        ]),
        _schema(_issue_args()),
    ])
    bound = tool.for_call()
    result, trace = invoke_structured_with_tools(
        llm, IssuedQuestion, "p", [bound, cand]
    )
    assert isinstance(result, IssuedQuestion)
    assert cand.calls == ["简历"]                          # 各自的分派各自执行
    assert list(bound.registry) == ["bank-t-101"]          # bank 账本不混 candidate
    assert [r["tool"] for r in trace] == ["search_corpus", "search_candidate"]  # 执行序
    fed = _tool_messages(llm.seen[1])
    assert len(fed) == 2 and fed[1].content == "candidate 命中内容"  # 各回各的话


# MockLLM 兼容（mock 走腿②回归）

def test_mock_llm_through_loop(tmp_path):
    """MockLLM.bind_tools 透传 + 无 tool_calls → 首轮断环走腿②——mock 全流程不炸。"""
    from interviewer.llm import MockLLM

    result, trace = invoke_structured_with_tools(
        MockLLM(), IssuedQuestion, "测试提示词（请输出 JSON）", [_make_tool(tmp_path).for_call()]
    )
    assert isinstance(result, IssuedQuestion) and trace == []
