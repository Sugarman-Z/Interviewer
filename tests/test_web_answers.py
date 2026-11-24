"""回答端点测试——api-service 的核心任务。
锁：全流程一场（含追问链——MockLLM 评分卡注入 action=followup、skip、
收场 + 收割产物落盘）；三种 409（in-flight/expect/finished）+ 404 + 410
全分支（含前置闸优先级区分用例——limbo 会话带 expect 仍 410）；门闩机制
下的并发语义（第二发立即 409 不排队、第一发照跑完、锁随回合释放）；
TURN_DELAY_MS 调试闸行为链。断连恢复的 GET 侧（409 快速路径）在
test_web_detail 锁——本文件锁锁表机制与回合结束后的引擎挂起态。

测试注入口纪律：MockLLM._SCORECARD 的类属性 monkeypatch 是追问链的
注入口（invoke 期生效、对已建图可用）——pytest 串行下安全；引入任何
并行测试框架前须先改造此通道。
"""
import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from fixtures.limbo_maker import make_limbo as _make_limbo  # noqa: E402
from web_testkit import loopback_client, offline_web_config  # noqa: E402

import interviewer.web.app as web_app  # noqa: E402
from interviewer.graph.session_driver import pending_interrupt  # noqa: E402
from interviewer.llm import MockLLM  # noqa: E402

_FORMAT_ERROR = '请求格式不正确：需要 {type:"answer",text} 或 {type:"skip"}'


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


class _LatchedExecutor:
    """门闩执行器（plan 并发时序对策）：包住真引擎线程的提交入口，
    任务先 gate.wait() 再跑——回合被确定性拖住、测试放行。不 sleep 不碰
    运气；shutdown 代理给 lifespan 关闭路径。"""

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


# limbo 手工构造：tests/fixtures/limbo_maker.py（T6 收拢的共享手法，
# 私有契约脆断点标注见该模块 docstring）


# 全流程


