"""CCF 目录匹配 + 会议固定审稿周期查询。

CCF 目录（journal_picker/data/ccf_2022.py）和会议周期表
（journal_picker/data/conference_cycles.py）都是本地静态数据，
不发网络请求，匹配纯粹是字符串层面的事。

匹配策略：
    1. 缩写精确匹配（大小写不敏感）—— 最可靠，比如聚合结果里刊名
       正好是 "TPAMI" 或者标准全称里带括号缩写 "(TPAMI)"
    2. 规范化全名精确匹配 —— 处理 "IEEE Transactions on ..." 这种长名字
    3. 括号缩写单独提取匹配 —— 处理 "2023 IEEE/CVF Conference on Computer
       Vision and Pattern Recognition (CVPR)" 这种论文层面常见的写法：
       strip_edition_year 只去年份，末尾括号缩写还留着，得单独抠出来试
    4. 词集合宽松匹配（复用 openalex.title_similar）—— 兜底，容忍
       "Proceedings of the "、"International Conference on " 这类通用前缀
       稀释相似度的情况

会议名称还有一层额外麻烦：OpenAlex/Crossref 给的会议名常年份和届次一起来
（"2023 IEEE/CVF Conference on Computer Vision..."），
所以匹配前先用 normalize.strip_edition_year 去掉这些噪声，
这一步复用聚合阶段已经做过的处理。
"""

from __future__ import annotations

from dataclasses import dataclass

from .data.ccf_2022 import CCF_CATEGORIES, CCF_ENTRIES, CCF_YEAR
from .data.conference_cycles import lookup_conference_cycle
from .normalize import normalize_for_match, strip_edition_year
from .openalex import title_similar


@dataclass(frozen=True)
class CCFEntry:
    abbr: str
    name: str
    venue_type: str  # 'journal' | 'conference'
    rank: str        # 'A' | 'B' | 'C'
    category_zh: str
    category_en: str


def _build_index() -> tuple[dict[str, CCFEntry], dict[str, CCFEntry], list[tuple[set[str], CCFEntry]]]:
    """预建三级索引：缩写 -> 规范化全名 -> 词集合列表（兜底模糊匹配用）。"""
    by_abbr: dict[str, CCFEntry] = {}
    by_name: dict[str, CCFEntry] = {}
    word_index: list[tuple[set[str], CCFEntry]] = []

    for abbr, name, vtype, rank, cat_id in CCF_ENTRIES:
        zh, en = CCF_CATEGORIES.get(cat_id, ("", ""))
        entry = CCFEntry(abbr=abbr, name=name, venue_type=vtype, rank=rank,
                         category_zh=zh, category_en=en)
        abbr_key = normalize_for_match(abbr)
        if abbr_key and abbr_key not in by_abbr:
            # 缩写冲突时保留先出现的（CCF 目录里同缩写在不同类目出现过，
            # 比如 "CC" 同时是网络类的 Computer Communications 和理论类的
            # Computational Complexity，这种情况精确匹配也无法区分，
            # 只能接受这个已知限制，交给全名匹配去纠正）
            by_abbr[abbr_key] = entry

        name_key = normalize_for_match(_strip_abbr_suffix(name))
        if name_key:
            by_name[name_key] = entry
            word_index.append((set(name_key.split()), entry))

    return by_abbr, by_name, word_index


def _strip_abbr_suffix(name: str) -> str:
    """CCF 目录里全名后面常跟着括号缩写，匹配全名时把这个尾巴去掉。"""
    idx = name.rfind("(")
    if idx > 0 and name.rstrip().endswith(")"):
        return name[:idx].strip()
    return name


_BY_ABBR, _BY_NAME, _WORD_INDEX = _build_index()


def match_ccf(venue_name: str, venue_type: str | None = None) -> CCFEntry | None:
    """给一个期刊/会议名找 CCF 条目，找不到返回 None。

    venue_type 传入时用于交叉校验：CCF 目录里期刊和会议是分开列的，
    如果聚合结果标的是会议但匹配到了 CCF 的期刊条目（或反过来），
    说明匹配错了，直接放弃而不是返回一个误导性的等级。
    """
    if not venue_name:
        return None

    stripped = strip_edition_year(venue_name)
    key = normalize_for_match(stripped)
    if not key:
        return None

    entry = _BY_ABBR.get(key) or _BY_NAME.get(key)

    # 论文/期刊层面的名字常年份、届次一起来（"2023 IEEE/CVF Conference on
    # Computer Vision and Pattern Recognition (CVPR)"），strip_edition_year
    # 只去年份，末尾括号里的缩写还留着；这类全名跟 CCF 目录里的全名往往
    # 一字不差，但因为多了个括号缩写尾巴，_BY_NAME 精确匹配会落空，
    # 所以优先试一次「括号里的缩写」+「去掉尾部括号后的名字」两条路。
    if entry is None and "(" in stripped and stripped.rstrip().endswith(")"):
        inner_abbr = normalize_for_match(stripped[stripped.rfind("(") + 1: -1])
        entry = _BY_ABBR.get(inner_abbr)
        if entry is None:
            outer_key = normalize_for_match(stripped[: stripped.rfind("(")])
            entry = _BY_NAME.get(outer_key)

    if entry is None:
        # 兜底：复用 openalex.title_similar 的判定（词集合 Jaccard，
        # 外加「一方是另一方子集」的宽松匹配）。CCF 全名常带
        # 通用前缀词（如 Proceedings of the / International Conference on），
        # 纯 Jaccard 会被这些通用词稀释，子集匹配能兜住这种情况；
        # title_similar 内部阈值本身就比较严，宁可漏配不可错配。
        for cand_words, cand_entry in _WORD_INDEX:
            if cand_words and title_similar(key, " ".join(sorted(cand_words))):
                entry = cand_entry
                break

    if entry is None:
        return None
    if venue_type and entry.venue_type != venue_type:
        return None
    return entry


def conference_cycle_note(venue_name: str, ccf_entry: CCFEntry | None) -> tuple[float, str] | None:
    """给会议查固定审稿周期参考。优先用 CCF 缩写查，查不到再用刊名本身试。"""
    if ccf_entry is not None:
        hit = lookup_conference_cycle(ccf_entry.abbr)
        if hit:
            return hit
    stripped = strip_edition_year(venue_name)
    # 从名字里抠括号里的缩写
    if "(" in stripped and stripped.rstrip().endswith(")"):
        inner = stripped[stripped.rfind("(") + 1: -1].strip()
        hit = lookup_conference_cycle(inner)
        if hit:
            return hit
    return lookup_conference_cycle(stripped)


__all__ = [
    "CCFEntry", "CCF_YEAR", "match_ccf", "conference_cycle_note",
]
