"""题库 lint：跨题检查，report-only。

用法：PYTHONUTF8=1 python scripts/lint_bank.py [--dir corpus/_staging]
（候选区模式自动与正式库交叉查 id/topic 撞车）

检查清单：
    1. id 规范：bank-<域>-<序号> 命名、同域序号断档（info）
    2. topic 重复：精确 + 归一化（去空白 casefold）——topic 是签发查重与
       verbatim 金标的锚，重复即歧义（⚠）
    3. text 精确重复：归一化后相同（⚠；近重复等稠密腿）
    4. tag 卫生：casefold 后同概念多拼写（⚠）、孤儿 tag 全库仅一现（info）、
       每题 tag 数 0 或 >4（⚠）
    5. 覆盖矩阵：域×难度计数表，空洞标 ·——出题需求信号（人读，不判级）
    6. source 格式：空（⚠）、无日期（info）
    7. 弱题信号：追问树/评分锚点不是 3 条（⚠）、锚点无 5/3/1 分层开头（info）
    8. tag ↔ 词表对账：tag 不在 data/glossary.yaml（⚠——promote 侧有同判据
       的硬闸，这里先报告给出修复位置）

report-only 哲学：只打印报告，退出码恒 0——规则太严会让手写题库处处飘红
被弃用。唯一非零退出 = 题库本身 parse 炸（烂块 fail-loud 是 local_corpus
的职责，lint 不吞也不替它兜）。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from interviewer.dataflows.local_corpus import parse_corpus

REPO_ROOT = Path(__file__).resolve().parent.parent
ID_PATTERN = re.compile(r"^bank-([a-z]+)-(\d{3})$")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _norm(s: str) -> str:
    """归一化比较键：去全部空白 + casefold——「高可用」与「高可用 」、
    「Kafka」与「kafka」在此相等；语义近重复（近义词）刻意不做，v1 只抓拼写面。"""
    return "".join(s.split()).casefold()


def lint(questions: list, cross: list | None = None, glossary=None) -> list[str]:
    """跑全部检查，返回报告行（含分节空行）。永不 raise、永不改数据。
    glossary：传入 Glossary 时启用第 8 节 tag↔词表对账——
    None 跳过该节（测试 fixture 语料不必配真词表）。"""
    out: list[str] = []

    def section(title: str) -> None:
        out.extend(["", f"── {title} ──"])

    families = sorted({m.group(1) for q in questions if (m := ID_PATTERN.match(q.id))})

    # 总览
    section(f"总览：{len(questions)} 题")
    by_family = Counter(ID_PATTERN.match(q.id).group(1) if ID_PATTERN.match(q.id) else "?" for q in questions)
    out.append("  " + "，".join(f"{f} {c}" for f, c in sorted(by_family.items())))

    # 1. id 规范
    section("id 规范")
    bad_ids = [q.id for q in questions if not ID_PATTERN.match(q.id)]
    if bad_ids:
        out.append(f"  ⚠ 不符 bank-<域>-<序号> 命名：{', '.join(bad_ids)}")
    # 序号基数：交叉模式下并入正式库已用序号——staging 只含新题，
    # 若只看自身，已入库的 1-20 会整段报成"断档"假噪音
    used_nums: dict[str, set[int]] = defaultdict(set)
    for q in questions:
        if (m := ID_PATTERN.match(q.id)):
            used_nums[m.group(1)].add(int(m.group(2)))
    for q in cross or []:
        if (m := ID_PATTERN.match(q.id)):
            used_nums[m.group(1)].add(int(m.group(2)))
    for fam in families:
        nums = sorted(used_nums[fam])
        gaps = [n for n in range(1, (nums[-1] if nums else 0) + 1) if n not in set(nums)]
        if gaps:
            out.append(f"  · {fam} 序号断档（历史事实，非错误）：{gaps}")

    # 2. topic 重复
    section("topic 重复（签发查重/verbatim 金标的锚，重复即歧义）")
    topic_map: dict[str, list[str]] = defaultdict(list)
    for q in questions:
        topic_map[_norm(q.topic)].append(f"{q.id}({q.topic})")
    dup = [(k, ids) for k, ids in topic_map.items() if len(ids) > 1]
    for _, ids in sorted(dup):
        out.append(f"  ⚠ topic 撞车：{' vs '.join(ids)}")
    if not dup:
        out.append("  ✓ 无重复")

    # 3. text 精确重复
    section("text 重复（归一化精确匹配；近重复等 C5 稠密腿）")
    text_map: dict[str, list[str]] = defaultdict(list)
    for q in questions:
        text_map[_norm(q.text)].append(q.id)
    dup_text = [(k, ids) for k, ids in text_map.items() if len(ids) > 1]
    for _, ids in sorted(dup_text):
        out.append(f"  ⚠ 题面重复：{' vs '.join(ids)}")
    if not dup_text:
        out.append("  ✓ 无重复")

    # 4. tag 卫生
    section("tag 卫生")
    for q in questions:
        if not (1 <= len(q.tags) <= 4):
            out.append(f"  ⚠ {q.id} tag 数 {len(q.tags)}（约定 1~4 个）：{q.tags}")
    spell: dict[str, set[str]] = defaultdict(set)
    for q in questions:
        for t in q.tags:
            spell[_norm(t)].add(t)
    for key, variants in sorted(spell.items()):
        if len(variants) > 1:
            out.append(f"  ⚠ 同概念多拼写，建议归一为一：{sorted(variants)}")
    # 孤儿判定同样并入正式库频次：「索引/缓存」这类正式库常用 tag
    # 在 staging 只出现一次是正常分工，不该报成孤儿候选
    freq = Counter(t for q in questions for t in q.tags)
    for q in cross or []:
        freq.update(q.tags)
    orphans = sorted(t for t, c in freq.items() if c == 1)
    if orphans:
        out.append(f"  · 孤儿 tag（全库仅一现，笔误或该并入近义 tag 的候选）：{orphans}")

    # 5. 覆盖矩阵（域×难度；空洞=出题需求信号）
    section("覆盖矩阵：域 × 难度（· 为空洞——出题需求信号）")
    matrix: dict[str, Counter] = defaultdict(Counter)
    for q in questions:
        fam = ID_PATTERN.match(q.id).group(1) if ID_PATTERN.match(q.id) else "?"
        matrix[fam][q.difficulty] += 1
    header = "  " + " ".join(f"d{d:<4}" for d in range(1, 6))
    out.append(header)
    for fam in sorted(matrix):
        cells = " ".join(f"{matrix[fam].get(d, '·'):<5}" for d in range(1, 6))
        out.append(f"  {fam:<8}{cells}")
    hist = Counter(q.difficulty for q in questions)
    out.append("  全局：" + "，".join(f"d{k}×{hist[k]}" for k in sorted(hist)))

    # 6. source 格式
    section("source 格式")
    no_src = [q.id for q in questions if not q.source.strip()]
    if no_src:
        out.append(f"  ⚠ source 为空：{no_src}")
    no_date = [q.id for q in questions if q.source.strip() and not DATE_PATTERN.search(q.source)]
    if no_date:
        out.append(f"  · source 无日期（人读溯源弱化，建议补）：{no_date}")
    if not no_src and not no_date:
        out.append("  ✓ 全部有日期")

    # 7. 弱题信号
    section("弱题信号（追问树/锚点结构）")
    weak = False
    for q in questions:
        if len(q.followup_tree) != 3:
            out.append(f"  ⚠ {q.id} 追问树 {len(q.followup_tree)} 条（约定 3）")
            weak = True
        if len(q.scoring_anchors) != 3:
            out.append(f"  ⚠ {q.id} 评分锚点 {len(q.scoring_anchors)} 条（约定 3）")
            weak = True
        elif not (q.scoring_anchors[0].startswith("5 分") and q.scoring_anchors[-1].startswith("1 分")):
            out.append(f"  · {q.id} 锚点无 5/3/1 分层开头：{q.scoring_anchors[0][:12]}…")
            weak = True
    if not weak:
        out.append("  ✓ 结构完整")

    # 交叉检查（staging vs 正式库）
    if cross:
        section(f"交叉检查：本区 vs 正式库（{len(cross)} 题）")
        bank_ids = {q.id for q in cross}
        bank_topics = {_norm(q.topic) for q in cross}
        hit = False
        for q in questions:
            if q.id in bank_ids:
                out.append(f"  ⚠ id 与正式库撞车（promote 会中止）：{q.id}")
                hit = True
            if _norm(q.topic) in bank_topics:
                out.append(f"  ⚠ topic 与正式库撞车：{q.id}({q.topic})")
                hit = True
        if not hit:
            out.append("  ✓ 无撞车")

            # 8. tag ↔ 词表对账
    # glossary 是可选参数：测试传 None 跳过本节（fixture 语料不必配真词表）；
    # main 恒传真词表（缺席会 fail-loud——词表现在是对账基准，不是增强件）。
    if glossary is not None:
        section("tag ↔ 词表对账（未知 tag：promote 会拒绝入库）")
        all_tags = sorted({t for q in questions for t in q.tags})
        unknown = glossary.unknown_tags(all_tags)
        if unknown:
            for t in unknown:
                out.append(f"  ⚠ tag 未入词表：{t}（先在 data/glossary.yaml 注册）")
        else:
            out.append(f"  ✓ {len(all_tags)} 个 tag 全部在表")

    warn_total = sum(1 for ln in out if "⚠" in ln)
    out.extend(["", f"合计：{warn_total} 条 ⚠（report-only，不阻塞任何流程）"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="题库 lint（report-only）")
    ap.add_argument("--dir", default=str(REPO_ROOT / "corpus"),
                    help="要 lint 的目录（默认正式题库；候选区传 corpus/_staging）")
    ap.add_argument("--glossary", default=str(REPO_ROOT / "data" / "glossary.yaml"),
                    help="术语词表路径（传空串跳过 tag 对账节）")
    args = ap.parse_args(argv)

    target = Path(args.dir).resolve()
    questions = parse_corpus(target)
    if not questions:
        print(f"目标目录没有可解析题目：{target}", file=sys.stderr)
        return 1

    glossary = None
    if args.glossary:
        from interviewer.dataflows.glossary import load_glossary
        glossary = load_glossary(args.glossary)   # 坏表 fail-loud：对账基准不许半载

    main_corpus = (REPO_ROOT / "corpus").resolve()
    cross = None
    if target != main_corpus and main_corpus.is_dir():
        cross = parse_corpus(main_corpus)  # lint 候选区时与正式库交叉查撞车

    print(f"\nlint：{target}（{len(questions)} 题）")
    print("\n".join(lint(questions, cross=cross, glossary=glossary)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
