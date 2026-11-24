"""探针：真智谱端点的流式分片行为。
fixture 录制发现 tool args 整块齐发——逐字效果是否可行取决于端点「到底流
不流」。三问：① 纯文本（无 tools）content 是否逐 token 分片？② bind_tools
（交卷 schema 同绑）tool args 是否分片？③ 各 chunk 的 additional_kwargs
（reasoning_content 落在哪、是否分片）？

跑法同 record_stream_chunks.py（.env + NO_PROXY）。真额度，单次几秒。
"""
import os
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO / ".env")
if "open.bigmodel.cn" not in os.environ.get("NO_PROXY", ""):
    os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",open.bigmodel.cn").lstrip(",")

from pydantic import BaseModel, Field  # noqa: E402

from interviewer.llm import create_llm_client  # noqa: E402


class Issued(BaseModel):
    text: str = Field(description="题面")
    topic: str = Field(default="", description="考点")


def _describe(chunk) -> str:
    ak = chunk.additional_kwargs or {}
    return (
        f"content[{len(chunk.content or '')}] "
        f"tcc[{len(chunk.tool_call_chunks or [])}] "
        f"ak{{{', '.join(f'{k}:{len(str(v))}' for k, v in ak.items())}}}"
    )


def _run(label, call):
    t0 = time.monotonic()
    print(f"\n== {label} ==")
    n = 0
    for chunk in call():
        n += 1
        print(f"  chunk{n} +{time.monotonic() - t0:5.2f}s {_describe(chunk)}")
    print(f"  共 {n} chunk，{time.monotonic() - t0:.2f}s")


def main() -> int:
    from interviewer.default_config import get_config

    cfg = get_config()
    assert cfg["llm_provider"] == "zhipu"
    # SPike 用标准端点对照（默认走 .env 的 coding 专用端点）：coding 端点
    # tool args 原子齐发，标准端点是否分片决定「逐字」还有没有真流路径
    if os.environ.get("SPIKE_STD_ENDPOINT") == "1":
        cfg["llm_base_url"] = "https://open.bigmodel.cn/api/paas/v4/"
        print("[probe] 覆盖为标准端点 open.bigmodel.cn/api/paas/v4/")
    llm = create_llm_client("quick", cfg)

    _run("① 纯文本流", lambda: llm.stream("用三句话介绍 ISR 机制。"))
    _run(
        "② bind_tools 流（交卷 schema）",
        lambda: llm.bind_tools([Issued]).stream(
            "出一道考 Kafka ISR 的面试题，参数 text 为题面、topic 为考点。"
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
