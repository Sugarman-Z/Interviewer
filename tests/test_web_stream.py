"""SSE 端点合同测试。
锁的合同：两段论（流前失败普通 JSON + 状态码；200 + text/event-stream
后一切失败落 error 事件）；事件序（四形态 opening/主问/追问/skip + 熔断
边界 + 终场 + 无 chunk 降级——期望序读 docs/contract/stream-events.sample.json
单一定义点，改表红 = golden 纪律流式版）；并发/锁（门闩下第二发 409、
GET 409、断连后窗口内第二发仍 409——锁寿命 = 回合寿命）；keepalive
（旋钮注入毫秒级 → 注释行出现）；delta 拼接 == question.text（tap 接线
端到端）。

离线出 delta 的机制：StreamingMockLLM（MockLLM 的流式皮，标记分流语义
逐字一致、_stream 按 2-3 字切片）经 monkeypatch coach_graph.create_llm_client
注入（from-import 名字绑定，patch 本名无效；图在 lifespan 内建，patch 须
在进入 TestClient 上下文前生效）。
"""
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from fixtures.limbo_maker import make_limbo
from interviewer.llm import MockLLM
from interviewer.web import app as web_app
from interviewer.web.gateway import EngineError, ExpectMismatch, StreamDisabled
from stream_fakes import StreamingMockLLM
from web_testkit import loopback_client, offline_web_config

CONTRACT = json.loads(
    (Path(__file__).parents[1] / "docs" / "contract" / "stream-events.sample.json")
    .read_text(encoding="utf-8")
)
PATTERNS = {s["name"]: s["pattern"] for s in CONTRACT["scenarios"]}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """流式假 provider 的离线 app：monkeypatch 图的 llm 工厂（lifespan 前）。"""
    monkeypatch.setattr(
        "interviewer.graph.coach_graph.create_llm_client",
        lambda tier, config: StreamingMockLLM(inner=MockLLM(tier)),
    )
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        yield c, app


def _create(c, position="后端开发") -> str:
    resp = c.post("/api/sessions", json={"position": position})
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _stream(c, sid, body):
    """发起流式应答并读全流：返回 (status, headers, events, raw_text)。"""
    with c.stream("POST", f"/api/sessions/{sid}/answers/stream", json=body) as resp:
        text = ""
        for chunk in resp.iter_text():
            text += chunk
        return resp.status_code, dict(resp.headers), _parse_sse(text), text


def _parse_sse(text: str) -> list:
    """SSE 帧解析：返回事件 dict 列表（注释行丢弃）。event: 行与
    data 载荷的 event 键**必须一致**（tech 把事件名列进 wire 合同——
    实现漂移必须红，不许静默只锁 data）。"""
    events = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame or frame.startswith(":"):
            continue
        data = None
        event_name = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if data is not None:
            assert event_name == data["event"], (
                f"event 行 {event_name!r} 与 data.event {data.get('event')!r} 不一致"
            )
            events.append(data)
    return events


def _to_pattern(events: list) -> list:
    """事件序 → pattern 记号（连续 delta 折叠为 delta*）。"""
    tokens = []
    for e in events:
        if e["event"] == "stage":
            tok = f"stage:{e['phase']}"
        else:
            tok = e["event"]
        if tok == "delta" and tokens and tokens[-1] == "delta*":
            continue
        tokens.append("delta*" if tok == "delta" else tok)
    return tokens


def _delta_text(events: list) -> str:
    return "".join(e.get("text", "") for e in events if e["event"] == "delta")


def _answer_body(text="标准回答", expect=None):
    body = {"type": "answer", "text": text}
    if expect is not None:
        body["expect_qnum"] = expect
    return body


# 流前两段论


