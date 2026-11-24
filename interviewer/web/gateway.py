"""gateway：引擎 ↔ wire 的纯翻译层。全模块不 import fastapi（可脱离框架
单测；HTTP 码与文案的翻译在 app.py 的全局 handler）。错误文案与映射规则
以本模块为唯一定义点——deletion test：删掉 gateway，这些规则应消失，
而不是散进端点。
"""
from __future__ import annotations

import math
from typing import NamedTuple

from interviewer.agents.schemas import SCORE_DIMS
from interviewer.agents.utils.agent_utils import resume_text
from interviewer.web.schemas import SessionState


class DomainError(Exception):
    """领域异常基类：app.py 的全局 handler 按本类拦截全部子类。

    message 属性是响应体 {"error": message} 的唯一来源。
    """

    http_status: int = 500
    default_message: str = ""

    def __init__(self, message: str | None = None) -> None:
        self.message = message if message is not None else self.default_message
        super().__init__(self.message)


class PositionRejected(DomainError):
    """规则层硬拒/岗位空。硬拒的具体原因在 raise 现场给 message。"""

    http_status = 400
    default_message = "岗位还没填——教练需要它来为你出题"


class ResumeFileInvalid(DomainError):
    """ResumeFileError 的翻译载体：具体格式/大小原因透传——文案只能
    来自 raise 现场，message 必填（漏传在构造期 TypeError，不静默出空文案）。"""

    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UploadTooLarge(DomainError):
    """上传体积闸（，Content-Length 预检 + 流式计数双保险的出口）。"""

    http_status = 413
    default_message = "简历文件太大，请压缩后重试"


class TextBodyInvalid(DomainError):
    """text 超长/为空（/）：message 必填，须指明字段与上限。"""

    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)


class HostOriginRejected(DomainError):
    """Host/Origin 白名单拒绝（回环与来源纪律）。"""

    http_status = 403
    default_message = "仅限本机访问"


class SessionNotFound(DomainError):
    """thread 直拼不中（含旧图签名失联/）。"""

    http_status = 404
    default_message = "会话不存在或已失效"


class TurnInFlight(DomainError):
    """同会话并发第二发与 GET 快速路径共用（//）。"""

    http_status = 409
    default_message = "上一条回答还在处理中，请稍候再试"


class ExpectMismatch(DomainError):
    """expect_qnum 幂等根防护不匹配（/）。"""

    http_status = 409
    default_message = "这道题已经收到了你的回答，请刷新看看当前题目"


class SessionFinished(DomainError):
    """已收场会话再发回答。"""

    http_status = 409
    default_message = "本场面试已结束"


class LimboState(DomainError):
    """snap.next 非空且无挂起中断：引擎中断态，不假装可续。"""

    http_status = 410
    default_message = "这场面试的记录不完整（引擎中断），无法恢复"


class EngineError(DomainError):
    """引擎异常（含 校验和违例）：traceback 只进日志，响应体
    永不携带内部异常文本。"""

    http_status = 500
    default_message = "面试官暂时掉线了，请稍后刷新查看本场状态"


class StreamDisabled(DomainError):
    """流式端点强降开关（INTERVIEWER_WEB_STREAM_DISABLED=1，
    SPEC-streaming）：/answers/stream 一律流前 503 人话，legacy
    POST /answers 不受影响——M3「流式不可用注入」的回退闭环落点。"""

    http_status = 503
    default_message = "流式输出暂不可用，本场将使用整轮等待模式"


# ═══ 翻译核心（T3；全部纯函数，确定性，测试锁死）══════════════

#: 跳过哨兵：经引擎自己的 resume_text 取得，不手抄字面量（——引擎改
#: 文案这里自动跟随；锁死测试断言两者相等）。skip 条目的 wire text
#: = 哨兵原文，前端凭 variant=skip 渲染「已跳过」标注
SKIP_SENTINEL = resume_text({"type": "skip"})

#: eval_failed 条目的 wire 文案（表：引擎原文不透传，换前端语境人话）
EVAL_FAILED_TEXT = "这一题的评分暂时不可用，已换下一题"


class _Walk(NamedTuple):
    """transcript 单遍走查结果（/ 的实现单点）。"""

    messages: list          # wire TranscriptMessage 列表（issue_done 不下发）
    candidate_slots: list   # 每条 candidate 消息的题槽位（None=开场，无题号）
    main_n: int             # 主问号 = 已签发主题题数（与 issued_keys 互为校验）
    last_slot: dict | None  # 最近一问的槽位——挂起态下即当前待答槽位


