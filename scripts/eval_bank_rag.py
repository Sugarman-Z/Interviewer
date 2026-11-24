"""检索判决脚本：多 vendor × 多 k 的离线评测。

用法：PYTHONUTF8=1 python scripts/eval_bank_rag.py [--baseline scripts/data/bank_rag_eval_<date>.json]

设计合同（与 gen_rag_goldset.py / tests/test_rag_regression.py 三方同源）：
直调 vendor 函数不走 markdown 格式化，exclude_topics=() 保证各 vendor 可比
（签发期查重是业务约束，不是检索质量变量）。recall@k 为门指标（下游读整份
top-5 做 listwise 选择，"找没找到" > "排第几"）；coverage@k 并排（top-k 里
不同考点的比例，多样性不掩盖召回缺失）；MRR@10 / nDCG@10 补位置与分级
增益；p50 为热缓存口径；cold_start_ms 用独立子进程量（同进程共享模块级
缓存，后测的 vendor 会读到先测者焐热的缓存）。offtopic 桶只判泄漏（远域
探针命中即泄漏，目标 0）。dense 在链上时自动追加 dense(mmr) 合成列（评
"检索+组卷"整链）。按 query_type 分桶切片，结果沉淀
scripts/data/bank_rag_eval_<date>.json（换心对拍 = 跑两次 + --baseline diff）。
未注册的 vendor 或依赖缺席跳过不炸，已完成的结果照常落盘。
"""
import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from interviewer.dataflows.local_corpus import parse_corpus

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

# nDCG/MRR 的截断深度：与"下游读整份 top-5"对齐放大到 10（MRR@10 同深）
CUTOFF = 10
GAIN_GOLD = 3
GAIN_ACCEPTABLE = 1


def resolve_vendors(names: list[str]) -> tuple[dict, list]:
    """名字 → vendor 函数。local 零依赖即时注册；其余（bm25/dense 等）
    走同一注册表，未注册 = 未安装/未实现 → 跳过不炸（TODO C3/C5 落地后
    自动出现在 --vendors 里，本脚本零改动）。"""
    import interviewer.dataflows.local_corpus  # noqa: F401 —— import 即注册 local
    try:
        import interviewer.dataflows.dense_corpus  # noqa: F401 —— C5 稠密腿
        import interviewer.dataflows.hybrid       # noqa: F401 —— C5 混合（RRF；连带注册 dense）
    except ImportError:
        pass
    from interviewer.dataflows.errors import SourceNotConfiguredError
    from interviewer.dataflows.interface import TOOL_NAME, get_vendor_chain

    resolved, skipped = {}, []
    for name in names:
        try:
            resolved[name] = get_vendor_chain(TOOL_NAME, name)[0]
        except SourceNotConfiguredError:
            skipped.append(name)
    return resolved, skipped


def ndcg_at(ids: list[str], gold: str, acceptable: list[str], cutoff: int = CUTOFF) -> float:
    """分级增益 nDCG。DCG = Σ gain_i / log2(名次+1)；IDCG = 理想排序
（gold 在前、acceptable 随后）同式。"""
    acc = set(acceptable)

    def dcg(gains: list[int]) -> float:
        return sum(g / math.log2(pos + 2) for pos, g in enumerate(gains[:cutoff]))

    gains = [GAIN_GOLD if i == gold else (GAIN_ACCEPTABLE if i in acc else 0) for i in ids]
    ideal = sorted([GAIN_GOLD] + [GAIN_ACCEPTABLE] * len(acc), reverse=True)
    idcg = dcg(ideal)
    return dcg(gains) / idcg if idcg else 0.0


