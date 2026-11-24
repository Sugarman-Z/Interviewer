"""本地文件语料源：corpus markdown → BankQuestion。

格式契约（与 corpus/project/*.md 互为合同，改一处必须同步另一处）：
    corpus/<模块>/<主题>.md
    - 独占一行的 `---` = 题块分隔符；第一个 --- 之前是文件头（人读，解析器跳过）
    - 块头：连续 `key: value` 行，ASCII 冒号（中文全角 `：` 不匹配——文件头
      `# 主题：xxx` 正是靠这条规则免于误伤）；tags 逗号分隔转 list、difficulty 转 int
    - 块身：`**题目**` / `**追问树**` / `**评分锚点**` 小节；后两者取 `- ` 弹头行
    - module 从父目录名推导；`_` 前缀目录（如 _staging/）整体排除——未人审
      的候选题在结构上不可能进签发链路，防幻觉纪律延伸到数据侧

失败语义四决策：① 无块头的块（文件头/空块）→ 跳过；② 有块头但校验失败
→ raise 带文件路径+id 定位（题库手写，烂块必现，报错要能 3 秒定位）；
③ 跨文件 id 重复 → raise（id 是防幻觉对账锚，重复即注册表歧义）；
④ 目录不存在 → [] + warning（题库是系统增强非用户承诺，缺席合法）。

fail-open 对照：简历是用户明确提供的输入，失败必须人话重输（fail-loud）；
题库是系统侧增强，缺席走 NO_RESULTS_AVAILABLE → generated 降级——
"fail-open 的前提是没提供，不是提供了但搞砸了"。本文件只管格式翻译 +
尽早暴露烂数据；运行期兜底（CorpusParseError → BANK_CORRUPTED 哨兵）
在 bank_tool 工具层。
"""
import logging
import os
import re
from pathlib import Path

from interviewer.dataflows.bank_schema import BankQuestion
from interviewer.dataflows.bm25f import bm25f_score, build_stats, tokenize
from interviewer.dataflows.errors import CorpusParseError
from interviewer.dataflows.interface import register_vendor

logger = logging.getLogger(__name__)

# 块头行：ASCII 冒号 + \w 键（中文键也匹配，但全角冒号行天然不匹配——见模块注释）
_HEADER_LINE = re.compile(r"^(\w+):\s*(.*)$")
# 块身小节标记：`**题目**` 这类整行加粗
_SECTION_MARKER = re.compile(r"^\*\*(.+?)\*\*$")
# 块分隔符：独占一行的 ---（允许行尾空白）；(?m) 让 ^ $ 按行匹配
_BLOCK_SEP = re.compile(r"(?m)^---\s*$")
# 追问树/评分锚点的弹头（- 或 * 开头）
_BULLET = re.compile(r"^[-*]\s+")


def _md_files(root: Path) -> list[Path]:
    """root 下全部可解析 md：`_` 前缀目录（如 _staging）整体排除。
    parse_corpus 与 _fingerprint 必须共用本函数——指纹若全量 glob，编辑
    staging 文件会白白掀翻语料缓存。只排目录不排 `_` 前缀文件名，排除
    范围贴着 staging 目录这条契约不外溢。"""
    return [
        p for p in sorted(root.glob("**/*.md"))
        if not any(part.startswith("_") for part in p.relative_to(root).parts[:-1])
    ]


