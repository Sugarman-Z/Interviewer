"""prompt 模板集中地（独立成模块，供后续 inspect.getsource 契约测试锁定格式约定）。"""
from interviewer.agents.prompts.evaluator import EVALUATOR_SYSTEM
from interviewer.agents.prompts.issue_question import (
    ISSUE_QUESTION_SYSTEM,
    ISSUE_QUESTION_TOOLS_ADDENDUM,
)
from interviewer.agents.prompts.report_writer import MODULE_SUMMARY_PROMPT, REPORT_SYSTEM

__all__ = [
    "EVALUATOR_SYSTEM",
    "ISSUE_QUESTION_SYSTEM",
    "ISSUE_QUESTION_TOOLS_ADDENDUM",
    "MODULE_SUMMARY_PROMPT",
    "REPORT_SYSTEM",
]