def evaluate_vendor(fn, items, ks, corpus_dir, runs) -> dict:
    """单 vendor 全量评测：per_query 明细 + overall/分桶汇总 + offtopic 泄漏。"""
    fetch_k = max(max(ks), CUTOFF)
    scored = [it for it in items if it["query_type"] != "offtopic"]
    probes = [it for it in items if it["query_type"] == "offtopic"]
    # 首调暖机（建索引/载模型，让延迟循环量的是热缓存口径）。冷启动不在
    # 本进程量——同进程多 vendor 共享模块级缓存，先测者焐热后测者
    #（真冷启动由 measure_cold_start 的独立子进程测）
    fn(scored[0]["query"], exclude_topics=(), top_k=fetch_k, corpus_dir=corpus_dir)
    per_query, per_bucket = [], {}

    def _blank() -> dict:
        return {"n": 0, "hits": {k: 0 for k in ks}, "cov": {k: 0.0 for k in ks},
                "rr": 0.0, "ndcg": 0.0, "latency_ms": []}

    for it in scored:
        hits = fn(it["query"], exclude_topics=(), top_k=fetch_k, corpus_dir=corpus_dir)
        ids = [h.id for h in hits]
        rank = ids.index(it["gold_id"]) + 1 if it["gold_id"] in ids else None
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            fn(it["query"], exclude_topics=(), top_k=fetch_k, corpus_dir=corpus_dir)
            times.append((time.perf_counter() - t0) * 1000)

        row = {
            "query": it["query"],
            "gold_id": it["gold_id"],
            "query_type": it["query_type"],
            "rank": rank,
            "hit_ids": ids,
        }
        per_query.append(row)

        bucket = per_bucket.setdefault(it["query_type"], _blank())
        bucket["n"] += 1
        for k in ks:
            if rank is not None and rank <= k:
                bucket["hits"][k] += 1
            # coverage@k：top-k 内不同考点比例（分母按实际命中数截断）
            denom = min(k, len(hits))
            bucket["cov"][k] += (len({h.topic for h in hits[:k]}) / denom) if denom else 0.0
        bucket["rr"] += (1.0 / rank) if (rank is not None and rank <= CUTOFF) else 0.0
        bucket["ndcg"] += ndcg_at(ids, it["gold_id"], it["acceptable_ids"])
        bucket["latency_ms"].append(statistics.median(times))

    def _summarize(b: dict) -> dict:
        n = b["n"] or 1
        out = {f"recall@{k}": round(b["hits"][k] / n, 4) for k in ks}
        for k in ks:
            out[f"coverage@{k}"] = round(b["cov"][k] / n, 4)
        out["mrr@10"] = round(b["rr"] / n, 4)
        out["ndcg@10"] = round(b["ndcg"] / n, 4)
        out["latency_p50_ms"] = round(statistics.median(b["latency_ms"]), 2) if b["latency_ms"] else None
        out["n"] = b["n"]
        return out

    overall = _summarize({
        "n": sum(b["n"] for b in per_bucket.values()),
        "hits": {k: sum(b["hits"][k] for b in per_bucket.values()) for k in ks},
        "cov": {k: sum(b["cov"][k] for b in per_bucket.values()) for k in ks},
        "rr": sum(b["rr"] for b in per_bucket.values()),
        "ndcg": sum(b["ndcg"] for b in per_bucket.values()),
        "latency_ms": [x for b in per_bucket.values() for x in b["latency_ms"]],
    })
    # offtopic 泄漏判决（无 gold：远域探针命中即泄漏，目标 0）
    offtopic = {"n": 0, "leaked": 0, "leaked_queries": []}
    for it in probes:
        hits = fn(it["query"], exclude_topics=(), top_k=fetch_k, corpus_dir=corpus_dir)
        offtopic["n"] += 1
        if hits:
            offtopic["leaked"] += 1
            offtopic["leaked_queries"].append(
                {"query": it["query"], "hit_ids": [h.id for h in hits]})
    offtopic["leak_rate"] = round(offtopic["leaked"] / offtopic["n"], 4) if offtopic["n"] else None

    return {
        "overall": overall,
        "by_query_type": {b: _summarize(v) for b, v in per_bucket.items()},
        "offtopic": offtopic,
        "per_query": per_query,
    }


