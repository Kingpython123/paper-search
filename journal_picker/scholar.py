"""Google 学术数据源。

谷歌学术没有官方 API，而且反爬很凶，所以这里做成三个可切换的后端：

    http      直接请求 scholar.google.com（默认，能直连时最省事）
    serpapi   走 SerpApi 的 Google Scholar 接口（要 API key，但稳定不封）
    selenium  打开可见的 Chrome，遇到验证码人工点一下再继续（最后兜底）

拿到的条目只有标题、链接、引用数，来源行（div.gs_a）里的刊名是截断的
（形如「…on Pattern Analysis …」），直接用会把同一个期刊拆成一堆碎片。
所以刊名一律由 OpenAlexClient.resolve_by_title 回查得到，
谷歌学术只负责「发现论文」，规范化交给 OpenAlex。

检索范围：
    title  -> allintitle: 前缀（实测 as_occt=title 并不真正限定标题，不能用）
    其它   -> 普通全文检索
"""

from __future__ import annotations

import random
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests

from .cache import Cache
from .models import Paper, SearchQuery
from .normalize import normalize_title, unescape

SCHOLAR_BASE = "https://scholar.google.com/scholar"
SERPAPI_BASE = "https://serpapi.com/search"
RESULTS_PER_PAGE = 10

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
]

_BLOCK_MARKERS = ("/sorry/", "captcha", "unusual traffic", "not a robot",
                  "enablejs", "我们的系统检测到")

_RESULT_BLOCK = re.compile(
    r'<div class="gs_r gs_or gs_scl"[^>]*>(.*?)'
    r'(?=<div class="gs_r gs_or gs_scl"|<div id="gs_res_ccl_bot)', re.S)
_TITLE_BLOCK = re.compile(r'<h3 class="gs_rt"[^>]*>(.*?)</h3>', re.S)
_META_BLOCK = re.compile(r'<div class="gs_a">(.*?)</div>', re.S)
_HREF = re.compile(r'<a[^>]*href="([^"]+)"')
_CITED = re.compile(r'>(?:Cited by|被引用次数：)\s*(\d+)<')
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_TAG = re.compile(r"<[^>]+>")

ProgressFn = Callable[[str], None]


class ScholarBlocked(RuntimeError):
    """谷歌学术要求验证码或直接拒绝服务。"""


class ScholarUnavailable(RuntimeError):
    """网络层面根本连不上（例如本地无法访问 Google）。"""


@dataclass
class ScholarHit:
    """谷歌学术搜索结果的一条原始记录。"""

    title: str
    url: str | None = None
    cited_by: int | None = None
    year: int | None = None
    # div.gs_a 原文，刊名是截断的，仅作参考/兜底
    meta_line: str | None = None

    @property
    def venue_hint(self) -> str | None:
        """从 gs_a 里粗取来源片段：「作者 - 来源, 年份 - 站点」的中间段。"""
        if not self.meta_line:
            return None
        parts = self.meta_line.split(" - ")
        if len(parts) < 2:
            return None
        mid = parts[1]
        mid = re.sub(r",?\s*(19|20)\d{2}\s*$", "", mid).strip(" ,")
        # 谷歌用省略号表示截断，留着反而误导
        return mid.replace("\u2026", "").strip(" ,-") or None


def _strip_tags(text: str) -> str:
    return unescape(_TAG.sub("", text or "")).strip()