def _interviewer_question_message(variant: str, text: str, slot: dict) -> dict:
    msg = {"role": "interviewer", "variant": variant, "text": text}
    if slot["topic"]:
        msg["topic"] = slot["topic"]
    msg["qnum"] = slot["qnum"]
    return msg


def _walk_transcript(state: dict, max_questions: int) -> _Walk:
    """映射表 + 题号推导的单遍走查（transcript_to_wire / current_slot /
    topic_results_to_wire 的共同地基）。遇 kind="issue" 主问号 +1、追问序
    清零；遇 followup 追问序 +1；candidate 挂在最近一问的题号下，开场回答
    无题号。校验和 fail-loud：主问号越界 / 与 issued_keys 不一致 → EngineError。
    """
    transcript = state.get("transcript") or []
    issued = state.get("issued_keys") or []
    main_n = 0
    followup_n = 0
    slot: dict | None = None
    messages: list[dict] = []
    candidate_slots: list[dict | None] = []
    for entry in transcript:
        role = entry.get("role")
        content = entry.get("content", "")
        if role == "interviewer":
            if entry.get("phase") == "intro":
                slot = None
                messages.append(
                    {"role": "interviewer", "variant": "opening", "text": content}
                )
            elif entry.get("kind") == "issue":
                main_n += 1
                followup_n = 0
                slot = {
                    "qnum": f"{main_n}",
                    "is_followup": False,
                    "topic": entry.get("topic") or "",
                }
                messages.append(_interviewer_question_message("question", content, slot))
            elif entry.get("kind") == "followup":
                followup_n += 1
                slot = {
                    "qnum": f"{main_n}.{followup_n}",
                    "is_followup": True,
                    "topic": entry.get("topic") or "",
                }
                messages.append(_interviewer_question_message("followup", content, slot))
            else:
                raise EngineError(
                    f"未知 transcript 条目形态：role=interviewer kind={entry.get('kind')!r}"
                )
        elif role == "candidate":
            if slot is None and candidate_slots:
                raise EngineError("candidate 回答挂在无题号的槽位（数据异常）")
            skipped = content == SKIP_SENTINEL
            candidate_slots.append(
                {**slot, "skipped": skipped} if slot is not None else None
            )
            msg = {
                "role": "candidate",
                "variant": "skip" if skipped else "answer",
                "text": content,
            }
            topic = entry.get("topic") or ""
            if topic:
                msg["topic"] = topic
            if slot is not None:
                msg["qnum"] = slot["qnum"]
            messages.append(msg)
        elif role == "system":
            if entry.get("kind") == "eval_failed":
                messages.append(
                    {"role": "system", "variant": "system", "text": EVAL_FAILED_TEXT}
                )
            elif entry.get("kind") != "issue_done":
                # issue_done 不下发（：收场由 status=finished 表达；closing
                # 变体仅 Mock 剧本使用）。其余 system kind 一律 fail-loud——
                # 引擎新增条目形态属 Ask-first 范畴，静默缺行比 500 更糟
                raise EngineError(
                    f"未知 transcript 条目形态：role=system kind={entry.get('kind')!r}"
                )
        else:
            raise EngineError(f"未知 transcript 条目形态：role={role!r}")
    if main_n > max_questions:
        raise EngineError(
            f"题号校验和违例：走查主问号 {main_n} > max_questions {max_questions}"
        )
    if main_n != len(issued):
        raise EngineError(
            f"题号校验和违例：走查主问号 {main_n} ≠ issued_keys 数 {len(issued)}"
        )
    return _Walk(
        messages=messages,
        candidate_slots=candidate_slots,
        main_n=main_n,
        last_slot=slot,
    )


def transcript_to_wire(state: dict, max_questions: int) -> list[dict]:
    """引擎 transcript → wire TranscriptMessage。at 不下发（引擎无每条时间戳）。"""
    return _walk_transcript(state, max_questions).messages


