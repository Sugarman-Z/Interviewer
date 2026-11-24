"""诊断真 API 1214（messages 参数非法）：哪种消息形状被智谱端点拒绝。
背景：mock 全绿但智谱 400/1214。新旧路径报文唯一差异：老路径 invoke(字符串)
→ langchain 转成 [user]；新工具循环手搓 [System] 开头。本脚本逐形状探测定案
（每种一次真实调用，reasoning_effort=low）。

用法：.env 有 ZHIPU_API_KEY 时直接跑，结果三行，谁 FAIL 谁就是凶手。
"""
import os

from dotenv import load_dotenv

load_dotenv()

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from interviewer.agents.schemas import IssuedQuestion  # noqa: E402
from interviewer.dataflows import resolve_search_tool  # noqa: E402

llm = ChatOpenAI(
    model=os.environ.get("QUICK_LLM", "glm-5.3-flash"),
    api_key=os.environ["ZHIPU_API_KEY"],
    base_url="https://open.bigmodel.cn/api/paas/v4/",
    timeout=120,
    max_retries=1,
    extra_body={"reasoning_effort": "low"},
)
tool = resolve_search_tool(
    {"corpus_vendors": "local", "corpus_dir": "corpus", "bank_top_k": 3}
)
tools = [tool.as_tool(), IssuedQuestion]

PROMPT = "你是面试出题官，为后端工程师岗位出下一道题。可先用工具检索题库再出题。"


def probe(name: str, messages: list):
    try:
        resp = llm.bind_tools(tools).invoke(messages)
        calls = [t["name"] for t in (getattr(resp, "tool_calls", None) or [])]
        print(f"{name}: OK  tool_calls={calls}  content={resp.content[:40]!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: FAIL {type(exc).__name__}: {str(exc)[:100]}", flush=True)


# A：现在代码的形状（system-only，1 参轮）
probe("A [System]", [SystemMessage(PROMPT)])

# B：老路径的形状（user-only）
probe("B [Human]", [HumanMessage(PROMPT)])

# C：工具往返第二轮的形状（user + 空content带tool_calls的assistant + tool）
probe("C [Human, AI(tool_calls), Tool]", [
    HumanMessage(PROMPT),
    AIMessage(content="", tool_calls=[
        {"name": "search_corpus", "args": {"query": "Kafka 宕机"}, "id": "call_1"}
    ]),
    ToolMessage(
        content="[bank-kafka-001] 考点：Kafka 高可用\n题目：三节点集群 leader 宕机如何保证消息不丢？",
        tool_call_id="call_1",
    ),
])
