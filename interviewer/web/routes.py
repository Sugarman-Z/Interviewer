"""业务端点：创建/回答/详情/列表四组 API。
图操作一律走单引擎线程 run_in_executor；口径计算全在 gateway，本层只做翻译和错误映射。
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile
from starlette.requests import ClientDisconnect

from interviewer.agents.utils.agent_states import RESUME_ANSWER, RESUME_SKIP
from interviewer.graph.coach_graph import graph_signature
from interviewer.graph.session_driver import (
    pending_interrupt,
    run_new_session,
    run_resumed_session,
)
from interviewer.graph.session_runner import create_initial_state, persist_session
from interviewer.resume.file_loader import (
    DEFAULT_MAX_CHARS,
    SUPPORTED_EXTS,
    ResumeFileError,
    load_resume_file,
)
from interviewer.resume.intake_guard import check_position
from interviewer.resume.jd_parser import parse_position_input
from interviewer.resume.parser import candidate_profile_text, parse_pasted_resume
from interviewer.web.gateway import (
    DomainError,
    EngineError,
    ExpectMismatch,
    LimboState,
    PositionRejected,
    QuestionTextTap,
    ResumeFileInvalid,
    SessionFinished,
    SessionNotFound,
    StreamDisabled,
    TextBodyInvalid,
    TurnInFlight,
    UploadTooLarge,
    build_final,
    classify_session,
    current_slot,
    expect_qnum,
    overall_score,
    pending_question,
    progress,
    session_view,
)
from interviewer.web.schemas import (
    AnswerResult,
    FinalResult,
    QuestionResult,
    SessionCreated,
    SessionState,
    SessionSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_DEGRADED_MESSAGE = "服务启动失败：面试记录存储或引擎无法初始化（详见服务日志）"

#: 上传后缀白名单：从 file_loader 的 SUPPORTED_EXTS 派生，注册表加格式这里自动跟随
_UPLOAD_EXTS = set(SUPPORTED_EXTS.split("/"))
_UPLOAD_CHUNK = 1024 * 1024


@router.get("/health")
def health(request: Request):
    """判活：200 = 进程活 + 组合根已建；503 = 降级态人话。
    组合根构建失败不炸启动——进程活着、health 说 503 更可观测；
    traceback 只进服务日志。
    """
    if getattr(request.app.state, "ready", False):
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"error": _DEGRADED_MESSAGE})


@router.post("/__test/slow")
async def test_slow(request: Request):
    """长 POST 代理验证端点：睡 test_sleep_ms 后回包（默认 0 = 立即返回）。
    唯一用途是经 Next rewrite 打 >60s 的人为长响应，验证 dev 代理不掐
    2 分钟级 POST。asyncio.sleep 只挂协程、不碰引擎线程；本地回环单用户，
    不做鉴权，不属业务端点。
    """
    settings = request.app.state.web_settings
    await asyncio.sleep(settings.test_sleep_ms / 1000)
    return {"status": "ok", "slept_ms": settings.test_sleep_ms}


# POST /sessions —— 创建会话


@router.post(
    "/sessions",
    status_code=201,
    response_model=SessionCreated,
    response_model_exclude_none=True,  # 可选键省略下发（gateway 不给键、Mock 不下发 null）
)
async def create_session(request: Request) -> SessionCreated:
    state = request.app.state
    if not getattr(state, "ready", False):
        raise EngineError()  # 降级态：组合根未建，人话走 表（detail 只进日志）
    settings = state.web_settings
    _precheck_content_length(request, settings)  # 超大请求体先拒，不进解析
    position, jd, resume_text, upload = await _parse_create_body(request)
    if len(resume_text) > settings.max_resume_text_chars:
        raise TextBodyInvalid(
            f"简历文本太长（上限 {settings.max_resume_text_chars} 字，"
            f"当前 {len(resume_text)} 字）——请精简或改用文件上传"
        )
    tmp_path: str | None = None
    try:
        if upload is not None:
            tmp_path = await _spool_upload(upload, settings)
        sid, state_values, pending = await asyncio.get_running_loop().run_in_executor(
            state.engine_executor,
            functools.partial(
                _create_session_sync,
                config=state.config,
                coach=state.coach,
                graph=state.graph,
                position=position,
                jd=jd,
                resume_path=tmp_path,
                resume_text=resume_text,
            ),
        )
        max_questions = state.config["max_questions"]
        return SessionCreated(
            session_id=sid,
            max_questions=max_questions,
            question=pending_question(state_values, pending, max_questions),
        )
    finally:
        if tmp_path is not None:
            try:
                #：成功失败一律清理（幂等——引擎线程读完已自清；此处
                # 兜 grace 取消窗口：协程 finally 可能与引擎线程读文件并发，
                # Windows 下对打开中的文件 unlink 抛 PermissionError）
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("临时文件清理失败（句柄被引擎线程持有？）path=%s", tmp_path)


def _precheck_content_length(request: Request, settings) -> None:
    raw = request.headers.get("content-length")
    if raw is None:  # chunked 编码无 CL——由拷贝侧流式计数兜底
        return
    try:
        length = int(raw)
    except ValueError:
        return
    # 预检量的是整包 CL（含 boundary/部件头开销），流式计数量的才是文件
    # 净字节——留 64KB 余量防边界体积的合规文件误伤，精确性交给计数闸
    if length > settings.max_upload_mb * 1024 * 1024 + 64 * 1024:
        raise UploadTooLarge()


async def _parse_create_body(request: Request):
    """双轨请求体：JSON {position, jd?, resume_text?} 或 multipart
