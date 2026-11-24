"""web 应用组合根（进程面）。与 cli/app.py 平级的第二个 transport：组合根搬进
FastAPI lifespan——启动建（dotenv → get_config → build_checkpointer → CoachGraph
→ build），关闭按写死顺序：engine_executor.shutdown(wait=True) 先 join 在途
回合，最后才 close_checkpointer()（倒序会在已关连接上继续写、炸 sqlite3）。
create_app(config=) 是测试离线 seam，注入即不 load_dotenv。
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from interviewer.default_config import get_config
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph, graph_signature
from interviewer.web.gateway import DomainError, EngineError, HostOriginRejected
from interviewer.web.routes import router
from interviewer.web.settings import WebSettings

logger = logging.getLogger(__name__)

#: 优雅关闭预算：330s = spec「收场轮最长约 5 分钟」口径 + 30s 余量，
#: 对理论最坏值（llm_timeout=300 × max_retries=3 × 收场轮多次调用，可上
#: 15 分钟）是**明确接受截断**，不是「给足」。到期行为（uvicorn 0.54 实
#: 测）两分法：在途 HTTP 任务被 cancel——只丢 HTTP 响应；已运行的引擎回
#: 合取消不掉，在 lifespan join 中跑到 END/挂起落 checkpoint，**收割落盘
#: 在引擎任务内照常发生**（T5 起 persist_session 于引擎线程内）。排队未
#: 跑的回合被取消丢弃（状态未推进，重连 GET 见旧挂起重答即可恢复）。
#: taskkill 硬杀才会连收割一起丢（会话本身仍在 checkpointer）。README
#: 运维警告沿用本口径。
GRACEFUL_SHUTDOWN_SECONDS = 330


def _load_dotenv() -> None:
    """组合根第一步：照抄 cli/app.py:187-192 的 dotenv 块。

    独立函数是为了可测——tests spy 本名，锁定「dotenv 先于 get_config」
    的顺序承诺（docstring 说了什么，测试就锁什么）。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _build_engine_executor() -> ThreadPoolExecutor:
    """单引擎执行线程：全部图操作经此串行（SqliteSaver 单写者约束的落地）。
    跨会话第二个请求排队。工人线程一律非 daemon（标准库行为），关闭靠
    shutdown(wait=True) 显式 join。测试经 monkeypatch 本函数换可观测执行器。
    """
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine")


class InFlightTable:
    """进程内 in-flight 锁表：每会话至多一个进行中回合。
    占锁与查询纯内存、不碰引擎线程（GET 快速路径 409 依赖此点）；锁随进程重启即清。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._active: set[str] = set()

    def try_acquire(self, session_id: str) -> bool:
        with self._guard:
            if session_id in self._active:
                return False
            self._active.add(session_id)
            return True

    def release(self, session_id: str) -> None:
        with self._guard:
            self._active.discard(session_id)

    def __contains__(self, session_id: str) -> bool:
        with self._guard:
            return session_id in self._active

    def snapshot(self) -> tuple[str, ...]:
        with self._guard:
            return tuple(self._active)


#: 422 的人话包络：不透传 pydantic 的 detail 结构，错误体恒 {"error": …}
_REQUEST_FORMAT_MESSAGE = "请求格式有误，请检查提交的内容"

#: 框架默认 HTTPException 文案的中文兜底：按 (status, detail) 精确匹配，
#: 只换框架自带固定串，自定义 detail 透传
_FRAMEWORK_DETAIL_ZH = {
    (404, "Not Found"): "接口不存在",
    (405, "Method Not Allowed"): "请求方法不支持",
    (400, "There was an error parsing the body"): _REQUEST_FORMAT_MESSAGE,
}

#: Host 白名单：挡 DNS rebinding 外域读取。只认主机名、端口不限——
#: Host 闸的敌人是 rebinding 域名，不是端口
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}

#: 写端点 Origin 白名单：Next dev 源 + 无 Origin 的同源/curl 请求。multipart
#: 属 CORS 简单请求（无预检），浏览器里任意网页可直 POST 本服务——这道闸
#: 是"用进程内真 key 建会话烧钱"的唯一防线。3001 = next dev 在 3000 被占时
#: 自动改绑的最常见落点，少了它端口漂移时经 rewrite 的浏览器 POST 全 403
_ALLOWED_WRITE_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
}


def _hostname_of(host_header: str) -> str:
    """Host 头 → 主机名（小写）：兼容 host:port / [::1]:port / 裸主机名。"""
    host = host_header.strip().lower()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


class _LoopbackGuardMiddleware:
    """Host/Origin 白名单，纯 ASGI：非回环 Host → 403；写方法（非 GET/HEAD）
    带非白名单 Origin → 403。GET/HEAD 不查 Origin（读操作无 CSRF 面）；OPTIONS
    一并校验，让恶意源预检也拿到统一 {"error": …} 403。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts: list[str] = []
        origins: list[str] = []
        for key, value in scope.get("headers") or ():
            name = key.decode("latin-1").lower()
            if name == "host":
                hosts.append(value.decode("latin-1"))
            elif name == "origin":
                origins.append(value.decode("latin-1"))
        # RFC 9110 §7.2：Host 必须恰一个；多 Origin 同样视为畸形——
        # last-wins 折叠会让「第二个头恰好是回环名」绕过白名单语义
        #（浏览器发不出双头，这是纵深防御）
        if len(hosts) != 1 or _hostname_of(hosts[0]) not in _ALLOWED_HOSTNAMES:
            await _send_forbidden(send)
            return
        if scope["method"] not in {"GET", "HEAD"}:
            if len(origins) > 1 or (origins and origins[0] not in _ALLOWED_WRITE_ORIGINS):
                await _send_forbidden(send)
                return
        await self.app(scope, receive, send)


