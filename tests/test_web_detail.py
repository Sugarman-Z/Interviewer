"""详情端点测试——恢复唯一数据源。
锁：三态视图（awaiting：status/pending_question/progress/末条重复下发；
finished：final 三件套 + issue_done 剥除；limbo：410）；未知 404；门闩下
在途回合 GET 立即 409 不排队 → 回合结束后 GET 恢复下一问（断连恢复
GET 两跳闭环）。
"""
import asyncio
import threading
import time

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from fixtures.limbo_maker import make_limbo  # noqa: E402
from web_testkit import loopback_client, offline_web_config  # noqa: E402

import interviewer.web.app as web_app  # noqa: E402
from interviewer.llm import MockLLM  # noqa: E402


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


def _get(c, sid):
    return c.get(f"/api/sessions/{sid}")


# awaiting 三阶段（opening 挂起 / q1 挂起 / 追问挂起）


def test_get_awaiting_opening_and_mid(client):
    c, _app = client
    sid = _create(c)

    # 阶段一：opening 挂起（刚创建）
    resp = _get(c, sid)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "session_id", "position", "status", "max_questions",
        "transcript", "pending_question", "progress",
    }
    assert (body["status"], body["position"], body["max_questions"]) == (
        "awaiting", "后端开发", 2,
    )
    assert body["progress"] == {"answered": 0, "total": 2}
    # 代表条目：开场条目 + 末条重复下发 pending_question
    assert len(body["transcript"]) == 1
    assert body["transcript"][0]["variant"] == "opening"
    assert body["pending_question"] == {
        "variant": "opening", "text": body["transcript"][0]["text"],
    }
    assert all("at" not in m for m in body["transcript"])  #：at 不下发

    # 阶段二：答完开场，q1 挂起
    _answer(c, sid, "自我介绍", expect="opening")
    body = _get(c, sid).json()
    assert body["progress"] == {"answered": 0, "total": 2}  # q1 待答：1-1
    assert body["pending_question"]["qnum"] == "1"
    assert body["pending_question"]["variant"] == "question"
    # 代表条目逐条：intro / 开场回答无题号 / question 带考点题号
    t = body["transcript"]
    assert len(t) == 3
    assert (t[0]["role"], t[0]["variant"]) == ("interviewer", "opening")
    assert t[1] == {"role": "candidate", "variant": "answer", "text": "自我介绍"}
    assert (t[2]["variant"], t[2]["qnum"], t[2]["topic"]) == (
        "question", "1", "项目整体架构",
    )
    # 末条重复下发（定案）：末条 == pending_question 同文同题号
    assert t[2]["text"] == body["pending_question"]["text"]
    assert t[2]["qnum"] == body["pending_question"]["qnum"]


def test_get_followup_pending(client, monkeypatch):
    c, _app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")
    followup_card = {
        **dict(MockLLM._SCORECARD),
        "action": "followup",
        "followup_text": "追问：再具体说说落地细节？",
    }
    monkeypatch.setattr(MockLLM, "_SCORECARD", followup_card)
    _answer(c, sid, "答主问", expect="1")
    body = _get(c, sid).json()
    assert body["pending_question"]["variant"] == "followup"
    assert body["pending_question"]["qnum"] == "1.1"
    # 追问挂起：主问已答（progress 不减），末条 == 追问
    assert body["progress"] == {"answered": 1, "total": 2}
    assert body["transcript"][-1]["variant"] == "followup"
    assert body["transcript"][-1]["text"] == body["pending_question"]["text"]


# finished / limbo / 404


def test_get_finished(client):
    c, _app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")
    _answer(c, sid, expect="1")
    assert _answer(c, sid, expect="2").json()["kind"] == "final"

    resp = _get(c, sid)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "session_id", "position", "status", "max_questions",
        "transcript", "progress", "final",
    }  # 无 pending_question 键
    assert body["status"] == "finished"
    assert body["progress"] == {"answered": 2, "total": 2}
    final = body["final"]
    assert set(final) == {"overall", "interview_report", "topic_results"}  # 值
    assert final["overall"] == 3.0
    assert [t["qnum"] for t in final["topic_results"]] == ["1", "2"]
    assert final["interview_report"]
    # transcript：issue_done 剥除（引擎 7 条 → wire 6 条），末条是 q2 的回答
    assert len(body["transcript"]) == 6
    assert body["transcript"][-1]["qnum"] == "2"
    assert body["transcript"][-1]["variant"] == "answer"


