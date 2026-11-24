"""泄漏治理实验：打分器档位 × 闸门信号形态的 2×2。
- 打分器：bge-small-zh（现状）vs jina-embeddings-v2-base-zh（fastembed 0.9.1
  内唯一更强的中文模型，base 档替身）。动机：BGE 官方典型阈值带 0.8-0.9，
  我们金标/探针落在 0.389-0.483——金标被裁可能是"最弱档"的伪影。
- 信号形态：MaxScore（s1 绝对阈值，现状）vs Gap@5（s1−s5 分布信号）。
- 探针 40 条（scripts/data/leak_probes.json）：远域 + 近域缺题两层——近域
（技术相邻但题库没有）才是闸门真正的难题。

口径三条：全库 cosine（保守侧：全库 max 都过不了闸，BM25 池里的更过不了）；
gold 保留 = 进 dense top-5 且过闸（AUROC 只拿 rank≤5 的 gold 当正类，排序
天花板单列）；零泄漏工作点 τ = max(探针信号) 的 nextafter（平局不算保留）。
离线无 API；纯函数（rank_auroc / rule_of_three_upper / zero_leak_point /
gate_at / check_probes）不碰 numpy/fastembed，tests/test_leak_gate_eval.py
离线锁。用法：PYTHONUTF8=1 python scripts/eval_leak_gate_2x2.py，
产出 scripts/data/leak_gate_2x2_<date>.json + 终端 2×2 表。
"""
import argparse
import json
import math
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# 与 dense 链 recall@5 / 下游"读整份 top-5"对齐；Gap@5 同深
TOP_K = 5
# 信号 → gold 行键（probe 行键见 _PROBE_KEY）。三种闸门形态：
# - goldcos：逐条闸（gold **自己那条**的 cosine ≥ τ）——dense 链现状与
#   业内标准形态（Azure/Google/LangChain 的 per-hit score filter）的口径；
# - maxscore：整单闸（s1 ≥ τ 放行整个列表）——列表级弃权（UAEval4RAG 型）；
# - gap5：分布信号（s1−s5，Magnitude Mirage 的 within-query 形态）。
# 探针侧 leak 三形态同义：逐条闸下"任何命中过阈" = s1 ≥ τ（全库口径，
# 对 BM25 池保守）。
SIGNAL_KEYS = {"goldcos": "goldcos", "maxscore": "s1", "gap5": "gap5"}
_PROBE_KEY = {"goldcos": "s1", "maxscore": "s1", "gap5": "gap5"}


def rank_auroc(pos: list[float], neg: list[float]) -> float:
    """AUROC（平均秩 Mann-Whitney U）。pos = 应得高分的信号（rank≤5 的 gold），
    neg = 应得低分的信号（探针）。并列取平均秩——闸门数据里并列常态
（cosine 四舍五入后成堆），按并列=0.5 记才是无偏估计。"""
    if not pos or not neg:
        return float("nan")
    vals = sorted(pos + neg)
    # 平均秩：同值的所有位置取均值（1-indexed）
    rank_of: dict[float, float] = {}
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[i]:
            j += 1
        avg = (i + 1 + j + 1) / 2
        rank_of[vals[i]] = avg
        i = j + 1
    u = sum(rank_of[v] for v in pos) - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


def rule_of_three_upper(n: int) -> float:
    """0 事件 n 次试验 → 真实率的 95% 置信上界（Hanley & Lippman-Hand 1983；
    n≥30 时 ≈3/n）。判决的误差棒，不是泄漏率的估计值。"""
    return 1.0 - 0.05 ** (1.0 / n)


def gate_at(tau: float, probe_signals: list[float], gold_rows: list[dict]) -> dict:
    """单一工作点：signal ≥ τ 视为"有货"放行（gold 侧还要 rank≤5 才算保住）。"""
    leaked = [s for s in probe_signals if s >= tau]
    kept = [g for g in gold_rows if g["signal"] >= tau and g["rank"] <= TOP_K]
    by_bucket = Counter(g["bucket"] for g in kept)
    return {
        "tau": round(tau, 4),
        "leaks": len(leaked),
        "leak_rate": round(len(leaked) / len(probe_signals), 4) if probe_signals else None,
        "gold_retained": len(kept),
        "retained_by_bucket": dict(sorted(by_bucket.items())),
    }


