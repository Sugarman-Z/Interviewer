"""题库检索工具层：把检索函数包装成 LLM 可调用的工具。
三层结构（见 dataflows/interface.py）：① agent 侧工具薄封装（本文件）；
② vendor 注册表/回退链；③ vendor 实现（local/dense/hybrid）。
RAG 闭环的"扳机"：模型经 as_tool() 发起检索，命中以 markdown 进上下文。
"""
import logging
import time

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.errors import CorpusParseError
from interviewer.dataflows.interface import TOOL_NAME, route_to_vendor
from interviewer.dataflows import relevance_gate   # 远域整单闸（模块顶层零重依赖）

logger = logging.getLogger(__name__)

# 空命中三态哨兵
# 合同对端：prompts/issue_question.py 的 TOOLS_ADDENDUM 按两个哨兵的行为
# 写作——改这里必须同步那边（test_bank_tool 锁跨文件锚）。
# 被查重排除（命中的全是已考考点）→ 换考点；真没有/表述不匹配 → 对照
# 考点清单：有相关考点 = 表述不匹配可改写重试，没有 = 题库真没有按材料生成。
NO_RESULTS_AVAILABLE = "NO_RESULTS_AVAILABLE：题库无此考点，不得凭空编造命中，请依据简历/JD 材料生成题目"
NO_RESULTS_EXCLUDED = (
    "NO_RESULTS_EXCLUDED：检索词命中的都是已考考点（查重排除），"
    "请改考其他考点或依据简历/JD 材料生成题目"
)

# 题库损坏哨兵：任何时点题库炸都不阻塞流程——降级成 LLM 按材料出题。
# 与空命中两哨兵不同类：不是"题库没有"而是"题库不可读"，重试与改写都无效，
# 模型唯一正确动作是弃用题库直接生成（prompt 第 3 条三哨兵纪律同步）。
# 可见性：留痕 record 带 error="corpus_parse_failed"（session JSON 全量可查），
# 进程内每目录警告一次（_warn_corrupt_once）。修复题库后下次检索自动恢复
#（BankIndex 坏库不缓存语义——每次检索重解析，修好即好）。
BANK_CORRUPTED = (
    "BANK_CORRUPTED：题库文件解析失败（题库损坏），本次检索无结果且改写重试无效。"
    "不得引用任何题库编号；请直接依据简历/JD 材料生成题目。"
)

_CORRUPT_WARNED: set[str] = set()


def _warn_corrupt_once(corpus_dir: str, exc: CorpusParseError) -> None:
    """每个进程每个目录只警告一次：坏库会话里每次检索都失败，逐条刷屏反而
    淹没信号；全量失败明细在每条留痕 record 里（session JSON 可查）。"""
    key = str(corpus_dir)
    if key not in _CORRUPT_WARNED:
        _CORRUPT_WARNED.add(key)
        logger.warning(
            "题库解析失败，检索降级为 BANK_CORRUPTED 哨兵（%s）"
            "——修复前所有题目将走简历/JD 材料生成", exc,
        )


