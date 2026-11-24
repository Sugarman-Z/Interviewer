"""会话驱动：喂应答，把图从当前进度驱动到下一挂起或终局。

合同四条：run_resumed_session 签名里没有 initial_state——续面只可能发
Command(resume)，重传初始 state 会把已考过的题塞回队列（想犯都犯不了）；
on_interrupt 是唯一交互 seam、on_token 是只读观察 seam（不碰 state/
checkpoint，Exception 级异常必吞——吐字失败绝不炸回合；控制流异常属
BaseException 正确穿透）；DriverResult.finished=False 是用户主动退出
（可再续）、True 是图跑完（caller 落盘+报告），不用 None 承担退出语义；
on_interrupt 返回的 dict 原样进 Command(resume=...)，answer/skip 的 type
语义归 agent_states，graph 层不 import agents。
"""
import logging
from typing import Callable, NamedTuple

from langgraph.types import Command

logger = logging.getLogger(__name__)

InterruptCallback = Callable[[dict], "dict | None"]

#: 只读观察 seam：(AIMessageChunk, metadata)。
#: driver 传**顶层浅拷贝**（chunk.model_copy() + dict(metadata)）——顶层属性
#: 赋值与 metadata 键被隔离（langgraph 对同 run 的 chunk 复用同一 metadata
#: dict，dict() 拷贝有实防护）；嵌套结构（tool_call_chunks/additional_kwargs）
#: 仍共享引用，当前不被回调污染靠的是**同步双分量流的排水时序**（节点任务
#: 完成后才吐 messages 事件，回调拿到的是已消费工件——langgraph pregel
#: 实测）。Ⓐ 日后迁 astream / 换流式路由 / langgraph 改排水时机时，此隔离
#: 需重审（加深拷贝或改设计）；拷贝开销为每 chunk 一次浅复制，量级可忽略
TokenCallback = Callable[[object, dict], None]


class DriverResult(NamedTuple):
    """驱动结果：final_values 只在 finished=True 时有值（get_state 快照）。"""

    final_values: dict | None
    finished: bool


class NoPendingInterrupt(RuntimeError):
    """续面入口发现会话没有挂起的问题（可能刚被其他进程续完）。

    fail-loud：怎么报告由 caller 决定（CLI 打 stderr + 退出码 1），
    driver 不做 I/O。
    """


def pending_interrupt(graph, cfg) -> dict | None:
    """读 checkpointer 重建的挂起中断载荷；无挂起返回 None。

    此前这段 snap.tasks 遍历在 CLI 续面分支里手写一份——收进来供
    run_resumed_session 与测试共用（"挂起在哪"的知识单点持有）。
    """
    snap = graph.get_state(cfg)
    for task in snap.tasks or []:
        interrupts = getattr(task, "interrupts", ()) or ()
        if interrupts:
            return interrupts[0].value
    return None


def run_new_session(graph, cfg, initial_state: dict,
                    on_interrupt: InterruptCallback,
                    on_token: TokenCallback | None = None) -> DriverResult:
    """新会话：初始 state 进图，此后每轮只发 Command(resume)。"""
    return _drive(graph, cfg, initial_state, on_interrupt, on_token)


def run_resumed_session(graph, cfg,
                        on_interrupt: InterruptCallback,
                        on_token: TokenCallback | None = None) -> DriverResult:
    """续面：从 checkpointer 挂起处继续——只发 Command(resume)，绝不重传
    初始 state（签名里没有这个参数，见模块头）。无挂起抛 NoPendingInterrupt。
    """
    pending = pending_interrupt(graph, cfg)
    if pending is None:
        raise NoPendingInterrupt("会话没有挂起的问题（可能刚被其他进程续完）")
    answer = on_interrupt(pending)
    if answer is None:
        return DriverResult(None, False)
    return _drive(graph, cfg, Command(resume=answer), on_interrupt, on_token)


def _drive(graph, cfg, payload, on_interrupt: InterruptCallback,
           on_token: TokenCallback | None = None) -> DriverResult:
    """驱动到图跑完或用户退出（双入口的公共循环）。

    default stream 下 __interrupt__ 事件是 Interrupt 对象的元组，取
    [0].value 得载荷（invoke 返回值里是 list，不同形）。on_token 为 None →
    默认 stream 模式；非 None → stream_mode=["updates","messages"] 双分量
    分流：updates 喂中断检测，messages 喂回调（try/except 兜吞）。
    """
    while True:
        interrupt_value = None
        if on_token is None:
            for chunk in graph.stream(payload, cfg):
                if isinstance(chunk, dict) and "__interrupt__" in chunk:
                    interrupt_value = chunk["__interrupt__"][0].value
        else:
            for mode, data in graph.stream(
                payload, cfg, stream_mode=["updates", "messages"]
            ):
                if mode == "messages":
                    chunk, metadata = data
                    try:
                        on_token(chunk.model_copy(), dict(metadata))
                    except Exception:  # noqa: BLE001 —— 只读观察口的失败面：吞
                        logger.warning(
                            "on_token 回调抛异常，已吞（回合继续）", exc_info=True
                        )
                elif isinstance(data, dict) and "__interrupt__" in data:
                    interrupt_value = data["__interrupt__"][0].value
        if interrupt_value is None:
            break                                    # 图跑完（该段无挂起）
        answer = on_interrupt(interrupt_value)
        if answer is None:
            return DriverResult(None, False)         # 用户退出：进度已在 checkpointer
        payload = Command(resume=answer)
    return DriverResult(graph.get_state(cfg).values, True)