def test_pre_stream_errors_are_plain_json(client):
    """流前失败五发：普通 JSON + 状态码 + **非 event-stream**
    content-type（TDD 承诺的负向断言）——同时锁 tech 流前段时序
（preflight 异常在 emit 前发生 → 路由 JSON 腿）。"""
    c, _app = client

    def _plain(r):
        assert "text/event-stream" not in (r.headers.get("content-type") or "")
        return r

    # ① 404：未知会话
    r = _plain(
        c.post(
            "/api/sessions/no-such/answers/stream", json=_answer_body(expect="opening")
        )
    )
    assert r.status_code == 404 and r.json() == {"error": "会话不存在或已失效"}
    # ② 400：空文本
    sid = _create(c)
    r = _plain(
        c.post(f"/api/sessions/{sid}/answers/stream", json={"type": "answer", "text": "  "})
    )
    assert r.status_code == 400 and "error" in r.json()
    # ③ 409：expect 不匹配（开场轮答成 "9"）
    r = _plain(
        c.post(f"/api/sessions/{sid}/answers/stream", json=_answer_body(expect="9"))
    )
    assert r.status_code == 409 and r.json() == {"error": ExpectMismatch.default_message}
    # ④ 410：limbo（手工构造）
    make_limbo(_app, sid)
    r = _plain(
        c.post(f"/api/sessions/{sid}/answers/stream", json=_answer_body())
    )
    assert r.status_code == 410 and "引擎中断" in r.json()["error"]
    # ⑤ 409：finished 会话再答（开场答完一路到收场）
    sid2 = _create(c)
    for expect in ("opening", "1", "2"):
        c.post(f"/api/sessions/{sid2}/answers", json=_answer_body(expect=expect))
    r = _plain(
        c.post(f"/api/sessions/{sid2}/answers/stream", json=_answer_body())
    )
    assert r.status_code == 409 and r.json() == {"error": "本场面试已结束"}


def test_stream_disabled_503(tmp_path, monkeypatch):
    """强降开关：/answers/stream 流前 503 人话，legacy /answers 不受影响
    ——M3「流式不可用注入」的服务端形态。"""
    monkeypatch.setenv("INTERVIEWER_WEB_STREAM_DISABLED", "1")
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        sid = _create(c)
        r = c.post(
            f"/api/sessions/{sid}/answers/stream", json=_answer_body(expect="opening")
        )
        assert r.status_code == 503 and r.json() == {"error": StreamDisabled.default_message}
        # legacy 不受影响：正常答题收下一问
        r2 = c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
        assert r2.status_code == 200 and r2.json()["kind"] == "question"


def test_stream_200_response_headers(client):
    """流式 200 响应头正向锁（tech wire 行；与流前错误的「非
    event-stream」负向断言成对称）。"""
    c, _app = client
    sid = _create(c)
    status, headers, events, _ = _stream(c, sid, _answer_body(expect="opening"))
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache"
    assert headers["x-accel-buffering"] == "no"


# 事件序合同（读表断言）


def test_event_order_opening(client):
    c, _app = client
    sid = _create(c)
    status, _, events, _ = _stream(c, sid, _answer_body(expect="opening"))
    assert status == 200
    assert _to_pattern(events) == PATTERNS["opening"]
    assert events[0] == {"event": "round", "expect": "opening"}
    question = events[-1]
    assert question["kind"] == "question" and question["question"]["qnum"] == "1"
    assert _delta_text(events) == question["question"]["text"]


def test_event_order_main(client):
    c, _app = client
    sid = _create(c)
    c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
    status, _, events, _ = _stream(c, sid, _answer_body(expect="1"))
    assert status == 200
    assert _to_pattern(events) == PATTERNS["main"]
    assert events[0]["event"] == "round" and events[0]["expect"] == "1"
    question = events[-1]
    assert question["question"]["qnum"] == "2"
    assert _delta_text(events) == question["question"]["text"]


def test_event_order_followup(client, monkeypatch):
    c, _app = client
    followup_card = {**MockLLM._SCORECARD,
                     "action": "followup", "followup_text": "追问：落地细节再展开讲讲？"}
    monkeypatch.setattr(MockLLM, "_SCORECARD", followup_card)
    sid = _create(c)
    c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
    status, _, events, _ = _stream(c, sid, _answer_body(expect="1"))
    assert status == 200
    assert _to_pattern(events) == PATTERNS["followup"]
    question = events[-1]
    assert question["question"]["qnum"] == "1.1"
    assert _delta_text(events) == question["question"]["text"] == "追问：落地细节再展开讲讲？"


