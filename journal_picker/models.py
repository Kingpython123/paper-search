"""数据模型：论文、期刊指标、期刊聚合结果。

这里刻意用 dataclass 而不是原项目那种嵌套 dict + 哨兵元素的结构，
原因是原项目把「共N篇」当字符串塞进 list[0] 当计数器，
一旦 list[0] 恰好是 dict 就会在 `'共' in list[0]` 处抛 TypeError。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# 期刊/会议/其它载体的类型归一化后的取值
VENUE_JOURNAL = "journal"
VENUE_CONFERENCE = "conference"
VENUE_REPOSITORY = "repository"
VENUE_BOOK = "book"
VENUE_OTHER = "other"


@dataclass
class Paper:
    """一篇论文。source_provider 记录它是从哪个检索源发现的。"""

    title: str
    # 规范化后的标题，用于跨源去重
    norm_title: str = ""
    doi: str | None = None
    year: int | None = None
    cited_by: int | None = None
    url: str | None = None
    authors: list[str] = field(default_factory=list)
    # 载体信息（可能为空，例如 Google 学术抓到但 OpenAlex 里查不到的条目）
    venue_name: str | None = None
    venue_id: str | None = None
    venue_issn_l: str | None = None
    venue_type: str | None = None
    # 发现该论文的检索源，如 {"openalex", "scholar"}
    providers: set[str] = field(default_factory=set)
    # OpenAlex work id
    work_id: str | None = None

    def merge(self, other: "Paper") -> None:
        """跨源合并同一篇论文，缺失字段互补。"""
        self.providers |= other.providers
        for attr in ("doi", "year", "cited_by", "url", "work_id",
                     "venue_name", "venue_id", "venue_issn_l", "venue_type"):
            if getattr(self, attr) in (None, "") and getattr(other, attr) not in (None, ""):
                setattr(self, attr, getattr(other, attr))
        if not self.authors and other.authors:
            self.authors = other.authors
        # 引用数取大值：不同源统计口径不同，取高的那个更接近真实影响力
        if other.cited_by is not None and (self.cited_by is None or other.cited_by > self.cited_by):
            self.cited_by = other.cited_by

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["providers"] = sorted(self.providers)
        return d


@dataclass
class JournalMetrics:
    """期刊层面的指标。OpenAlex 部分必然有，letpub/CCF 部分是可选富化。"""

    # --- OpenAlex ---
    # 2yr_mean_citedness，口径等价于 JIF（两年期平均被引），是 IF 的开放替代
    impact_2yr: float | None = None
    h_index: int | None = None
    works_count: int | None = None
    cited_by_count: int | None = None
    is_oa: bool | None = None
    is_in_doaj: bool | None = None
    apc_usd: int | None = None
    publisher: str | None = None
    country_code: str | None = None
    homepage: str | None = None

    # --- letpub（中文用户关心的中科院分区等，best-effort） ---
    cas_quartile: int | None = None       # 中科院分区，1~4
    letpub_if: float | None = None        # letpub 展示的 JCR 影响因子
    letpub_citescore: float | None = None
    review_period: str | None = None      # 审稿周期，如「约7.9个月」
    acceptance: str | None = None         # 录用比例，如「很难」
    sci_index: str | None = None          # SCI / SCIE / SSCI ...
    cas_category: str | None = None       # 大类/小类学科
    letpub_url: str | None = None

    # --- CCF（本地静态目录，2022 版） ---
    ccf_rank: str | None = None           # A / B / C
    ccf_abbr: str | None = None           # 缩写，如 CVPR / TPAMI
    ccf_category: str | None = None       # CCF 十大类目里的中文类目名
    # 会议固定审稿周期（本地静态参考表，跟 letpub 的统计口径不是同一种东西，
    # 字段单独起名以示区分，报告里也会分列展示）
    conf_cycle_months: float | None = None
    conf_cycle_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Journal:
    """按期刊聚合后的结果，是最终报告的一行。"""

    name: str
    venue_type: str = VENUE_OTHER
    venue_id: str | None = None
    issn_l: str | None = None
    is_chinese: bool = False
    metrics: JournalMetrics = field(default_factory=JournalMetrics)
    papers: list[Paper] = field(default_factory=list)
    # 会议按系列聚合后会对应多个 OpenAlex source（每届一个），全都记下来，
    # 取指标时挑发文量最大的那一届作为代表，而不是碰上哪个算哪个
    venue_ids: set[str] = field(default_factory=set)

    @property
    def paper_count(self) -> int:
        return len(self.papers)

    @property
    def total_citations(self) -> int:
        return sum(p.cited_by or 0 for p in self.papers)

    @property
    def best_impact(self) -> float:
        """排序用的影响力代理值：优先 letpub 的 IF，其次 OpenAlex 两年期均被引。"""
        if self.metrics.letpub_if is not None:
            return self.metrics.letpub_if
        if self.metrics.impact_2yr is not None:
            return self.metrics.impact_2yr
        return -1.0

    @property
    def quartile_sort_key(self) -> int:
        """未收录的排在最后，而不是被当成 0 区排到最前。"""
        return self.metrics.cas_quartile if self.metrics.cas_quartile else 99

    @property
    def ccf_sort_key(self) -> int:
        """A/B/C -> 0/1/2，未收录排最后。"""
        order = {"A": 0, "B": 1, "C": 2}
        return order.get(self.metrics.ccf_rank or "", 9)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "venue_type": self.venue_type,
            "venue_id": self.venue_id,
            "issn_l": self.issn_l,
            "is_chinese": self.is_chinese,
            "venue_ids": sorted(self.venue_ids),
            "paper_count": self.paper_count,
            "total_citations": self.total_citations,
            "metrics": self.metrics.to_dict(),
            "papers": [p.to_dict() for p in self.papers],
        }


@dataclass
class SearchQuery:
    """一次检索的全部参数。"""

    keywords_all: list[str] = field(default_factory=list)   # 需要同时命中（AND）
    keywords_any: list[str] = field(default_factory=list)   # 命中任一即可（OR）
    scope: str = "title"          # title | fulltext
    year_from: int | None = None
    year_to: int | None = None
    max_papers: int = 200
    providers: list[str] = field(default_factory=lambda: ["openalex"])
    include_types: list[str] = field(
        default_factory=lambda: [VENUE_JOURNAL, VENUE_CONFERENCE])

    def describe(self) -> str:
        parts = []
        if self.keywords_all:
            parts.append(" AND ".join(f'"{k}"' for k in self.keywords_all))
        if self.keywords_any:
            parts.append("(" + " OR ".join(f'"{k}"' for k in self.keywords_any) + ")")
        q = " AND ".join(parts) if parts else "(空)"
        rng = ""
        if self.year_from or self.year_to:
            rng = f" [{self.year_from or ''}-{self.year_to or ''}]"
        return f"{q}{rng} scope={self.scope}"
