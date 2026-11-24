"""IssueQuestion（发题官）：全图唯一有权签发新题的节点；预算/查重/降级梯/熔断
全在节点内确定性判断，条件边只读 current_question 有无。

纪律：无 reducer 字段整块返回新列表（in-place append 会把 None 写进 state）；
熔断计数读旧值再写（跨调用累计）；换题重置 followup_state 但保留 total_count；
bank_qid/cand 引用一律过检索账本对账——不在账本 = 编造。
"""
import re

from interviewer.agents.fallback_bank import next_unused_fallback
from interviewer.agents.prompts import ISSUE_QUESTION_SYSTEM
from interviewer.agents.prompts.issue_question import (
    ISSUE_QUESTION_CANDIDATE_ADDENDUM,
    ISSUE_QUESTION_TOOLS_ADDENDUM,
)
from interviewer.agents.schemas import SCORE_DIMS, IssuedQuestion
from interviewer.agents.utils.agent_states import fresh_followup_state
from interviewer.agents.utils.agent_utils import (
    answer_or_marker,
    append_transcript,
    clip,
    first_candidate_text,
)
from interviewer.agents.utils.structured import (
    invoke_structured_or_freetext,
    invoke_structured_with_tools,
)


def make_issue_question(llm, spec: dict | None = None):
    """spec 必需键（[] 直取，缺键即炸）：max_questions、
    max_consecutive_llm_failures、structured_output_method。
    可选键 .get（存在性即开关）：search_tool、max_tool_rounds、
    candidate_tool_factory（工厂而非工具本体，简历/JD 全文在运行期 state）。
    """
    search_tool = (spec or {}).get("search_tool")
    candidate_tool_factory = (spec or {}).get("candidate_tool_factory")
    max_tool_rounds = (spec or {}).get("max_tool_rounds", 3)
    # 调用数硬顶：与轮数保险丝独立；.get 缺省 None = 不限
    max_tool_calls = (spec or {}).get("max_tool_calls")

    def issue_question_node(state: dict) -> dict:
        issued_keys = state.get("issued_keys") or []
        failures = state.get("consecutive_llm_failures") or 0  # 熔断跨调用累计：读旧值

        # ① 预算保险丝（确定性，先于一切 LLM 调用——预算尽时 LLM 必须零调用）
        if len(issued_keys) >= spec["max_questions"]:
            return _done(state, f"题数上限（{spec['max_questions']} 题）")

        # ② 签发通道：任一检索工具存在 → 工具循环；双 None → 无工具老路径。
        # 两副工具独立开关：只看 search_tool 会在"bank 关、candidate 开"时
        # 让 prompt 宣告了工具却绑不进环。查重硬约束烙在适配器闭包里，模型不可见
        prompt = _build_prompt(state, issued_keys, spec)
        if search_tool is None and candidate_tool_factory is None:
            result = invoke_structured_or_freetext(
                llm, IssuedQuestion, prompt, method=spec["structured_output_method"]
            )
            registry: dict = {}
            cand_registry: dict = {}
            round_trace: list = []  # 无工具路径：本轮零检索，留痕只保留历史
        else:
            # 单次调用适配器：exclude_topics=issued_keys 闭包随行、新账本随行——
            # 跨签发复用 = 脏账（旧命中混入对账表），每轮必新造
            tools = []
            bound = None
            if search_tool is not None:
                bound = search_tool.for_call(exclude_topics=tuple(issued_keys))
                tools.append(bound)
                # 第二检索面：工厂拿 state 全文现造段落适配器——prompt
            # 摘录只有 1500 字，工具面给模型按需查全文的能力（真问题④）
            cand_bound = None
            if candidate_tool_factory is not None:
                cand_bound = candidate_tool_factory({
                    "resume": state.get("candidate_profile") or "",
                    "jd": state.get("jd_requirements") or "",
                })
                tools.append(cand_bound)
            result, round_trace = invoke_structured_with_tools(
                llm, IssuedQuestion, prompt, tools,
                max_rounds=max_tool_rounds, max_calls=max_tool_calls,
            )
            # 防幻觉账本：出环自取（循环不搬运领域形状）；缺席的工具账本为空
            registry = bound.registry if bound is not None else {}
            cand_registry = cand_bound.registry if cand_bound is not None else {}

        # ③ done 哨兵：模型建议收场（预算/熔断仍由 ①⑤ 代码强制，模型无权绕过）
        if result is not None and result.done:
            return _done(state, result.reason or "模型判断覆盖已足够", round_trace=round_trace)

        # ④ 业务校验：schema 合法 ≠ 业务合法——done=False 而 topic/text 为空 = 空转
        llm_ok = result is not None and result.topic.strip() and result.text.strip()

        # ⑤ 盖帽 + 查重（顺序敏感：先用库内 topic 锚盖掉模型的改名，再查重——
        # 顺序反了模型改个考点名就能绕开 issued_keys）
        if llm_ok:
            stamped, hallucinated = _stamp_bank(result, registry)
            if hallucinated:
                # 编造题库命中并入⑦失败梯（fail-visible：reason 原样进 transcript）
                if failures + 1 >= spec["max_consecutive_llm_failures"]:
                    return _done(
                        state,
                        f"编造题库命中熔断（bank_qid={result.bank_qid}，连续 {failures + 1} 次）",
                        round_trace=round_trace,
                    )
                fallback = next_unused_fallback(issued_keys)
                if fallback is None:
                    return _done(
                        state,
                        f"编造题库命中（bank_qid={result.bank_qid}）且兜底池已尽",
                        round_trace=round_trace,
                    )
                return _issue(
                    fallback, state, failures=failures + 1, round_trace=round_trace,
                    fallback_reason=f"编造题库命中（bank_qid={result.bank_qid}）",
                )
                # ④.6 评分锚点泄漏闸：只对题库命中生效——
            # 命中题的答案钥匙与题面 bigram 重叠超阈值 = 题面抄了锚点，并入
            # 失败梯。锚点已不进模型视野（bank_tool._format_entry 最小知识），
            # 本闸是纵深防御：将来任何路径把锚点带回模型，这里兜底
            qid = _normalized_qid(result)
            if qid and qid in registry and _anchor_leak(
                    result.text, registry[qid].scoring_anchors):
                if failures + 1 >= spec["max_consecutive_llm_failures"]:
                    return _done(
                        state,
                        f"评分锚点泄漏熔断（bank_qid={qid}，连续 {failures + 1} 次）",
                        round_trace=round_trace,
                    )
                fallback = next_unused_fallback(issued_keys)
                if fallback is None:
                    return _done(
                        state, f"评分锚点泄漏（bank_qid={qid}）且兜底池已尽",
                        round_trace=round_trace,
                    )
                return _issue(
                    fallback, state, failures=failures + 1, round_trace=round_trace,
                    fallback_reason=f"评分锚点泄漏（bank_qid={qid}，题面与答案钥匙重叠）",
                )
            if stamped["topic"] in issued_keys:
                fallback = next_unused_fallback(issued_keys)
                if fallback is None:
                    return _done(state, "考点重复且兜底池已尽", round_trace=round_trace)
                return _issue(
                    fallback, state, failures=0, round_trace=round_trace,
                    fallback_reason="考点重复（模型走神，不算 LLM 失败）",
                )
                # cand 引用编号对账：在正常签发前摘除未核实
            # 的 cand-XXXX（账本里没有 = 模型没查就报编号）；题目照常使用
            stamped = _sanitize_candidate_refs(stamped, cand_registry)
            # ⑥ 正常签发（盖帽后的 dict：进入 state 的统一形态）
            return _issue(stamped, state, failures=0, round_trace=round_trace)

        # ⑦ 失败路径：熔断 → 兜底池 → 池尽收场（fail-visible：reason 都进 transcript）
        if failures + 1 >= spec["max_consecutive_llm_failures"]:
            return _done(
                state, f"连续 LLM 失败熔断（{failures + 1} 次）", round_trace=round_trace
            )
        fallback = next_unused_fallback(issued_keys)
        if fallback is None:
            return _done(state, "签发失败且兜底池已尽", round_trace=round_trace)
        return _issue(
            fallback, state, failures=failures + 1, round_trace=round_trace,
            fallback_reason="LLM 签发失败",
        )

    return issue_question_node