def test_event_order_skip(client):
    c, _app = client
    sid = _create(c)
    c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
    status, _, events, _ = _stream(c, sid, {"type": "skip", "expect_qnum": "1"})
    assert status == 200
    assert _to_pattern(events) == PATTERNS["skip"]
    question = events[-1]
    assert question["question"]["qnum"] == "2"
    assert _delta_text(events) == question["question"]["text"]


def test_fused_followup_deltas_replaced_by_terminal_event(tmp_path, monkeypatch):
    """熔断边界：max_followups=2（含主问轮）——答主问 count=1<2
    出追问 1.1；答追问 count=2≥2 熔断，评分卡的 followup_text 的 delta 已流
    出，但预算触顶换题——终局 question 是新主问（整题权威替换），text != 被
    弃用的 followup_text。"""
    monkeypatch.setattr(
        "interviewer.graph.coach_graph.create_llm_client",
        lambda tier, config: StreamingMockLLM(inner=MockLLM(tier)),
    )
    cfg = offline_web_config(tmp_path)
    cfg["max_followups"] = 2
    app = web_app.create_app(config=cfg)
    followup_card = {**MockLLM._SCORECARD,
                     "action": "followup", "followup_text": "追问：预算内的第一次追问？"}
    monkeypatch.setattr(MockLLM, "_SCORECARD", followup_card)
    with loopback_client(app) as c:
        sid = _create(c)
        c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
        status, _, events1, _ = _stream(c, sid, _answer_body(expect="1"))
        assert status == 200 and events1[-1]["question"]["qnum"] == "1.1"
        # 答追问 → 评估又产 followup_text（delta 已流出）但预算触顶 → 换主问
        status2, _, events2, _ = _stream(c, sid, _answer_body(expect="1.1"))
        assert status2 == 200
        assert _to_pattern(events2) == PATTERNS["fused_followup"]
        question = events2[-1]
        assert question["question"]["qnum"] == "2"  # 新主问，非追问
        assert question["question"]["text"] != "追问：预算内的第一次追问？"
        # delta 构成精确锁（评审强化：原 != 断言对错误拼接形态也绿）：
        # 被弃用的追问整段 + 新主问整段（方案 A 整块直出，两段各成一字不差的串）
        assert _delta_text(events2) == (
            "追问：预算内的第一次追问？" + question["question"]["text"]
        )


def test_final_round_event_is_flat_final_result(client, monkeypatch):
    """终场轮：final 事件体 == legacy POST /answers 的 FinalResult 扁平形
（对拍，逐字段）。对拍前提：_issue_idx 是图内单例的实例态，同 app 两个
    会话共用出题计数器——把 _ISSUE_CYCLE 钉成单项，两轮签发同考点，跨会话
    脚本位置无关。"""
    monkeypatch.setattr(MockLLM, "_ISSUE_CYCLE", [MockLLM._ISSUE_CYCLE[0]])
    c, _app = client
    sid = _create(c)
    c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
    c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="1"))
    status, _, events, _ = _stream(c, sid, _answer_body(expect="2"))
    assert status == 200
    assert _to_pattern(events) == PATTERNS["final"]
    final_event = events[-1]
    # 对拍：同剧本第二会话走 legacy（event 标记键之外逐字段相等）
    sid2 = _create(c)
    c.post(f"/api/sessions/{sid2}/answers", json=_answer_body(expect="opening"))
    c.post(f"/api/sessions/{sid2}/answers", json=_answer_body(expect="1"))
    legacy = c.post(f"/api/sessions/{sid2}/answers", json=_answer_body(expect="2"))
    assert final_event == {"event": "final", **legacy.json()}
    assert set(final_event) >= {"kind", "overall", "interview_report", "topic_results"}


