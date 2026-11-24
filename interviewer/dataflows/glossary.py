"""领域术语词表：canonical + aliases 的硬映射加载与查询。
bigram 切词量的是字面重叠——"js 闭包"找不到只写 "javascript 闭包"的题面、
"垃圾回收"与"GC"互为盲区；词表把这层同义关系从"指望切词撞上"变成
"显式声明、显式测量"。三个消费方见 data/glossary.yaml 头注释：lint 对账 /
promote 拒绝入库 / 金标变体。本模块刻意不接检索本体——别名追加进 query
会改变全部检索数字，等 synonym 桶基线带 eval 对拍后再议。

匹配护栏：拉丁面带词边界（匹配位置两侧不得是 [A-Za-z0-9]，"os" 切不中
"postgres"；边界只认 ASCII 字母数字，汉字不是边界——"js闭包原理"里的
"js" 算有界命中，LLM 检索串天然无空格混排）；中文面无边界；一次变体只换
一个面（控 precision：多重替换测不出单点归因）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _latin_alnum(ch: str) -> bool:
    """ch 是否为 [A-Za-z0-9]（护栏文档口径的边界字符）。空串（串首/串尾）
    不是边界字符。不得直接用 str.isalnum()：它对汉字恒 True，会把 CJK
    邻接误判成"无界"（见 _find_bounded 的踩坑注）。"""
    return bool(ch) and ch.isascii() and ch.isalnum()


@dataclass
class Glossary:
    """加载后的词表。surfaces 把每个面（canonical+aliases，casefold）指回
    canonical——同义归一的查表结构；faces 保留每个 canonical 的原始面
    序列（变体生成按"换面"产出，不改原始大小写）。"""

    faces: dict[str, list[str]] = field(default_factory=dict)
    surfaces: dict[str, str] = field(default_factory=dict)

    def covers(self, term: str) -> bool:
        """term（tag 或任意词面）是否在词表内——大小写不敏感。"""
        return term.casefold() in self.surfaces

    def unknown_tags(self, tags: list[str]) -> list[str]:
        """题库/候选题 tags 里词表未覆盖的（保序去重）——lint ⚠ 与
        promote 拒绝入库共用一个判据。"""
        seen, out = set(), []
        for t in tags:
            if t not in seen and not self.covers(t):
                seen.add(t)
                out.append(t)
        return out

    def variant_queries(self, query: str) -> list[str]:
        """query 的同义变体：把 query 里出现的一个面换成同 canonical 的
        另一个面，其余原样。每个可换点 × 每个备选面各产一条，去重保序。
        无可换点 → []（调用方据此跳过，不产空变体）。"""
        variants, seen = [], set()
        for surface, canonical in self.surface_hits(query):
            for face in self.faces[canonical]:
                if face.casefold() == surface:
                    continue
                v = self._replace_one(query, surface, face)
                if v != query and v not in seen:
                    seen.add(v)
                    variants.append(v)
        return variants

    # 面命中与单点替换

    def surface_hits(self, query: str) -> list[tuple[str, str]]:
        """query 里命中的 (casefold 面, canonical)，按面长度降序（长面优先
        ——"垃圾回收"先于"回收"类子面），同长按 query 位置从左到右。
        公共面：缺题挖掘（suggested_tags）与 variant_queries 共用同一份
        命中判定——两处各自写 find 循环 = 边界护栏悄悄分叉。"""
        hits = []
        for surface, canonical in self.surfaces.items():
            latin = any(c.isascii() and c.isalnum() for c in surface)
            pos = self._find_bounded(query.casefold(), surface, latin)
            while pos is not None:
                hits.append((surface, canonical, pos))
                pos = self._find_bounded(query.casefold(), surface, latin, start=pos + 1)
        hits.sort(key=lambda h: (-len(h[0]), h[2]))
        out = []
        for surface, canonical, _ in hits:
            if (surface, canonical) not in out:
                out.append((surface, canonical))
        return out

    @staticmethod
    def _find_bounded(lowered: str, surface: str, latin: bool, start: int = 0) -> int | None:
        """lowered 里下一个"有界"出现位置（None=无——两条分支同一语义；
        踩坑记录：非拉丁分支曾直接返回 find 的 -1，调用方判 is not None
        → -1 被当命中、无限追加撑爆内存）。

        边界护栏只对含拉丁/数字的面生效：匹配段两侧不得是 ASCII 字母数字
