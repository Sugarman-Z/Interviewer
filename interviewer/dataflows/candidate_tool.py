"""第二检索面工具层：把 candidate_corpus 的段落检索包装成 IssueQuestion
工具循环可绑定的工具。位置与 bank_tool 对称（agent 侧薄封装），形状照抄
BankSearchTool/BoundBankSearch 的分工——一副身体两副面孔 + 循环期适配器
自带账本。

与 bank 面的结构差异：语料是运行期 state。题库语料在磁盘上建图时一造
全场复用；简历/JD 全文每次会话不同——本类构造参数带 texts，由节点每次
签发时经工厂现造。防幻觉账本语义与 bank_qid 同构、严重度不同级：编造
bank 编号 = 谎报题源（熔断级）；编造 cand 编号 = 引用标注错误（题目可能
仍成立），处置是摘除编号，不进失败梯。
"""
import time

from interviewer.dataflows.candidate_corpus import CandidateChunk
from interviewer.dataflows.interface import CANDIDATE_TOOL_NAME, route_to_vendor

# 空命中哨兵：单态（段落检索没有"被查重排除"的第二态——exclude_ids 是
# v1 预留参数，当前无调用方）。合同对端：prompts/issue_question.py 的
# ISSUE_QUESTION_CANDIDATE_ADDENDUM 按本哨兵的行为写作（跨文件锚测试锁）。
CANDIDATE_NO_RESULTS = (
    "CANDIDATE_NO_RESULTS：简历/JD 原文无匹配段落，不得虚构引用——"
    "依据已有材料出题即可"
)


class CandidateSearchTool:
    """简历/JD 段落检索的身体：run 服务节点、as_tool 服务模型。
    as_tool() → 模型只见 query: str（texts/exclude_ids 是节点侧闭包）；
    run() → (markdown, chunks)：markdown 喂模型（段落原文 = 引用证据），
    chunks 喂节点（registry 对账的原料）。空命中返回 (哨兵, [])。
    """

    def __init__(self, config: dict, texts: dict | None):
        """[] 直取两键（fail-loud，配置拼错首次检索就炸）：candidate_vendors
（回退链串）、candidate_top_k。texts = {"resume": 全文, "jd": 全文}，
        由节点从 state 现取——本类不 import agents 层任何东西。"""
        self.candidate_vendors = config["candidate_vendors"]
        self.candidate_top_k = config["candidate_top_k"]
        self._texts = dict(texts or {})

    def as_tool(self):
        from langchain_core.tools import StructuredTool  # 惰性导入（同 bank 面）

        def _search(query: str) -> str:
            return self.run(query)[0]

        return StructuredTool.from_function(
            func=_search,
            name=CANDIDATE_TOOL_NAME,
            description=(
                "在候选人简历与岗位 JD 的原文段落中检索证据。query 用关键词"
                "（如 'Kafka'、'性能优化'），不是完整问句。命中段落带 "
                "[cand-XXXX] 编号，可在 source_ref 中引用。"
            ),
        )

    def run(self, query: str, exclude_ids: tuple = ()) -> tuple[str, list[CandidateChunk]]:
        hits = route_to_vendor(
            CANDIDATE_TOOL_NAME,
            self.candidate_vendors,
            query,
            exclude_ids=exclude_ids,
            top_k=self.candidate_top_k,
            texts=self._texts,
        )
        if hits:
            return "\n\n".join(_format_chunk(c) for c in hits), list(hits)
        return CANDIDATE_NO_RESULTS, []

    def for_call(self, exclude_ids: tuple = ()) -> "BoundCandidateSearch":
        """循环期适配器（LoopTool seam）。bank 面的 for_call 烙查重闭包；
        段落面 v1 无逐次闭包（同段落允许跨题复证），但保留同款入口——
        账本每轮新造的粒度与 bank 一致（跨签发复用 = 脏账）。"""
        return BoundCandidateSearch(self, tuple(exclude_ids))


class BoundCandidateSearch:
    """段落检索的循环期适配器：两副面孔（as_tool/execute）+ 引用账本。
    registry 累积本轮实际回喂模型的段落——节点出环自取，
    _sanitize_candidate_refs 拿它核对 source_ref 里的 cand 编号。"""

    def __init__(self, inner: CandidateSearchTool, exclude_ids: tuple = ()):
        self._inner = inner
        self._exclude = tuple(exclude_ids)
        self.registry: dict = {}

    def as_tool(self):
        return self._inner.as_tool()

    def execute(self, args: dict) -> tuple[str, dict | None]:
        """执行一次段落检索：回喂 markdown + 留痕一条（形状与 bank 面的
        execute 完全一致——retrieval_trace 里靠循环补的 tool 键区分两副
        工具，归因时不用猜）。空 query 放行（自然得哨兵，模型自行修正）。"""
        query = str(args.get("query", "")).strip()
        start = time.perf_counter()
        md, hits = self._inner.run(query, exclude_ids=self._exclude)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        self.registry.update({h.id: h for h in hits})
        record = {
            "query": query,
            "hit_ids": [h.id for h in hits],
            "elapsed_ms": elapsed_ms,
            "empty_hit": not hits,
        }
        return md, record


def _format_chunk(c: CandidateChunk) -> str:
    """单段落 → markdown 块（模型逐字消费的引用证据）。
    格式契约（id 首行方括号，模型回填 source_ref 全靠它）：
        [cand-0003] 简历第2段（字符 156-421）
        负责交易系统的性能优化……

    标注行两个核验锚：段落序号（人读）与字符区间（机器可核验——
    start:end 切原文必得段落正文，区间合同测试锁死）。
    """
    label = "简历" if c.source == "resume" else "JD"
    return f"[{c.id}] {label}第{c.para}段（字符 {c.start}-{c.end}）\n{c.text}"
