"""服务骨架·进程面测试。
锁四件事：[api] 组缺席 → 本文件整组跳过（dense 同款契约）；/api/health
200（组合根就绪）与 503（checkpointer 打不开 → 人话）；create_app(config=…)
注入口真的被用（离线 tmp 配置，不碰开发库）；关闭顺序合同：executor
join 先于 close_checkpointer，引擎线程非 daemon。
"""
import asyncio
import importlib.util
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过
pytest.importorskip("httpx")  # TestClient 的依赖，随 [api] 组安装
pytest.importorskip("uvicorn")  # __main__ 顶层 import uvicorn——半装环境守它才跳而非报错

from fastapi.testclient import TestClient  # noqa: E402
from web_testkit import loopback_client, offline_web_config  # noqa: E402

import interviewer.web.__main__ as web_main  # noqa: E402
import interviewer.web.app as web_app  # noqa: E402
from interviewer.web.settings import WebSettings  # noqa: E402


def test_health_ok_with_injected_config(tmp_path):
    cfg = offline_web_config(tmp_path)
    app = web_app.create_app(config=cfg)
    with loopback_client(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert app.state.config is cfg  # 注入口：测试离线 seam 生效
        assert app.state.ready is True
        # 前缀合同：路由统一带 /api 前缀，裸 /health 不挂载
        assert client.get("/health").status_code == 404


def test_health_503_when_checkpointer_broken(tmp_path, monkeypatch):
    def _boom(cfg):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(web_app, "build_checkpointer", _boom)
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 503
        assert "error" in resp.json()
        assert "服务启动失败" in resp.json()["error"]
        assert isinstance(app.state.startup_error, RuntimeError)


def test_slow_endpoint_defaults_to_immediate(tmp_path):
    """ 长 POST 用例的端点侧：默认 0 = 立即返回（睡 0 ms，形状锁定）。"""
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as client:
        resp = client.post("/api/__test/slow")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "slept_ms": 0}


def test_slow_endpoint_sleeps_configured_ms(tmp_path, monkeypatch):
    """INTERVIEWER_WEB_TEST_SLEEP_MS 真的拖长响应（经代理的长 POST 用例
    打这个开关）；app 创建时读 env，所以 setenv 必须在 create_app 前。"""
    monkeypatch.setenv("INTERVIEWER_WEB_TEST_SLEEP_MS", "150")
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as client:
        started = time.perf_counter()
        resp = client.post("/api/__test/slow")
        elapsed = time.perf_counter() - started
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "slept_ms": 150}
    # Windows asyncio 时钟粒度 ~15.6ms：sleep(0.15) 可比墙钟早一拍返回，
    # 断言留余量防 flake（下界仍远高于 0——睡没睡一眼可辨）
    assert elapsed >= 0.10


def test_default_path_loads_dotenv_before_get_config(monkeypatch, tmp_path):
    """ 组合根顺序（默认路径）：dotenv 先于 get_config——.env 里的 key 与
    INTERVIEWER_* 覆盖只有这一条路进来，锁死防回归。"""
    calls: list[str] = []

    def fake_get_config():
        calls.append("get_config")
        return offline_web_config(tmp_path)

    monkeypatch.setattr(web_app, "_load_dotenv", lambda: calls.append("dotenv"))
    monkeypatch.setattr(web_app, "get_config", fake_get_config)
    app = web_app.create_app()  # config=None → 默认路径
    with loopback_client(app) as client:
        assert client.get("/api/health").status_code == 200
    assert calls == ["dotenv", "get_config"]


def test_shutdown_order_join_before_close(tmp_path, monkeypatch):
    """关闭顺序合同：executor.shutdown(wait=True) 先于 close_checkpointer，
    且 join 真的等了在途任务（close 时 fut 已 done）——不是只看调用序。"""
    events: list[str] = []
    holder: dict = {}

    real_executor = web_app._build_engine_executor()

    class RecordingExecutor:
        def submit(self, *args, **kwargs):
            return real_executor.submit(*args, **kwargs)

        def shutdown(self, wait=True, **kwargs):
            events.append("shutdown_enter")
            real_executor.shutdown(wait=wait, **kwargs)
            events.append("shutdown_returned")

    monkeypatch.setattr(web_app, "_build_engine_executor", RecordingExecutor)

    real_build = web_app.build_checkpointer

    def recording_build(cfg):
        saver, close = real_build(cfg)

        def close_rec():
            events.append("close")
            assert holder["fut"].done(), "close_checkpointer 必须晚于 executor join（§7）"

        return saver, close_rec

    monkeypatch.setattr(web_app, "build_checkpointer", recording_build)

    app = web_app.create_app(config=offline_web_config(tmp_path))

    async def scenario():
        async with app.router.lifespan_context(app):
            release = threading.Event()
            holder["fut"] = app.state.engine_executor.submit(release.wait)
            threading.Timer(0.3, release.set).start()  # 在途"回合"0.3s 后自了结

    asyncio.run(scenario())
    assert events == ["shutdown_enter", "shutdown_returned", "close"]


def test_engine_thread_is_not_daemon(tmp_path):
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app):
        fut = app.state.engine_executor.submit(lambda: None)
        fut.result()
        engine_threads = [t for t in threading.enumerate() if t.name.startswith("engine")]
        assert engine_threads, "引擎线程应已存在（submit 即孵化）"
        assert all(t.daemon is False for t in engine_threads)  # Never


def test_inflight_table_semantics():
    table = web_app.InFlightTable()
    assert table.try_acquire("s1") is True
    assert "s1" in table
    assert table.try_acquire("s1") is False  # 同会话第二发占锁失败 → 409 的根
    assert table.try_acquire("s2") is True
    table.release("s1")
    assert "s1" not in table
    assert table.try_acquire("s1") is True


