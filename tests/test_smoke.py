"""整图冒烟：mock LLM + InMemorySaver，脚本化 resume 驱动动态面试闭环。
两个用例覆盖两条主路径：① 签发主循环（mock 轮换 3 题、评分恒 advance →
预算收场 → 报告落盘）；② 追问路径（注入 action=followup + 追问句 → 每题
追问到熔断线再换题）。验证的组合机制：interrupt 挂起/恢复、__interrupt__
消费、动态签发、issued_keys 查重落账、topic_results 留痕、路由决策表、
ModuleSummary 重置、终场报告、落盘。
"""
import json
from pathlib import Path

import pytest

from interviewer.agents.evaluator import make_evaluator
from interviewer.default_config import get_config
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph
from interviewer.graph.session_driver import run_new_session
from interviewer.graph.session_runner import create_initial_state, persist_session


@pytest.fixture()
def env(tmp_path):
    """只备料不建图：第二测要先 monkeypatch 再 build，图必须在测试体内组装。"""
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 3
    cfg["max_followups"] = 2  # 钉死与默认解耦（默认已随  放宽为 3，改默认不该牵连本用例数字）
    saver, close = build_checkpointer(cfg)
    yield cfg, saver
    close()


def _answerer(answer_fn):
    """把 answer_fn(第几次回答) 包成 driver 的 on_interrupt 回调。

    2026-11-19 架构审查卡 C：驱动循环改走 graph/session_driver（与 CLI/
    headless 同一引擎），本文件不再手抄 __interrupt__ 消费循环。state 计数
    供调用方断言挂起次数。
    """
    state = {"n": 0}

    def on_interrupt(value):
        state["n"] += 1
        return {"type": "answer", "text": answer_fn(state["n"])}

    return on_interrupt, state


def test_dynamic_interview_loop(env):
    """动态签发主链路：3 题现场出 → 预算收场 → 全部留痕与落盘。"""
    cfg, saver = env
    coach = CoachGraph(cfg, checkpointer=saver)
    graph = coach.build()
    invoke_cfg = coach.invoke_config("smoke-dyn")
    initial = create_initial_state(
        session_id="smoke-dyn",
        position="后端开发",
        candidate_profile="候选人简历：做过 LangGraph 多智能体项目改造。",
        jd_requirements="",
    )

    on_interrupt, state = _answerer(lambda i: f"第 {i} 轮回答")
    result = run_new_session(graph, invoke_cfg, initial, on_interrupt)
    assert result.finished is True
    final = result.final_values

    # 自我介绍 + 3 次答题 = 4 次挂起（批式 plan_review 已退役，不再多一次）
    assert state["n"] == 4

    candidate = [e for e in final["transcript"] if e["role"] == "candidate"]
    assert len(candidate) == 4
    assert len(final["issued_keys"]) == 3                      # 3 题查重落账
    assert len(final["topic_results"]) == 3                    # 每答一条留痕
    assert final["topic_results"][0]["scores"]["technical_depth"] == 3
    assert final["interview_report"], "终场报告应产出"
    assert final["module_report"], "模块小结应产出"
    assert final["followup_state"]["total_count"] == 3         # 全场计数只增
    assert final["followup_state"]["followup_count"] == 0      # ModuleSummary 已重置
    kinds = [e.get("kind") for e in final["transcript"]]
    assert kinds.count("issue") == 3                           # 每题一次签发发言
    assert "issue_done" in kinds                               # 预算收场留痕（fail-visible）

    paths = persist_session(cfg, final)
    assert Path(paths["session"]).exists()
    assert Path(paths["report"]).exists()
    #  验收：检索留痕随会话 JSON 落盘（mock 会话不走工具路径，值为 []；
    # 留痕内容本身由 test_issue_question 的累加测试锁，这里锁的是落盘管道）
    saved = json.loads(Path(paths["session"]).read_text(encoding="utf-8"))
    assert "retrieval_trace" in saved


def test_followup_path_until_cap(env, monkeypatch):
    """追问路径：注入 action=followup+追问句 → 每题追问到熔断线（钉 max_followups=2）再换题。"""
    import interviewer.graph.setup as setup_mod

    followup_scorecard = {
        "technical_depth": 2,
        "correctness": 2,
        "communication": 2,
        "action": "followup",
        "followup_text": "能具体讲讲你负责的部分吗？",
        "eval_failed": False,
    }
    # 先换评分工厂再建图：图工厂在 setup 模块命名空间里查找 make_evaluator；
    # 注入走真实节点的 spec.scorecard 通道（与生产同一份注入语义，评分桩已退役）
    monkeypatch.setattr(
        setup_mod,
        "make_evaluator",
        lambda llm, spec: make_evaluator(llm, {**spec, "scorecard": followup_scorecard}),
    )
    cfg, saver = env
    coach = CoachGraph(cfg, checkpointer=saver)
    graph = coach.build()
    invoke_cfg = coach.invoke_config("smoke-followup")
    payload = create_initial_state("smoke-followup", "后端开发", "简历", "")

    on_interrupt, state = _answerer(lambda i: f"回答 {i}")
    result = run_new_session(graph, invoke_cfg, payload, on_interrupt)
    assert result.finished is True
    final = result.final_values

    # 自我介绍 + 3 题 ×（1 主问 + 1 追问）= 7 次挂起
    assert state["n"] == 7
    kinds = [e.get("kind") for e in final["transcript"]]
    assert kinds.count("followup") == 3     # 每题追问一轮后到顶（cap=2 含主问轮）
    assert len(final["topic_results"]) == 6  # 每答一条：3 主问 + 3 追问
    assert len({e["topic"] for e in final["topic_results"]}) == 3  # 追问同考点
    assert len(final["issued_keys"]) == 3    # 追问不进登记簿（只按签发计）