def test_no_chunks_still_delivers_question(tmp_path):
    """纯 mock（无 BaseChatModel 流式腿）→ 零 chunk 降级：主问轮只有 round +
    同帧直发的 evaluating + question（issuing/delta 都靠 on_token，无 chunk
    即无）——US4-1 的服务端合同。注意必须答主问轮：开场轮的 issuing 是路由
    同帧直发，与 chunk 无关，验不出降级。"""
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        sid = _create(c)
        c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
        status, _, events, _ = _stream(c, sid, _answer_body(expect="1"))
        assert status == 200
        assert _to_pattern(events) == PATTERNS["no_chunks"]
        assert events[-1]["kind"] == "question"
        assert events[-1]["question"]["qnum"] == "2"


# 保活


def _gate_after_round(monkeypatch, gate: threading.Event):
    """门闩点在 round/stage 事件**之后**：包住 routes 侧 run_resumed_session。
    不能用 _LatchedExecutor（它拦整个任务含 round emit——路由 await 首事件
    会死锁：await-first 设计要求 round 先流出）。"""
    import interviewer.web.routes as routes

    real = routes.run_resumed_session

    def gated(graph, cfg, on_interrupt, on_token=None):
        gate.wait()
        return real(graph, cfg, on_interrupt, on_token=on_token)

    monkeypatch.setattr(routes, "run_resumed_session", gated)


def test_ping_keepalive_during_silent_phase(tmp_path, monkeypatch):
    """门闩拖住回合 + keepalive 旋钮注入毫秒级 → 注释行出现（不做 15s
    真实等待；Windows 时钟粒度 ~15.6ms 对毫秒级间隔留余量）。已知失败
    形态：keepalive 实现断裂时本用例挂死而非断言红（iter_text 阻塞读、
    finally 放门闩走不到）——与哨兵加固前的 M6 同类，接受并记录。"""
    monkeypatch.setattr(
        "interviewer.graph.coach_graph.create_llm_client",
        lambda tier, config: StreamingMockLLM(inner=MockLLM(tier)),
    )
    monkeypatch.setenv("INTERVIEWER_WEB_KEEPALIVE_S", "0.1")
    app = web_app.create_app(config=offline_web_config(tmp_path))
    gate = threading.Event()
    _gate_after_round(monkeypatch, gate)
    with loopback_client(app) as c:
        sid = _create(c)
        try:
            text = ""
            with c.stream(
                "POST",
                f"/api/sessions/{sid}/answers/stream",
                json=_answer_body(expect="opening"),
            ) as resp:
                assert resp.status_code == 200
                for chunk in resp.iter_text():
                    text += chunk
                    if ": ping" in text:
                        break
        finally:
            gate.set()  # 放行回合（避免悬挂线程）
        assert ": ping" in text


# 并发与锁


def test_second_post_and_get_409_while_streaming(tmp_path, monkeypatch):
    """锁寿命两腿（M7 判别锚，两条都要绿）：
    腿一（在途窗口）：门闩下流式在途，第二发（流式与 legacy 各一）+ GET
    全部立即 409 JSON（队列外快速路径）；
    腿二（收尾放锁）：放行门闩、回合跑完 → 锁必须释放——**此时流仍开着、
    生成器挂着没人读**。若放错挂在生成器 finally 上，GET 将
    永远 409——TestClient 的断开不会同步关生成器，「断连不放锁」那条腿
    在本 harness 无判别力，可判别的锚在此。"""
    monkeypatch.setattr(
        "interviewer.graph.coach_graph.create_llm_client",
        lambda tier, config: StreamingMockLLM(inner=MockLLM(tier)),
    )
    app = web_app.create_app(config=offline_web_config(tmp_path))
    gate = threading.Event()
    _gate_after_round(monkeypatch, gate)
    with loopback_client(app) as c:
        sid = _create(c)
        try:
            with c.stream(
                "POST",
                f"/api/sessions/{sid}/answers/stream",
                json=_answer_body(expect="opening"),
            ) as resp:
                assert resp.status_code == 200
                # 读到 round 事件即证明回合已被接受、锁已占
                text = ""
                for chunk in resp.iter_text():
                    text += chunk
                    if '"round"' in text:
                        break
                r_stream2 = c.post(
                    f"/api/sessions/{sid}/answers/stream",
                    json=_answer_body(expect="opening"),
                )
                assert r_stream2.status_code == 409 and "error" in r_stream2.json()
                r_legacy = c.post(
                    f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening")
                )
                assert r_legacy.status_code == 409
                r_get = c.get(f"/api/sessions/{sid}")
                assert r_get.status_code == 409
                # 腿二：放行回合（流不关、生成器不读）→ 收尾必须放锁
                gate.set()
                for _ in range(100):
                    r_after = c.get(f"/api/sessions/{sid}")
                    if r_after.status_code == 200:
                        break
                    time.sleep(0.05)
                assert r_after.status_code == 200, "回合收尾必须放锁（与连接无关，M7）"
                detail = r_after.json()
                assert detail["status"] == "awaiting"
                assert detail["pending_question"]["qnum"] == "1"
        finally:
            gate.set()


