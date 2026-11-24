"""pytest 全局夹具。纪律：unit/integration 标记物理隔离（默认回归全部离线，
mock LLM 不花钱不挂网）；get_config() 每次返回深拷贝，测试内改动不污染全局。
"""
import sys
from pathlib import Path

# 保证任意工作目录下 `pytest tests` 都能导入 interviewer 包（无需先 pip install）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
