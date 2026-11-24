"""签发质量 e2e 脚本测试（离线 mock 自检路径）。
真 API 不进 pytest（marker 纪律）——这里锁的是管道：A/B 两配置跑通、
留痕归因聚合正确、judge 两腿（绝对分/pairwise）解析成功、落盘形状完整。
mock 的数字无科学意义（MockJudgeLLM 恒 4/5/4 与 tie）。
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "scripts" / "data" / "e2e"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules：pydantic v2 解析嵌套模型（JudgeReport→JudgeItem）
    # 要按 __module__ 查命名空间，不注册 = "not fully defined" 运行时炸
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


e2e = _load("eval_issue_e2e")


def _fixture():
    return e2e._fixture(FIXTURE_DIR)


def test_run_config_ab_both_issue_questions():
    """A/B 两配置都能签发（mock 走 freetext 降级腿），B 的工具在环。"""
    from interviewer.llm import MockLLM

    fx = _fixture()
    for name in e2e.CONFIGS:
        run = e2e.run_config(
            MockLLM("quick"), e2e._config(name, fx["corpus_dir"]),
            fx, n_issues=3, structured_method="function_calling")
        assert len(run["questions"]) == 3
        assert all(q["text"] for q in run["questions"])
        # mock 不发工具调用 → trace 恒空（管道不依赖工具真的被调）
        assert run["trace"] == []


def test_config_difference_is_candidate_face_only():
    """对照组的手术纪律：A/B 唯一差异 = candidate_search_enabled。"""
    fx = _fixture()
    a, b = (e2e._config(n, fx["corpus_dir"]) for n in ("A_bank", "B_candidate"))
    diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    assert diff == {"candidate_search_enabled"}
    assert a["candidate_search_enabled"] is False and b["candidate_search_enabled"] is True


def test_summarize_run_attribution():
    """留痕归因：tool 键分账、空命中、source_type 计数、兜底计数。"""
    run = {
        "questions": [
            {"source_type": "bank"}, {"source_type": "generated"},
            {"source_type": "bank"},
        ],
        "trace": [
            {"tool": "search_corpus", "hit_ids": ["x"], "empty_hit": False},
            {"tool": "search_corpus", "hit_ids": [], "empty_hit": True},
            {"tool": "search_corpus", "hit_ids": [], "empty_hit": True,
             "error": "corpus_parse_failed"},
            {"tool": "search_candidate", "hit_ids": ["cand-0001"], "empty_hit": False},
        ],
        "transcript": [
            {"kind": "issue", "content": "t", "fallback_reason": "考点重复"},
            {"kind": "issue", "content": "t"},
        ],
    }
    s = e2e.summarize_run(run)
    assert s == {
        "questions": 3,
        "tool_calls": {"search_corpus": 3, "search_candidate": 1},
        "tool_empty_hits": {"search_corpus": 1},      # error 记录不进空命中桶
        "tool_errors": {"search_corpus": 1},          # 工具失败单列（审查修正）
        "source_types": {"bank": 2, "generated": 1},
        "fallback_used": 1,
    }


def test_mock_end_to_end_pipeline_writes_report(tmp_path, capsys):
    """main() 的 mock 全链自检：两配置 + 双腿 judge + 落盘 JSON 形状。"""
    rc = e2e.main(["--provider", "mock", "--issues", "3", "--out-dir", str(tmp_path)])
    assert rc == 0
    out_files = list(tmp_path.glob("issue_e2e_mock_*.json"))
    assert len(out_files) == 1
    data = json.loads(out_files[0].read_text(encoding="utf-8"))
    assert set(data["configs"]) == set(e2e.CONFIGS)
    for name in e2e.CONFIGS:
        assert len(data["questions"][name]) == 3
        assert data["absolute"][name] and len(data["absolute"][name]["scores"]) == 3
    assert data["pairwise"]["overall"] == "tie"
    assert "（mock）" in data["pairwise"]["reason"]
