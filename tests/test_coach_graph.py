"""CoachGraph 组合根测试：图形状签名（纪律 #7）与 invoke_config 装配。

不建图（不 build）——组合根测试只锁"签名/配置怎么算"，图行为归 test_smoke。
"""
from interviewer.graph.coach_graph import CoachGraph, graph_signature


def _config(**over):
    cfg = {
        "graph_version": "w2-dynamic",
        "modules": ["project"],
        "max_followups": 2,
        "max_total_turns": 24,
        "max_questions": 10,
        "max_consecutive_llm_failures": 2,
        "max_recur_limit": 100,
        "llm_provider": "mock",
    }
    cfg.update(over)
    return cfg


def test_graph_signature_changes_with_shape():
    """图形状变 → 签名变 → thread_id 变 → 旧 checkpoint 自动失联。
（TA #1089 教训：否则旧会话静默续跑在错误的图上。）"""
    assert graph_signature(_config()) != graph_signature(_config(graph_version="w3-multi"))
    assert graph_signature(_config()) != graph_signature(_config(max_followups=3))


def test_graph_signature_covers_all_routing_limits():
    """签名键集锁（2026-11-13 评审修复）：路由决策表读的每个上限都必须掺——
    漏一个 = 改上限后旧会话静默换预算续跑（当时正是漏了 max_questions）。
    反向锁：max_recur_limit 是 invoke 期执行预算，不掺——改它不作废会话。"""
    sig = graph_signature(_config())
    for key, changed in [
        ("max_questions", 11),
        ("max_consecutive_llm_failures", 3),
        ("max_total_turns", 25),
        ("max_followups", 3),
    ]:
        assert graph_signature(_config(**{key: changed})) != sig, f"{key} 未掺进签名"
    assert graph_signature(_config(max_recur_limit=200)) == sig  # 执行预算不进签名


def test_graph_signature_stable_when_unchanged():
    """同形状必须同签名（哈希确定性——不稳定会让每场面试都"失联"）。"""
    assert graph_signature(_config()) == graph_signature(_config())


def test_invoke_config_carries_recursion_limit():
    """每次 invoke 的 config 都带显式 recursion_limit（langgraph 默认 25 会撞）
    与掺了签名的 thread_id。"""
    coach = CoachGraph(_config(), checkpointer=object())  # 只装配不 build
    cfg = coach.invoke_config("sess1")
    assert cfg["recursion_limit"] == 100
    assert "sess1" in cfg["configurable"]["thread_id"]