def current_slot(state: dict, max_questions: int) -> dict:
    """当前待答槽位 {variant, topic?, qnum?}：expect_qnum 与 pending_question
    的唯一推导出口。issued_keys 空 → opening；最后一问是追问 → followup
（"N.k"）；否则主题题 qnum = 签发序，与走查主问号互为校验和。

    仅 awaiting 态可调（调用方先过 classify_session）；issued 非空而
    current_topic 为空 = 收场后误入 → 409 SessionFinished 而非校验和 500。
    """
    issued = state.get("issued_keys") or []
    if not issued:
        return {"variant": "opening"}
    if not (state.get("current_topic") or "").strip():
        raise SessionFinished()
    walk = _walk_transcript(state, max_questions)
    topic = state.get("current_topic") or ""
    if walk.last_slot is not None and walk.last_slot["is_followup"]:
        slot = {"variant": "followup", "qnum": walk.last_slot["qnum"]}
        if topic:
            slot["topic"] = topic
        return slot
    try:
        idx = issued.index(topic)
    except ValueError:
        raise EngineError(
            f"题号校验和违例：current_topic {topic!r} 不在 issued_keys"
        ) from None
    if idx + 1 != walk.main_n:
        raise EngineError(
            f"题号校验和违例：签发序 {idx + 1} ≠ 走查主问号 {walk.main_n}"
        )
    slot = {"variant": "question", "qnum": str(idx + 1)}
    if topic:
        slot["topic"] = topic
    return slot


def expect_qnum(state: dict, max_questions: int) -> str:
    """幂等校验值：当前槽位号（opening 写死字面量——两挂起点载荷同构，不能
    从载荷区分）。"""
    slot = current_slot(state, max_questions)
    return slot.get("qnum", "opening")


def pending_question(state: dict, pending_payload: dict | None,
                     max_questions: int) -> dict:
    """wire IssuedQuestion：text 取中断载荷 question 键（与引擎
    pending_question 同源，缺载荷兜底）。"""
    slot = current_slot(state, max_questions)
    text = (pending_payload or {}).get("question", "")
    if not text:
        text = state.get("pending_question", "")
    question = {"variant": slot["variant"], "text": text}
    if slot.get("topic"):
        question["topic"] = slot["topic"]
    if slot.get("qnum"):
        question["qnum"] = slot["qnum"]
    return question


def topic_results_to_wire(state: dict, max_questions: int) -> list[dict]:
    """engine topic_results ↔ transcript 第 i 条 candidate 一一对齐，开场
    自我介绍除外（不经 Evaluator，基数 = candidate 数 − 1）；不等长即
    EngineError。eval_failed 条目不下发 scores 键（报告页显示「未评分」）。
    """
    walk = _walk_transcript(state, max_questions)
    engine_results = state.get("topic_results") or []
    scored = walk.candidate_slots[1:]
    if len(scored) != len(engine_results):
        raise EngineError(
            f"topic_results 对齐违例：可评分回答 {len(scored)} 条"
            f" ≠ 评分条目 {len(engine_results)} 条"
        )
    wire = []
    for slot, entry in zip(scored, engine_results):
        # slot 恒非 None：walk 的孤儿守卫（非首条 candidate 无题号即炸）
        # 保证 candidate_slots 只有下标 0 可为 None，而 scored 切掉了下标 0
        item = {
            "qnum": slot["qnum"],
            "topic": entry.get("topic") or "",
            "is_followup": slot["is_followup"],
            "skipped": slot["skipped"],
        }
        scores = entry.get("scores") or {}
        if scores:
            item["scores"] = {k: scores[k] for k in SCORE_DIMS if k in scores}
        wire.append(item)
    return wire


def overall_score(engine_topic_results: list) -> float:
    """overall 聚合：所有 scores 非空条目各自三维均值再取条目间均值，半值
    进位保留一位小数；全部 eval_failed → 0.0。前端 reportLogic 同口径，
    golden 聚合锚双端锁定。两条硬约束：舍入半值进位——Python 内建 round
    是银行家舍入，.x5 平局会比 JS Math.round 低 0.1；外层求和朴素左折叠
    ——CPython≥3.12 内建 sum 对 float 是 Neumaier 补偿求和，1/3 型均值
    平局时与 JS reduce 差 1 ulp → overall 差 0.1。内层 sum 只吃 int，无此问题。
    """
    averages = []
    for entry in engine_topic_results or []:
        scores = entry.get("scores") or {}
        values = [
            scores[dim]
            for dim in SCORE_DIMS
            if isinstance(scores.get(dim), (int, float))
        ]
        if values:
            averages.append(sum(values) / len(values))
    if not averages:
        return 0.0
    total = 0.0
    for avg in averages:
        total += avg
    overall = total / len(averages)
    return math.floor(overall * 10 + 0.5) / 10


# 流式：token → 题面增量抽取


