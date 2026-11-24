"""数据源注册表：route_to_vendor 门面。

三层结构：① agent 侧 @tool 薄封装（函数体只有一行 route_to_vendor）；
② 本注册表：{方法名: {来源名: 具体函数}}，按 config 路由到具体实现；
③ vendor 实现：local_corpus（BM25F）/ dense_corpus（稠密）/ hybrid
（RRF 混合）/ candidate_corpus（简历/JD 段落）。

vendor 返回结构化命中列表（题库侧 list[BankQuestion]，候选侧
list[CandidateChunk]）；markdown 格式化与空命中哨兵在工具层——vendor
只管"找到了什么"，"怎么讲给模型听"是工具层的事。

纪律：空结果如实返回空列表，哨兵化发生在工具层；配置的 vendor 列表就是
全部链路，绝不静默补上用户没选的来源。
"""
from interviewer.dataflows.errors import CorpusParseError, SourceNotConfiguredError

# 方法名合同：prompts/issue_question.py 的 ISSUE_QUESTION_TOOLS_ADDENDUM 写的是
# "检索工具 search_corpus(query)"——改这个名字 = 改 prompt，两侧必须同批改
#（test_prompts 锁的就是这类跨文件合同）。放在 interface（注册表）层：
# 方法名的所有权在接口，工具实现只是引用方。
TOOL_NAME = "search_corpus"

# 第二检索面的方法名 = 工具名：bind_tools 的 name 与
# ISSUE_QUESTION_CANDIDATE_ADDENDUM 里的写法必须一致，合同同上一条。
CANDIDATE_TOOL_NAME = "search_candidate"

# {方法名: {来源名: 可调用}}
_VENDOR_METHODS: dict[str, dict[str, object]] = {}


def register_vendor(method: str, vendor: str, fn) -> None:
    """W2 接入新语料源时注册一行即可，agent 层零改动。"""
    _VENDOR_METHODS.setdefault(method, {})[vendor] = fn


def get_vendor_chain(method: str, config_value: str) -> list:
    """把 'local,chroma' 这样的配置拆成有序回退链。"""
    registry = _VENDOR_METHODS.get(method, {})
    chain = []
    for name in [v.strip() for v in config_value.split(",") if v.strip()]:
        if name not in registry:
            raise SourceNotConfiguredError(f"方法 {method} 未注册来源 {name}")
        chain.append(registry[name])
    return chain


def route_to_vendor(method: str, vendors_config: str, *args, **kwargs):
    """按配置的回退链逐家尝试；全链失败时抛首错（不静默吞）。
    空链显式报错——不搞 raise None→TypeError 的隐形炸法（六节微修缮，。"""
    chain = get_vendor_chain(method, vendors_config)
    if not chain:
        raise ValueError(f"vendor 链为空：方法 {method}（配置值 {vendors_config!r}）")
    first_error = None
    for fn in chain:
        try:
            return fn(*args, **kwargs)
        except CorpusParseError:
            # 数据损坏是库级事实（所有要 parse 的腿都会同样炸），立即短路
            # 重抛：既省掉注定失败的后腿，也防它被先发的依赖错遮蔽——
            # dense 首链 + fastembed 缺席的部署里，ImportError 曾顶掉
            # corrupt 信号让降级失效（2026-11-18 审查确认）
            raise
        except Exception as exc:  # noqa: BLE001 —— 显式失败可见，继续下一家
            if first_error is None:
                first_error = exc
    raise first_error
