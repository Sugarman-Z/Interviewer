"""节点通用小工具：transcript 追加、空值 marker、截断。"""
from interviewer.agents.utils.agent_states import RESUME_ANSWER


def append_transcript(state: dict, role: str, content: str, **meta) -> list:
    """返回追加了新条目的全量新列表（transcript 无 reducer，写者必须整块返回）。"""
    entry = {"role": role, "content": content}
    entry.update(meta)
    return list(state.get("transcript", [])) + [entry]


def last_candidate_text(state: dict) -> str:
    """取候选人最近一次发言（自我介绍/最近答案）。"""
    for entry in reversed(state.get("transcript", [])):
        if entry.get("role") == "candidate":
            return entry.get("content", "")
    return ""


def first_candidate_text(state: dict) -> str:
    """取候选人第一次发言（自我介绍——IssueQuestion 签发上下文用；与上者对称）。

    注意不能用 self_intro_summary：那是 Planner 批式时代的字段，Phase B 图里无人写入。
    """
    for entry in state.get("transcript", []):
        if entry.get("role") == "candidate":
            return entry.get("content", "")
    return ""


def resume_text(payload: dict) -> str:
    """从 interrupt 的 resume 载荷里取答案文本（skip 给显式占位，防下游空值幻觉）。"""
    if payload.get("type") == RESUME_ANSWER:
        return payload.get("text", "")
    return "(候选人跳过了此题)"


def answer_or_marker(text: str, marker: str) -> str:
    """空值 marker（TA agent_utils.opponent_argument_or_opening 同款）：
    空串不等于"存在一个空答案"——缺席状态必须显式标记，否则模型会脑补。"""
    return text if text and text.strip() else marker


def clip(text: str, limit: int = 500) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def current_module(state: dict) -> str:
    """当前题所属模块（改键名只改这里）。字段缺席/空 dict 回落默认模块，
    必须真值判断——空 dict 的 is not None 恒真。"""
    return (state.get("current_question") or {}).get("module", "project")


def transcript_lines(state: dict) -> str:
    """transcript → "[role] content" 多行文本（报告类 prompt 的统一渲染出口）。"""
    return "\n".join(
        f"[{e.get('role', '?')}] {e.get('content', '')}"
        for e in state.get("transcript", [])
    )