def zero_leak_point(probe_signals: list[float], gold_rows: list[dict]) -> dict:
    """零泄漏工作点：τ 刚好越过探针最大值。lost 全量列出（闸门代价的账目，
    哪些桶、多少条、差多少分——决策要看的正是这份账）。"""
    tau = math.nextafter(max(probe_signals), math.inf)
    out = gate_at(tau, probe_signals, gold_rows)
    out["lost"] = sorted(
        ({"query": g["query"], "bucket": g["bucket"], "rank": g["rank"],
          "signal": round(g["signal"], 4)} for g in gold_rows
         if not (g["signal"] >= tau and g["rank"] <= TOP_K)),
        key=lambda r: r["signal"],
    )
    return out


def check_probes(probes: list[dict], questions) -> list[tuple[str, str]]:
    """近域探针缺席核查（parse 同口径）：forbidden_terms 出现在任何题的
    topic/tags/题面/追问树（casefold 子串）= 探针已被题库覆盖 → 违规。
    题库扩容后重跑在此 fail-loud——探针集不许静默过期（"题库真没有"是
    近域探针作为负样本的全部合法性）。"""
    corpus_text = "\n".join(
        "\n".join([q.topic, " ".join(q.tags), q.text, *q.followup_tree])
        for q in questions
    ).casefold()
    violations = []
    for p in probes:
        for term in p.get("forbidden_terms", ()):
            if term.casefold() in corpus_text:
                violations.append((p["query"], term))
    return violations


# 嵌入与逐查询信号（numpy/fastembed 只活在下面，纯函数不依赖）

def _embed_matrix(model_name: str, doc_texts: list[str], query_texts: list[str]):
    """(题向量矩阵, 查询向量矩阵)，单位行——两侧 embed 交给 fastembed 按各
    模型卡处理（指令前缀等库内差异不自己拼，同 FastembedEncoder 纪律）。"""
    import numpy as np
    from fastembed import TextEmbedding

    from interviewer.dataflows.dense_corpus import CACHE_DIR, _unit_rows
    model = TextEmbedding(model_name=model_name, cache_dir=CACHE_DIR)
    doc = _unit_rows(np.asarray(
        [np.asarray(v, dtype=np.float32) for v in model.passage_embed(doc_texts)]))
    qry = _unit_rows(np.asarray(
        [np.asarray(v, dtype=np.float32) for v in model.query_embed(query_texts)]))
    return doc, qry


def _signals_from_sims(sims, order, gold_row=None) -> dict:
    """排序后取信号：s1..s5（全库降序）、maxscore=s1、gap5=s1−s5；
    gold_row 给定时附带其名次（parse 序决胜，同 DenseIndex.search 口径）。"""
    top = [sims[j] for j in order[:TOP_K]]
    out = {
        "s1": float(top[0]) if top else 0.0,
        "gap5": float(top[0] - top[-1]) if top else 0.0,
        "maxscore": float(top[0]) if top else 0.0,
    }
    if gold_row is not None:
        out["goldcos"] = float(sims[gold_row])
        out["rank"] = order.index(gold_row) + 1
    return out