class BankSearchTool:
    """发题官的检索扳机：一副身体、两副面孔。
    as_tool() → langchain 工具（模型只见 query 一个参数；exclude_topics 是
    闭包里的签发查重硬约束，模型不可见）；run() → 节点直调，返回
    (markdown, entries)：markdown 喂模型，entries 喂节点回填注册表
（bank_qid 对账防幻觉的依据）。

    空命中契约：run 返回 (哨兵, [])，绝不返回 ("", [])——空串是"没说话"，
    哨兵是"查过了，没有"。EXCLUDED 态的二次探测命中不进 entries：模型
    无从合法引用已考考点编号，报了即编造，归因走 excluded_hit_ids。
    """

    def __init__(self, config: dict):
        """从 config 持有检索链所需的几个键。必备用 [] 直取（拼错当场炸，
        fail-loud）；开关用 .get、缺席即关（老配置不带新键不炸）。开关开启
        需题向量（dense/hybrid 链），local 链探测不到自动降级。"""
        # [] 直取（缺键当场 KeyError）——与 issue_question 读 spec 同款 fail-loud：
        # 配置拼错在首次检索就炸，而不是事后 None.split() 的莫名 AttributeError
        self.corpus_vendors = config["corpus_vendors"]
        self.corpus_dir = config["corpus_dir"]
        self.bank_top_k = config["bank_top_k"]
        # 组卷层后处理：开启需题向量（dense/hybrid 链），
        # local 链探测不到向量方法自动降级原样返回；Phase D 复考要"同考点
        # 相似"时关 bank_mmr（TODO ④ 点名的反例场景，开关存在的理由）
        self.bank_mmr = bool(config.get("bank_mmr", False))
        self.bank_topic_dedup = bool(config.get("bank_topic_dedup", False))
        # 远域整单闸（默认配置开）：BM25 无阈值
        # 永远有 top-k，远域查询拿垃圾命中——闸读查询对全库的最高语义分，
        # 低于阈值整批转 AVAILABLE 哨兵。fastembed 缺席自动 fail-open 关闸
        #（增强件缺席不炸检索本体）。校准与边界见 relevance_gate 模块头。
        self.bank_far_gate = bool(config.get("bank_far_gate", False))

    def as_tool(self):
        """→ langchain StructuredTool。契约：name=TOOL_NAME；模型可见参数
        只有 query: str（exclude_topics 走 run()，不进工具面——签名里多任何
        参数都破坏硬约束设计）；工具执行 = self.run(query)[0]，entries
        那一半不进模型视野。"""
        from langchain_core.tools import StructuredTool  # 惰性导入（第 4 次用同一手法）

        def _search(query: str) -> str:
            # 闭包捕获 self：langchain 只认识这个函数，永远不知道 run/entries 的存在
            return self.run(query)[0]

        return StructuredTool.from_function(
            func=_search,
            name=TOOL_NAME,
            description=(
                "在本地题库中检索现成面试题。query 用考点关键词"
                "（如 'Redis 持久化'），不是完整问句。"
            ),
        )

    def _search(self, query: str, exclude_topics: tuple = (), top_k: int | None = None):
        """单次 vendor 检索（三态探测的原子动作）。top_k 缺省 = bank_top_k；
        组卷层后处理开启时 run_with_probe 传池深（见 _fetch_k）。"""
        return route_to_vendor(
            TOOL_NAME,
            self.corpus_vendors,
            query,
            exclude_topics=exclude_topics,
            top_k=self.bank_top_k if top_k is None else top_k,
            corpus_dir=self.corpus_dir,
        )

    def _topics_line(self, exclude_topics: tuple = ()) -> str:
        """未考考点清单行（三态哨兵的附件，单行顿号连接）。逐 vendor 尝试
        corpus_topics 第二方法而非整链路由（任一 vendor 未注册该方法整链即
        抛，会连坐能答的 vendor）。全败 → 空串（增强件缺席降级，不炸检索）。"""
        for name in (v.strip() for v in self.corpus_vendors.split(",")):
            if not name:
                continue
            try:
                topics = route_to_vendor("corpus_topics", name, corpus_dir=self.corpus_dir)
            except Exception:  # noqa: BLE001 —— 清单缺席可降级；检索本体不在这条路径上
                logger.debug("corpus_topics 路由失败（vendor=%s），清单降级为空", name)
                continue
            remaining = [t for t in topics if t not in set(exclude_topics)]
            if not remaining:
                return ""
            return "题库现有考点：" + "、".join(remaining) + "（有相关考点即可改写检索词再试一次）"
        return ""

    # 组卷层后处理

    def _fetch_k(self) -> int:
        """取题深度：组卷层任一开关开启 → 池深（MMR_POOL_N 与 bank_top_k 取大
        ——池浅于 top-k 的过滤/重排没有补位可言）；全关 → bank_top_k。
        依赖缺席（numpy 没装）同样回落 bank_top_k：降级而非炸。"""
        if not (self.bank_mmr or self.bank_topic_dedup):
            return self.bank_top_k
        try:
            from interviewer.dataflows.mmr import MMR_POOL_N
            return max(MMR_POOL_N, self.bank_top_k)
        except ImportError:
            logger.warning("组卷开关开启但 mmr 模块不可用（numpy 缺席？），取题深度回落 bank_top_k")
            return self.bank_top_k

    def _dedup_hits(self, hits, exclude_topics):
        """查重 v1.5：换皮题剔除，委托 dedup.filter_near_duplicates
（题向量逐 vendor 探测，全链缺席自动降级原样返回）。"""
        try:
            from interviewer.dataflows.dedup import filter_near_duplicates
            return filter_near_duplicates(
                hits, exclude_topics,
                corpus_dir=self.corpus_dir, vendors=self.corpus_vendors,
            )
        except ImportError:
            logger.warning("bank_topic_dedup 开启但 dedup 模块不可用（numpy 缺席？），回落精确匹配")
            return hits

    def _mmr_hits(self, query, hits):
        """组卷多样性：池内贪心 MMR 选 top-k，委托
        mmr.diversify_hits（向量缺席截断原序，恰好交回 top_k 条）。"""
        try:
            from interviewer.dataflows.mmr import diversify_hits
            return diversify_hits(
                query, hits,
                corpus_dir=self.corpus_dir, vendors=self.corpus_vendors,
                top_k=self.bank_top_k,
            )
        except ImportError:
            logger.warning("bank_mmr 开启但 mmr 模块不可用（numpy 缺席？），回落检索原序")
            return hits[: self.bank_top_k]

    def run_with_probe(self, query: str, exclude_topics: tuple = ()) -> tuple[str, list[BankQuestion], dict]:
        """执行检索 + 空命中三态探测 → (markdown, entries, 探测详情)。
        三态：首查（带查重）命中 → hit；首查空 + 二次探测（exclude=()）命中
        → EXCLUDED 哨兵（已考考点只进 excluded_hit_ids，不给模型）；首查空
        + 探测仍空 / 本就无查重 → AVAILABLE 哨兵 + 未考考点清单（"表述不
        匹配 vs 真没有"交给模型对照清单判断）。

        第四态 corrupt：题库解析失败 → BANK_CORRUPTED 哨兵。捕获点在这里
        是因为 run()/execute()/as_tool() 三条路径共用本函数，一个 try 覆盖
        全部入口；窄捕获只认 CorpusParseError。
        """
        try:
            return self._run_with_probe_inner(query, exclude_topics)
        except CorpusParseError as exc:
            _warn_corrupt_once(self.corpus_dir, exc)
            return (
                BANK_CORRUPTED, [],
                {"state": "corrupt", "excluded_hit_ids": [],
                 "detail": str(exc)[:200]},
            )

    def _run_with_probe_inner(self, query: str, exclude_topics: tuple = ()):
        """三态判定本体（corrupt 捕获由 run_with_probe 统一负责）。"""
        hits = self._search(query, exclude_topics, top_k=self._fetch_k())
        # 远域整单闸：只读查询对全库的最高语义分，低于阈值 = 远域 → 整批转
        # AVAILABLE 哨兵（留痕 state=far_gated）。命中清单本身不动（keyword
        # 命中的 dense 绝对分天然低，逐条过滤会砍 BM25 主场）。顺序在组卷层前
        if hits and self.bank_far_gate and relevance_gate.should_gate_empty(
                query, self.corpus_dir):
            extra = self._topics_line(exclude_topics)
            md = NO_RESULTS_AVAILABLE + (f"\n{extra}" if extra else "")
            return md, [], {"state": "far_gated", "excluded_hit_ids": []}
        # 组卷层后处理（默认全关）：查重 → MMR → 截回 top_k。两步都要池深
        # 于 top-k 才有补位可言（_fetch_k 已放大取题深度）
        if hits and self.bank_topic_dedup:
            hits = self._dedup_hits(hits, exclude_topics)
        if hits and self.bank_mmr:
            hits = self._mmr_hits(query, hits)
        hits = hits[: self.bank_top_k]
        if hits:
            md = "\n\n".join(_format_entry(q) for q in hits)
            return md, list(hits), {"state": "hit", "excluded_hit_ids": []}

        extra = self._topics_line(exclude_topics)
        if exclude_topics:
            rehits = self._search(query, ())
            if rehits:
                md = NO_RESULTS_EXCLUDED + (f"\n{extra}" if extra else "")
                return md, [], {"state": "excluded", "excluded_hit_ids": [q.id for q in rehits]}

        md = NO_RESULTS_AVAILABLE + (f"\n{extra}" if extra else "")
        return md, [], {"state": "none", "excluded_hit_ids": []}

    def run(self, query: str, exclude_topics: tuple = ()) -> tuple[str, list[BankQuestion]]:
        """公共合同（不变）：执行检索 → (给模型的 markdown, 给节点账本的 entries)。
        三态详情走 run_with_probe——run 的二元组解包点是跨文件合同，不扩元。"""
        md, entries, _ = self.run_with_probe(query, exclude_topics)
        return md, entries

    def for_call(self, exclude_topics: tuple = ()) -> "BoundBankSearch":
        """把本次签发的查重闭包烙进单次调用适配器。exclude_topics 是逐次
        签发的 state 快照，而工具本体建图时一造全场复用——"每次调用新造
        适配器"就是闭包的自然粒度。复用旧适配器 = 拿上一轮的查重表查这轮。"""
        return BoundBankSearch(self, tuple(exclude_topics))


