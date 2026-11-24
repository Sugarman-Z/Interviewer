"""第二检索面 vendor：简历/JD 原文的段落检索（修"简历大部分内容签发
prompt 看不见"的问题）。把 state 全文切成带编号的段落，模型经工具按需查
原文——每个段落有 cand-XXXX 编号 + 原文字符区间，source_ref 里引用的编号
可拿区间回原文核验（全程溯源从口号变机制）。

与题库面的三个不同：语料在内存不在磁盘（state 快照，tokenize 全量重算
毫秒级，不引缓存）；单一字段（同一套 K1/B 与 tokenize，字段权重退化为
1.0，公式手算锁在 test_candidate_corpus.py）；不扫目录（无 `_` 隔离议题）。
已知边界：BM25 无分数阈值，远域 query 靠功能词 bigram 也可能非零分——
段落命中代价低（多读一段无关原文），v1 不设闸。
"""
import math
import re
from collections import Counter
from dataclasses import dataclass

from interviewer.dataflows.bm25f import B, K1, tokenize
from interviewer.dataflows.interface import CANDIDATE_TOOL_NAME, register_vendor


@dataclass(frozen=True)
class CandidateChunk:
    """简历/JD 的一个段落块。区间合同：full_text[start:end] == text
（strip 只削两端，段落正文必是原文本的连续切片——测试锁死这条）。"""

    id: str          # cand-0001 起，resume+jd 合并顺序编号（会话内全局唯一）
    source: str      # "resume" | "jd"——"简历第N段/JD第N段"标注的锚
    para: int        # 段序号（1 起，按 source 内独立计数）
    start: int       # 原文字符区间（source 各自的全文坐标系）
    end: int
    text: str        # 段落原文（内部换行保留，引用证据要逐字）


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """原文上的非空段落区间。空白行是切段边界；段内单个换行保留（简历的
    bullet 群通常空行分组，组内行属同一段证据）。按行扫描累计偏移——
    re.split 会丢掉偏移量，编号的字符区间就没了，可核验性正建立在偏移上。"""
    spans, start, pos = [], None, 0
    for line in text.splitlines(keepends=True):
        if line.strip():
            if start is None:
                start = pos
        elif start is not None:
            spans.append((start, pos))
            start = None
        pos += len(line)
    if start is not None:
        spans.append((start, pos))
    return spans


def chunk_texts(texts: dict | None) -> list[CandidateChunk]:
    """{"resume": 全文, "jd": 全文} → 段落块列表（resume 在前、jd 在后，
    编号跨源连续——一份清单一个账本，防幻觉对账不关心段落来自哪个源）。"""
    chunks, idx = [], 0
    for source in ("resume", "jd"):
        raw = (texts or {}).get(source) or ""
        for para, (s, e) in enumerate(_paragraph_spans(raw), start=1):
            seg = raw[s:e]
            body = seg.strip()
            if not body:
                continue   # 理论不可达（span 首尾行非空白），防御性保留
            off = seg.find(body)   # strip 只削两端 → body 必是 seg 的连续切片
            idx += 1
            chunks.append(CandidateChunk(
                id=f"cand-{idx:04d}", source=source, para=para,
                start=s + off, end=s + off + len(body), text=body,
            ))
    return chunks


def _bm25_single_field(query_terms: set, counter: Counter, df: Counter,
                       n: int, dl: int, avgdl: float) -> float:
    """单字段 BM25 = 字段权重 1.0 的 BM25F 特例（idf 平滑、K1 饱和、B 长度
    归一与 bm25f.py 同式同参）。独立成纯函数是为了 test_candidate_corpus
    的硬编码数值手算锁。"""
    s = 0.0
    for t in query_terms:
        tf = counter.get(t, 0)
        if not tf:
            continue
        idf = math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1)
        s += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / avgdl))
    return s


def search_candidate_local(query: str, *, exclude_ids: tuple = (),
                           top_k: int = 3, texts: dict | None = None) -> list[CandidateChunk]:
    """段落 BM25 检索（单字段，K1/B 与题库面同值——同一把尺子量两种语料）。
    语义决策：零分不进榜（无词块交集不返回，空列表是工具层哨兵的原料）；
    平票按 id 升序（确定性输出）；df 文档级（与题库面同口径）。"""
    chunks = chunk_texts(texts)
    if not chunks:
        return []
    exclude = set(exclude_ids)
    counters = [Counter(tokenize(c.text)) for c in chunks]
    df: Counter = Counter()
    for ctr in counters:
        df.update(ctr.keys())
    n = len(chunks)
    avgdl = (sum(sum(ctr.values()) for ctr in counters) / n) or 1.0

    qterms = set(tokenize(query))
    scored = []
    for c, ctr in zip(chunks, counters):
        if c.id in exclude:
            continue
        s = _bm25_single_field(qterms, ctr, df, n, sum(ctr.values()), avgdl)
        if s > 0:
            scored.append((s, c))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [c for _, c in scored[:top_k]]


register_vendor(CANDIDATE_TOOL_NAME, "local", search_candidate_local)