def test_client_disconnect_round_completes(tmp_path, monkeypatch):
    """断连语义：门闩 + 客户端提前关流 → 窗口内第二发（断开后、
    回合结束前）仍 409——锁未随断连释放；放行后回合照跑落 checkpoint，GET
    恢复完整下一问。harness 局限：TestClient 断开不同步关 ASGI 生成器，本
    用例锁「断连不放锁」语义不判别 M7——可判别的锚在 second_post 用例的
    腿二（真断连的取消语义归 T8 真机验收）。"""
    monkeypatch.setattr(
        "interviewer.graph.coach_graph.create_llm_client",
        lambda tier, config: StreamingMockLLM(inner=MockLLM(tier)),
    )
    app = web_app.create_app(config=offline_web_config(tmp_path))
    gate = threading.Event()
    _gate_after_round(monkeypatch, gate)
    with loopback_client(app) as c:
        sid = _create(c)
        try:
            with c.stream(
                "POST",
                f"/api/sessions/{sid}/answers/stream",
                json=_answer_body(expect="opening"),
            ) as resp:
                assert resp.status_code == 200
                text = ""
                for chunk in resp.iter_text():
                    text += chunk
                    if '"round"' in text:
                        break
            # 流已关（断连），回合仍被 gate 拖住：窗口内第二发必须 409
            r = c.post(f"/api/sessions/{sid}/answers", json=_answer_body(expect="opening"))
            assert r.status_code == 409, "断连不得放锁（锁寿命 = 回合寿命，M7）"
        finally:
            gate.set()
        # 回合放行后照跑落库：GET 恢复完整下一问（重试轮询至 200/409 消失）
        for _ in range(100):
            r = c.get(f"/api/sessions/{sid}")
            if r.status_code == 200:
                break
            time.sleep(0.05)
        assert r.status_code == 200
        detail = r.json()
        assert detail["status"] == "awaiting"
        assert detail["pending_question"]["qnum"] == "1"


async def _drive_disconnect(app, sid: str, body: dict) -> dict:
    """raw-ASGI 驱动流式应答并在首帧后断开（必须由调用方的活动事件循环
    await——引擎线程的 emit 要往这个循环投递）。

    spec_version 声明 2.3：Starlette 对 ≥2.4 不监听 http.disconnect（只靠
    send 的 OSError），<2.4 才走「监听断开 → 取消响应任务」——取消打进
    停靠在生成器 __anext__ 的消费任务，生成器帧随之解栈（其 finally 执行，
    生产 uvicorn 断连的同款取消路径）。TestClient 的 close() 是排干不是
    取消，测不出这条腿。首帧后留 50ms 让消费侧停靠进 queue.get 再断开。
    """
    payload = json.dumps(body).encode("utf-8")
    path = f"/api/sessions/{sid}/answers/stream"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
            (b"host", b"127.0.0.1:80"),  # 回环守卫白名单；写方法不带 Origin 合法
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    result: dict = {}

    async def run() -> None:
        body_sent = False
        got_first = asyncio.Event()
        do_disconnect = asyncio.Event()
        chunks: list[bytes] = []

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await do_disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
            elif message.get("body"):
                chunks.append(message["body"])
                got_first.set()

        async def watch():
            await got_first.wait()
            await asyncio.sleep(0.05)  # 让消费侧停靠进 __anext__ 再断开
            do_disconnect.set()

        await asyncio.gather(app(scope, receive, send), watch())
        result["body"] = b"".join(chunks)

    await run()
    return result


