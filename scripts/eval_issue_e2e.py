"""签发质量端到端评测：LLM-judge 三轴 + 主判 pairwise + 留痕归因。
评的是 IssueQuestion 的签发质量（检索质量是 eval_bank_rag 的事）。三轴映射
防幻觉三道闸的软面：简历相关性（题锚在候选人材料上）、来源忠实度（不虚构
经历/事实）、全场不重复（换角度而非换个问法重考——查重锁考点名，换皮重复
只有 judge 看得出）。主判 pairwise（判相对优劣比绝对分稳一个量级）：两配置
各出一组题逐轴判胜负；默认对照组 = bank 检索 vs +第二检索面。留痕归因：
retrieval_trace 聚合与 source_type 计数和 judge 分数并排落盘——"检索好而
签发差 = 提示词的锅"。

真 API 不进 pytest（marker 纪律），pytest 侧只测 mock 自检路径；真跑：
PYTHONUTF8=1 python scripts/eval_issue_e2e.py --issues 5（issuer=quick 档
judge=deep 档，~2N+1 次调用）。fixture：scripts/data/e2e/（6 题固定小库 +
固定简历/JD，确定性对照）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from interviewer.agents.issue_question import make_issue_question
from interviewer.agents.utils.agent_states import fresh_followup_state
from interviewer.agents.utils.structured import invoke_structured_or_freetext
from interviewer.dataflows import resolve_candidate_tool, resolve_search_tool
from interviewer.llm import MockLLM, create_llm_client

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
FIXTURE_DIR = SCRIPT_DIR / "data" / "e2e"

# A/B 对照（C6① 判决数据）：唯一差异 = candidate_search_enabled——
# 判"第二检索面有没有让签发更贴简历"。加新对照行即可评其他开关。
CONFIGS = {
    "A_bank": {"candidate_search_enabled": False},
    "B_candidate": {"candidate_search_enabled": True},
}


class JudgeItem(BaseModel):
    """单题三轴裁决（1-5）。三轴语义见模块头——与防幻觉三道闸一一对应。"""
    index: int = Field(description="题目在列表中的序号（从 1 起）")
    relevance: int = Field(ge=1, le=5, description="简历相关性：题锚在候选人材料上吗")
    faithfulness: int = Field(ge=1, le=5, description="来源忠实度：题面虚构了材料里没有的事实吗")
    non_repetition: int = Field(ge=1, le=5, description="不重复性：与本组其他题相比换角度了吗")
    note: str = ""


class JudgeReport(BaseModel):
    scores: list[JudgeItem]


class PairwiseVerdict(BaseModel):
    """主判：逐轴胜负 + 总裁决。tie 是合法结果（判相对优劣稳，但真分不出
    就承认分不出——硬判二选一是噪声放大器）。"""
    relevance: str = Field(description="A|B|tie")
    faithfulness: str = Field(description="A|B|tie")
    non_repetition: str = Field(description="A|B|tie")
    overall: str = Field(description="A|B|tie")
    reason: str


def _fixture(fixture_dir: Path) -> dict:
    return {
        "corpus_dir": str(fixture_dir / "corpus"),
        "resume": (fixture_dir / "resume.txt").read_text(encoding="utf-8"),
        "jd": (fixture_dir / "jd.txt").read_text(encoding="utf-8"),
    }


def _config(name: str, corpus_dir: str) -> dict:
    base = {
        "corpus_vendors": "local", "corpus_dir": corpus_dir, "bank_top_k": 5,
        "candidate_vendors": "local", "candidate_top_k": 3,
    }
    base.update(CONFIGS[name])
    return base


def run_config(issue_llm, cfg: dict, fixture: dict, n_issues: int,
               structured_method: str) -> dict:
    """跑一个配置：循环签发 n 题（节点直调，不建图——评的是签发不是图）。"""
    spec = {
        "max_questions": n_issues + 4,        # 余量：兜底/收场不吃掉预算
        "max_consecutive_llm_failures": 3,
        "structured_output_method": structured_method,
        "search_tool": resolve_search_tool(cfg),
        "candidate_tool_factory": resolve_candidate_tool(cfg),
        "max_tool_rounds": 3,
        "max_tool_calls": 5,
    }
    node = make_issue_question(issue_llm, spec)
    state = {
        "position": "后端开发工程师",
        "candidate_profile": fixture["resume"],
        "jd_requirements": fixture["jd"],
        "transcript": [], "issued_keys": [], "topic_results": [],
        "retrieval_trace": [], "consecutive_llm_failures": 0,
        "followup_state": fresh_followup_state(),
    }
    questions = []
    for _ in range(n_issues):
        ret = node(state)
        state.update(ret)
        q = ret.get("current_question") or {}
        if not q:
            break                              # 收场（done/熔断/池尽）
        questions.append(q)
        # 假装每题答了个中等水平：给下一轮签发喂 topic_results（真实信号）
        state["topic_results"] = state["topic_results"] + [{
            "topic": q["topic"],
            "scores": {"technical_depth": 3, "correctness": 3, "communication": 3},
        }]
    return {"questions": questions, "trace": state["retrieval_trace"],
            "transcript": state["transcript"]}


def summarize_run(run: dict) -> dict:
    """留痕归因：两副工具的调用/空命中率 + 签发来源计数 + 兜底计数。
    工具失败（error 键，如题库解析失败）单列 tool_errors——与空命中分账
