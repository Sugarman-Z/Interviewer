"""创建端点测试。
锁：双轨各自成功（真进引擎——state 侧证）；文件优先；岗位空/硬拒 400
（固定文案）；灰区放行 + warning 日志；resume_text 超长 400（指明上限）；
上传超限 413；坏扩展名/坏文件 400 透传；失败路径 %TEMP% 无残留（临时
文件合同）；响应过 response_model；降级态 500 人话。
"""
import glob
import logging
import os
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # [api] 组缺席 → 整组跳过
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from web_testkit import loopback_client, offline_web_config  # noqa: E402

import interviewer.web.app as web_app  # noqa: E402
import interviewer.web.routes as web_routes  # noqa: E402
from interviewer.web.gateway import EngineError  # noqa: E402


@pytest.fixture(autouse=True)
def _no_upload_residue(tmp_path):
    """：上传失败不得在临时目录遗留含简历 PII 的文件。独享 spool 目录
（重定向 tempfile.tempdir——生产 mkstemp 无 dir 参数会跟随）——同机
    真服务的同前缀写入不再构成噪声。"""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    pattern = os.path.join(str(spool_dir), "peimian-upload-*")
    old_tempdir = tempfile.tempdir
    tempfile.tempdir = str(spool_dir)
    try:
        before = set(glob.glob(pattern))
        yield
        after = set(glob.glob(pattern))
        assert after == before, f"临时目录上传残留：{after - before}"
    finally:
        tempfile.tempdir = old_tempdir


@pytest.fixture()
def client(tmp_path):
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        yield c, app


def _engine_state(app, session_id: str) -> dict:
    def _read():
        return app.state.graph.get_state(app.state.coach.invoke_config(session_id)).values

    # 纪律：图操作走引擎线程（与生产同形状；memory backend 下直读无害，
    # 但这写法会被 T5/T6 抄走——从第一天就正）
    return app.state.engine_executor.submit(_read).result()


# 双轨成功


