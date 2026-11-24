"""评分解析：解析不出来就显式标记 eval_failed，绝不静默给"及格分"——
坏 case 显式暴露是评估系统可信度的底线。action="advance" 是保守方向：
评分失败绝不默认追问。
"""
REQUIRED_KEYS = ("action",)


def validate_scorecard(scorecard: dict | None) -> dict:
    """校验评分卡最小字段集；不完整则返回 eval_failed 失败 dict
（路由②走显式降级出口，绝不默认及格）。"""
    if not isinstance(scorecard, dict) or any(k not in scorecard for k in REQUIRED_KEYS):
        return {"eval_failed": True, "action": "advance", "unscored": True}
    return scorecard
