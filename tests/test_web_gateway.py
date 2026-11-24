"""gateway 翻译核心测试。
fixture 种子 = mock provider 真跑 dump（不手写 18 字段 state）：
tests/fixtures/web_state_*.json 与真跑结果由 test_fixtures_match_live_engine
持续对拍——引擎输出漂移时该测试红；确认后设 INTERVIEWER_WEB_REGEN_FIXTURES=1
重跑即重生成。核心单测不依赖 fastapi：本文件不 import fastapi/TestClient
（无 fastapi 环境可跑，可选组契约的另一半）；test_gateway_does_not_import_
fastapi 在 AST 层锁住 gateway 源码面。
"""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from interviewer.agents.schemas import SCORE_DIMS
from interviewer.agents.utils.agent_utils import resume_text
from interviewer.default_config import get_config
from interviewer.graph.checkpointer import build_checkpointer
from interviewer.graph.coach_graph import CoachGraph
from interviewer.graph.session_driver import pending_interrupt, run_new_session
from interviewer.graph.session_runner import create_initial_state
from interviewer.web import gateway

FIXTURES = Path(__file__).parent / "fixtures"

_ANSWER = {"type": "answer", "text": "标准回答"}
_SKIP = {"type": "skip"}

# 场景剧本（max_questions=2）：awaiting = 答开场 + 答 q1 后停在 q2；
# skip = 答开场后跳过 q1；finished = 一直答到 END（mock 每答必 advance）
_SCENARIOS = {
    "awaiting": [_ANSWER, _ANSWER],
    "skip": [_ANSWER, _SKIP],
    "finished": [_ANSWER, _ANSWER, _ANSWER],
}


def _run_mock_session(sid: str, payloads: list, tmp_path):
    """mock provider 真跑一场：payloads 按序消费，耗尽即退出（挂起留在
    checkpointer）。返回 (最终 state, pending 载荷)。"""
    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["max_questions"] = 2
    saver, close = build_checkpointer(cfg)
    try:
        coach = CoachGraph(cfg, checkpointer=saver)
        graph = coach.build()
        invoke_cfg = coach.invoke_config(sid)
        it = iter(payloads)

        def on_interrupt(value):
            try:
                return next(it)
            except StopIteration:
                return None

        result = run_new_session(
            graph,
            invoke_cfg,
            create_initial_state(sid, "后端开发", "简历", ""),
            on_interrupt,
        )
        if result.finished:
            return result.final_values, None
        return graph.get_state(invoke_cfg).values, pending_interrupt(graph, invoke_cfg)
    finally:
        close()


def _dump(name: str, state: dict, pending) -> None:
    path = FIXTURES / f"web_state_{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"state": state, "pending": pending}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load(name: str):
    payload = json.loads(
        (FIXTURES / f"web_state_{name}.json").read_text(encoding="utf-8")
    )
    return payload["state"], payload["pending"]


def _mini_state(transcript, issued, topic_results=(), current_topic="", **extra):
    """聚焦单测的最小 state（真实全量形状由 fixture 承担）。

    纪律：gateway 每新增读取一个 state 键，用到 mini 态的规则锁用例必须
    补全该键或迁到 fixture 真态——否则测试绿在引擎不可能产生的 state 上。
    """
    state = {
        "transcript": transcript,
        "issued_keys": issued,
        "topic_results": list(topic_results),
        "current_topic": current_topic,
    }
    state.update(extra)
    return state


# fixture 新鲜度（引擎漂移警报）


def test_fixtures_match_live_engine(tmp_path):
    """fixture 新鲜度（引擎漂移警报）。对拍是**全量 state** 整等——mock 文案
（evidence/source_ref）微调也会红，这是有意的：fail-visible，见红先
    git diff 确认是什么变了。重生成 = 设 INTERVIEWER_WEB_REGEN_FIXTURES=1
    重跑：dump 后本测试**显式失败**并提示确认 diff——护栏是防「见红就
    regen」的反射把引擎回归洗白进 fixture。"""
    for name, payloads in _SCENARIOS.items():
        state, pending = _run_mock_session(f"fx-{name}", payloads, tmp_path)
        if os.environ.get("INTERVIEWER_WEB_REGEN_FIXTURES") == "1":
            _dump(name, state, pending)
            print(f"[regen] 已重写 tests/fixtures/web_state_{name}.json")
            pytest.fail(
                f"fixture {name} 已重写——请 git diff tests/fixtures/ 确认引擎"
                f"漂移是有意的，然后清掉 INTERVIEWER_WEB_REGEN_FIXTURES 重跑回归"
            )
        committed_state, committed_pending = _load(name)
        assert (committed_state, committed_pending) == (state, pending), (
            f"fixture {name} 与 mock 真跑结果不一致——引擎输出变了；"
            f"确认后设 INTERVIEWER_WEB_REGEN_FIXTURES=1 重跑本测试重生成"
        )


