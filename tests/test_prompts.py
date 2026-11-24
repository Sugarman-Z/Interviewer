"""prompt 契约测试：占位符集合与节点传参逐字对齐。
为什么值得测：占位符错位与路由标签错位（"follow_up" vs "followup"）同族
——纯字符串约定，无编译器检查，跑到该分支才炸。这里用 string.Formatter
抽出每个模板的占位符集合，与"节点调用 .format() 时实际传的键"断言集合
相等：多一个、少一个都会红。另有两条合同锁死：① MockLLM 双分流标记
（"请输出 JSON"只在签发 prompt、"技术面试评估官"只在评分 prompt）；②
评分→追问的 weak_points 锚（定义与追问指向同在 EVALUATOR_SYSTEM 单文件）。
"""
from string import Formatter

from interviewer.agents import prompts


def placeholders(template: str) -> set:
    """抽出 format 模板的全部命名字段名（忽略格式规格与嵌套下标）。"""
    return {fname for _, fname, _, _ in Formatter().parse(template) if fname}


def test_registry_complete():
    """__all__ 里的常量全部真实存在且非空（防删除/改名后注册表留空壳）。"""
    for name in prompts.__all__:
        value = getattr(prompts, name)
        assert isinstance(value, str) and value.strip(), f"{name} 应为非空字符串"


def test_issue_question_placeholders():
    """与 issue_question._build_prompt 的 8 个传参逐字对齐。"""
    assert placeholders(prompts.ISSUE_QUESTION_SYSTEM) == {
        "position", "candidate_profile", "jd_requirements", "self_intro",
        "topic_results", "max_questions", "issued_count", "remaining_budget",
    }


def test_evaluator_placeholders():
    """与 evaluator 节点的 7 个传参对齐（含 followup_hints/followup_budget）。"""
    assert placeholders(prompts.EVALUATOR_SYSTEM) == {
        "position", "topic", "question", "qa_history", "answer",
        "followup_hints", "followup_budget",
    }


def test_report_placeholders():
    assert placeholders(prompts.MODULE_SUMMARY_PROMPT) == {"module", "transcript"}
    assert placeholders(prompts.REPORT_SYSTEM) == {"position", "module_report", "transcript"}


def test_tools_addendum_placeholders():
    """工具版 ADDENDUM 两个占位符：轮数上限 + 调用次数上限，
    与 issue_question._build_prompt 的 format 传参对齐。"""
    assert placeholders(prompts.ISSUE_QUESTION_TOOLS_ADDENDUM) == {
        "max_tool_rounds", "max_calls",
    }


def test_mock_marker_contract():
    """MockLLM 按"请输出 JSON"分流签发应答。标记只允许在签发 prompt 里——
    若出现在别处，mock 会对那个节点返回签发 JSON，测试静默错乱。"""
    assert "请输出 JSON" in prompts.ISSUE_QUESTION_SYSTEM
    for name in prompts.__all__:
        if name == "ISSUE_QUESTION_SYSTEM":
            continue
        assert "请输出 JSON" not in getattr(prompts, name), name


def test_weak_points_anchor_contract():
    """评分→追问的锚合同（合并后单文件）：weak_points 的定义（规则 3）与
    追问指向（规则 5"优先打在你指出的薄弱处"）都在 EVALUATOR_SYSTEM 里——任何
    一半被删，合同即断。原两文件接力（FOLLOWUP_ASKER_PROMPT 持另一半）已随
    FollowupAsker 退役（2026-11-13）。"""
    assert prompts.EVALUATOR_SYSTEM.count("weak_points") >= 2


def test_evaluator_mock_anchor_contract():
    """MockLLM 评分分流锚（llm.py 检测"技术面试评估官"回固定 Scorecard JSON，
     起）：锚串只允许出现在评分 prompt——出现在别处会把那个节点的调用
    劫持成评分应答。分流检测须排在"请输出 JSON"之前（评分 prompt 内嵌候选
    人原文，防用户输入恰含签发标记时评分调用被劫持）——顺序约定在 llm.py，
    此处锁锚串归属。"""
    assert "技术面试评估官" in prompts.EVALUATOR_SYSTEM
    for name in prompts.__all__:
        if name == "EVALUATOR_SYSTEM":
            continue
        assert "技术面试评估官" not in getattr(prompts, name), name