（"检索弱"和"工具坏了"是两种归因）。"""
    trace = run["trace"]
    by_tool = Counter(r.get("tool", "?") for r in trace)
    empty = Counter(r.get("tool", "?") for r in trace
                    if r.get("empty_hit") and not r.get("error"))
    errors = Counter(r.get("tool", "?") for r in trace if r.get("error"))
    src = Counter(q.get("source_type", "?") for q in run["questions"])
    fallback = sum(1 for e in run["transcript"]
                   if e.get("kind") == "issue" and e.get("fallback_reason"))
    return {
        "questions": len(run["questions"]),
        "tool_calls": dict(by_tool),
        "tool_empty_hits": dict(empty),
        "tool_errors": dict(errors),
        "source_types": dict(src),
        "fallback_used": fallback,
    }


def _judge_prompt(run: dict, fixture: dict, label: str) -> str:
    qlist = "\n".join(
        f"{i}. [{q.get('source_type', '?')}] {q.get('topic', '?')}：{q.get('text', '?')}"
        for i, q in enumerate(run["questions"], start=1))
    return f"""你是模拟面试签发质量评测裁判。请输出 JSON。

【候选人简历】
{fixture['resume']}

【岗位 JD】
{fixture['jd']}

【配置 {label} 本场签发的全部题目】
{qlist}

逐题打三个分（1-5 整数）：
- relevance 简历相关性：题考察的东西在简历/JD 里有落点吗（完全无关=1，精准锚定=5）
- faithfulness 来源忠实度：题面是否虚构了材料里不存在的事实/经历（"我看到你提到X"而简历无 X=1 分）
- non_repetition 不重复性：与本组其他题相比是否换了考察角度（同一件事换个问法=1）
输出 {{"scores": [{{"index": 1, "relevance": 1-5, "faithfulness": 1-5, "non_repetition": 1-5, "note": "一句话"}}]}}，
每题一项，共 {len(run['questions'])} 项。只输出 JSON。"""


def _pairwise_prompt(run_a: dict, run_b: dict, fixture: dict) -> str:
    def _fmt(run):
        return "\n".join(f"- [{q.get('source_type', '?')}] {q.get('topic', '?')}：{q.get('text', '?')}"
                         for q in run["questions"])
    return f"""你是模拟面试签发质量评测裁判。两组题目出自同一简历/JD 的两次独立签发，请判相对优劣。请输出 JSON。

【候选人简历】
{fixture['resume']}

【A 组】
{_fmt(run_a)}

【B 组】
{_fmt(run_b)}