def evaluate_model(model_name: str, questions, gold_items, probes) -> dict:
    """单打分器全量：gold 四桶 + 探针两层的逐查询信号 → 两信号各三个
    工作点（τ=0.50 现状 / 零泄漏 / 排序天花板）+ 分离度（AUROC、区间）。"""
    from interviewer.dataflows.dense_corpus import embed_text

    doc_texts = [embed_text(q) for q in questions]
    gold_queries = [it["query"] for it in gold_items]
    probe_queries = [p["query"] for p in probes]
    # gold+探针查询拼一列一次嵌入（同一查询跑两次前向没有意义）
    doc_m, all_q = _embed_matrix(model_name, doc_texts, gold_queries + probe_queries)
    gold_q, probe_q = all_q[: len(gold_queries)], all_q[len(gold_queries):]

    row_of = {q.id: i for i, q in enumerate(questions)}
    n = len(questions)
    gold_rows_raw = []
    for i, it in enumerate(gold_items):
        sims = doc_m @ gold_q[i]
        order = sorted(range(n), key=lambda j: (-sims[j], j))
        sig = _signals_from_sims(sims, order, gold_row=row_of[it["gold_id"]])
        gold_rows_raw.append({
            "query": it["query"], "gold_id": it["gold_id"], "bucket": it["query_type"],
            "rank": sig["rank"], "s1": sig["s1"], "gap5": sig["gap5"],
            "goldcos": sig["goldcos"],
        })
    probe_rows = []
    for i, p in enumerate(probes):
        sims = doc_m @ probe_q[i]
        order = sorted(range(n), key=lambda j: (-sims[j], j))
        sig = _signals_from_sims(sims, order)
        probe_rows.append({
            "query": p["query"], "stratum": p["stratum"], "s1": sig["s1"],
            "gap5": sig["gap5"],
        })

    ceiling = sum(1 for g in gold_rows_raw if g["rank"] <= TOP_K)
    by_signal = {}
    for signal, key in SIGNAL_KEYS.items():
        pkey = _PROBE_KEY[signal]
        gold_rows = [{**g, "signal": g[key]} for g in gold_rows_raw]
        p_sigs = [r[pkey] for r in probe_rows]
        good = [g["signal"] for g in gold_rows if g["rank"] <= TOP_K]
        near_sigs = [r[pkey] for r in probe_rows if r["stratum"] == "near"]
        far_sigs = [r[pkey] for r in probe_rows if r["stratum"] == "far"]
        by_signal[signal] = {
            "auroc": round(rank_auroc(good, p_sigs), 4),
            "gold_good_min": round(min(good), 4) if good else None,
            "probe_max": round(max(p_sigs), 4) if p_sigs else None,
            "probe_max_near": round(max(near_sigs), 4) if near_sigs else None,
            "probe_max_far": round(max(far_sigs), 4) if far_sigs else None,
            "overlap_count": sum(1 for s in good if s <= max(p_sigs)),
            "at_tau_050": gate_at(0.50, p_sigs, gold_rows),
            "zero_leak": zero_leak_point(p_sigs, gold_rows),
            # 分层判决的另一半：仅对远域地层零泄漏（现哨兵的保证范围就是
            # 远域——近域地层是另一层，交给消费侧弃权/换题语义）
            "zero_leak_far": zero_leak_point(far_sigs, gold_rows),
        }
    return {
        "dim": int(doc_m.shape[1]),
        "gold_total": len(gold_rows_raw),
        "ranking_ceiling_rank5": ceiling,       # 不设闸时的上限（排序的账）
        "by_signal": by_signal,
        "gold_rows": gold_rows_raw,
        "probe_rows": probe_rows,
    }


def print_2x2(results: dict) -> None:
    print(f"\n{'打分器':<28}{'信号':<10}{'AUROC':>8}{'零泄漏τ':>9}"
          f"{'gold保留':>11}{'keyword':>8}{'探针max':>9}")
    for model, r in results.items():
        short = model.split("/")[-1]
        for signal in SIGNAL_KEYS:
            s = r["by_signal"][signal]
            z = s["zero_leak"]
            kw = z["retained_by_bucket"].get("keyword")
            print(f"{short:<28}{signal:<10}{s['auroc']:>8}{z['tau']:>9}"
                  f"{z['gold_retained']:>6}/{r['gold_total']}"
                  f"{('' if kw is None else f'{kw}'):>8}{s['probe_max']:>9}")
            zf = s["zero_leak_far"]
            print(f"{'':<28}└ 仅远域零泄漏：τ={zf['tau']:<8}"
                  f"保留 {zf['gold_retained']}/{r['gold_total']}"
                  f"（探针max 近 {s['probe_max_near']} / 远 {s['probe_max_far']}）")
        print(f"  └ 排序天花板（rank≤5，不设闸）：{r['ranking_ceiling_rank5']}"
              f"/{r['gold_total']}｜dim={r['dim']}")
    n = max((len(r["probe_rows"]) for r in results.values()), default=0)
    if n:
        print(f"\nrule of three：0 泄漏 n={n} → 真实泄漏率 95% 上界 "
              f"{rule_of_three_upper(n):.1%}")