def test_raw_asgi_disconnect_keeps_lock_until_round_done(tmp_path, monkeypatch):
    """真断连取消语义（M7 判别锚，TestClient 无判别力的补位）：断连后、
    回合收尾前，session 必须仍在 inflight（锁不随断连释放）；放行门闩后
    由任务 done 回调释放。断言用 inflight 成员判定（纯内存快速路径同款
    observable，不排引擎队列——单线程执行器下任何第二引擎操作都会挂死
    在门闩后面，变异红会退化成死锁）。"""
    app = web_app.create_app(config=offline_web_config(tmp_path))
    gate = threading.Event()
    _gate_after_round(monkeypatch, gate)
    with loopback_client(app) as c:
        sid = _create(c)

        async def scenario():
            result = await _drive_disconnect(app, sid, _answer_body(expect="opening"))
            assert result["status"] == 200
            assert '"round"' in result["body"].decode("utf-8", "replace")
            # 断连已发生（响应任务被取消收尾），回合仍被门闩拖住：锁必须仍占
            assert sid in app.state.inflight, "真断连不得放锁（锁寿命 = 回合寿命，M7）"
            gate.set()
            # 循环保持存活（吸收引擎线程的 emit），轮询 done 回调放锁
            for _ in range(200):
                if sid not in app.state.inflight:
                    # 收尾余量：done 回调的哨兵投递落地，避免良性竞态的
                    # RuntimeError 日志噪声（评审 nit 采纳）
                    await asyncio.sleep(0.05)
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("回合收尾后锁未释放（done 回调失效）")

        try:
            asyncio.run(scenario())
        finally:
            gate.set()  # 失败路径也放行，避免引擎线程悬挂拖死 lifespan 收尾
            for _ in range(200):
                if sid not in app.state.inflight:
                    break
                time.sleep(0.02)
        # 回合照跑落 checkpoint：GET 恢复完整下一问
        r = c.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["pending_question"]["qnum"] == "1"


# 流中失败


def test_engine_error_mid_round_emits_error_event(client, monkeypatch):
    """回合接受后引擎异常 → error 事件（fail-visible）+ 连接收尾，文案 ==
    同源（500 家族用 default_message，不带内部文本）。"""
    c, _app = client
    sid = _create(c)

    def _boom(graph, cfg, on_interrupt, on_token=None):
        raise RuntimeError("引擎内部炸了")

    monkeypatch.setattr("interviewer.web.routes.run_resumed_session", _boom)
    status, _, events, _ = _stream(c, sid, _answer_body(expect="opening"))
    assert status == 200  # 已进流
    assert _to_pattern(events) == ["round", "stage:issuing", "error"]
    assert events[-1] == {"event": "error", "error": EngineError.default_message}


def test_engine_domain_error_mid_round_carries_domain_message(client, monkeypatch):
    """流中域异常 <500 家族：error 事件带 exc.message 原文——与上一用例
（≥500 → default_message）合成对三元分支的完整覆盖（评审补位：原
    零覆盖，翻转实现不红）。"""
    c, _app = client
    sid = _create(c)

    def _raise(graph, cfg, on_interrupt, on_token=None):
        raise ExpectMismatch("回合已被推进（模拟）")

    monkeypatch.setattr("interviewer.web.routes.run_resumed_session", _raise)
    status, _, events, _ = _stream(c, sid, _answer_body(expect="opening"))
    assert status == 200
    assert _to_pattern(events) == ["round", "stage:issuing", "error"]
    assert events[-1] == {"event": "error", "error": "回合已被推进（模拟）"}
