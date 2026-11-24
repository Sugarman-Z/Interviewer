"""dataflows：数据源注册表与语料 vendor。

resolve_search_tool 是组装入口（建图时调用一次）：config 回退链解析成
BankSearchTool，链为空串 → None（工具路径整体关闭的存在性开关）。
resolve_candidate_tool 是第二检索面入口：开启时返回工厂而非工具本体——
简历/JD 全文是运行期 state，节点每次签发拿 state 文本现造适配器。
"""
from interviewer.dataflows.errors import SourceNotConfiguredError
from interviewer.dataflows.interface import (
    CANDIDATE_TOOL_NAME,
    TOOL_NAME,
    get_vendor_chain,
)


def resolve_candidate_tool(config: dict):
    """config → 段落检索工具工厂；candidate_search_enabled 缺席/False → None。
    与 resolve_search_tool 的差异：返回工厂不返回本体（candidate 的"语料"是
    会话期 state）；校验时机相同（链名拼错建图时就炸，错误左移）。"""
    if not config.get("candidate_search_enabled", False):
        return None
    chain = (config["candidate_vendors"]).strip()   # 开启即必需键：[] 语义直取
    names = [v.strip() for v in chain.split(",") if v.strip()]
    if not names:
        return None

    if "local" in names:
        import interviewer.dataflows.candidate_corpus  # noqa: F401 —— import 即注册

    get_vendor_chain(CANDIDATE_TOOL_NAME, chain)  # 校验：未注册名在此抛错

    from interviewer.dataflows.candidate_tool import CandidateSearchTool

    def _factory(texts: dict):
        return CandidateSearchTool(config, texts).for_call()

    return _factory


def resolve_search_tool(config: dict):
    """配置的回退链 → BankSearchTool；corpus_vendors 为空串 → None。

    注册靠 import 副作用：各 vendor 模块底部有 register_vendor(...)——不
    import 该模块注册表里就没有它（踩过的坑：import 了 bank_tool 没 import
    local_corpus → "未注册来源 local"）。按链名显式 import：local 零依赖
    即时导入；dense（fastembed 重依赖）只在链里真有它才导入；hybrid 导入
    连带注册 dense 腿。解析时即调 get_vendor_chain 校验——链名拼错建图
    当场炸，不留到面试中段（错误左移）。
    """
    chain = (config.get("corpus_vendors") or "").strip()
    if not chain:
        return None
    names = [v.strip() for v in chain.split(",") if v.strip()]

    if "local" in names:
        import interviewer.dataflows.local_corpus  # noqa: F401 —— import 即注册
    if "dense" in names or "hybrid" in names:
        import interviewer.dataflows.dense_corpus  # noqa: F401 —— dense 注册 + hybrid 的腿
    if "hybrid" in names:
        import interviewer.dataflows.hybrid  # noqa: F401 —— import 即注册

    get_vendor_chain(TOOL_NAME, chain)  # 校验：未注册名在此抛 SourceNotConfiguredError

    from interviewer.dataflows.bank_tool import BankSearchTool

    return BankSearchTool(config)
