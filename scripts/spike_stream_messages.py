"""离线腿：证 langgraph messages 流式模式的三条机制断言（离线，不联网；
真机腿由 record_stream_chunks.py 承担）。三件事：

A. 吐 chunk：同步 graph.stream(..., stream_mode=["updates","messages"]) 对
   节点内 model.invoke(...)（plain / bind_tools / with_structured_output 三种
   调用形）会吐 messages 分量的 (AIMessageChunk, metadata)——机制是
   langchain-core 的 _should_stream：回调链里有想收 on_llm_new_token 的
   handler 时，invoke 内部改走 _stream 并聚合。
B. 聚合等价：messages 模式下并行 tool_call_chunks（多 index 车道交错）经
   聚合后的 AIMessage.tool_calls 与非流式结果逐字段一致；content 一致。
C. 节点输出不变：messages 模式与默认模式的 updates 分量逐字段相同。

跑法：`python scripts/spike_stream_messages.py`（默认 mock 环境无需 key）。
退出码 0 = 三断言全过。不进 pytest 回归（spike 产出是结论不是测试）。
"""
import sys
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel as PydanticV2Model, Field


class Scorecard(PydanticV2Model):
    """假 schema：名字与字段形态仿 agents/schemas.Scorecard（最小子集）。"""

    technical_depth: int = Field(description="技术深度评分 1-5")
    followup_text: str = Field(default="", description="追问句")
    action: str = Field(description="advance|followup")


# 假流式模型：_stream 按脚本吐 chunk（含并行 tool_call_chunks 交错车道），
# _generate 返回 ground truth 聚合（不经流式路径）
def _chunk_script() -> list[AIMessageChunk]:
    """两条并行 tool_call 车道（index 0/1）交错 + 尾部 content。
    车道 0 = Scorecard（评估腿形：含 followup_text 键）；车道 1 = search_bank
（检索腿形：只有 query 键）——模拟 GLM 单轮并行发多个 tool_calls 的实测
    行为。chunk 边界刻意切在键名中间，验聚合。"""
    return [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                ToolCallChunk(name="Scorec", args="", id="call_0", index=0),
                ToolCallChunk(name="search_", args="", id="call_1", index=1),
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                ToolCallChunk(name="", args='{"technical', id=None, index=0),
                ToolCallChunk(name="", args='{"qu', id=None, index=1),
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                ToolCallChunk(name="", args='_depth":3,"followup_text":"你刚说', id=None, index=0),
                ToolCallChunk(name="", args='ery":"Kafka 消费", ', id=None, index=1),
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                ToolCallChunk(name="ard", args='', id=None, index=0),
                ToolCallChunk(name="bank", args='', id=None, index=1),
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                ToolCallChunk(
                    name="",
                    args='用了 CDN，能展开讲讲缓存一致性怎么处理的吗？","action":"followup"}',
                    id=None,
                    index=0,
                ),
                ToolCallChunk(name="", args='"other": 1}', id=None, index=1),
            ],
        ),
        AIMessageChunk(content="（正文尾巴）"),
    ]


#: ground truth：非流式 _generate 直接返回的完整应答（聚合由构造保证）
_GROUND_TRUTH = AIMessage(
    content="（正文尾巴）",
    tool_calls=[
        {
            "name": "Scorecard",
            "args": {
                "technical_depth": 3,
                "followup_text": "你刚说用了 CDN，能展开讲讲缓存一致性怎么处理的吗？",
                "action": "followup",
            },
            "id": "call_0",
            "type": "tool_call",
        },
        {
            "name": "search_bank",
            "args": {"query": "Kafka 消费", "other": 1},
            "id": "call_1",
            "type": "tool_call",
        },
    ],
)


