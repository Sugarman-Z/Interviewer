"""HTTP 合同层测试。
锁三件事：错误映射表逐行（领域异常 → HTTP 码 + 文案全等，经 probe 路由
真触发，走的是生产 handler 链，不是直接调函数）；Host/Origin 白名单
（非白名单各一例 403，白名单各形态放行）；错误体恒 {"error": …}（领域/
422/路由 404/未处理 500 全覆盖）。probe 路由是测试专用脚手架，注册在
create_app 产物上，中间件与 handler 栈就是生产栈。
"""
import logging

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402
from web_testkit import loopback_client, offline_web_config  # noqa: E402

import interviewer.web.app as web_app  # noqa: E402
from interviewer.web.gateway import (  # noqa: E402
    EngineError,
    ExpectMismatch,
    HostOriginRejected,
    LimboState,
    PositionRejected,
    ResumeFileInvalid,
    SessionFinished,
    SessionNotFound,
    TextBodyInvalid,
    TurnInFlight,
    UploadTooLarge,
)

# 表逐行（动态文案两行带现场 message；文案与码全等断言）
_TABLE = [
    (PositionRejected(), 400, "岗位还没填——教练需要它来为你出题"),
    (ResumeFileInvalid("不支持的简历格式：仅支持 .pdf/.docx/.txt"), 400,
     "不支持的简历格式：仅支持 .pdf/.docx/.txt"),
    (UploadTooLarge(), 413, "简历文件太大，请压缩后重试"),
    (TextBodyInvalid("回答太长了（上限 20000 字），请精简后重试"), 400,
     "回答太长了（上限 20000 字），请精简后重试"),
    (HostOriginRejected(), 403, "仅限本机访问"),
    (SessionNotFound(), 404, "会话不存在或已失效"),
    (TurnInFlight(), 409, "上一条回答还在处理中，请稍候再试"),
    (ExpectMismatch(), 409, "这道题已经收到了你的回答，请刷新看看当前题目"),
    (SessionFinished(), 409, "本场面试已结束"),
    (LimboState(), 410, "这场面试的记录不完整（引擎中断），无法恢复"),
    (EngineError(), 500, "面试官暂时掉线了，请稍后刷新查看本场状态"),
]
_BY_NAME = {type(e).__name__: e for e, _, _ in _TABLE}


def _probe_raise(exc: str):
    raise _BY_NAME[exc]


def _probe_ping():
    return {"ok": True}


def _probe_echo(x: int):
    return {"x": x}


def _probe_crash():
    raise RuntimeError("secret-internal-detail")


def _probe_engine_boom():
    raise EngineError("secret-engine-detail")


def _probe_session_boom(session_id: str):
    raise EngineError()


@pytest.fixture()
def client(tmp_path):
    app = web_app.create_app(config=offline_web_config(tmp_path))
    app.add_api_route("/api/_probe/raise", _probe_raise, methods=["POST"])
    app.add_api_route("/api/_probe/ping", _probe_ping, methods=["GET", "POST"])
    app.add_api_route("/api/_probe/echo", _probe_echo, methods=["GET"])
    app.add_api_route("/api/_probe/crash", _probe_crash, methods=["GET"])
    app.add_api_route("/api/_probe/engine-boom", _probe_engine_boom, methods=["GET"])
    app.add_api_route("/api/_probe/sessions/{session_id}/boom", _probe_session_boom,
                      methods=["GET"])
    # probe 不碰组合根，无需 lifespan；ServerErrorMiddleware 对未处理异常
    # 发完响应后仍会 re-raise，关掉它让 crash 用例拿到 500 响应体
    return loopback_client(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "exc,code,message", _TABLE, ids=[type(e).__name__ for e, _, _ in _TABLE]
)
def test_domain_error_table_row_by_row(client, exc, code, message):
    resp = client.post(f"/api/_probe/raise?exc={type(exc).__name__}")
    assert resp.status_code == code
    assert resp.json() == {"error": message}          # 码 + 文案全等


def test_validation_error_422_human_body(client):
    resp = client.get("/api/_probe/echo")             # 缺必填参数 x → 422
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"error"}                     # 不透传 pydantic detail 结构
    assert body["error"] == web_app._REQUEST_FORMAT_MESSAGE


def test_unhandled_500_human_body_no_internals(client):
    resp = client.get("/api/_probe/crash")
    assert resp.status_code == 500
    assert resp.json() == {"error": EngineError.default_message}
    assert "secret-internal-detail" not in resp.text  # 内部异常文本永不入响应体


