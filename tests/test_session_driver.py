"""session_driver 单测：驱动 seam 的行为合同（对应模块头四条）。
双入口两终止原因（finished=True 图跑完 / False 用户退出，None 不与"炸了"
混用）；续面行为锁（"绝不重传初始 state"——挂起重放的是同一道题、已考
考点前缀原样保留、续答只增）；NoPendingInterrupt fail-loud（空会话与已
收场会话续面即抛）；载荷透传（skip 的 dict 引擎不解释，原样进
Command(resume)）。
"""
import pytest

from interviewer.default_config import get_config
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph
from interviewer.graph.session_driver import (
    NoPendingInterrupt,
    pending_interrupt,
    run_new_session,
    run_resumed_session,
)
from interviewer.graph.session_runner import create_initial_state


@pytest.fixture()
def env(tmp_path):
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 2
    saver, close = build_checkpointer(cfg)
    coach = CoachGraph(cfg, checkpointer=saver)
    yield cfg, coach, coach.build()
    close()


def _initial(sid: str) -> dict:
    return create_initial_state(sid, "后端开发", "简历", "")


def _answer(value):
    return {"type": "answer", "text": "驱动测试的标准回答"}


def test_new_session_runs_to_completion(env):
    """新会话跑完：自我介绍 + 2 题 = 3 次挂起；挂起载荷原样到达回调。"""
    cfg, coach, graph = env
    cfg_i = coach.invoke_config("drv-full")
    seen = []

    def on_interrupt(value):
        seen.append(value)
        return _answer(value)

    result = run_new_session(graph, cfg_i, _initial("drv-full"), on_interrupt)
    assert result.finished is True
    assert len(seen) == 3                       # max_questions=2：自我介绍 + 2 题
    assert all("question" in v for v in seen)   # interrupt() 载荷不丢键
    assert result.final_values["issued_keys"] and result.final_values["interview_report"]


def test_exit_mid_session_returns_unfinished_and_persisted(env):
    """第一问即退出：finished=False、final_values=None；进度已落
    checkpointer（pending_interrupt 能读回挂起——这就是 --resume 能续的地基）。"""
    cfg, coach, graph = env
    cfg_i = coach.invoke_config("drv-exit")
    result = run_new_session(graph, cfg_i, _initial("drv-exit"), lambda v: None)
    assert result.finished is False and result.final_values is None
    assert pending_interrupt(graph, cfg_i) is not None


def test_resume_never_replays_initial_state(env):
    """续面行为锁（卡 C 的核心断言）：第一场在第一题挂起处退出 → 续面
    ① 重放的是同一道挂起题（重传初始 state 会重新 pop 队列 = 换题）；
    ② 已考考点前缀原样保留（重传会把 issued_keys 洗回空）；
    ③ 续答后 issued_keys 只增（2 题上限收场）。"""
    cfg, coach, graph = env
    sid = "drv-resume"
    cfg_i = coach.invoke_config(sid)
    first_questions = []

    def until_first_question(value):
        first_questions.append(value["question"])
        return None if len(first_questions) == 2 else _answer(value)

    run_new_session(graph, cfg_i, _initial(sid), until_first_question)
    mid = graph.get_state(cfg_i).values
    mid_issued = list(mid["issued_keys"])       # 自我介绍答完 → 第一题已签发挂起
    assert len(mid_issued) == 1

    resumed_questions = []
    result = run_resumed_session(
        graph, cfg_i, lambda v: (resumed_questions.append(v["question"]), _answer(v))[1])
    assert result.finished is True
    assert resumed_questions[0] == first_questions[1]      # ① 挂起重放，不换题
    final = result.final_values
    assert final["issued_keys"][:1] == mid_issued          # ② 前缀原样
    assert len(final["issued_keys"]) == 2                  # ③ 只增到上限


def test_resume_without_pending_raises(env):
    """NoPendingInterrupt fail-loud：空会话（一个 checkpoint 都没有）与
    已收场会话（图跑完、无挂起）都续不了——绝不静默开新场。"""
    cfg, coach, graph = env
    empty_cfg = coach.invoke_config("drv-empty")
    assert pending_interrupt(graph, empty_cfg) is None
    with pytest.raises(NoPendingInterrupt):
        run_resumed_session(graph, empty_cfg, _answer)

    done_cfg = coach.invoke_config("drv-done")
    run_new_session(graph, done_cfg, _initial("drv-done"), _answer)
    assert pending_interrupt(graph, done_cfg) is None
    with pytest.raises(NoPendingInterrupt):
        run_resumed_session(graph, done_cfg, _answer)


def test_passthrough_skip_payload(env):
    """载荷透传：{"type": "skip"} 引擎不解释、原样进 Command——图按
    skip 语义继续走完（graph 层不 import agents 的合同在此锁行为面）。"""
    cfg, coach, graph = env
    cfg_i = coach.invoke_config("drv-skip")
    result = run_new_session(graph, cfg_i, _initial("drv-skip"),
                             lambda v: {"type": "skip"})
    assert result.finished is True and result.final_values["interview_report"]
