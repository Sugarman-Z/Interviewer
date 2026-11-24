"""真机流式探针：真智谱一场，录 issue/evaluator 的 chunk 序列。
真机 chunk 序列落 tests/fixtures/（默认工具配置、bind_tools 真路径——
tap 抽取器拿它回归，防「手写扫描器对真机 token 边界想当然」）；同时验证
真图 + 真 zhipu + stream_mode=["updates","messages"] 吐 chunk 且回合语义不变。

跑法（PowerShell，仓库根）：
  $env:PYTHONUTF8='1'; $env:NO_PROXY='open.bigmodel.cn'
  python scripts/record_stream_chunks.py            # 录两类 fixture 后自动停
花真额度：约 2-4 轮 × 15-25s。tmp sqlite checkpointer（不碰开发库）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")
if "open.bigmodel.cn" not in os.environ.get("NO_PROXY", ""):
    os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",open.bigmodel.cn").lstrip(",")

from langgraph.types import Command  # noqa: E402

from interviewer.default_config import get_config  # noqa: E402
from interviewer.graph.checkpointer import build_checkpointer  # noqa: E402
from interviewer.graph.coach_graph import CoachGraph  # noqa: E402
from interviewer.graph.session_runner import create_initial_state  # noqa: E402
from scripts.spike_stream_messages import _norm_tool_calls  # noqa: E402

if not os.environ.get("ZHIPU_API_KEY"):
    sys.exit("缺 ZHIPU_API_KEY（.env）——真机腿必须真 key")

#: 钓追问的答案：自信但说错——评估官看见误区倾向同考点变形追问（比「不知道」
#: 更容易触发 followup，实测纯摆烂会被直接换题）
_SELF_INTRO = (
    "我是五年经验的 Python 后端，主要做交易系统的行情网关和风控服务，"
    "技术栈 FastAPI 加 Kafka，最近在做一个低延迟撮合模拟器。"
)
_LAZY_ANSWERS = [
    "Kafka 我很熟：我们开了 auto-commit 每毫秒提交一次，消息就绝对不丢了，"
    "消费慢了就把分区数调大就行，没有顺序问题。",
    "高并发我们就是多开线程，Python 的 GIL 对多线程完全没影响，"
    "锁随便加，性能不够就加机器。",
    "缓存一致性我们用的先更新数据库再删缓存，这样永远不会有脏数据，"
    "不需要任何过期时间。",
]


def _ser_chunk(chunk, metadata) -> dict:
    return {
        "content": chunk.content or "",
        "tool_call_chunks": [
            {"name": c.get("name") or "", "args": c.get("args") or "",
             "id": c.get("id") or "", "index": c.get("index")}
            for c in (chunk.tool_call_chunks or [])
        ],
        # 键集即可：bind_tools 路径实测恒空（reasoning 不经 langchain 面泄漏，
        # TDD 事实 4）——留键名防日后 provider 行为变化静默漂移
        "additional_kwargs_keys": sorted((chunk.additional_kwargs or {}).keys()),
        "node": metadata.get("langgraph_node", ""),
    }


def _main() -> int:
    cfg = get_config()
    assert cfg["llm_provider"] == "zhipu", "真机腿必须真 provider"
    cfg["checkpoint_backend"] = "sqlite"
    cfg["checkpoint_path"] = os.path.join(tempfile.mkdtemp(), "spike.sqlite")
    cfg["data_dir"] = os.path.join(tempfile.mkdtemp(), "data")
    cfg["max_questions"] = 4

    saver, _close = build_checkpointer(cfg)
    coach = CoachGraph(cfg, checkpointer=saver)
    graph = coach.build()
    sid = "t0-spike-record"
    invoke_cfg = coach.invoke_config(sid)

    # 按「LLM 调用边界」分组的现场聚合（离线 spike 同款规则）：非空 id 开新组
    calls: list[dict] = []  # 每组 {chunks:[...], agg_tool_calls:[...]}

    def _feed(chunk, metadata):
        tccs = list(chunk.tool_call_chunks or [])
        starts_new = any(c.get("id") for c in tccs) or (
            bool(chunk.content)
            and calls
            and not any(pc["raw"].tool_call_chunks for pc in calls[-1]["chunks"])
            and any(pc["raw"].content for pc in calls[-1]["chunks"])
        )
        if not calls or starts_new:
            calls.append({"chunks": []})
        calls[-1]["chunks"].append({"raw": chunk, "meta": metadata})

    def _finalize_calls():
        for call in calls:
            agg = None
            for pc in call["chunks"]:
                agg = pc["raw"] if agg is None else agg + pc["raw"]
            call["agg_tool_calls"] = _norm_tool_calls(agg)
            call["nodes"] = sorted({pc["meta"].get("langgraph_node") for pc in call["chunks"]})

    initial = create_initial_state(
        sid, position="Python 后端开发", candidate_profile=_SELF_INTRO
    )
    payload = initial
    rounds = 0
    pending_qnum = "opening"
    followup_hit = False
    issue_done = False
    while rounds < 6 and not (issue_done and followup_hit):
        interrupt_value = None
        for mode, data in graph.stream(payload, invoke_cfg, stream_mode=["updates", "messages"]):
            if mode == "messages":
                _feed(*data)
            elif isinstance(data, dict) and "__interrupt__" in data:
                interrupt_value = data["__interrupt__"][0].value
        if interrupt_value is None:
            print("图跑完（意外收场）")
            break
        rounds += 1
        # 本轮挂起题：从 interrupt 载荷/checkpoint 读回
        from interviewer.graph.session_driver import pending_interrupt

        pend = pending_interrupt(graph, invoke_cfg)
        qtext = (pend or {}).get("question", "")  # 中断载荷键是 question（gateway pending_question 同源）
        print(f"[round {rounds}] 挂起题面前 30 字：{qtext[:30]!r}")

        _finalize_calls()
        # 本轮新增的 LLM 调用（含上一轮未命中的）里找 issue/evaluator 材料。
        # 追问判定自校验：evaluator 的 followup_text == 挂起题面 即铁证
        #（挂起载荷无 qnum——qnum 是 gateway 从 state 推导的，spike 拿不到）。
        # 注：calls 跨轮全累积（选取窗口偏宽）——本脚本靠「args 终文本 ==
        # 本轮挂起题面」的 ground truth 对齐兜底误并风险；正式 tap的
        # reset 锚节点切换 + 非空 id，不吃这个宽窗口
        for call in calls:
            if call.get("saved"):
                continue
            node0 = (call["nodes"] or [""])[0]
            for _tname, args, _tid in call["agg_tool_calls"]:
                if "evaluator" in node0.lower():
                    ft = args.get("followup_text") or ""
                    if ft.strip() and ft.strip() == qtext.strip() and not followup_hit:
                        call["saved"] = True
                        call["fixture"] = {
                            "source": "evaluator",
                            "node": node0,
                            "chunks": [_ser_chunk(pc["raw"], pc["meta"]) for pc in call["chunks"]],
                            "agg_args": args,
                            "final_followup_text": qtext,
                        }
                        followup_hit = True
                        print(f"  [fix] evaluator 追问腿命中：{len(call['chunks'])} chunk")
                elif "issue" in node0.lower():
                    it = args.get("text") or ""
                    if it.strip() and it.strip() == qtext.strip() and not issue_done:
                        call["saved"] = True
                        call["fixture"] = {
                            "source": "issue",
                            "node": node0,
                            "chunks": [_ser_chunk(pc["raw"], pc["meta"]) for pc in call["chunks"]],
                            "agg_args": args,
                            "final_text": qtext,
                        }
                        issue_done = True
                        print(f"  [fix] issue 腿命中：{len(call['chunks'])} chunk")

        # 下一答：开场后给「自信但说错」的答案钓追问
        answer = _SELF_INTRO if pending_qnum == "opening" else _LAZY_ANSWERS[(rounds - 2) % len(_LAZY_ANSWERS)]
        pending_qnum = str((pend or {}).get("qnum", ""))
        payload = Command(resume={"type": "answer", "text": answer})

    _finalize_calls()
    print(f"\n共 {rounds} 轮 / {len(calls)} 个 LLM 调用")
    fixtures = [c["fixture"] for c in calls if c.get("saved") and "fixture" in c]
    for fx in fixtures:
        name = "stream_chunks_issue.json" if fx["source"] == "issue" else "stream_chunks_evaluator.json"
        out = _REPO / "tests" / "fixtures" / name
        out.write_text(json.dumps(fx, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"落盘 {out.relative_to(_REPO)}（{len(fx['chunks'])} chunk）")
    if issue_done and followup_hit:
        print("== SPIKE-2 PASS：issue + evaluator(followup) 两份 fixture 齐 ==")
        return 0
    print(f"== SPIKE-2 不完整：issue={issue_done} followup={followup_hit}（懒答案没钓到追问？看上文件志）")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
