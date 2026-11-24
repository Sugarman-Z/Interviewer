"""IssueQuestion 节点单测（节点级：直接调节点函数、检查返回的 update dict）。
与 test_smoke（整图冒烟）的分工：这里不建图，图形状/路由归冒烟管。
FakeLLM 只需 duck-type 一个 .invoke——没有 with_structured_output 方法，
invoke_structured_or_freetext 会捕获 AttributeError 降级到 freetext 路径解析
JSON——顺带每次都回归了降级链本身。覆盖的出口（与 issue_question.py
docstring 的 ①-⑦ 一一对应）：预算① / done③ / 正常⑥ / 查重⑤ / 空转⑦ /
熔断⑦ / 池尽⑦ / 失败计数⑦。
"""
import json

from interviewer.agents.issue_question import (
    _render_topic_results,
    make_issue_question,
)
from interviewer.agents.utils.agent_states import fresh_followup_state

SPEC = {
    "max_questions": 3,
    "max_consecutive_llm_failures": 2,
    "structured_output_method": "function_calling",
}


class _Msg:
    def __init__(self, content):
        self.content = content


class FakeOkLLM:
    """返回合法单题 JSON（字段可注入：done/dup/空 topic 都靠它造场景）。"""

    def __init__(self, **fields):
        self.payload = {
            "done": False,
            "reason": "测试签发",
            "module": "project",
            "topic": "新考点",
            "text": "新问题：聊聊缓存？",
            "source_type": "generated",
            "source_ref": "测试依据",
            "followup_hints": [],
        }
        self.payload.update(fields)

    def invoke(self, prompt, **kwargs):
        return _Msg(json.dumps(self.payload, ensure_ascii=False))


class FakeFailLLM:
    """invoke 永远抛错 + 调用计数（预算测试要断言'零调用'）。"""

    def __init__(self):
        self.calls = 0

    def invoke(self, prompt, **kwargs):
        self.calls += 1
        raise RuntimeError("api down")


def make_state(issued=None, failures=0, trace=None):
    """最小节点输入。followup_state 故意带脏值（旧考点/旧历史/计数 2）：
    换题必须把它重置——这是断言点之一。trace=已有检索留痕（测累加纪律用）。"""
    return {
        "position": "后端开发",
        "candidate_profile": "做过库存 SaaS",
        "jd_requirements": "",
        "transcript": [{"role": "candidate", "content": "我是小明，做过库存 SaaS。"}],
        "issued_keys": issued or [],
        "consecutive_llm_failures": failures,
        "topic_results": [],
        "retrieval_trace": trace or [],
        "followup_state": {
            "topic": "旧考点",
            "qa_history": "旧的问答史",
            "followup_count": 2,
            "total_count": 7,
        },
    }


def node(llm):
    return make_issue_question(llm, SPEC)


# ⑥ 正常签发
def test_normal_issue_full_writes():
    ret = node(FakeOkLLM(topic="新考点", text="聊聊缓存？"))(make_state())
    assert ret["pending_question"] == "聊聊缓存？"
    assert ret["current_topic"] == "新考点"
    assert ret["current_question"]["source_ref"] == "测试依据"
    assert ret["issued_keys"] == ["新考点"]                 # 整块新列表，不是 append
    assert ret["consecutive_llm_failures"] == 0            # 成功清零
    # 换题重置追问工作区，total_count 保留
    assert ret["followup_state"] == {
        "topic": "新考点", "qa_history": "", "followup_count": 0, "total_count": 7
    }
    entry = ret["transcript"][-1]
    assert entry["role"] == "interviewer" and entry["kind"] == "issue"


# ③ done 哨兵
def test_done_sentinel_clears_state():
    ret = node(
        FakeOkLLM(done=True, reason="覆盖足够", topic="(占位)", text="(占位)")
    )(make_state())
    assert ret["current_question"] == {} and ret["current_topic"] == ""
    assert ret["pending_question"] == ""                   #：显式清场，不留幽灵题
    assert "issued_keys" not in ret                        # 收场不落账
    done_entry = ret["transcript"][-1]
    assert done_entry["kind"] == "issue_done" and "覆盖足够" in done_entry["content"]


