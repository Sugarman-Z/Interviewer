"""引擎 seam 单测：on_token 只读观察口的合同。
锁的合同：不传回调 → 现行默认 stream 模式（CLI 零改动锁）；传回调 →
messages 分量逐 chunk 到达（含 metadata.langgraph_node），updates 分量照常
驱动中断检测；回调抛异常必吞（回合照跑、checkpoint 照落）；回调改不了
state（恶意回调与无回调的落盘 state 一致）；真图（mock provider）带回调
跑整场挂起序列与无回调逐字相同；工具循环多轮聚合不变形。
"""
from typing import TypedDict

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from interviewer.graph.session_driver import (
    pending_interrupt,
    run_new_session,
    run_resumed_session,
)
from stream_fakes import ScriptedStreamingLLM, StubSearchTool, tool_call_message


class _St(TypedDict):
    text: str


def _mini_graph(llm, saver, llm_calls: int = 1, weave_content: bool = False):
    """单节点图：节点内 LLM 调用 → interrupt 挂起 → resume 后落答案。

    最小可判形态：messages 分量只能来自节点内的 llm.invoke（chunk 断言的
    唯一来源），interrupt 走引擎同款 langgraph.types.interrupt。llm_calls
    控制节点内调用次数（多剧本断言用）；weave_content=True 把 LLM content
    织进终态（「回调改不了节点产物」的可观测面——否则 content 根本不进
    被比较的 state，恶意回调用例零判别力，T1 评审 #3）。
    """

    def node(state: _St) -> dict:
        for _ in range(llm_calls):
            resp = llm.invoke("probe-prompt")
        value = interrupt({"question": resp.content or "（tool 应答）"})
        answer = str(value.get("text", ""))
        return {"text": f"{answer}|{resp.content}" if weave_content else answer}

    g = StateGraph(_St)
    g.add_node("probe", node)
    g.add_edge(START, "probe")
    g.add_edge("probe", END)
    return g.compile(checkpointer=saver)


class _StreamKwargsProbe:
    """包一层 graph.stream 记录并转发调用 kwargs（legacy 路径锁的探针）。

    转发 kwargs：driver 若给 legacy 分支加了任何 kwargs（不止 stream_mode），
    真图行为与断言都见到它——「零 kwargs」是与 CLI 时代逐行相同的合同。
    """

    def __init__(self, graph):
        self._g = graph
        self.calls: list[dict] = []

    def stream(self, payload, cfg=None, **kwargs):
        self.calls.append(dict(kwargs))
        yield from self._g.stream(payload, cfg, **kwargs)

    def get_state(self, cfg):
        return self._g.get_state(cfg)


