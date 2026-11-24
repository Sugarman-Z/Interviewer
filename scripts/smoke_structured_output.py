"""双方法结构化输出冒烟（一次性脚本，不进 pytest）。
背景：GLM 经智谱 OpenAI 兼容端点，with_structured_output 两种 method
（json_schema 约束解码 / function_calling 工具填参）支持度未知——必须同
prompt、同 schema、只变 method 实测遵约率，再显式选型写进配置。

用法（真实模式，消耗少量 token）：
    python scripts/smoke_structured_output.py --n 3   # 先小样本验证管道
    python scripts/smoke_structured_output.py          # 全量 20 样本 × 2 方法

结果存 scripts/data/structured_smoke_results.json（覆盖写，含时间戳与逐样本明细）。
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from interviewer.agents.prompts.evaluator import EVALUATOR_SYSTEM
from interviewer.agents.schemas import SCORE_DIMS, Scorecard
from interviewer.default_config import get_config
from interviewer.llm import create_llm_client

RESULTS_PATH = Path(__file__).parent / "data" / "structured_smoke_results.json"

POSITION = "后端开发工程师"

QUESTIONS = {
    "arch": {"topic": "项目整体架构", "question": "介绍一下你简历里最核心的一个项目：它解决什么问题，你负责哪部分？"},
    "choice": {"topic": "技术选型", "question": "这个项目里你做过哪些技术选型？当时为什么选它而不是备选方案？"},
    "btree": {"topic": "MySQL 索引原理", "question": "为什么 MySQL 选择 B+ 树做索引，而不是哈希表或红黑树？"},
    "cache": {"topic": "缓存一致性", "question": "缓存和数据库的一致性你是怎么处理的？"},
}

# 20 组（题目, 期望档位, 回答）：每题覆盖回答光谱 5 档——
# strong=扎实 / mid=正确但浅 / weak=几乎无内容 / vague=空泛方法论 / off=答非所问。
# expect 不是金标（校准在 W5），只用于人工核对打分方向是否正确。
_RAW_ANSWERS = [
    ("arch", "strong",
     "我最核心的项目是给连锁餐饮做库存 SaaS，解决门店库存和总部采购脱节、损耗率高的问题。"
     "我负责库存同步服务：门店 POS 的增量变更经 Kafka 进来，消费端做幂等去重后更新 MySQL，"
     "再由定时对账任务兜底修正差异。上线后损耗率从 8% 降到 3%。幂等和对账这两个模块的方案是我设计的。"),
    ("arch", "mid",
     "我做过一个电商后台管理系统，分订单管理和库存管理两个模块。我负责后端接口开发，"
     "用 Spring Boot 写的，数据库用 MySQL，前端同事对接我给的接口。项目上线后公司内部一直在用，整体效果还行。"),
    ("arch", "weak",
     "项目是我们团队一起做的管理系统，我负责的部分不算多，主要是修了一些别人模块的 bug，"
     "也帮忙写过几个页面。整体架构是组长搭的，我没有参与太多设计。"),
    ("arch", "vague",
     "架构设计上我一直坚持高内聚低耦合的原则，我们用的是微服务架构，技术栈都是业界主流，性能表现也不错。"
     "具体细节太多了，一下子讲不完，但整体是一套很成熟的方案。"),
    ("arch", "off",
     "说到项目，我觉得团队合作是最重要的。我们团队氛围特别好，每周都有技术分享和团建，"
     "遇到问题大家一起加班解决，所以项目最后都按时交付了，领导对我们评价也很高。"),

    ("choice", "strong",
     "消息队列在 Kafka 和 RabbitMQ 之间选的。我们的事件量大、允许秒级延迟、还要支持故障后回溯重放，"
     "所以选 Kafka：分区并行度高、日志可回放，对账任务能从头消费。代价是运维更重，我们只维护一套 3 节点集群。"
     "RabbitMQ 路由灵活但消息堆积能力弱，扛不住我们的峰值。"),
    ("choice", "mid",
     "数据库选了 MySQL 而不是 MongoDB，主要因为团队都熟悉 MySQL，社区资料多，出问题好排查，"
     "公司现有的运维体系也是围绕 MySQL 建的。没有做过特别深入的对比测试。"),
    ("choice", "weak",
     "技术选型主要是架构师和领导定的，我们负责执行。用的是公司统一的技术栈，Java 那一套。"
     "当时为什么这么选我没有参与讨论，选型会议我没有资格参加。"),
    ("choice", "vague",
     "我的选型理念是：没有最好的技术，只有最合适的技术。我们会综合考量性能、成本、社区活跃度和团队熟悉度"
     "做全面权衡，最终选择对业务最合适的方案，这是最基本的工程素养。"),
    ("choice", "off",
     "我对新技术一直很敏感，业余时间在学 Rust 和 AI 大模型。我觉得选型就应该选最新最热的技术，"
     "旧技术迟早会被淘汰，用新技术也能倒逼团队成长。"),

    ("btree", "strong",
     "哈希表等值查询 O(1)，但不支持范围扫描和排序，而数据库里范围查询和 order by 非常常见；"
     "红黑树是二叉的，千万级数据树高二十多层，每层一次磁盘 IO，太贵。B+ 树矮胖，3 层约能撑两千万行，"
     "非叶子节点只存键，单页能放更多索引项；叶子节点链表相连，范围查询是顺序 IO。"
     "InnoDB 单表建议不超过两千万，也是从树高推出来的。"),
    ("btree", "mid",
     "B+ 树比红黑树矮胖，磁盘 IO 次数更少。哈希虽然等值查询快，但不支持范围查询。"
     "另外 B+ 树叶子节点之间有指针相连，范围查询和排序都很方便，所以数据库选它。"),
    ("btree", "weak",
     "B+ 树是 MySQL 自带的索引数据结构，是它默认的实现，效率应该比其他数据结构高。"
     "具体为什么选它我没有深入了解过，我平时主要是用索引，比如加索引、看执行计划。"),
    ("btree", "vague",
     "因为 B+ 树是经过大厂大规模实践检验的最优方案，是数据库领域几十年沉淀下来的业界共识。"
     "数据库的发明者们肯定比我们更懂底层原理，直接相信成熟方案就不会错。"),
    ("btree", "off",
     "索引非常重要，我们之前有个慢 SQL 加了联合索引后从 10 秒优化到 0.1 秒。"
     "所以我认为索引是性能优化的第一要务，每个后端都应该掌握索引优化的技巧，面试也经常考。"),

    ("cache", "strong",
     "我们用 Cache Aside 模式：读的时候先缓存后库，写的时候先更新数据库再删除缓存。"
     "删缓存失败靠 MQ 异步重试兜底，所有 key 都带过期时间作为最终一致的保底。两个关键取舍："
     "不先删缓存后更库，因为并发下旧值会回填；删而不是改缓存，避免并发写覆盖。"
     "要强一致的链路我们直接查库不走缓存。"),
    ("cache", "mid",
     "用的是先更新数据库、再删除缓存的策略，并且给缓存设置了过期时间。如果删除失败会进重试队列。"
     "常规场景没问题，特别极端的并发时序我没有实际处理过。"),
    ("cache", "weak",
     "缓存就是 Redis，数据库是 MySQL。读的时候先读 Redis，Redis 没有就去读 MySQL 再写回 Redis。"
     "一致性问题我们没怎么遇到过，应该问题不大吧。"),
    ("cache", "vague",
     "缓存一致性是分布式系统的经典难题。我们会根据业务场景在强一致和最终一致之间做权衡，"
     "采用业界成熟方案，配合监控告警保证数据的一致性，把不一致窗口控制到最小。"),
    ("cache", "off",
     "缓存的穿透、击穿、雪崩这三个经典问题我们都处理过：布隆过滤器防穿透，互斥锁防击穿，"
     "过期时间加随机值防雪崩。这三个问题是缓存面试的高频考点，我都背得很熟。"),
]

SAMPLES = [
    {
        "topic": QUESTIONS[q]["topic"],
        "question": QUESTIONS[q]["question"],
        "answer": answer,
        "expect": expect,  # 期望档位，仅供人工核对，不参与评分
    }
    for q, expect, answer in _RAW_ANSWERS
]


def build_prompt(sample: dict) -> str:
    # ：EVALUATER_SYSTEM 扩到 7 占位符（+followup_hints/followup_budget），
    # 冒烟场景无追问树与历史，给单轮初答的哨兵值
    return EVALUATOR_SYSTEM.format(
        position=POSITION,
        topic=sample["topic"],
        question=sample["question"],
        qa_history="（无）",
        answer=sample["answer"],
        followup_budget="本题已答 0 轮 / 熔断线 3 轮（含主问轮）",
        followup_hints="（无）",
    )


def run_method(structured, method: str, samples: list, n: int) -> list:
    """逐样本调用并记录明细。失败是测量数据，不是异常：全部捕获进记录。"""
    records = []
    for i, sample in enumerate(samples[:n]):
        t0 = time.perf_counter()
        card, error = None, ""
        try:
            card = structured.invoke(build_prompt(sample))
        except Exception as exc:  # noqa: BLE001 —— 冒烟就是要收集失败样本
            error = f"{type(exc).__name__}: {exc}"[:300]
        latency = time.perf_counter() - t0
        ok = isinstance(card, Scorecard)
        records.append({
            "idx": i,
            "expect": sample["expect"],
            "ok": ok,
            "latency_s": round(latency, 2),
            "scorecard": card.model_dump() if ok else None,
            "error": error,
        })
        mark = "ok  " if ok else "FAIL"
        scores = ""
        if ok:
            scores = (f"depth={card.technical_depth} corr={card.correctness} "
                      f"comm={card.communication} action={card.action}")
        print(f"  [{method} #{i:>2}] {mark} expect={sample['expect']:<6} "
              f"{latency:>5.1f}s  {scores}  {error[:80]}", flush=True)
    return records


def summarize(method: str, records: list) -> dict:
    """汇总单方法的遵约率、延迟与打分分布（打分分布供人工核对方向是否正确）。"""
    oks = [r for r in records if r["ok"]]
    rate = len(oks) / len(records) if records else 0.0
    dims = SCORE_DIMS
    dim_means = {}
    for dim in dims:
        values = [r["scorecard"][dim] for r in oks]
        dim_means[dim] = round(sum(values) / len(values), 2) if values else None
    by_expect = {}
    for r in records:
        by_expect.setdefault(r["expect"], []).append("ok" if r["ok"] else "FAIL")
    stats = {
        "success_rate": round(rate, 3),
        "n_ok": len(oks),
        "n_total": len(records),
        "avg_latency_s": round(sum(r["latency_s"] for r in oks) / len(oks), 2) if oks else None,
        "dim_means": dim_means,
        "action_followup": sum(1 for r in oks if r["scorecard"]["action"] == "followup"),
        "by_expect": by_expect,
        "failures": [{"idx": r["idx"], "expect": r["expect"], "error": r["error"]}
                     for r in records if not r["ok"]],
        "records": records,
    }
    print(f"\n  [汇总] {method}: 遵约率 {stats['n_ok']}/{stats['n_total']}"
          f"（{rate:.0%}）  平均延迟 {stats['avg_latency_s']}s  各维均分 {dim_means}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="结构化输出双方法遵约率冒烟")
    parser.add_argument("--n", type=int, default=len(SAMPLES), help="样本数上限")
    parser.add_argument("--methods", default="json_schema,function_calling",
                        help="逗号分隔的 method 列表")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config = get_config()
    if config.get("llm_provider") == "mock":
        raise SystemExit("冒烟脚本必须真实模式：去掉 INTERVIEWER_LLM_PROVIDER=mock，"
                         "并确保 .env 里填了 API key。遵约率对 mock 无意义。")

    llm = create_llm_client("deep", config)
    results = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "model": config["deep_llm"],
        "prompt": "EVALUATOR_SYSTEM v1-e1",
        "schema": "Scorecard",
        "methods": {},
    }

    for method in [m.strip() for m in args.methods.split(",") if m.strip()]:
        print(f"\n=== method = {method} ===")
        structured = llm.with_structured_output(Scorecard, method=method)
        records = run_method(structured, method, SAMPLES, args.n)
        results["methods"][method] = summarize(method, records)

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rates = {m: s["success_rate"] for m, s in results["methods"].items()}
    print(f"\n结果已存 {RESULTS_PATH}")
    print(f"遵约率对比：{rates}")


if __name__ == "__main__":
    main()