# transcript 映射


def test_skip_sentinel_from_engine_function():
    """：跳过哨兵经 resume_text 取得（不手抄字面量），双保险断言。"""
    assert gateway.SKIP_SENTINEL == resume_text({"type": "skip"})
    assert gateway.SKIP_SENTINEL == "(候选人跳过了此题)"


def test_awaiting_transcript_mapping():
    state, _pending = _load("awaiting")
    wire = gateway.transcript_to_wire(state, max_questions=2)
    intro, ans_open, q1, ans1, q2 = wire
    assert intro == {
        "role": "interviewer",
        "variant": "opening",
        "text": state["transcript"][0]["content"],
    }
    # 开场回答：无题号、无 topic（：开场阶段 candidate 回答无题号）
    assert ans_open == {"role": "candidate", "variant": "answer", "text": "标准回答"}
    assert set(q1) == {"role", "variant", "text", "topic", "qnum"}
    assert (q1["variant"], q1["topic"], q1["qnum"]) == ("question", "项目整体架构", "1")
    assert ans1 == {
        "role": "candidate",
        "variant": "answer",
        "text": "标准回答",
        "topic": "项目整体架构",
        "qnum": "1",
    }
    assert (q2["variant"], q2["topic"], q2["qnum"]) == ("question", "技术选型", "2")
    assert all("at" not in m for m in wire)  #：at 不下发


def test_finished_transcript_drops_issue_done():
    state, _pending = _load("finished")
    wire = gateway.transcript_to_wire(state, max_questions=2)
    # 引擎 transcript 末条是 issue_done system 条目 → 不下发
    assert len(state["transcript"]) == 7 and len(wire) == 6
    assert wire[-1]["role"] == "candidate" and wire[-1]["qnum"] == "2"
    assert not any(m["variant"] == "system" for m in wire)


def test_skip_entry_mapping_and_flag():
    state, _pending = _load("skip")
    wire = gateway.transcript_to_wire(state, max_questions=2)
    # 负向：skip 后不复刻 system 提示行（那是前端展示逻辑）——
    # 引擎 transcript 5 条 → wire 恰 5 条、无任何 system 行
    assert len(wire) == 5
    assert not any(m["variant"] == "system" for m in wire)
    skip_msg = wire[3]
    assert (skip_msg["role"], skip_msg["variant"]) == ("candidate", "skip")
    assert skip_msg["text"] == gateway.SKIP_SENTINEL  # 哨兵原文下发
    assert (skip_msg["topic"], skip_msg["qnum"]) == ("项目整体架构", "1")
    results = gateway.topic_results_to_wire(state, max_questions=2)
    assert results[0]["skipped"] is True  #：skipped 取自回答槽位


def test_eval_failed_mapping_and_optional_scores():
    transcript = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "candidate", "content": "自我介绍", "topic": ""},
        {"role": "interviewer", "content": "Q1", "kind": "issue",
         "module": "project", "topic": "缓存"},
        {"role": "candidate", "content": "答1", "topic": "缓存"},
        {"role": "system", "content": "（评分失败：本回答未评分，按换题处理）",
         "kind": "eval_failed", "module": "project", "topic": "缓存"},
    ]
    state = _mini_state(
        transcript,
        ["缓存"],
        topic_results=[
            {"module": "project", "topic": "缓存", "scores": {},
             "action": "advance", "eval_failed": True}
        ],
        current_topic="缓存",
    )
    wire = gateway.transcript_to_wire(state, max_questions=2)
    assert wire[-1] == {"role": "system", "variant": "system",
                        "text": gateway.EVAL_FAILED_TEXT}  # 引擎原文换人话
    results = gateway.topic_results_to_wire(state, max_questions=2)
    assert "scores" not in results[0]  #：空 scores 不下发（「未评分」）