class _TargetStringScanner:
    """增量 JSON 目标串扫描器（QuestionTextTap 的内部态）：只追一个目标键的
    字符串值，跨 fragment 状态机。转义延迟解码（反斜杠落在片段末尾等下个
    字符，\\uXXXX 攒满 4 位再出字；非法 \\uXXXX 落 DONE 不出字——绝不 raise、
    绝不猜）；键匹配只发生在字符串外的位置（目标键字面出现在别的字符串值
    内部不会误配）；值闭引号后 DONE 吸掉一切；值非字符串 → DONE 不出字。
    已知边界：嵌套对象里的同名键会先匹配（现行两 schema 无嵌套目标键，
    不设防——设防 = 全量解析，违背增量初衷）。
    """

    __slots__ = ("key", "state", "_buf", "_hex", "_out")

    (_TOP, _IN_STR, _AFTER_STR, _AWAIT, _IN_VAL,
     _ESC_STR, _ESC_VAL, _UNI, _DONE) = range(9)

    _SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}

    def __init__(self, key: str):
        self.key = key
        self.state = self._TOP
        self._buf: list[str] = []
        self._hex: list[str] = []
        self._out: list[str] = []

    def push(self, fragment: str) -> str:
        """喂一段新到文本，返回本次新解码出的目标值字符（无则空串）。"""
        emitted = len(self._out)
        for ch in fragment:
            s = self.state
            if s == self._TOP:
                if ch == '"':
                    self.state = self._IN_STR
                    self._buf = []
            elif s == self._IN_STR:
                if ch == "\\":
                    self.state = self._ESC_STR
                elif ch == '"':
                    self.state = self._AFTER_STR
                else:
                    self._buf.append(ch)
            elif s == self._ESC_STR:  # 键名/普通串里的转义：按解码字面收
                self._buf.append(self._decode_escape(ch))
                self.state = self._IN_STR
            elif s == self._AFTER_STR:
                if ch == ":":
                    self.state = (
                        self._AWAIT if "".join(self._buf) == self.key else self._TOP
                    )
                elif ch not in " \t\r\n":
                    self.state = self._TOP  # 闭串后非冒号：是值不是键
            elif s == self._AWAIT:
                if ch == '"':
                    self.state = self._IN_VAL
                elif ch not in " \t\r\n":
                    self.state = self._DONE  # 非串值：绝不猜
            elif s == self._IN_VAL:
                if ch == "\\":
                    self.state = self._ESC_VAL
                elif ch == '"':
                    self.state = self._DONE
                else:
                    self._out.append(ch)
            elif s == self._ESC_VAL:
                if ch == "u":
                    self.state = self._UNI
                    self._hex = []
                else:
                    self._out.append(self._decode_escape(ch))
                    self.state = self._IN_VAL
            elif s == self._UNI:
                self._hex.append(ch)
                if len(self._hex) == 4:
                    try:
                        self._out.append(chr(int("".join(self._hex), 16)))
                    except ValueError:
                        self.state = self._DONE  # 非法 \uXXXX：本调用作废，不出字
                    else:
                        self.state = self._IN_VAL
            elif s == self._DONE:
                break  # 值已闭：吸掉本 fragment 余下字符
        return "".join(self._out[emitted:])

    def _decode_escape(self, ch: str) -> str:
        if ch in self._SIMPLE_ESCAPES:
            return self._SIMPLE_ESCAPES[ch]
        return ch  # \" \\ \/ 与陌生转义：按字面收（宽松，不炸）