class ScholarClient:
    """谷歌学术检索客户端。"""

    def __init__(self, backend: str = "http", cache: Cache | None = None,
                 serpapi_key: str | None = None,
                 base_url: str = SCHOLAR_BASE,
                 lang: str = "en",
                 min_delay: float = 5.0, max_delay: float = 12.0,
                 on_progress: ProgressFn | None = None) -> None:
        self.backend = backend
        self.cache = cache
        self.serpapi_key = serpapi_key
        self.base_url = base_url
        self.lang = lang
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.on_progress = on_progress or (lambda msg: None)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(_UA_POOL),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        })
        self._driver = None  # selenium 后端惰性创建

    # ---------------------------------------------------------------- 查询构造

    @staticmethod
    def build_query(query: SearchQuery) -> str:
        """拼谷歌学术的检索串。

        谷歌学术默认多个词之间是 AND，OR 需要大写，短语要加引号。
        """
        parts: list[str] = []
        for kw in query.keywords_all:
            kw = kw.strip()
            if kw:
                parts.append(f'"{kw}"' if " " in kw else kw)
        anys = [k.strip() for k in query.keywords_any if k.strip()]
        if anys:
            quoted = [f'"{k}"' if " " in k else k for k in anys]
            parts.append(f"({' OR '.join(quoted)})" if len(quoted) > 1 else quoted[0])
        expr = " ".join(parts)
        # allintitle: 才真正限定在标题内；as_occt=title 实测无效
        if query.scope == "title" and expr:
            expr = f"allintitle: {expr}"
        return expr

    def _page_url(self, expr: str, query: SearchQuery, start: int) -> str:
        params: dict[str, Any] = {"q": expr, "hl": self.lang, "start": start}
        if query.year_from:
            params["as_ylo"] = query.year_from
        if query.year_to:
            params["as_yhi"] = query.year_to
        # 排除引文和专利，它们没有可用的期刊信息
        params["as_vis"] = 1
        params["as_sdt"] = "0,5"
        return f"{self.base_url}?" + urllib.parse.urlencode(params)

    # ---------------------------------------------------------------- 抓取入口

    def iter_hits(self, query: SearchQuery,
                  stop: Callable[[], bool] | None = None) -> Iterator[ScholarHit]:
        """逐页产出结果。谷歌学术每页固定 10 条。"""
        expr = self.build_query(query)
        if not expr:
            raise ValueError("检索词为空")
        self.on_progress(f"Google 学术检索式：{expr}")

        wanted = query.max_papers
        seen: set[str] = set()
        produced = 0
        start = 0
        empty_pages = 0

        while produced < wanted:
            if stop and stop():
                return
            page_no = start // RESULTS_PER_PAGE + 1
            self.on_progress(f"Google 学术第 {page_no} 页（start={start}）")
            try:
                html = self._fetch_page(expr, query, start)
            except ScholarBlocked:
                # 被拦就停下，已经拿到的结果照常返回，不把整个流程带崩
                self.on_progress("Google 学术触发反爬拦截，停止翻页并保留已获取结果")
                return

            hits = self.parse_page(html)
            if not hits:
                empty_pages += 1
                # 连续两页空基本就是到底了或结构变了
                if empty_pages >= 2:
                    return
            else:
                empty_pages = 0

            for hit in hits:
                key = normalize_title(hit.title)
                if not key or key in seen:
                    continue
                seen.add(key)
                yield hit
                produced += 1
                if produced >= wanted:
                    return

            start += RESULTS_PER_PAGE
            if start >= 1000:  # 谷歌学术本身翻不过 1000 条
                return
            self._sleep()

    def _sleep(self) -> None:
        delay = random.uniform(self.min_delay, self.max_delay)
        self.on_progress(f"等待 {delay:.1f}s 再翻页（降低被封概率）")
        time.sleep(delay)

    def _fetch_page(self, expr: str, query: SearchQuery, start: int) -> str:
        ck = (f"{self.backend}|{self.base_url}|{expr}"
              f"|{query.year_from}|{query.year_to}|{start}")
        if self.cache:
            hit = self.cache.get("scholar_page", ck)
            if hit:
                self.on_progress("命中缓存，跳过请求")
                return hit

        if self.backend == "serpapi":
            html = self._fetch_serpapi(expr, query, start)
        elif self.backend == "selenium":
            html = self._fetch_selenium(expr, query, start)
        else:
            html = self._fetch_http(expr, query, start)

        if self.cache:
            self.cache.set("scholar_page", ck, html)
        return html

    # ---------------------------------------------------------------- 后端实现

    def _fetch_http(self, expr: str, query: SearchQuery, start: int) -> str:
        url = self._page_url(expr, query, start)
        try:
            resp = self.session.get(url, timeout=30)
        except requests.RequestException as exc:
            raise ScholarUnavailable(
                f"无法访问 Google 学术：{type(exc).__name__}。"
                f"如果本地网络访问不了 Google，请改用 --scholar-backend serpapi 或 selenium"
            ) from exc
        body = resp.text
        if resp.status_code != 200 or _looks_blocked(body):
            raise ScholarBlocked(f"HTTP {resp.status_code}，疑似验证码/限流")
        return body

    def _fetch_serpapi(self, expr: str, query: SearchQuery, start: int) -> str:
        if not self.serpapi_key:
            raise ValueError("serpapi 后端需要提供 API key")
        params: dict[str, Any] = {
            "engine": "google_scholar", "q": expr, "start": start,
            "hl": self.lang, "api_key": self.serpapi_key,
        }
        if query.year_from:
            params["as_ylo"] = query.year_from
        if query.year_to:
            params["as_yhi"] = query.year_to
        resp = self.session.get(SERPAPI_BASE, params=params, timeout=60)
        if resp.status_code != 200:
            raise ScholarBlocked(f"SerpApi HTTP {resp.status_code}: {resp.text[:200]}")
        # 统一成「JSON 文本」交给 parse_page，由它按内容判断格式
        return resp.text

    def _fetch_selenium(self, expr: str, query: SearchQuery, start: int) -> str:
        driver = self._ensure_driver()
        url = self._page_url(expr, query, start)
        driver.get(url)
        # Selenium 4 自带 Selenium Manager，会自动下载匹配的 driver，
        # 不再需要原项目那样手填 chromedriver 路径
        for _ in range(60):
            html = driver.page_source
            if '<div class="gs_r gs_or gs_scl"' in html:
                return html
            if _looks_blocked(html):
                self.on_progress("浏览器里出现验证码，请手动完成后程序会自动继续（最多等 60 秒）")
            time.sleep(1)
        return driver.page_source

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise ScholarUnavailable("selenium 后端需要先 pip install selenium") from exc
        opts = Options()
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        self._driver = webdriver.Chrome(options=opts)
        self._driver.implicitly_wait(3)
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    # ---------------------------------------------------------------- 页面解析

    @classmethod
    def parse_page(cls, payload: str) -> list[ScholarHit]:
        """解析一页结果。自动识别是 HTML 还是 SerpApi 的 JSON。"""
        text = (payload or "").lstrip()
        if text.startswith("{"):
            return cls._parse_serpapi_json(text)
        return cls._parse_html(text)

    @staticmethod
    def _parse_html(html: str) -> list[ScholarHit]:
        hits: list[ScholarHit] = []
        for block in _RESULT_BLOCK.findall(html):
            tm = _TITLE_BLOCK.search(block)
            if not tm:
                continue
            title_html = tm.group(1)
            title = _strip_tags(title_html)
            # 去掉 [PDF] [HTML] [图书] 这类前缀标记。
            # 谷歌学术有时会连着给两个（[HTML][HTML] 标题），所以要循环剥。
            while True:
                stripped = re.sub(r"^\[[^\]]{1,12}\]\s*", "", title).strip()
                if stripped == title:
                    break
                title = stripped
            if not title:
                continue
            href = _HREF.search(title_html)
            meta = _META_BLOCK.search(block)
            meta_line = _strip_tags(meta.group(1)) if meta else None
            cited = _CITED.search(block)
            year = None
            if meta_line:
                ym = _YEAR.search(meta_line)
                if ym:
                    year = int(ym.group(0))
            hits.append(ScholarHit(
                title=title,
                url=href.group(1) if href else None,
                cited_by=int(cited.group(1)) if cited else None,
                year=year,
                meta_line=meta_line,
            ))
        return hits

    @staticmethod
    def _parse_serpapi_json(text: str) -> list[ScholarHit]:
        import json
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        hits: list[ScholarHit] = []
        for item in data.get("organic_results") or []:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            info = item.get("publication_info") or {}
            summary = info.get("summary")
            cited = ((item.get("inline_links") or {}).get("cited_by") or {}).get("total")
            year = None
            if summary:
                ym = _YEAR.search(summary)
                if ym:
                    year = int(ym.group(0))
            hits.append(ScholarHit(title=title, url=item.get("link"),
                                   cited_by=cited, year=year, meta_line=summary))
        return hits


