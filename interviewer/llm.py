"""LLM 客户端工厂：quick/deep 双档槽位（zhipu 真端点 / mock 离线假 LLM）。

结构化输出用 function_calling——智谱兼容端点 json_schema 全炸（Invalid JSON），
调用方经 utils/structured.invoke_structured_or_freetext 透传 method。
"""
import json
import os

from interviewer.dataflows.errors import SourceNotConfiguredError


def create_llm_client(tier: str, config: dict):
    """tier: 'quick'（提问/出题/小结等高频调用）| 'deep'（终场报告/未来评分）。"""
    tier = tier or "quick"
    if config.get("llm_provider") == "mock":
        return MockLLM(tier)

    from langchain_openai import ChatOpenAI

    model = config["quick_llm"] if tier == "quick" else config["deep_llm"]
    api_key = os.environ.get(config["api_key_env"], "")
    if not api_key:
        raise SourceNotConfiguredError(
            f"未设置环境变量 {config['api_key_env']}。"
            f"请复制 .env.example 为 .env 填入 key，"
            f"或设置 INTERVIEWER_LLM_PROVIDER=mock 离线体验。"
        )
    # 厂商扩展参数经 OpenAI SDK 的 extra_body 透传（langchain 不感知，原样进请求体）
    extra_body = {}
    effort = config.get("llm_reasoning_effort", "")
    if effort:
        extra_body["reasoning_effort"] = effort
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=config["llm_base_url"],
        timeout=config["llm_timeout"],
        max_retries=config["llm_max_retries"],
        extra_body=extra_body or None,
    )


class MockLLM:
    """离线假 LLM：按 prompt 标记分流（评分/核验/签发，见 invoke），各回 canned
    JSON，形状对齐 schemas；无 with_structured_output，structured.py 捕 AttributeError
    自动降 freetext 路径。计数器轮换考点，测试勿并行共享同一 CoachGraph。
    """

    _ISSUE_CYCLE = [
        {
            "done": False, "reason": "mock 签发", "module": "project",
            "topic": "项目整体架构",
            "text": "介绍一下你简历里最核心的一个项目：它解决什么问题，你负责哪部分？",
            "source_type": "generated", "source_ref": "mock 简历主项目",
            "followup_hints": [],
        },
        {
            "done": False, "reason": "mock 签发", "module": "project",
            "topic": "技术选型",
            "text": "这个项目里你做过哪些技术选型？当时为什么选它而不是备选方案？",
            "source_type": "generated", "source_ref": "mock 技术选型",
            "followup_hints": [],
        },
        {
            "done": False, "reason": "mock 签发", "module": "project",
            "topic": "难点与结果",
            "text": "这个项目遇到的最大困难是什么，最后结果如何衡量？",
            "source_type": "generated", "source_ref": "mock 难点结果",
            "followup_hints": [],
        },
    ]

    # 评分应答：三维 3 分 + action=advance，mock 全流程每答必换题；
    # 追问路径由测试经 spec.scorecard 注入 action="followup" 走
    _SCORECARD = {
        "technical_depth": 3,
        "correctness": 3,
        "communication": 3,
        "evidence": {
            "technical_depth": "（mock）回答涉及了相关内容",
            "correctness": "（mock）未见事实性错误",
            "communication": "（mock）能说清大意",
        },
        "weak_points": [],
        "action": "advance",
        "followup_text": "",
    }

    def __init__(self, tier: str = "quick"):
        self.tier = tier
        self._issue_idx = 0

    def bind_tools(self, tools):
        """工具循环兼容：透传返回 self（不真绑，mock 不发工具调用）。
        mock 回复 tool_calls 恒空，环首轮即断、content 走解析腿——
        顺带回归有工具路径的降级腿。"""
        return self

    def invoke(self, prompt, config=None, **kwargs):
        from langchain_core.messages import AIMessage

        p = str(prompt)
        # 评分分流排在签发之前：评分 prompt 内嵌候选人原文（{answer}/{qa_history}），
        # 若候选人回答里恰含签发标记而此分支在后，评分调用会被劫持成签发 JSON
        if "技术面试评估官" in p:
            return AIMessage(content=json.dumps(self._SCORECARD, ensure_ascii=False))
        # 进件核验分流排在签发之前（同因：核验 prompt 里写着"请输出 JSON"，
        # 排在后会被劫持成签发应答）。标记须与 intake_guard.GUARD_MARKER 保持一致
        if "进件核验官" in p:
            canned = {k: {"verdict": "valid", "reason": "（mock）视为有效"}
                      for k in ("position", "resume", "jd")}
            return AIMessage(content=json.dumps(canned, ensure_ascii=False))
        if "请输出 JSON" in p:
            item = dict(self._ISSUE_CYCLE[self._issue_idx % len(self._ISSUE_CYCLE)])
            self._issue_idx += 1
            return AIMessage(content=json.dumps(item, ensure_ascii=False))
        return AIMessage(content=f"（{self.tier} mock 回复：按指令处理。）")
