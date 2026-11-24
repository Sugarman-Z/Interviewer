"""进件护栏单测：规则层硬拒边界 + 语义层灰区确认 + fail-open 三条腿。
两层分工的锁法（与 intake_guard 模块头互为合同）：规则层只硬拒"确定性
垃圾"，测试钉每条规则的边界，尤其零误杀侧（"后端"这种两字真岗位不许被
重复字符规则吃掉）；语义层永远 ok=True（只灰区不硬拒），解析失败/空输入
fail-open；MockLLM 的"进件核验官"分流锚（llm.py 同批）——改标记必须
两侧同改。
"""
from interviewer.llm import MockLLM
from interviewer.resume.intake_guard import (
    GUARD_MARKER,
    _extract_json,
    check_jd,
    check_position,
    check_resume_text,
    semantic_guard,
)


# 规则层：岗位

def test_position_hard_rejects_deterministic_garbage():
    """确定性垃圾硬拒：纯数字误触（用户真实翻车样本"23455…"）、纯符号、
    单字符、超长粘贴事故、有效字符近全同。"""
    for bad in ("234555555555555", "12345678901", "@@@@$$$", "a",
                "x" * 130, "哈哈哈哈哈哈哈哈"):
        v = check_position(bad)
        assert v.ok is False and v.reasons, bad


def test_position_normal_and_gray():
    """零误杀侧：两字中文岗位/正常英文头衔直接过；真实形状的超长头衔与
    多行粘贴进灰区（needs_confirm 不硬拒——决定权在用户）。同字母长跑
（"S"×70）是误触形状，走硬拒不走灰区。"""
    assert check_position("后端").ok is True
    assert check_position("后端").needs_confirm is False
    assert check_position("Senior Software Engineer, Payments").needs_confirm is False
    assert check_position("S" * 70).ok is False          # 误触形状：硬拒
    v = check_position("Senior Staff Software Engineer, Platform Infrastructure"
                       " & Developer Experience (Remote/Hybrid)")
    assert v.ok is True and v.needs_confirm is True      # 真实长头衔：灰区
    v = check_position("后端开发\n顺便帮我写个简历")
    assert v.ok is True and v.needs_confirm is True


# 规则层：JD 与简历

def test_jd_empty_ok_and_garbage_rejected():
    assert check_jd("").ok is True and check_jd("").needs_confirm is False
    assert check_jd("88888888888").ok is False
    assert check_jd("负责支付系统的设计与维护").needs_confirm is False
    assert check_jd("字" * 3001).needs_confirm is True


def test_resume_text_structure_signal():
    """结构信号：命中简历标记词 → 直接过；菜谱/超短文本 → 灰区不硬拒；
    空 = 用户跳过路径，不判。"""
    assert check_resume_text("").needs_confirm is False
    assert check_resume_text("工作经历：FNZ Analyst Developer，负责清算系统").needs_confirm is False
    assert check_resume_text("红烧肉五花的做法：先焯水，炒糖色，小火炖四十分钟").needs_confirm is True
    assert check_resume_text("会 Java").needs_confirm is True   # 太短（<20 字）


# 语义层：LLM 软判

class _StubLLM:
    def __init__(self, content):
        self._content = content

    def invoke(self, prompt):
        class _Msg:
            content = None
        _Msg.content = self._content
        return _Msg()


def test_semantic_guard_invalid_becomes_confirm_never_hard_reject():
    """语义判 invalid → 灰区确认（绝不硬拒）；理由带字段名给用户看。"""
    llm = _StubLLM('{"position": {"verdict": "invalid", "reason": "像一道菜名"},'
                   ' "resume": {"verdict": "valid", "reason": ""},'
                   ' "jd": {"verdict": "valid", "reason": ""}}')
    v = semantic_guard(llm, "红烧肉怎么做", "", "")
    assert v.ok is True and v.needs_confirm is True
    assert any("岗位" in r and "菜名" in r for r in v.reasons)


def test_semantic_guard_skips_empty_inputs_and_valid_all():
    llm = _StubLLM('{"position": {"verdict": "valid", "reason": ""},'
                   ' "resume": {"verdict": "invalid", "reason": "是一篇小说"},'
                   ' "jd": {"verdict": "valid", "reason": ""}}')
    v = semantic_guard(llm, "后端开发", "", "")   # 简历为空 → 不判
    assert v.needs_confirm is False


def test_semantic_guard_unparseable_fails_open():
    """回复抠不出 JSON → 全过（fail-open 解析腿）：判定器输出坏了不挡面试。"""
    v = semantic_guard(_StubLLM("我觉得挺好的"), "后端开发", "", "简历")
    assert v.ok is True and v.needs_confirm is False
    assert _extract_json("前置说明 {\"a\": 1} 后置") == {"a": 1}


def test_mock_llm_guard_branch_anchor():
    """MockLLM 的进件核验分流（锚 = GUARD_MARKER；排在"请输出 JSON"之前，
    否则核验 prompt 会被签发分支劫持）——mock CLI 流程因此不中断。"""
    out = MockLLM().invoke(f"{GUARD_MARKER}：……请输出 JSON ……")
    assert '"position"' in str(out.content) and '"valid"' in str(out.content)
