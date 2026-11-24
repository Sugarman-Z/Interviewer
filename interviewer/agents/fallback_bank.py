"""兜底题池：LLM 签发失败或考点重复时的确定性降级题源。
零 LLM、不依赖简历材料；source_ref 显式标注"兜底"，报告中如实披露。
"""
_FALLBACK_QUESTIONS = [
    {
        "module": "project",
        "topic": "项目概览",
        "text": "介绍一个你最有代表性的项目：背景、你的角色、最终结果。",
        "source_type": "generated",
        "source_ref": "兜底题：无材料依据（材料解析失败或为空）",
        "followup_hints": [],
    },
    {
        "module": "project",
        "topic": "难点深挖",
        "text": "这个项目里你遇到的最大技术困难是什么？你是怎么定位和解决的？",
        "source_type": "generated",
        "source_ref": "兜底题：无材料依据",
        "followup_hints": [],
    },
]

def next_unused_fallback(issued_keys: list[str]) -> dict | None:
    """ 返回第一个没有问过的_FALLBACK_QUESTIONS """
    for q in _FALLBACK_QUESTIONS:
        if q["topic"] not in issued_keys:
            return q
    return None