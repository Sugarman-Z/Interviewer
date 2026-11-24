"""缺题挖掘单测：三态聚合 + 老档兼容 + re-probe 判真缺 + 词表建议。
mine() 是纯函数（sessions 喂 dict、语料喂 tmp 目录）——测的是聚合规则，
不是文件 IO；load_sessions 的坏档跳过单独用真实文件测。
"""
import importlib.util
import json
from pathlib import Path

from interviewer.dataflows.local_corpus import parse_corpus

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mine_mod = _load("mine_bank_gaps")

_BLOCK = """# 主题：测试库

---
id: bank-t-001
topic: Kafka 高可用
difficulty: 3
tags: kafka, 消息队列

**题目**
三节点集群 leader 宕机时如何保证消息不丢？

**追问树**
- ISR 与 acks？
- 副本数？
- min.insync.replicas？

**评分锚点**
- 5 分：说清联动
- 3 分：说对一半
- 1 分：只知名词
"""


def _corpus(tmp_path: Path, extra_blocks: tuple[str, ...] = ()) -> str:
    d = tmp_path / "project"
    d.mkdir()
    (d / "a.md").write_text(_BLOCK, encoding="utf-8")
    for j, block in enumerate(extra_blocks):
        (d / f"b{j}.md").write_text(block, encoding="utf-8")
    return str(tmp_path)


# 第二题：与缺口探针共享"通信"bigram（BM25 无阈值 → 命中即"已被补上"）
_BLOCK_TCP = """# 主题：测试库

---
id: bank-t-002
topic: TCP 通信超时
difficulty: 2
tags: 网络

**题目**
TCP 连接建立后长时间无数据往返，如何设计超时与重连？

**追问树**
- keepalive 参数？

**评分锚点**
- 5 分：说清参数联动
"""


def _trace_session(sid: str, records: list[dict]) -> dict:
    return {"session_id": sid, "retrieval_trace": records, "transcript": []}


def test_three_state_aggregation(tmp_path):
    """三态分流：hit 不进缺口；excluded 单列（复考信号）；none 进缺口
    并按当前库 re-probe 区分真缺/已被补上。

    缺口探针用零词块重叠的 query：'Kubernetes 集群调度' 这种会经'集群'
    bigram 撞上 Kafka 题面（三节点集群）——BM25 无阈值 + 公共块命中的
    老朋友（RAG 学习笔记），真缺判定会被它吃掉。"""
    corpus = _corpus(tmp_path)
    sessions = [_trace_session("s1", [
        {"query": "kafka", "hit_ids": ["bank-t-001"], "empty_hit": False},
        {"query": "Kafka 高可用", "hit_ids": [], "empty_hit": True,
         "empty_state": "excluded", "excluded_hit_ids": ["bank-t-001"]},
        {"query": "音视频编解码", "hit_ids": [], "empty_hit": True,
         "empty_state": "none", "excluded_hit_ids": []},
        {"query": "音视频编解码", "hit_ids": [], "empty_hit": True,
         "empty_state": "none", "excluded_hit_ids": []},
    ])]
    result = mine_mod.mine(sessions, corpus)
    assert result["sessions_with_trace"] == 1
    assert len(result["gaps"]) == 1
    g = result["gaps"][0]
    assert g["query"] == "音视频编解码" and g["count"] == 2
    assert g["still_empty"] is True and g["current_hit_ids"] == []
    assert result["excluded_top"][0]["query"] == "Kafka 高可用"
    assert result["hit_top"][0]["query"] == "kafka"


def test_old_gap_fixed_by_expansion(tmp_path):
    """re-probe 判'已被扩容补上'：老会话的空命中 query 现在能查到 → 不是
    真缺（历史信息留档，不进出题清单）。"""
    corpus = _corpus(tmp_path)
    sessions = [_trace_session("s1", [
        {"query": "kafka 宕机", "hit_ids": [], "empty_hit": True,
         "empty_state": "none", "excluded_hit_ids": []},
    ])]
    result = mine_mod.mine(sessions, corpus)
    assert result["gaps"][0]["still_empty"] is False
    assert result["gaps"][0]["current_hit_ids"] == ["bank-t-001"]


