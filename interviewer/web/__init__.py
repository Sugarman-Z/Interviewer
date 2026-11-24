"""api-service：HTTP 传输层，与 cli/ 平级的第二个 transport（包住 LangGraph
面试引擎，给 webapp 提供真实后端）。依赖方向 routes → gateway → 引擎公共面，
图与 agents 零反向感知。本 __init__ 不 import 任何子模块——未装 [api] 可选组
的环境里包本身仍可导入，fastapi 依赖只从 app/routes/__main__ 子模块开始。
"""
