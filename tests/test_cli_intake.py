"""CLI 进件环与四模式分发单测。
_resolve：三态裁决合同（干净通过/灰区确认/红字打回，三环同为 fail-visible）；
_select_session：四模式分发的每个出口（续面选中/退出码），rows+args 纯输入，
只打印不问问题；_intake：三环走 monkeypatch 的 input 序列（垃圾打回重输/
干净通过/语义核验终止），语义核验打桩——语义层合同在 test_intake_guard 锁。
"""
import pytest

from interviewer.cli.app import _intake, _parse_args, _resolve, _select_session
from interviewer.default_config import get_config
from interviewer.resume.intake_guard import IntakeVerdict


class _Recorder:
    """_PlainConsole 同款最小 console，顺带记下打印内容供断言。"""

    def __init__(self):
        self.lines = []

    def print(self, *args, **kwargs):
        self.lines.append(" ".join(str(a) for a in args))


def _inputs(seq):
    """固定序列变 input() 桩：越界即抛（测试不该问多余的问题）。"""
    it = iter(seq)

    def fake(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"意外的多余输入请求：{prompt}")

    return fake


# _resolve：三环共用的裁决收敛


def test_resolve_clean_pass(monkeypatch):
    """干净通过不打扰：不打印、不问任何问题。"""
    monkeypatch.setattr("builtins.input", _inputs([]))
    console = _Recorder()
    assert _resolve(console, IntakeVerdict(True), "岗位") is True
    assert console.lines == []


def test_resolve_confirm_gate(monkeypatch):
    """灰区决定权在用户：y 放行、其他打回。"""
    console = _Recorder()
    monkeypatch.setattr("builtins.input", _inputs(["y", "n"]))
    grey = IntakeVerdict(True, needs_confirm=True, reasons=["很长，更像一句话"])
    assert _resolve(console, grey, "JD") is True
    assert _resolve(console, grey, "JD") is False


def test_resolve_rejects_visible(monkeypatch):
    """not ok → 红字原因 + 打回（三环 fail-visible 的单点锁）。"""
    monkeypatch.setattr("builtins.input", _inputs([]))
    console = _Recorder()
    verdict = IntakeVerdict(False, reasons=["没有字母或汉字"])
    assert _resolve(console, verdict, "简历内容") is False
    assert any("没有字母或汉字" in line for line in console.lines)


# _select_session：四模式分发


def _rows():
    return [
        {"session_id": "s-new", "ongoing": True, "position": "后端", "asked": 1},
        {"session_id": "s-done", "ongoing": False, "position": "前端", "asked": 3},
    ]


def test_select_list_prints_and_exits_zero(capsys):
    console = _Recorder()
    assert _select_session(_parse_args(["--list"]), _rows(), console) == 0
    assert "s-new" in capsys.readouterr().out      # --list 走裸 print（stdout）


def test_select_list_empty():
    assert _select_session(_parse_args(["--list"]), [], _Recorder()) == 0


def test_select_continue_picks_latest_ongoing():
    console = _Recorder()
    got = _select_session(_parse_args(["--continue"]), _rows(), console)
    assert got == ("s-new", True)


def test_select_continue_without_ongoing_fails_loud(capsys):
    assert _select_session(_parse_args(["--continue"]), [_rows()[1]], _Recorder()) == 1
    assert "没有未完成会话" in capsys.readouterr().err


def test_select_resume_unknown_id_fails_loud(capsys):
    assert _select_session(_parse_args(["--resume", "nope"]), _rows(), _Recorder()) == 1
    assert "不存在" in capsys.readouterr().err


def test_select_resume_finished_exits_zero(capsys):
    assert _select_session(_parse_args(["--resume", "s-done"]), _rows(), _Recorder()) == 0
    assert "已收场" in capsys.readouterr().out


def test_select_resume_ongoing():
    console = _Recorder()
    got = _select_session(_parse_args(["--resume", "s-new"]), _rows(), console)
    assert got == ("s-new", True)


def test_select_default_starts_new_and_hints_ongoing():
    console = _Recorder()
    sid, mode = _select_session(_parse_args([]), _rows(), console)
    assert mode is False and sid                # 新场：uuid + 非续面
    assert any("未完成会话" in line for line in console.lines)


# _intake：三环收集


@pytest.fixture()
def intake_env(monkeypatch):
    def make(seq, semantic=True):
        console = _Recorder()
        monkeypatch.setattr("builtins.input", _inputs(seq))
        monkeypatch.setattr("interviewer.cli.app._semantic_check", lambda *a: semantic)
        return console, get_config()

    return make


def test_intake_happy_path(intake_env):
    """岗位 + 跳过 JD + 跳过简历：三问即出，岗位原样透传（jd_parser 纯透传）。"""
    console, config = intake_env(["后端开发工程师", "", ""])
    got = _intake(console, config)
    assert got is not None
    assert got[0] == "后端开发工程师" and got[1] == ""


def test_intake_garbage_position_retried(intake_env):
    """纯数字岗位被红字打回后重输——_resolve 的 not ok 路径穿过真实规则层。"""
    console, config = intake_env(["123", "后端开发工程师", "", ""])
    got = _intake(console, config)
    assert got[0] == "后端开发工程师"
    assert any("岗位输入无效" in line for line in console.lines)


def test_intake_semantic_abort_returns_none(intake_env):
    """语义核验处用户终止：返回 None 并打印终止提示（main 只看 None 退出）。"""
    console, config = intake_env(["后端开发工程师", "", ""], semantic=False)
    assert _intake(console, config) is None
    assert any("已终止" in line for line in console.lines)


def test_main_intake_abort_exits_clean(monkeypatch, tmp_path, capsys):
    """main 级回归锁（批 2 对抗审查发现）：语义核验终止路径必须干净
    return 0——曾在 _intake 的 None 上先解包后判空，TypeError 炸穿 main。"""
    from interviewer.cli.app import main

    cfg = get_config()
    cfg["llm_provider"] = "mock"
    cfg["checkpoint_backend"] = "memory"
    cfg["data_dir"] = str(tmp_path / "data")
    monkeypatch.setattr("interviewer.cli.app._load_config", lambda: cfg)
    monkeypatch.setattr("builtins.input", _inputs(["后端开发工程师", "", ""]))
    monkeypatch.setattr("interviewer.cli.app._semantic_check", lambda *a: False)

    assert main([]) == 0
    assert "已终止" in capsys.readouterr().out