def _build_prompt(state: dict, issued_keys: list, spec: dict) -> str:
    """渲染签发上下文。纪律：空值显式 marker（防模型脑补缺席信息）+ 长材料 clip。
    工具模式下在 format 后拼接 ADDENDUM（后拼纪律见 prompts/issue_question.py 头注释：
    两模板各持自己的占位符，避免花括号转义互相干扰）。"""
    base = ISSUE_QUESTION_SYSTEM.format(
        position=answer_or_marker(state.get("position", ""), "(未声明岗位)"),
        candidate_profile=clip(state.get("candidate_profile", ""), 1500) or "(无简历材料)",
        jd_requirements=answer_or_marker(state.get("jd_requirements", ""), "(未提供 JD)"),
        self_intro=clip(first_candidate_text(state), 500) or "(无自我介绍)",
        topic_results=_render_topic_results(state.get("topic_results") or []),
        max_questions=spec["max_questions"],
        issued_count=len(issued_keys),
        remaining_budget=spec["max_questions"] - len(issued_keys),
    )
    if spec.get("search_tool") is not None:
        base += ISSUE_QUESTION_TOOLS_ADDENDUM.format(
            max_tool_rounds=spec.get("max_tool_rounds", 3),
            max_calls=spec.get("max_tool_calls", 5),
        )
        # 第二检索面的纪律段：无占位符直接拼。两段 ADDENDUM 的拼接
    # 条件与节点内工具入环条件**逐键对称**（search_tool / candidate_tool_
    # factory 各管各的）——prompt 说了有工具，工具就必须真的在环里；说了
    # 没有就不能提（模型按 prompt 规划行为，2026-11-17 审查补锁）
    if spec.get("candidate_tool_factory") is not None:
        base += ISSUE_QUESTION_CANDIDATE_ADDENDUM
    return base