# 题号推导与当前槽位


def test_awaiting_slot_expect_pending_progress():
    state, pending = _load("awaiting")
    assert gateway.current_slot(state, 2) == {
        "variant": "question", "topic": "技术选型", "qnum": "2",
    }
    assert gateway.expect_qnum(state, 2) == "2"
    q = gateway.pending_question(state, pending, 2)
    assert q == {
        "variant": "question", "text": pending["question"],
        "topic": "技术选型", "qnum": "2",
    }
    assert q["text"] == state["pending_question"]  # 载荷与 state 同源
    assert gateway.progress(state, pending, 2) == {"answered": 1, "total": 2}


def test_opening_slot_hardcoded():
    fresh = _mini_state(
        [{"role": "interviewer", "content": "开场", "phase": "intro"}], []
    )
    fresh["pending_question"] = "开场"
    assert gateway.current_slot(fresh, 2) == {"variant": "opening"}
    assert gateway.expect_qnum(fresh, 2) == "opening"  # 槽位写死
    assert gateway.pending_question(
        fresh, {"kind": "question", "question": "开场"}, 2
    ) == {"variant": "opening", "text": "开场"}
    assert gateway.progress(fresh, {}, 2) == {"answered": 0, "total": 2}


def test_followup_qnum_and_slot():
    """追问挂起：qnum "N.k"、主问已答（progress 不减）。mock 天然不追问，
    在 awaiting fixture 上追加真实的 followup 条目形状（evaluator.py:141-146）。
    注意：手造态的 topic_results 未同步追加（真追问挂起态应有 2 条评分），
    本态不可调 build_final 与 topic_results_to_wire（会触发对齐违例）——
    追问编号规则的锁在 mini-state 单测（test_followup_counter_reset…）。"""
    state, _pending = _load("awaiting")
    followup_text = "追问：具体说说选型的对比依据？"
    state = {
        **state,
        "transcript": list(state["transcript"]) + [
            {"role": "candidate", "content": "答2", "topic": "技术选型"},
            {"role": "interviewer", "content": followup_text, "kind": "followup",
             "module": "project", "topic": "技术选型"},
        ],
        "pending_question": followup_text,
    }
    wire = gateway.transcript_to_wire(state, max_questions=2)
    assert (wire[-1]["variant"], wire[-1]["qnum"], wire[-1]["topic"]) == (
        "followup", "2.1", "技术选型",
    )
    assert gateway.current_slot(state, 2) == {
        "variant": "followup", "topic": "技术选型", "qnum": "2.1",
    }
    assert gateway.expect_qnum(state, 2) == "2.1"
    assert gateway.progress(state, {}, 2) == {"answered": 2, "total": 2}


def test_checksum_fail_loud():
    """ 校验和违例 → EngineError（500），不静默下发坏题号。"""
    state, _pending = _load("awaiting")
    # 走查主问号 2 ≠ issued_keys 1
    with pytest.raises(gateway.EngineError):
        gateway.transcript_to_wire({**state, "issued_keys": ["项目整体架构"]}, 2)
    # 主问号 3 > max_questions 2（走查与 issued 同步越界）
    triple = {
        **state,
        "transcript": list(state["transcript"]) + [
            {"role": "interviewer", "content": "Q3", "kind": "issue",
             "module": "project", "topic": "难点与结果"},
        ],
        "issued_keys": list(state["issued_keys"]) + ["难点与结果"],
    }
    with pytest.raises(gateway.EngineError):
        gateway.transcript_to_wire(triple, 2)
    # current_topic 不在 issued_keys
    with pytest.raises(gateway.EngineError):
        gateway.current_slot({**state, "current_topic": "不存在的考点"}, 2)


def test_finished_state_raises_session_finished_not_500():
    """finished 态误调 current_slot/expect_qnum（收场后双发）→ 409 语义的
    SessionFinished，而非校验和 500——_done 清空 current_topic 是 finished
    特征（issue_question.py）。"""
    state, _pending = _load("finished")
    assert not state["current_topic"] and state["issued_keys"]
    with pytest.raises(gateway.SessionFinished) as excinfo:
        gateway.current_slot(state, 2)
    assert excinfo.value.http_status == 409
    with pytest.raises(gateway.SessionFinished):
        gateway.expect_qnum(state, 2)