class BoundBankSearch:
    """bank 检索的循环期适配器：两副面孔（as_tool/execute，LoopTool seam，
    本类不 import structured.py——结构对上即入环）+ 银行自己的账。
    registry 是防幻觉对账账本（累积型），节点出环后自取，_stamp_bank 拿它
    核对 bank_qid——BankQuestion 类型不穿过循环。"""

    def __init__(self, inner: BankSearchTool, exclude_topics: tuple = ()):
        self._inner = inner
        self._exclude = tuple(exclude_topics)   # 查重硬约束闭包：模型不可见
        self.registry: dict = {}                # 防幻觉对账账本（盖帽依据）

    def as_tool(self):
        """模型面委托本体：名字/签名/描述由 BankSearchTool 单点持有。"""
        return self._inner.as_tool()

    def execute(self, args: dict) -> tuple[str, dict | None]:
        """执行一次检索：回喂 markdown + 留痕一条。空 query 放行（自然得到
        哨兵，模型看到哨兵会自己修正）；elapsed_ms 量探测全程（含二次探测）。"""
        query = str(args.get("query", "")).strip()
        start = time.perf_counter()
        md, entries, probe = self._inner.run_with_probe(query, self._exclude)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        self.registry.update({e.id: e for e in entries})
        record = {
            "query": query,
            "hit_ids": [e.id for e in entries],
            "elapsed_ms": elapsed_ms,
            "empty_hit": not entries,
        }
        if not entries:
            # 三态细化：excluded=被查重排除（excluded_hit_ids 是归因证据，
            # 不进 hit_ids——hit_ids 语义是"喂给模型的命中"）
            record["empty_state"] = probe["state"]
            record["excluded_hit_ids"] = probe["excluded_hit_ids"]
            if probe["state"] == "corrupt":
                # 工具自身失败（≠ 题库缺题）：mine 聚合按 error 键过滤
                record["error"] = "corpus_parse_failed"
                record["detail"] = probe.get("detail", "")
        return md, record


def _format_entry(q: BankQuestion) -> str:
    """单题 → markdown 块（模型逐字消费的"检索结果"）。格式契约（首行即
    id——模型回填 bank_qid 全靠它，藏深处 = 降低遵约率）：
        [bank-kafka-001] 考点：Kafka 高可用｜难度 3｜tags: kafka, 消息队列
        题目：……　追问树：……｜出处：……

    评分锚点刻意不出现在这张脸上（最小知识）：锚点是答案钥匙，题面允许
    模型改编，喂锚点 = 邀请抄进题面；评分侧零消费方（Evaluator 只读
    followup_hints）。追问树单行"｜"连接（保持一题一块）；空字段整行省略。
    """
    head = f"[{q.id}] 考点：{q.topic}｜难度 {q.difficulty}"
    if q.tags:
        head += f"｜tags: {', '.join(q.tags)}"
    lines = [head, f"题目：{q.text}"]
    if q.followup_tree:
        lines.append("追问树：" + "｜".join(q.followup_tree))
    if q.source:
        lines.append(f"出处：{q.source}")
    return "\n".join(lines)