# ⑤ 查重：模型走神 → 兜底池顶上（不算 LLM 失败）
def test_duplicate_topic_uses_fallback_and_resets_counter():
    ret = node(FakeOkLLM(topic="项目概览"))(make_state(issued=["项目概览"]))
    assert ret["current_topic"] == "难点深挖"              # 池里第一个未问过的
    assert ret["consecutive_llm_failures"] == 0            # 调用成功，只是走神
    assert ret["issued_keys"] == ["项目概览", "难点深挖"]


def test_duplicate_with_pool_exhausted_ends():
    ret = node(FakeOkLLM(topic="项目概览"))(
        make_state(issued=["项目概览", "难点深挖"])
    )
    assert ret["current_question"] == {}                   # done 收场
    assert "兜底池已尽" in ret["transcript"][-1]["content"]


# ⑦ 失败路径：兜底落账计数 / 熔断 / 池尽
def test_llm_failure_uses_fallback_and_counts():
    ret = node(FakeFailLLM())(make_state())
    assert ret["current_topic"] == "项目概览"              # 兜底第一题
    assert ret["consecutive_llm_failures"] == 1            # 失败落账（下次可熔断）


def test_fallback_entries_carry_downgrade_reason():
    """六节顺路件：兜底落账的 transcript 条目带降级原因——终场报告
    要看得出哪些题是 LLM 失败后兜底的（MVP_PLAN fail-visible）。
    正常签发不带该键（不是兜底就不是兜底）。"""
    ret0 = node(FakeOkLLM())(make_state())
    assert "fallback_reason" not in ret0["transcript"][-1]

    ret1 = node(FakeFailLLM())(make_state())
    assert ret1["transcript"][-1]["fallback_reason"] == "LLM 签发失败"

    ret2 = node(FakeOkLLM(topic="项目概览"))(make_state(issued=["项目概览"]))
    assert ret2["transcript"][-1]["fallback_reason"].startswith("考点重复")


def test_breaker_trips_on_second_consecutive_failure():
    """熔断必须读 state 旧值——函数内初始化 0 会让它永远不触发（用户版抓到的 bug）。"""
    ret = node(FakeFailLLM())(make_state(failures=1))      # 上次已失败 1 次
    assert ret["current_question"] == {}                   # 1+1>=2 → 熔断收场
    assert "熔断" in ret["transcript"][-1]["content"]


def test_failure_with_pool_exhausted_ends():
    ret = node(FakeFailLLM())(make_state(issued=["项目概览", "难点深挖"]))
    assert ret["current_question"] == {}
    assert "兜底池已尽" in ret["transcript"][-1]["content"]


# ① 预算保险丝：确定性优先，LLM 零调用
def test_budget_fuse_skips_llm_entirely():
    llm = FakeFailLLM()
    ret = node(llm)(make_state(issued=["a", "b", "c"]))    # 已达 max_questions=3
    assert ret["current_question"] == {}
    assert "题数上限" in ret["transcript"][-1]["content"]
    assert llm.calls == 0                                  # 预算尽 → LLM 必须零调用


# ④ 业务校验：done=False 而 topic 为空 = 空转，按失败处理
def test_empty_topic_treated_as_failure():
    ret = node(FakeOkLLM(topic="   ", text="x"))(make_state())
    assert ret["current_topic"] == "项目概览"              # 走兜底
    assert ret["consecutive_llm_failures"] == 1            # 且计为失败


# 渲染辅助
def test_render_topic_results():
    assert _render_topic_results([]) == "（尚未开考）"
    one = _render_topic_results(
        [{"topic": "缓存", "scores": {"technical_depth": 2, "correctness": 3, "communication": 4}}]
    )
    assert one == "缓存｜均分 3.0"
    assert _render_topic_results([{"topic": "x", "scores": {}}]) == "x｜均分 无分"


# 工具路径：检索 → 核对编号 → 改字段/判失败

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from interviewer.agents.issue_question import _stamp_bank  # noqa: E402
from interviewer.agents.schemas import IssuedQuestion  # noqa: E402
from interviewer.dataflows import resolve_search_tool  # noqa: E402
from interviewer.dataflows.bank_schema import BankQuestion  # noqa: E402
from interviewer.dataflows.bank_tool import NO_RESULTS_EXCLUDED  # noqa: E402

