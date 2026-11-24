"""limbo 态手工构造器。
limbo = next 非空且无挂起中断（崩在超步之间）——mock provider 正常跑不出
此态（引擎吞 LLM 失败），只能手工构造：真跑到挂起后深拷贝最新 checkpoint
——换新 id（中断记录挂在 checkpoint 的写集上，新 id 天然无中断）并挖空
一个已执行节点的 versions_seen（使其显待执行 → next 非空），saver.put
落为新最新。

私有契约（langgraph 升级时红在这里属预期）：checkpoint dict 形状（v4：
versions_seen 键语义等）；saver.put 四参签名（config 需带 checkpoint_ns）；
checkpoint id 须合法 UUID 且 uuid6 进程内单调。
（langgraph.checkpoint.base.id.uuid6）——新 id 排序在后成为「最新」
- saver.list 按 checkpoint_id 降序 → 首个即最新
重传初始 state 仅对刚建会话安全；复用到已推进会话须改为取既有最新
checkpoint。图操作统一经 app.state.engine_executor（与生产同纪律）。
"""
import copy

from langgraph.checkpoint.base import uuid6 as langgraph_uuid6

from interviewer.graph.session_driver import (
    pending_interrupt,
    run_new_session,
)
from interviewer.graph.session_runner import create_initial_state
from interviewer.web.gateway import classify_session


def make_limbo(app, sid: str, position: str = "后端开发") -> None:
    """在 app 的引擎里给 sid 造一个 limbo thread（原地替换其挂起态）。"""
    def _sync(coach, graph, saver):
        cfg = coach.invoke_config(sid)
        run_new_session(
            graph, cfg, create_initial_state(sid, position, "简历", ""), lambda v: None
        )
        tup = next(saver.list(cfg))
        cp = copy.deepcopy(tup.checkpoint)
        node = next(iter(cp["versions_seen"]), None)
        cp["versions_seen"].pop(node, None)
        cp["id"] = str(langgraph_uuid6())
        put_cfg = {
            "configurable": {
                "thread_id": cfg["configurable"]["thread_id"],
                "checkpoint_ns": tup.config["configurable"].get("checkpoint_ns", ""),
            }
        }
        saver.put(
            put_cfg,
            cp,
            {"source": "loop", "step": ((tup.metadata or {}).get("step") or 0) + 1,
             "writes": None},
            {},
        )
        snap = graph.get_state(cfg)
        status = classify_session(snap, pending_interrupt(graph, cfg))
        assert status == "limbo", f"limbo 构造失败：classify={status} next={snap.next}"

    app.state.engine_executor.submit(
        lambda: _sync(app.state.coach, app.state.graph, app.state.saver)
    ).result(timeout=30)