async def _send_forbidden(send) -> None:
    body = json.dumps(
        {"error": HostOriginRejected.default_message}, ensure_ascii=False
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": HostOriginRejected.http_status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _install_error_handlers(app: FastAPI) -> None:
    """错误映射的 HTTP 侧：领域异常 → 码 + 文案；422/500 人话包络。
    handler 按 MRO 就近匹配，DomainError 一个入口接住全部子类；500 族走
    logger.exception 带 session_id，响应体永不携带内部异常文本。
    """

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError):
        if exc.http_status >= 500:
            #：500 响应体永不携带内部异常文本——EngineError raise 现场
            # 携带的诊断细节（题号/考点/计数）只进日志，体回类级固定人话
            logger.exception(
                "引擎异常 session=%s path=%s detail=%s",
                request.path_params.get("session_id", "-"),
                request.url.path,
                exc.message,
            )
            return JSONResponse(
                status_code=exc.http_status,
                content={"error": type(exc).default_message},
            )
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422, content={"error": _REQUEST_FORMAT_MESSAGE}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        # 路由层 404/405 等：默认体是 {"detail": …}，统一改写为 {"error": …}；
        # 框架自带默认文案是英文（Not Found 等）——按 (码, detail) 精确匹配换
        # 中文人话，自定义 HTTPException 的 detail 原样透传（中文合同）
        message = _FRAMEWORK_DETAIL_ZH.get(
            (exc.status_code, str(exc.detail)), str(exc.detail)
        )
        return JSONResponse(status_code=exc.status_code, content={"error": message})

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception):
        logger.exception(
            "未处理异常 session=%s path=%s",
            request.path_params.get("session_id", "-"),
            request.url.path,
        )
        return JSONResponse(
            status_code=500, content={"error": EngineError.default_message}
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 日志合同：stderr + 时间戳 + level；uvicorn access log 保持默认开启。
    # 不加 force=True：root 已被宿主（嵌入方/测试框架）配置时本调用 no-op——
    # 有意尊重宿主配置；独立进程形态下 uvicorn 的 dictConfig 不碰 root，
    # 首次进入即生效
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    app.state.engine_executor = _build_engine_executor()
    app.state.inflight = InFlightTable()
    app.state.ready = False
    app.state.startup_error = None
    # 重入清理：同一 app 二次进入 lifespan 且构建降级时不留上一场的陈旧
    # 组合根（旧 saver 已在上一场关闭，是死对象；端点只准经 ready 标志取用）
    app.state.signature = None
    app.state.saver = None
    app.state.coach = None
    app.state.graph = None
    close_checkpointer = None
    try:
        # 外层 finally 从组合根第一步就兜住关闭顺序：pre-yield 段即使被
        # BaseException 打断（理论窗口）也不跳过 _shutdown（合同无空洞）
        try:
            if app.state.config is None:
                _load_dotenv()  # 第一步；注入 config 时跳过（测试隔离：不把仓库 .env 泄进测试进程）
                app.state.config = get_config()
            config = app.state.config
            app.state.signature = graph_signature(config)
            saver, close_checkpointer = build_checkpointer(config)
            app.state.saver = saver
            coach = CoachGraph(config, checkpointer=saver)
            app.state.coach = coach
            app.state.graph = coach.build()
            app.state.ready = True
            logger.info(
                "组合根就绪：provider=%s checkpoint_backend=%s signature=%s",
                config.get("llm_provider"),
                config.get("checkpoint_backend"),
                app.state.signature,
            )
        except Exception as exc:  # 降级活着比启动即死可观测（/api/health 503 人话）
            app.state.startup_error = exc
            logger.exception("组合根构建失败——服务以降级态运行（/api/health 将返回 503）")
        yield
    finally:
        _shutdown(app, close_checkpointer)


def _shutdown(app: FastAPI, close_checkpointer) -> None:
    """关闭顺序合同（，禁止倒序）：executor join 先于 close_checkpointer。

    join 等待期对每个在途回合打日志（「正在等待回合结束 session=…」），
    让挂住的控制台有解释。
    """
    executor = getattr(app.state, "engine_executor", None)
    if executor is not None:
        inflight = getattr(app.state, "inflight", None)
        if inflight is not None:
            for sid in inflight.snapshot():
                logger.info("正在等待回合结束 session=%s", sid)
        logger.info("服务关闭：等待引擎线程收尾（在途回合跑完为止）")
        executor.shutdown(wait=True)
        logger.info("引擎线程已收尾")
    if close_checkpointer is not None:
        logger.info("关闭 checkpointer")
        close_checkpointer()
        logger.info("checkpointer 已关闭，服务退出")


def create_app(config: dict | None = None) -> FastAPI:
    """config=None → lifespan 内 get_config()（含 dotenv）；测试注入离线 config。"""
    app = FastAPI(title="陪面 api-service", lifespan=lifespan)
    app.state.config = config
    app.state.web_settings = WebSettings.from_env()
    # 中间件：后加者在外层。回环闸最外（恶意请求最先被 403），CORS 在内
    #（放行 Next dev 源的直连形态 的 API_BASE 逃生通道前置）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(_ALLOWED_WRITE_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(_LoopbackGuardMiddleware)
    _install_error_handlers(app)
    app.include_router(router)
    return app