def _looks_blocked(html: str) -> bool:
    low = (html or "").lower()
    if any(m in low for m in _BLOCK_MARKERS):
        # 结果页里如果同时有正常结果块，说明只是页面里带了这些词
        return '<div class="gs_r gs_or gs_scl"' not in html
    return False


def hits_to_papers(hits: list[ScholarHit],
                   crossref: Any = None,
                   openalex: Any = None,
                   on_progress: ProgressFn | None = None,
                   stop: Callable[[], bool] | None = None) -> list[Paper]:
    """把谷歌学术条目转成带规范刊名的 Paper。

    解析顺序是刻意安排的：

    1. Crossref —— 免费、没有每日额度，实测 30 条里能认出 27 条。
    2. OpenAlex —— 只在 Crossref 未命中时补刀。它的 title.search 属于
       search 类请求（$0.001/次，无 key 时每天只有约 100 次），
       逐篇回查一百多篇会直接把额度打爆；额度一旦用尽就自动停用这一路。

    两边都认不出来的条目仍然保留，venue_name 用 gs_a 的截断片段兜底，
    在报告里落到「未识别载体」一档，而不是被悄悄丢掉。
    """
    log = on_progress or (lambda m: None)
    papers: list[Paper] = []
    total = len(hits)
    by_crossref = 0
    by_openalex = 0
    oa_disabled = False

    for i, hit in enumerate(hits, 1):
        if stop and stop():
            break

        matched: Paper | None = None
        if crossref is not None:
            try:
                matched = crossref.resolve_by_title(hit.title)
            except Exception:
                matched = None
            if matched is not None:
                by_crossref += 1

        if (matched is None or not matched.venue_name) and openalex is not None \
                and not oa_disabled and not getattr(openalex, "budget_exhausted", False):
            try:
                oa_hit = openalex.resolve_by_title(hit.title)
            except Exception as exc:
                # 额度用尽是硬限制，退避重试只会白等，直接停掉这一路
                oa_disabled = True
                log(f"OpenAlex 标题回查已停用：{exc}")
                oa_hit = None
            if oa_hit is not None:
                if matched is None:
                    matched = oa_hit
                else:
                    matched.merge(oa_hit)
                by_openalex += 1

        if matched is not None:
            matched.providers = {"scholar", *matched.providers}
            # 谷歌学术的引用数通常更高（覆盖面更广），取大值
            if hit.cited_by is not None and (matched.cited_by is None
                                             or hit.cited_by > matched.cited_by):
                matched.cited_by = hit.cited_by
            if not matched.url and hit.url:
                matched.url = hit.url
            if matched.year is None:
                matched.year = hit.year
            papers.append(matched)
        else:
            papers.append(Paper(
                title=hit.title,
                norm_title=normalize_title(hit.title),
                year=hit.year,
                cited_by=hit.cited_by,
                url=hit.url,
                venue_name=hit.venue_hint,
                venue_type=None,
                providers={"scholar"},
            ))

        if i % 10 == 0 or i == total:
            log(f"标题回查载体：{i}/{total}"
                f"（Crossref {by_crossref} + OpenAlex {by_openalex}）")
    return papers
