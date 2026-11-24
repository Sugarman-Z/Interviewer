"""配置系统单测：ENV 覆盖、类型强转 fail-loud（测 _coerce 机制本身）、深拷贝隔离。

plan_review_enabled 退役后项目暂无布尔配置键——布尔强转直接测 _coerce：
测机制不测某个键，以后键退役/新增不再牵连本测试。
"""
import pytest

from interviewer.default_config import _coerce, get_config


def test_env_override_and_coercion(monkeypatch):
    monkeypatch.setenv("INTERVIEWER_MAX_FOLLOWUPS", "5")
    monkeypatch.setenv("INTERVIEWER_MAX_QUESTIONS", "8")
    monkeypatch.setenv("INTERVIEWER_LLM_PROVIDER", "mock")
    monkeypatch.setenv("INTERVIEWER_MODULES", "project,behavior")
    cfg = get_config()
    assert cfg["max_followups"] == 5 and isinstance(cfg["max_followups"], int)
    assert cfg["max_questions"] == 8
    assert cfg["llm_provider"] == "mock"
    assert cfg["modules"] == ["project", "behavior"]


def test_bool_coercion_fails_loud():
    """拼错的布尔值必须当场炸，不静默用默认（fail-loud）。"""
    with pytest.raises(ValueError):
        _coerce("maybe", True)
    assert _coerce("true", True) is True
    assert _coerce("off", False) is False
    assert _coerce("0", False) is False


def test_get_config_returns_deepcopy(monkeypatch):
    monkeypatch.delenv("INTERVIEWER_MAX_FOLLOWUPS", raising=False)
    cfg = get_config()
    cfg["max_followups"] = 99
    assert get_config()["max_followups"] == 3  # 2→3（降级为熔断线后放宽）