def test_orphan_candidate_fail_loud():
    """ fail-loud 分支：非首条 candidate 却无题号（首条=开场，合法）。"""
    transcript = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "candidate", "content": "自我介绍", "topic": ""},
        {"role": "candidate", "content": "多余的无题回答", "topic": ""},
    ]
    with pytest.raises(gateway.EngineError):
        gateway.transcript_to_wire(_mini_state(transcript, []), 2)


def test_slot_index_mismatch_fail_loud():
    """ fail-loud 分支：签发序 ≠ 走查主问号（issued 非末位考点）。"""
    transcript = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "candidate", "content": "自我介绍", "topic": ""},
        {"role": "interviewer", "content": "Q1", "kind": "issue",
         "module": "project", "topic": "A"},
        {"role": "interviewer", "content": "Q2", "kind": "issue",
         "module": "project", "topic": "B"},
    ]
    with pytest.raises(gateway.EngineError):
        gateway.current_slot(_mini_state(transcript, ["A", "B"], current_topic="A"), 2)


def test_unknown_entry_shape_fail_loud():
    """引擎新增 transcript 形态（role/kind 未知）→ EngineError fail-loud，
    不静默缺行（：引擎侧改动属 Ask-first，炸出来比缺行好）。"""
    base = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "mystery", "content": "未来角色"},
    ]
    with pytest.raises(gateway.EngineError):
        gateway.transcript_to_wire(_mini_state(base, []), 2)
    bad_kind = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "interviewer", "content": "X", "kind": "future_kind", "topic": "T"},
    ]
    with pytest.raises(gateway.EngineError):
        gateway.transcript_to_wire(_mini_state(bad_kind, []), 2)
    bad_system = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "system", "content": "X", "kind": "future_system"},
    ]
    with pytest.raises(gateway.EngineError):
        gateway.transcript_to_wire(_mini_state(bad_system, []), 2)


def test_eval_failed_text_literal():
    """spec 引号串字面锁（同 SKIP_SENTINEL 的双保险纪律）。"""
    assert gateway.EVAL_FAILED_TEXT == "这一题的评分暂时不可用，已换下一题"


def test_pending_question_fallback_legs():
    """缺载荷兜底：payload None / 无 question 键 → text 取 state.pending_question。"""
    state, _pending = _load("awaiting")
    expected_text = state["pending_question"]
    for payload in (None, {}, {"kind": "question"}):
        q = gateway.pending_question(state, payload, 2)
        assert q["text"] == expected_text


def test_followup_counter_reset_and_second_level():
    """「遇 issue 追问序清零」+ 追问计数：q1 追问 1.1 后换题 q2，其
    首个追问必须是 "2.1" 而非 "2.2"；再追一级到 "2.2"。"""
    transcript = [
        {"role": "interviewer", "content": "开场", "phase": "intro"},
        {"role": "candidate", "content": "自我介绍", "topic": ""},
        {"role": "interviewer", "content": "Q1", "kind": "issue",
         "module": "project", "topic": "A"},
        {"role": "candidate", "content": "答A", "topic": "A"},
        {"role": "interviewer", "content": "追问A1", "kind": "followup",
         "module": "project", "topic": "A"},
        {"role": "candidate", "content": "答A1", "topic": "A"},
        {"role": "interviewer", "content": "Q2", "kind": "issue",
         "module": "project", "topic": "B"},
        {"role": "candidate", "content": "答B", "topic": "B"},
        {"role": "interviewer", "content": "追问B1", "kind": "followup",
         "module": "project", "topic": "B"},
        {"role": "candidate", "content": "答B1", "topic": "B"},
        {"role": "interviewer", "content": "追问B2", "kind": "followup",
         "module": "project", "topic": "B"},
    ]
    state = _mini_state(transcript, ["A", "B"], current_topic="B")
    wire = gateway.transcript_to_wire(state, max_questions=2)
    followups = [m["qnum"] for m in wire if m["variant"] == "followup"]
    assert followups == ["1.1", "2.1", "2.2"]  # 清零 + 二级计数
    assert gateway.current_slot(state, 2)["qnum"] == "2.2"
    assert gateway.expect_qnum(state, 2) == "2.2"


# 对齐 / 聚合 / 三件套


