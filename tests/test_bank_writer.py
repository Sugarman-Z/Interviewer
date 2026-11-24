"""写出层测试：render → parse roundtrip + append 不动既有字节。
roundtrip 是写出层的存在性证明——格式契约若与 parser 脱钩（改一侧漏一侧），
这里当场红；与 test_bank_corpus 的解析侧测试互为对账。
"""
from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.bank_writer import (
    append_blocks,
    render_block,
    render_file,
    write_file,
)
from interviewer.dataflows.local_corpus import parse_corpus


def _sample(num: int) -> BankQuestion:
    return BankQuestion(
        id=f"bank-w-{num:03d}",
        topic=f"写出层样题 {num}",
        difficulty=3,
        tags=["测试", "写出层"],
        text=f"第 {num} 题：场景式题面，直接对候选人说话，含中文标点与 English 名词。",
        followup_tree=["追问一：细节在哪？", "追问二：边界情况呢？", "追问三：反例是什么？"],
        scoring_anchors=["5 分：机制联动说得清", "3 分：概念对但模糊", "1 分：只答表面"],
        source="测试·2026-11-16",
    )


def test_render_file_roundtrip(tmp_path):
    """render_file → parse_corpus → 八字段全等（module 从目录推导）。"""
    questions = [_sample(1), _sample(2)]
    f = tmp_path / "project" / "w.md"
    f.parent.mkdir()
    f.write_text(render_file("写出层样题", questions), encoding="utf-8")

    parsed = parse_corpus(tmp_path)
    assert len(parsed) == 2
    for orig, got in zip(questions, parsed):
        assert got.id == orig.id
        assert got.topic == orig.topic
        assert got.difficulty == orig.difficulty
        assert got.tags == orig.tags
        assert got.text == orig.text
        assert got.followup_tree == orig.followup_tree
        assert got.scoring_anchors == orig.scoring_anchors
        assert got.source == orig.source
        assert got.module == "project"


def test_append_blocks_preserves_existing_bytes(tmp_path):
    """追加不动既有字节（人审过的历史文件不重排）；新块在尾、可解析。"""
    f = tmp_path / "project" / "w.md"
    f.parent.mkdir()
    f.write_text(render_file("标题", [_sample(1)]), encoding="utf-8")
    before = f.read_text(encoding="utf-8")

    append_blocks(f, [_sample(2), _sample(3)])

    after = f.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))  # 既有内容只允许尾部换行差异
    assert [q.id for q in parse_corpus(tmp_path)] == [
        "bank-w-001", "bank-w-002", "bank-w-003",
    ]


def test_render_block_carries_no_separator():
    """块文本自身不含 `---`——分隔符由文件层统一拼装（混进块内会被 parser 切碎）。"""
    assert not render_block(_sample(1)).startswith("---")
    assert "\n---" not in render_block(_sample(1))


# 换行契约（2026-11-17 全量审查 high① 回归锁）

def test_append_blocks_no_crlf_flip(tmp_path):
    """Windows 换行翻转回归锁：旧实现 read_text+write_text 在 Windows 把整个
    LF 文件翻写成 CRLF（"不动既有字节"合同机械性失效——正式库 7 文件全 LF，
    下一次 promote --all 就会整批翻写）。字节级断言：零 CR、既有字节原样。"""
    f = tmp_path / "project" / "w.md"
    f.parent.mkdir()
    original = render_file("标题", [_sample(1)]).encode("utf-8")
    f.write_bytes(original)                     # 直接写字节：测试自身先杜绝翻写

    append_blocks(f, [_sample(2)])

    raw = f.read_bytes()
    assert b"\r" not in raw                     # 无换行翻译
    assert raw.startswith(original.rstrip(b"\n"))  # 既有字节原样（只动 EOF 空行）


def test_write_file_writes_lf(tmp_path):
    """新建文件走统一写出面（显式 LF）：staging 曾因裸 write_text 整批复成
    CRLF——gen/promote 的磁盘写出从此只有这一个入口。"""
    f = tmp_path / "project" / "w.md"
    f.parent.mkdir()
    write_file(f, "标题", [_sample(1)])
    assert b"\r" not in f.read_bytes()
