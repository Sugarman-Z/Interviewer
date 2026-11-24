"""integration e2e：真 zhipu 一场全链路。
默认 skip——`pytest tests -q` 不真联网不花钱（全仓离线纪律；e2e 标
integration、手动跑、不进默认回归）。两种跑法（完整命令见 README 联调节）：

1) 进程内（tmp 库 + 控成本）：
     INTERVIEWER_E2E_ZHIPU=1 pytest tests/test_e2e_integration.py -q
   仓库 .env 的 ZHIPU_API_KEY / INTERVIEWER_LLM_BASE_URL（coding 专用端点）
   经 monkeypatch 注入进程环境、跑完还原；max_questions 压到 2 控成本。
2) 经 Next rewrite 全链路（「真收场轮经 rewrite」必查项）：
     先起真智谱后端 + `cd webapp; npm run dev`，再
     INTERVIEWER_E2E_ZHIPU=1 INTERVIEWER_E2E_BASE_URL=http://[::1]:3000 \
       pytest tests/test_e2e_integration.py -q
   打的是在跑服务（dev 库、.env 全量生效）。走 [::1] 是本机已知环境事实：
   VS Code 端口转发占用 127.0.0.1:3000 而 next dev 绑 [::]:3000（README）。
   **rewrite 模式的 provider 由在跑后端决定**——须按真智谱起服务（删 mock 行
   + NO_PROXY）：mock 后端每轮亚秒、照样收场三件套齐全，会假绿架空「真收场
   轮经 rewrite」必查项（本分支无法校验远端 provider，README 已注明前提）。

断言口径：真 LLM 非确定性——只锁合同形状（端点契约 + 聚合）与
时延上限，不锁内容文本。时延：进程内 TestClient 对 ASGI transport 不执行
httpx 默认 5s 超时（8s 慢端点实证）；经 rewrite 模式的 proxyTimeout 190s
由代理自身执行——超限会 500，断言 200 即覆盖。
"""
import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过（进程内模式需要）
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from web_testkit import loopback_client  # noqa: E402

import interviewer.web.app as web_app  # noqa: E402
from interviewer.default_config import get_config  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 控成本：主问压到 2（.env 的 3 留给手动浏览器真跑）；开场与追问由引擎预算自管
_E2E_MAX_QUESTIONS = 2

#: 三段互不重复的可用答案（真评分会按质量打分——复读会被如实打低分，故给真内容）
_ANSWERS = [
    "我是五年经验的 Python 后端，主要做交易系统的行情网关和风控服务，技术栈 FastAPI 加 Kafka，最近在做一个低延迟撮合模拟器。",
    "核心思路是先量化再归因：用 profile 和火焰图定位热点，区分 CPU 密集与 IO 等待——CPU 密集考虑进程池或换算法，IO 密集确认并发度是否打满，再查有没有同步调用把事件循环卡死。",
    "按延迟敏感度分层：行情推送这类大量网络等待的用 asyncio 协程，解析计算重的放线程池或独立进程，进程间用队列解耦，同时监控事件循环阻塞时长防劣化。",
]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INTERVIEWER_E2E_ZHIPU") != "1",
        reason="真 zhipu e2e 默认跳过：设 INTERVIEWER_E2E_ZHIPU=1 才跑（真联网、花额度）",
    ),
]


def _env_from_dotenv(monkeypatch) -> None:
    """仓库 .env → 进程环境（monkeypatch，跑完还原）。

    app.py 组合根的合同是「注入 config 即离线、不 load_dotenv」；e2e 刻意
    要真实环境（key 与 coding 端点都只在 .env），故自行注入而非走默认路径。
    """
    values = dotenv_values(_REPO_ROOT / ".env")
    if not values.get("ZHIPU_API_KEY"):
        pytest.fail("INTERVIEWER_E2E_ZHIPU=1 但仓库 .env 里没有 ZHIPU_API_KEY")
    for name, value in values.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    # 环境里若有 provider 覆盖残留会静默把 e2e 偷换成 mock——显式清掉，
    # 由下方 assert cfg["llm_provider"] == "zhipu" 兜底
    monkeypatch.delenv("INTERVIEWER_LLM_PROVIDER", raising=False)
    # Windows 系统代理掐智谱 TLS 的既知坑（PROJECT_MEMORY 2026-11-20）：
    # 目标域名进 NO_PROXY 直连
    no_proxy = os.environ.get("NO_PROXY", "")
    if "open.bigmodel.cn" not in no_proxy:
        monkeypatch.setenv("NO_PROXY", f"{no_proxy},open.bigmodel.cn".lstrip(","))