def test_on_token_none_uses_legacy_stream_path():
    """CLI 零改动锁：不传 on_token → graph.stream 收到零 kwargs（与 CLI
    时代 graph.stream(payload, cfg) 逐行相同——比「不含 stream_mode」更强，
    静默类 kwargs（langgraph 无声吸收的陌生键）只有这道断言能拦）。"""
    llm = ScriptedStreamingLLM(responses=[AIMessage(content="答")])
    graph = _mini_graph(llm, InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-legacy"}}
    run_new_session(graph, cfg, {"text": ""}, lambda v: None)  # 挂起即退
    probe = _StreamKwargsProbe(graph)
    result = run_resumed_session(probe, cfg, lambda v: {"text": "继续"})
    assert result.finished is True
    assert probe.calls, "driver 应至少调一次 graph.stream"
    assert all(kw == {} for kw in probe.calls)


def test_on_token_receives_chunks_with_metadata():
    """messages 分量逐 chunk 到达：节点名正确、content 拼接 == 剧本、
    tool 车道首片带 id（T0 事实 1 的调用边界锚）。"""
    llm = ScriptedStreamingLLM(
        responses=[
            AIMessage(content="逐字出题的内容九个字"),
            tool_call_message(
                [{"name": "IssuedQuestion", "args": {"text": "题面", "topic": "考点"}, "id": "call_x"}]
            ),
        ]
    )
    graph = _mini_graph(llm, InMemorySaver(), llm_calls=2)
    cfg = {"configurable": {"thread_id": "t-chunks"}}
    got: list[tuple[object, dict]] = []

    def on_token(chunk, metadata):
        got.append((chunk, metadata))

    result = run_new_session(graph, cfg, {"text": ""}, lambda v: None, on_token=on_token)
    assert result.finished is False  # updates 分量照常驱动中断检测（挂起即退）
    assert got, "messages 模式应吐 chunk（ScriptedStreamingLLM 走 _stream）"
    assert all(m.get("langgraph_node") == "probe" for _, m in got)
    content = "".join(c.content or "" for c, _ in got)
    assert content == "逐字出题的内容九个字"      # 含空收尾片不影响拼接
    tccs = [c for chunk, _ in got for c in (chunk.tool_call_chunks or [])]
    assert tccs and any(c.get("id") for c in tccs)  # 首片带 id（dict 形取键）


def test_chunks_flow_across_nodes():
    """跨节点 chunk 流（T3 stage 直发的地基）：同一次 drive 内 chunk 的
    langgraph_node 随节点切换、先到节点的块先到、挂起检测不受交错影响——
    全套件此前只有单节点图，跨节点形态零离线覆盖。"""
    llm = ScriptedStreamingLLM(
        responses=[AIMessage(content="评估内容"), AIMessage(content="出题内容")]
    )

    class _TwoNodeSt(TypedDict):
        text: str

    def evaluator_node(state: _TwoNodeSt) -> dict:
        llm.invoke("eval")
        return {"text": "evaluated"}

    def issue_node(state: _TwoNodeSt) -> dict:
        resp = llm.invoke("issue")
        value = interrupt({"question": resp.content})
        return {"text": str(value.get("text", ""))}

    g = StateGraph(_TwoNodeSt)
    g.add_node("evaluator", evaluator_node)
    g.add_node("issuer", issue_node)
    g.add_edge(START, "evaluator")
    g.add_edge("evaluator", "issuer")
    g.add_edge("issuer", END)
    graph = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-across"}}
    got: list[tuple[object, dict]] = []
    run_new_session(
        graph, cfg, {"text": ""},
        lambda v: None,
        on_token=lambda chunk, meta: got.append((chunk, meta)),
    )
    nodes_seen = [m["langgraph_node"] for _, m in got]
    assert set(nodes_seen) == {"evaluator", "issuer"}   # 随节点切换
    first_issue = nodes_seen.index("issuer")
    assert "evaluator" not in nodes_seen[first_issue:]  # eval 块全在 issue 块前
    assert pending_interrupt(graph, cfg) is not None     # 挂起检测不受交错影响


def test_on_token_exception_does_not_break_round(caplog):
    """不变量 1：回调抛 RuntimeError → 吞掉记 WARNING 日志、回合照跑到挂起、
    checkpoint 落盘（pending_interrupt 能读回）——记日志的半边也钉住。"""
    import logging

    llm = ScriptedStreamingLLM(responses=[AIMessage(content="内容")])
    graph = _mini_graph(llm, InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-raise"}}

    def hostile(chunk, metadata):
        raise RuntimeError("观察口炸了")

    with caplog.at_level(logging.WARNING, logger="interviewer.graph.session_driver"):
        result = run_new_session(graph, cfg, {"text": ""}, lambda v: None, on_token=hostile)
    assert result.finished is False                    # 挂起即退（用户退出语义）
    assert pending_interrupt(graph, cfg) is not None   # checkpoint 照落
    assert any("on_token" in r.message for r in caplog.records)  # 不许静默 pass
    resumed = run_resumed_session(graph, cfg, lambda v: {"text": "续"}, on_token=hostile)
    assert resumed.finished is True and resumed.final_values["text"] == "续"


def test_callback_cannot_mutate_state():
    """不变量（回调只观察）：恶意回调（改 metadata、改 chunk 顶层与嵌套）与
    无回调的落盘 state 逐字段一致——节点把 LLM content 织进终态，锁的是
    「回调改不了节点产物」这一可观测面。

    如实记（T1 评审 #1/#3）：driver 的顶层浅拷贝黑盒上不可判别（当前栈的
    嵌套隔离实际靠同步流「任务完成后排水」时序）；本用例是防「库内部改共
    享对象/提前暴露」的回归锚 + 回合存活锁，不是拷贝存在性的证明。"""
    from interviewer.graph.session_driver import run_new_session

    def run(hostile: bool) -> dict:
        llm = ScriptedStreamingLLM(responses=[AIMessage(content="同一剧本")])
        graph = _mini_graph(llm, InMemorySaver(), weave_content=True)
        cfg = {"configurable": {"thread_id": f"t-mut-{hostile}"}}

        def on_token(chunk, metadata):
            if hostile:
                metadata["langgraph_node"] = "hacked"
                chunk.content = "篡改"  # 顶层：浅拷贝隔离
                if chunk.tool_call_chunks:
                    chunk.tool_call_chunks[0]["args"] = '{"text": "HACKED"}'  # 嵌套：时序隔离

        run_new_session(
            graph, cfg, {"text": ""},
            lambda v: {"text": "答案"},
            on_token=on_token if hostile else None,
        )
        return dict(graph.get_state(cfg).values)

    assert run(hostile=True) == run(hostile=False)


def _real_env(tmp_path, sid):
    """真图 + mock provider（现有 test_session_driver 同款形态）。"""
    from interviewer.default_config import get_config
    from interviewer.graph.checkpointer import build_checkpointer
    from interviewer.graph.coach_graph import CoachGraph
    from interviewer.graph.session_runner import create_initial_state

    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 2
    saver, close = build_checkpointer(cfg)
    coach = CoachGraph(cfg, checkpointer=saver)
    return cfg, coach, coach.build(), create_initial_state(sid, "后端开发", "简历", ""), close


def test_interrupt_flow_unchanged_with_callback(tmp_path):
    """回归锚：真图带回调跑整场（含追问剧本）——挂起序列与无回调逐字相同、
    收场报告齐。MockLLM 非 BaseChatModel → 零 chunk 属预期降级（US4 引擎侧：
    吐不出 token 不影响回合）。"""
    from interviewer.graph.session_driver import run_new_session
    from interviewer.llm import MockLLM

    followup_scorecard = dict(MockLLM._SCORECARD)
    followup_scorecard.update({"action": "followup", "followup_text": "（追问）再展开讲讲？"})
    questions = {}
    for with_cb in (False, True):
        original = MockLLM._SCORECARD
        MockLLM._SCORECARD = followup_scorecard  # 先例：test_web_answers 的注入法
        try:
            cfg, coach, graph, initial, close = _real_env(tmp_path, f"t-flow-{with_cb}")
            cfg_i = coach.invoke_config(f"t-flow-{with_cb}")
            seen: list = []
            chunks: list = []

            def on_interrupt(value):
                seen.append(value["question"])
                return {"type": "answer", "text": "标准回答"}

            def on_token(chunk, metadata):
                chunks.append(chunk)

            result = run_new_session(
                graph, cfg_i, initial, on_interrupt,
                on_token=(on_token if with_cb else None),
            )
            questions[with_cb] = seen
            assert result.finished is True
            assert result.final_values["interview_report"]
            assert len(seen) >= 4  # 自我介绍 + 2 题 + 至少 1 追问（剧本生效）
            if with_cb:
                assert not chunks  # MockLLM 非 BaseChatModel：零 chunk = 预期降级
        finally:
            MockLLM._SCORECARD = original
            close()
    assert questions[False] == questions[True]  # 挂起序列逐字相同（追问剧本）


class _Issued(BaseModel):
    text: str
    topic: str = ""


def test_tool_loop_multiround_toolmessage_refeed():
    """工具循环多轮（T0 离线腿未证、此处承接）：search 交卷两轮剧本经
    invoke_structured_with_tools 在 messages 模式下跑通——ToolMessage 回喂
    后下一轮照发、聚合不变形、按非空 id 可辨两轮。"""
    from interviewer.agents.utils.structured import invoke_structured_with_tools
    from interviewer.graph.session_driver import run_new_session

    llm = ScriptedStreamingLLM(
        responses=[
            tool_call_message([{"name": "search_bank", "args": {"query": "Kafka"}, "id": "call_s"}]),
            tool_call_message(
                [{"name": "_Issued", "args": {"text": "循环出的题面", "topic": "T"}, "id": "call_f"}]
            ),
        ]
    )
    captured: dict = {}

    class _LoopSt(TypedDict):
        text: str

    def node(state: _LoopSt) -> dict:
        result, trace = invoke_structured_with_tools(
            llm, _Issued, "出题", [StubSearchTool()]
        )
        value = interrupt({"question": result.text if result else "None", "trace": trace})
        return {"text": str(value.get("text", ""))}

    g = StateGraph(_LoopSt)
    g.add_node("loop", node)
    g.add_edge(START, "loop")
    g.add_edge("loop", END)
    graph = g.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t-loop"}}

    def on_token(chunk, metadata):
        captured.setdefault("chunks", []).append(chunk)

    run_new_session(graph, cfg, {"text": ""}, lambda v: None, on_token=on_token)
    pend = pending_interrupt(graph, cfg)
    assert pend and pend["question"] == "循环出的题面"   # 两腿解析正确
    assert len(pend["trace"]) == 1 and pend["trace"][0]["tool"] == "search_bank"
    # 回喂后下一轮照发：按非空 id 分组恰两轮，末轮 tool 名 == schema 名
    rounds: list[list] = []
    for chunk in captured["chunks"]:
        tccs = list(chunk.tool_call_chunks or [])
        if not rounds or any(c.get("id") for c in tccs):
            rounds.append([])
        rounds[-1].append(chunk)
    assert len([r for r in rounds if any(c.tool_call_chunks for c in r)]) == 2
