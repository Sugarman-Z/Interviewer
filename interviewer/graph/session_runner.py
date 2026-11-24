"""会话运行辅助：初始 state 构造 + 会话落盘（I/O 全在图外，图保持纯净）。"""
import json
from datetime import datetime
from pathlib import Path

from interviewer.agents.utils.agent_states import fresh_followup_state


def create_initial_state(
    session_id: str,
    position: str,
    candidate_profile: str,
    jd_requirements: str = "",
    past_context: str = "",
) -> dict:
    """初始 state：入口上下文一次性注入（TA create_initial_state 同款模板方法）。"""
    return {
        "session_id": session_id,
        "position": position,
        "candidate_profile": candidate_profile,
        "jd_requirements": jd_requirements,
        "past_context": past_context,
        "transcript": [],
        "pending_question": "",
        "last_answer": "",
        "current_question": {},
        "current_topic": "",
        "followup_state": fresh_followup_state(),
        "last_scorecard": {},
        "issued_keys": [],
        "consecutive_llm_failures": 0,
        "topic_results": [],
        "retrieval_trace": [],
        "module_report": "",
        "interview_report": "",
    }


def persist_session(config: dict, final_state: dict) -> dict:
    """会话落盘：transcript JSON + 终场报告 markdown。返回写出的文件路径。"""
    session_id = final_state.get("session_id", "unknown")
    sessions_dir = Path(config["data_dir"]) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_path = sessions_dir / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "position": final_state.get("position", ""),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "issued_keys": final_state.get("issued_keys", []),
        "topic_results": final_state.get("topic_results", []),
        "retrieval_trace": final_state.get("retrieval_trace", []),
        "transcript": final_state.get("transcript", []),
        "module_report": final_state.get("module_report", ""),
    }
    session_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report_path = None
    report = final_state.get("interview_report", "")
    if report:
        report_path = sessions_dir / f"{session_id}_report.md"
        report_path.write_text(report, encoding="utf-8")

    return {"session": str(session_path), "report": str(report_path) if report_path else None}


# W3 起由 cli 在会话结束后调用：从 final state 收割 weak_points → MistakeBook（去重后 append）
def harvest_weak_points(final_state: dict) -> list[str]:
    """收割全场评分卡里的薄弱点（图外收口，I/O 不进图）。"""
    seen, result = set(), []
    for wp in final_state.get("last_scorecard", {}).get("weak_points", []):
        if wp and wp not in seen:
            seen.add(wp)
            result.append(wp)
    return result
