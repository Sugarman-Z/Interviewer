"""真图无头会话（candidate_search_enabled 开启的验收/演示）。
驱动走 graph/session_driver（与 CLI/pytest 同一引擎），交互环换成脚本化
回答——transport 收口的另一面：图的全部输入都从 interrupt 载荷进来，谁来
答都一样。用途：真 API 会话里看 retrieval_trace 的 tool 键——模型是否自主
在题库面与简历/JD 段落面之间选工具（离线测试只能锁"机制在"，"模型真会用"
必须真会话验证）；顺手给缺题挖掘喂带留痕的真会话档。

开关不走环境变量——本脚本存在的目的就是开 candidate 面，直接改 config
dict（真 CLI 会话的开关仍由 INTERVIEWER_CANDIDATE_SEARCH_ENABLED 控制）。

用法：
    PYTHONUTF8=1 python scripts/run_session_headless.py               # 默认 4 题 + 内置回答池
    PYTHONUTF8=1 python scripts/run_session_headless.py --questions 6
    PYTHONUTF8=1 python scripts/run_session_headless.py --answers 回答.txt --resume 我的简历.txt
"""
from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter
from pathlib import Path

from interviewer.agents.utils.agent_states import RESUME_ANSWER
from interviewer.default_config import get_config
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph
from interviewer.graph.session_driver import run_new_session
from interviewer.graph.session_runner import create_initial_state, persist_session
from interviewer.resume.parser import candidate_profile_text, parse_pasted_resume

SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = SCRIPT_DIR / "data" / "e2e"

# 内置回答池：中等偏实的后端答案，措辞锚在 fixture 简历的事实上（Redis
# 事故/延迟双锁、Kafka 消费、P99 优化、带实习生）。轮换使用——验收要的
# 是走完整场图（追问/评分/换题路由全部真跑），不是演完美候选人。
DEFAULT_ANSWERS = [
    "这个我在上个项目里真做过：先定位瓶颈再动手。我们当时 P99 从 180ms 优化到 45ms，"
    "第一步是加链路追踪拆出耗时分布，发现 70% 时间在两次同步 RPC 和一次未命中缓存，"
    "然后把其中一个调用改成异步补偿、缓存加上过期时间和预热，才把尾巴压下来。",
    "Redis 这块我踩过真实的坑。那次缓存事故是热key过期瞬间大量请求直落数据库，"
    "我们用了延迟双锁加随机过期时间，另外核心接口加了熔断降级，数据库侧再兜一层"
    "最大连接数限制，之后没再复发。",
    "Kafka 消费我负责过：消费者组重平衡时的重复消费问题，靠幂等消费解决的——"
    "以业务主键写去重表，配合手动提交 offset，保证至少一次投递下的最终一致。",
    "带实习生那次我学到的比教的多：我把一个独立小模块的方案设计交给他做，"
    "我只定验收标准和每周review。他第一版方案有问题，我没有直接改，而是列了"
    "三个边界场景让他自己压测，他自己推翻重做了第二版。",
    "MySQL 索引这块我实践过的：一次慢查询排查，联合索引的列顺序和查询条件不匹配，"
    "导致全表扫描。我按最左前缀原则调整了索引顺序，同时把一个 select * 改成"
    "只查需要的列，覆盖索引直接在索引层返回，慢查询日志里这条就消失了。",
    "系统设计上我的原则是先量化再设计。上线前我会明确容量目标和失败模式，"
    "比如消息队列的选型，我们对比了吞吐、回放能力和运维成本才定 Kafka，"
    "不是因为它流行，是因为对账任务需要从头回放这个硬需求。",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真图无头会话（candidate 面验收）")
    ap.add_argument("--position", default="后端开发工程师")
    ap.add_argument("--resume", default=str(FIXTURE_DIR / "resume.txt"),
                    help="简历文本文件（默认 e2e fixture，与验收口径一致）")
    ap.add_argument("--jd", default=str(FIXTURE_DIR / "jd.txt"))
    ap.add_argument("--questions", type=int, default=4, help="题数上限（控制成本）")
    ap.add_argument("--answers", help="回答清单文件（每行一条；缺省内置池轮换）")
    ap.add_argument("--dry-run", action="store_true",
                    help="允许 mock provider 干跑——只验管道不验行为，不算验收")
    args = ap.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    config = get_config()
    if config.get("llm_provider") == "mock" and not args.dry_run:
        print("本脚本必须真实模式：.env 配好 ZHIPU_API_KEY（对 mock 会话跑验收没有意义；"
              "只验管道用 --dry-run）", file=sys.stderr)
        return 1
    config["candidate_search_enabled"] = True     # 本脚本的存在目的
    config["max_questions"] = args.questions

    answers = (Path(args.answers).read_text(encoding="utf-8").splitlines()
               if args.answers else DEFAULT_ANSWERS)
    answers = [a.strip() for a in answers if a.strip()]
    if not answers:
        print("回答清单为空", file=sys.stderr)
        return 1

    resume_text = Path(args.resume).read_text(encoding="utf-8")
    jd_text = Path(args.jd).read_text(encoding="utf-8")
    profile = candidate_profile_text(parse_pasted_resume(resume_text))

    session_id = f"c6acc-{uuid.uuid4().hex[:8]}"
    saver, close_checkpointer = build_checkpointer(config)
    try:
        coach = CoachGraph(config, checkpointer=saver)
        graph = coach.build()
        cfg = coach.invoke_config(session_id)

        payload = create_initial_state(session_id, args.position, profile, jd_text)
        turn = 0

        def on_interrupt(value: dict):
            """driver 的应答回调：打印本轮问答，从回答池轮换取一条。"""
            nonlocal turn
            turn += 1
            question = str(value.get("question") or "")
            answer = answers[(turn - 1) % len(answers)]
            print(f"\n── 第 {turn} 轮 ──\n[面试官] {question[:100]}", flush=True)
            print(f"[脚本作答] {answer[:60]}…", flush=True)
            return {"type": RESUME_ANSWER, "text": answer}

        result = run_new_session(graph, cfg, payload, on_interrupt)
        final_state = result.final_values
        paths = persist_session(config, final_state)

        # 验收观察位：留痕 tool 分账 + 题目来源
        trace = final_state.get("retrieval_trace") or []
        by_tool = Counter(r.get("tool", "?") for r in trace)
        cand_empty = sum(1 for r in trace
                         if r.get("tool") == "search_candidate" and r.get("empty_hit"))
        print(f"\n== 会话完成：{session_id} ==")
        print(f"题数 {len(final_state.get('issued_keys') or [])}｜留痕 {len(trace)} 条"
              f"｜tool 分账 {dict(by_tool)}｜candidate 面空命中 {cand_empty}")
        for r in trace:
            hits = len(r.get("hit_ids") or [])
            empty = "（空）" if r.get("empty_hit") else ""
            print(f"  [{r.get('tool', '?')}] {r.get('query', '')} → {hits} 命中{empty}")
        for e in final_state.get("transcript") or []:
            if e.get("kind") == "issue":
                fr = f"｜fallback: {e['fallback_reason']}" if e.get("fallback_reason") else ""
                print(f"  [签发] {e.get('topic', '?')}{fr}")
        print(f"\n会话已落盘：{paths['session']}")
        print("判据：trace 里两副工具都有调用 = 模型自主选工具成立；"
              "只有 search_corpus = candidate 面没被用（看 ADDENDUM 措辞）。")
        return 0
    finally:
        close_checkpointer()


if __name__ == "__main__":
    sys.exit(main())
