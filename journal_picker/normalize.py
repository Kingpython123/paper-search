"""刊名与标题的规范化。

原项目在这一层几乎没有处理，导致同一个期刊因为大小写、副标题、
HTML 实体（&amp; / &#039;）的差异被拆成多个 key。这里统一收拢。
"""

from __future__ import annotations

import html
import re
import unicodedata

# OpenAlex 的 source.type 到我们内部类型的映射
_VENUE_TYPE_MAP = {
    "journal": "journal",
    "conference": "conference",
    "proceedings": "conference",
    "book series": "book",
    "book": "book",
    "ebook platform": "book",
    "repository": "repository",
    "preprint": "repository",
    "metadata": "other",
    "other": "other",
}

# 会议关键词：OpenAlex 没标 conference 但刊名明显是会议的情况
_CONF_HINTS = (
    "conference", "proceedings", "symposium", "workshop", "congress",
    "annual meeting", "cvpr", "iccv", "eccv", "neurips", "icassp", "interspeech",
)

# 预印本平台：Google 学术兜底路径只能从来源行拿到一个名字、拿不到类型，
# 光靠 type 字段过滤会让 bioRxiv / arXiv 这类从侧门溜进最终榜单。
_REPO_HINTS = (
    "arxiv", "biorxiv", "medrxiv", "chemrxiv", "techrxiv", "engrxiv",
    "research square", "researchsquare", "ssrn", "zenodo", "preprints.org",
    "osf preprints", "hal", "authorea", "figshare", "researchgate",
    "preprint",
)

# 刊名尾部常见的噪声后缀
_TRAILING_NOISE = re.compile(
    r"\s*[\(（](?:print|online|electronic|paper|e-?journal)[\)）]\s*$", re.I)


def unescape(text: str) -> str:
    """HTML 实体反转义 + 全角空格清理。"""
    if not text:
        return ""
    return html.unescape(text).replace("\xa0", " ").replace("\u3000", " ")


def contains_chinese(text: str) -> bool:
    """判断字符串里是否含 CJK 汉字（含扩展区，原项目只判断了基本区）。"""
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF:
            return True
    return False


def clean_journal_name(raw: str) -> str:
    """清洗刊名，用于展示。

    - HTML 实体反转义
    - 折叠空白
    - 去掉「(Print)」这类载体后缀
    - 保留原始大小写（IEEE / ACM 这类缩写不能小写化）
    """
    if not raw:
        return ""
    name = unescape(raw)
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"\s+", " ", name).strip(" .,;:-")
    name = _TRAILING_NOISE.sub("", name).strip()
    return name


# 会议名里的届次/年份标记，例如「2023 IEEE/CVF …」「31st Annual …」「(ICCV'21)」
_EDITION_NOISE = re.compile(
    r"\b(19|20)\d{2}\b|\b\d{1,3}(?:st|nd|rd|th)\b|'\d{2}\b", re.I)


def strip_edition_year(name: str) -> str:
    """去掉会议名里的年份和届次，用于把同一会议的各届合成一行。

    注意不能把 () [] 放进 strip 的字符集：
    「2022 IEEE/CVF Conference on ... (CVPR)」摘掉年份后仍以 ) 结尾，
    一并 strip 掉就变成「... (CVPR」这种缺半个括号的名字。
    正确做法是先清掉「年份被摘走后留下的空括号」，再只 strip 首尾的标点空白。
    """
    if not name:
        return ""
    s = _EDITION_NOISE.sub(" ", name)
    # 形如「Conference on X (2023)」摘掉年份后剩下「(  )」
    s = re.sub(r"[（(\[]\s*[）)\]]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,-–—")
    # 年份处于括号内一侧时可能留下不成对的括号，补回去
    if s.count("(") == s.count(")") + 1 and not s.endswith(")"):
        s += ")"
    elif s.count(")") == s.count("(") + 1 and s.endswith(")"):
        s = s[:-1].strip()
    return s


def conference_series_key(name: str) -> str:
    """会议系列键：CVPR 2022 与 CVPR 2023 应该聚合成同一个投稿目标。"""
    return normalize_for_match(strip_edition_year(name))


def journal_key(name: str, issn_l: str | None = None,
                venue_type: str | None = None) -> str:
    """期刊聚合键。

    会议优先按「系列」聚合：OpenAlex/Crossref 里会议名自带年份，
    不归并的话 CVPR 会被拆成 2021/2022/2023 好几行，失去选刊参考价值。
    期刊则优先用 ISSN-L，这是最可靠的标识；都没有才退化成规范化刊名。
    """
    if venue_type == "conference":
        series = conference_series_key(name)
        if series:
            return f"conf:{series}"
    if issn_l:
        return f"issn:{issn_l}"
    return "name:" + normalize_for_match(name)


def normalize_for_match(text: str) -> str:
    """用于匹配/去重的强规范化：小写、去标点、折叠空白。"""
    if not text:
        return ""
    s = unescape(text)
    s = unicodedata.normalize("NFKD", s)
    s = s.lower()
    # LaTeX 残留与上下标标记
    s = re.sub(r"\$[^$]*\$", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    # 只保留字母数字和 CJK
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_title(title: str) -> str:
    """论文标题规范化，用于跨源去重。"""
    t = unescape(title or "")
    # Google 学术前缀标记，如「[PDF]」「[HTML]」「[图书]」
    t = re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", t)
    return normalize_for_match(t)


def normalize_doi(doi: str | None) -> str | None:
    """DOI 规范化为不带前缀的小写形式，便于跨源比对。"""
    if not doi:
        return None
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d or None


def map_venue_type(oa_type: str | None, name: str = "") -> str:
    """把 OpenAlex 的 source.type 映射成内部类型，并用刊名兜底判会议。"""
    t = (oa_type or "").strip().lower()
    mapped = _VENUE_TYPE_MAP.get(t)
    if mapped:
        # OpenAlex 常把会议论文集标成 journal，用刊名再纠一次
        if mapped == "journal" and looks_like_conference(name):
            return "conference"
        return mapped
    if looks_like_repository(name):
        return "repository"
    if looks_like_conference(name):
        return "conference"
    return "other"


def looks_like_conference(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _CONF_HINTS)


def looks_like_repository(name: str) -> bool:
    """仅凭名字判断是不是预印本平台。"""
    low = (name or "").strip().lower()
    return any(h in low for h in _REPO_HINTS)


def effective_venue_type(venue_type: str | None, venue_name: str | None) -> str | None:
    """论文的实际载体类型。

    类型字段缺失时用刊名推断，这样过滤和聚合两处用的是同一套判断，
    不会出现「过滤时算未知所以留下、聚合时算 other 所以显示出来」的错位。
    名字也没有就返回 None，表示确实无从判断。
    """
    if venue_type:
        return venue_type
    if venue_name:
        return map_venue_type(None, venue_name)
    return None


def short_openalex_id(value: str | None) -> str | None:
    """把 https://openalex.org/S123 变成 S123。"""
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]
