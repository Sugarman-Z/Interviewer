"""服务层配置。与引擎 config（default_config）刻意分家：只管 HTTP 传输层
旋钮（stale 天数/体积闸/字数闸/调试闸），全部走 INTERVIEWER_WEB_* 前缀，
不混入引擎 config、不碰图签名六键。env 值按字段类型强转，拼错启动即抛。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

_ENV_INT_FIELDS = {
    "INTERVIEWER_WEB_STALE_DAYS": "stale_days",
    "INTERVIEWER_WEB_MAX_UPLOAD_MB": "max_upload_mb",
    "INTERVIEWER_WEB_MAX_RESUME_CHARS": "max_resume_text_chars",
    "INTERVIEWER_WEB_MAX_ANSWER_CHARS": "max_answer_chars",
    "INTERVIEWER_WEB_TURN_DELAY_MS": "turn_delay_ms",
    "INTERVIEWER_WEB_TEST_SLEEP_MS": "test_sleep_ms",
}

#: 流式两闸：keepalive 秒（保活 ping 间隔，测试 monkeypatch 毫秒级）与
#: 强降开关（answers/stream 一律流前 503，legacy 不受影响）
_ENV_FLOAT_FIELDS = {
    "INTERVIEWER_WEB_KEEPALIVE_S": "keepalive_s",
}
_ENV_BOOL_FIELDS = {
    "INTERVIEWER_WEB_STREAM_DISABLED": "stream_disabled",
}
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class WebSettings:
    """HTTP 层全部旋钮（默认值见字段）。

    stale_days：挂起超 N 天无活动 → interrupted；max_upload_mb：体积闸
（CL 预检 + 流式计数双保险）；max_resume_text_chars / max_answer_chars：
    粘贴简历/回答字数闸（引擎对 answer 不截断，闸必须在传输层）；
    turn_delay_ms：调试闸，默认 0 关，联调人工场景用；test_sleep_ms：
    /api/__test/slow 睡眠毫秒；keepalive_s：SSE 保活间隔（兜住前端 45s
    死亡判定）；stream_disabled：流式强降（流前 503，legacy 不受影响）。
    """

    stale_days: int = 7
    max_upload_mb: int = 10
    max_resume_text_chars: int = 8000
    max_answer_chars: int = 20000
    turn_delay_ms: int = 0
    test_sleep_ms: int = 0
    keepalive_s: float = 15.0
    stream_disabled: bool = False

    @classmethod
    def from_env(cls) -> "WebSettings":
        values: dict[str, object] = {}
        for env_key, field in _ENV_INT_FIELDS.items():
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{env_key} 需要整数，收到 {raw!r}") from exc
            if value < 0:
                raise ValueError(f"{env_key} 需要非负整数，收到 {raw!r}")
            values[field] = value
        for env_key, field in _ENV_FLOAT_FIELDS.items():
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"{env_key} 需要数字，收到 {raw!r}") from exc
            if value <= 0:
                raise ValueError(f"{env_key} 需要正数，收到 {raw!r}")
            values[field] = value
        for env_key, field in _ENV_BOOL_FIELDS.items():
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            lowered = raw.strip().lower()
            if lowered in _TRUTHY:
                values[field] = True
            elif lowered in _FALSY:
                values[field] = False
            else:
            # fail-loud 同款纪律：拼错值静默当 False，
                # 对强降开关而言是「想关没关成」的反向静默
                raise ValueError(
                    f"{env_key} 需要布尔（{'/'.join(sorted(_TRUTHY | _FALSY))}），"
                    f"收到 {raw!r}"
                )
        return cls(**values)
