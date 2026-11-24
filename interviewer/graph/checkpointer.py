"""Checkpointer 构建。实测要点：SqliteSaver.from_conn_string 是
@contextmanager（退出 with 块即关连接，不能裸赋值）——正确写法是自持
sqlite3.Connection + SqliteSaver(conn)；compile 必带 checkpointer，
无 checkpointer 时 interrupt 静默失败（不报错但无法恢复）。
"""
import sqlite3
from pathlib import Path


def build_checkpointer(config: dict):
    """返回 (saver, close_fn)。backend=memory 仅供测试（InMemorySaver 官方名，
    MemorySaver 只是向后兼容别名）。"""
    if config.get("checkpoint_backend") == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver(), (lambda: None)

    from langgraph.checkpoint.sqlite import SqliteSaver

    path = Path(config["checkpoint_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn), conn.close