def test_full_flow_to_final(client):
    c, app = client
    sid = _create(c)
    r1 = _answer(c, sid, "我是后端工程师", expect="opening")
    assert r1.status_code == 200
    q1 = r1.json()
    assert q1["kind"] == "question"
    assert q1["question"]["variant"] == "question"
    assert q1["question"]["qnum"] == "1"
    assert q1["question"]["topic"]

    r2 = _answer(c, sid, expect="1")
    assert r2.json()["question"]["qnum"] == "2"

    r3 = _answer(c, sid, expect="2")
    final = r3.json()
    assert final["kind"] == "final"
    assert set(final) == {"kind", "overall", "interview_report", "topic_results"}
    assert final["overall"] == 3.0  # 两条 mock 3/3/3
    assert [t["qnum"] for t in final["topic_results"]] == ["1", "2"]

    # 收割落盘：产物给 CLI/挖掘飞轮——内容哨兵锁端点接线
    #（防重构传剥空 dict 静默缺字段）
    sessions_dir = Path(app.state.config["data_dir"]) / "sessions"
    harvested = json.loads((sessions_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert harvested["session_id"] == sid
    assert harvested["position"] == "后端开发"
    assert len(harvested["topic_results"]) == 2
    assert (sessions_dir / f"{sid}_report.md").exists()
    # mock 的 ReportWriter 恒非空——final 的 interview_report 透传锁
    #（report.md 正是 persist_session 用同值写出的）
    report_text = (sessions_dir / f"{sid}_report.md").read_text(encoding="utf-8")
    assert final["interview_report"]
    assert final["interview_report"] == report_text


def test_followup_chain(client, monkeypatch):
    """追问链：MockLLM 评分卡注入 action=followup（engine 侧零改动的
    测试注入口）→ 答主问得 followup 1.1 → 恢复 advance 答追问换 q2。"""
    original = dict(MockLLM._SCORECARD)
    followup_card = {
        **original,
        "action": "followup",
        "followup_text": "追问：再具体说说落地细节？",
    }
    c, _app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")

    monkeypatch.setattr(MockLLM, "_SCORECARD", followup_card)
    r = _answer(c, sid, "答主问", expect="1")
    body = r.json()
    assert body["kind"] == "question"
    assert body["question"]["variant"] == "followup"
    assert body["question"]["qnum"] == "1.1"
    assert body["question"]["topic"] == "项目整体架构"

    monkeypatch.setattr(MockLLM, "_SCORECARD", original)
    r2 = _answer(c, sid, "答追问", expect="1.1")
    assert r2.json()["question"]["qnum"] == "2"
    final = _answer(c, sid, expect="2").json()
    assert final["kind"] == "final"
    results = final["topic_results"]
    assert [t["qnum"] for t in results] == ["1", "1.1", "2"]
    assert results[1]["is_followup"] is True


def test_skip_flow(client):
    c, _app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")
    r = c.post(
        f"/api/sessions/{sid}/answers", json={"type": "skip", "expect_qnum": "1"}
    )
    assert r.status_code == 200
    assert r.json()["question"]["qnum"] == "2"
    final = _answer(c, sid, expect="2").json()
    assert final["kind"] == "final"
    assert final["topic_results"][0]["skipped"] is True


# 400 shape 闸（不占锁）


def test_answer_shape_branches(client):
    c, _app = client
    sid = _create(c)
    for bad in (
        {"type": "exit"},
        {"type": "answer", "text": "   "},
        {"type": "answer"},
        {"type": "answer", "text": 123},
    ):
        resp = c.post(f"/api/sessions/{sid}/answers", json=bad)
        assert resp.status_code == 400
        assert resp.json() == {"error": _FORMAT_ERROR}
    raw = c.post(
        f"/api/sessions/{sid}/answers",
        content='"只是一个字符串"',
        headers={"content-type": "application/json"},
    )
    assert raw.status_code == 400
    assert raw.json() == {"error": _FORMAT_ERROR}
    # 超长闸：人话指明上限
    resp = c.post(
        f"/api/sessions/{sid}/answers", json={"type": "answer", "text": "字" * 20001}
    )
    assert resp.status_code == 400
    assert "20000" in resp.json()["error"]
    # shape 400 在占锁之前——后续合法提交照常成功
    ok = _answer(c, sid, "自我介绍")
    assert ok.status_code == 200


# 404 / 三种 409 / 410


def test_unknown_session_404(client):
    c, app = client
    resp = c.post(
        "/api/sessions/nosuchsession/answers", json={"type": "answer", "text": "x"}
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "会话不存在或已失效"}
    # 404 路径同样占锁（实现口径）、finally 必释放——本断言验证
    # 的是「响应后表已空」，不是「从未占锁」（排队期间第二发本会得 409）
    assert "nosuchsession" not in app.state.inflight


def test_expect_qnum_branches(client):
    c, _app = client
    sid = _create(c)
    # 槽位是 opening，带 expect "1" → 409（幂等根防护）
    resp = _answer(c, sid, expect="1")
    assert resp.status_code == 409
    assert resp.json() == {"error": "这道题已经收到了你的回答，请刷新看看当前题目"}
    # 正确槽位放行
    assert _answer(c, sid, "自我介绍", expect="opening").status_code == 200
    # 响应丢失后的盲目重试（同 expect 重发）：槽位已到 "1" → 挡死
    resp = _answer(c, sid, "自我介绍重发", expect="opening")
    assert resp.status_code == 409
    # skip 同样过 expect 闸（错误槽位 → 409，不因 skip 放行）
    resp = c.post(
        f"/api/sessions/{sid}/answers", json={"type": "skip", "expect_qnum": "opening"}
    )
    assert resp.status_code == 409


def test_finished_session_409(client):
    c, _app = client
    sid = _create(c)
    _answer(c, sid, "自我介绍", expect="opening")
    _answer(c, sid, expect="1")
    assert _answer(c, sid, expect="2").json()["kind"] == "final"
    for payload in ({"type": "answer", "text": "再答"}, {"type": "skip"}):
        resp = c.post(f"/api/sessions/{sid}/answers", json=payload)
        assert resp.status_code == 409
        assert resp.json() == {"error": "本场面试已结束"}


def test_limbo_session_410(client):
    c, app = client
    sid = _create(c)
    _make_limbo(app, sid)
    resp = c.post(
        f"/api/sessions/{sid}/answers", json={"type": "answer", "text": "x"}
    )
    assert resp.status_code == 410
    assert resp.json() == {"error": "这场面试的记录不完整（引擎中断），无法恢复"}
    # 前置闸优先级区分锁：带 expect_qnum 的请求打到 limbo 也
    # 必须 410——expect 校验若被重排到 classify 之前，会误答 409「刷新
    # 可续」、对不可恢复会话给出错误预期，此测即红
    resp = c.post(
        f"/api/sessions/{sid}/answers",
        json={"type": "answer", "text": "x", "expect_qnum": "1"},
    )
    assert resp.status_code == 410


def test_expect_qnum_type_gate(client):
    """带了但类型/形状不对（数字笔误/空串）→ 400：幂等防护不许被类型
    笔误无声关闭（静默当「不带」会让盲重试失去唯一挡板）。"""
    c, _app = client
    sid = _create(c)
    for bad_expect in (1, "", True):
        resp = c.post(
            f"/api/sessions/{sid}/answers",
            json={"type": "answer", "text": "x", "expect_qnum": bad_expect},
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": _FORMAT_ERROR}
    # 不带键 → 不校验、照常放行
    assert _answer(c, sid, "自我介绍").status_code == 200


def test_turn_delay_gate_slows_turn(client, monkeypatch, tmp_path):
    """TURN_DELAY_MS 行为链（plan 调试闸）：sleep 在引擎回合开头
（持锁窗口内）——联调人工场景「拖长回合看退避」依赖这一位置。"""
    monkeypatch.setenv("INTERVIEWER_WEB_TURN_DELAY_MS", "300")
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        sid = _create(c)
        started = time.monotonic()
        resp = _answer(c, sid, "自我介绍")
        elapsed = time.monotonic() - started
    assert resp.status_code == 200
    assert elapsed >= 0.3  # 单位是毫秒（ms 当 s 会 >300s 超时；不 sleep 会 <0.3s）


# 门闩：并发与断连恢复的确定性基础


def test_inflight_latch_second_post_409_and_recovery(client):
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
        except BaseException as exc:  # 线程异常回传主线程——否则以裸 KeyError 红且掩盖根因
            holder["err"] = exc

    t = threading.Thread(target=poster)
    t.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and sid not in app.state.inflight:
            time.sleep(0.01)
        assert sid in app.state.inflight, "回合应已占锁（门闩拖住中）"
        # 门闩下第二发：队列外立即 409
        started = time.monotonic()
        second = c.post(
            f"/api/sessions/{sid}/answers", json={"type": "answer", "text": "并发第二发"}
        )
        assert second.status_code == 409
        assert second.json() == {"error": "上一条回答还在处理中，请稍候再试"}
        assert time.monotonic() - started < 2  # 快速失败，未被拖到回合结束
    finally:
        gate.set()
        t.join(timeout=15)
    assert not t.is_alive()
    assert "err" not in holder, f"poster 线程异常：{holder.get('err')!r}"

    # 断连语义：并发/取消不影响第一发——回合跑完、响应 200
    first = holder["resp"]
    assert first.status_code == 200
    assert first.json()["question"]["qnum"] == "2"
    assert sid not in app.state.inflight  # 回合结束锁释放
    # 回合结束后引擎已挂起下一问——T6 的 GET 将从这一数据源恢复
    cfg = app.state.coach.invoke_config(sid)

    def _pending():
        return pending_interrupt(app.state.graph, cfg)

    pending = app.state.engine_executor.submit(_pending).result(timeout=30)
    assert pending is not None and "选型" in pending.get("question", "")
