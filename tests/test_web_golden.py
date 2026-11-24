"""golden 双锚双端共锚——pytest 侧。

状态锚 docs/contract/session-state.sample.json：awaiting 半场（开场已答、
q1 待答）。两端各自产出、剥传输层生成值（session_id/at）后与样本全等：
- 本文件：gateway.session_view 对「携带 Mock 剧本文本的引擎形 state」的输出
  ——文本取自 webapp/src/lib/mock/script.ts 常量（vitest 侧唯一能产出的
  文本）；引擎形 fidelity 由 test_web_gateway/test_web_detail 的真 dump
  测试另锁，golden 只锁双端契约值
- webapp/src/lib/mock/golden.test.ts：驱动真 Mock store 同场景输出同一样本

样本不存 session_id/at（生成值不属漂移监控面 ②）。改任一样本字段 →
两端同时红（防「双绿但各测各的」）。

聚合锚 docs/contract/final-aggregation.sample.json：每行是 wire + engine
字段并集（单一输入源，两端各取所需）；本文件断言 gateway.overall_score
，vitest 侧断言 buildFinal.overall + reportLogic.dimAverages 同值。
舍入口径双端两条锁定语义各有机器闸：half-up-tie
（overall 恰 3.25 → 3.3）锁半值进位（Python 内建 round 是银行家舍入会得
3.2）；repeating-mean-tie（行和 3,4,4,4 → 1.2）锁外层求和朴素左折叠
（CPython≥3.12 内建 sum() 是 Neumaier 补偿求和，会得 1.3）；另
eval-failed-excluded 用例锁「空分行不进聚合」。

skip 语义不进双端全等（④：Mock 的 system 提示行/全角括号与引擎
半角哨兵三方不一致，归一超出规范化职责）——pytest 侧哨兵锁在
test_web_gateway.py::test_skip_sentinel_from_engine_function，
vitest 侧独立断言在 golden.test.ts。

本文件不 import fastapi（gateway/schemas 纯 pydantic）——与
test_web_gateway.py 同款，无 [api] 环境可跑。
"""
import json
from pathlib import Path

import pytest

import interviewer.web.gateway as gateway
from interviewer.web.schemas import SessionState

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "docs" / "contract"

# 与 docs/contract/session-state.sample.json、webapp/src/lib/mock/script.ts
# 三点同源的场景常量（改其一而不改其余 → 对应端红，这正是锚的用途）
OPENING_TEXT = (
    "你好，我是本场「后端开发」模拟面试的面试官。整场大约 10 个问题，"
    "每答完一题我会视情况追问或换题。开始之前，请先用一两分钟做个自我"
    "介绍：教育背景、最近一段经历，以及你最有把握的方向。"
)
ANSWER_TEXT = (
    "自我介绍：后端工程师，五年后端经验，主要做高并发服务与中间件，"
    "最有把握的方向是分布式系统。"
)
Q1_TEXT = (
    "先从并发聊起。讲讲线程池的核心参数是怎么协同工作的：任务提交之后，"
    "内部的执行流程是怎样的？为什么需要拒绝策略？"
)
Q1_TOPIC = "并发与线程池"


def _golden_state() -> dict:
    """Mock 剧本场景的引擎形 state（awaiting @ q1）。只含视图路径读到的
    字段——完整 18 字段形状的真 dump 锁在 test_web_gateway 的 fixture。"""
    return {
        "session_id": "golden-awaiting",
        "position": "后端开发",
        "transcript": [
            {"role": "interviewer", "content": OPENING_TEXT, "phase": "intro"},
            {"role": "candidate", "content": ANSWER_TEXT, "topic": ""},
            {
                "role": "interviewer",
                "content": Q1_TEXT,
                "kind": "issue",
                "module": "project",
                "topic": Q1_TOPIC,
            },
        ],
        "current_topic": Q1_TOPIC,
        "issued_keys": [Q1_TOPIC],
        "pending_question": Q1_TEXT,
        "topic_results": [],
    }


def test_state_anchor_gateway_view_equals_sample():
    sample = json.loads(
        (CONTRACT_DIR / "session-state.sample.json").read_text(encoding="utf-8")
    )
    view = gateway.session_view(
        session_id="golden-awaiting",
        values=_golden_state(),
        pending={"kind": "question", "question": Q1_TEXT},
        status="awaiting",
        max_questions=10,
    )
    assert isinstance(view, SessionState)
    # 与路由 response_model_exclude_none 同一序列化面（Always）
    payload = view.model_dump(exclude_none=True)
    # ② 剥除传输层生成值；at 不建模本就无键，这里自证契约
    payload.pop("session_id")
    assert "at" not in {k for m in payload["transcript"] for k in m}
    assert payload == sample


def test_state_anchor_guard_pending_text_drift_fail_loud():
    """ 定案不变量的机器闸（T6-T8 合并评审确认项①补锁）：awaiting 态
    transcript 末条文本 ≠ pending 载荷 question → EngineError。前端零兜底
    依赖该不变量——弱化/删除守卫本测试即红。"""
    with pytest.raises(gateway.EngineError, match="末条与 pending_question 不一致"):
        gateway.session_view(
            session_id="golden-drift",
            values=_golden_state(),
            pending={"kind": "question", "question": Q1_TEXT + "（文本漂移）"},
            status="awaiting",
            max_questions=10,
        )


def test_state_anchor_guard_empty_transcript_fail_loud():
    """守卫的 last is None 分支：opening 形空 transcript（issued_keys 空 →
    current_slot 走 opening 槽位，不触发题号校验和，恰好到达守卫）+ 挂起
    载荷 → EngineError。"""
    values = {
        "session_id": "golden-empty",
        "position": "后端开发",
        "transcript": [],
        "current_topic": "",
        "issued_keys": [],
        "pending_question": OPENING_TEXT,
        "topic_results": [],
    }
    with pytest.raises(gateway.EngineError, match="末条与 pending_question 不一致"):
        gateway.session_view(
            session_id="golden-empty",
            values=values,
            pending={"kind": "question", "question": OPENING_TEXT},
            status="awaiting",
            max_questions=10,
        )


def test_aggregation_anchor_overall_equals_sample():
    doc = json.loads(
        (CONTRACT_DIR / "final-aggregation.sample.json").read_text(encoding="utf-8")
    )
    cases = doc["cases"]
    assert len(cases) >= 3  # 用例集缩水即红——锚不许静默变浅
    for case in cases:
        got = gateway.overall_score(case["topic_results"])
        assert got == case["expected"]["overall"], (
            f"{case['name']}: gateway.overall_score={got} "
            f"≠ 样本 {case['expected']['overall']}"
        )
