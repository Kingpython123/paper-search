"""OpenAlex 数据源。

取代原项目的「selenium 爬百度学术 + requests 抓详情页」两段式流程：
一次 /works 请求（per_page=100）就直接带回论文和它的载体信息，
不需要 chromedriver，也不会被封 IP。

检索范围对应关系：
    title     -> filter=title.search:...               等价于百度学术的 intitle:
    abstract  -> filter=title_and_abstract.search:...   标题+摘要
    fulltext  -> filter=default.search:...             全文，噪声最大
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

import requests

from .cache import Cache
from .models import Paper, SearchQuery
from .normalize import (
    clean_journal_name,
    map_venue_type,
    normalize_doi,
    normalize_title,
    short_openalex_id,
)

API = "https://api.openalex.org"

# 只取需要的字段，减少传输量（官方也推荐用 select）
WORK_FIELDS = ",".join([
    "id", "doi", "title", "publication_year", "cited_by_count",
    "primary_location", "authorships", "type",
])
SOURCE_FIELDS = ",".join([
    "id", "display_name", "issn_l", "issn", "type", "is_oa", "is_in_doaj",
    "host_organization_name", "works_count", "cited_by_count", "apc_usd",
    "summary_stats", "country_code", "homepage_url",
])

_SCOPE_FIELD = {
    "title": "title.search",
    "abstract": "title_and_abstract.search",
    "fulltext": "default.search",
}

# filter 语法里 , 是 AND 分隔符、| 是 OR 分隔符，关键词里出现会破坏查询
_FILTER_RESERVED = str.maketrans({",": " ", "|": " ", ":": " "})

ProgressFn = Callable[[str], None]


class OpenAlexError(RuntimeError):
    pass


class OpenAlexBudgetExhausted(OpenAlexError):
    """当天的检索额度用完了。

    这是硬额度，不是瞬时限流：实测响应头会给出
    Retry-After: 71248（约 20 小时，UTC 零点重置），
    响应体是 "Insufficient budget. This request costs $0.001 but you only
    have $0.0006 remaining"。
    对这种 429 做指数退避重试是纯浪费时间，必须立刻放弃。
    """


# 无 key 时每日预算 $0.1，各类请求的单价差 10~100 倍：
#   search（title.search / ?search=）$0.001  -> 每天约 100 次
#   list（filter=openalex:.. / issn:..）$0.0001 -> 每天约 1000 次
#   singleton（/works/W123）免费
# 所以能用 list 就别用 search，这是省额度的关键。
_BUDGET_MARKERS = ("insufficient budget", "rate limit exceeded")


class OpenAlexClient:
    """带重试、限速和缓存的 OpenAlex 客户端。"""

    def __init__(self, cache: Cache | None = None, mailto: str | None = None,
                 api_key: str | None = None, max_retries: int = 5,
                 min_interval: float = 0.12,
                 on_progress: ProgressFn | None = None) -> None:
        self.cache = cache
        self.api_key = api_key
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.on_progress = on_progress or (lambda msg: None)
        self._last_request = 0.0
        # 一旦确认额度用尽就置位，后续请求直接短路，不再一个个去撞墙
        self.budget_exhausted = False
        self.budget_message = ""
        self.session = requests.Session()
        # OpenAlex 官方建议带上联系方式，能进 polite pool
        ua = "journal-picker/2.0 (https://github.com/nickchen121/cyd-selected-journal)"
        if mailto:
            ua += f" mailto:{mailto}"
        self.session.headers.update({"User-Agent": ua, "Accept": "application/json"})

    # ---------------------------------------------------------------- 底层请求

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict[str, Any],
             kind: str = "list") -> dict[str, Any]:
        # 额度是一个美元池，search 用尽时 list（便宜 10 倍）通常还能跑，
        # 所以只短路 search 类请求，别把还能用的便宜请求一起掐掉。
        if kind == "search" and self.budget_exhausted:
            raise OpenAlexBudgetExhausted(self.budget_message or "OpenAlex 当日检索额度已用尽")
        if self.api_key:
            params = {**params, "api_key": self.api_key}
        url = f"{API}{path}?" + urllib.parse.urlencode(params, safe=':|><,"()')
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=45)
            except requests.RequestException as exc:
                last_err = exc
                time.sleep(min(2 ** attempt, 16))
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 403):
                self._raise_if_budget(resp)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 16)
                self.on_progress(
                    f"OpenAlex 返回 {resp.status_code}，{wait}s 后重试"
                    f"（{attempt + 1}/{self.max_retries}）")
                time.sleep(wait)
                last_err = OpenAlexError(f"HTTP {resp.status_code}")
                continue
            # 其余 4xx 是请求本身的问题，重试没有意义
            raise OpenAlexError(f"OpenAlex HTTP {resp.status_code}: {resp.text[:300]}")
        raise OpenAlexError(f"OpenAlex 请求失败（已重试 {self.max_retries} 次）: {last_err}")

    def _raise_if_budget(self, resp: requests.Response) -> None:
        """区分「当日额度用尽」和「瞬时限流」。

        判据有两个，任一成立即认定是硬额度：
        - 响应体里有 Insufficient budget / Rate limit exceeded
        - Retry-After 大到不可能是瞬时限流（这里取 > 5 分钟）
        """
        body = (resp.text or "")[:500]
        low = body.lower()
        retry_after = 0
        try:
            retry_after = int(resp.headers.get("Retry-After", "0"))
        except ValueError:
            retry_after = 0

        if not (any(m in low for m in _BUDGET_MARKERS) or retry_after > 300):
            return

        remaining = resp.headers.get("X-RateLimit-Remaining-USD", "?")
        limit = resp.headers.get("X-RateLimit-Limit-USD", "?")
        hours = retry_after / 3600 if retry_after else 0
        msg = (f"OpenAlex 当日检索额度已用尽（剩余 ${remaining} / 每日 ${limit}）"
               f"，约 {hours:.1f} 小时后（UTC 零点）重置。")
        if not self.api_key:
            msg += ("免费申请 API key 可把额度提到 10 倍："
                    "https://openalex.org/settings/api ，然后用 --openalex-key 传入。")
        self.budget_exhausted = True
        self.budget_message = msg
        self.on_progress(msg)
        raise OpenAlexBudgetExhausted(msg)

    # ---------------------------------------------------------------- 查询构造

    @staticmethod
    def _quote(term: str) -> str:
        term = term.strip().translate(_FILTER_RESERVED).strip()
        term = " ".join(term.split())
        if not term:
            return ""
        return f'"{term}"' if " " in term else term

    @classmethod
    def build_search_expr(cls, query: SearchQuery) -> str:
        """把关键词拼成 OpenAlex 的检索表达式。

        实测 title.search 支持 AND / OR / 引号短语和小括号分组。
        """
        parts: list[str] = []
        alls = [t for t in (cls._quote(k) for k in query.keywords_all) if t]
        anys = [t for t in (cls._quote(k) for k in query.keywords_any) if t]
        if alls:
            parts.append(" AND ".join(alls))
        if anys:
            parts.append(f"({' OR '.join(anys)})" if len(anys) > 1 else anys[0])
        return " AND ".join(parts)

    def build_filter(self, query: SearchQuery) -> str:
        expr = self.build_search_expr(query)
        if not expr:
            raise ValueError("检索词为空")
        field = _SCOPE_FIELD.get(query.scope, "title.search")
        filters = [f"{field}:{expr}"]

        if query.year_from and query.year_to:
            filters.append(f"publication_year:{query.year_from}-{query.year_to}")
        elif query.year_from:
            filters.append(f"publication_year:>{query.year_from - 1}")
        elif query.year_to:
            filters.append(f"publication_year:<{query.year_to + 1}")

        # 只保留期刊和会议，把 arXiv / Zenodo / Research Square 这类预印本库挡掉。
        # 原项目没做这层过滤，聚合结果里 arXiv 会以绝对数量霸榜。
        types = [t for t in query.include_types if t in ("journal", "conference")]
        if len(types) == 1:
            filters.append(f"primary_location.source.type:{types[0]}")
        elif types:
            filters.append("primary_location.source.type:journal|conference")
        return ",".join(filters)

    # ---------------------------------------------------------------- 论文检索

    def count_works(self, query: SearchQuery) -> int:
        data = self._get("/works", {
            "filter": self.build_filter(query), "per_page": 1, "select": "id"},
            kind="search")
        return int(data.get("meta", {}).get("count") or 0)

    def iter_works(self, query: SearchQuery,
                   stop: Callable[[], bool] | None = None) -> Iterator[Paper]:
        """按需分页拉取论文。stop() 返回 True 时提前结束（供 GUI 中断）。"""
        filt = self.build_filter(query)
        per_page = min(100, max(1, query.max_papers))
        fetched = 0
        page = 1
        while fetched < query.max_papers:
            if stop and stop():
                return
            data = self._get("/works", {
                "filter": filt,
                "per_page": min(per_page, query.max_papers - fetched),
                "page": page,
                "select": WORK_FIELDS,
                "sort": "cited_by_count:desc",
            }, kind="search")
            results = data.get("results") or []
            if not results:
                return
            total = data.get("meta", {}).get("count")
            self.on_progress(
                f"OpenAlex 第 {page} 页：本页 {len(results)} 篇 / 命中总数 {total}")
            for raw in results:
                paper = self.parse_work(raw)
                if paper is not None:
                    yield paper
            fetched += len(results)
            page += 1
            if page * per_page > 10000:  # 基础分页上限
                return

    @staticmethod
    def parse_work(raw: dict[str, Any]) -> Paper | None:
        title = clean_journal_name(raw.get("title") or "")
        if not title:
            return None
        loc = raw.get("primary_location") or {}
        src = loc.get("source") or {}
        venue_name = clean_journal_name(src.get("display_name") or "")
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (raw.get("authorships") or [])
        ]
        return Paper(
            title=title,
            norm_title=normalize_title(title),
            doi=normalize_doi(raw.get("doi")),
            year=raw.get("publication_year"),
            cited_by=raw.get("cited_by_count"),
            url=loc.get("landing_page_url") or raw.get("doi") or raw.get("id"),
            authors=[a for a in authors if a][:8],
            venue_name=venue_name or None,
            venue_id=short_openalex_id(src.get("id")),
            venue_issn_l=src.get("issn_l"),
            venue_type=map_venue_type(src.get("type"), venue_name) if src else None,
            providers={"openalex"},
            work_id=short_openalex_id(raw.get("id")),
        )

    # ------------------------------------------------- 标题回查（供 Google 学术使用）

    def resolve_by_title(self, title: str) -> Paper | None:
        """按标题在 OpenAlex 里找对应的 work。

        Google 学术的来源行（div.gs_a）里刊名是截断的，
        例如「…on Pattern Analysis …」，没法直接用；
        所以拿标题回查 OpenAlex，换取规范刊名 / ISSN / 类型 / 指标。
        """
        key = normalize_title(title)
        if not key:
            return None
        if self.cache:
            hit = self.cache.get("oa_title", key)
            if hit is not None:
                return Paper(**_paper_from_cache(hit)) if hit else None

        # 注意这是 search 类请求（$0.001），比 list 贵 10 倍。
        # 逐篇回查很容易打爆当日额度，所以 Scholar 那一路默认先走 Crossref，
        # 这里只作为 Crossref 未命中时的补充。
        expr = self._quote(title)
        try:
            data = self._get("/works", {
                "filter": f"title.search:{expr}",
                "per_page": 3,
                "select": WORK_FIELDS,
            }, kind="search")
        except OpenAlexBudgetExhausted:
            raise
        except OpenAlexError:
            return None

        best: Paper | None = None
        for raw in data.get("results") or []:
            cand = self.parse_work(raw)
            if cand is None:
                continue
            # 只接受标题高度相似的结果，避免张冠李戴
            if title_similar(key, cand.norm_title):
                best = cand
                break
        if self.cache:
            self.cache.set("oa_title", key, _paper_to_cache(best) if best else {})
        return best

    # ---------------------------------------------------------------- 期刊元数据

    def fetch_sources(self, source_ids: list[str],
                      issns: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """批量取期刊元数据，返回 {source_id: source_dict}。

        OpenAlex 的 filter 单字段最多 100 个 OR 值，
        所以按 100 一批打包，几百个期刊也就几次请求。
        """
        out: dict[str, dict[str, Any]] = {}
        ids = [i for i in dict.fromkeys(source_ids) if i]
        if self.cache and ids:
            cached = self.cache.get_many("oa_source", ids)
            out.update({k: v for k, v in cached.items() if v})
            ids = [i for i in ids if i not in cached]

        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            self.on_progress(f"拉取期刊指标 {i + 1}-{i + len(chunk)} / {len(ids)}")
            data = self._get("/sources", {
                "filter": "openalex:" + "|".join(chunk),
                "per_page": 100,
                "select": SOURCE_FIELDS,
            })
            got = set()
            for raw in data.get("results") or []:
                sid = short_openalex_id(raw.get("id"))
                if sid:
                    out[sid] = raw
                    got.add(sid)
                    if self.cache:
                        self.cache.set("oa_source", sid, raw)
            # 请求成功但查不到的 id 写空缓存，避免下次重复问
            if self.cache:
                for sid in chunk:
                    if sid not in got:
                        self.cache.set("oa_source", sid, {})

        # 只有 ISSN 没有 OpenAlex id 的兜底路径
        if issns:
            pending = [s for s in dict.fromkeys(issns) if s]
            for i in range(0, len(pending), 100):
                chunk = pending[i:i + 100]
                data = self._get("/sources", {
                    "filter": "issn:" + "|".join(chunk),
                    "per_page": 100,
                    "select": SOURCE_FIELDS,
                })
                for raw in data.get("results") or []:
                    sid = short_openalex_id(raw.get("id"))
                    if sid and sid not in out:
                        out[sid] = raw
                        if self.cache:
                            self.cache.set("oa_source", sid, raw)
        return out


def title_similar(a: str, b: str) -> bool:
    """判断两个规范化标题是否指同一篇。

    用词集合的 Jaccard 相似度：比字符串相等宽松（容忍副标题差异），
    又比子串匹配严格（不会把短标题误配到长标题上）。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    inter = len(wa & wb)
    if inter / len(wa | wb) >= 0.72:
        return True
    # 一方基本是另一方的子集（Scholar 常把副标题截掉），且重合词足够多
    shorter = min(len(wa), len(wb))
    return shorter >= 4 and inter / shorter >= 0.9


def _paper_to_cache(p: Paper) -> dict[str, Any]:
    d = p.to_dict()
    d["providers"] = sorted(p.providers)
    return d


def _paper_from_cache(d: dict[str, Any]) -> dict[str, Any]:
    d = dict(d)
    d["providers"] = set(d.get("providers") or [])
    return d
