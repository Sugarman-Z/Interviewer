"""CoachGraph：门面 + 组合根。只做依赖注入与编译，不实现节点逻辑——
建图在 setup，路由在 flow_logic，结算在 review_settler，解析在
score_parser，落盘在 session_runner。每个部件可单独替换。
"""
import hashlib

from interviewer.graph.flow_logic import FlowLogic
from interviewer.llm import create_llm_client


def graph_signature(config: dict) -> str:
    """图形状签名：掺进 thread_id——图拓扑/路由上限变了，旧 checkpoint 自动
    落到新线程，而不是静默续跑在错的图上。掺哪些键 = 路由决策表读的"上限"
    全集（graph_version/modules/max_followups/max_total_turns/max_questions/
    max_consecutive_llm_failures）；max_recur_limit 是 invoke 期预算不掺。
    键集与 default_config"图形状"区注释互为合同，测试正反两向锁死。
    """
    raw = "|".join(
        [
            str(config.get("graph_version", "")),
            ",".join(config.get("modules", [])),
            str(config.get("max_followups", "")),
            str(config.get("max_total_turns", "")),
            str(config.get("max_questions", "")),
            str(config.get("max_consecutive_llm_failures", "")),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class CoachGraph:
    def __init__(self, config: dict, checkpointer=None):
        self.config = config
        self.checkpointer = checkpointer
        self.flow = FlowLogic(config)
        # 双档客户端在组合根创建一次，角色工厂按职责挑选档位
        self.quick_llm = create_llm_client("quick", config)
        self.deep_llm = create_llm_client("deep", config)

    def build(self):
        """编译图。checkpointer 缺失时 interrupt 会静默失败—— refuse 编译。"""
        if self.checkpointer is None:
            raise ValueError("checkpointer 不能为空：无 checkpointer 时 interrupt 不报错但无法恢复")
        from interviewer.graph.setup import build_graph

        return build_graph(self)

    def invoke_config(self, session_id: str) -> dict:
        """每次 invoke/stream/resume 都要带的 config：thread_id + 显式 recursion_limit
（langgraph 默认 25，检索工具循环会撞 GraphRecursionError）。"""
        return {
            "configurable": {"thread_id": f"{session_id}::{graph_signature(self.config)}"},
            "recursion_limit": self.config["max_recur_limit"],
        }
