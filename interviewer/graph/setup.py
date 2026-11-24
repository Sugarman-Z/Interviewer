"""建图：节点注册 + 条件边（全枚举 path_map + 保底出口）。

拓扑（7 节点，※=interrupt 挂起点）：
  START → OpeningIntro(零LLM) → AwaitIntro※ → IssueQuestion → route_after_issue
        route_after_issue{ask→AwaitAnswer※ | end→ModuleSummary}
  AwaitAnswer※ → Evaluator(真评分+裁决+追问句) → route_after_answer
        route_after_answer{followup→AwaitAnswer※ | issue→IssueQuestion | summary→ModuleSummary}
  ModuleSummary → ReportWriter → END

path_map 的 key 集与 flow_logic 各路由函数的返回值集合是同一份合同的两半。
"""
from langgraph.graph import END, START, StateGraph

from interviewer.agents.evaluator import make_evaluator
from interviewer.agents.interrupt_nodes import make_await_node
from interviewer.agents.issue_question import make_issue_question
from interviewer.agents.opening import opening_intro_node
from interviewer.agents.report_writer import make_report_writer
from interviewer.agents.utils.agent_states import InterviewState
from interviewer.dataflows import resolve_candidate_tool, resolve_search_tool
from interviewer.graph.flow_nodes import make_module_summary

# 路由返回值 → 节点名的全枚举映射（TA setup.py 的 #1088 防崩纪律：标签拼写
# 与 flow_logic 返回值逐一核对，错位=跑到该分支才炸的运行时错误）
ANSWER_PATH_MAP = {
    # followup 回到 AwaitAnswer：追问句已由 Evaluator 写进 pending_question
    "followup": "AwaitAnswer",
    "issue": "IssueQuestion",
    "module_summary": "ModuleSummary",  # 时长保险丝的保底出口
}
ISSUE_PATH_MAP = {"ask": "AwaitAnswer", "end": "ModuleSummary"}
MODULE_PATH_MAP = {"report_writer": "ReportWriter"}


def build_graph(coach):
    """coach: CoachGraph 实例（提供 quick_llm/deep_llm/flow/config）。"""
    builder = StateGraph(InterviewState)

    module_spec = {"key": "project", "label": "项目深挖"}
    issue_spec = {
        "max_questions": coach.config["max_questions"],
        "max_consecutive_llm_failures": coach.config["max_consecutive_llm_failures"],
        "structured_output_method": coach.config["structured_output_method"],
        # 工具循环开关：None = 无工具旧路径（corpus_vendors 空串时），
        # 存在 = 走 invoke_structured_with_tools。注意这里是 .get 不是 []——
        # 存在性即开关，缺失/关闭都必须走老路（零回归保证的根基）
        "search_tool": resolve_search_tool(coach.config),
        # 第二检索面：同样存在性即开关；工厂而非工具本体——
        # 简历/JD 全文是运行期 state，节点每次签发拿 state 文本现造适配器
        "candidate_tool_factory": resolve_candidate_tool(coach.config),
        "max_tool_rounds": coach.config["max_tool_rounds"],
        "max_tool_calls": coach.config["max_tool_calls"],
    }
    # Evaluator 两道保险丝的自查用料：必需键 [] 直取，缺了建图期就炸
    evaluator_spec = {
        "structured_output_method": coach.config["structured_output_method"],
        "max_followups": coach.config["max_followups"],
        "max_total_turns": coach.config["max_total_turns"],
    }

    builder.add_node("OpeningIntro", opening_intro_node)
    builder.add_node(
        "AwaitIntro",
        make_await_node(lambda state: {
            "kind": "question",
            "question": state.get("pending_question", ""),
        }),
    )
    builder.add_node("IssueQuestion", make_issue_question(coach.quick_llm, issue_spec))
    builder.add_node(
        "AwaitAnswer",
        make_await_node(lambda state: {
            "kind": "question",
            "question": state.get("pending_question", ""),
        }),
    )
    builder.add_node("Evaluator", make_evaluator(coach.deep_llm, evaluator_spec))
    builder.add_node("ModuleSummary", make_module_summary(coach.quick_llm, module_spec))
    builder.add_node("ReportWriter", make_report_writer(coach.deep_llm, module_spec))

    # 边
    builder.add_edge(START, "OpeningIntro")
    builder.add_edge("OpeningIntro", "AwaitIntro")
    builder.add_edge("AwaitIntro", "IssueQuestion")
    builder.add_conditional_edges(
        "IssueQuestion", coach.flow.route_after_issue, ISSUE_PATH_MAP
    )
    builder.add_edge("AwaitAnswer", "Evaluator")
    builder.add_conditional_edges(
        "Evaluator", coach.flow.route_after_answer, ANSWER_PATH_MAP
    )
    builder.add_conditional_edges(
        "ModuleSummary", coach.flow.route_after_module_summary, MODULE_PATH_MAP
    )
    builder.add_edge("ReportWriter", END)

    return builder.compile(checkpointer=coach.checkpointer)
