"""await 类 interrupt 节点工厂。

三条军规（违反=最难自查的 bug）：interrupt() 必须是节点第一行；
每条执行路径最多一个 interrupt；恢复只发 Command(resume=...) 且同 thread_id。
"""
from interviewer.agents.utils.agent_states import RESUME_ANSWER
from interviewer.agents.utils.agent_utils import append_transcript, resume_text
from langgraph.types import interrupt


def make_await_node(meta_fn):
    """meta_fn(state) -> dict：本次 interrupt 暴露给 CLI 的载荷（含问题文本）。

    节点返回：last_answer（skip 时为显式占位文本）+ transcript 追加候选人发言。
    """

    def await_node(state: dict) -> dict:
        # 纪律 1：interrupt 是第一行，之前零副作用
        payload = interrupt(meta_fn(state))
        ptype = payload.get("type", RESUME_ANSWER) if isinstance(payload, dict) else RESUME_ANSWER
        text = resume_text(payload if isinstance(payload, dict) else {})
        return {
            "last_answer": text,
            "transcript": append_transcript(
                state, "candidate", text, topic=state.get("current_topic", "")
            ),
        }

    return await_node