def parse_corpus(corpus_dir: str | Path) -> list[BankQuestion]:
    """解析 corpus 下全部 md。目录缺失 → []（决策④）；坏块/重复 id → raise（决策②③）。"""
    root = Path(corpus_dir)
    if not root.is_dir():
        logger.warning("题库目录不存在（题库缺席合法，检索将返回空）：%s", root)
        return []

    questions: list[BankQuestion] = []
    seen_ids: set[str] = set()
    for md in _md_files(root):  # sorted：解析顺序确定 → 报错可复现
        # 父目录即模块；直接放在 corpus 根下的文件按默认模块处理
        module = md.parent.name if md.parent != root else "project"
        # 语料是我们自己的文件，固定 utf-8——对比 resume/file_loader 的解码梯：
        # 那边收用户文件必须 utf-8-sig→gb18030，这边不需要
        # 读取与切块同属"解析"：GBK/ANSI 存档（UnicodeDecodeError）与
        # _parse_block 的字段翻译失败（int() 等裸 ValueError）都要收口成
        # CorpusParseError——2026-11-18 审查抓到的逃逸：这两类曾裸穿
        # bank_tool 窄捕获，题库一坏面试照炸（同类手误两种结局）。
        try:
            blocks = _blocks_of(md.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise CorpusParseError(f"题库坏文件（要求 UTF-8 编码）：{md}｜{exc}") from exc
        except ValueError as exc:
            raise CorpusParseError(f"题库坏块（字段翻译失败）：{md}｜{exc}") from exc
        for parsed in blocks:
            if parsed is None:  # 决策①：文件头/空块占位，跳过
                continue
            header, body = parsed
            try:
                question = BankQuestion(module=module, **header, **body)
            except Exception as exc:  # 决策②：翻译/校验任何失败都带上定位再抛
                raise CorpusParseError(
                    f"题库坏块：{md}｜id={header.get('id', '?')}｜{exc}"
                ) from exc
            if question.id in seen_ids:  # 决策③
                raise CorpusParseError(f"题库 id 重复：{question.id}（{md}）")
            seen_ids.add(question.id)
            questions.append(question)
    return questions


def _blocks_of(text: str) -> list[tuple[dict, dict] | None]:
    """单文件 → [(块头, 块身)]。无块头的块（文件头/空块）以 None 占位，
    由调用方跳过（决策①：None 是"结构性噪音"的显式信号，与"数据错误"区分）。"""
    return [_parse_block(chunk) for chunk in _BLOCK_SEP.split(text)]


def _parse_block(chunk: str) -> tuple[dict, dict] | None:
    """单块 → (块头, 块身)；无块头行 → None。本函数做"格式翻译"（tags→list、
    difficulty→int、弹头剥离），语义校验交给 pydantic、id 查重交给上层——
    每层只做自己的事，报错的调用栈才分工清晰。"""
    lines = [ln.strip() for ln in chunk.splitlines()]

    # 先跳过前导空行：块分隔符 --- 被吃掉后，块首必残留一个空行——
    # 不跳过的话，块头状态机第一行就撞上空行，"头没开始就结束了"
    i = 0
    while i < len(lines) and not lines[i]:
        i += 1

    # 块头状态机：从块首起连续吃 key: value 行，遇第一个不匹配行即停——
    # 块头与块身之间的空行不匹配 ^(\w+):，天然就是边界，无需专门判断
    header: dict = {}
    while i < len(lines):
        m = _HEADER_LINE.match(lines[i])
        if not m:
            break
        header[m.group(1)] = m.group(2).strip()
        i += 1
    if not header:
        return None

    # 格式翻译：markdown 的字符串表示 → 模型字段类型
    #（int()/split 失败抛的裸 ValueError 在 parse_corpus 的读取切块处统一
    # 收口成 CorpusParseError——2026-11-18 审查修正：此处曾宣称"上层会并入
    # 坏块报错"，实际上层 try 只包 BankQuestion 构造，包不到这里的翻译）
    if "tags" in header:
        header["tags"] = [t.strip() for t in header["tags"].split(",") if t.strip()]
    if "difficulty" in header:
        header["difficulty"] = int(header["difficulty"])

    # 块身：按 **小节名** 切分，行归入当前小节；小节标记之前的内容不属于任何小节
    sections: dict[str, list[str]] = {"题目": [], "追问树": [], "评分锚点": []}
    current = None
    for ln in lines[i:]:
        marker = _SECTION_MARKER.match(ln)
        if marker and marker.group(1) in sections:
            current = marker.group(1)
        elif current is not None and ln:
            sections[current].append(ln)

    body = {
        "text": "\n".join(sections["题目"]),
        "followup_tree": [_BULLET.sub("", ln) for ln in sections["追问树"]],
        "scoring_anchors": [_BULLET.sub("", ln) for ln in sections["评分锚点"]],
    }
    return header, body


# 检索（BM25F + BankIndex 缓存）

def search_corpus_local(query, *, exclude_topics=(), top_k=5, corpus_dir="./corpus") -> list[BankQuestion]:
    """BM25F 检索。exclude_topics 是签发查重硬约束，由工具闭包传
    issued_keys 进来，模型看不到这个参数。本函数只是 vendor 签名适配层：
    打分公式在 bm25f 模块、缓存与排序在 BankIndex。"""
    return _get_index(corpus_dir).search(query, exclude_topics=exclude_topics, top_k=top_k)


def topics_of(corpus_dir: str | Path = "./corpus") -> list[str]:
    """题库全部考点清单（三态哨兵的数据源；经 BankIndex 缓存零额外开销）。"""
    return _get_index(corpus_dir).topics


class BankIndex:
    """题库索引：parse 结果 + BM25F 预计算统计，全库只建一次。
    排序键 (-score, -difficulty) + sorted 稳定排序：同分时难度高者前，再同分
    按 parse 顺序决胜（勿加第三键、勿 reverse，行为已被 eval 基线锁定）。"""

    def __init__(self, corpus_dir, fingerprint: tuple = ()):
        self.questions = parse_corpus(corpus_dir)   # 决策②③：坏块/重复 id 在此 raise
        self.stats = build_stats(self.questions)
        self.topics = [q.topic for q in self.questions]
        self.fingerprint = fingerprint

    def search(self, query, *, exclude_topics=(), top_k=5) -> list[BankQuestion]:
        candidates = [q for q in self.questions if q.topic not in exclude_topics]
        terms = set(tokenize(query))
        related = [
            (q, bm25f_score(terms, self.stats["docs"][q.id], self.stats))
            for q in candidates
            if terms
        ]
        related = [pair for pair in related if pair[1] > 0]
        related.sort(key=lambda pair: (-pair[1], -pair[0].difficulty))

        # topic 精确全等直通：query 去空白 casefold 后与某考点全等 → 该题
        # 无条件第一。反例：verbatim "Agent 状态管理"会被 tags 里写着
        # "状态管理"的"前端状态管理"反超——bigram 切出的"态管"恰是别人
        # tag 的部件；"query 就是考点本身"不该输给部件拼盘，直通兜底。
        exact = "".join((query or "").split()).casefold()
        result = [q for q, _ in related]
        if exact:
            for q in candidates:
                if "".join(q.topic.split()).casefold() == exact:
                    result = [q] + [x for x in result if x.id != q.id]
                    break
        return result[:top_k]


def _fingerprint(root: Path) -> tuple:
    """语料指纹：sorted((路径, mtime_ns, size))，与 parse_corpus 同口径
（staging 编辑不触发缓存重建）。mtime_ns 而非浮点 st_mtime（同 tick
    精度洞）；st_size 关掉"同 tick 改等长内容"的理论洞。"""
    files = _md_files(root)
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)


