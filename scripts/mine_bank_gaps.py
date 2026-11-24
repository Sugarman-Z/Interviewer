"""缺题挖掘飞轮：扫会话留痕出缺题清单 → 出题管道的优先级队列。
逻辑链：IssueQuestion 的检索留痕（retrieval_trace）记录了模型真实发过的
每条 query 与三态结果——state=none 的 query 是模型想考但题库没有的考点，
这正是 gen_bank_candidates 分配表想要的输入。

四态读法（与 bank_tool.run_with_probe 合同对齐）：
- none → 缺题候选：post-hoc 用当前题库 re-probe 区分真缺（优先出题）与
  "老缺口已被扩容补上"（留档）；
- far_gated → 与 none 同进缺口：闸判定"全库无语义近邻"是比"BM25 空"更强
  的真缺信号；re-probe 同用闸。诚实边界：近域相邻缺题（LoRA vs 微调选型）
  闸分不开，仍偏保守；
- excluded（命中的全是已考考点）→ 非缺题：复考需求信号；
- hit / corrupt（record 带 error 键）→ 不进清单（题库供上了 / 工具失败
  ≠ 缺题——坏库会话里每条 query 都失败，进清单等于把整场检索词全当缺题）。

双工具留痕：trace 混着两副工具的记录，tool 键分账；三态聚合只吃题库面
（段落面的空命中是"简历里没有这段"，不是题库缺题；老档无 tool 键按题库
面放行）。transcript 信号（老会话无 trace 时的补充来源）：issue 条目的
fallback_reason 与 issue_done 熔断字样 → 签发质量信号，同源落盘。

输出：控制台报告 + JSON（{query, count, session_ids, still_empty,
suggested_tags}）；suggested_tags = query 命中的词表面 → canonical，直接是
分配表 tags 字段的候选值。用法：PYTHONUTF8=1 python scripts/mine_bank_gaps.py
[--sessions 目录 --out xxx.json]。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from interviewer.dataflows import relevance_gate
from interviewer.dataflows.local_corpus import parse_corpus, search_corpus_local

REPO_ROOT = Path(__file__).resolve().parent.parent

# transcript 侧的质量信号关键词（issue_done 收场原因 / issue 条目 fallback_reason）
_QUALITY_SIGNALS = ("熔断", "兜底池已尽", "编造", "泄漏", "失败")


def load_sessions(sessions_dir: str | Path) -> list[dict]:
    """读全部 session JSON（坏文件跳过并警告——挖掘脚本不许被一份坏档炸停）。"""
    out = []
    for p in sorted(Path(sessions_dir).glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"⚠ 跳过坏档 {p.name}: {exc}", file=sys.stderr)
    return out


def mine(sessions: list[dict], corpus_dir: str, glossary=None,
         gate=None) -> dict:
    """聚合三态 + 老档兼容 + 当前库 re-probe。纯函数（sessions 是已加载的
    dict 列表），测试喂 fixture 即可。gate：re-probe 的"核验"腿（远域整单
    闸），None = 无闸现状语义（BM25 有命中即判已补上）；main 生产入口显式
    传 relevance_gate.should_gate_empty（真闸要动 jina 模型，pytest 注入
    lambda 锁行为）。
    返回形状（JSON 落盘与报告共用）：
        {"gaps": [...], "excluded": [...], "hits": Counter-ish, "quality": [...],
         "sessions_scanned": int, "sessions_with_trace": int, "bank_size": int}
    """
    bank = parse_corpus(corpus_dir)
    gap_counter: Counter[str] = Counter()
    gap_sessions: dict[str, set[str]] = {}
    excluded_counter: Counter[str] = Counter()
    hit_counter: Counter[str] = Counter()
    quality: list[str] = []
    with_trace = 0

    for s in sessions:
        sid = s.get("session_id") or "?"
        trace = s.get("retrieval_trace") or []
        if trace:
            with_trace += 1
        for rec in trace:
            # tool 键分账（structured.py 循环补的）：缺口语义只属于题库面。
            # search_candidate 的空命中 = 简历/JD 里没有这段，进缺口清单会把
            # 简历探针污染成"题库缺题"（2026-11-17 审查 high 项）
            if rec.get("tool") not in (None, "search_corpus"):
                continue
            if rec.get("error"):
                # 工具自身失败（如题库解析失败 record 带 error 键）≠ 题库
                # 缺题：坏库会话里每条 query 都失败，进缺口清单等于把整场
                # 会话的检索词都当缺题（corrupt 按 error 键过滤）
                continue
            query = str(rec.get("query") or "").strip()
            if not query:
                continue
            if rec.get("hit_ids"):
                hit_counter[query] += 1
            elif rec.get("empty_state") == "excluded":
                excluded_counter[query] += 1
            else:
                # state=none（或旧版记录缺 empty_state 但 empty_hit 真）：
                # 空命中且未被查重排除 → 缺题候选
                gap_counter[query] += 1
                gap_sessions.setdefault(query, set()).add(sid)
        # transcript 质量信号（老会话无 trace 时的唯一来源）。两类条目看的
        # 字段不同：issue 的 content 是题面正文（"部分失败"这类词是题目的
        # 一部分，扫它会误报——真数据实测踩过），降级信息只在
        # fallback_reason；issue_done 的 content 才承载收场原因
        for e in s.get("transcript") or []:
            if e.get("kind") == "issue_done":
                text = str(e.get("content") or "")
            elif e.get("kind") == "issue":
                text = str(e.get("fallback_reason") or "")
            else:
                continue
            if any(k in text for k in _QUALITY_SIGNALS):
                line = f"[{sid}] {e.get('kind')}: {text[:80]}"
                if line not in quality:
                    quality.append(line)

    gaps = []
    for query, count in sorted(gap_counter.items(), key=lambda kv: (-kv[1], kv[0])):
        # 已知局限（远域部分已由远域闸修）：BM25 无阈值，
        # 单个公共 bigram 命中（如"通信"撞上 TCP 题）即判"当前库查得到"——
        # 现在多一道核验：闸判远域（全库无语义近邻）→ 仍算真缺。近域相邻
        # 缺题仍偏保守（语义近邻分不开，实验读数③）。
        # test_still_empty_bigram_collision_semantics 把"闸关时的现状"锁成
        # 明示语义；test_reprobe_gate 把"闸开的修复"锁成行为。
        hits = search_corpus_local(query, exclude_topics=(), top_k=5, corpus_dir=corpus_dir)
        gated = bool(gate(query, corpus_dir)) if gate else False
        gaps.append({
            "query": query,
            "count": count,
            "session_ids": sorted(gap_sessions.get(query, set())),
            "still_empty": (not hits) or gated,   # BM25 空 或 闸判远域 = 真缺
            "current_hit_ids": [h.id for h in hits[:3]],
            "suggested_tags": _suggest_tags(query, glossary),
        })
    return {
        "generated_at": None,   # main 落盘时填（纯函数不取当天日期以外的环境）
        "bank_size": len(bank),
        "sessions_scanned": len(sessions),
        "sessions_with_trace": with_trace,
        "gaps": gaps,
        "excluded_top": [{"query": q, "count": c}
                         for q, c in excluded_counter.most_common(20)],
        "hit_top": [{"query": q, "count": c} for q, c in hit_counter.most_common(10)],
        "quality_signals": quality,
    }


def _suggest_tags(query: str, glossary) -> list[str]:
    """query 命中的词表面 → canonical（分配表 tags 字段候选值）。
    词表缺席 → []（增强件降级，不炸挖掘本体）。"""
    if glossary is None:
        return []
    tags = []
    for surface, canonical in glossary.surface_hits(query):
        if canonical not in tags:
            tags.append(canonical)
    return tags[:5]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫会话留痕出缺题清单（供给优先级队列）")
    ap.add_argument("--sessions", default=str(REPO_ROOT / "data" / "sessions"))
    ap.add_argument("--corpus", default=str(REPO_ROOT / "corpus"))
    ap.add_argument("--glossary", default=str(REPO_ROOT / "data" / "glossary.yaml"),
                    help="术语词表（suggested_tags 原料；传空串跳过）")
    ap.add_argument("--out", default=str(REPO_ROOT / "scripts" / "data" / "bank_gaps.json"))
    args = ap.parse_args(argv)

    sessions = load_sessions(args.sessions)
    if not sessions:
        print(f"没有可读会话档：{args.sessions}", file=sys.stderr)
        return 1

    glossary = None
    if args.glossary:
        from interviewer.dataflows.glossary import load_glossary
        glossary = load_glossary(args.glossary)

    result = mine(sessions, args.corpus, glossary,
                  gate=relevance_gate.should_gate_empty)
    result["generated_at"] = date.today().isoformat()

    true_gaps = [g for g in result["gaps"] if g["still_empty"]]
    fixed = len(result["gaps"]) - len(true_gaps)
    print(f"扫 {result['sessions_scanned']} 份会话（{result['sessions_with_trace']} 份带检索留痕；"
          f"老会话只有 transcript 信号）｜当前题库 {result['bank_size']} 题")
    if result["gaps"]:
        print(f"\n空命中 query {len(result['gaps'])} 条：真缺 {len(true_gaps)}（当前库仍查不到）"
              f"｜已被扩容补上 {fixed}（历史信息）")
        for g in true_gaps:
            tags = f"｜建议 tags: {', '.join(g['suggested_tags'])}" if g["suggested_tags"] else ""
            print(f"  {g['count']} 次｜{g['query']}{tags}")
    else:
        print("\n没有空命中留痕（留痕随 C2① 落地——老会话不产缺口数据）")
    if result["quality_signals"]:
        print(f"\n签发质量信号 {len(result['quality_signals'])} 条（熔断/池尽/编造/泄漏）：")
        for line in result["quality_signals"][:10]:
            print(f"  {line}")
    if result["excluded_top"]:
        print("\n被查重排除的高频 query（复考需求信号，Phase D 原料）：")
        for e in result["excluded_top"][:5]:
            print(f"  {e['count']} 次｜{e['query']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n清单已沉淀：{out}（真缺条目即 gen_bank_candidates 分配表原料）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
