"""远域整单闸单测：判定数学 + fail-open 腿。
离线合同：真闸要动 jina 模型（嵌入跑分是真模型的事，同"评测脚本不进
pytest"边界）——本文件只锁 should_gate_empty 的判定语义与 None 处理，
gate_max_cosine 经 monkeypatch 注入。
"""
from interviewer.dataflows import relevance_gate


def test_below_threshold_gates_empty(monkeypatch):
    """s1 低于阈值 → 判远域（整批转空哨兵的扳机）。"""
    monkeypatch.setattr(relevance_gate, "gate_max_cosine",
                        lambda q, c: 0.3799)
    assert relevance_gate.should_gate_empty("量子纠缠是怎么回事", "./corpus") is True


def test_at_or_above_threshold_passes(monkeypatch):
    """s1 ≥ 阈值 → 放行（远域探针 max 0.3556、次高 0.3177，τ=0.38 带余量）；
    恰好等于阈值也放行——闸切的是"严格低于"。"""
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", lambda q, c: 0.38)
    assert relevance_gate.should_gate_empty("Kafka 高可用", "./corpus") is False
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", lambda q, c: 0.9)
    assert relevance_gate.should_gate_empty("redis 持久化", "./corpus") is False


def test_gate_unavailable_fails_open(monkeypatch):
    """闸不可用（None：fastembed/numpy 缺席、空库）→ 不加闸——增强件缺席
    不炸检索本体，行为回现状。"""
    monkeypatch.setattr(relevance_gate, "gate_max_cosine", lambda q, c: None)
    assert relevance_gate.should_gate_empty("任何查询", "./nope") is False


def test_constants_locked_to_calibration():
    """打分器与阈值 = 校准产物（leak_gate_2x2_2026-11-18.json 的 jina 列），
    换模型/换题库不重校就改这里 = 对着考卷改答案——常量锁防止静默漂移。"""
    assert relevance_gate.GATE_MODEL == "jinaai/jina-embeddings-v2-base-zh"
    assert relevance_gate.GATE_MIN_COSINE == 0.38