class QuestionTextTap:
    """token → 题面增量抽取器：feed 喂 (chunk, metadata)，返回本次新增题面文本
（无则空串）。纯函数状态机，不碰 state/checkpoint、不做 I/O，喂错形状
    最多吐空串，绝不猜、绝不 raise。

    车道判别：tool_call_chunks 按 index 分道，首片段非空 name 锁目标 schema
    车道（issue→IssuedQuestion / evaluator→Scorecard），其余整道丢弃——GLM
    单轮并行多 tool_calls，单缓冲会把不同调用的 args 缝串。目标键按节点选：
    issue → "text"，evaluator → "followup_text"。reset 锚「节点切换 + 非空
    id 开新调用」，同一 content 调用的多 chunk 不 reset（围栏 JSON 靠它拼齐）。

    freetext 降级：bind_tools 路径 content 恒空，故 tool chunk 之后的纯
    content 必属 structured 失败后的 freetext 调用 → 重开调用；structured 腿
    吐过半截时输出是坏前缀拼接，终局事件权威替换兜底。
    """

    #: 节点名（小写包含匹配）→ (目标 schema 工具名, 目标键)
    NODE_TARGETS = {
        "issue": ("IssuedQuestion", "text"),
        "evaluator": ("Scorecard", "followup_text"),
    }

    def __init__(self):
        self._reset_state()

    def reset(self) -> None:
        self._reset_state()

    def _reset_state(self) -> None:
        self._node: str | None = None
        self._schema: str | None = None
        self._key: str | None = None
        self._scanner: _TargetStringScanner | None = None
        self._lane = None          # 目标车道 index（锁定后；None=未锁）
        self._tool_call_seen = False

    def _start_new_call(self) -> None:
        """开新 LLM 调用：车道与扫描器全清（键目标不变）。"""
        self._lane = None
        self._scanner = _TargetStringScanner(self._key)

    def feed(self, chunk, metadata: dict) -> str:
        node = (metadata or {}).get("langgraph_node", "") or ""
        n = node.lower()
        target = None
        for prefix, cfg in self.NODE_TARGETS.items():
            if prefix in n:
                target = cfg
                break
        if node != self._node:
            self._reset_state()  # 节点切换（含切到非目标节点）＝整段重来
            if target is None:
                return ""
            self._node = node
            self._schema, self._key = target
            self._scanner = _TargetStringScanner(self._key)
        elif target is None:
            return ""  # 同节点但未配置目标（不可达，防御）

        new_out: list[str] = []
        tccs = list(getattr(chunk, "tool_call_chunks", None) or [])
        if tccs:
            if any(c.get("id") for c in tccs):
                self._start_new_call()  # 非空 id = 新调用首片段
            self._tool_call_seen = True
            for c in tccs:
                idx = c.get("index")
                name = c.get("name") or ""
                if name and name == self._schema:
                    self._lane = idx
                if self._lane is not None and idx == self._lane:
                    frag = c.get("args") or ""
                    if frag:
                        new_out.append(self._scanner.push(frag))
        content = getattr(chunk, "content", "") or ""
        if content and not tccs:
            if self._tool_call_seen:
                # tool→content 转换 = structured 失败后的 freetext 降级调用
                #（bind_tools 路径 content 恒空 ⇒ 其后的纯 content 必属新调用）
                self._tool_call_seen = False
                self._start_new_call()
            new_out.append(self._scanner.push(content))
        return "".join(new_out)


def build_final(state: dict, max_questions: int) -> dict:
    """finished 态 final 三件套（answers 收场分支与 GET finished 共用）。
    interview_report 原样直传（含空串）。max_questions 显式参数（引擎
    state 不携带预算，progress 同理）。
    """
    return {
        "overall": overall_score(state.get("topic_results")),
        "interview_report": state.get("interview_report", ""),
        "topic_results": topic_results_to_wire(state, max_questions),
    }


def classify_session(snap, pending) -> str:
    """三态判定唯一实现：有挂起中断 → awaiting；图跑完（snap.next 空）→
    finished；next 非空且无挂起 → limbo（崩在超步之间，引擎无恢复路径，
    不假装可续）。
    """
    if pending is not None:
        return "awaiting"
    if not (getattr(snap, "next", None) or ()):
        return "finished"
    return "limbo"


def progress(state: dict, pending, max_questions: int) -> dict:
    """answered = len(issued_keys) − (待答题是主题题 ? 1 : 0)——追问挂起时
    主问已答、opening 时为 0；追问不计入（与 issued_keys 只记主题题同口径）。
    total 取自 config（引擎 state 不携带预算）。
    """
    issued = len(state.get("issued_keys") or [])
    if pending is None:
        answered = issued
    else:
        slot = current_slot(state, max_questions)
        answered = issued - (1 if slot["variant"] == "question" else 0)
    return {"answered": answered, "total": max_questions}


def session_view(*, session_id: str, values: dict, pending, status: str,
                 max_questions: int) -> SessionState:
    """三态视图组装单点：GET 详情端点与 golden 状态锚共用的唯一入口。
    status 来自 classify_session；limbo 抛 LimboState（410）。awaiting 附带
    不变量：末条 transcript 消息 == pending_question，违例 fail-loud。
    """
    if status == "limbo":
        raise LimboState()
    base = {
        "session_id": session_id,
        "position": values.get("position") or "",
        "max_questions": max_questions,
        "transcript": transcript_to_wire(values, max_questions),
    }
    if status == "finished":
        return SessionState(
            **base,
            status="finished",
            progress=progress(values, None, max_questions),
            final=build_final(values, max_questions),
        )
    question = pending_question(values, pending, max_questions)
    last = base["transcript"][-1] if base["transcript"] else None
    if last is None or last["text"] != question["text"]:
        raise EngineError(
            f"挂起态 transcript 末条与 pending_question 不一致 session={session_id}"
        )
    return SessionState(
        **base,
        status="awaiting",
        pending_question=question,
        progress=progress(values, pending, max_questions),
    )
