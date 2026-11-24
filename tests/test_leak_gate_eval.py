"""泄漏闸门 2×2 实验的纯函数单测（scripts/eval_leak_gate_2x2.py）。
离线合同：只锁判决数学（AUROC/规则三/工作点/缺席核查），不碰
numpy/fastembed——实验本体（嵌入跑分）是真模型的事，同"评测脚本
不进 pytest"的既有边界。
"""
import importlib.util
from pathlib import Path

from interviewer.dataflows.bank_schema import BankQuestion

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate_mod = _load("eval_leak_gate_2x2")


def test_auroc_known_values():
    """完全分离=1.0、完全反序=0.0、全体并列=0.5；混合例手算 4/6。"""
    assert gate_mod.rank_auroc([0.9, 0.8], [0.3, 0.2]) == 1.0
    assert gate_mod.rank_auroc([0.1], [0.9, 0.8]) == 0.0
    assert gate_mod.rank_auroc([0.5, 0.5], [0.5, 0.5]) == 0.5
    # pos 0.9 胜 2、0.5 胜 0.3、0.4 胜 0.3 → 4/6
    assert abs(gate_mod.rank_auroc([0.9, 0.5, 0.4], [0.6, 0.3]) - 4 / 6) < 1e-12


def test_auroc_tie_counts_half():
    """并列记 0.5（平均秩）：0.5 平 0.5 + 胜 0.3、0.4 负 0.5 + 胜 0.3 → 2.5/4。"""
    assert abs(gate_mod.rank_auroc([0.5, 0.4], [0.5, 0.3]) - 0.625) < 1e-12


def test_rule_of_three_matches_survey_numbers():
    """调研文档仲裁二的锚数字：n=6→39.3%、n=40→7.2%、n=59→5.0%。"""
    assert abs(gate_mod.rule_of_three_upper(6) - 0.393) < 0.001
    assert abs(gate_mod.rule_of_three_upper(40) - 0.0722) < 0.001
    assert abs(gate_mod.rule_of_three_upper(59) - 0.050) < 0.001


def _fixture_rows():
    """四条 gold：两条稳过闸、一条被闸裁（rank 够但信号低于探针 max）、
    一条排序就丢（rank>5 是排序的账，不是闸的账）。"""
    return [
        {"query": "稳-1", "bucket": "verbatim", "rank": 1, "signal": 0.70},
        {"query": "稳-2", "bucket": "keyword", "rank": 2, "signal": 0.62},
        {"query": "闸裁", "bucket": "keyword", "rank": 3, "signal": 0.58},
        {"query": "排序丢", "bucket": "paraphrase", "rank": 7, "signal": 0.90},
    ], [0.40, 0.60]   # 探针两条，max=0.60


def test_zero_leak_point_and_gate_at():
    """零泄漏点：τ 刚越过 0.60 → 保留 2（0.58 被裁 = 闸门代价；rank7 是
    排序账）；τ=0.55 手算：泄漏 1（探针 0.60）、保留 3。"""
    rows, probes = _fixture_rows()
    z = gate_mod.zero_leak_point(probes, rows)
    assert z["leaks"] == 0 and z["gold_retained"] == 2
    assert z["retained_by_bucket"] == {"keyword": 1, "verbatim": 1}
    assert [r["query"] for r in z["lost"]] == ["闸裁", "排序丢"]   # 按信号升序
    g = gate_mod.gate_at(0.55, probes, rows)
    assert g["leaks"] == 1 and g["gold_retained"] == 3


def test_check_probes_flags_covered_and_passes_absent():
    """缺席核查 = parse 同口径的 casefold 子串：题面含 'Vue' 的题让
    forbidden=['vue'] 的近域探针违规；远域探针（无 forbidden_terms）恒过。"""
    questions = [
        BankQuestion(id="bank-fs-099", topic="框架", text="Vue 组件怎么通信",
                     tags=["vue"], followup_tree=["组合式 API？"]),
        BankQuestion(id="bank-be-099", topic="事务", text="锁的粒度",
                     tags=["数据库"], followup_tree=[]),
    ]
    probes = [
        {"query": "Vue 3 组合式 API 和 Options API 怎么选", "stratum": "near",
         "forbidden_terms": ["vue"]},
        {"query": "量子纠缠是怎么回事", "stratum": "far", "forbidden_terms": []},
    ]
    violations = gate_mod.check_probes(probes, questions)
    assert violations == [("Vue 3 组合式 API 和 Options API 怎么选", "vue")]
