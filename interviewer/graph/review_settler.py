"""入口补考结算（W4 启用）。同构映射：面试挂掉的考点 = 挂 pending；隔几天
补考 = 下次会话入口结算；AI 复盘错因；写回错题本。结算纪律：数据不可得
（用户没来补考）保持 pending 下次再试，绝不编造结果。
"""
def settle_due_reviews(mistake_book, today) -> list[dict]:
    """读错题本 pending 条目 → Scheduler 挑出到期 → 生成变形题 → 排队。
    当前返回空列表：错题本 W3 才建立，结构先立、行为后到。"""
    return []