def test_get_limbo_410(client):
    c, app = client
    sid = _create(c)
    make_limbo(app, sid)
    resp = _get(c, sid)
    assert resp.status_code == 410
    assert resp.json() == {"error": "这场面试的记录不完整（引擎中断），无法恢复"}


def test_get_unknown_404(client):
    c, _app = client
    resp = _get(c, "nosuchsession")
    assert resp.status_code == 404
    assert resp.json() == {"error": "会话不存在或已失效"}


# 门闩：in-flight 快速路径（断连恢复 GET 两跳闭环）


class _LatchedExecutor:
    def __init__(self, inner, gate):
        self._inner = inner
        self._gate = gate

    def submit(self, fn, /, *args, **kwargs):
        def latched(*a, **kw):
            self._gate.wait()
            return fn(*a, **kw)

        return self._inner.submit(latched, *args, **kwargs)

    def shutdown(self, wait=True, **kwargs):
        return self._inner.shutdown(wait=wait, **kwargs)


def test_get_inflight_fast_path_then_recovery(client):
    """ 快速路径 + T5 挂账的断连恢复 GET 两跳：在途回合 GET 队列外
    立即 409（同 answers 文案）→ 回合结束后首次 GET 恢复下一问。"""
    c, app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")  # → q1 挂起

    gate = threading.Event()
    app.state.engine_executor = _LatchedExecutor(app.state.engine_executor, gate)
    holder = {}

    def poster():
        try:
            holder["resp"] = c.post(
                f"/api/sessions/{sid}/answers", json={"type": "answer", "text": "答q1"}
            )
        except BaseException as exc:
            holder["err"] = exc

    t = threading.Thread(target=poster)
    t.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and sid not in app.state.inflight:
            time.sleep(0.01)
        assert sid in app.state.inflight, "回合应已占锁（门闩拖住中）"
        # 快速路径：队列外立即 409，同 answers 文案
        started = time.monotonic()
        resp = _get(c, sid)
        assert resp.status_code == 409
        assert resp.json() == {"error": "上一条回答还在处理中，请稍候再试"}
        assert time.monotonic() - started < 2  # 不排队、不被长回合拖住
    finally:
        gate.set()
        t.join(timeout=15)
    assert not t.is_alive()
    assert "err" not in holder, f"poster 线程异常：{holder.get('err')!r}"
    assert holder["resp"].status_code == 200

    # 回合结束后首次 GET 即恢复：下一问（q2）原样重建
    resp = _get(c, sid)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "awaiting"
    assert body["pending_question"]["qnum"] == "2"
    assert body["progress"] == {"answered": 1, "total": 2}
    assert body["transcript"][-1]["text"] == body["pending_question"]["text"]


async def _seed_limbo_via_lifespan(app, sid: str) -> None:
    """scripts/run_web_limbo.py 的种库形态：手动进一次 lifespan 建组合根、
    make_limbo 种 thread、退出（引擎 join + checkpointer 关闭）。"""
    async with app.router.lifespan_context(app):
        make_limbo(app, sid)


def test_limbo_survives_lifespan_reentry_on_sqlite(tmp_path):
    """T11 limbo 人工场景的机制锚：种库 lifespan 进出后，第二次进入
（uvicorn 正式起服务的形态）能从**文件 sqlite** 读回 limbo thread——
    详情 410、列表 interrupted。也顺带锁 app.py 的 lifespan 重入清理分支
（此前全仓无测试覆盖同一 app 二次进入）。"""
    cfg = offline_web_config(tmp_path)
    cfg["checkpoint_backend"] = "sqlite"
    cfg["checkpoint_path"] = str(tmp_path / "checkpoints.sqlite")
    sid = "limboreentry1"
    app = web_app.create_app(config=cfg)
    asyncio.run(_seed_limbo_via_lifespan(app, sid))

    with loopback_client(app) as c:  # TestClient 再次进 lifespan = 起服务形态
        resp = _get(c, sid)
        assert resp.status_code == 410
        assert resp.json() == {"error": "这场面试的记录不完整（引擎中断），无法恢复"}
        rows = c.get("/api/sessions").json()
        assert [r["session_id"] for r in rows] == [sid]
        assert rows[0]["status"] == "interrupted"
