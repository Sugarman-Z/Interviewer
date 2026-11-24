"""python -m interviewer.web —— 服务进程入口。默认 127.0.0.1:8000；--host
仅接受回环地址（非回环绑定 = 公网化，必须先有鉴权，入口处硬拒）。
GRACEFUL_SHUTDOWN_SECONDS 显式传给 uvicorn：停收请求后给在途回合留足
跑完的时间（uvicorn 默认 None = 无限等，挂死不可运维）。
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from interviewer.web.app import GRACEFUL_SHUTDOWN_SECONDS, create_app

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m interviewer.web", description="陪面 HTTP 服务（api-service）"
    )
    ap.add_argument(
        "--host", default="127.0.0.1",
        help="监听地址（仅回环；非回环绑定属公网化范畴，直接拒绝）",
    )
    ap.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.host not in _LOOPBACK_HOSTS:
        print(
            f"拒绝绑定非回环地址 {args.host}：公网化另立模块且必须先有鉴权"
            f"（SPEC-api-service §9）",
            file=sys.stderr,
        )
        return 2
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