def test_engine_error_detail_never_leaks_to_body(client):
    """「500 响应体永不携带内部异常文本」：EngineError raise 现场的
    诊断细节（题号/考点等）只进日志，体回类级固定人话。"""
    resp = client.get("/api/_probe/engine-boom")
    assert resp.status_code == 500
    assert resp.json() == {"error": EngineError.default_message}
    assert "secret-engine-detail" not in resp.text


def test_engine_error_logs_session_id(client, caplog):
    """ 日志合同：500 族走 logger.exception 且带 session_id。"""
    with caplog.at_level(logging.ERROR, logger="interviewer.web.app"):
        resp = client.get("/api/_probe/sessions/abc12345def/boom")
    assert resp.status_code == 500
    hits = [
        r for r in caplog.records
        if "abc12345def" in r.getMessage() and r.exc_info is not None
    ]
    assert hits, "应有带 session_id 且含 traceback 的 ERROR 日志"


def test_host_whitelist(client):
    # 非白名单 Host → 403（DNS rebinding 闸）
    resp = client.get("/api/_probe/ping", headers={"host": "evil.com"})
    assert resp.status_code == 403
    assert resp.json() == {"error": "仅限本机访问"}
    # TestClient 默认 testserver 同样被拒——白名单不认人工环境
    default_client = TestClient(client.app)
    assert default_client.get("/api/_probe/ping").status_code == 403
    # 回环各形态放行（base_url 自带 127.0.0.1:8000）
    assert client.get("/api/_probe/ping").status_code == 200
    assert client.get("/api/_probe/ping", headers={"host": "localhost:9999"}).status_code == 200
    assert client.get("/api/_probe/ping", headers={"host": "[::1]:8000"}).status_code == 200
    assert client.get("/api/_probe/ping", headers={"host": "localhost"}).status_code == 200


def test_origin_whitelist_on_write(client):
    # 写方法（POST）：无 Origin（curl/同源）与 Next dev 源放行
    assert client.post("/api/_probe/ping").status_code == 200
    assert client.post("/api/_probe/ping",
                       headers={"origin": "http://localhost:3000"}).status_code == 200
    assert client.post("/api/_probe/ping",
                       headers={"origin": "http://127.0.0.1:3000"}).status_code == 200
    # 3001 放行：next dev 在 3000 被占时 EADDRINUSE 自动改绑的落点（T11 评审
    # 确认少了它端口漂移时浏览器写请求全 403、API_BASE 直连逃生同时失效）
    assert client.post("/api/_probe/ping",
                       headers={"origin": "http://localhost:3001"}).status_code == 200
    # 恶意源 / 未知端口 → 403 同文案
    for bad in ("https://evil.com", "http://localhost:8123", "null"):
        resp = client.post("/api/_probe/ping", headers={"origin": bad})
        assert resp.status_code == 403
        assert resp.json() == {"error": "仅限本机访问"}
    # GET 不查 Origin（读操作无 CSRF 面，恶意源 GET 只拿到公开数据）
    assert client.get("/api/_probe/ping",
                      headers={"origin": "https://evil.com"}).status_code == 200
    # OPTIONS（预检）同样过 Origin 闸：恶意源预检 403 统一错误体
    resp = client.options("/api/_probe/ping", headers={"origin": "https://evil.com"})
    assert resp.status_code == 403


def test_cors_preflight_from_dev_origin(client):
    """ 前置：API_BASE 直连形态依赖 CORSMiddleware 放行 Next dev 源。"""
    resp = client.options(
        "/api/_probe/ping",
        headers={
            "origin": "http://localhost:3000",
            "access-control-request-method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_routing_404_body_contract(client):
    """路由层 404 也走 {"error": 中文}（默认 {"detail": "Not Found"} 被改写）。"""
    resp = client.get("/api/nonexistent")
    assert resp.status_code == 404
    assert resp.json() == {"error": "接口不存在"}


def test_routing_405_chinese_message(client):
    """GET 打 POST-only 路由 → 405 中文人话（框架默认文案兜底映射）。"""
    resp = client.get("/api/_probe/raise")
    assert resp.status_code == 405
    assert resp.json() == {"error": "请求方法不支持"}


def test_dynamic_message_exceptions_require_message():
    """透传类异常无默认文案：漏传 message 必须在构造期炸（TypeError），
    不许静默产出 {"error": ""} 的空人话错误体。"""
    with pytest.raises(TypeError):
        ResumeFileInvalid()
    with pytest.raises(TypeError):
        TextBodyInvalid()


def test_duplicate_origin_header_rejected(client):
    """多值头（一恶一善）不做 last-wins 折叠——直接 403（纵深防御，
    RFC 9110 的多 Host 同理在闸内被拒）。"""
    resp = client.post(
        "/api/_probe/ping",
        headers=[
            ("origin", "https://evil.com"),
            ("origin", "http://localhost:3000"),
        ],
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "仅限本机访问"}