def test_settings_defaults_and_env_overrides(monkeypatch):
    for key in (
        "INTERVIEWER_WEB_STALE_DAYS",
        "INTERVIEWER_WEB_MAX_UPLOAD_MB",
        "INTERVIEWER_WEB_MAX_RESUME_CHARS",
        "INTERVIEWER_WEB_MAX_ANSWER_CHARS",
        "INTERVIEWER_WEB_TURN_DELAY_MS",
        "INTERVIEWER_WEB_TEST_SLEEP_MS",
    ):
        monkeypatch.delenv(key, raising=False)
    defaults = WebSettings.from_env()
    assert (
        defaults.stale_days,
        defaults.max_upload_mb,
        defaults.max_resume_text_chars,
        defaults.max_answer_chars,
        defaults.turn_delay_ms,
        defaults.test_sleep_ms,
    ) == (7, 10, 8000, 20000, 0, 0)  # 定案值（test_sleep_ms 默认关）

    monkeypatch.setenv("INTERVIEWER_WEB_STALE_DAYS", "14")
    monkeypatch.setenv("INTERVIEWER_WEB_TURN_DELAY_MS", "90000")
    tuned = WebSettings.from_env()
    assert tuned.stale_days == 14 and tuned.turn_delay_ms == 90000

    monkeypatch.setenv("INTERVIEWER_WEB_MAX_UPLOAD_MB", "ten")
    with pytest.raises(ValueError):  # fail-loud：拼错启动即抛，不静默用默认值
        WebSettings.from_env()

    monkeypatch.setenv("INTERVIEWER_WEB_STALE_DAYS", "-1")
    with pytest.raises(ValueError):  # 值域：类型对但语义荒（负数）同样 fail-loud
        WebSettings.from_env()

        # 流式两闸：默认值 + 布尔 spell-check fail-loud（拼错静默当
    # False 对强降开关是「想关没关成」的反向静默）
    monkeypatch.setenv("INTERVIEWER_WEB_STALE_DAYS", "7")
    monkeypatch.setenv("INTERVIEWER_WEB_MAX_UPLOAD_MB", "10")
    monkeypatch.setenv("INTERVIEWER_WEB_KEEPALIVE_S", "0.5")
    monkeypatch.setenv("INTERVIEWER_WEB_STREAM_DISABLED", "off")
    knobs = WebSettings.from_env()
    assert knobs.keepalive_s == 0.5 and knobs.stream_disabled is False
    monkeypatch.setenv("INTERVIEWER_WEB_STREAM_DISABLED", "flase")
    with pytest.raises(ValueError):
        WebSettings.from_env()


def test_main_rejects_non_loopback_and_passes_grace(monkeypatch):
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(web_main.uvicorn, "run", fake_run)
    assert web_main.main(["--host", "0.0.0.0"]) == 2  # Never：入口硬拒非回环
    assert captured == {}
    assert web_main.main([]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
    assert captured["timeout_graceful_shutdown"] == 330  # 显式优雅关闭预算
    assert web_main.main(["--host", "::1"]) == 0  # IPv6 回环放行


def _load_limbo_script():
    """importlib 按路径装载 scripts/run_web_limbo.py（test_promote_staging 等
    脚本测试的同款模式——scripts/ 不是包）。"""
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_web_limbo.py"
    spec = importlib.util.spec_from_file_location("run_web_limbo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_web_limbo_script_guard_and_isolation(tmp_path, monkeypatch):
    """T11 评审确认项：limbo 场景脚本的两道离线闸——

    - 非回环 --host 硬拒 return 2 且不起服务（Never 逻辑的脚本侧副本，
      原处由 test_main_rejects_non_loopback_and_passes_grace 锁）
    - config 隔离合同：tmp sqlite + mock provider + tmp data_dir。丢了
      checkpoint_path/data_dir 覆盖，get_config() 落回 data/checkpoints.sqlite
      ——limbo thread 会被静默种进真实开发库（脚本 docstring「不碰开发库」
      承诺的机器闸）
（「种库 lifespan 进出 → 二次进入从 sqlite 读回」的核心机制由
    test_web_detail.py 的 sqlite 重入用例锁）
    """
    mod = _load_limbo_script()
    captured: dict = {}
    seeded: list[str] = []
    monkeypatch.setattr(
        mod.uvicorn, "run", lambda app, **kwargs: captured.update(app=app, kwargs=kwargs)
    )
    monkeypatch.setattr(
        mod, "make_limbo", lambda app, sid, position="后端开发": seeded.append(sid)
    )
    # 脚本 main 内部会 setenv——先经 monkeypatch 落锚，teardown 统一还原
    monkeypatch.setenv("INTERVIEWER_LLM_PROVIDER", "mock")

    assert mod.main(["--host", "0.0.0.0"]) == 2  # 硬拒在起服务与种库之前
    assert captured == {}
    assert seeded == []

    assert mod.main([]) == 0
    assert len(seeded) == 1  # 种库真的发生（fake 只替身，不动引擎）
    cfg = captured["app"].state.config
    assert cfg["llm_provider"] == "mock"
    assert cfg["checkpoint_backend"] == "sqlite"
    assert "peimian-limbo-" in cfg["checkpoint_path"]  # tmp 库，非开发库路径
    assert "peimian-limbo-" in cfg["data_dir"]
    assert captured["kwargs"]["timeout_graceful_shutdown"] == web_app.GRACEFUL_SHUTDOWN_SECONDS