def print_table(name: str, result: dict, ks) -> None:
    cols = ([f"recall@{k}" for k in ks] + [f"coverage@{k}" for k in ks]
            + ["mrr@10", "ndcg@10", "latency_p50_ms"])
    header = f"{'桶':<12}" + "".join(f"{c:>13}" for c in cols) + f"{'n':>5}"
    print(f"\n== {name} ==")
    print(header)
    rows = [("overall", result["overall"])] + sorted(result["by_query_type"].items())
    for label, m in rows:
        cells = "".join(f"{str(m.get(c, '')):>13}" for c in cols)
        print(f"{label:<12}{cells}{m['n']:>5}")
    off = result.get("offtopic")
    if off and off["n"]:
        print(f"offtopic 泄漏：{off['leaked']}/{off['n']}（leak_rate={off['leak_rate']}，目标 0"
              "——远域探针命中即哨兵击穿）")


# 冷启动子进程驱动器：按 vendor 名给一段最小 import + 单次检索代码。
# 子进程隔离模块级缓存（_ENCODER_CACHE/_INDEX_CACHE 是进程内单例），
# 三列 cold_start_ms 才同口径可比。dense(mmr) 复用 dense 的驱动：冷路径
# 大头是模型加载+索引载入，MMR 包装层只多一次 query_embed（毫秒级）。
_COLD_DRIVERS = {
    "local": "from interviewer.dataflows.local_corpus import search_corpus_local as f",
    "dense": "from interviewer.dataflows.dense_corpus import search_corpus_dense as f",
    "dense(mmr)": "from interviewer.dataflows.dense_corpus import search_corpus_dense as f",
    "hybrid": "from interviewer.dataflows.hybrid import search_corpus_hybrid as f",
}


def measure_cold_start(name: str, corpus_dir: str) -> float | None:
    """独立子进程量冷启动（新进程首调：模型加载 + 索引载入/构建）。
    复用 ./data 下的模型权重与磁盘索引——量的是"新进程"，不是"全新机器"。
    子进程失败（依赖缺席等）返回 None 并告警，不炸主流程。"""
    driver = _COLD_DRIVERS.get(name)
    if driver is None:
        return None
    code = f"{driver}\nf('kafka', exclude_topics=(), top_k=5, corpus_dir={corpus_dir!r})\n"
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True, timeout=300,
    )
    if proc.returncode != 0:
        print(f"⚠ {name} 冷启动子进程失败：\n{proc.stderr.decode(errors='replace')[-500:]}",
              file=sys.stderr)
        return None
    return round((time.perf_counter() - t0) * 1000, 2)


def print_baseline_diff(current: dict, baseline: dict) -> None:
    """换心对拍：同名 vendor × 同名指标的 delta（+ 为变好，延迟相反）。"""
    print("\n== vs 基线 ==")
    for name, cur in current.items():
        base = baseline.get("vendors", {}).get(name)
        if not base:
            print(f"{name}: 基线里没有，跳过")
            continue
        pairs = [("overall", cur["overall"], base["overall"])]
        for bucket, m in cur["by_query_type"].items():
            if bucket in base["by_query_type"]:
                pairs.append((bucket, m, base["by_query_type"][bucket]))
        for label, c, b in pairs:
            for key in c:
                if key in ("n",) or key not in b:
                    continue
                delta = round(c[key] - b[key], 4)
                if delta:
                    arrow = "↑" if (delta < 0 if key.startswith("latency") else delta > 0) else "↓"
                    print(f"{name}｜{label}｜{key}: {b[key]} → {c[key]} ({arrow}{abs(delta)})")