def main() -> int:
    ap = argparse.ArgumentParser(description="泄漏闸门 2×2 实验：打分器档位 × 信号形态")
    ap.add_argument("--corpus", default=str(REPO_ROOT / "corpus"))
    ap.add_argument("--goldset", default=str(SCRIPT_DIR / "data" / "bank_rag_goldset.json"))
    ap.add_argument("--probes", default=str(SCRIPT_DIR / "data" / "leak_probes.json"))
    ap.add_argument("--models", default="BAAI/bge-small-zh-v1.5,jinaai/jina-embeddings-v2-base-zh")
    ap.add_argument("--out-dir", default=str(SCRIPT_DIR / "data"))
    args = ap.parse_args()

    from interviewer.dataflows.local_corpus import parse_corpus

    questions = parse_corpus(args.corpus)
    probes_doc = json.loads(Path(args.probes).read_text(encoding="utf-8"))
    goldset = json.loads(Path(args.goldset).read_text(encoding="utf-8"))

    # 探针集与题库的对账（两级：粗 = bank_size，细 = forbidden_terms 缺席核查）
    if probes_doc.get("bank_size") != len(questions):
        print(f"⚠ 探针集与题库失配：探针 bank_size={probes_doc.get('bank_size')}"
              f" vs 当前 {len(questions)}——近域探针的缺席假定可能已过期，先重出探针集",
              file=sys.stderr)
        return 1
    violations = check_probes(probes_doc["probes"], questions)
    if violations:
        print("⚠ 近域探针被题库覆盖（forbidden_terms 命中），先重出探针集：", file=sys.stderr)
        for q, term in violations:
            print(f"  {q}｜命中词：{term}", file=sys.stderr)
        return 1
    if goldset.get("bank_size") != len(questions):
        print(f"⚠ 金标与题库失配（{goldset.get('bank_size')} vs {len(questions)}）"
              f"——先重跑 gen_rag_goldset.py，否则数字不可比", file=sys.stderr)

    gold_items = [it for it in goldset["items"] if it["query_type"] != "offtopic"]
    probes = probes_doc["probes"]
    strata = Counter(p["stratum"] for p in probes)
    print(f"题库 {len(questions)} 题｜gold {len(gold_items)} 条（四桶）｜"
          f"探针 {len(probes)} 条（{dict(strata)}）")

    results = {}
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"嵌入中：{model} ……", flush=True)
        try:
            results[model] = evaluate_model(model, questions, gold_items, probes)
        except ImportError as exc:
            print(f"跳过 {model}（依赖不可用：{exc}）")
            continue
    if not results:
        print("没有任何打分器跑完，退出", file=sys.stderr)
        return 1

    print_2x2(results)
    for model, r in results.items():
        for signal in ("goldcos", "maxscore"):
            z = r["by_signal"][signal]["zero_leak"]
            if z["lost"]:
                print(f"\n[{model.split('/')[-1]}｜{signal} 零泄漏点] 丢 {len(z['lost'])} 条 gold"
                      f"（信号最低的 10 条）：")
                for row in z["lost"][:10]:
                    print(f"  {row['bucket']:<10}rank={row['rank']:<3}{row['signal']:<8}{row['query']}")

    out = Path(args.out_dir) / f"leak_gate_2x2_{date.today().isoformat()}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bank_size": len(questions),
        "goldset_file": Path(args.goldset).name,
        "probes_file": Path(args.probes).name,
        "n_probes": len(probes),
        "probe_strata": dict(strata),
        "rule_of_three_upper_at_zero": round(rule_of_three_upper(len(probes)), 4),
        "design": "打分器档位 × 信号形态 2×2（探针 40 × 金标 780 全库 cosine）",
        "models": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已沉淀：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