_TOOL_BLOCK = """---
id: bank-t-201
topic: Kafka 高可用
difficulty: 3
tags: kafka

**题目**
三节点集群 leader 宕机时如何保证消息不丢？

**追问树**
- ISR 与 acks？

**评分锚点**
- 5 分：说清联动
"""


class FakeToolLLM:
    """按剧本逐拍回放（支持 bind_tools 透传），记录每拍收到的消息。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        self.seen.append(list(messages))
        self.calls += 1
        return self.script.pop(0)


class _FakeFace:
    """模型面替身：只需 .name 当分派键（FakeToolLLM 的 bind_tools 不消费 schema）。"""

    def __init__(self):
        self.name = "search_corpus"


class FakeBoundSearch:
    """诚实的循环期替身（LoopTool 形状）：execute 记账 + 回假 markdown。"""

    def __init__(self, entries):
        self.entries = list(entries)
        self.registry = {}

    def as_tool(self):
        return _FakeFace()

    def execute(self, args):
        self.registry.update({e.id: e for e in self.entries})
        query = str(args.get("query", "")).strip()
        return "[fake md]", {
            "query": query,
            "hit_ids": [e.id for e in self.entries],
            "elapsed_ms": 0.5,
            "empty_hit": not self.entries,
        }


class FakeSearchTool:
    """固定返回指定命中，绕过真实检索——专测节点内部逻辑（改字段顺序/失败路由）。
    节点经 for_call 拿适配器入环，本替身照抄该形状。"""

    def __init__(self, entries):
        self.entries = list(entries)

    def for_call(self, exclude_topics=()):
        return FakeBoundSearch(self.entries)


def _search_msg(query):
    return AIMessage(content="", tool_calls=[
        {"name": "search_corpus", "args": {"query": query}, "id": "c1"}])


def _submit_msg(**fields):
    payload = {
        "done": False, "reason": "t", "module": "project", "topic": "模型乱起的名",
        "text": "贴合简历的改编题面", "source_type": "generated",
        "source_ref": "x", "followup_hints": [],
    }
    payload.update(fields)
    return AIMessage(content="", tool_calls=[
        {"name": "IssuedQuestion", "args": payload, "id": "c2"}])


def _tool_node(llm, tmp_path, spec_over=None):
    d = tmp_path / "project"
    d.mkdir(exist_ok=True)  # 同一测试内可能重复搭建
    (d / "a.md").write_text(_TOOL_BLOCK, encoding="utf-8")
    tool = resolve_search_tool(
        {"corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    )
    spec = dict(SPEC, search_tool=tool, max_tool_rounds=2, **(spec_over or {}))
    return make_issue_question(llm, spec)


def test_tool_path_issues_stamped_bank_question(tmp_path):
    """命中并被采用：字段改成题库原值，模型改编的题面保留。"""
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(bank_qid="bank-t-201", source_type="bank", source_ref="y"),
    ])
    ret = _tool_node(llm, tmp_path)(make_state())

    q = ret["current_question"]
    assert ret["current_topic"] == "Kafka 高可用"          # 考点名 = 题库原值
    assert q["source_type"] == "bank"
    assert q["source_ref"].startswith("bank:bank-t-201")
    assert q["text"] == "贴合简历的改编题面"                  # 模型改编保留
    assert q["followup_hints"] == ["ISR 与 acks？"]           # 题库追问列表补上
    assert ret["issued_keys"] == ["Kafka 高可用"]
    # prompt 契约：有工具时拼上 ADDENDUM（工具名进了模型视野）
    assert "search_corpus(" in llm.seen[0][0].content


def test_corrupt_bank_falls_through_to_generated(tmp_path):
    """题库解析失败端到端锁：循环喂 BANK_CORRUPTED
    哨兵 → 模型按材料交卷 → 节点正常签发 generated 题——面试流程零阻塞；
    留痕带 error 键（降级归因进 session JSON）。"""
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_TOOL_BLOCK, encoding="utf-8")
    tool = resolve_search_tool(
        {"corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5}
    )
    (d / "b.md").write_text(_TOOL_BLOCK, encoding="utf-8")   # 同 id → 坏库
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(),
    ])
    spec = dict(SPEC, search_tool=tool, max_tool_rounds=2)
    ret = make_issue_question(llm, spec)(make_state())

    assert ret["current_question"]["source_type"] == "generated"
    assert ret["issued_keys"] == ["模型乱起的名"]
    rec = ret["retrieval_trace"][0]
    assert rec["error"] == "corpus_parse_failed" and rec["hit_ids"] == []


def test_prompt_addendum_absent_without_tool():
    """无工具路径的 prompt 不含工具说明（spec 缺 search_tool = 老路径）。"""

    class CapturingLLM(FakeOkLLM):
        def __init__(self, **f):
            super().__init__(**f)
            self.prompts = []

        def invoke(self, prompt, **kwargs):
            self.prompts.append(str(prompt))
            return super().invoke(prompt, **kwargs)

    llm = CapturingLLM()
    node(llm)(make_state())
    assert "search_corpus" not in llm.prompts[0]


def test_stamp_runs_before_dedup():
    """改字段在查重之前：即使检索返回了已考考点的题，查重仍拦得住。"""
    entry = BankQuestion(id="bank-t-201", topic="Kafka 高可用", text="库内题面",
                         followup_tree=["追问A"], source="")
    spec = dict(SPEC, search_tool=FakeSearchTool([entry]), max_tool_rounds=2)
    # 剧本必须先有一拍检索（清单靠工具执行才填充），再交卷报编号
    llm = FakeToolLLM([_search_msg("Kafka 宕机"), _submit_msg(bank_qid="bank-t-201")])
    ret = make_issue_question(llm, spec)(make_state(issued=["Kafka 高可用"]))

    assert ret["current_topic"] == "项目概览"               # 兜底池第一个未问过的
    assert ret["consecutive_llm_failures"] == 0
    assert ret["issued_keys"] == ["Kafka 高可用", "项目概览"]


def test_fabricated_bank_qid_counts_failure_and_falls_back(tmp_path):
    """报了检索清单里没有的编号 = 来源造假：走兜底并计失败。"""
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(bank_qid="bank-编造-999"),
    ])
    ret = _tool_node(llm, tmp_path)(make_state())
    assert ret["current_topic"] == "项目概览"                # 兜底第一题
    assert ret["consecutive_llm_failures"] == 1


def test_fabricated_bank_qid_breaks_on_second(tmp_path):
    llm = FakeToolLLM([
        _search_msg("Kafka"),
        _submit_msg(bank_qid="bank-编造-999"),
    ])
    ret = _tool_node(llm, tmp_path)(make_state(failures=1))
    assert ret["current_question"] == {}
    assert "编造题库命中" in ret["transcript"][-1]["content"]


def test_claimed_bank_without_qid_downgraded(tmp_path):
    """没报编号但 source_type 自称 bank：改回 generated，题目照用、不算失败。"""
    llm = FakeToolLLM([_submit_msg(source_type="bank")])
    ret = _tool_node(llm, tmp_path)(make_state())
    assert ret["current_question"]["source_type"] == "generated"
    assert ret["current_topic"] == "模型乱起的名"            # 题目本体照常签发
    assert ret["consecutive_llm_failures"] == 0


def test_issued_topic_excluded_from_search(tmp_path):
    """已考考点检索时就该被排除：命中为空 → 回喂 EXCLUDED 哨兵 三态）；
    此时再报编号必判编造——二次探测命中的已考考点不进防幻觉账本。"""
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(bank_qid="bank-t-201"),   # 检索被排除后账本里没有它
    ])
    ret = _tool_node(llm, tmp_path)(make_state(issued=["Kafka 高可用"]))

    fed = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    assert fed[0].content.startswith(NO_RESULTS_EXCLUDED)
    assert ret["consecutive_llm_failures"] == 1              # 编造 = 失败（账本不含已考考点）


# 检索留痕

def test_retrieval_trace_accumulates_across_issues(tmp_path):
    """留痕跨签发累加：读旧值整块拼接（无 reducer 纪律，同 issued_keys）。"""
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(bank_qid="bank-t-201", source_type="bank", source_ref="y"),
    ])
    ret = _tool_node(llm, tmp_path)(make_state())
    assert ret["retrieval_trace"][0]["query"] == "Kafka 宕机"
    assert ret["retrieval_trace"][0]["hit_ids"] == ["bank-t-201"]

    llm2 = FakeToolLLM([
        _search_msg("缓存 一致性"),
        _submit_msg(bank_qid="bank-t-201", source_type="bank", source_ref="y"),
    ])
    ret2 = _tool_node(llm2, tmp_path)(make_state(trace=ret["retrieval_trace"]))
    assert [r["query"] for r in ret2["retrieval_trace"]] == ["Kafka 宕机", "缓存 一致性"]


def test_retrieval_trace_preserved_without_tool():
    """无工具老路径：本轮零检索——留痕不新增，历史也不清空。"""
    old = [{"query": "旧查询", "hit_ids": ["bank-x"], "elapsed_ms": 1.0, "empty_hit": False}]
    ret = node(FakeOkLLM(topic="新考点", text="q"))(make_state(trace=old))
    assert ret["retrieval_trace"] == old


def test_stamp_bank_unit_three_cases():
    """_stamp_bank 单元三分支（节点测试之外的白盒锁定）。"""
    hit = BankQuestion(id="bank-x", topic="库内考点", text="库内题面",
                       followup_tree=["追问1"], source="出处A")
    r1 = IssuedQuestion(topic="模型名", text="改编", bank_qid="bank-x")
    d, h = _stamp_bank(r1, {"bank-x": hit})
    assert not h and d["topic"] == "库内考点"
    assert d["source_ref"] == "bank:bank-x｜出处A"
    assert d["followup_hints"] == ["追问1"]

    r2 = IssuedQuestion(topic="t", text="q", source_type="bank")
    d2, h2 = _stamp_bank(r2, {})
    assert not h2 and d2["source_type"] == "generated"

    r3 = IssuedQuestion(topic="t", text="q", bank_qid="不存在")
    assert _stamp_bank(r3, {"bank-x": hit}) == (None, True)


def test_literal_null_string_treated_as_empty(tmp_path):
    """真 API 事故回放（2026-09-20 熔断）：模型把 "null" 当字符串填进 bank_qid，
    提示词旧措辞"填 null"诱导——必须视为未填，正常出题、不计失败。"""
    llm = FakeToolLLM([_submit_msg(bank_qid="null")])
    ret = _tool_node(llm, tmp_path)(make_state())
    assert ret["current_topic"] == "模型乱起的名"            # 题目照常签发
    assert ret["current_question"]["bank_qid"] is None        # state 不留脏值
    assert ret["consecutive_llm_failures"] == 0               # 不算编造、不计数

    # 自称 bank + 字符串 "null"：降级路径照走
    llm2 = FakeToolLLM([_submit_msg(bank_qid="NULL", source_type="bank")])
    ret2 = _tool_node(llm2, tmp_path)(make_state())
    assert ret2["current_question"]["source_type"] == "generated"
    assert ret2["consecutive_llm_failures"] == 0


# ：第二检索面（cand 引用对账）+ 评分锚点安全闸

from interviewer.dataflows import resolve_candidate_tool  # noqa: E402
from interviewer.agents.issue_question import (  # noqa: E402
    LEAK_ANCHOR_BIGRAM_RECALL,
    _anchor_leak,
)

_CAND_CFG = {"candidate_search_enabled": True, "candidate_vendors": "local",
             "candidate_top_k": 3}


def _cand_msg(query):
    return AIMessage(content="", tool_calls=[
        {"name": "search_candidate", "args": {"query": query}, "id": "c0"}])


def test_candidate_tool_in_loop_and_verified_ref_kept(tmp_path):
    """工厂存在 → 段落工具入环：模型可调 search_candidate 查简历原文，
    source_ref 里核实过的 cand 编号原样保留，留痕按 tool 键区分两副工具。"""
    llm = FakeToolLLM([
        _cand_msg("库存 SaaS"),
        _submit_msg(source_ref="简历第1段（cand-0001）做过库存 SaaS"),
    ])
    spec = dict(SPEC, search_tool=resolve_search_tool(
        {"corpus_vendors": "local", "corpus_dir": str(tmp_path), "bank_top_k": 5}),
        candidate_tool_factory=resolve_candidate_tool(_CAND_CFG),
        max_tool_rounds=2)
    ret = make_issue_question(llm, spec)(make_state())

    assert ret["current_topic"] == "模型乱起的名"             # 题目照常签发
    ref = ret["current_question"]["source_ref"]
    assert "cand-0001" in ref and "未核实" not in ref          # 核实过的编号保留
    cand_trace = [r for r in ret["retrieval_trace"] if r.get("tool") == "search_candidate"]
    assert cand_trace and cand_trace[0]["hit_ids"] == ["cand-0001"]
    # prompt 契约：工厂存在时拼上 candidate ADDENDUM（工具名进了模型视野）
    assert "search_candidate(" in llm.seen[0][0].content


def test_unknown_cand_ref_sanitized():
    """没查就报编号 = 幻觉引用：摘除编号、标注未核实——题目本体照常签发
