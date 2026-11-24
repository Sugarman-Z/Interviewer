"""简历文件加载：把磁盘文件变成 text（上传文件只是 text 的另一个来源，
图/parser/prompt 零改动）。三条纪律：

① 注册表分发：_EXTRACTORS 按扩展名分发，加格式 = 注册表加一行 + 一个函数；
② fail-loud：提供了文件而失败必抛 ResumeFileError（人话 message）由 CLI 重
   提示——fail-open 的前提是"没提供"，不是"提供了但搞砸了"（否则用户以为
   AI 读了简历）；
③ Windows 编码现实：txt 解码梯 utf-8-sig → gb18030（记事本 GBK 存档常见），
   绝不信 locale 默认编码——那会把行为绑在运行机器上。
"""
from pathlib import Path

# 截断上限：正常简历 1-2 页 ≈ 3000 字内，给足余量防 50 页 PDF 撑爆 LLM 上下文。
# default_config.resume_max_chars 引用此常量（单一事实源）。
DEFAULT_MAX_CHARS = 8000


class ResumeFileError(Exception):
    """简历文件加载失败。message 面向终端用户，CLI 直接展示、不做二次翻译。"""


def _read_text(p: Path) -> str:
    """txt/md 共用：显式解码梯（utf-8-sig 兼容记事本带 BOM 的存档）。"""
    data = p.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ResumeFileError("无法识别文本编码（不是 UTF-8 也不是 GBK 系）——请用 UTF-8 重新保存")


def _extract_pdf(p: Path) -> str:
    """PDF 文字层提取。扫描件/图片型 PDF 提不出文字 → 由上层空内容守卫报"可能是扫描件"。"""
    from pypdf import PdfReader  # 惰性导入：依赖只在对应格式真正用到时加载

    reader = PdfReader(str(p))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(p: Path) -> str:
    """docx 段落拼接（表格内文字 v0 不取——简历正文基本都在段落里）。"""
    from docx import Document  # 惰性导入，同上

    return "\n".join(para.text for para in Document(str(p)).paragraphs)


# 扩展名 → 提取器注册表。.doc（老 Word 二进制）明确不支持：报错引导另存，
# 不引入重型解析依赖（本地优先的边界判断）。
_EXTRACTORS = {
    ".txt": _read_text,
    ".md": _read_text,
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
}
SUPPORTED_EXTS = "/".join(_EXTRACTORS)  # 给提示语用，跟注册表永不脱钩


def load_resume_file(path: str | Path, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """入口：路径 → 提取后的简历文本。一切失败抛 ResumeFileError（人话 message）。

    成功保证：返回值非空、已 strip、不超过 max_chars（超长截断并留显式标记，不静默砍）。
    """
    # strip 引号：Windows 拖拽文件进终端会得到带引号的路径
    p = Path(str(path).strip().strip('"')).expanduser()
    if not p.exists():
        raise ResumeFileError(f"文件不存在：{p}（检查路径，或直接回车跳过简历）")
    if not p.is_file():
        raise ResumeFileError(f"不是文件（是目录？）：{p}")

    extractor = _EXTRACTORS.get(p.suffix.lower())
    if extractor is None:
        raise ResumeFileError(
            f"不支持的格式 {p.suffix or '(无扩展名)'}，支持 {SUPPORTED_EXTS}；.doc 请另存为 .docx"
        )

    try:
        text = extractor(p).strip()
    except ResumeFileError:
        raise  # 自己抛的人话错误直接透传，不再包一层
    except Exception as exc:  # 解析库内部异常（损坏文件等）统一翻译成人话
        raise ResumeFileError(
            f"解析 {p.suffix} 文件失败（{type(exc).__name__}）——文件可能已损坏"
        ) from exc

    if not text:
        if p.suffix.lower() == ".pdf":
            # 简历上传的头号翻车现场：扫描件 PDF 有页面、无文字层
            raise ResumeFileError("PDF 里没提取到文字：很可能是扫描件/图片型 PDF——请换文字版或另存 .txt")
        raise ResumeFileError("文件里没有内容（空文件）")

    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[简历超过 {max_chars} 字，已截断保留前文]"
    return text
