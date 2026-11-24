# 归因二：Accept-Encoding 对代理流式透传的影响（identity vs gzip）
import json
import time

import httpx

BODY = {"type": "answer", "text": "自我介绍：后端工程师五年，做风控系统。", "expect_qnum": "opening"}

with httpx.Client(base_url="http://[::1]:3000", timeout=120.0, trust_env=False) as c:
    r = c.post("/api/sessions", json={"position": "诊断编码"})
    sid = r.json()["session_id"]
    t0 = time.monotonic()
    with c.stream(
        "POST", f"/api/sessions/{sid}/answers/stream", json=BODY,
        headers={"Accept-Encoding": "identity"},
    ) as resp:
        stamps = []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                stamps.append((round(time.monotonic() - t0, 2), json.loads(line[6:])["event"]))
        print(f"[identity] {len(stamps)} 事件，时刻 {stamps}")

with httpx.Client(base_url="http://[::1]:3000", timeout=120.0, trust_env=False) as c:
    r = c.post("/api/sessions", json={"position": "诊断编码"})
    sid2 = r.json()["session_id"]
    t1 = time.monotonic()
    with c.stream(
        "POST", f"/api/sessions/{sid2}/answers/stream", json=BODY,
        headers={"Accept-Encoding": "gzip"},
    ) as resp:
        print(f"[gzip] content-encoding={resp.headers.get('content-encoding')!r}")
        stamps2 = []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                stamps2.append((round(time.monotonic() - t1, 2), json.loads(line[6:])["event"]))
        print(f"[gzip] {len(stamps2)} 事件，时刻 {stamps2}")
