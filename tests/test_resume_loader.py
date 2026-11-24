"""简历文件加载单测。fixture 全部测试内自产、完全离线：
txt/md 用 tmp_path 现写（覆盖 utf-8 / 带 BOM / gb18030 三种现实编码）；
docx 用 python-docx 现生成；PDF 用纯 stdlib 手写最小合法 PDF（Helvetica
文本页）——pypdf 只负责读，不为测试引入 PDF 生成库。中文进 PDF 需要
CID 字体子集，超出本测试目的（测的是提取管道不是排版），样本用 ASCII。
空文字页模拟扫描件（有页面、无文字层）。
"""
import pytest

from interviewer.resume.file_loader import (
    _EXTRACTORS,
    ResumeFileError,
    load_resume_file,
)


# fixture 工厂

def _make_text_pdf(text: str) -> bytes:
    """最小合法单页 PDF 1.4（标准 Helvetica，文本画在页首）。

    PDF 是文本格式——对象、xref 偏移按实际字节累加算出，pypdf 严格按 xref 读，
    偏移错一个字节就读不出来，所以不能手敲固定偏移、必须边写边记。
    """
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF".encode()
    return bytes(out)


def _make_docx(path, text: str) -> None:
    from docx import Document

    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(str(path))


# 注册表契约

def test_registry_covers_all_supported_exts():
    """四种格式都有注册且是可调用对象——加/删格式时此测试逼你显式改这里。"""
    assert set(_EXTRACTORS) == {".txt", ".md", ".pdf", ".docx"}
    assert all(callable(f) for f in _EXTRACTORS.values())


# txt/md：编码梯（Windows 现实三连）

def test_txt_utf8_and_bom(tmp_path):
    p = tmp_path / "resume.txt"
    p.write_text("后端工程师，三年 Kafka 经验", encoding="utf-8")
    assert load_resume_file(p) == "后端工程师，三年 Kafka 经验"

    bom = tmp_path / "resume_bom.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + "后端工程师".encode("utf-8"))  # 记事本"UTF-8 带 BOM"档
    assert load_resume_file(bom) == "后端工程师"


def test_txt_gb18030_encoding_ladder(tmp_path):
    """记事本 ANSI（GBK）存档的中文简历是 Windows 现实主流，必须无感读取。"""
    p = tmp_path / "gbk_resume.txt"
    p.write_bytes("全栈工程师：TypeScript、SQL、C#".encode("gb18030"))
    assert load_resume_file(p) == "全栈工程师：TypeScript、SQL、C#"


def test_txt_undecodable_fails_loud(tmp_path):
    """两种编码都解不开的二进制垃圾 → 人话报错，不静默、不乱码。"""
    p = tmp_path / "garbage.txt"
    p.write_bytes(b"\xff\xff\xff\xff")  # 0xFF 在 utf-8 和 gb18030 里都非法
    with pytest.raises(ResumeFileError, match="无法识别"):
        load_resume_file(p)


def test_md_shares_text_path(tmp_path):
    p = tmp_path / "resume.md"
    p.write_text("# 简历\n- Kafka 调优经验", encoding="utf-8")
    assert "Kafka" in load_resume_file(p)


# PDF / docx 提取

def test_pdf_extracts_text(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(_make_text_pdf("ZhangSan Backend Engineer Kafka Redis SQL"))
    text = load_resume_file(p)
    assert "Kafka" in text and "SQL" in text


def test_scanned_style_pdf_reports_human_message(tmp_path):
    """扫描件/图片型 PDF：有页面、无文字层——必须报"扫描件"，不能静默当空简历。"""
    p = tmp_path / "scan.pdf"
    p.write_bytes(_make_text_pdf(""))
    with pytest.raises(ResumeFileError, match="扫描件"):
        load_resume_file(p)


def test_corrupt_pdf_translated_to_human_error(tmp_path):
    """解析库内部异常（损坏文件）必须翻译成人话，不让 traceback 吓用户。"""
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 this is not a real pdf")
    with pytest.raises(ResumeFileError, match="解析 .pdf"):
        load_resume_file(p)


def test_docx_extracts_paragraphs(tmp_path):
    p = tmp_path / "resume.docx"
    _make_docx(p, "全栈工程师\n熟悉 TypeScript 与 SQL")
    text = load_resume_file(p)
    assert "TypeScript" in text and "SQL" in text


# fail-loud 家族

def test_missing_file(tmp_path):
    with pytest.raises(ResumeFileError, match="不存在"):
        load_resume_file(tmp_path / "nope.txt")


def test_directory_rejected(tmp_path):
    with pytest.raises(ResumeFileError, match="不是文件"):
        load_resume_file(tmp_path)


def test_unsupported_ext(tmp_path):
    p = tmp_path / "old.doc"
    p.write_bytes(b"Word binary whatever")
    with pytest.raises(ResumeFileError, match="不支持"):
        load_resume_file(p)


def test_empty_file(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n  ", encoding="utf-8")
    with pytest.raises(ResumeFileError, match="没有内容"):
        load_resume_file(p)


def test_quotes_and_tilde_stripped(tmp_path):
    """Windows 拖拽进终端的路径带引号——loader 要剥掉。"""
    p = tmp_path / "my resume.txt"
    p.write_text("Kafka 工程师", encoding="utf-8")
    assert load_resume_file(f'"{p}"') == "Kafka 工程师"


# 超长截断（fail-visible：留标记，不静默砍）

def test_overlong_resume_truncated_with_marker(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("字" * 500, encoding="utf-8")
    text = load_resume_file(p, max_chars=100)
    assert text.startswith("字" * 100)
    assert "已截断" in text


# CLI 输入环（transport 层）

def _fake_console():
    from interviewer.cli.app import _PlainConsole

    return _PlainConsole()


def test_cli_loop_reprompts_until_valid(tmp_path, monkeypatch, capsys):
    """坏路径 → 红字原因 + 重提；好路径 → 读入返回。绝不静默跳过。"""
    from interviewer.cli.app import _load_resume_input

    good = tmp_path / "r.txt"
    good.write_text("Kafka 工程师", encoding="utf-8")
    replies = iter([str(tmp_path / "nope.pdf"), f'"{good}"'])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))

    text = _load_resume_input(_fake_console(), {"resume_max_chars": 100})

    assert text == "Kafka 工程师"
    assert "不存在" in capsys.readouterr().out


def test_cli_loop_empty_input_skips(monkeypatch):
    """回车 = 用户主动选择无简历（fail-open 由用户按出来，不是系统替他选）。"""
    from interviewer.cli.app import _load_resume_input

    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert _load_resume_input(_fake_console(), {"resume_max_chars": 100}) == ""
