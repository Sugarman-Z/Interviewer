"""Evaluator 节点：候选人每答一次做三件事——三维打分（每维引用原话 evidence）、
followup/advance 裁决、追问句产出，一次 LLM 调用完成。

函数体按第 1-8 步编号，tests/test_evaluator.py 逐条对应（读测试 = 读说明书）。
"""
from interviewer.agents.prompts import EVALUATOR_SYSTEM
from interviewer.agents.schemas import SCORE_DIMS, Scorecard
from interviewer.agents.utils.agent_states import FollowupState, fresh_followup_state
from interviewer.agents.utils.agent_utils import (
    answer_or_marker,
    append_transcript,
    clip,
    current_module,
)
from interviewer.agents.utils.structured import invoke_structured_or_freetext
from interviewer.graph.score_parser import validate_scorecard


def make_evaluator(llm, spec: dict | None = None):
    """真评分 Evaluator（deep 档 LLM）。

    spec 必备键（[] 直取，缺键建图当场报错）：structured_output_method、
    max_followups（单题追问熔断线，含主问轮）、max_total_turns。
    可选键 scorecard 为测试专用，给了就不调 LLM。
    """
    spec = spec or {}
    method = spec["structured_output_method"]
    max_followups = spec["max_followups"]
    max_total_turns = spec["max_total_turns"]
    override = spec.get("scorecard") or {}

    def evaluator_node(state: dict) -> dict:
        # 第 1 步：读现场
        # old_count / old_total 是"这一答之前"的计数，后面的判断全基于它们。
        followup = state.get("followup_state") or fresh_followup_state()
        old_count = followup["followup_count"]   # 本题已答几轮（主问算第 1 轮）
        old_total = followup["total_count"]      # 全场已答几轮
        question = state.get("pending_question", "")   # 候选人刚回答的问题
        answer = state.get("last_answer", "")          # 候选人的回答

        # 第 2 步：把本轮问答拼进 qa 历史
        # prompt 里的 {qa_history} 用这份——追问时模型能看到此前问过什么、答过什么。
        qa = followup["qa_history"] + f"[问] {question}\n[答] {answer}\n"

        # 第 3 步：从追问树里取本轮的 hint
        # 题库题自带 followup_hints（预写的追问方向）。答完第 1 轮（old_count=0）
        # 产出的追问用 hints[0]，答完第 2 轮用 hints[1]——下标必须用自增前的
        # old_count（用错会变 hints[-1]，Python 负下标不报错、悄悄取到最后一条）。
        # hints 用完或题目没有 → 给"（无）"，模型改打自己指出的薄弱处。
        hints = (state.get("current_question") or {}).get("followup_hints") or []
        hint_text = hints[old_count] if old_count < len(hints) else "（无）"

        # 第 4 步：渲染 prompt
        # followup_budget 把追问预算明说给模型：快到线时它该给 advance。
        # 但这只是"请求"，硬限制在代码里（第 7 步 + 路由层，双保险）。
        prompt = EVALUATOR_SYSTEM.format(
            position=answer_or_marker(state.get("position", ""), "未声明岗位"),
            topic=state.get("current_topic", ""),
            question=question,
            qa_history=clip(qa, 2000) or "（无）",
            answer=answer,
            followup_budget=(
                f"本题已答 {old_count} 轮 / 熔断线 {max_followups} 轮（含主问轮）；"
                "预算尽时 action 必须为 \"advance\""
            ),
            followup_hints=hint_text,
        )

        # 第 5 步：拿到 scorecard（一个普通字典）
        # 三条路汇成一个字典：测试注入（跳过 LLM）/ LLM 成功 / LLM 失败。
        # 降级细节不用管：invoke_structured_or_freetext 内部已做两层降级
        #（结构化失败 → 自由文本重试），返回 None 才算彻底失败。
        if override:
            # 测试注入：完全跳过 LLM（FakeEvalLLM 剧本是空的，被调会当场炸——
            # 这正是"注入必须跳过 LLM"的验收方式）
            scorecard = {
                "technical_depth": 3, "correctness": 3, "communication": 3,
                "evidence": {}, "weak_points": [],
                "action": "advance", "followup_text": "",
                **override,
            }
        else:
            result = invoke_structured_or_freetext(llm, Scorecard, prompt, method=method)
            scorecard = result.model_dump() if result is not None else validate_scorecard(None)
        ok = not scorecard.get("eval_failed")

        # 第 6 步：按成功/失败备三样
        # (a) 熔断计数新值：与 IssueQuestion 共用一个字段，约定"任何一次成功
        #     就清零"；失败必须读 state 旧值 + 1（从 0 数会永远熔不断）
        failures = 0 if ok else (state.get("consecutive_llm_failures") or 0) + 1

        # (b) 每答一条留痕：失败时 scores 空 → 渲染走"无分"腿，
        #     成绩单看得出哪些题没评上分
        topic_entry = {
            "module": current_module(state),
            "topic": state.get("current_topic", ""),
            "scores": {k: scorecard.get(k, 0) for k in SCORE_DIMS} if ok else {},
            "action": scorecard.get("action", "advance"),
            "eval_failed": not ok,
        }

        # (c) 只有失败时往 transcript 记一笔（终场报告可见这次没评上分）。
        #     先置 None 再进 if：成功路径上这个变量也必须存在，第 8 步才读得
        fail_entry = None
        if not ok:
            fail_entry = append_transcript(
                state, "system", "（评分失败：本回答未评分，按换题处理）",
                kind="eval_failed",
                module=current_module(state),
                topic=state.get("current_topic", ""),
            )

        # 第 7 步：追问落账闸门（先自查两道保险丝）
        # 模型说 followup ≠ 这句追问真的会被问：路由层稍后会再查同样的两条线，
        # 到线就改道收尾/换题。若先把追问写了出去，路由一改道，那句追问就成了
        # 候选人从没见过、却留在终场报告里的"幽灵问题"。
        # 两个 +1 是因为本题这一答马上要计入计数（第 8 步写回后路由读到的
        # 就是 +1 之后的值）——自查与路由必须看同一个"未来值"。
        want_followup = (
            scorecard.get("action") == "followup"
            and scorecard.get("followup_text", "").strip()
        )
        fuse_hit = (old_total + 1 >= max_total_turns) or (old_count + 1 >= max_followups)

        followup_entry = None
        if want_followup and not fuse_hit:
            followup_entry = append_transcript(
                state, "interviewer", scorecard["followup_text"],
                kind="followup",
                module=current_module(state),
                topic=state.get("current_topic", ""),
            )

        # 第 8 步：拼 update dict
        # 永远写四样；追问/失败各写一笔 transcript（两者不会同时发生：
        # 失败字典的 action 是 "advance"，want_followup 必为 False）。
        # list 类字段一律"读旧值 + 整块返回新列表"（无 reducer，就地修改会丢）
        update = {
            "last_scorecard": scorecard,
            "topic_results": (state.get("topic_results") or []) + [topic_entry],
            "followup_state": FollowupState(
                topic=followup["topic"],
                qa_history=qa,
                followup_count=old_count + 1,
                total_count=old_total + 1,
            ),
            "consecutive_llm_failures": failures,
        }
        if followup_entry is not None:
            update["pending_question"] = scorecard["followup_text"]
            update["transcript"] = followup_entry
        elif fail_entry is not None:
            update["transcript"] = fail_entry
        return update

    return evaluator_node