（position / jd / resume_text / resume_file）。宽松构造对齐 Mock 路由：
    值缺失按空串处理，业务闸（岗位空等）在后续层按 出 400。"""
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in ctype:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError, ClientDisconnect):
            body = {}  # 坏 JSON/读断 → 按空输入处理 → 走岗位空 400（Mock 同款语义；断连不出 500 噪声）
        if not isinstance(body, dict):
            body = {}

        def _json_field(name: str) -> str:
            value = body.get(name)
            # 只判 None（对齐 Mock 的 ?? 语义）：0/false 照 String() 强转，
            # 交给 check_position 按内容裁决
            return str(value).strip() if value is not None else ""

        return _json_field("position"), _json_field("jd"), _json_field("resume_text"), None
    try:
        form = await request.form()
    except Exception as exc:
        logger.warning("multipart 解析失败：%r", exc)
        raise ResumeFileInvalid("请求体不是合法的表单") from exc

    def _form_field(name: str) -> str:
        value = form.get(name)
        # 文件部件按空处理（对齐 Mock 的 typeof 守卫——str(UploadFile) 是
        # repr 垃圾串，会被灰区放行建出垃圾会话）
        return value.strip() if isinstance(value, str) else ""

    upload = form.get("resume_file")
    return (
        _form_field("position"),
        _form_field("jd"),
        _form_field("resume_text"),
        upload if isinstance(upload, UploadFile) else None,
    )


async def _spool_upload(upload: UploadFile, settings) -> str:
    """上传落临时文件：系统临时目录（禁 cwd/data）、不采用客户端文件名
