"""lint 测试（离线）：report-only 语义锁。
预埋的问题（topic 撞车/text 重复/tag 多拼写/弱题）逐条出现在报告里；
干净语料零 ⚠（规则太严会让手写题库处处飘红被弃用——反向也要锁）；
交叉检查：staging 候选与正式库撞 id → 报告点破。
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


lint_mod = _load("lint_bank")


def _q(num: int, topic: str, **over) -> BankQuestion:
    base = dict(
        id=f"bank-l-{num:03d}", topic=topic, difficulty=3, tags=["测试"],
        text=f"{topic}的场景式题面", followup_tree=["追问一？", "追问二？", "追问三？"],
        scoring_anchors=["5 分：清", "3 分：中", "1 分：糊"], source="测试·2026-01-01",
        module="project",
    )
    base.update(over)
    return BankQuestion(**base)


def test_lint_reports_planted_issues():
    report = "\n".join(lint_mod.lint([
        _q(1, "考点甲"),
        _q(2, "考点甲", tags=["redis"]),                        # topic 撞车
        _q(3, "考点乙", text="考点甲的场景式题面", tags=["Redis"]),  # text 重复 + tag 多拼写
        _q(4, "考点丙", followup_tree=["只有一条？"], tags=["独苗"]),  # 弱题 + 孤儿 tag
    ]))
    assert "topic 撞车" in report
    assert "题面重复" in report
    assert "同概念多拼写" in report and "redis" in report
    assert "追问树 1 条" in report
    assert "孤儿 tag" in report and "独苗" in report
    assert "覆盖矩阵" in report


def test_lint_clean_corpus_has_no_warnings():
    report_lines = lint_mod.lint([_q(1, "考点甲"), _q(2, "考点乙")])
    warns = [ln for ln in report_lines if ln.strip().startswith("⚠")]
    assert not warns, warns


def test_lint_cross_check_flags_bank_collision():
    report = "\n".join(lint_mod.lint(
        [_q(1, "staging 考点")],
        cross=[_q(1, "正式库考点")],  # 同 id 不同 topic：id 撞车也该报
    ))
    assert "id 与正式库撞车" in report


def test_lint_glossary_section():
    """tag↔词表对账节：未知 tag ⚠ 报告（promote 侧同判据硬闸的
    report-only 前置）；glossary=None 整节缺席（fixture 语料不必配真词表）。"""
    from interviewer.dataflows.glossary import Glossary

    gl = Glossary(faces={"测试": ["测试"]}, surfaces={"测试": "测试"})
    report = lint_mod.lint([_q(1, "考点甲")], glossary=gl)
    assert "tag ↔ 词表对账" in "\n".join(report)
    assert "✓ 1 个 tag 全部在表" in "\n".join(report)

    report_unknown = lint_mod.lint(
        [_q(1, "考点甲", tags=["测试", "神秘新tag"])], glossary=gl)
    assert "tag 未入词表：神秘新tag" in "\n".join(report_unknown)

    assert "词表对账" not in "\n".join(lint_mod.lint([_q(1, "考点甲")]))