def test_real_zhipu_full_session(tmp_path, monkeypatch):
    base_url = os.environ.get("INTERVIEWER_E2E_BASE_URL")
    if base_url:
    # trust_env=False：本机系统代理连 [::1] 回环也会截——
        # httpx 的 no-proxy 豁免不认 IPv6 字面量，默认 trust_env 把对 Next 的
        # 请求送进 127.0.0.1:29290（通不通全看代理心情）。e2e 打回环 Next，
        # 直连才是正确语义
        client_cm = httpx.Client(base_url=base_url, timeout=300.0, trust_env=False)
        mode = f"经 rewrite {base_url}"
    else:
        _env_from_dotenv(monkeypatch)
        cfg = get_config()
        assert cfg["llm_provider"] == "zhipu", "e2e 必须真 provider（env 有残留覆盖？）"
        cfg["checkpoint_backend"] = "sqlite"
        cfg["checkpoint_path"] = str(tmp_path / "checkpoints.sqlite")
        cfg["data_dir"] = str(tmp_path / "data")
        cfg["max_questions"] = _E2E_MAX_QUESTIONS
        app = web_app.create_app(config=cfg)
        client_cm = loopback_client(app)
        mode = "进程内"

    timings: list[float] = []
    with client_cm as client:
        resp = client.post("/api/sessions", json={"position": "Python 后端开发"})
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["question"]["variant"] == "opening"  # OpeningIntro 零 LLM
        assert created["question"]["text"].strip()
        sid = created["session_id"]

        expect = "opening"  # opening 轮 qnum 字面量（；开场 pending 不带 qnum）
        # 答题轮上限按引擎预算推导而非硬编码赌 .env：
        # 开场 1 + 每题主问轮数 ≤ max_followups(3，含主问) + 富余 2；
        # create 响应现带 max_questions——.env 改 4 题时上限自动跟上
        round_cap = 1 + created["max_questions"] * 3 + 2
        final = None
        for i in range(round_cap):
            started = time.monotonic()
            resp = client.post(
                f"/api/sessions/{sid}/answers",
                json={
                    "type": "answer",
                    "text": _ANSWERS[i % len(_ANSWERS)],
                    "expect_qnum": expect,
                },
            )
            elapsed = time.monotonic() - started
            assert resp.status_code == 200, resp.text
            # 时延哨兵（非合同）：300s 是单次 LLM 调用的 llm_timeout，一个回合
            # 可串多次调用（Evaluator→IssueQuestion；收场轮再接 ReportWriter）——
            # 600s 是「绝不该到」的护栏线（真收场轮实测 40-120s）
            assert elapsed < 600, f"单轮 {elapsed:.0f}s——连多次 300s 级调用串行都兜不住，真异常"
            timings.append(elapsed)
            body = resp.json()
            if body["kind"] == "final":
                # FinalResult 扁平形（schemas.py：FinalResult(FinalPayload) 直继承）
                final = body
                break
            question = body["question"]
            assert question["text"].strip(), "空题面"
            assert question["qnum"], "缺 qnum"
            expect = question["qnum"]
        assert final is not None, f"{round_cap} 轮未收场（引擎预算失灵？）"

        #/ 收场合同：三件套齐全、值域合法。先查「全 eval_failed」再查
        # overall 区间——全部空分时 overall==0.0，先炸区间会遮住诊断文案
        topics = final["topic_results"]
        assert topics and all(t["qnum"] for t in topics)
        assert any(t.get("scores") for t in topics), "全部 eval_failed？真 API 不该如此"
        assert 1.0 <= final["overall"] <= 5.0
        # interview_report 容忍空串：gateway 对「模型成功但空内容」原样透传
        #（合同内合法值）——锁类型不锁内容
        assert isinstance(final["interview_report"], str)

        # 详情收口：finished + 同一 final + progress
        resp = client.get(f"/api/sessions/{sid}")
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["status"] == "finished"
        assert detail["final"]["overall"] == final["overall"]
        # 覆盖足够可提前收场（done 哨兵），answered==total 不是恒等式——只锁下界
        assert detail["progress"]["answered"] >= 1

    print(f"\n[e2e {mode}] {len(timings)} 轮，单轮秒数 {[round(t, 1) for t in timings]}")