def test_topic_results_alignment():
    state, _pending = _load("awaiting")
    results = gateway.topic_results_to_wire(state, max_questions=2)
    assert results == [
        {
            "qnum": "1",
            "topic": "项目整体架构",
            "is_followup": False,
            "skipped": False,
            "scores": {"technical_depth": 3, "correctness": 3, "communication": 3},
        }
    ]


def test_topic_results_alignment_fail_loud():
    state, _pending = _load("awaiting")
    bad = {
        **state,
        "topic_results": list(state["topic_results"]) + [dict(state["topic_results"][0])],
    }
    with pytest.raises(gateway.EngineError):  # 2 条评分 vs 1 条可评分回答
        gateway.topic_results_to_wire(bad, 2)


def test_overall_score_rules():
    def entry(dims):
        return {"scores": dict(zip(SCORE_DIMS, dims)), "eval_failed": False}

    assert gateway.overall_score([entry((3, 3, 3)), entry((4, 5, 6))]) == 4.0
    assert gateway.overall_score([entry((3, 3, 3)), entry((3, 3, 4))]) == 3.2  # round 一位
    # 空分条目不计入分子分母（禁止 ?? 0 充数）
    assert gateway.overall_score(
        [entry((5, 4, 3)), {"scores": {}, "eval_failed": True}]
    ) == 4.0
    assert gateway.overall_score([]) == 0.0
    assert gateway.overall_score([{"scores": {}, "eval_failed": True}]) == 0.0


def test_build_final():
    state, _pending = _load("finished")
    final = gateway.build_final(state, max_questions=2)
    assert final["overall"] == 3.0  # 两条 3/3/3 → 3.0
    assert final["interview_report"] == state["interview_report"]  # 原样直传
    assert [t["qnum"] for t in final["topic_results"]] == ["1", "2"]
    assert final["topic_results"][0]["skipped"] is False


def test_classify_session_three_states():
    snap_done = SimpleNamespace(next=(), values={})
    snap_mid = SimpleNamespace(next=("Evaluator",), values={})
    assert gateway.classify_session(
        snap_mid, {"kind": "question", "question": "x"}
    ) == "awaiting"
    assert gateway.classify_session(snap_done, None) == "finished"
    assert gateway.classify_session(snap_mid, None) == "limbo"


def test_progress_finished_counts_all_issued():
    state, _pending = _load("finished")
    assert gateway.progress(state, None, 2) == {"answered": 2, "total": 2}


# 纪律锁


