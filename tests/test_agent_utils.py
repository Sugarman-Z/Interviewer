"""agent_utils 工具函数直测：transcript 取值 / resume 载荷 / 空值 marker / 截断。

这些函数此前只被冒烟测试间接覆盖——间接覆盖抓的是"图炸了"，
直测抓的是"返回值悄悄不对"（如 first/last 取反这类静默错误）。
"""
from interviewer.agents.utils.agent_states import RESUME_ANSWER
from interviewer.agents.utils.agent_utils import (
    answer_or_marker,
    clip,
    current_module,
    first_candidate_text,
    last_candidate_text,
    resume_text,
    transcript_lines,
)


def test_first_vs_last_candidate_text():
    """first 取自我介绍（签发上下文用），last 取最新回答（Planner 时代遗产，
    现主要供调试）——两端各取正确且互不相同。"""
    state = {
        "transcript": [
            {"role": "interviewer", "content": "请自我介绍"},
            {"role": "candidate", "content": "自我介绍内容"},
            {"role": "interviewer", "content": "问题一"},
            {"role": "candidate", "content": "回答一"},
        ]
    }
    assert first_candidate_text(state) == "自我介绍内容"
    assert last_candidate_text(state) == "回答一"


def test_candidate_text_empty():
    """无候选人发言时返回空串——IssueQuestion 的 prompt 会走"(无自我介绍)" marker。"""
    assert first_candidate_text({"transcript": []}) == ""
    assert last_candidate_text(
        {"transcript": [{"role": "interviewer", "content": "问"}]}
    ) == ""


def test_resume_text_answer_and_skip():
    """answer 取文本；skip/未知载荷给显式占位（防下游空值幻觉的纪律载体）。"""
    assert resume_text({"type": RESUME_ANSWER, "text": "我的回答"}) == "我的回答"
    assert resume_text({"type": "skip"}) == "(候选人跳过了此题)"
    assert resume_text({}) == "(候选人跳过了此题)"


def test_answer_or_marker():
    """空串与纯空白都算"缺席"——缺席必须显式标记，不能当"存在的空答案"。"""
    assert answer_or_marker("", "(无)") == "(无)"
    assert answer_or_marker("   ", "(无)") == "(无)"
    assert answer_or_marker("有内容", "(无)") == "有内容"


def test_clip():
    """超限截断加省略号；None 安全；不超限原样。"""
    assert clip("a" * 600, 500) == "a" * 500 + "…"
    assert clip(None, 500) == ""
    assert clip("短文本", 500) == "短文本"


def test_current_module():
    """ 收拢：三处 `(current_question or {}).get("module", "project")` 的
    唯一出口。字段缺席/空 dict 回落默认模块（真值判断，同 教训）。"""
    assert current_module({}) == "project"
    assert current_module({"current_question": {}}) == "project"
    assert current_module({"current_question": {"module": "behavior"}}) == "behavior"


def test_transcript_lines():
    """ 收拢：报告类 prompt 的 transcript 渲染唯一出口（原两处拼接）。"""
    state = {"transcript": [
        {"role": "interviewer", "content": "问"},
        {"role": "candidate", "content": "答"},
    ]}
    assert transcript_lines(state) == "[interviewer] 问\n[candidate] 答"
    assert transcript_lines({}) == ""
