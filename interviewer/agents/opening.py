"""开场节点：确定性、零 LLM。

真实面试的开场白是固定脚本（"请先做个自我介绍"），不需要模型生成——
这也顺带消灭了一个幻觉源：开场环节没有 LLM 参与。
"""
from interviewer.agents.utils.agent_utils import append_transcript


def opening_intro_node(state: dict) -> dict:
    position = (state.get("position") or "").strip() or "未声明岗位"
    text = (
        f"你好，我是今天「{position}」岗位的面试官。"
        "我们先从一个简单的自我介绍开始：请介绍一下你的背景、主要经历和你想强调的方向。"
    )
    return {
        "pending_question": text,
        "transcript": append_transcript(state, "interviewer", text, phase="intro"),
    }
