"""JD/岗位输入解析。岗位是用户声明的事实（防捏造第一闸），必填。"""
def parse_position_input(position: str, raw_jd: str = "") -> dict:
    name = (position or "").strip()
    if not name:
        raise ValueError("岗位必填：agent 不得从简历推断面试岗位（防捏造第一闸）")
    return {"position": name, "requirements": (raw_jd or "").strip()}
