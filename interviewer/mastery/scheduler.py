"""遗忘曲线调度器：纯确定性代码，零 LLM。纪律："隔 N 天"只看错题记录里的
时间戳，不用 LLM 算数；历史重放只统计当时已存在的记录；全部离线单测覆盖
时间边界（固定时钟 monkeypatch）。
"""
from datetime import date


def next_review_date(created_at: date, wrong_count: int, intervals: list[int]) -> date:
    """按答错次数在间隔表里取档：错 1 次 +1 天，错 2 次 +3 天……超出取最大档。"""
    idx = min(max(wrong_count - 1, 0), len(intervals) - 1)
    return date.fromordinal(created_at.toordinal() + intervals[idx])


def is_due(entry: dict, today: date) -> bool:
    return entry.get("next_review", today) <= today


def get_due_reviews(entries: list[dict], today: date) -> list[dict]:
    """W4 由 ReviewSettler 调用；当前错题本尚未建立，entries 恒为空。"""
    return [e for e in entries if is_due(e, today)]
