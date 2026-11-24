"""CLI 续面数据层单测（--list/--continue/--resume 的地基）。
session_rows 走 saver 公开 list 接口枚举当前图签名的会话：进行中/已结束
判定、新→旧排序（--continue 取第一条 ongoing）、旧签名失联会话不列、
空库空清单。会话起停全部经 graph/session_driver（驱动行为在
test_session_driver 锁，这里只锁枚举）。
"""
import pytest

from interviewer.cli.app import _parse_args, session_rows
from interviewer.default_config import get_config
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph, graph_signature
from interviewer.graph.session_driver import run_new_session
from interviewer.graph.session_runner import create_initial_state


@pytest.fixture()
def env(tmp_path):
    """memory backend：枚举走 saver.list，不再需要 sqlite 真库
（卡 C 的顺带收益——InMemorySaver 也是一等公民）。"""
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 2
    saver, close = build_checkpointer(cfg)
    coach = CoachGraph(cfg, checkpointer=saver)
    yield cfg, saver, coach, coach.build()
    close()


def _start_ongoing(coach, graph, sid):
    """开场到第一次挂起（自我介绍提问）即退出——一场"进行中"会话的最小形态。"""
    run_new_session(
        graph, coach.invoke_config(sid),
        create_initial_state(sid, "后端开发", "简历", ""),
        lambda v: None,                       # 第一问即退出：挂起留在 checkpointer
    )


def _run_finished(coach, graph, sid):
    """答完全场——"已结束"会话（走 driver，不再手抄循环）。"""
    result = run_new_session(
        graph, coach.invoke_config(sid),
        create_initial_state(sid, "后端开发", "简历", ""),
        lambda v: {"type": "answer", "text": "标准回答"},
    )
    assert result.finished is True


def test_rows_mark_ongoing_and_finished(env):
    cfg, saver, coach, graph = env
    _start_ongoing(coach, graph, "s-ongoing")
    _run_finished(coach, graph, "s-finished")
    rows = {r["session_id"]: r for r in
            session_rows(saver, graph, graph_signature(cfg))}
    assert rows["s-ongoing"]["ongoing"] is True
    assert rows["s-ongoing"]["position"] == "后端开发"
    assert rows["s-ongoing"]["asked"] == 0
    assert rows["s-finished"]["ongoing"] is False
    assert rows["s-finished"]["asked"] == 2               # max_questions=2 全问完


def test_rows_order_latest_first_and_continue_picks_it(env):
    """新→旧排序（每 thread 最大 checkpoint_id 降序）——--continue 取第一条
    ongoing 就是"最近一场未完成"。"""
    cfg, saver, coach, graph = env
    _start_ongoing(coach, graph, "s-old")
    _start_ongoing(coach, graph, "s-new")      # 更晚开始 → checkpoint 更新
    rows = session_rows(saver, graph, graph_signature(cfg))
    assert rows[0]["session_id"] == "s-new" and rows[0]["ongoing"] is True


def test_old_signature_threads_hidden(tmp_path):
    """图形状变了的失联会话不列：thread_id 带签名，当前图续不了，列了误导。
    跨图版本共库只有持久库才会发生——本用例专用 sqlite（memory saver 各自
    独立存储，复现不了这个场景）。"""
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_path"] = str(tmp_path / "cp.sqlite")
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 2
    saver, close = build_checkpointer(cfg)
    try:
        coach = CoachGraph(cfg, checkpointer=saver)
        graph = coach.build()
        _start_ongoing(coach, graph, "s-cur")
        old_cfg = {**cfg, "max_questions": 9}              # 上限变 → 图签名变
        old_saver, old_close = build_checkpointer(old_cfg)
        try:
            old_coach = CoachGraph(old_cfg, checkpointer=old_saver)
            _start_ongoing(old_coach, old_coach.build(), "s-ghost")
        finally:
            old_close()
        rows = session_rows(saver, graph, graph_signature(cfg))
        assert [r["session_id"] for r in rows] == ["s-cur"]
    finally:
        close()


def test_empty_saver_returns_empty(env):
    """一次都没跑过：空枚举合法（旧版对应的"库文件不存在"场景——现在 saver
    在手就无所谓存在性，天然空清单）。"""
    cfg, saver, coach, graph = env
    assert session_rows(saver, graph, graph_signature(cfg)) == []


def test_parse_args_flags():
    a = _parse_args(["--resume", "abc123"])
    assert a.resume == "abc123" and a.continue_latest is False and a.list is False
    assert _parse_args(["--continue"]).continue_latest is True
    assert _parse_args(["--list"]).list is True
