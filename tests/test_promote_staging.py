"""入库脚本测试（离线）：scripts/ 不是包，importlib 按路径装载。
锁四件事：人审合同（source 强制改写「LLM 草拟·人审通过·日期」）；撞车
fail-loud（id/topic 撞正式库 → 该文件整体中止、一个字不写、staging 不
归档）；追加式入库（同名既有文件字节不动，新文件继承 staging 文件头）；
归档可追溯（原件进 _promoted/<日期>-<文件名>，staging 顶层清空）。
"""
import importlib.util
from pathlib import Path

from interviewer.dataflows.local_corpus import parse_corpus

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


promote = _load("promote_staging")

_STAGING_BLOCK = """# 主题：staging 样题

> 文件头（解析器跳过）。

---
id: bank-s-001
topic: staging 考点
difficulty: 3
tags: 测试
source: LLM 草拟·2026-11-16

**题目**
staging 的场景式题面，直接问候选人？

**追问树**
- 追问一？
- 追问二？
- 追问三？

**评分锚点**
- 5 分：清
- 3 分：中
- 1 分：糊
"""


def _make_bank(root: Path) -> Path:
    (root / "project").mkdir(parents=True)
    (root / "project" / "existing.md").write_text(
        "---\nid: bank-e-001\ntopic: 既有考点\n\n**题目**\n既有题面\n", encoding="utf-8"
    )
    return root / "project"


def _make_staging(root: Path, name: str, text: str = _STAGING_BLOCK) -> Path:
    staging = root / "_staging"
    staging.mkdir(exist_ok=True)
    p = staging / name
    p.write_text(text, encoding="utf-8")
    return p


def test_promote_appends_and_rewrites_source(tmp_path):
    """同名文件 → 追加（既有块字节不动）；source 改写人审口径；原件归档。"""
    project = _make_bank(tmp_path)
    existing_before = (project / "existing.md").read_text(encoding="utf-8")
    _make_staging(tmp_path, "existing.md")

    status, n = promote.promote_file(
        tmp_path / "_staging" / "existing.md", project,
        bank_ids={"bank-e-001"}, bank_topics={"既有考点"}, today="2026-11-16",
    )
    assert (status, n) == ("ok", 1)

    qs = parse_corpus(project)
    assert [q.id for q in qs] == ["bank-e-001", "bank-s-001"]
    assert qs[1].source == "LLM 草拟·人审通过·2026-11-16"
    assert (project / "existing.md").read_text(encoding="utf-8").startswith(existing_before)
    assert not (tmp_path / "_staging" / "existing.md").exists()
    assert (tmp_path / "_staging" / "_promoted" / "2026-11-16-existing.md").exists()


def test_promote_new_file_inherits_header(tmp_path):
    """目标不存在 → 新建并继承 staging 文件头（# 主题：行）。"""
    project = _make_bank(tmp_path)
    _make_staging(tmp_path, "fresh.md")

    status, _ = promote.promote_file(
        tmp_path / "_staging" / "fresh.md", project,
        bank_ids=set(), bank_topics=set(), today="2026-11-16",
    )
    assert status == "ok"
    assert (project / "fresh.md").read_text(encoding="utf-8").startswith("# 主题：staging 样题")
    assert [q.id for q in parse_corpus(project)] == ["bank-e-001", "bank-s-001"]


def test_promote_aborts_on_collision_nothing_written(tmp_path):
    """id 撞正式库 → 整文件中止：目标不写、staging 不动（等人审当场改名/删块）。"""
    project = _make_bank(tmp_path)
    _make_staging(tmp_path, "clash.md", _STAGING_BLOCK.replace("bank-s-001", "bank-e-001"))

    status, n = promote.promote_file(
        tmp_path / "_staging" / "clash.md", project,
        bank_ids={"bank-e-001"}, bank_topics={"既有考点"}, today="2026-11-16",
    )
    assert status == "aborted"
    assert not (project / "clash.md").exists()
    assert (tmp_path / "_staging" / "clash.md").exists()
    assert [q.id for q in parse_corpus(project)] == ["bank-e-001"]


def test_promote_topic_collision_also_aborts(tmp_path):
    """归一化 topic 撞车同样中止（topic 是签发查重锚，撞车=歧义）。"""
    project = _make_bank(tmp_path)
    _make_staging(tmp_path, "clash2.md", _STAGING_BLOCK.replace("staging 考点", "既有考点"))

    status, _ = promote.promote_file(
        tmp_path / "_staging" / "clash2.md", project,
        bank_ids={"bank-e-001"}, bank_topics={"既有考点"}, today="2026-11-16",
    )
    assert status == "aborted"
    assert not (project / "clash2.md").exists()


def test_promote_empty_after_review_archives_only(tmp_path):
    """人审后一块不剩：不写任何题，空文件归档，工作区保持干净。"""
    _make_bank(tmp_path)
    _make_staging(tmp_path, "rejected.md", "# 主题：全删\n\n> 只剩文件头\n")

    status, n = promote.promote_file(
        tmp_path / "_staging" / "rejected.md", tmp_path / "project",
        bank_ids=set(), bank_topics=set(), today="2026-11-16",
    )
    assert (status, n) == ("skipped（人审后 0 块，原件已归档）", 0)
    assert not (tmp_path / "_staging" / "rejected.md").exists()


def test_promote_unknown_tag_aborts(tmp_path):
    """ 词表硬闸：候选题 tag 不在词表 → 整文件中止不入库（词表因此
    不腐化——tag 是检索/金标/synonym 桶的公共锚）。glossary=None 跳过检查
（旧 fixture 零回归），撞车优先级在词表之前。"""
    from interviewer.dataflows.glossary import Glossary

    project = _make_bank(tmp_path)
    _make_staging(tmp_path, "newtag.md")

    # 空词表 = 一切 tag 都未知（fixture 的 tags 是「测试」）
    gl = Glossary()
    status, n = promote.promote_file(
        tmp_path / "_staging" / "newtag.md", project,
        bank_ids={"bank-e-001"}, bank_topics={"既有考点"}, today="2026-11-17",
        glossary=gl,
    )
    assert (status, n) == ("aborted", 1)
    assert not (project / "newtag.md").exists()
    assert (tmp_path / "_staging" / "newtag.md").exists()   # 归档也不发生

    # None = 跳过检查（老行为：入库成功）
    status2, _ = promote.promote_file(
        tmp_path / "_staging" / "newtag.md", project,
        bank_ids={"bank-e-001"}, bank_topics={"既有考点"}, today="2026-11-17",
    )
    assert status2 == "ok"
