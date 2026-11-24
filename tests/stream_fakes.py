"""流式假模型：供流式系列测试共用的注入式假 LLM。
设计口径来自真机 spike 实测：

1. langchain-core 1.x：_stream 须 yield ChatGenerationChunk（裸
   AIMessageChunk 在流式路由里炸 AttributeError）。
2. messages 分量的 tool_call_chunks 是 dict 形：首片带 id+name（index 定
   车道），后续片只带 args 增量、id 为空。
3. 剧本按 invoke 次序消费（循环取余）：图内顺序调用安全；测试勿并行共享
   同一实例（MockLLM 同款警示）。
4. 剧本即形态：responses 列表可排出并行多车道、工具循环多轮、纯 content
   多 chunk 分片（「同一 content 调用多 chunk 不 reset」语义锁）。
"""
from types import SimpleNamespace
from typing import Any, Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


def tool_call_message(tool_calls: list[dict], content: str = "") -> AIMessage:
    """造一条带 tool_calls 的完整应答（剧本单元）。dict 形同 AIMessage.tool_calls。"""
    return AIMessage(
        content=content,
        tool_calls=[
            {"name": tc["name"], "args": tc["args"], "id": tc["id"], "type": "tool_call"}
            for tc in tool_calls
        ],
    )


def _fragments(resp: AIMessage) -> Iterator[AIMessageChunk]:
    """把完整应答切成 chunk 序列：tool 车道交错分片 + content 切片 + 空尾巴。

    args JSON 串按 ~1/3 切三段、content 按 2-3 字切片——多 chunk 形态是
    T2 抽取器增量语义的回归载体（真机是单 chunk 整块，见 fixture）。
    """
    lanes: list[list[ToolCallChunk]] = []
    for i, tc in enumerate(resp.tool_calls or []):
        args_json = _dumps(tc["args"])
        cuts = [args_json[: len(args_json) // 3], args_json[len(args_json) // 3 : 2 * len(args_json) // 3], args_json[2 * len(args_json) // 3 :]]
        lanes.append(
            [
                ToolCallChunk(name=tc["name"], args=cuts[0], id=tc["id"], index=i),
                ToolCallChunk(name="", args=cuts[1], id=None, index=i),
                ToolCallChunk(name="", args=cuts[2], id=None, index=i),
            ]
        )
    # 车道交错（round-robin）——并行 tool_calls 的实测到达形态
    while any(lanes):
        step: list[ToolCallChunk] = []
        for lane in lanes:
            if lane:
                step.append(lane.pop(0))
        if step:
            yield AIMessageChunk(content="", tool_call_chunks=step)
    text = resp.content or ""
    for i in range(0, len(text), 3):
        yield AIMessageChunk(content=text[i : i + 3])
    yield AIMessageChunk(content="")  # 真机观测到的空收尾片


def _dumps(args: dict) -> str:
    import json

    return json.dumps(args, ensure_ascii=False)


class ScriptedStreamingLLM(BaseChatModel):
    """剧本假模型：_generate 整条返回、_stream 逐片吐（两腿一致性由构造保证）。"""

    responses: list
    idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-streaming"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedStreamingLLM":
        """MockLLM 同款透传：不真绑（验流式/聚合机制，不验工具绑定本身）。"""
        return self

    def _current(self) -> AIMessage:
        resp = self.responses[self.idx % len(self.responses)]
        return resp

    def _advance(self) -> AIMessage:
        resp = self._current()
        object.__setattr__(self, "idx", self.idx + 1)
        return resp

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:  # noqa: ARG002
        return ChatResult(generations=[ChatGeneration(message=self._advance())])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:  # noqa: ARG002
        for chunk in _fragments(self._advance()):
            yield ChatGenerationChunk(message=chunk)


class StubSearchTool:
    """LoopTool 最小桩（invoke_structured_with_tools 多轮回喂用例的检索面）。"""

    def as_tool(self):
        return SimpleNamespace(
            name="search_bank",
            description="题库检索（桩）",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )

    def execute(self, args: dict):
        return "（stub 检索结果：两条候选）", {"tool": "search_bank", "args": args}


def _messages_text(messages: Any) -> str:
    """langchain 调用入参归一为纯文本（str 或 BaseMessage 列表）。"""
    if isinstance(messages, str):
        return messages
    parts = []
    for m in messages or []:
        content = getattr(m, "content", m)
        parts.append(content if isinstance(content, str) else str(content))
    return "".join(parts)


class StreamingMockLLM(BaseChatModel):
    """MockLLM 的流式皮（T3 离线出 delta 的机制，TDD 处方）。

    `_generate` 委托真 MockLLM 的标记分流（评估官/核验官/请输出 JSON →
    各自 JSON；invoke 语义与 MockLLM 逐字一致——含 issue 轮换计数器）；
    `_stream` 把同一应答 content 按 2-3 字切片——messages 模式下节点内
    invoke 走此腿，tap 在 freetext/content 车道上增量抽取。
    `with_structured_output` 抛 AttributeError：模拟 MockLLM 缺该方法 →
    structured.py 捕获后走 freetext 降级腿（与现行 mock 全流程同路径）。

    注入通道：monkeypatch `interviewer.graph.coach_graph.create_llm_client`
（coach_graph 是 from-import 名字绑定，patch 本名无效）；图在 lifespan
    内建，patch 须在进入 TestClient 上下文前生效。_SCORECARD 的追问注入
    照旧 monkeypatch MockLLM 类属性（inner 读类属性）。
    """

    inner: Any  # MockLLM 实例

    @property
    def _llm_type(self) -> str:
        return "streaming-mock"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "StreamingMockLLM":
        return self  # MockLLM 同款透传

    def with_structured_output(self, schema: Any, **kwargs: Any):
        raise AttributeError("StreamingMockLLM 模拟 MockLLM 无结构化方法 → freetext 降级腿")

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:  # noqa: ARG002
        return ChatResult(
            generations=[ChatGeneration(message=self._mock_response(_messages_text(messages)))]
        )

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Iterator[ChatGenerationChunk]:  # noqa: ARG002
        resp = self._mock_response(_messages_text(messages))
        text = resp.content or ""
        for i in range(0, len(text), 3):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[i : i + 3]))

    def _mock_response(self, prompt: str) -> AIMessage:
        """委托 MockLLM 的标记分流，但产物重新包一层（不共享实例状态对象）。"""
        msg = self.inner.invoke(prompt)
        return AIMessage(content=msg.content)