def test_legacy_session_without_trace(tmp_path):
    """老档兼容：Phase B 会话没有 retrieval_trace——不产缺口数据，
    transcript 质量信号（熔断/编造字样）仍然收割。"""
    corpus = _corpus(tmp_path)
    sessions = [{
        "session_id": "old-1",
        "transcript": [
            {"kind": "issue_done", "content": "（发题官收场：编造题库命中熔断（bank_qid=x，连续 2 次））"},
            {"kind": "issue", "content": "兜底题", "fallback_reason": "LLM 签发失败"},
        ],
    }]
    result = mine_mod.mine(sessions, corpus)
    assert result["sessions_with_trace"] == 0
    assert result["gaps"] == []
    assert len(result["quality_signals"]) == 2


def test_quality_signal_ignores_question_text(tmp_path):
    """issue 条目只看 fallback_reason 不扫题面正文——题面里"部分失败"
    这类词是题目的一部分，不是降级信号（真数据 1c937ab4d78c 误报教训）。"""
    corpus = _corpus(tmp_path)
    sessions = [{
        "session_id": "s-q",
        "transcript": [
            {"kind": "issue",
             "content": "如果接口调用遇到数据延迟、部分失败，你怎么设计兜底和重试？"},
        ],
    }]
    result = mine_mod.mine(sessions, corpus)
    assert result["quality_signals"] == []


def test_gap_count_orders_and_dedups_across_sessions(tmp_path):
    corpus = _corpus(tmp_path)
    sessions = [
        _trace_session("s1", [{"query": "音视频编解码", "hit_ids": [], "empty_hit": True,
                               "empty_state": "none"}]),
        _trace_session("s2", [{"query": "音视频编解码", "hit_ids": [], "empty_hit": True,
                               "empty_state": "none"},
                               {"query": "编译原理优化", "hit_ids": [], "empty_hit": True,
                               "empty_state": "none"}]),
    ]
    result = mine_mod.mine(sessions, corpus)
    assert [g["query"] for g in result["gaps"]] == ["音视频编解码", "编译原理优化"]  # 频次降序
    assert result["gaps"][0]["session_ids"] == ["s1", "s2"]


def test_candidate_face_trace_does_not_pollute_gaps(tmp_path):
    """ 双工具留痕：search_candidate 的空命中是"简历/JD 里没有这段"，
    不是题库缺题——三态聚合只吃题库面（tool 键分账；老档无 tool 键按题库
    面放行）。审查 high 项：不过滤时简历探针 query 会进真缺清单污染
    gen_bank_candidates 分配表。"""
    corpus = _corpus(tmp_path)
    sessions = [_trace_session("s1", [
        {"query": "量子物理研究所实习", "hit_ids": [], "empty_hit": True,
         "tool": "search_candidate"},
        {"query": "简历里的 Kafka 段落", "hit_ids": ["cand-0001"], "empty_hit": False,
         "tool": "search_candidate"},
        {"query": "音视频编解码", "hit_ids": [], "empty_hit": True,
         "empty_state": "none", "tool": "search_corpus"},
        {"query": "老档记录无 tool 键", "hit_ids": [], "empty_hit": True,
         "empty_state": "none"},
    ])]
    result = mine_mod.mine(sessions, corpus)
    # 同频次按 query 码位升序（"老" U+8000 < "音" U+97F3）
    assert [g["query"] for g in result["gaps"]] == ["老档记录无 tool 键", "音视频编解码"]


