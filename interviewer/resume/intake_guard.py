"""进件护栏：岗位/JD/简历的进件合理性判定。独立模块（不写在 CLI 输入环里）
是因为规则要被 CLI 与 web 进件端点共用——transport 只管交互循环，规则单点持有。

两层分工：规则层（check_* 纯函数，零依赖零 LLM）只硬拒"确定性垃圾"——
纯数字误触、无字母无汉字、超长粘贴；只判"肯定不是"，不判"像不像"。
语义层（semantic_guard，LLM 软判）判断"像不像真实的岗位/简历/JD"，只出
灰区确认、绝不硬拒——判定器自己会错，最终决定权留给用户（系统大声提醒，
用户按 y 才继续）。失败语义：semantic_guard 内部异常由调用方 fail-open
放行并提示"核验不可用"——进件提醒是增强件，缺席不挡面试本体。
"""
import json
import re
from dataclasses import dataclass, field


@dataclass
class IntakeVerdict:
    """判定三态：ok=False 硬拒（只有规则层会给）；needs_confirm=True 灰区
（用户确认后可继续）；reasons 是给人看的人话（也进日志/留痕）。"""

    ok: bool
    needs_confirm: bool = False
    reasons: list[str] = field(default_factory=list)


# 简历结构信号（规则层的"像简历"判据——命中任一即视为结构可信）。
# 刻意宽松：这里漏掉的由语义层兜，规则层只求零误杀。
_RESUME_MARKERS = re.compile(
    r"教育|经历|技能|项目|实习|工作|职责|负责|开发|维护|优化|大学|学院|学校"
    r"|本科|硕士|博士|GPA|邮箱|@|电话|手机|工程师|Developer|Engineer|Intern",
    re.IGNORECASE,
)


def _no_readable_text(s: str) -> bool:
    """没有任何字母/汉字（纯数字、符号、空白）——键盘误触/乱码的确定性形状。"""
    return not any(ch.isalpha() for ch in s)


def check_position(position: str) -> IntakeVerdict:
    """岗位：规则层硬拒确定性垃圾；过长/多行进灰区确认。"""
    s = position.strip()
    if not s:
        return IntakeVerdict(False, reasons=["岗位为空"])
    if len(s) < 2:
        return IntakeVerdict(False, reasons=["太短（不足 2 字符），不像岗位名"])
    if len(s) > 120:
        return IntakeVerdict(False, reasons=["超过 120 字符，像整段粘贴事故而非岗位名"])
    if _no_readable_text(s):
        return IntakeVerdict(False, reasons=["没有字母或汉字（纯数字/符号），像误触输入"])
    if len(s) > 6 and len({ch.casefold() for ch in s if ch.isalpha()}) < 3:
        return IntakeVerdict(False, reasons=["有效字符几乎全相同（键盘误触形状）"])
    reasons = []
    if len(s) > 60:
        reasons.append("很长，更像一句话而非岗位名（英文长头衔请确认）")
    if "\n" in s:
        reasons.append("包含换行（多行粘贴）")
    return IntakeVerdict(True, needs_confirm=bool(reasons), reasons=reasons)


def check_jd(jd: str) -> IntakeVerdict:
    """JD（可选字段）：空=合法跳过；误触形状硬拒；超长进灰区。"""
    s = jd.strip()
    if not s:
        return IntakeVerdict(True)
    if _no_readable_text(s):
        return IntakeVerdict(False, reasons=["没有字母或汉字（纯数字/符号），像误触输入"])
    if len(s) > 3000:
        return IntakeVerdict(True, needs_confirm=True,
                             reasons=["JD 超过 3000 字，像整篇文档而不是岗位描述"])
    return IntakeVerdict(True)


def check_resume_text(text: str) -> IntakeVerdict:
    """简历正文：文件层（编码/扫描件/超长）已由 file_loader 把过关，这里管
    语义形状——太短、无任何结构信号 → 灰区确认（用户可能真只想贴三行
    技能清单，不替他做主）。空文本=用户跳过简历，由调用方处理。"""
    s = text.strip()
    if not s:
        return IntakeVerdict(True)
    if len(s) < 20:
        return IntakeVerdict(True, needs_confirm=True,
                             reasons=["内容很短（不足 20 字），不像简历正文"])
    if not _RESUME_MARKERS.search(s):
        return IntakeVerdict(True, needs_confirm=True, reasons=[
            "未找到简历常见结构信号（经历/技能/项目/教育/联系方式）",
        ])
    return IntakeVerdict(True)


# 语义层（LLM 软判）

# MockLLM 按这个标记分流（llm.py 的进件核验分支）——改这里必须同批改那边。
GUARD_MARKER = "进件核验官"

_GUARD_PROMPT = """{marker}：下面是一场模拟面试的进件材料。分别判断它们像不像真实有效的
【岗位名称】【简历正文】【岗位JD】。判定保守：拿不准一律给 "unsure"，不要替用户
断言无效；理由用一句中文。请输出 JSON（三键固定；对应输入为空时照填 valid）：
{{"position": {{"verdict": "valid|invalid|unsure", "reason": "…"}},
  "resume":   {{"verdict": "valid|invalid|unsure", "reason": "…"}},
  "jd":       {{"verdict": "valid|invalid|unsure", "reason": "…"}}}}

岗位：{position}
简历：{resume}
JD：{jd}"""


def semantic_guard(llm, position: str, jd: str, resume: str) -> IntakeVerdict:
    """LLM 语义软判 → 只出灰区确认。任何异常由调用方 fail-open（本函数自身
    不吞——调用方要区分"核验不可用"与"核验通过"）。llm 是任何有
    .invoke(prompt) 的客户端，依赖注入而非自建，测试离线可跑。
    """
    prompt = _GUARD_PROMPT.format(
        marker=GUARD_MARKER,
        position=position.strip()[:200] or "（空）",
        resume=resume.strip()[:800] or "（空）",
        jd=jd.strip()[:400] or "（空）",
    )
    content = str(llm.invoke(prompt).content)
    data = _extract_json(content)
    reasons = []
    labels = (("岗位", "position", position), ("简历", "resume", resume),
              ("JD", "jd", jd))
    for label, key, raw in labels:
        if not raw.strip():
            continue                     # 空输入不判（空=用户跳过，合法）
        item = data.get(key) if isinstance(data, dict) else None
        verdict = item.get("verdict", "") if isinstance(item, dict) else ""
        if verdict not in ("valid", "", None):
            reason = str(item.get("reason", "")).strip() or verdict
            reasons.append(f"{label}存疑：{reason}")
    return IntakeVerdict(True, needs_confirm=bool(reasons), reasons=reasons)


def _extract_json(content: str) -> dict:
    """从模型回复里抠第一个 JSON 对象。抠不出 → {}（调用方视为全部通过，
    fail-open 的解析腿——判定器输出坏了不该挡面试）。"""
    start = content.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(content[start:i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}
