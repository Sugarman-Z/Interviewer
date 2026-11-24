"""人审入库：corpus/_staging/*.md → corpus/project/*.md。

人审合同：人审 = 直接编辑 staging 文件（删块/改措辞随意）；跑本脚本时，
staging 里剩下的全部块视为「人审通过」。

用法：
    PYTHONUTF8=1 python scripts/promote_staging.py agent-rag-与检索.md [更多文件]
    PYTHONUTF8=1 python scripts/promote_staging.py --all     # _staging 顶层全部 md

入库动作（每文件）：解析 staging 块（借 temp 目录走公开 parse_corpus）→
与正式库 + 本次批次交叉查 id/topic 撞车（撞车即该文件整体中止——id 是
防幻觉对账锚、topic 是查重锚，必须在人审当场解决）→ source 强制改写
「LLM 草拟·人审通过·YYYY-MM-DD」→ 追加进 corpus/project/<同名文件>
（append_blocks 不动既有字节；目标不存在则新建带文件头）→ staging 原件
移入 _staging/_promoted/<日期>-<文件>。

收尾自检：全库重解析（重复 id 当场炸）+ 提示重跑 gen_rag_goldset.py
（金标随题库再生，不再生则 test_rag_regression 必红）+ pytest 回归。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.bank_writer import append_blocks, write_file
from interviewer.dataflows.local_corpus import parse_corpus

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"
PROJECT_DIR = CORPUS_DIR / "project"
STAGING_DIR = CORPUS_DIR / "_staging"
_TITLE_LINE = re.compile(r"^# 主题：(.+)$")

APPROVED_SOURCE_PREFIX = "LLM 草拟·人审通过·"


def _norm(s: str) -> str:
    return "".join(s.split()).casefold()


def _parse_staging_file(path: Path) -> list[BankQuestion]:
    """解析单个 staging 文件：拷进 temp 目录走公开 parse_corpus。

    不 import _blocks_of/_parse_block（私有函数，借道等于把解析器的内部
    形状变成第二个公共合同）；拷贝开销微秒级，换来只用公共接口。
    """
    with tempfile.TemporaryDirectory() as td:
        shutil.copy(path, Path(td) / path.name)
        return parse_corpus(td)


def _title_of(path: Path) -> str:
    """新目标文件的标题：staging 文件头 `# 主题：X` 行，缺省用文件名。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        if (m := _TITLE_LINE.match(line.strip())):
            return m.group(1).strip()
    return path.stem


def promote_file(
    staging_path: Path,
    project_dir: Path,
    *,
    bank_ids: set[str],
    bank_topics: set[str],
    today: str,
    glossary=None,
) -> tuple[str, int]:
    """单文件入库。返回 (状态, 块数)；状态 ok / skipped / aborted。
    project_dir 由调用方传入（测试/异地语料目录同样成立，可独立测试）。
    aborted 时不动任何文件（含不归档 staging）——撞车/未入词表清单已打印
    给人，人审处理后重跑即可。

    glossary（"拒绝入库"硬闸）：传入 Glossary 时，候选题 tag 不在词表 →
    该文件整体中止（与撞车同判据级——tag 是检索/金标/synonym 桶的公共锚，
    未注册的 tag 会让词表悄悄腐化）。None = 跳过检查；main 恒传真词表。
    """
    if not staging_path.is_file():
        return f"skipped（文件不存在：{staging_path.name}）", 0

    questions = _parse_staging_file(staging_path)
    if not questions:
        # 人审后一块不剩：文件本身归档，保持工作区干净
        _archive(staging_path, today)
        return "skipped（人审后 0 块，原件已归档）", 0

    collisions = [q.id for q in questions if q.id in bank_ids]
    collisions += [f"{q.id}({q.topic})" for q in questions if _norm(q.topic) in bank_topics]
    if collisions:
        print(f"  ✗ {staging_path.name} 中止——与正式库/本次已入库批次撞车：{collisions}")
        return "aborted", len(questions)

    if glossary is not None:
        unknown = glossary.unknown_tags([t for q in questions for t in q.tags])
        if unknown:
            print(f"  ✗ {staging_path.name} 中止——tag 未入词表（词表因此不腐化，"
                  f"先在 data/glossary.yaml 注册再入库）：{unknown}")
            return "aborted", len(questions)

    for q in questions:
        q.source = f"{APPROVED_SOURCE_PREFIX}{today}"

    target = project_dir / staging_path.name
    if target.exists():
        append_blocks(target, questions)
    else:
        write_file(target, _title_of(staging_path), questions)

    # 本次入库的 id/topic 立刻记账（同批多文件互查）
    bank_ids.update(q.id for q in questions)
    bank_topics.update(_norm(q.topic) for q in questions)
    _archive(staging_path, today)
    return "ok", len(questions)


def _archive(staging_path: Path, today: str) -> None:
    # 归档目录跟着 staging 文件走（staging_path.parent 下的 _promoted），
    # 不引用模块级常量——测试/异地语料目录同样成立
    archive_dir = staging_path.parent / "_promoted"
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging_path), archive_dir / f"{today}-{staging_path.name}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="人审后入库：_staging → corpus/project")
    ap.add_argument("files", nargs="*", help="staging 文件名（如 agent-rag-与检索.md）")
    ap.add_argument("--all", action="store_true", help="处理 _staging 顶层全部 md")
    args = ap.parse_args(argv)

    if args.all:
        names = sorted(p.name for p in STAGING_DIR.glob("*.md"))  # 顶层：_promoted 子目录天然不在
    elif args.files:
        names = args.files
    else:
        ap.error("指定文件名或 --all")
        return 2

    if not names:
        print("staging 里没有待入库文件", file=sys.stderr)
        return 1

    bank = parse_corpus(CORPUS_DIR)
    bank_ids = {q.id for q in bank}
    bank_topics = {_norm(q.topic) for q in bank}
    today = date.today().isoformat()

    # "tag 不在词表拒绝入库"硬闸：词表是真库的 tag 基准（lint 第 8 节
    # 是它的 report-only 前置）。坏表 fail-loud——词表半载比缺席更糟。
    from interviewer.dataflows.glossary import load_glossary
    glossary = load_glossary(REPO_ROOT / "data" / "glossary.yaml")

    print(f"入库前题库 {len(bank)} 题；待处理 {len(names)} 个文件")
    ok_count = promoted_total = 0
    for name in names:
        status, n = promote_file(
            STAGING_DIR / name, PROJECT_DIR,
            bank_ids=bank_ids, bank_topics=bank_topics, today=today,
            glossary=glossary,
        )
        print(f"  {status:<10} {name}（{n} 块）")
        ok_count += status == "ok"
        promoted_total += n if status == "ok" else 0

    if promoted_total:
        after = parse_corpus(CORPUS_DIR)  # 收尾自检：重复 id/坏块在此当场炸
        print(f"\n入库 {promoted_total} 块（{ok_count} 个文件），题库 {len(bank)} → {len(after)} 题")
        print("后续两步（不做则回归锁必红）：")
        print("  1. PYTHONUTF8=1 python scripts/gen_rag_goldset.py   # 金标随题库再生")
        print("  2. pytest tests -q                                   # 全量回归")
    else:
        print("\n没有块入库（全部 skipped/aborted？）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