（严重度分级：cand 编造 ≠ bank 编造，不进失败梯）。"""
    llm = FakeToolLLM([
        _submit_msg(source_ref="简历第9段（cand-0099）虚构的性能数字"),
    ])
    spec = dict(SPEC, search_tool=FakeSearchTool([]), max_tool_rounds=2,
                candidate_tool_factory=resolve_candidate_tool(_CAND_CFG))
    ret = make_issue_question(llm, spec)(make_state())

    ref = ret["current_question"]["source_ref"]
    assert "cand-0099" not in ref and "未核实引用" in ref
    assert ret["consecutive_llm_failures"] == 0                # 修数据不判失败


def test_sanitize_regex_edge_cases():
    """对账正则边界（2026-11-17 审查实测复现的绕过路径）：5 位编造编号
    不得以 4 位前缀命中账本放行；同一未知编号多次出现全数摘除。"""
    from interviewer.agents.issue_question import _sanitize_candidate_refs

    ledger = {"cand-0001": object()}
    d = _sanitize_candidate_refs(
        {"source_ref": "简历第1段（cand-00012）虚构的延迟数字"}, ledger)
    assert "cand-00012" not in d["source_ref"]
    assert "未核实引用" in d["source_ref"]
    # 同一未知编号出现两次：str.replace 全换，不留残骸
    d2 = _sanitize_candidate_refs({"source_ref": "cand-0099 与 cand-0099"}, {})
    assert d2["source_ref"].count("未核实引用") == 2


def test_candidate_only_path_without_bank_tool(tmp_path):
    """bank 面关闭 + candidate 面开启（合法组合，2026-11-17 审查修正的
    合同漏洞）：循环只绑段落工具、prompt 只宣告 search_candidate——
    "说了有工具就必须在环里"双向成立（旧写法此组合下 prompt 宣告工具
    却走零工具路径，工具永远绑不进环）。"""
    llm = FakeToolLLM([
        _cand_msg("库存 SaaS"),
        _submit_msg(source_ref="简历第1段（cand-0001）做过库存 SaaS"),
    ])
    spec = dict(SPEC, candidate_tool_factory=resolve_candidate_tool(_CAND_CFG),
                max_tool_rounds=2)
    ret = make_issue_question(llm, spec)(make_state())

    assert "search_candidate(" in llm.seen[0][0].content
    assert "search_corpus(" not in llm.seen[0][0].content
    assert ret["current_topic"] == "模型乱起的名"
    assert "cand-0001" in ret["current_question"]["source_ref"]
    assert any(r.get("tool") == "search_candidate" for r in ret["retrieval_trace"])


def test_candidate_addendum_absent_without_factory(tmp_path):
    """工厂缺席（默认关）= prompt 不提第二工具、环里也没有它——说了有工具
    工具就必须真的在环里，反之亦然（prompt 与代码同一份合同）。"""
    llm = FakeToolLLM([_submit_msg()])
    ret = _tool_node(llm, tmp_path)(make_state())
    assert "search_candidate" not in llm.seen[0][0].content
    assert ret["current_topic"] == "模型乱起的名"             # 老路径零回归


def test_anchor_leak_unit():
    """泄漏闸手算锁死：逐字抄录 ≈1.0 判漏、正常改编远低于阈值放行、
    两三个 bigram 的短锚点豁免（不构成答案钥匙，卡它只会误伤）。"""
    anchor = "说清楚三节点集群的副本同步机制与消息不丢的保障"
    # 抄录（多一个"请"字前缀）：anchor 的 bigram 全部在场 → recall 1.0
    assert _anchor_leak(f"请{anchor}", [anchor]) is True
    # 同话题改编：只有"副本/本同/同步/步机/机制"等零星 bigram 撞上 → ≈0.2
    assert _anchor_leak("说说副本同步机制就行", [anchor]) is False
    # 完全无关
    assert _anchor_leak("讲讲 Kafka 高可用架构", [anchor]) is False
    # 短锚点豁免：全包含也不判（"ISR" 只有 2 个 bigram < 3）
    assert _anchor_leak("ISR", ["ISR"]) is False
    # 多锚点任一超线即漏
    assert _anchor_leak("请说清楚三节点集群的副本同步机制与消息不丢的保障",
                        ["无关锚点", anchor]) is True
    assert LEAK_ANCHOR_BIGRAM_RECALL == 0.6                    # 阈值数值锁
    # 边界锁（2026-11-17 审查补）：>= 语义与判定区间下沿——5-bigram 锚点
    # 命中恰 3/5（=0.6）判漏、2/5（=0.4）放行；改 > 或调阈值此处当场红
    a5 = "甲乙丙丁戊己"
    assert _anchor_leak("甲乙丙丁", [a5]) is True             # 3/5 = 0.6 恰在阈值
    assert _anchor_leak("甲乙丙Z", [a5]) is False             # 2/5 = 0.4


def test_anchor_leak_falls_back_and_counts():
    """泄漏负样本（验收条款）：题面抄了答案钥匙 → 兜底池落账 + 计失败，
    transcript 带降级原因（fail-visible）。"""
    entry = BankQuestion(
        id="bank-t-201", topic="Kafka 高可用", text="库内题面",
        scoring_anchors=["五分答案要说清楚 ISR 与 acks 与副本联动的完整机制"])
    spec = dict(SPEC, search_tool=FakeSearchTool([entry]), max_tool_rounds=2)
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(bank_qid="bank-t-201",
                    text="请说清楚 ISR 与 acks 与副本联动的完整机制"),
    ])
    ret = make_issue_question(llm, spec)(make_state())
    assert ret["current_topic"] == "项目概览"                  # 兜底第一题
    assert ret["consecutive_llm_failures"] == 1
    assert "评分锚点泄漏" in ret["transcript"][-1].get("fallback_reason", "")


def test_anchor_leak_breaks_on_second():
    entry = BankQuestion(id="bank-t-201", topic="Kafka 高可用", text="库内题面",
                         scoring_anchors=["五分答案要说清楚 ISR 与 acks 与副本联动的完整机制"])
    spec = dict(SPEC, search_tool=FakeSearchTool([entry]), max_tool_rounds=2)
    llm = FakeToolLLM([
        _search_msg("Kafka"),
        _submit_msg(bank_qid="bank-t-201",
                    text="请说清楚 ISR 与 acks 与副本联动的完整机制"),
    ])
    ret = make_issue_question(llm, spec)(make_state(failures=1))
    assert ret["current_question"] == {}                       # 熔断收场
    assert "评分锚点泄漏熔断" in ret["transcript"][-1]["content"]


def test_benign_bank_adaptation_not_flagged(tmp_path):
    """正常改编不误伤：题库命中 + 题面正常改写（锚点 "5 分：说清联动" 的
    bigram 与题面零重叠）→ 原路签发，不进失败梯（回归 test_tool_path 的
    锚点存在版——泄漏闸不能把好签发误杀）。"""
    llm = FakeToolLLM([
        _search_msg("Kafka 宕机"),
        _submit_msg(bank_qid="bank-t-201", source_type="bank", source_ref="y"),
    ])
    ret = _tool_node(llm, tmp_path)(make_state())
    assert ret["current_topic"] == "Kafka 高可用"
    assert ret["consecutive_llm_failures"] == 0