（"os" 切不中 "postgres"，两侧都查）。判定不得用裸 str.isalnum()：
        对汉字恒 True，会把 CJK 邻接误判成"无界"（"js闭包原理"里的 "js"
        整面漏配）——_latin_alnum 钉死 [A-Za-z0-9]。纯中文面无边界概念。
        """
        pos = lowered.find(surface, start)
        if not latin:
            return pos if pos != -1 else None
        while pos != -1:
            before = lowered[pos - 1] if pos > 0 else ""
            after = lowered[pos + len(surface)] if pos + len(surface) < len(lowered) else ""
            if not (_latin_alnum(before) or _latin_alnum(after)):
                return pos
            pos = lowered.find(surface, pos + 1)
        return None

    @staticmethod
    def _replace_one(query: str, surface: str, face: str) -> str:
        """把 query 里第一处（带边界）的 surface 换成 face（边界判定与
        surface_hits 同一份 _find_bounded——替换点和命中点口径必须一致，
        否则出现"命中了却换不掉"的幽灵面）。

        pos 是在 query.casefold() 上找到的，只能用于等长 casefold 的
        query：casefold 会把 ß→ss 变长，下标与原串错位（'straße kafka
        集群'曾产出 'kkafka' 乱码变体静默进金标）。长度不等时放弃该
        换面点——宁少产变体，不产错位变体。"""
        latin = any(c.isascii() and c.isalnum() for c in surface)
        lowered = query.casefold()
        if len(lowered) != len(query):
            return query
        pos = Glossary._find_bounded(lowered, surface, latin)
        if pos is None:
            return query
        return query[:pos] + face + query[pos + len(surface):]


def load_glossary(path: str | Path) -> Glossary:
    """读 yaml → Glossary。fail-loud 三连（词表是手工维护的数据，坏形状
    必须当场炸，不能静默半载）：文件不存在/yaml 语法错 → 原样抛；条目形状
    不对（缺 canonical、aliases 不是列表、未知键）→ ValueError 带条目号；
    同一个面挂在两个 canonical 下 → ValueError 带两侧名字。
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries = (raw or {}).get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: 缺 entries 列表（词表形状见文件头注释）")

    gl = Glossary()
    for i, e in enumerate(entries, start=1):
        if not isinstance(e, dict) or not str(e.get("canonical", "")).strip():
            raise ValueError(f"{path}: 第 {i} 条缺 canonical")
        unknown_keys = set(e) - {"canonical", "aliases"}
        if unknown_keys:
            raise ValueError(
                f"{path}: 第 {i} 条有未知键 {sorted(unknown_keys)}"
                f"（只认 canonical/aliases——键名拼错必须当场炸，静默当无别名"
                f"等于词表面悄悄丢一半）"
            )
        canonical = str(e["canonical"]).strip()
        if canonical in gl.faces:
            raise ValueError(
                f"{path}: canonical {canonical!r} 出现第二条（第 {i} 条）——"
                f"后条会静默覆盖前条的换面池（faces 被覆盖、surfaces 残留），"
                f"变体生成单向残缺且零报警，先合并成一条"
            )
        aliases = e.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise ValueError(f"{path}: 第 {i} 条（{canonical}）的 aliases 必须是字符串列表")
        faces = [canonical] + [a.strip() for a in aliases if a.strip()]
        gl.faces[canonical] = faces
        for face in faces:
            key = face.casefold()
            owner = gl.surfaces.get(key)
            if owner is not None and owner != canonical:
                raise ValueError(
                    f"{path}: 面 {face!r} 同时挂在 {owner!r} 和 {canonical!r} 下"
                    f"——同义归一会随机指向，先解除表内冲突"
                )
            gl.surfaces[key] = canonical
    return gl