def test_gateway_does_not_import_fastapi():
    """：gateway 不 import fastapi（AST 层检查——源码字符串会误伤注释）。"""
    tree = ast.parse(Path(gateway.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all("fastapi" not in (alias.name or "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "fastapi" not in (node.module or "")


# T2 流式：QuestionTextTap（SPEC-streaming-tdd；）


def _tap_chunk(content="", tccs=None):
    """tap 用鸭子类型喂料（getattr 取 content/tool_call_chunks），无需
    langchain 消息对象——SimpleNamespace 即合同形状。"""
    return SimpleNamespace(content=content, tool_call_chunks=tccs or [])


def _tcc(name="", args="", id=None, index=0):
    return {"name": name, "args": args, "id": id, "index": index}


def _tap_feed(tap, chunks, node):
    return [tap.feed(c, {"langgraph_node": node}) for c in chunks]


def test_tap_extracts_text_from_tool_args_incrementally():
    """主问题面增量抽取：fragments 切在键名中间，每步输出都是最终题面的
    新前缀段（TDD 首条）。"""
    tap = gateway.QuestionTextTap()
    frags = ['{"top', 'ic":"项目"', ',"text":"介绍', ' Kafka', '架构"', "}"]
    chunks = [
        _tap_chunk(tccs=[_tcc(name="IssuedQuestion", args=frags[0], id="c0", index=0)]),
        *[_tap_chunk(tccs=[_tcc(args=f, index=0)]) for f in frags[1:]],
    ]
    outs = _tap_feed(tap, chunks, "IssueQuestion")
    assert outs == ["", "", "介绍", " Kafka", "架构", ""]
    assert "".join(outs) == "介绍 Kafka架构"


def test_tap_extracts_followup_text_from_evaluator_chunks():
    """追问轮：evaluator 的 Scorecard 车道只抠 followup_text——输出不含
    scores/evidence/weak_points 片段。"""
    tap = gateway.QuestionTextTap()
    scorecard_json = (
        '{"action":"followup","technical_depth":1,'
        '"evidence":{"technical_depth":"没展开"},'
        '"weak_points":["混淆概念"],'
        '"followup_text":"那你说说 auto-commit 到底管什么？"}'
    )
    cuts = [scorecard_json[:40], scorecard_json[40:80], scorecard_json[80:]]
    chunks = [
        _tap_chunk(tccs=[_tcc(name="Scorecard", args=cuts[0], id="c1", index=0)]),
        *[_tap_chunk(tccs=[_tcc(args=f, index=0)]) for f in cuts[1:]],
    ]
    outs = _tap_feed(tap, chunks, "Evaluator")
    joined = "".join(outs)
    assert joined == "那你说说 auto-commit 到底管什么？"
    for forbidden in ("technical_depth", "evidence", "weak_points", "followup", "action"):
        assert forbidden not in joined  # 评估内部字段一律不外显


def test_tap_handles_json_escapes():
    """转义跨 fragment 延迟解码：反斜杠落在片尾、\\uXXXX 攒 4 位（M2 的
    判别锚）。"""
    tap = gateway.QuestionTextTap()
    # 原始 JSON 值为 a\nb中"c（转义序列被 fragment 边界切开）
    frags = ['{"text":"a\\', 'nb\\', 'u4e', '2d', '\\"', 'c"}']
    chunks = [
        _tap_chunk(tccs=[_tcc(name="IssuedQuestion", args=frags[0], id="c", index=0)]),
        *[_tap_chunk(tccs=[_tcc(args=f, index=0)]) for f in frags[1:]],
    ]
    outs = _tap_feed(tap, chunks, "IssueQuestion")
    assert "".join(outs) == 'a\nb中"c'


def test_tap_silent_before_target_key_opens():
    """目标键未出现/值未开串 → 空串；非串值 → 绝不猜。"""
    tap = gateway.QuestionTextTap()
    outs = _tap_feed(
        tap,
        [
            _tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"topic":"x"', id="c", index=0)]),
            _tap_chunk(tccs=[_tcc(args=',"done":false', index=0)]),
        ],
        "IssueQuestion",
    )
    assert outs == ["", ""]
    tap2 = gateway.QuestionTextTap()
    outs2 = _tap_feed(
        tap2,
        [_tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"text":123}', id="c", index=0)])],
        "IssueQuestion",
    )
    assert outs2 == [""]


def test_tap_drops_non_schema_lanes_in_parallel_tool_calls():
    """并行车道：检索车道（args 只含 query）与交卷车道交错到达——输出只含
    交卷车道增量、检索片段零外显（含交错形态）。"""
    tap = gateway.QuestionTextTap()
    chunks = [
        _tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"te', id="c0", index=0),
                          _tcc(name="search_bank", args='{"qu', id="c1", index=1)]),
        _tap_chunk(tccs=[_tcc(args='xt":"真', index=0), _tcc(args='ery":"Kafka"', index=1)]),
        _tap_chunk(tccs=[_tcc(args='题面"}', index=0), _tcc(args='}', index=1)]),
    ]
    outs = _tap_feed(tap, chunks, "IssueQuestion")
    assert "".join(outs) == "真题面"


def test_tap_lane_lock_by_name_not_position():
    """车道按 name 锁而非按位置：schema 车道落在 index=1、
    检索车道在 index=0 的交错形态——「硬编码 lane 0」变异在此红。"""
    tap = gateway.QuestionTextTap()
    chunks = [
        _tap_chunk(tccs=[_tcc(name="search_bank", args='{"query":"K', id="s0", index=0),
                          _tcc(name="IssuedQuestion", args='{"te', id="c0", index=1)]),
        _tap_chunk(tccs=[_tcc(args='afka"}', index=0), _tcc(args='xt":"反位题面"}', index=1)]),
    ]
    outs = _tap_feed(tap, chunks, "IssueQuestion")
    assert "".join(outs) == "反位题面"


def test_tap_ignores_target_key_literal_inside_string_values():
    """in_string 守卫：目标键字面出现在更早的字符串值内部（reason 里引用
    "text": 字样）不被误配——键匹配只在非字符串位置成立。

    后两个输入各杀一类变异：键冒号前空白只杀朴素 find 变异、
    目标键前的简单转义只杀 _ESC_STR 删除变异——两个都在才算锁住键匹配侧
    状态机（_IN_STR/_ESC_STR/_AFTER_STR 整面）。"""
    tap = gateway.QuestionTextTap()
    args = '{"reason":"碰见 \\"text\\": \\"假\\" 的场合","text":"真题面"}'
    outs = _tap_feed(
        tap,
        [_tap_chunk(tccs=[_tcc(name="IssuedQuestion", args=args, id="c", index=0)])],
        "IssueQuestion",
    )
    assert "".join(outs) == "真题面"
    # 键冒号前带空白：AFTER_STR 的空白容忍（朴素子串匹配在此输出空串）
    tap2 = gateway.QuestionTextTap()
    outs2 = _tap_feed(
        tap2,
        [_tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"text" : "带空白"}', id="c", index=0)])],
        "IssueQuestion",
    )
    assert "".join(outs2) == "带空白"
    # 目标键前有简单转义的普通串：_ESC_STR 删除变异在此状态漂移输出空串
    tap3 = gateway.QuestionTextTap()
    outs3 = _tap_feed(
        tap3,
        [_tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"reason":"a\\"b","text":"真"}', id="c", index=0)])],
        "IssueQuestion",
    )
    assert "".join(outs3) == "真"


def test_tap_freetext_after_structured_failure():
    """structured 失败后的 freetext 降级腿可达：同节点先流过
    tool chunk（半截 args 或整块坏 args）再来的纯 content chunk 属新调用——
    车道/扫描器全清后正确抽取。已知跳变：坏 args 半截文本已吐的部分与
    freetext 文本拼接，非前缀——终局 question 事件权威替换（兜底）。"""
    # 形态一：半截 args（扫描器停在未闭字符串态）
    tap = gateway.QuestionTextTap()
    out1 = tap.feed(
        _tap_chunk(tccs=[_tcc(name="Scorecard", args='{"followup_', id="e1", index=0)]),
        {"langgraph_node": "Evaluator"},
    )
    out2 = tap.feed(
        _tap_chunk(content='{"action":"followup","followup_text":"降级腿的追问"}'),
        {"langgraph_node": "Evaluator"},
    )
    assert out1 == ""
    assert out2 == "降级腿的追问"
    # 形态二：整块坏 args（含 followup_text 但校验会失败的完整 JSON——扫描器
    # DONE 态），随后 freetext 调用照抽
    tap2 = gateway.QuestionTextTap()
    tap2.feed(
        _tap_chunk(tccs=[_tcc(name="Scorecard", args='{"followup_text":"坏args文本"}', id="e2", index=0)]),
        {"langgraph_node": "Evaluator"},
    )
    out3 = tap2.feed(
        _tap_chunk(content='```json\n{"followup_text":"换腿后的追问"}\n```'),
        {"langgraph_node": "Evaluator"},
    )
    assert out3 == "换腿后的追问"


def test_tap_bad_unicode_escape_silent():
    """非法 \\uXXXX：不 raise、落 DONE 零输出（T2 评审 #2——json.loads 对同串
    同样拒收，该调用的值不可能成为最终题面；比空串更差的「炸出/卡死」
    已修）。"""
    tap = gateway.QuestionTextTap()
    outs = _tap_feed(
        tap,
        [
            _tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"text":"bad \\uZZ here', id="c", index=0)]),
            _tap_chunk(tccs=[_tcc(args='"}', index=0)]),
        ],
        "IssueQuestion",
    )
    assert outs == ["bad ", ""]  # 坏转义前的前缀照出，其后零输出


def test_tap_freetext_fenced_json():
    """freetext 降级路径：content 里的 ```json 围栏 JSON 同款增量抽取——
    issue 形（text 键）与 evaluator 形（followup_text 键）。"""
    fenced_issue = '前置说明\n```json\n{"topic":"t","text":"围栏里的题面"}\n```\n后记'
    tap = gateway.QuestionTextTap()
    pieces = [fenced_issue[:15], fenced_issue[15:30], fenced_issue[30:]]
    outs = _tap_feed(tap, [_tap_chunk(content=p) for p in pieces], "IssueQuestion")
    assert "".join(outs) == "围栏里的题面"

    tap2 = gateway.QuestionTextTap()
    ev = '{"action":"followup","followup_text":"追问句"}'
    outs2 = _tap_feed(tap2, [_tap_chunk(content=ev)], "Evaluator")
    assert "".join(outs2) == "追问句"


def test_tap_same_content_call_multi_chunk_no_reset():
    """同一纯 content 调用的多 chunk 分片不 reset（reset 契约：纯 content
    边界不可判——freetext 围栏 JSON 靠它拼齐，T0 事实 1）。"""
    tap = gateway.QuestionTextTap()
    full = '```json\n{"text":"分片拼齐的题面"}\n```'
    outs = _tap_feed(
        tap,
        [_tap_chunk(content=full[i : i + 5]) for i in range(0, len(full), 5)],
        "IssueQuestion",
    )
    assert "".join(outs) == "分片拼齐的题面"


def test_tap_ignores_non_target_nodes():
    """非目标节点（ModuleSummary/ReportWriter）不喂——evaluator 不是非目标
    节点（它有 followup 车道）。"""
    tap = gateway.QuestionTextTap()
    for node in ("ModuleSummary", "ReportWriter"):
        outs = _tap_feed(tap, [_tap_chunk(content='{"text":"不该出现"}')], node)
        assert outs == [""]


def test_tap_reset_between_calls():
    """两次 LLM 调用间 reset：同节点按非空 id 开新调用、跨节点（评估→
    出题）整段重来——不串题。"""
    tap = gateway.QuestionTextTap()
    outs_a = _tap_feed(
        tap,
        [_tap_chunk(tccs=[_tcc(name="Scorecard", args='{"followup_text":"追问A"}', id="e1", index=0)])],
        "Evaluator",
    )
    outs_b = _tap_feed(
        tap,
        [_tap_chunk(tccs=[_tcc(name="Scorecard", args='{"followup_text":"追问B"}', id="e2", index=0)])],
        "Evaluator",
    )
    assert "".join(outs_a) == "追问A" and "".join(outs_b) == "追问B"
    outs_c = _tap_feed(
        tap,
        [_tap_chunk(tccs=[_tcc(name="IssuedQuestion", args='{"text":"新主问"}', id="i1", index=0)])],
        "IssueQuestion",
    )
    assert "".join(outs_c) == "新主问"  # 不串题


@pytest.mark.parametrize("fixture_name,target_field", [
    ("stream_chunks_issue.json", "final_text"),
    ("stream_chunks_evaluator.json", "final_followup_text"),
])
def test_tap_real_zhipu_chunk_sequence(fixture_name, target_field):
    """真机回归：SPIKE-2 录制的 chunk 序列（单 chunk 整块 args 形态）——
    拼接 == fixture 存的最终题面/追问句；agg_args 对照字段同值（fixture
    被手改漂移时红灯，T2 评审 #5）。"""
    data = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    tap = gateway.QuestionTextTap()
    outs = []
    for entry in data["chunks"]:
        chunk = _tap_chunk(
            content=entry.get("content", ""),
            tccs=[dict(c) for c in entry.get("tool_call_chunks", [])],
        )
        outs.append(tap.feed(chunk, {"langgraph_node": entry.get("node", "")}))
    assert "".join(outs) == data[target_field]
    agg_key = "followup_text" if "evaluator" in fixture_name else "text"
    assert data[target_field] == data["agg_args"][agg_key]


@pytest.mark.parametrize("name,final,chunks_and_nodes", [
    (
        "issue_tool_lane",
        "介绍 Kafka 架构",
        [(_tap_chunk(tccs=[_tcc(name="IssuedQuestion",
                                 args='{"topic":"t","text":"介绍 Kafka 架构","module":"p}', id="c", index=0)]), "IssueQuestion")],
    ),
    (
        "freetext_multi_chunk",
        "前缀保证的题面",
        [
            (_tap_chunk(content='{"text":"前'), "IssueQuestion"),
            (_tap_chunk(content='缀保证的题面"}'), "IssueQuestion"),
        ],
    ),
])
def test_tap_never_overruns_final_text(name, final, chunks_and_nodes):
    """前缀不变式：任意序列的拼接输出 ⊆ 最终题面前缀
（M3 的断言锚——越过闭引号的变异在此红）。"""
    tap = gateway.QuestionTextTap()
    joined = "".join(
        tap.feed(c, {"langgraph_node": node}) for c, node in chunks_and_nodes
    )
    assert final.startswith(joined)
