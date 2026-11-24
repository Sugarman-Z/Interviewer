"""简历/JD 解析（W1 粘贴版；PDF/结构化抽取 W2 引入）。

fail-open 纪律（TA resolve_instrument_identity 同款）：解析失败不阻塞——
降级为无画像面试并带 degraded 标记，绝不崩在会话入口。
"""
def parse_pasted_resume(raw: str) -> dict:
    """W1：简历即粘贴原文，原文本身就是事实锚（W2 换 PDF 解析 + LLM 结构化）。"""
    text = (raw or "").strip()
    return {"raw": text, "structured": None, "degraded": text == ""}


def candidate_profile_text(parsed: dict) -> str:
    if parsed.get("raw"):
        return parsed["raw"]
    return "(未提供简历——仅依据岗位与自我介绍面试)"
