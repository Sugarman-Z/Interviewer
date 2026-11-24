"""路由决策表单测（纯离线，零 LLM）。
action 驱动决策表（含空转闸：action=followup 而追问句空白 → 换题）。
优先级链 + route_after_issue 的真值判断是评分校准前最重要的确定性护栏，
逐分支锁死。标签拼写与 setup.py 的 path_map key 是同一份合同——
flow_logic 一侧由行为测试锁死，map 一侧由 test_path_map_keys_lock_the_
other_side 锁死（双侧都必须真的被断言碰过，map 侧拼错了 198 项照绿）。
"""
import pytest

from interviewer.graph.flow_logic import FlowLogic

CONFIG = {"max_followups": 3, "max_total_turns": 10}


def make_state(total=0, followups=0, scorecard=None, current_question=...):
    state = {
        "followup_state": {
            "topic": "t",
            "qa_history": "",
            "followup_count": followups,
            "total_count": total,
        },
        "last_scorecard": scorecard or {},
    }
    if current_question is not ...:
        state["current_question"] = current_question
    return state


def follow_card(text="追问句：ISR 和 acks 怎么配合？"):
    return {"action": "followup", "followup_text": text, "eval_failed": False}


@pytest.fixture()
def flow():
    return FlowLogic(CONFIG)


# route_after_issue：真值判断
def test_route_after_issue_asks_when_holding_question(flow):
    assert flow.route_after_issue(make_state(current_question={"text": "题"})) == "ask"


def test_route_after_issue_ends_on_cleared_state(flow):
    """done 清场后 current_question 是空 dict——空 dict 真值为 False。
    用 "is not None" 判断会把收场当出题（WP3 验收抓到的 bug，此处锁死）。"""
    assert flow.route_after_issue(make_state(current_question={})) == "end"
    assert flow.route_after_issue({}) == "end"  # 字段缺席同样收场


# route_after_answer：优先级链 action 版）
def test_total_turns_fuse_has_priority(flow):
    """时长保险丝一票否决：即便 action 说追问，也必须收模块。"""
    state = make_state(total=10, scorecard=follow_card())
    assert flow.route_after_answer(state) == "module_summary"


def test_eval_failed_goes_explicit_downgrade(flow):
    """评分失败 → 显式降级换题（绝不默认及格后继续纠缠）——即使失败前的
    裁决是 followup 也不得穿透（评分失败必须覆写 last_scorecard 的守门行）。"""
    state = make_state(scorecard={"eval_failed": True, "action": "followup",
                                  "followup_text": "旧追问"})
    assert flow.route_after_answer(state) == "issue"


def test_followup_fuse_is_breaker_not_gate(flow):
    """追问熔断线（followup_count 含主问轮）：到线必换题——常规追问归模型
    action，这里只是极端兜底 从常规闸门降级）。"""
    state = make_state(followups=3, scorecard=follow_card())
    assert flow.route_after_answer(state) == "issue"


def test_advance_moves_on(flow):
    """action=advance（答得好/无追问价值的判断归还模型）→ 换题。"""
    state = make_state(scorecard={"action": "advance", "followup_text": "",
                                  "eval_failed": False})
    assert flow.route_after_answer(state) == "issue"


def test_missing_action_is_conservative_issue(flow):
    """评分卡缺 action（旧形状/异常中间态）→ 保守换题，绝不误入追问。"""
    assert flow.route_after_answer(make_state(scorecard={})) == "issue"


def test_blank_followup_text_is_spin_guard(flow):
    """空转闸：action=followup 而追问句空/纯空白 → 换题（同空题纪律：
    手里没货不问；strip 防模型回空白串绕过）。"""
    assert flow.route_after_answer(make_state(scorecard=follow_card(""))) == "issue"
    assert flow.route_after_answer(make_state(scorecard=follow_card("   "))) == "issue"


def test_followup_label_contract(flow):
    """追问标签契约：返回 "followup" ↔ setup.ANSWER_PATH_MAP 的 key
（WP3 曾写成 "follow_up" 与 map 键错位，图一跑到此分支就炸——锁死拼写）。"""
    assert flow.route_after_answer(make_state(scorecard=follow_card())) == "followup"


def test_module_summary_goes_to_report(flow):
    assert flow.route_after_module_summary({}) == "report_writer"


def test_path_map_keys_lock_the_other_side():
    """标签合同的 setup 半边（教训"两侧必须测试锁死"的另一半）：
    三张 path_map 的 key 集必须与上面各行为测试钉死的 flow_logic 返回
    字面量逐一相等——route_after_answer 能出 {followup, issue,
    module_summary}（前两者见上，module_summary 见时长保险丝测试），
    route_after_issue 出 {ask, end}，route_after_module_summary 出
    {report_writer}。map 侧任何拼写改动在这里当场红，而不是线上
    跑到该分支才炸（module_summary 分支冒烟测试根本到不了）。"""
    from interviewer.graph import setup

    assert set(setup.ANSWER_PATH_MAP) == {"followup", "issue", "module_summary"}
    assert set(setup.ISSUE_PATH_MAP) == {"ask", "end"}
    assert set(setup.MODULE_PATH_MAP) == {"report_writer"}
