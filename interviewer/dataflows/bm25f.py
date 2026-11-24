"""BM25F 检索内核：bigram 分词 + 手写 BM25F 打分 + 索引统计。
数据流顺序：tokenize（中英混合切 token，中文用字符二元组，零词典零依赖）→
build_stats（全库词频地图，BankIndex 缓存的就是它）→ bm25f_score（query
词集对单题打分：idf 稀有词值钱、饱和函数边际递减、字段权重 topic>tags>text）。
零新依赖：不引 rank_bm25（单字段接不了字段权重），不引 jieba（bigram 对
CJK 检索够用且行为确定）。
"""
import math
import re
from collections import Counter

from interviewer.dataflows.bank_schema import BankQuestion

# 参数锁：小题库上扫参 = overfitting 表演，锁死不动
K1 = 1.5
B = 0.75
FIELDS = ("topic", "tags", "text")
FIELD_WEIGHTS = {"topic": 3.0, "tags": 2.0, "text": 1.0}

# 一段连续 CJK，或一段连续拉丁/数字；其余字符（标点、空白）天然是切段符
_RUN = re.compile(r"[一-鿿]+|[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """文本 → token 列表，规则三条（test_bm25f 逐条锁）：
    1. CJK 连续段切字符二元组："高可用" → ["高可", "可用"]；单字段保留该字；
    2. 拉丁/数字段整体 casefold（"Kafka"→"kafka"、"ISR"→"isr"，整词不切）；
    3. 标点/空白天然切段："CI/CD" → ["ci", "cd"]。

    bigram 的理由：中文没空格，按字切太碎（什么都命中=什么都不命中），词典
    分词要背依赖和歧义包袱；bigram 让双字词天然成块。本函数不去重，query
    侧由调用方收成 set——BM25F 对唯一 term 求和。
    """
    tokens: list[str] = []
    for m in _RUN.finditer(text or ""):
        seg = m.group(0)
        if seg[0].isascii():                    # 拉丁/数字段：整词 + 统一小写
            tokens.append(seg.casefold())
        elif len(seg) == 1:                     # 单个汉字成段：保留该字
            tokens.append(seg)
        else:                                   # CJK 段：相邻两字成块
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def doc_fields(question: BankQuestion) -> dict[str, str]:
    """BankQuestion → 三字段原文。tags 用空格拼接——直拼会造出跨 tag 边界的
    伪 bigram（"队列"+"缓存"拼出谁都没有的"队缓"），空格是天然切段符。"""
    return {
        "topic": question.topic,
        "tags": " ".join(question.tags),
        "text": question.text,
    }


def build_stats(questions: list[BankQuestion]) -> dict:
    """list[BankQuestion] → 索引统计（全库只算一次，BankIndex 缓存它）。
    返回形状（test_bm25f 按此锁死）：
        {
          "n": 题数,
          "df": Counter[str],        # 文档级 df：term 出现在某题任一字段该题计 1
          "avgdl": {field: float},   # 每字段 token 数全库均值，皆空 → 0
          "docs": {qid: {field: Counter, "len": {field: int}}},
        }
    """
    df: Counter = Counter()
    docs: dict = {}
    totals = {f: 0 for f in FIELDS}
    for q in questions:
        fields = doc_fields(q)
        entry: dict = {"len": {}}
        doc_terms: set = set()
        for f in FIELDS:
            toks = tokenize(fields[f])
            entry[f] = Counter(toks)
            entry["len"][f] = len(toks)
            totals[f] += len(toks)
            doc_terms.update(entry[f])       # 同一词跨字段出现，df 仍只计 1 次
        df.update(doc_terms)
        docs[q.id] = entry
    n = len(questions)
    return {
        "n": n,
        "df": df,
        "avgdl": {f: (totals[f] / n if n else 0.0) for f in FIELDS},
        "docs": docs,
    }


def bm25f_score(query_terms: set, doc_entry: dict, stats: dict) -> float:
    """单题打分（Sarawagi-Zaragoza BM25F 形态，字段权重进饱和函数内）：

        score(d, q) = Σ_{t ∈ query_terms} idf(t) · Σ_{f ∈ 字段} sat(W_f · tf)
        sat(x)      = x·(k1+1) / (x + k1·(1 - b + b · len_f(d)/avgdl_f))
        idf(t)      = ln((N - df(t) + 0.5) / (df(t) + 0.5) + 1)   # 平滑恒正

    idf：词越稀有越值钱（"系统"题题都有就不该贡献分）；sat 饱和：出现 2 次
    不是 2 倍分，b 项是长度归一（防长题面靠字多蹭分）；字段权重 W：落在
    topic（W=3）比埋在 text（W=1）值钱。细节契约（手算 fixture 锁死）：
    query_terms 是 set；avgdl_f 为 0 → 长度比取 0；tf=0 的字段直接跳过。
    """
    df = stats["df"]
    avgdl = stats["avgdl"]
    n = stats["n"]
    total = 0.0
    for term in query_terms:
        df_t = df.get(term, 0)
        idf = math.log((n - df_t + 0.5) / (df_t + 0.5) + 1)
        field_sum = 0.0
        for f in FIELDS:
            tf = doc_entry[f].get(term, 0)
            if tf == 0:
                continue
            x = FIELD_WEIGHTS[f] * tf
            ratio = doc_entry["len"][f] / avgdl[f] if avgdl[f] > 0 else 0.0
            field_sum += x * (K1 + 1) / (x + K1 * (1 - B + B * ratio))
        total += idf * field_sum
    return total
