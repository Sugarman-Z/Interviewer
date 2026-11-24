"""web 测试组的共享脚手架（离线 config / 回环 TestClient）。
Host 白名单生效后，TestClient 默认 host=testserver 会被 403——回环
base_url 是所有 web 测试的共同前提；离线 config 配方（mock provider +
memory backend + tmp data_dir）单点收拢，防多文件各抄一份后口径漂移。
文件名不带 test_ 前缀：脚手架不进测试收集；test 文件经 pytest 的
prepend 导入模式直接 `from web_testkit import …`（tests 目录在 sys.path）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from interviewer.default_config import get_config

#: TestClient 默认 host=testserver 会被 Host 白名单 403——契约即如此
BASE_URL = "http://127.0.0.1:8000"


def offline_web_config(tmp_path) -> dict:
    """离线 config（纪律）：mock provider + memory backend + tmp data_dir。"""
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 2
    return cfg


def loopback_client(app, **kwargs) -> TestClient:
    kwargs.setdefault("base_url", BASE_URL)
    return TestClient(app, **kwargs)
