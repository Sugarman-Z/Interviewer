from interviewer.agents.fallback_bank import next_unused_fallback

# test fallback bank has result scenario
def test_next_unused_fallback_has_result():
    # 场景 1：无issued_keys
    q1 = next_unused_fallback(issued_keys=[])
    assert q1["topic"] == "项目概览"

    # 场景 2：有issued_keys，但仍有题库
    q2 = next_unused_fallback(issued_keys=["项目概览"])
    assert q2["topic"] == "难点深挖"

# test fallback bank has no result scenario
def test_next_unused_fallback_no_result():
    # 场景 3：题库耗尽
    q = next_unused_fallback(issued_keys=["项目概览", "难点深挖"])
    assert q is None