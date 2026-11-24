"""全局配置：默认值字典 + 环境变量覆盖表 + 按默认值类型强转。

新键只需在 DEFAULT_CONFIG 加一行（需环境覆盖再加 _ENV_OVERRIDES 一行）；
环境变量按默认值类型强转，拼错启动即抛；get_config() 返回深拷贝。
"""
import copy
import os

from interviewer.resume.file_loader import DEFAULT_MAX_CHARS

DEFAULT_CONFIG = {
    # LLM：quick/deep 双档槽位（deep 留给以后的强模型升级位）
    "llm_provider": "zhipu",              # zhipu | mock（mock 离线开发/测试用）
    "quick_llm": "glm-5.3-flash",
    "deep_llm": "glm-5.3-flash",
    "llm_base_url": "https://open.bigmodel.cn/api/paas/v4/",
    "api_key_env": "ZHIPU_API_KEY",       # 从哪个环境变量读 key
    "llm_timeout": 300,
    "llm_max_retries": 3,
    # 结构化输出：json_schema 经智谱兼容端点全炸（Invalid JSON），必须用 function_calling
    "structured_output_method": "function_calling",
    # GLM-5.3 始终思考不可关；thinking.effort 在工具路径被无视，reasoning_effort
    # 才生效（评分调用 78s→13s）。可选 low|high|max，空串=不透传
    "llm_reasoning_effort": "low",
    # 图形状：本区全部参与 thread_id 签名，改动后旧 checkpoint 自动作废（test_coach_graph 锁定）
    "graph_version": "w4-e1",            # Evaluator 吞并 FollowupAsker（action 路由/追问合并）后的图形状
    "modules": ["project"],               # 当前单模块；注册表支持多模块
    "max_followups": 3,                   # 追问熔断线：本题已答轮数上限（含主问轮）。常规追问归模型 action，此线仅极端兜底
    "max_total_turns": 24,                # 全场轮数保险丝
    "max_questions": 10,                  # 题数上限（与轮数保险丝分工：按题/按轮）
    "max_consecutive_llm_failures": 2,    # 连续 LLM 失败熔断（IssueQuestion+Evaluator 共计数，任一成功清零）
    # 执行预算：invoke_config 逐次透传，不进图签名（改它不作废进行中的会话）
    "max_recur_limit": 100,               # langgraph 默认 25，检索工具循环会撞，显式放大
    # 简历入口
    "resume_max_chars": DEFAULT_MAX_CHARS,  # 超长截断上限（file_loader 守卫，防大 PDF 撑爆上下文）
    # 存储（本地优先，有意不上数据库）
    "data_dir": "./data",
    "checkpoint_backend": "sqlite",       # sqlite | memory（memory 仅供测试）
    "checkpoint_path": "./data/checkpoints.sqlite",
    # 错题本/遗忘曲线
    "mistake_book_path": "./data/mastery/mistakes.md",
    "review_intervals_days": [1, 3, 7, 14],
    "corpus_vendors": "local",
    "corpus_dir": "./corpus",
    "bank_top_k": 5,
    "max_tool_rounds": 3,
    "max_tool_calls": 5,             # 工具调用总数硬顶，独立于轮数（GLM 单轮可并行多个 tool_calls）；循环侧 None/≤0 = 不限
    # 组卷层后处理，默认关。需题向量：dense/hybrid 链生效，
    # local 链探测不到向量方法自动降级原样返回
    "bank_mmr": False,               # 组卷多样性：检索池 top-20 → 贪心 MMR(λ=0.7) 选 top-5；复考模式要同考点相似题，届时关掉
    "bank_topic_dedup": False,       # 命中题与已考题整题向量 cosine≥0.90 视为换皮剔除（校准见 dedup.py 头注释）
    # 远域整单闸：local 链 BM25 无阈值，远域查询拿到垃圾命中。闸读查询对全库最高
    # 语义分，低于 0.38 整批转 AVAILABLE 哨兵；校准见 relevance_gate.py。
    # fastembed 缺席自动关闸（fail-open）
    "bank_far_gate": True,
    # 第二检索面：简历/JD 段落检索。prompt 摘录只 1500 字（file_loader 收 8000 字），
    # 检索面给模型按需查全文的能力
    "candidate_search_enabled": True,    # 真会话实证模型会自主选检索面（简历走 search_candidate、
                                         # 题库走 search_corpus）；关闭用环境变量 INTERVIEWER_CANDIDATE_SEARCH_ENABLED=false
    "candidate_vendors": "local",        # 段落检索回退链（当前仅 local）
    "candidate_top_k": 3,                # 段落命中条数（段落比题块长，3 条控上下文）
}

# 环境变量 → 配置键映射表（新键加一行即可）
_ENV_OVERRIDES = {
    "INTERVIEWER_LLM_PROVIDER": "llm_provider",
    "INTERVIEWER_QUICK_LLM": "quick_llm",
    "INTERVIEWER_DEEP_LLM": "deep_llm",
    "INTERVIEWER_LLM_BASE_URL": "llm_base_url",
    "INTERVIEWER_API_KEY_ENV": "api_key_env",
    "INTERVIEWER_STRUCTURED_METHOD": "structured_output_method",
    "INTERVIEWER_LLM_REASONING_EFFORT": "llm_reasoning_effort",
    "INTERVIEWER_MAX_FOLLOWUPS": "max_followups",
    "INTERVIEWER_MAX_TOTAL_TURNS": "max_total_turns",
    "INTERVIEWER_MAX_QUESTIONS": "max_questions",
    "INTERVIEWER_RESUME_MAX_CHARS": "resume_max_chars",
    "INTERVIEWER_MODULES": "modules",
    "INTERVIEWER_DATA_DIR": "data_dir",
    "INTERVIEWER_CHECKPOINT_BACKEND": "checkpoint_backend",
    "INTERVIEWER_CORPUS_VENDORS": "corpus_vendors",
    "INTERVIEWER_CORPUS_DIR": "corpus_dir",
    "INTERVIEWER_BANK_TOP_K": "bank_top_k",
    "INTERVIEWER_MAX_TOOL_ROUNDS": "max_tool_rounds",
    "INTERVIEWER_MAX_TOOL_CALLS": "max_tool_calls",
    "INTERVIEWER_BANK_MMR": "bank_mmr",
    "INTERVIEWER_BANK_TOPIC_DEDUP": "bank_topic_dedup",
    "INTERVIEWER_BANK_FAR_GATE": "bank_far_gate",
    "INTERVIEWER_CANDIDATE_SEARCH_ENABLED": "candidate_search_enabled",
    "INTERVIEWER_CANDIDATE_VENDORS": "candidate_vendors",
    "INTERVIEWER_CANDIDATE_TOP_K": "candidate_top_k",
}


def _coerce(raw: str, reference):
    """按默认值的 Python 类型强转环境变量值。"""
    if isinstance(reference, bool):
        v = raw.strip().lower()
        if v in ("1", "true", "on", "yes"):
            return True
        if v in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"非法布尔值: {raw!r}")
    if isinstance(reference, int):
        return int(raw)
    if isinstance(reference, float):
        return float(raw)
    if isinstance(reference, list):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return raw


def apply_env_overrides(config: dict) -> dict:
    for env_key, cfg_key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is not None:
            config[cfg_key] = _coerce(raw, config[cfg_key])
    return config


def get_config() -> dict:
    """返回应用了环境变量覆盖的配置深拷贝。"""
    return apply_env_overrides(copy.deepcopy(DEFAULT_CONFIG))
