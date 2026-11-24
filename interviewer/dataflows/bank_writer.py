"""题库 md 写出层：BankQuestion → 合法题块文本。

与 local_corpus._parse_block 互为逆操作——roundtrip 由 test_bank_writer
锁死；格式契约改任何一处两侧必须同批改。消费方：gen_bank_candidates /
promote_staging / 一次性批量落地。

换行契约：corpus 正典是 LF，Windows 的 write_text 默认翻 CRLF——磁盘写出
必须走本模块的 write_file / append_blocks（显式 LF / 字节级追加），禁止
别处裸 write_text 写题库文件。
"""
from __future__ import annotations

from pathlib import Path

from interviewer.dataflows.bank_schema import BankQuestion

# 文件头第二行的人读注释（与 kafka-高可用.md 样板同款；解析器跳过整个文件头）
_FILE_HEADER_NOTE = (
    "> 解析契约见 dataflows/local_corpus.py：`---` 分块，块头 key: value"
    "（ASCII 冒号），块身按 **小节名** 切分。本行以上的文件头解析器跳过。"
)


def render_block(q: BankQuestion) -> str:
    """单题 → 块文本（不含前导 `---` 分隔符——分隔符由整文件拼装层负责）。"""
    lines = [
        f"id: {q.id}",
        f"topic: {q.topic}",
        f"difficulty: {q.difficulty}",
        f"tags: {', '.join(q.tags)}",
        f"source: {q.source}",
        "",
        "**题目**",
        q.text,
        "",
        "**追问树**",
        *[f"- {h}" for h in q.followup_tree],
        "",
        "**评分锚点**",
        *[f"- {a}" for a in q.scoring_anchors],
    ]
    return "\n".join(lines)


def render_file(title: str, questions: list[BankQuestion]) -> str:
    """整文件：人读文件头 + `---` 引导的首块 + `\n\n---\n` 连接的后续块。
    块间分隔符与 corpus/project 既有文件逐字节同款，追加后无格式跳变。"""
    header = f"# 主题：{title}\n\n{_FILE_HEADER_NOTE}\n\n"
    blocks = "\n\n---\n".join(render_block(q) for q in questions)
    return f"{header}---\n{blocks}\n"


def write_file(path: Path, title: str, questions: list[BankQuestion]) -> None:
    """新建题库文件（gen 写 staging / promote 建新目标的统一写出面）。
    显式 newline 关掉 Windows 的换行翻译——裸 write_text 会整批复成
    CRLF，与正式库 LF 分裂。"""
    path.write_text(render_file(title, questions), encoding="utf-8", newline="\n")


def append_blocks(path: Path, questions: list[BankQuestion]) -> None:
    """把块追加进既有文件（不动既有字节，人审过的历史文件不重排，git diff
    只显示新增块）。字节级 read_bytes/write_bytes 绕开文本模式的换行翻译
（文本模式在 Windows 会把整个 LF 文件翻写成 CRLF）；只有 EOF 尾部空行
    归一成追加分隔所需的一个空行。
    """
    if not questions:
        return
    text = path.read_bytes().decode("utf-8")
    blocks = "\n\n---\n".join(render_block(q) for q in questions)
    path.write_bytes(
        (text.rstrip("\r\n") + "\n\n---\n" + blocks + "\n").encode("utf-8")
    )
