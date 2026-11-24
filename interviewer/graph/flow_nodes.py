"""确定性流程节点：ModuleSummary（模块边界）。
不含 LLM 主逻辑，只做确定性状态变更——"条件边只读不写"纪律下的合法落点。
"""
from interviewer.agents.prompts import MODULE_SUMMARY_PROMPT
from interviewer.agents.utils.agent_states import FollowupState, fresh_followup_state
from interviewer.agents.utils.agent_utils import answer_or_marker, clip, current_module, transcript_lines


def make_module_summary(llm, spec: dict | None = None):
    """模块边界三件事：① 从 transcript 写 module_report 小结；② 重置
    followup_state（保留 total_count，否则追问跨段串台）；③ 引入 messages
    通道后清空签发工具循环的消息段。
    """

    def module_summary_node(state: dict) -> dict:
        prompt = MODULE_SUMMARY_PROMPT.format(
            module=current_module(state),
            transcript=clip(transcript_lines(state), 4000) or "(无记录)",
        )
        resp = llm.invoke(prompt)
        followup = state.get("followup_state") or fresh_followup_state()
        return {
            "module_report": answer_or_marker(
                getattr(resp, "content", str(resp)), "(本次模块无小结)"
            ),
            "followup_state": FollowupState(
                topic="",
                qa_history="",
                followup_count=0,
                total_count=followup["total_count"],
            ),
        }

    return module_summary_node