def _stream_round(client, sid: str, expect: str, text: str):
    """一轮流式应答：POST /answers/stream 读全流。

    返回 (events, first_s, total_s)。帧解析与 pytest 侧 _parse_sse 同规则
（event 行 == data.event 锚；`: ping` 注释行不匹配前缀自然跳过）。
    httpx stream 上下文入口 ≈ 首事件时刻——T3 路由 withhold 响应头直到
    引擎首事件（await-first 两段论），status_code 200 即证明首事件已到。
    """
    events: list[dict] = []
    first_s: float | None = None
    t0 = time.monotonic()
    with client.stream(
        "POST",
        f"/api/sessions/{sid}/answers/stream",
        json={"type": "answer", "text": text, "expect_qnum": expect},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        cur_event = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                cur_event = line[len("event: "):]
            elif line.startswith("data: "):
                if first_s is None:
                    first_s = time.monotonic() - t0
                ev = json.loads(line[len("data: "):])
                assert ev["event"] == cur_event, f"event 行 {cur_event!r} != data.event"
                events.append(ev)
    return events, first_s, time.monotonic() - t0


def test_real_zhipu_streaming_session(tmp_path, monkeypatch):
    """T8 流式 e2e（tasks/todo.md T8；TDD）：SSE 一场全链路。

    断言四指标（真 LLM 非确定性——锁合同形状与时延上限，不锁内容）：
    - 首事件 ≤2s（「round 同帧」：await-first 下响应头
      即首事件）；
    - 每个 LLM 出题轮 delta ≥1（方案 A：真机 args 原子落地整块，事件序
      定格后恒有载荷）；
    - delta 拼接 ⊆ 终局题面（诚实性：弃用路径 ⊂，常规轮 ==）；
    - 终局事件 == 详情 GET（每轮 pending_question 对拍；final 轮对拍
      status/overall）。
    """
    base_url = os.environ.get("INTERVIEWER_E2E_BASE_URL")
    if base_url:
        client_cm = httpx.Client(base_url=base_url, timeout=300.0, trust_env=False)
        mode = f"经 rewrite {base_url}"
    else:
        _env_from_dotenv(monkeypatch)
        cfg = get_config()
        assert cfg["llm_provider"] == "zhipu", "e2e 必须真 provider（env 有残留覆盖？）"
        cfg["checkpoint_backend"] = "sqlite"
        cfg["checkpoint_path"] = str(tmp_path / "checkpoints.sqlite")
        cfg["data_dir"] = str(tmp_path / "data")
        cfg["max_questions"] = _E2E_MAX_QUESTIONS
        app = web_app.create_app(config=cfg)
        client_cm = loopback_client(app)
        mode = "进程内"

    metrics: list[tuple[float, float, int]] = []  # (first_s, total_s, delta 片数)
    with client_cm as client:
        resp = client.post("/api/sessions", json={"position": "Python 后端开发"})
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session_id"]
        expect = "opening"
        round_cap = 1 + resp.json()["max_questions"] * 3 + 2
        final = None
        for i in range(round_cap):
            events, first_s, total_s = _stream_round(
                client, sid, expect, _ANSWERS[i % len(_ANSWERS)]
            )
            assert first_s is not None
            assert first_s <= 2.0, f"首事件 {first_s:.2f}s > 2s（SPEC §4 同帧预算）"
            assert events and events[0]["event"] == "round"
            terminal = events[-1]
            assert terminal["event"] in ("question", "final"), terminal
            assert any(e["event"] == "stage" for e in events), "每轮 ≥1 次 stage（§2.2）"
            delta_count = sum(1 for e in events if e["event"] == "delta")
            metrics.append((first_s, total_s, delta_count))
            if terminal["event"] == "final":
                # 终场轮 delta 非目标（报告正文不流式）——不做 delta 断言
                final = terminal
                break
            q = terminal["question"]
            assert q["text"].strip() and q["qnum"], "空题面/缺 qnum"
            deltas = "".join(e["text"] for e in events if e["event"] == "delta")
            assert deltas, f"LLM 出题轮零 delta（tap 失效？expect={expect}）"
            assert deltas in q["text"], "delta 拼接 ⊄ 题面（§4.3 诚实性）"
            # 终局 == 详情 GET：pending_question 对拍（整题权威）
            resp = client.get(f"/api/sessions/{sid}")
            assert resp.status_code == 200, resp.text
            assert resp.json()["pending_question"] == q
            expect = q["qnum"]
        assert final is not None, f"{round_cap} 轮未收场"

        resp = client.get(f"/api/sessions/{sid}")
        detail = resp.json()
        assert detail["status"] == "finished"
        assert detail["final"]["overall"] == final["overall"]
        assert detail["progress"]["answered"] >= 1

    print(
        f"\n[e2e 流式 {mode}] {len(metrics)} 轮；"
        f"首事件秒数 {[round(m[0], 2) for m in metrics]}；"
        f"单轮总秒数 {[round(m[1], 1) for m in metrics]}；"
        f"delta 片数 {[m[2] for m in metrics]}"
    )