def _render_topic_results(topic_results: list) -> str:
    """topic_results → "考点｜均分 X" 行；空时返回哨兵 marker（空值显式化）。"""
    if not topic_results:
        return "（尚未开考）"
    lines = []
    for entry in topic_results:
        scores = entry.get("scores") or {}
        values = [scores.get(k) for k in SCORE_DIMS]
        values = [v for v in values if isinstance(v, (int, float))]
        avg = round(sum(values) / len(values), 1) if values else "无分"
        lines.append(f"{entry.get('topic', '?')}｜均分 {avg}")
    return "\n".join(lines)


def _issue(question: dict, state: dict, failures: int, round_trace: tuple = (),
           fallback_reason: str = "") -> dict:
    """落账一次签发。question 必须是 plain dict（两路在入口处统一形态）。
    fallback_reason 非空 = 走了兜底池，随 transcript 落账供终场报告披露。"""
    followup = state.get("followup_state") or fresh_followup_state()
    extra = {"fallback_reason": fallback_reason} if fallback_reason else {}
    return {
        "pending_question": question["text"],
        "current_question": question,
        "current_topic": question["topic"],
        # 换题即重置追问工作区；total_count 保留（全场保险丝只增不减）
        "followup_state": fresh_followup_state(
            topic=question["topic"], total_count=followup["total_count"]
        ),
        # 无 reducer 字段：整块返回全量新列表，绝不 in-place append
        "issued_keys": (state.get("issued_keys") or []) + [question["topic"]],
        # 留痕：读旧值整块拼接
        "retrieval_trace": (state.get("retrieval_trace") or []) + list(round_trace),
        "consecutive_llm_failures": failures,
        "transcript": append_transcript(
            state,
            "interviewer",
            question["text"],
            kind="issue",
            module=question.get("module", "project"),
            topic=question["topic"],
            **extra,
        ),
    }