def _dense_mmr_vendor():
    """dense+MMR 合成列：vendor 面检索池 → 组卷层 MMR，与生产 bank_tool
    路径同一个 diversify_hits。eval 合同"直调 vendor 评检索本身"在本列有意
    破例——列名自带 (mmr) 区分口径，评的是"检索+组卷"整链。"""
    from interviewer.dataflows.dense_corpus import search_corpus_dense
    from interviewer.dataflows.mmr import MMR_POOL_N, diversify_hits

    def _search(query, *, exclude_topics=(), top_k=5, corpus_dir="./corpus"):
        pool = search_corpus_dense(query, exclude_topics=exclude_topics,
                                   top_k=max(MMR_POOL_N, top_k), corpus_dir=corpus_dir)
        return diversify_hits(query, pool, corpus_dir=corpus_dir,
                              vendors="dense", top_k=top_k)

    return _search


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 检索评测：vendors × k × 四桶金标")
    ap.add_argument("--vendors", default="local", help="逗号分隔，如 local,bm25,dense（未装的自动跳过）")
    ap.add_argument("--k", default="1,3,5", help="recall@k 的 k 列表")
    ap.add_argument("--runs", type=int, default=5, help="延迟采样次数（取中位）")
    ap.add_argument("--goldset", default=str(SCRIPT_DIR / "data" / "bank_rag_goldset.json"))
    ap.add_argument("--corpus", default=str(REPO_ROOT / "corpus"))
    ap.add_argument("--baseline", default=None, help="上一版 eval JSON，输出逐指标 delta")
    ap.add_argument("--out-dir", default=str(SCRIPT_DIR / "data"))
    args = ap.parse_args()

    goldset = json.loads(Path(args.goldset).read_text(encoding="utf-8"))
    bank_size = len(parse_corpus(args.corpus))
    if goldset.get("bank_size") != bank_size:
        print(f"⚠ 金标与题库失配：金标 bank_size={goldset.get('bank_size')}，当前题库 {bank_size} 题"
              f"——先重跑 gen_rag_goldset.py 再评，否则数字不可比", file=sys.stderr)
    items = goldset["items"]
    ks = sorted({int(x) for x in args.k.split(",") if x.strip()})

    resolved, skipped = resolve_vendors([v.strip() for v in args.vendors.split(",") if v.strip()])
    for name in skipped:
        print(f"跳过 {name}（未注册/未安装——C3/C5 落地后可用）")
    if not resolved:
        print("没有任何可用 vendor，退出", file=sys.stderr)
        return 1
    if "dense" in resolved:
        try:
            resolved["dense(mmr)"] = _dense_mmr_vendor()
        except ImportError:
            pass   # mmr/numpy 缺席：合成列不加，其余照常

    results = {}
    for name, fn in resolved.items():
        try:
            results[name] = evaluate_vendor(fn, items, ks, args.corpus, args.runs)
        except ImportError as exc:
            # 注册≠可用（fastembed 惰性导入在首次调用才触发）：按"未装自动
            # 跳过"契约处理，已完成 vendor 的结果仍能走到落盘；其余异常照炸
            #（fail-loud 管坏库/坏缓存，不管依赖缺席）
            print(f"跳过 {name}（依赖不可用：{exc}）")
            continue
        results[name]["overall"]["cold_start_ms"] = measure_cold_start(name, args.corpus)
        print_table(name, results[name], ks)

    if args.baseline:
        print_baseline_diff(results, json.loads(Path(args.baseline).read_text(encoding="utf-8")))

    out = Path(args.out_dir) / f"bank_rag_eval_{date.today().isoformat()}.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "bank_size": bank_size,
        "goldset_file": str(Path(args.goldset).name),
        "goldset_bank_size": goldset.get("bank_size"),
        "k": ks,
        "runs_for_latency": args.runs,
        "vendors": {name: {kk: vv for kk, vv in r.items() if kk != "per_query"}
                    for name, r in results.items()},
        "per_query": {name: r["per_query"] for name, r in results.items()},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已沉淀：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