# 模块级缓存：key 用 normcase(resolve)——相对/绝对路径与 Windows 大小写
# 差异会造出"同目录多条目"，normcase 消掉。坏库（parse raise）不进缓存：
# fail-loud 不被缓存短路，修好前每次调用都重新 parse、重新炸。
_INDEX_CACHE: dict[str, tuple[tuple, BankIndex]] = {}


def _get_index(corpus_dir) -> BankIndex:
    """缓存入口：指纹同 → 复用索引；异/首次 → 重 parse 重建。"""
    root = Path(corpus_dir)
    if not root.is_dir():
        # 目录缺席（决策④ fail-open）：现 parse 得空索引且**不缓存**——
        # 目录可能随时被建出来，缓存空结果会吃掉它
        return BankIndex(corpus_dir)
    key = os.path.normcase(str(root.resolve()))
    fp = _fingerprint(root)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == fp:
        return cached[1]
    index = BankIndex(corpus_dir, fingerprint=fp)
    _INDEX_CACHE[key] = (fp, index)
    return index


# vendor 注册：经 interface.route_to_vendor 按配置路由到这里；
# C5 的稠密版（dense_corpus）与混合版（hybrid）以不同源名注册同一接口，
# 同签名可互换
register_vendor("search_corpus", "local", search_corpus_local)
# 第二方法：三态哨兵的"题库现有考点"清单——空命中时 bank_tool 逐家探测
register_vendor("corpus_topics", "local", topics_of)