（只取 suffix 且须白名单命中，挡路径注入）、流式拷贝计数 413。
    清理双保险：调用方 finally 幂等兜底 + 引擎线程读完自清，失败路径也自清。
    """
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in _UPLOAD_EXTS:
        raise ResumeFileInvalid(
            f"不支持的格式 {ext or '(无扩展名)'}，支持 {SUPPORTED_EXTS}；.doc 请另存为 .docx"
        )
    max_bytes = settings.max_upload_mb * 1024 * 1024
    fd, path = tempfile.mkstemp(prefix="peimian-upload-", suffix=ext)
    os.close(fd)
    try:
        counted = 0
        with open(path, "wb") as tmp:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                counted += len(chunk)
                if counted > max_bytes:
                    raise UploadTooLarge()
                tmp.write(chunk)
        return path
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _create_session_sync(
    *, config, coach, graph, position, jd, resume_path, resume_text
):
    """同步进件 + 建场，跑在单引擎线程。流程：check_position（硬拒 → 400
    人话，灰区放行记日志）→ 双轨简历（文件优先、粘贴兜底，两轨同收敛到
    parse_pasted_resume）→ create_initial_state → 推到 AwaitIntro 挂起即停。
    """
    verdict = check_position(position)
    if not verdict.ok:
        logger.warning(
            "岗位硬拒 position=%r reasons=%s",
            position[:120] + ("…" if len(position) > 120 else ""),
            verdict.reasons,
        )
        raise PositionRejected()
    if verdict.needs_confirm:
        logger.warning("岗位灰区放行 position=%r reasons=%s", position, verdict.reasons)
    sid = uuid.uuid4().hex[:12]
    if resume_path is not None:
        try:
            resume_raw = load_resume_file(
                resume_path, max_chars=config.get("resume_max_chars", DEFAULT_MAX_CHARS)
            )
        except ResumeFileError as exc:
            raise ResumeFileInvalid(str(exc)) from exc  # 格式/大小原因透传
        finally:
            # 引擎线程侧自清：读完即删（grace 取消在途请求时，协程 finally
            # 可能与读本文件的线程竞态——此处句柄已闭，删除必成，协程侧
            # 兜底幂等）
            Path(resume_path).unlink(missing_ok=True)
        track = "file"
    else:
        resume_raw = resume_text
        track = "text"
    parsed = parse_position_input(position, jd)
    profile = candidate_profile_text(parse_pasted_resume(resume_raw))
    invoke_cfg = coach.invoke_config(sid)
    payload = create_initial_state(
        sid, parsed["position"], profile, parsed["requirements"]
    )
    run_new_session(graph, invoke_cfg, payload, lambda value: None)
    pending = pending_interrupt(graph, invoke_cfg)
    if pending is None:
        # fail-loud：图未在 AwaitIntro 挂起（引擎接线回归）——Mock 基线同款
        # 500，不许静默下发空开场问
        raise EngineError(f"创建后无挂起开场问 session={sid}")
    state_values = graph.get_state(invoke_cfg).values
    logger.info(
        "会话创建 session=%s position=%r track=%s", sid, parsed["position"], track
    )
    return sid, state_values, pending


# POST /sessions/{id}/answers —— 回答/跳过（，核心）

#: 回答体的格式坏/空 text 文案（Mock 同款「text 超长/为空」行）
_ANSWER_FORMAT_MESSAGE = '请求格式不正确：需要 {type:"answer",text} 或 {type:"skip"}'


@router.post(
    "/sessions/{session_id}/answers",
    response_model=AnswerResult,
    response_model_exclude_none=True,
)
async def submit_answer(session_id: str, request: Request) -> AnswerResult:
    state = request.app.state
    if not getattr(state, "ready", False):
        raise EngineError()  # 降级态：组合根未建，人话走 表
    payload, expect = await _parse_answer_body(request, state.web_settings)
    # 占锁（纯内存、先于引擎线程排队）：在途回合期间同会话第二发
    # 立即快速失败；未知会话无锁可占，照常进定位步拿 404
    if not state.inflight.try_acquire(session_id):
        raise TurnInFlight()
    try:
        engine_task = state.engine_executor.submit(
            functools.partial(
                _submit_answer_sync,
                config=state.config,
                coach=state.coach,
                graph=state.graph,
                session_id=session_id,
                payload=payload,
                expect=expect,
                turn_delay_ms=state.web_settings.turn_delay_ms,
            )
        )
    except BaseException:
        state.inflight.release(session_id)  # 提交失败：无任务即无 done 回调
        raise
    # 锁寿命 = 引擎回合寿命，done 回调**单点**放锁（streaming 评审修正）：
    # 原先协程 finally 的「幂等兜底」双放，在「回调放锁后、协程醒转前」的
    # 窗口里会放掉后继请求已重占的锁（release 是盲 discard，不认持有者）；
    # 排队取消路径无需 finally——cancelled 同样触发 done 回调
    engine_task.add_done_callback(lambda _f: state.inflight.release(session_id))
    # 断连语义两分法：任务已运行 → 取消不掉，跑到 END/挂起
    # 落 checkpoint（协程取消只丢 HTTP 响应，收割在任务内照常发生）；
    # 任务尚在排队 → 取消成功、回合不执行（状态未推进，客户端重连
    # GET 见旧挂起重答即可恢复）
    return await asyncio.wrap_future(engine_task)


async def _parse_answer_body(request: Request, settings):
    """ 请求：{type:"answer",text} | {type:"skip"} | 可选 expect_qnum。
    shape 闸在最前（Mock 同款：先格式后占锁）；answer 空文本/超长在
    text 行按人话出 400。"""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError, ClientDisconnect):
        body = None  # 坏 JSON/读断（客户端中途断连）→ 按空输入处理走格式 400（Mock 同款语义；断连不出 500 噪声）
    if not isinstance(body, dict):
        body = None
    ptype = body.get("type") if body else None
    if ptype == "skip":
        payload = {"type": RESUME_SKIP}
    elif ptype == "answer":
        text = body.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise TextBodyInvalid(_ANSWER_FORMAT_MESSAGE)
        if len(text) > settings.max_answer_chars:
            raise TextBodyInvalid(
                f"回答太长了（上限 {settings.max_answer_chars} 字，"
                f"当前 {len(text)} 字）——请精简后重试"
            )
        payload = {"type": RESUME_ANSWER, "text": text}
    else:
        raise TextBodyInvalid(_ANSWER_FORMAT_MESSAGE)
    expect = body.get("expect_qnum") if body else None
    if expect is not None and (not isinstance(expect, str) or not expect):
        # 带了但类型/形状不对（数字笔误等）→ 400：幂等防护不许被类型笔误
        # 无声关闭（静默当「不带」会让盲重试失去唯一挡板）
        raise TextBodyInvalid(_ANSWER_FORMAT_MESSAGE)
    # 注：本端点无传输层体积预检（回环单用户威胁模型，20000 字闸是语义上限）
    return payload, expect


def _preflight_round(graph, coach, config, session_id, expect, turn_delay_ms):
    """五步流程的第 1-3 步（legacy 与流式共用）：调试闸 → 定位（无
    checkpoint → 404）→ classify 前置闸（limbo 410 / finished 409 /
    awaiting 才进 expect 校验）。返回 (invoke_cfg, snap, slot_expect)。
    异常原样穿出——流式路径靠「emit 前发生」让这些 4xx 走 JSON 腿；
    权威题号恒在此求值（两段论对称，不依赖客户端带不带 expect_qnum）。
    """
    if turn_delay_ms:
        time.sleep(turn_delay_ms / 1000)  # 调试闸（默认 0）：人为拖长回合，联调人工场景用
    invoke_cfg = coach.invoke_config(session_id)
    snap = graph.get_state(invoke_cfg)
    if not (snap.values or {}):
        raise SessionNotFound()
    pending = pending_interrupt(graph, invoke_cfg)
    status = classify_session(snap, pending)
    if status == "limbo":
        raise LimboState()  # 前置闸：避免 NoPendingInterrupt 落入未定义分支
    if status == "finished":
        raise SessionFinished()
    slot_expect = expect_qnum(snap.values, config["max_questions"])
    if expect is not None and expect != slot_expect:
        raise ExpectMismatch()
    return invoke_cfg, snap, slot_expect


def _submit_answer_sync(
    *, config, coach, graph, session_id, payload, expect, turn_delay_ms
):
    """五步流程的 2-5 步，跑在单引擎线程：_preflight_round 共用内核 →
    run_resumed_session（on_interrupt 第一次返回本次应答、第二次返回 None
    结束驱动，新状态已落 checkpoint）→ END 则 persist_session 落收割 +
    build_final。
    """
    invoke_cfg, _snap, _slot_expect = _preflight_round(
        graph, coach, config, session_id, expect, turn_delay_ms
    )

    answered = {"done": False}

    def on_interrupt(value):
        if answered["done"]:
            return None  # 下一挂起点：结束驱动（DriverResult(None, False)）
        answered["done"] = True
        return payload

    result = run_resumed_session(graph, invoke_cfg, on_interrupt)
    if result.finished:
        persist_session(config, result.final_values)  # 收割落盘（三收口之三）
        return FinalResult(**build_final(result.final_values, config["max_questions"]))
    state_values = graph.get_state(invoke_cfg).values
    pending = pending_interrupt(graph, invoke_cfg)
    if pending is None:
        raise EngineError(f"回合结束但无新挂起 session={session_id}")
    return QuestionResult(
        question=pending_question(state_values, pending, config["max_questions"])
    )


# POST /sessions/{id}/answers/stream —— 流式应答


def _sse_encode(event: dict) -> str:
    """SSE 帧编码：event 行 + 单行 JSON data（payload 自带 event 键，自描述）。"""
    return f"event: {event.get('event', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _first_stream_event(engine_task, queue) -> dict:
    """两段论流前段：先 await 引擎首个结果——任务在流前段以域异常失败则
    重抛（穿出到 DomainError handler 出 JSON + 状态码）；首事件到达则
    交回生成器最先写出。
    """
    fut = asyncio.ensure_future(asyncio.wrap_future(engine_task))
    getter = asyncio.ensure_future(queue.get())
    try:
        while True:
            _done, _ = await asyncio.wait(
                {getter, fut}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter.done():
                event = getter.result()
                if event is None:
                    # 哨兵 = 任务已收尾：带异常则 await 重抛（哨兵不得把
                    # JSON 腿掩盖成 500）；正常收尾却零事件 = 不变量破坏，
                    # 走 EngineError JSON 腿
                    await fut
                    raise EngineError()
                return event
            if fut.done():
                exc = fut.exception()
                if exc is not None:
                    raise exc
                # fut 正常完成但事件未取到（已排程）：继续等 getter
    finally:
        if not getter.done():
            getter.cancel()


async def _sse_stream(first: dict, queue: asyncio.Queue, keepalive_s: float):
    """SSE 生成器：首事件先出，此后按序；终局事件（question/final/error）
    写出后收流。queue 空等 keepalive_s 超时写 `: ping` 保活。哨兵 None
