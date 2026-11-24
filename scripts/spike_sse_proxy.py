"""dev 代理对 SSE 的行为探针（临时服务，不进产品代码）。
两项验证：① 逐块透传——interval 流（每 interval_ms 一条 data）经 Next
rewrite 代理是否按块到达（curl -N --trace-time 看每行时间戳），还是被缓冲
到整响应；② proxyTimeout 空闲语义——临时调小 proxyTimeout 后，持续有字节
的流是否存活超过 timeout、静默流是否恰在 timeout 处被切。

跑法：python scripts/spike_sse_proxy.py（占 127.0.0.1:8000——先停真后端）。
端点：GET /api/spike/sse?interval_ms=1000&total_s=8 | ?silent_s=8
"""
import asyncio
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.get("/api/spike/sse")
async def sse(interval_ms: int = 1000, total_s: float = 8.0, silent_s: float = 0.0):
    async def gen():
        start = time.monotonic()
        if silent_s > 0:
            # 静默流：先憋住不写任何字节，再一次性收尾——验 proxyTimeout 空闲掐断
            await asyncio.sleep(silent_s)
            yield f'data: {{"after": {silent_s}}}\n\n'
            return
        i = 0
        while time.monotonic() - start < total_s:
            i += 1
            yield f"data: {{\"i\": {i}, \"t\": {round(time.monotonic() - start, 2)}}}\n\n"
            await asyncio.sleep(interval_ms / 1000)
        yield f"data: {{\"done\": true, \"n\": {i}}}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
