"""列表端点测试。
混合库自造五类 thread：旧签名（失联不列）/ opening 挂起 / ongoing（q1
挂起）/ finished / limbo——锁行字段、三态、排序（最后活动新→旧）；
opening 挂起行省略 pending_qnum 键；stale 翻转走 _utcnow 时钟 seam
（改 checkpoint ts 会连带剥中断变 limbo，时间旅行只能走时钟）。
"""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from fixtures.limbo_maker import make_limbo  # noqa: E402
from web_testkit import loopback_client, offline_web_config  # noqa: E402

import interviewer.web.app as web_app  # noqa: E402
import interviewer.web.routes as web_routes  # noqa: E402
from interviewer.graph.coach_graph import CoachGraph  # noqa: E402
from interviewer.graph.session_driver import run_new_session  # noqa: E402
from interviewer.graph.session_runner import create_initial_state  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        yield c, app


def _create(c, position="后端开发") -> str:
    resp = c.post("/api/sessions", json={"position": position})
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _answer(c, sid, text="标准回答", expect=None):
    body = {"type": "answer", "text": text}
    if expect is not None:
        body["expect_qnum"] = expect
    return c.post(f"/api/sessions/{sid}/answers", json=body)


def _make_old_signature_thread(app, sid: str) -> None:
    """旧图签名失联 thread：同一 saver 上用改图形状的 config 再建一张图
    跑一场（复用 test_cli_resume 的手法；图操作走引擎线程）。"""
    old_cfg = {**app.state.config, "max_questions": 9}   # 上限变 → 签名变

    def _sync():
        old_coach = CoachGraph(old_cfg, checkpointer=app.state.saver)
        run_new_session(
            old_coach.build(),
            old_coach.invoke_config(sid),
            create_initial_state(sid, "旧岗位", "", ""),
            lambda v: None,
        )

    app.state.engine_executor.submit(_sync).result(timeout=30)


def test_list_mixed_library(client):
    c, app = client
    # 造库顺序决定最后活动序：open → ongoing → finished → limbo（最新）
    s_open = _create(c, "算法工程师")
    s_ongoing = _create(c)
    _answer(c, s_ongoing, "自我介绍", expect="opening")   # → q1 挂起
    s_finished = _create(c, "数据工程师")
    _answer(c, s_finished, "自我介绍", expect="opening")
    _answer(c, s_finished, expect="1")
    assert _answer(c, s_finished, expect="2").json()["kind"] == "final"
    s_limbo = _create(c)
    make_limbo(app, s_limbo)
    _make_old_signature_thread(app, "s-old-ghost")

    resp = c.get("/api/sessions")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["session_id"] for r in rows] == [s_limbo, s_finished, s_ongoing, s_open]

    by_sid = {r["session_id"]: r for r in rows}
    assert "s-old-ghost" not in by_sid          # 旧签名失联不列

    # limbo → interrupted
    r = by_sid[s_limbo]
    assert r["status"] == "interrupted"
    assert set(r) == {"session_id", "position", "started_at", "status", "progress"}
    assert r["position"] == "后端开发"

    # finished：overall + progress 满
    r = by_sid[s_finished]
    assert r["status"] == "finished"
    assert r["overall"] == 3.0
    assert r["progress"] == {"answered": 2, "total": 2}
    assert "pending_qnum" not in r

    # ongoing（q1 挂起）：pending_qnum 有值、progress 0/2
    r = by_sid[s_ongoing]
    assert r["status"] == "ongoing"
    assert r["pending_qnum"] == "1"
    assert r["progress"] == {"answered": 0, "total": 2}
    assert "overall" not in r

    # opening 挂起：pending_qnum 键省略（与 IssuedQuestion.qnum 同口径）
    r = by_sid[s_open]
    assert r["status"] == "ongoing"
    assert set(r) == {"session_id", "position", "started_at", "status", "progress"}
    assert r["position"] == "算法工程师"

    # started_at：UTC ISO 可解析、不晚于当前时刻
    for r in rows:
        started = datetime.fromisoformat(r["started_at"])
        assert started.tzinfo is not None
        assert started <= datetime.now(timezone.utc)

    # 分离点（T6-T8 合并评审确认项②补锁）：恢复最旧会话使其「创建最早、
    # 活动最新」——按最后活动（checkpoint_id）排序 s_open 应跳到首位；若
    # 回归成按创建时间排序则 s_open 仍在末位，下断言红（造库阶段两种键
    # 同向，区分不了口径）
    _answer(c, s_open, "自我介绍", expect="opening")
    rows2 = c.get("/api/sessions").json()
    assert [r["session_id"] for r in rows2] == [s_open, s_limbo, s_finished, s_ongoing]


def test_list_empty(client):
    c, _app = client
    resp = c.get("/api/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_stale_flips_to_interrupted(client, monkeypatch):
    """：挂起超 stale 天数无活动 → interrupted（时间线划界是展示
    分类，不是引擎状态——finished 不受时钟影响）。"""
    c, _app = client
    s_fresh = _create(c)
    _answer(c, s_fresh, "自我介绍", expect="opening")
    s_finished = _create(c)
    _answer(c, s_finished, "自我介绍", expect="opening")
    _answer(c, s_finished, expect="1")
    assert _answer(c, s_finished, expect="2").json()["kind"] == "final"

    real_now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        web_routes, "_utcnow", lambda: real_now + timedelta(days=8)
    )
    rows = {r["session_id"]: r for r in c.get("/api/sessions").json()}
    assert rows[s_fresh]["status"] == "interrupted"   # 8 天无活动
    assert "pending_qnum" not in rows[s_fresh]        # stale 后不再给题号
    assert rows[s_finished]["status"] == "finished"   # 时钟不影响终态
    assert rows[s_finished]["overall"] == 3.0

    # 恰满 7 整天（T6-T8 合并评审确认项③补锁）：边界取 ≥——checkpoint ts
    # 严格早于 real_now，(now - last_ts).days == 7 == stale_days；比较符
    # 回归成 >（或任何 off-by-one）则此断言红
    monkeypatch.setattr(
        web_routes, "_utcnow", lambda: real_now + timedelta(days=7)
    )
    rows = {r["session_id"]: r for r in c.get("/api/sessions").json()}
    assert rows[s_fresh]["status"] == "interrupted"


def test_list_and_detail_agree(client):
    """列表 ↔ 详情一致性冒烟：同一会话在两端的判据同源（词表不同属契约：
    详情 awaiting/finished，列表 ongoing/finished/interrupted—— vs）。"""
    c, _app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")
    rows = {r["session_id"]: r for r in c.get("/api/sessions").json()}
    detail = c.get(f"/api/sessions/{sid}").json()
    assert detail["status"] == "awaiting"
    assert rows[sid]["status"] == "ongoing"      # awaiting ↔ ongoing 映射
    assert rows[sid]["pending_qnum"] == detail["pending_question"]["qnum"]
    assert rows[sid]["progress"] == detail["progress"]