def test_create_json_track_success(client):
    c, app = client
    resp = c.post(
        "/api/sessions",
        json={"position": "后端开发", "jd": "会 Python", "resume_text": "五年后端经验"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {"session_id", "max_questions", "question"}
    assert len(body["session_id"]) == 12
    assert body["max_questions"] == 2  # 取自 config，不硬编码
    assert set(body["question"]) == {"variant", "text"}  # 可选键省略下发（非 null）
    assert body["question"]["variant"] == "opening"
    assert body["question"]["text"].startswith("你好")
    # 真进引擎：checkpointer 已有挂起开场问 + 进件材料落 state
    values = _engine_state(app, body["session_id"])
    assert values["position"] == "后端开发"
    assert values["jd_requirements"] == "会 Python"
    assert values["candidate_profile"] == "五年后端经验"
    assert values["pending_question"] == body["question"]["text"]


def test_create_multipart_track_success(client):
    c, app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发"},
        files={"resume_file": ("resume.txt", "python 后端，交易系统".encode("utf-8"),
                               "text/plain")},
    )
    assert resp.status_code == 201
    values = _engine_state(app, resp.json()["session_id"])
    # 文件轨：提取出的文本进引擎（上传只是 text 的另一个来源）
    assert values["candidate_profile"] == "python 后端，交易系统"


def test_file_wins_over_pasted_text(client):
    """双轨都给时文件优先（，与前端互斥逻辑一致——服务端再挡一道）。"""
    c, app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发", "resume_text": "粘贴版内容"},
        files={"resume_file": ("r.txt", "文件版内容".encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201
    assert _engine_state(app, resp.json()["session_id"])["candidate_profile"] == "文件版内容"


def test_no_resume_is_fail_open(client):
    """两轨都不给 = 没提供（fail-open by absence），照常开场。"""
    c, _app = client
    resp = c.post("/api/sessions", json={"position": "后端开发"})
    assert resp.status_code == 201
    assert resp.json()["question"]["variant"] == "opening"


# 4xx 分支


def test_position_empty_400(client):
    c, _app = client
    resp = c.post("/api/sessions", json={"position": "  "})
    assert resp.status_code == 400
    assert resp.json() == {"error": "岗位还没填——教练需要它来为你出题"}


def test_position_hard_reject_400(client):
    """规则层硬拒（纯数字误触）→ 400 固定文案，具体原因只进日志。"""
    c, _app = client
    resp = c.post("/api/sessions", json={"position": "1234"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "岗位还没填——教练需要它来为你出题"}


def test_position_grey_zone_passes_with_log(client, caplog):
    """灰区（多行粘贴）→ 放行 + warning 日志（，HTTP 无交互确认）。"""
    c, _app = client
    with caplog.at_level(logging.WARNING, logger="interviewer.web.routes"):
        resp = c.post("/api/sessions", json={"position": "后端开发\n负责交易平台"})
    assert resp.status_code == 201
    assert any("灰区放行" in r.getMessage() for r in caplog.records)


def test_resume_text_too_long_400(client):
    c, _app = client
    resp = c.post(
        "/api/sessions",
        json={"position": "后端开发", "resume_text": "字" * 8001},
    )
    assert resp.status_code == 400
    assert "8000" in resp.json()["error"]
    assert "太长" in resp.json()["error"]


def test_upload_too_large_413(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERVIEWER_WEB_MAX_UPLOAD_MB", "1")
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        resp = c.post(
            "/api/sessions",
            data={"position": "后端开发"},
            files={"resume_file": ("big.txt", b"x" * (1024 * 1024 + 200 * 1024),
                                   "text/plain")},
        )
    assert resp.status_code == 413
    assert resp.json() == {"error": "简历文件太大，请压缩后重试"}


def test_upload_chunked_streaming_count_413(monkeypatch, tmp_path):
    """流式计数腿（双保险的第二道，防 chunked 无 CL 绕过预检）：
    生成器 body 无 Content-Length，超限只能在拷贝侧被数出来；自清路径
    的无残留由 autouse fixture 对账。"""
    monkeypatch.setenv("INTERVIEWER_WEB_MAX_UPLOAD_MB", "1")
    app = web_app.create_app(config=offline_web_config(tmp_path))
    boundary = "----peimian-t4"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="position"\r\n\r\n'
        "后端开发\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="resume_file"; filename="big.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    def body_stream():
        yield head
        chunk = b"x" * (256 * 1024)
        for _ in range(6):  # 1.5MB > 1MB 上限
            yield chunk
        yield tail

    with loopback_client(app) as c:
        resp = c.post(
            "/api/sessions",
            content=body_stream(),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
    assert resp.status_code == 413
    assert resp.json() == {"error": "简历文件太大，请压缩后重试"}


def test_upload_bad_extension_400(client):
    c, _app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发"},
        files={"resume_file": ("malware.exe", b"MZ...", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "不支持的格式" in resp.json()["error"]
    assert ".docx" in resp.json()["error"]  # 指引另存格式


def test_upload_corrupt_pdf_400(client):
    """坏 PDF → ResumeFileError 人话透传，且临时目录无残留。"""
    c, _app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发"},
        files={"resume_file": ("broken.pdf", b"%PDF-1.4 not really a pdf",
                               "application/pdf")},
    )
    assert resp.status_code == 400
    assert "解析" in resp.json()["error"]


def test_upload_filename_injection_neutralized(client):
    """ 文件名合同：不采用客户端原始名（只取 suffix）——路径注入
    形状的 filename 不落盘到任何它指向的位置。"""
    c, app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发"},
        files={"resume_file": ("..\\..\\evil.txt", "内容".encode("utf-8"),
                               "text/plain")},
    )
    assert resp.status_code == 201
    assert not (Path.cwd() / "evil.txt").exists()
    assert _engine_state(app, resp.json()["session_id"])["candidate_profile"] == "内容"


def test_upload_suffix_case_insensitive(client):
    c, app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发"},
        files={"resume_file": ("resume.TXT", "大小写后缀".encode("utf-8"),
                               "text/plain")},
    )
    assert resp.status_code == 201
    assert _engine_state(app, resp.json()["session_id"])["candidate_profile"] == "大小写后缀"


def test_upload_no_extension_400(client):
    c, _app = client
    resp = c.post(
        "/api/sessions",
        data={"position": "后端开发"},
        files={"resume_file": ("resume", b"no ext")},
    )
    assert resp.status_code == 400
    assert "无扩展名" in resp.json()["error"]


# 状态与合同


def test_degraded_state_create_is_500_human(monkeypatch, tmp_path):
    """降级态（组合根未建）业务端点 → EngineError 500 人话。"""
    def _boom(cfg):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(web_app, "build_checkpointer", _boom)
    app = web_app.create_app(config=offline_web_config(tmp_path))
    with loopback_client(app) as c:
        resp = c.post("/api/sessions", json={"position": "后端开发"})
    assert resp.status_code == 500
    assert resp.json() == {"error": EngineError.default_message}


def test_create_fails_loud_when_no_interrupt(client, monkeypatch):
    """引擎接线回归（图未在 AwaitIntro 挂起）→ 500 fail-loud——Mock 基线
    同款（「会话创建异常：首问缺失」），不下发空开场问。"""
    c, _app = client
    monkeypatch.setattr(web_routes, "pending_interrupt", lambda graph, cfg: None)
    resp = c.post("/api/sessions", json={"position": "后端开发"})
    assert resp.status_code == 500
    assert resp.json() == {"error": EngineError.default_message}


def test_malformed_multipart_400(client):
    c, _app = client
    resp = c.post(
        "/api/sessions",
        content=b"--broken\r\nnot a real multipart",
        headers={"content-type": "multipart/form-data; boundary=broken"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "请求体不是合法的表单"}  # Mock 同款文案（行注）