def _done(state: dict, reason: str, round_trace: tuple = ()) -> dict:
    """收场写入：current_question/current_topic/pending_question 必须显式清空
（state 合并语义下残留不会自己消失，不清场 = 幽灵题被再问一遍）。
    reason 进 transcript；留痕不清（跨签发累计）。"""
    return {
        "current_question": {},
        "current_topic": "",
        "pending_question": "",
        "retrieval_trace": (state.get("retrieval_trace") or []) + list(round_trace),
        "transcript": append_transcript(
            state, "system", f"（发题官收场：{reason}）", kind="issue_done"
        ),
    }


def _normalized_qid(result: IssuedQuestion) -> str:
    """模型报的 bank_qid → 规范化编号（去空白 + "null"/"none" 字面量归空——
    真模型会把 null 当字符串抄进去）。与泄漏闸共用同一份归一化锁死口径。"""
    qid = (result.bank_qid or "").strip()
    return "" if qid.lower() in ("null", "none") else qid


def _stamp_bank(result: IssuedQuestion, registry: dict) -> tuple[dict | None, bool]:
    """拿检索账本核对模型报的 bank_qid：命中则把 topic/source_type/source_ref
    盖写成题库原值（text 保留模型的改写），followup_hints 空时填库内追问树。

    返回 (dict, False)；编号不在账本 = 编造 → (None, True) 走失败路径；
    没报编号但自称 bank → 改回 generated 照常使用（只是来源标错）。
    """
    d = result.model_dump()
    # 规范化字面空值：归一化逻辑见 _normalized_qid（两处共用一份）
    qid = _normalized_qid(result)
    if not qid:
        d["bank_qid"] = None  # state 里不留脏值
        if d["source_type"] == "bank":
            d["source_type"] = "generated"
    else:
        hit = registry.get(qid)
        if hit is None:
            return None, True
        d["topic"] = hit.topic
        d["source_type"] = "bank"
        d["source_ref"] = f"bank:{qid}｜{hit.source}" if hit.source else f"bank:{qid}"
        if not d["followup_hints"]:
            d["followup_hints"] = hit.followup_tree
    return d, False


# 安全闸与 cand 引用对账

# 泄漏闸阈值：任一锚点的字符 bigram 有 ≥60% 出现在题面 = 泄漏
#（逐字抄录 ≈1.0、正常改编 ≤0.3；锚点不足 3 个 bigram 豁免）
LEAK_ANCHOR_BIGRAM_RECALL = 0.6
LEAK_MIN_ANCHOR_BIGRAMS = 3

# cand 编号形状：cand- + 至少 4 位数字，不设上限——写成 \d{4} 会把编造的
# "cand-00012" 截成 4 位前缀，前缀在账本则整条绕过对账
_CAND_ID_RE = re.compile(r"cand-\d{4,}")


def _bigrams(text: str) -> set:
    """去空白后的字符二元组。自包含小实现、不 import bm25f——agents 层
    不依赖检索内核；泄漏检测只要"连续两字"的粒度，不需要检索级切词。"""
    s = re.sub(r"\s+", "", text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _anchor_leak(text: str, anchors: list) -> bool:
    """确定性泄漏闸：题面 vs 命中题评分锚点的 bigram 重叠检测，零 LLM。"""
    q = _bigrams(text)
    for a in anchors:
        grams = _bigrams(a)
        if len(grams) < LEAK_MIN_ANCHOR_BIGRAMS:
            continue
        if len(grams & q) / len(grams) >= LEAK_ANCHOR_BIGRAM_RECALL:
            return True
    return False


def _sanitize_candidate_refs(d: dict, cand_registry: dict) -> dict:
    """source_ref 里的 cand-XXXX 过账本对账（面关闭时账本恒空，任何 cand 引用
    都算未核实）。账本里没有的编号：摘除并原地标注"未核实引用"——cand 编造
    是引用标注错误（修数据），不像 bank 编造是谎报题源（熔断级）。"""
    ref = d.get("source_ref") or ""
    unknown = {c for c in _CAND_ID_RE.findall(ref) if c not in cand_registry}
    if not unknown:
        return d
    for c in unknown:
        ref = ref.replace(c, "未核实引用")
    d["source_ref"] = ref
    return d

