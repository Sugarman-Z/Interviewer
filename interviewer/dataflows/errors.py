"""异常按"路由器要做什么不同的事"归类，不按可描述的原因堆类。

NoCorpusHitError → 出题降级（NO_RESULTS_AVAILABLE 哨兵）；CorpusParseError
→ BANK_CORRUPTED 哨兵降级，修库后自愈（坏库不缓存）；SourceRateLimitError
→ 退避重试；SourceNotConfiguredError → 跳过并给可操作的提示。
"""
class CorpusError(Exception):
    """语料/外部源错误基类。"""


class CorpusParseError(CorpusError, ValueError):
    """题库解析失败（坏块/重复 id 等数据损坏）。

    bank_tool 窄捕获本类型降级为 BANK_CORRUPTED 哨兵，不阻塞面试流程；
    只认这个类型，其它 ValueError 照炸。兼继承 ValueError 与既有
    pytest.raises(ValueError) 兼容。
    """


class NoCorpusHitError(CorpusError):
    """检索无命中。调用方应返回 NO_RESULTS_AVAILABLE 哨兵并降级，不得编造内容。"""


class SourceRateLimitError(CorpusError):
    """外部源限流（智谱 1302/1305 等）。客户端层负责退避重试。"""


class SourceNotConfiguredError(CorpusError, ValueError):
    """缺 API key / 未配置。兼继承 ValueError，方便在入口被当作参数问题提示。"""