（任务收尾而无终局事件）→ 兜底 error 收口，连接不挂死。断连
（GeneratorExit）自然穿出：不取消引擎任务、不放锁——锁寿命 = 回合寿命。
    """
    yield _sse_encode(first)
    if first.get("event") in ("question", "final", "error"):
        return
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
        except asyncio.TimeoutError:
            yield ": ping\n\n"
            continue
        if event is None:
            yield _sse_encode(
                {"event": "error", "error": EngineError.default_message}
            )
            return
        yield _sse_encode(event)
        if event.get("event") in ("question", "final", "error"):
            return


def _submit_answer_stream_sync(
    *, config, coach, graph, session_id, payload, expect,
    turn_delay_ms, emit,
):
    """五步流程的流式变体，跑在单引擎线程。preflight 异常在 emit 之前发生
    → 原样穿出走 JSON 腿（两段论前半：404/409/410/400 均普通 JSON）；
    round 之后的一切失败落 error 事件（域异常带 message，≥500 用
    default_message；未知异常 logger.exception）。

    stage 事件：evaluating/开场轮 issuing 与 round 同帧直发，其余由
    on_token 观察 phase 变化触发（幂等）；delta 由 QuestionTextTap 从
    chunk 增量抠出（追问轮 followup_text / 主问 text）。
    """
    invoke_cfg, snap, slot_expect = _preflight_round(
        graph, coach, config, session_id, expect, turn_delay_ms
    )
    max_questions = config["max_questions"]
    try:
        opening = slot_expect == "opening"
        phase = {"current": "issuing" if opening else "evaluating"}
        emit({"event": "round", "expect": slot_expect})
        emit({"event": "stage", "phase": phase["current"]})

        tap = QuestionTextTap()

        def on_token(chunk, metadata):
            node = (metadata.get("langgraph_node") or "").lower()
            if "report" in node and phase["current"] != "reporting":
                phase["current"] = "reporting"
                emit({"event": "stage", "phase": "reporting"})
            elif "issue" in node and phase["current"] != "issuing":
                phase["current"] = "issuing"
                emit({"event": "stage", "phase": "issuing"})
            text = tap.feed(chunk, metadata)
            if text:
                emit({"event": "delta", "text": text})

        answered = {"done": False}

        def on_interrupt(value):
            if answered["done"]:
                return None
            answered["done"] = True
            return payload

        result = run_resumed_session(graph, invoke_cfg, on_interrupt, on_token=on_token)
        if result.finished:
            persist_session(config, result.final_values)
            final = FinalResult(**build_final(result.final_values, max_questions))
            emit({"event": "final", **final.model_dump(exclude_none=True)})
            return final
        state_values = graph.get_state(invoke_cfg).values
        pending = pending_interrupt(graph, invoke_cfg)
        if pending is None:
            raise EngineError(f"回合结束但无新挂起 session={session_id}")
        question = QuestionResult(
            question=pending_question(state_values, pending, max_questions)
        )
        emit({"event": "question", **question.model_dump(exclude_none=True)})
        return question
    except DomainError as exc:
    # ≥500 不携带内部文本（同款纪律）；<500 用域异常人话
        message = type(exc).default_message if exc.http_status >= 500 else exc.message
        emit({"event": "error", "error": message})
        return None
    except Exception:  # noqa: BLE001 —— 流中失败面：error 事件 + 日志，不炸连接
        logger.exception("流式回合引擎异常 session=%s", session_id)
        emit({"event": "error", "error": EngineError.default_message})
        return None


@router.post("/sessions/{session_id}/answers/stream")
async def submit_answer_stream(session_id: str, request: Request):
    """流式应答端点：请求体与 legacy 逐字段相同。两段论——流前失败