逐轴判 A|B|tie（小写）：relevance 简历相关性 / faithfulness 来源忠实度 /
non_repetition 不重复性，再给 overall 总裁决（三轴外可考虑题目的信息量与
自然度）与一句 reason。只输出 JSON，形状：
{{"relevance": "A|B|tie", "faithfulness": "A|B|tie", "non_repetition": "A|B|tie", "overall": "A|B|tie", "reason": "一句话"}}"""


class MockJudgeLLM(MockLLM):
    """mock 自检的裁判：绝对分恒 4/5/4，pairwise 恒 tie——量的是管道不是科学。"""

    def invoke(self, prompt, config=None, **kwargs):
        from langchain_core.messages import AIMessage
        p = str(prompt)
        if "签发质量评测裁判" in p:
            n = p.count("- [") + p.count(". [") if "逐题打三个分" in p else 0
            if "逐轴判" in p:
                return AIMessage(content=json.dumps({
                    "relevance": "tie", "faithfulness": "tie",
                    "non_repetition": "tie", "overall": "tie",
                    "reason": "（mock）管道自检：平局",
                }, ensure_ascii=False))
            # 绝对分腿：数不出题数就回 1 项（自检对形状不对数量较真）
            scores = [{"index": i, "relevance": 4, "faithfulness": 5,
                       "non_repetition": 4, "note": "（mock）"}
                      for i in range(1, max(n, 1) + 1)]
            return AIMessage(content=json.dumps({"scores": scores}, ensure_ascii=False))
        return super().invoke(prompt, config, **kwargs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="签发质量端到端评测（LLM-judge）")
    ap.add_argument("--provider", default="zhipu", choices=["zhipu", "mock"],
                    help="mock=离线自检（管道校验，数字无意义）")
    ap.add_argument("--issues", type=int, default=5, help="每个配置签发题数")
    ap.add_argument("--fixture", default=str(FIXTURE_DIR))
    ap.add_argument("--out-dir", default=str(SCRIPT_DIR / "data"))
    args = ap.parse_args(argv)

    fixture = _fixture(Path(args.fixture))
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if args.provider == "mock":
        issuer = MockLLM("quick")
        judge = MockJudgeLLM("deep")
        method = "function_calling"     # 走哪条腿都行——mock 无该方法是降级链的正路
    else:
        from interviewer.default_config import get_config
        cfg = get_config()
        issuer = create_llm_client("quick", cfg)
        judge = create_llm_client("deep", cfg)
        method = cfg["structured_output_method"]

    runs = {}
    for name in CONFIGS:
        print(f"── 配置 {name}：签发 {args.issues} 题 …")
        runs[name] = run_config(issuer, _config(name, fixture["corpus_dir"]),
                                fixture, args.issues, method)
        s = summarize_run(runs[name])
        print(f"   {s['questions']} 题｜来源 {s['source_types']}｜"
              f"工具 {s['tool_calls']}｜空命中 {s['tool_empty_hits']}")

    # 绝对分（每配置一次 listwise 调用——不重复轴要看到整组才判得了）
    judged = {}
    for name, run in runs.items():
        report = invoke_structured_or_freetext(
            judge, JudgeReport, _judge_prompt(run, fixture, name), method=method)
        judged[name] = report.model_dump() if report else None
        if report is None:
            print(f"⚠ {name} 绝对分裁决失败（降级腿也炸）——留空继续", file=sys.stderr)

    # 主判 pairwise（两个配置都在场才有对照）
    names = list(CONFIGS)
    pairwise = invoke_structured_or_freetext(
        judge, PairwiseVerdict,
        _pairwise_prompt(runs[names[0]], runs[names[1]], fixture), method=method)
    if pairwise is None:
        print("⚠ pairwise 裁决失败", file=sys.stderr)

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provider": args.provider, "issues_per_config": args.issues,
        "configs": {n: summarize_run(r) for n, r in runs.items()},
        "questions": {n: r["questions"] for n, r in runs.items()},
        "absolute": judged,
        "pairwise": pairwise.model_dump() if pairwise else None,
    }
    out_path = Path(args.out_dir) / f"issue_e2e_{args.provider}_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n== 归因摘要 ==")
    for n in names:
        s = out["configs"][n]
        print(f"{n}: {s}")
    if pairwise:
        print(f"pairwise（{names[0]} vs {names[1]}）: overall={pairwise.overall}｜{pairwise.reason}")
    print(f"\n结果已沉淀：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
