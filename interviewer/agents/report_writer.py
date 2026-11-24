"""ReportWriter：终场报告（deep 档）。只依据 transcript 与 module_report，引用原话。"""
from interviewer.agents.prompts import REPORT_SYSTEM
from interviewer.agents.utils.agent_utils import answer_or_marker, clip, transcript_lines


def make_report_writer(llm, spec: dict | None = None):
    def report_node(state: dict) -> dict:
        prompt = REPORT_SYSTEM.format(
            position=answer_or_marker(state.get("position", ""), "未声明岗位"),
            module_report=answer_or_marker(state.get("module_report", ""), "(无模块小结)"),
            transcript=clip(transcript_lines(state), 6000),
        )
        resp = llm.invoke(prompt)
        return {"interview_report": getattr(resp, "content", str(resp))}

    return report_node
