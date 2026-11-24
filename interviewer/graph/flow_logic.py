"""条件边函数（只读）+ 路由决策表。

铁律：路由函数体只允许读 state 和 return 字面量——langgraph 结构上不给
条件边写通道，但函数拿到的是 state 引用，就地 .append() 技术上可行且
不进 checkpoint、出 bug 无迹可查；所有变更都在节点内。

标签契约：本文件每个路由函数的返回值集合 ↔ setup.py 对应 path_map 的
key 集合，任何一边改拼写另一边必须同步（纯字符串约定，无编译器检查）。
"""


class FlowLogic:
    def __init__(self, config: dict):
        self.max_followups = config["max_followups"]
        self.max_total_turns = config["max_total_turns"]

    # IssueQuestion 之后：手里有没有题（真值判断，不能用 is not None）
    # done 清场后 current_question 是空 dict {}：空 dict "is not None" 为真，
    # 会把收场当出题路由去 AwaitAnswer 问空题（教训在动态架构下的形态）。
    def route_after_issue(self, state: dict) -> str:
        return "ask" if state.get("current_question") else "end"

    # 每答之后的路由（核心决策表，优先级从上到下；"换题"=重新签发）
    #    版：LLM 判断驱动的对话状态机——结构化 action 字段驱动路由，
    #   "答得好换题"的判断归还模型（删 score≥4 启发式与 worth_followup 字段）
    #   1 全场轮数≥熔断线       → ModuleSummary（时长保险丝，一票否决）
    #   2 评分失败(eval_failed) → 换题（显式降级，绝不默认及格，报告里可见）
    #   3 本题已答轮数≥熔断线   → 换题（followup_count 含主问轮；极端兜底而非常规闸门）
    #   4 action≠followup 或追问句空白 → 换题（空转闸并入：手里没货不问，同空题纪律）
    #   5 否则                 → 同考点追问（追问句由 Evaluator 同调用产出）
    def route_after_answer(self, state: dict) -> str:
        followup = state.get("followup_state") or {}
        if followup.get("total_count", 0) >= self.max_total_turns:
            return "module_summary"
        scorecard = state.get("last_scorecard") or {}
        if scorecard.get("eval_failed"):
            return "issue"
        if followup.get("followup_count", 0) >= self.max_followups:
            return "issue"
        # 空转闸并入此行：action 缺席/advance/追问句空白（strip 防模型回空白串绕过）
        # 都换题——评分卡缺 action 同样走保守出口
        text = (scorecard.get("followup_text") or "").strip()
        if scorecard.get("action") != "followup" or not text:
            return "issue"
        return "followup"

    # 模块小结之后
    # 单模块恒进报告；Phase D 多模块在此扩展为路由到下一模块的开题节点。
    def route_after_module_summary(self, state: dict) -> str:
        return "report_writer"
