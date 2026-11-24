"""挂 limbo thread 的 tmp 库起 api-service（前端恢复路径人工场景）。
用途：前端走一次 410 卡片——打开 /interview/{sid}（或从列表点入
interrupted 行）即见「这场面试的记录不完整（引擎中断），无法恢复」卡片。
limbo 态 mock provider 正常跑不出，只能手工构造（与 tests/fixtures/
limbo_maker 同款种法）。

库为全新 tmp sqlite + mock provider，不碰开发库。种库流程：进一次 lifespan
建组合根 → make_limbo（内含 classify==limbo 断言）→ 退出（引擎线程 join、
checkpointer 关闭）；uvicorn 随后正式起服务，同一 config 从 sqlite 读回
limbo thread（「种库进出 → 重入读回」由 test_web_detail 的 reentry 测试锁定）。

用法（仓库根；Windows 控制台加 PYTHONUTF8=1 防中文乱码）：
    PYTHONUTF8=1 python scripts/run_web_limbo.py              # 127.0.0.1:8000
    PYTHONUTF8=1 python scripts/run_web_limbo.py --port 8001  # 避开在用端口
前端配合（next dev 在 3000、本脚本在 8000）：浏览器直接开
    http://localhost:3000/interview/{启动日志里的 session_id}
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))  # limbo_maker 非包，路径直挂

import uvicorn  # noqa: E402

from limbo_maker import make_limbo  # noqa: E402

from interviewer.default_config import get_config  # noqa: E402
from interviewer.web.app import GRACEFUL_SHUTDOWN_SECONDS, create_app  # noqa: E402


async def _seed(app, sid: str, position: str) -> None:
    """进一次 lifespan 建组合根并种 limbo thread，退出即关干净。

    make_limbo 经引擎线程执行（与生产同纪律）；mock provider 秒回，阻塞
    等待只发生在服务启动前，无并发面。
    """
    async with app.router.lifespan_context(app):
        make_limbo(app, sid, position)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="挂 limbo thread 的 tmp 库起 api-service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--position", default="后端开发")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"--host 只允许回环地址（收到 {args.host}）", file=sys.stderr)
        return 2  # 与 interviewer.web.__main__ 同款硬拒（Never：不绑非回环）

    os.environ["INTERVIEWER_LLM_PROVIDER"] = "mock"
    db_dir = Path(tempfile.mkdtemp(prefix="peimian-limbo-"))
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "sqlite"
    cfg["checkpoint_path"] = str(db_dir / "checkpoints.sqlite")
    cfg["data_dir"] = str(db_dir / "data")  # persist_session 收割目录（limbo 不会触发）

    sid = uuid.uuid4().hex[:12]
    app = create_app(config=cfg)
    asyncio.run(_seed(app, sid, args.position))
    print(f"[limbo] session_id={sid} db={db_dir}")
    print(f"[limbo] 详情预期 410：curl http://127.0.0.1:{args.port}/api/sessions/{sid}")
    uvicorn.run(
        app, host=args.host, port=args.port, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