class FakeStreamingModel(BaseChatModel):
    """_stream 吐脚本 chunk；_generate 返回 ground truth（两腿独立构造）。"""

    stream_calls: int = 0
    generate_calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-streaming"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeStreamingModel":  # noqa: ARG002
        # 与 MockLLM 同款透传（不真绑 schema——本 spike 验的是流式/聚合机制，
        # 不验工具绑定本身）
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:  # noqa: ARG002
        object.__setattr__(self, "generate_calls", getattr(self, "generate_calls", 0) + 1)
        return ChatResult(generations=[ChatGeneration(message=_GROUND_TRUTH)])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:  # noqa: ARG002
        # langchain-core 1.x：_stream 须吐 ChatGenerationChunk（.message 载荷），
        # 裸 AIMessageChunk 会在 _generate_with_cache 的流式路由里炸
        object.__setattr__(self, "stream_calls", getattr(self, "stream_calls", 0) + 1)
        for chunk in _chunk_script():
            yield ChatGenerationChunk(message=chunk)


# 最小图：一个节点把三种调用形各跑一遍，节点返回值携带三腿结果
def _build_graph(model: FakeStreamingModel):
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class St(TypedDict):
        calls: list

    def node(state: St) -> dict:
        calls = []
        # 腿 1：plain invoke
        m1 = model.invoke("plain")
        calls.append({"leg": "plain", "content": m1.content})
        # 腿 2：bind_tools 工具循环形（invoke_structured_with_tools 的调用形）
        bound = model.bind_tools([{"name": "search_bank", "args": {"query": {"type": "string"}}}])
        m2 = bound.invoke("tools")
        calls.append(
            {
                "leg": "bind_tools",
                "content": m2.content,
                "tool_calls": [(t["name"], t["args"], t["id"]) for t in m2.tool_calls],
            }
        )
        # 腿 3：with_structured_output(method="function_calling")
        structured = model.with_structured_output(Scorecard, method="function_calling")
        s3 = structured.invoke("structured")
        calls.append({"leg": "structured", "parsed": s3.model_dump() if s3 else None})
        return {"calls": calls}

    g = StateGraph(St)
    g.add_node("spike", node)
    g.add_edge(START, "spike")
    g.add_edge("spike", END)
    return g.compile()


def _norm_tool_calls(msg: AIMessage):
    return [(t["name"], t["args"], t["id"]) for t in (msg.tool_calls or [])]


