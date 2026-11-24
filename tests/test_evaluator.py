"""Evaluator 节点级测试。
节点级 = 直接调 make_evaluator 返回的节点函数、检查 update dict——不建图。
FakeEvalLLM 逼降级 freetext 腿（无 with_structured_output → AttributeError），
按剧本回 JSON 文本或垃圾文本，并记录 prompt（hint 轮次 / budget /
qa_history 的断言都从这份记录读）。
"""
import json
from types import SimpleNamespace

from interviewer.agents.evaluator import make_evaluator
from interviewer.graph.score_parser import validate_scorecard

SPEC = {
    "structured_output_method": "function_calling",
    "max_followups": 3,
    "max_total_turns": 24,
}

_CARD_FOLLOW = {
    "technical_depth": 2, "correctness": 2, "communication": 3,
    "evidence": {"technical_depth": "呃，Kafka 我用过"},
    "weak_points": ["ISR 机制不明"],
    "action": "followup",
    "followup_text": "那 ISR 副本和 acks 是怎么配合保证不丢的？",
}
_CARD_ADVANCE = {
    "technical_depth": 4, "correctness": 4, "communication": 4,
    "evidence": {}, "weak_points": [],
    "action": "advance", "followup_text": "",
}


class FakeEvalLLM:
    """剧本 LLM：freetext 腿专用（invoke 弹剧本、记录 prompt）。
    replies 耗尽再被调用 = 测试期望零调用却被调了 → IndexError 当场红。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def with_structured_output(self, schema, **kwargs):
        raise AttributeError("fake：逼降级 freetext 腿")

    def invoke(self, prompt, **kwargs):
        self.prompts.append(str(prompt))
        return SimpleNamespace(content=self.replies.pop(0))


def make_eval_state(**over):
    base = {
        "position": "后端开发",
        "current_topic": "Kafka 高可用",
        "pending_question": "三节点集群 leader 宕机时如何保证消息不丢？",
        "last_answer": "呃，Kafka 我用过，具体原理没深入了解。",
        "current_question": {
            "module": "project", "topic": "Kafka 高可用",
            "followup_hints": ["ISR 与 acks？", "acks=all 时延迟怎么权衡？"],
        },
        "followup_state": {
            "topic": "Kafka 高可用", "qa_history": "",
            "followup_count": 0, "total_count": 0,
        },
        "transcript": [],
        "topic_results": [],
        "consecutive_llm_failures": 1,   # 非零起步：成功清零才是断言点
    }
    base.update(over)
    return base


def _llm_ok(card):
    return FakeEvalLLM([json.dumps(card, ensure_ascii=False)])


# 成功腿：advance
def test_advance_scores_and_moves_on():
    node = make_evaluator(_llm_ok(_CARD_ADVANCE), SPEC)
    ret = node(make_eval_state())
    sc = ret["last_scorecard"]
    assert sc["action"] == "advance" and sc["technical_depth"] == 4
    assert ret["consecutive_llm_failures"] == 0            # 成功清零（非零起步）
    entry = ret["topic_results"][0]
    assert entry["module"] == "project" and entry["topic"] == "Kafka 高可用"
    assert entry["scores"]["technical_depth"] == 4 and entry["eval_failed"] is False
    assert entry["action"] == "advance"
    fs = ret["followup_state"]
    assert fs["followup_count"] == 1 and fs["total_count"] == 1   # 双计数 +1
    assert "[问] 三节点" in fs["qa_history"] and "[答] 呃" in fs["qa_history"]
    assert "pending_question" not in ret                     # advance 不写追问
    assert "transcript" not in ret                           # 也不开口


# 成功腿：followup
def test_followup_writes_question_and_transcript():
    node = make_evaluator(_llm_ok(_CARD_FOLLOW), SPEC)
    ret = node(make_eval_state())
    assert ret["pending_question"] == _CARD_FOLLOW["followup_text"]
    entry = ret["transcript"][-1]
    assert entry["role"] == "interviewer" and entry["kind"] == "followup"
    assert entry["module"] == "project" and entry["topic"] == "Kafka 高可用"


# 幻影闸（审计洞一）：保险丝将触时追问不落账
def test_followup_suppressed_at_followup_fuse():
    """followup_count=2、熔断线 3——自增后 3≥3，路由必改道 issue；节点若照样
    落账，transcript 里就多一条候选人从未见过的追问（幻影条目）。评分照实留痕。"""
    state = make_eval_state(followup_state={
        "topic": "Kafka 高可用", "qa_history": "",
        "followup_count": 2, "total_count": 5,
    })
    ret = make_evaluator(_llm_ok(_CARD_FOLLOW), SPEC)(state)
    assert ret["last_scorecard"]["action"] == "followup"   # 评分照实留痕
    assert "pending_question" not in ret and "transcript" not in ret  # 追问不落账


def test_followup_suppressed_at_total_fuse():
    """全场保险丝同理：total_count=23、上限 24——自增后即触，追问不落账。"""
    state = make_eval_state(followup_state={
        "topic": "Kafka 高可用", "qa_history": "",
        "followup_count": 0, "total_count": 23,
    })
    ret = make_evaluator(_llm_ok(_CARD_FOLLOW), SPEC)(state)
    assert "pending_question" not in ret


# 失败腿（审计洞二：必须整块覆写）
def test_llm_failure_overwrites_scorecard_and_counts():
    """失败必须整块覆写 last_scorecard——否则上轮 action="followup" 穿透
    路由②被④读到，旧追问再问一遍。freetext 腿默认重试 1 次 = 2 拍垃圾。"""
    state = make_eval_state(
        last_scorecard={"action": "followup", "followup_text": "旧追问"},
    )
    llm = FakeEvalLLM(["我不会评分", "还是不会"])
    ret = make_evaluator(llm, SPEC)(state)
    assert ret["last_scorecard"] == validate_scorecard(None)   # 失败 dict 整块覆写
    assert ret["consecutive_llm_failures"] == 2                # 旧值 1 + 1
    entry = ret["topic_results"][0]
    assert entry["eval_failed"] is True and entry["scores"] == {}   # 渲染走"无分"腿
    fail_entry = ret["transcript"][-1]
    assert fail_entry["kind"] == "eval_failed" and fail_entry["role"] == "system"
    assert "pending_question" not in ret                       # 失败绝不写追问


# 追问树轮次消费
def test_hint_consumed_by_round():
    """第 1 轮答完给 hints[0]，第 2 轮给 hints[1]——下标 = 自增前
    followup_count（用自增后值，首轮会变 hints[-1] 静默回绕到最后一条）。"""
    llm = _llm_ok(_CARD_FOLLOW)
    make_evaluator(llm, SPEC)(make_eval_state())
    assert "ISR 与 acks？" in llm.prompts[0]
    assert "acks=all 时延迟" not in llm.prompts[0]

    state2 = make_eval_state(followup_state={
        "topic": "Kafka 高可用", "qa_history": "",
        "followup_count": 1, "total_count": 1,
    })
    llm2 = _llm_ok(_CARD_FOLLOW)
    make_evaluator(llm2, SPEC)(state2)
    assert "acks=all 时延迟" in llm2.prompts[0]


def test_hint_exhausted_falls_back():
    """hints 用尽 → "（无）"（模型回落打自己产出的 weak_points，prompt 规则 5）。
    兜底题/mock 题 hints 恒空——回落路径是常态不是边缘。"""
    state = make_eval_state(followup_state={
        "topic": "Kafka 高可用", "qa_history": "",
        "followup_count": 2, "total_count": 2,
    })
    llm = _llm_ok(_CARD_FOLLOW)
    make_evaluator(llm, SPEC)(state)
    assert "ISR 与 acks？" not in llm.prompts[0]
    assert "acks=all 时延迟" not in llm.prompts[0]
    assert "（无）" in llm.prompts[0]


def test_followup_budget_rendered():
    """{followup_budget} 与路由同一份合同：已答轮数 + 熔断线必须进 prompt。"""
    llm = _llm_ok(_CARD_ADVANCE)
    make_evaluator(llm, SPEC)(make_eval_state())
    assert "已答 0 轮" in llm.prompts[0] and "熔断线 3" in llm.prompts[0]


# 注入通道（冒烟/测试用）
def test_scorecard_injection_skips_llm():
    """注入 = 默认值合并语义（同桩先例）：稀疏 dict 不经 pydantic 直接并进
    默认三维——过 schema 反而会被三维必填炸。空剧本 = 被调即 IndexError。"""
    spec = dict(SPEC, scorecard={"action": "followup", "followup_text": "注入追问"})
    ret = make_evaluator(FakeEvalLLM([]), spec)(make_eval_state())
    assert ret["pending_question"] == "注入追问"
    assert ret["last_scorecard"]["technical_depth"] == 3     # 默认值合并
    assert ret["last_scorecard"]["action"] == "followup"


# qa_history 累加（{qa_history} 的数据源）
def test_qa_history_accumulates_across_rounds():
    """第一轮的问答必须出现在第二轮 prompt 里——丢了 prompt 恒空，
    且除本文件外没有任何测试会替你红。"""
    llm1 = _llm_ok(_CARD_FOLLOW)
    ret1 = make_evaluator(llm1, SPEC)(make_eval_state())
    state2 = make_eval_state(
        followup_state=ret1["followup_state"],
        pending_question=ret1["pending_question"],
        last_answer="ISR 就是同步副本吧，具体 acks 配合没研究过。",
    )
    llm2 = _llm_ok(_CARD_FOLLOW)
    make_evaluator(llm2, SPEC)(state2)
    assert "三节点集群 leader" in llm2.prompts[0]     # 第一轮的问
    assert "没深入了解" in llm2.prompts[0]            # 第一轮的答