（解析/404/409/410/400/强降 503）普通 JSON + 状态码，首事件到达后
    200 + text/event-stream。断连不取消（引擎回合照跑）；锁寿命 = 回合寿命
（done 回调，不挂在 SSE 连接/生成器上）。
    """
    state = request.app.state
    if not getattr(state, "ready", False):
        raise EngineError()  # 降级态：组合根未建，人话走 表
    settings = state.web_settings
    payload, expect = await _parse_answer_body(request, settings)
    if settings.stream_disabled:
        raise StreamDisabled()
    if not state.inflight.try_acquire(session_id):
        raise TurnInFlight()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(event: dict) -> None:
        # executor → asyncio 线程安全桥：不阻塞引擎线程，事件量随回合封顶
        #（无界队列峰值有界）。循环已死时丢弃并记日志——不让桥层
        # RuntimeError 掩盖引擎原始异常
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            logger.debug("SSE 桥已关，丢弃事件 %r", event.get("event"))

    engine_task = state.engine_executor.submit(
        functools.partial(
            _submit_answer_stream_sync,
            config=state.config,
            coach=state.coach,
            graph=state.graph,
            session_id=session_id,
            payload=payload,
            expect=expect,
            turn_delay_ms=settings.turn_delay_ms,
            emit=emit,
        )
    )

    def _on_engine_done(_fut) -> None:
    # 锁寿命 = 回合寿命：放锁只认任务收尾，与连接/生成器无关
        #（生成器挂着不读、客户端早断，都不影响）
        state.inflight.release(session_id)
        # 哨兵兜底：任务收尾后终局事件若已发出，哨兵无人读（生成器已收流，
        # 无害）；若未发出（不变量破坏）→ 生成器据此发 error 收口。循环
        # 已死时无处可投（生成器已不在，锁已放）——静默
        try:
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:
            pass

    engine_task.add_done_callback(_on_engine_done)
    try:
        first = await _first_stream_event(engine_task, queue)
    except BaseException:
        # 协程被取消（优雅关闭）或任务排队被取消：传播取消（断连语义
        # 两分法同 legacy——排队任务可取消，运行中任务跑到落 checkpoint）
        engine_task.cancel()
        raise
    return StreamingResponse(
        _sse_stream(first, queue, keepalive_s=settings.keepalive_s),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )



# GET /sessions/{id} —— 会话详情（，恢复唯一数据源）

@router.get(
    "/sessions/{session_id}",
    response_model=SessionState,
    response_model_exclude_none=True,
)
async def get_session(session_id: str, request: Request) -> SessionState:
    state = request.app.state
    if not getattr(state, "ready", False):
        raise EngineError()
        # in-flight 快速路径：纯内存判定，先于引擎线程排队——
    # 在途回合期间 GET 在队列外立即 409（同 answers 同文案），不被长回合
    # 拖到 30s 超时；回合结束后首次 GET 即恢复。不可用「二号只读
    # checkpointer 连接」替代（中途 get_state 会读到中间超步态 → 伪 410）
    if session_id in state.inflight:
        raise TurnInFlight()
    return await asyncio.get_running_loop().run_in_executor(
        state.engine_executor,
        functools.partial(
            _get_session_sync,
            config=state.config,
            coach=state.coach,
            graph=state.graph,
            session_id=session_id,
        ),
    )


def _get_session_sync(*, config, coach, graph, session_id):
    """三态视图组装跑在单引擎线程；组装单点在 gateway.session_view
    。数据源只有 get_state + pending_interrupt——不读 data/sessions
    收割产物（能力地图开放问题 3 的定案：权威只在 checkpoint）。"""
    invoke_cfg = coach.invoke_config(session_id)
    snap = graph.get_state(invoke_cfg)
    if not (snap.values or {}):
        raise SessionNotFound()  # 含旧图签名失联（thread 直拼拼不中）
    pending = pending_interrupt(graph, invoke_cfg)
    return session_view(
        session_id=session_id,
        values=snap.values,
        pending=pending,
        status=classify_session(snap, pending),
        max_questions=config["max_questions"],
    )


# GET /sessions —— 会话列表


def _utcnow() -> datetime:
    """时钟 seam：stale 判定的「现在」——测试 monkeypatch 本名做确定性
    时间旅行（aging checkpoint 的 ts 会连带剥中断记录变 limbo，走不通）。"""
    return datetime.now(timezone.utc)


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    response_model_exclude_none=True,
)
async def list_sessions(request: Request):
    state = request.app.state
    if not getattr(state, "ready", False):
        raise EngineError()
    # 无 in-flight 快速路径：列表跨会话，长回合期间排队挂起是 记录在案的
    # 已知降级（表现为前端加载态延长后最终成功，非报错）
    return await asyncio.get_running_loop().run_in_executor(
        state.engine_executor,
        functools.partial(
            _list_sessions_sync,
            config=state.config,
            coach=state.coach,
            graph=state.graph,
            saver=state.saver,
            stale_days=state.web_settings.stale_days,
        ),
    )


def _list_sessions_sync(*, config, coach, graph, saver, stale_days):
    """session_rows 模式：saver.list 枚举 → 当前图签名过滤 → 每 thread
    get_state 重建行。行字段/三态/排序口径全在 gateway，本函数只做编排。
    """
    suffix = f"::{graph_signature(config)}"
    latest: dict[str, str] = {}    # thread_id → 最大 checkpoint_id（排序键）
    earliest_ts: dict[str, str] = {}
    latest_ts: dict[str, str] = {}
    for tup in saver.list(None):
        tid = tup.config["configurable"]["thread_id"]
        if not tid.endswith(suffix):
            continue                # 旧图签名失联会话不列（与 CLI 同口径）
        cid = tup.config["configurable"].get("checkpoint_id") or ""
        ts = tup.checkpoint.get("ts") or ""
        if tid not in latest or cid >= latest[tid]:
            latest[tid] = cid
            latest_ts[tid] = ts
        if tid not in earliest_ts or ts < earliest_ts[tid]:
            earliest_ts[tid] = ts

    max_questions = config["max_questions"]
    now = _utcnow()
    rows = []
    # 最后活动新→旧：checkpoint_id 是 uuid6 时间序（同 CLI session_rows）
    for tid in sorted(latest, key=latest.get, reverse=True):
        sid = tid[: -len(suffix)]
        cfg = {"configurable": {"thread_id": tid}}
        snap = graph.get_state(cfg)
        values = snap.values or {}
        pending = pending_interrupt(graph, cfg)
        row = {
            "session_id": sid,
            "position": values.get("position") or "",
            "started_at": earliest_ts.get(tid, ""),
            "progress": progress(values, pending, max_questions),
        }
        status = classify_session(snap, pending)
        if status == "finished":
            row["status"] = "finished"
            row["overall"] = overall_score(values.get("topic_results"))
        elif status == "limbo":
            row["status"] = "interrupted"   # 引擎中断态
        else:                               # awaiting：时间线划界
            # stale 边界取 ≥（满 N 整天无活动即归 interrupted）；ts 解析失败
            # 按不 stale 处理（保守方向：不把可疑数据归入 interrupted）
            last_ts = _parse_checkpoint_ts(latest_ts.get(tid, ""))
            if last_ts is not None and (now - last_ts).days >= stale_days:
                row["status"] = "interrupted"   # 挂起超 stale 天数无活动
            else:
                row["status"] = "ongoing"
                # pending_qnum：仅 awaiting（ongoing）且非 opening 挂起时给出
                #（与 IssuedQuestion.qnum 同口径；opening 挂起省略该键）。
                # current_slot 校验和违例会让整个列表 500——与详情端点同一
                # fail-loud 策略（坏数据不静默降级成半截行）
                qnum = current_slot(values, max_questions).get("qnum")
                if qnum:
                    row["pending_qnum"] = qnum
        rows.append(SessionSummary(**row))
    return rows


def _parse_checkpoint_ts(raw: str):
    """checkpoint ts → aware datetime；解析失败返回 None（按不 stale 处理，
    保守方向：不把可疑数据归入 interrupted）。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