def main() -> int:
    failures: list[str] = []
    model = FakeStreamingModel()
    graph = _build_graph(model)

    # 基准：默认模式 invoke（现行引擎路径）——节点三腿的返回值
    base_state = graph.invoke({"calls": []})
    base_calls = base_state["calls"]

    # 断言 C 的基准在后面 messages 模式跑完后对比；先证 A：吐不吐 chunk
    chunks: list[tuple[Any, dict]] = []
    updates_default_mode: list = []
    for item in graph.stream({"calls": []}, stream_mode=["updates", "messages"]):
        mode, data = item
        if mode == "messages":
            chunk, metadata = data
            chunks.append((chunk, metadata))
        else:
            updates_default_mode.append(data)
    print(f"[A] messages 分量收到 {len(chunks)} 个 chunk")
    if not chunks:
        failures.append("A/R1：messages 模式没吐 chunk（langgraph/langchain 组合不路由流式）")
    node_names = {m.get("langgraph_node") for _, m in chunks}
    print(f"[A] chunk 的 langgraph_node：{node_names}")
    if not node_names <= {"spike"} or not node_names:
        failures.append(f"A：metadata 节点名异常 {node_names}（tap 判别归属依赖它）")

    # 断言 B：聚合等价——messages 分量的 chunk 按「LLM 调用边界」分组后各自
    # 拼接（AIMessageChunk +），与 ground truth 逐字段比。
    # **边界规则（spike 发现，如实记）**：messages metadata 无 run_id（实测
    # 键集只有 langgraph_node/step/path/triggers 等），可判边界只有——
    # ① tool_call_chunk 带非空 id = 新调用首片段（OpenAI 兼容流式语义，
    #   离线+真机双证，这是 tap reset 的契约锚点之一）；
    # ② 下方 _prev_is_pure_content 只是**离线 demo 脚手架**（防本脚本三腿
    #   跨调用缝合），不是 T2 契约——真机 content 1-9 字/片形态下它会把
    #   一个调用拆成上百组；纯 content 调用的真实边界不可判（无 run_id），
    # tap reset 只能锚 metadata 节点切换 + 非空 id（TDD 事实 1）
    runs: list[list] = []
    for chunk, _ in chunks:
        tccs = list(chunk.tool_call_chunks or [])

        def _prev_is_pure_content() -> bool:
            # 离线 demo 脚手架（见上方注释②）：真机纯 content 分片形态下
            # 误判为多调用——仅防本脚本三腿缝合用，勿进 T2
            if not runs or not runs[-1]:
                return False
            prev = runs[-1]
            return (
                not any(pc.tool_call_chunks for pc in prev)
                and any(pc.content for pc in prev)
            )

        starts_new = any(c.get("id") for c in tccs) or (
            bool(chunk.content) and _prev_is_pure_content()
        )
        if not runs or starts_new:
            runs.append([])
        runs[-1].append(chunk)
    print(f"[B] chunk 按 LLM 调用边界分组：{len(runs)} 组（三腿）")
    gt_calls = _norm_tool_calls(_GROUND_TRUTH)
    print(f"[B] ground truth tool_calls：{gt_calls}")
    tool_bearing: list[tuple[list, object]] = []
    for rchunks in runs:
        agg: Optional[AIMessageChunk] = None
        for chunk in rchunks:
            agg = chunk if agg is None else agg + chunk
        got_calls = _norm_tool_calls(agg)  # type: ignore[arg-type]
        print(f"[B] 调用组 tool_calls：{got_calls} / content：{agg.content!r}")  # type: ignore[union-attr]
        if got_calls:  # 三腿 ground truth 相同：每个含 tool_call 的组都须逐字段等价
            tool_bearing.append((got_calls, agg.content))  # type: ignore[union-attr]
    if not tool_bearing:
        failures.append("B/R6：没有任何调用组产出 tool_calls（分组规则失效？）")
    for got_calls, got_content in tool_bearing:
        if got_calls != gt_calls:
            failures.append(f"B/R6：聚合 tool_calls 不等价\n  got  {got_calls}\n  want {gt_calls}")
        if got_content != _GROUND_TRUTH.content:
            failures.append(f"B/R6：聚合 content 不等价 {got_content!r}")

    # 断言 C：messages 模式下节点的返回值（updates 分量）与默认模式一致
    msg_state = None
    for upd in updates_default_mode:
        if "spike" in upd:
            msg_state = upd["spike"]
    same = msg_state is not None and msg_state.get("calls") == base_calls
    print(f"[C] messages 模式节点返回 == 默认模式：{same}")
    if not same:
        failures.append(f"C：节点输出分叉\n  messages {msg_state}\n  default {base_calls}")

    # 附带证据：invoke 是否真的被路由进 _stream（_should_stream 机制）
    print(
        f"[mech] _stream 调用 {model.stream_calls} 次 / _generate 调用 "
        f"{model.generate_calls} 次（messages 模式下 invoke 走流式聚合的证据）"
    )

    # with_structured_output 腿的解析结果顺带断言（腿 3 能否原样工作）
    leg3 = base_calls[2]
    ok3 = leg3["parsed"] == {
        "technical_depth": 3,
        "followup_text": "你刚说用了 CDN，能展开讲讲缓存一致性怎么处理的吗？",
        "action": "followup",
    }
    print(f"[leg3] with_structured_output(function_calling) 解析：{leg3['parsed']} ok={ok3}")
    if not ok3:
        failures.append(f"leg3：结构化腿解析异常 {leg3}")

    if failures:
        print("\n== SPIKE-1 离线腿 FAIL ==")
        for f in failures:
            print(" -", f)
        return 1
    print("\n== SPIKE-1 离线腿 PASS：A(吐 chunk)/B(并行车道聚合等价)/C(节点输出不变) ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