def test_still_empty_bigram_collision_semantics(tmp_path):
    """现状语义锁（闸关时）：re-probe 无相似度阈值，单个公共 bigram 命中
    即判"已被补上"——'量子纠缠通信' 只靠"通信"撞上 TCP 题就算已覆盖。
    真缺清单因此系统性偏保守（漏报多、误报少），与远域泄漏同根（RAG 学习
    笔记）。泄漏治理后的另一半修复见
    test_reprobe_gate_marks_far_collision_true_missing（闸开）。"""
    corpus = _corpus(tmp_path, extra_blocks=(_BLOCK_TCP,))
    sessions = [_trace_session("s1", [
        {"query": "量子纠缠通信", "hit_ids": [], "empty_hit": True, "empty_state": "none"},
    ])]
    g = mine_mod.mine(sessions, corpus)["gaps"][0]
    assert g["still_empty"] is False
    assert g["current_hit_ids"] == ["bank-t-002"]


def test_reprobe_gate_marks_far_collision_true_missing(tmp_path):
    """远域闸的 re-probe 修复（"检索+核验"两步）：
    同一个"量子纠缠通信"撞 TCP 题场景，闸判远域（全库无语义近邻）→ 仍算
    真缺。生产 main 显式传真闸（relevance_gate.should_gate_empty）；此处
    注入 lambda 离线锁行为——真闸要动 jina 模型，pytest 不碰真模型。"""
    corpus = _corpus(tmp_path, extra_blocks=(_BLOCK_TCP,))
    sessions = [_trace_session("s1", [
        {"query": "量子纠缠通信", "hit_ids": [], "empty_hit": True, "empty_state": "none"},
    ])]
    g = mine_mod.mine(sessions, corpus, gate=lambda q, c: True)["gaps"][0]
    assert g["still_empty"] is True            # BM25 有命中但闸判远域 → 真缺
    assert g["current_hit_ids"] == ["bank-t-002"]  # 撞上的垃圾命中仍留档（归因证据）


def test_tool_error_records_do_not_pollute_gaps(tmp_path):
    """工具自身失败（题库解析失败，record 带 error 键）≠ 题库缺题：坏库
    会话里每条 query 都失败，进缺口清单等于把整场会话的检索词全当缺题
（corrupt 按 error 键过滤）。"""
    corpus = _corpus(tmp_path)
    sessions = [_trace_session("s1", [
        {"query": "kafka", "hit_ids": [], "empty_hit": True,
         "empty_state": "corrupt", "error": "corpus_parse_failed"},
        {"query": "音视频编解码", "hit_ids": [], "empty_hit": True,
         "empty_state": "none"},
    ])]
    result = mine_mod.mine(sessions, corpus)
    assert [g["query"] for g in result["gaps"]] == ["音视频编解码"]


def test_suggested_tags_from_glossary(tmp_path):
    """词表第三消费方：缺题 query 命中的面 → canonical 作分配表 tags 候选；
    词表缺席 → 空列表（增强件降级不炸本体）。"""
    from interviewer.dataflows.glossary import load_glossary

    corpus = _corpus(tmp_path)
    sessions = [_trace_session("s1", [
        {"query": "kafka 消息可靠性", "hit_ids": [], "empty_hit": True, "empty_state": "none"},
    ])]
    gl = load_glossary(REPO_ROOT / "data" / "glossary.yaml")
    result = mine_mod.mine(sessions, corpus, glossary=gl)
    assert "kafka" in result["gaps"][0]["suggested_tags"]
    assert mine_mod.mine(sessions, corpus)["gaps"][0]["suggested_tags"] == []


def test_load_sessions_skips_bad_files(tmp_path):
    (tmp_path / "good.json").write_text(
        json.dumps({"session_id": "g", "retrieval_trace": [], "transcript": []}),
        encoding="utf-8")
    (tmp_path / "bad.json").write_text("{不是json", encoding="utf-8")
    sessions = mine_mod.load_sessions(tmp_path)
    assert [s["session_id"] for s in sessions] == ["g"]
