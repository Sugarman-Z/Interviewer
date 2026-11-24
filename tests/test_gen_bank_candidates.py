"""出题脚本测试（离线）：假 LLM 注入，真 API 不进 pytest。
锁三件事：分配表对齐（数量/id 对不上拒绝产出，宁可不写文件也不写半批）；
staging 路径防线（直接父目录非 `_` 前缀 → 拒绝写入）；产物可解析且隔离
（直接指 staging 目录能 parse，从上层看不可见——与 test_staging_isolation
互为对账）。
"""
import importlib.util
from pathlib import Path

import pytest

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.local_corpus import parse_corpus

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("gen_bank_candidates")

SPEC = [
    {"id": "bank-g-001", "topic": "假题一", "difficulty": 3, "tags": ["测试"], "hint": ""},
    {"id": "bank-g-002", "topic": "假题二", "difficulty": 2, "tags": ["测试"], "hint": ""},
]


class _FakeStructured:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, prompt):
        return gen.QuestionList(questions=self.payload)


class FakeLLM:
    """与 create_llm_client 产物同形：有 with_structured_output。"""

    def __init__(self, payload):
        self.payload = payload

    def with_structured_output(self, schema, method=None):
        assert method == "function_calling"  # 2026-09-16 冒烟选型，不许悄悄换
        return _FakeStructured(self.payload)


def _draft(spec_item, **over):
    base = dict(
        id=spec_item["id"],
        topic=spec_item["topic"],
        difficulty=spec_item["difficulty"],
        tags=list(spec_item["tags"]),
        text=f"{spec_item['topic']}的场景式题面，直接对候选人说话，两到三句。",
        followup_tree=["追问一？", "追问二？", "追问三？"],
        scoring_anchors=["5 分：清", "3 分：中", "1 分：糊"],
        source="LLM 草拟·2026-11-16",
    )
    base.update(over)
    return gen.QuestionDraft(**base)


def test_generate_and_write_staging(tmp_path, monkeypatch):
    llm = FakeLLM([_draft(s) for s in SPEC])
    questions = gen.generate_candidates(llm, SPEC, "2026-11-16")

    assert [q.id for q in questions] == ["bank-g-001", "bank-g-002"]
    assert all(isinstance(q, BankQuestion) for q in questions)

    monkeypatch.setattr(gen, "STAGING_DIR", tmp_path / "_staging")
    out = gen.write_staging("g.md", "假题集", questions)
    assert out.parent == (tmp_path / "_staging").resolve()

    # 产物可解析；从上层目录看不可见（staging 隔离口径）
    assert [q.id for q in parse_corpus(tmp_path / "_staging")] == ["bank-g-001", "bank-g-002"]
    assert parse_corpus(tmp_path) == []


def test_generate_refuses_misaligned_output(tmp_path):
    """LLM 少交一题 → 对齐校验拒绝（不产出半批）。"""
    llm = FakeLLM([_draft(SPEC[0])])  # 只交了第一条
    with pytest.raises(ValueError, match="不对齐"):
        gen.generate_candidates(llm, SPEC, "2026-11-16")


def test_write_staging_refuses_non_underscore_dir(tmp_path, monkeypatch):
    """防线：目标目录不带 `_` 前缀（如误指 corpus/project）→ 拒绝执行。"""
    monkeypatch.setattr(gen, "STAGING_DIR", tmp_path / "project")
    d = _draft(SPEC[0]).model_dump()
    with pytest.raises(ValueError, match="staging 隔离"):
        gen.write_staging("g.md", "假题集", [BankQuestion(**d)])


def test_mock_llm_rejected_with_human_message():
    """无 with_structured_output 的客户端（mock 模式）→ 人话报错，不神秘炸。"""
    class _NoStructured:
        pass

    with pytest.raises(TypeError, match="mock 模式"):
        gen.generate_candidates(_NoStructured(), SPEC, "2026-11-16")
