"""编排层：检索 -> 合并去重 -> 补载体 -> 按期刊聚合 -> 取指标 -> 标 CCF -> 排序。

原项目把这套流程和 tkinter 界面、网络请求全都揉在一个 run() 里，
中间还夹着「每处理一篇就全量读写 JSON 再整体重排」的 O(n^2) 操作。
这里拆成纯函数 + 一个 Pipeline 类，网络层可替换，聚合逻辑可单独测。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .cache import Cache
from .ccf import conference_cycle_note, match_ccf
from .crossref import CrossrefClient
from .letpub import LetpubClient
from .models import Journal, Paper, SearchQuery
from .normalize import (
    clean_journal_name,
    contains_chinese,
    effective_venue_type,
    journal_key,
    short_openalex_id,
    strip_edition_year,
)
from .openalex import OpenAlexBudgetExhausted, OpenAlexClient
from .scholar import ScholarClient, ScholarUnavailable, hits_to_papers

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]

SORT_KEYS = {
    "papers": "命中论文数（该方向在此刊的发文量，默认）",
    "impact": "影响力（letpub IF 优先，其次 OpenAlex 两年期均被引）",
    "quartile": "分区优先，再按影响力",
    "citations": "该刊命中论文的总被引",
    "ccf": "CCF 等级优先（A>B>C>未收录），再按发文量",
    "name": "刊名字典序",
}

UNKNOWN_VENUE = "（未识别载体）"


@dataclass
class RunStats:
    """一次运行的统计，用于在报告和界面上说明数据是怎么来的。"""

    started_at: float = field(default_factory=time.time)
    elapsed: float = 0.0
    openalex_hits: int = 0
    openalex_total: int = 0
    scholar_hits: int = 0
    papers_after_merge: int = 0
    crossref_filled: int = 0
    dropped_by_type: int = 0
    letpub_hits: int = 0
    ccf_hits: int = 0
    journals: int = 0
    unresolved_papers: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "elapsed_seconds": round(self.elapsed, 1),
            "openalex_hits": self.openalex_hits,
            "openalex_total_matches": self.openalex_total,
            "scholar_hits": self.scholar_hits,
            "papers_after_merge": self.papers_after_merge,
            "crossref_filled": self.crossref_filled,
            "dropped_by_type": self.dropped_by_type,
            "letpub_hits": self.letpub_hits,
            "ccf_hits": self.ccf_hits,
            "journals": self.journals,
            "unresolved_papers": self.unresolved_papers,
            "warnings": self.warnings,
        }


@dataclass
class PipelineResult:
    query: SearchQuery
    journals: list[Journal]
    papers: list[Paper]
    stats: RunStats
    sort_key: str = "papers"


# --------------------------------------------------------------------- 合并去重

def merge_papers(batches: list[list[Paper]]) -> list[Paper]:
    """跨源合并论文。

    先按 DOI 对齐（最可靠），DOI 缺失时退化到规范化标题。
    原项目没有去重概念，同一篇论文被不同页/不同源重复计数。
    """
    by_doi: dict[str, Paper] = {}
    by_title: dict[str, Paper] = {}
    order: list[Paper] = []

    for batch in batches:
        for paper in batch:
            existing = None
            if paper.doi and paper.doi in by_doi:
                existing = by_doi[paper.doi]
            elif paper.norm_title and paper.norm_title in by_title:
                existing = by_title[paper.norm_title]

            if existing is not None:
                existing.merge(paper)
                # 合并后可能新拿到 DOI，补进索引
                if existing.doi:
                    by_doi.setdefault(existing.doi, existing)
                continue

            order.append(paper)
            if paper.doi:
                by_doi[paper.doi] = paper
            if paper.norm_title:
                by_title[paper.norm_title] = paper
    return order


def filter_by_types(papers: list[Paper],
                   include_types: list[str]) -> tuple[list[Paper], int]:
    """按载体类型过滤论文，返回 (保留的论文, 丢弃数)。

    OpenAlex 那一路是在 API filter 里就把预印本库挡掉的，
    但 Google 学术那一路是「先拿标题、再回查 OpenAlex」，绕过了那道过滤，
    结果 arXiv / Research Square 会从侧门溜进最终榜单。
    这里统一在聚合前再筛一遍。

    venue_type 为空的论文保留：那是「查不到载体」，不是「载体类型不符」，
    直接丢掉会让用户以为这些论文不存在。
    """
    allowed = {t for t in include_types if t}
    if not allowed:
        return papers, 0
    kept: list[Paper] = []
    dropped = 0
    for paper in papers:
        vtype = effective_venue_type(paper.venue_type, paper.venue_name)
        if vtype and vtype not in allowed:
            dropped += 1
            continue
        kept.append(paper)
    return kept, dropped


# ----------------------------------------------------------------------- 聚合

def aggregate(papers: list[Paper], include_unknown: bool = True) -> list[Journal]:
    """按期刊/会议把论文聚合成 Journal 列表。"""
    buckets: dict[str, Journal] = {}
    for paper in papers:
        name = clean_journal_name(paper.venue_name or "")
        vtype = effective_venue_type(paper.venue_type, name)
        if not name:
            if not include_unknown:
                continue
            key = "unknown"
            display = UNKNOWN_VENUE
            vtype = "other"
            # 占位名本身带汉字，不能让它被 contains_chinese 误判成国内刊
            is_cn = False
        else:
            key = journal_key(name, paper.venue_issn_l, vtype)
            # 会议按系列聚合，展示名去掉年份，否则 CVPR 会按届拆开
            display = strip_edition_year(name) if vtype == "conference" else name
            is_cn = contains_chinese(display or name)

        journal = buckets.get(key)
        if journal is None:
            journal = Journal(
                name=display or name,
                venue_type=vtype or "other",
                venue_id=paper.venue_id,
                issn_l=paper.venue_issn_l,
                is_chinese=is_cn,
            )
            if paper.venue_id:
                journal.venue_ids.add(paper.venue_id)
            buckets[key] = journal
        else:
            # 同一桶里补齐标识，某些论文记录里 ISSN / venue_id 是空的
            journal.venue_id = journal.venue_id or paper.venue_id
            journal.issn_l = journal.issn_l or paper.venue_issn_l
        if paper.venue_id:
            journal.venue_ids.add(paper.venue_id)
        journal.papers.append(paper)

    for journal in buckets.values():
        journal.papers.sort(key=lambda p: (-(p.cited_by or 0), -(p.year or 0)))
    return list(buckets.values())


def attach_openalex_metrics(journals: list[Journal],
                            sources: dict[str, dict[str, Any]]) -> None:
    """把 /sources 返回的指标写进 Journal.metrics。"""
    by_issn = {
        (s.get("issn_l") or "").lower(): s
        for s in sources.values() if s.get("issn_l")
    }
    for journal in journals:
        # 会议跨届合并后会有多个 source，挑发文量最大的一届当代表
        candidates = [sources[v] for v in journal.venue_ids
                      if sources.get(v)]
        raw = None
        if candidates:
            raw = max(candidates, key=lambda s: s.get("works_count") or 0)
        elif journal.venue_id:
            raw = sources.get(journal.venue_id)
        if not raw and journal.issn_l:
            raw = by_issn.get(journal.issn_l.lower())
        if not raw:
            continue
        stats = raw.get("summary_stats") or {}
        m = journal.metrics
        # OpenAlex 不给会议算 2yr_mean_citedness，一律返回 0.0。
        # 实测 CVPR 2022（23 万被引）也是 0.0，照抄会让人以为这会议没人引。
        impact = stats.get("2yr_mean_citedness")
        if journal.venue_type == "conference" and not impact:
            impact = None
        m.impact_2yr = impact
        m.h_index = stats.get("h_index")
        m.works_count = raw.get("works_count")
        m.cited_by_count = raw.get("cited_by_count")
        m.is_oa = raw.get("is_oa")
        m.is_in_doaj = raw.get("is_in_doaj")
        m.apc_usd = raw.get("apc_usd")
        m.publisher = raw.get("host_organization_name")
        m.country_code = raw.get("country_code")
        # OpenAlex 对国内期刊统一用英文刊名（「Chinese Journal of Lasers」而不是「中国激光」），
        # 所以「刊名含汉字」这个判据几乎不会触发。
        # 对国内投稿来说真正想知道的是「这是不是国刊」，用出版国判断才准。
        if raw.get("country_code") == "CN":
            journal.is_chinese = True
        m.homepage = raw.get("homepage_url")
        journal.issn_l = journal.issn_l or raw.get("issn_l")
        journal.venue_id = journal.venue_id or short_openalex_id(raw.get("id"))
        # 刊名统一用 OpenAlex 的 display_name：
        # Crossref 的 container-title 大小写不稳定（会给出 "Genome biology"、
        # "Journal of medical imaging"），同一本刊在不同来源下长得不一样。
        # 会议除外——那边要保留去掉年份后的系列名。
        canonical = clean_journal_name(raw.get("display_name") or "")
        if canonical and journal.venue_type != "conference":
            journal.name = canonical


# ------------------------------------------------------------------- CCF 匹配

def attach_ccf_info(journals: list[Journal]) -> int:
    """给聚合结果标 CCF 等级 + 会议固定审稿周期，返回命中数。

    这一步是纯本地字符串匹配，不发网络请求。放在指标拉取之后执行，
    是因为这时候期刊名已经被 OpenAlex 的 display_name 规范化过一轮，
    跟 CCF 目录里的官方全称更接近，匹配命中率更高。
    """
    hits = 0
    for journal in journals:
        entry = match_ccf(journal.name, journal.venue_type)
        if entry is not None:
            journal.metrics.ccf_rank = entry.rank
            journal.metrics.ccf_abbr = entry.abbr
            journal.metrics.ccf_category = entry.category_zh
            hits += 1
        if journal.venue_type == "conference":
            cyc = conference_cycle_note(journal.name, entry)
            if cyc:
                journal.metrics.conf_cycle_months, journal.metrics.conf_cycle_note = cyc
    return hits


# ----------------------------------------------------------------------- 排序

def sort_journals(journals: list[Journal], key: str = "papers") -> list[Journal]:
    """多级排序。

    原项目把排序信息编码进字典 key（「2区 Citescore:8.5 刊名」）
    然后靠字符串排序，取巧但没法换排序方式；这里改成显式比较函数。
    未识别载体一律沉底。
    """
    def bottom(j: Journal) -> int:
        return 1 if j.name == UNKNOWN_VENUE else 0

    if key == "impact":
        keyfn = lambda j: (bottom(j), -j.best_impact, -j.paper_count, j.name.lower())
    elif key == "quartile":
        keyfn = lambda j: (bottom(j), j.quartile_sort_key, -j.best_impact,
                           -j.paper_count, j.name.lower())
    elif key == "citations":
        keyfn = lambda j: (bottom(j), -j.total_citations, -j.paper_count, j.name.lower())
    elif key == "ccf":
        keyfn = lambda j: (bottom(j), j.ccf_sort_key, -j.paper_count,
                           -j.best_impact, j.name.lower())
    elif key == "name":
        keyfn = lambda j: (bottom(j), j.name.lower())
    else:  # papers
        keyfn = lambda j: (bottom(j), -j.paper_count, -j.best_impact, j.name.lower())
    return sorted(journals, key=keyfn)


# --------------------------------------------------------------------- Pipeline

class Pipeline:
    """把各数据源串起来的编排器。

    所有耗时操作都接受 stop 回调，GUI 点「停止」时能立刻收尾，
    而不是像原项目那样只能 Ctrl+C 掉整个终端。
    """

    def __init__(self, cache: Cache | None = None, mailto: str | None = None,
                 openalex_key: str | None = None,
                 scholar_backend: str = "http", serpapi_key: str | None = None,
                 scholar_base: str | None = None,
                 scholar_delay: tuple[float, float] = (5.0, 12.0),
                 use_crossref: bool = True, use_letpub: bool = True,
                 use_ccf: bool = True,
                 on_progress: ProgressFn | None = None,
                 stop: StopFn | None = None) -> None:
        self.on_progress = on_progress or (lambda msg: None)
        self.stop = stop or (lambda: False)
        self.cache = cache
        self.use_crossref = use_crossref
        self.use_letpub = use_letpub
        self.use_ccf = use_ccf

        self.openalex = OpenAlexClient(cache=cache, mailto=mailto,
                                       api_key=openalex_key,
                                       on_progress=self.on_progress)
        self._scholar_kwargs = dict(
            backend=scholar_backend, cache=cache, serpapi_key=serpapi_key,
            min_delay=scholar_delay[0], max_delay=scholar_delay[1],
            on_progress=self.on_progress)
        if scholar_base:
            self._scholar_kwargs["base_url"] = scholar_base
        self._scholar: ScholarClient | None = None
        self.crossref = CrossrefClient(cache=cache, mailto=mailto,
                                       on_progress=self.on_progress) if use_crossref else None
        self.letpub = LetpubClient(cache=cache,
                                   on_progress=self.on_progress) if use_letpub else None

    @property
    def scholar(self) -> ScholarClient:
        if self._scholar is None:
            self._scholar = ScholarClient(**self._scholar_kwargs)
        return self._scholar

    def close(self) -> None:
        if self._scholar is not None:
            self._scholar.close()

    # ------------------------------------------------------------------ 主流程

    def run(self, query: SearchQuery, sort_key: str = "papers") -> PipelineResult:
        stats = RunStats()
        batches: list[list[Paper]] = []

        if "openalex" in query.providers:
            batches.append(self._run_openalex(query, stats))
        if "scholar" in query.providers:
            batches.append(self._run_scholar(query, stats))

        papers = merge_papers(batches)
        stats.papers_after_merge = len(papers)
        self.on_progress(f"合并去重后共 {len(papers)} 篇论文")

        if self.crossref and not self.stop():
            stats.crossref_filled = self.crossref.fill_missing_venues(papers, self.stop)
            if stats.crossref_filled:
                self.on_progress(f"Crossref 补回 {stats.crossref_filled} 篇论文的载体")

        papers, stats.dropped_by_type = filter_by_types(papers, query.include_types)
        if stats.dropped_by_type:
            self.on_progress(
                f"按载体类型剔除 {stats.dropped_by_type} 篇（预印本库/图书等不在所选类型内）")

        stats.unresolved_papers = sum(1 for p in papers if not p.venue_name)
        journals = aggregate(papers)
        stats.journals = len(journals)
        self.on_progress(f"聚合出 {len(journals)} 个候选载体")

        if not self.stop():
            self._attach_metrics(journals)

        if self.letpub and not self.stop():
            stats.letpub_hits = self.letpub.enrich(journals, stop=self.stop)
            self.on_progress(f"letpub 命中 {stats.letpub_hits} 本期刊")
            if self.letpub.blocked_count:
                stats.warnings.append(
                    f"letpub 期间被限流 {self.letpub.blocked_count} 次，"
                    f"部分期刊的分区/IF 可能为空，稍后重跑可命中（已缓存的不会重复请求）")

        if self.use_ccf and not self.stop():
            stats.ccf_hits = attach_ccf_info(journals)
            self.on_progress(f"CCF 目录匹配命中 {stats.ccf_hits} / {len(journals)} 个载体")

        journals = sort_journals(journals, sort_key)
        stats.elapsed = time.time() - stats.started_at
        return PipelineResult(query=query, journals=journals, papers=papers,
                              stats=stats, sort_key=sort_key)

    # ------------------------------------------------------------------ 分步骤

    def _run_openalex(self, query: SearchQuery, stats: RunStats) -> list[Paper]:
        self.on_progress("=== OpenAlex 检索 ===")
        try:
            stats.openalex_total = self.openalex.count_works(query)
            self.on_progress(f"OpenAlex 命中总数：{stats.openalex_total}")
        except OpenAlexBudgetExhausted as exc:
            stats.warnings.append(str(exc))
            self.on_progress(str(exc))
            return []
        except Exception as exc:
            stats.warnings.append(f"OpenAlex 计数失败：{exc}")
        try:
            papers = list(self.openalex.iter_works(query, stop=self.stop))
        except OpenAlexBudgetExhausted as exc:
            stats.warnings.append(str(exc))
            self.on_progress(str(exc))
            return []
        except Exception as exc:
            stats.warnings.append(f"OpenAlex 检索失败：{exc}")
            self.on_progress(f"OpenAlex 检索失败：{exc}")
            return []
        stats.openalex_hits = len(papers)
        self.on_progress(f"OpenAlex 取回 {len(papers)} 篇")
        return papers

    def _run_scholar(self, query: SearchQuery, stats: RunStats) -> list[Paper]:
        self.on_progress("=== Google 学术检索 ===")
        try:
            hits = list(self.scholar.iter_hits(query, stop=self.stop))
        except ScholarUnavailable as exc:
            stats.warnings.append(f"Google 学术不可用：{exc}")
            self.on_progress(f"Google 学术不可用：{exc}")
            return []
        except Exception as exc:
            stats.warnings.append(f"Google 学术检索失败：{exc}")
            self.on_progress(f"Google 学术检索失败：{exc}")
            return []

        stats.scholar_hits = len(hits)
        self.on_progress(f"Google 学术取回 {len(hits)} 条，开始回查规范刊名")
        if not hits:
            return []
        papers = hits_to_papers(
            hits, crossref=self.crossref, openalex=self.openalex,
            on_progress=self.on_progress, stop=self.stop)
        if self.crossref is None:
            stats.warnings.append(
                "关闭了 Crossref，Google 学术的刊名只能靠 OpenAlex 回查，"
                "很容易打爆当日检索额度")
        return papers

    def _attach_metrics(self, journals: list[Journal]) -> None:
        ids: list[str] = []
        for j in journals:
            ids.extend(j.venue_ids or ([j.venue_id] if j.venue_id else []))
        issns = [j.issn_l for j in journals if j.issn_l and not j.venue_ids]
        if not ids and not issns:
            return
        self.on_progress(f"拉取 {len(ids) + len(issns)} 个载体的 OpenAlex 指标")
        try:
            sources = self.openalex.fetch_sources(ids, issns)
        except Exception as exc:
            self.on_progress(f"期刊指标拉取失败：{exc}")
            return
        attach_openalex_metrics(journals, sources)
