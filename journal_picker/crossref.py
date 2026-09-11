"""Crossref 兜底。

OpenAlex 有一部分记录（尤其是 IEEE 系的会议论文）确实缺 primary_location.source，
实测 Vid2Seq、Streaming Dense Video Captioning 这类 CVPR 论文就查不到载体。
这种情况下拿 DOI 去 Crossref 问 container-title，能补回完整的会议/期刊名。

只在「已知论文但不知道载体」时才调用，所以请求量很小。
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import requests

from .cache import Cache
from .models import Paper
from .normalize import (
    clean_journal_name,
    map_venue_type,
    normalize_doi,
    normalize_title,
)

API = "https://api.crossref.org"

# Crossref 的 type 到内部 venue 类型
_TYPE_MAP = {
    "journal-article": "journal",
    "proceedings-article": "conference",
    "proceedings": "conference",
    "book-chapter": "book",
    "book": "book",
    "monograph": "book",
    "posted-content": "repository",
}

ProgressFn = Callable[[str], None]


class CrossrefClient:
    def __init__(self, cache: Cache | None = None, mailto: str | None = None,
                 min_interval: float = 0.15, max_retries: int = 3,
                 on_progress: ProgressFn | None = None) -> None:
        self.cache = cache
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.on_progress = on_progress or (lambda msg: None)
        self._last = 0.0
        self.session = requests.Session()
        ua = "journal-picker/2.0"
        if mailto:
            ua += f" (mailto:{mailto})"
        self.session.headers.update({"User-Agent": ua, "Accept": "application/json"})

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    def _get(self, url: str) -> dict[str, Any] | None:
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=30)
            except requests.RequestException:
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    return None
            if resp.status_code == 404:
                return None
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 8))
                continue
            return None
        return None

    # ------------------------------------------------------------------ 查询

    def venue_by_doi(self, doi: str) -> dict[str, Any] | None:
        """按 DOI 取载体信息，返回 {name, type, issn_l}。"""
        if not doi:
            return None
        if self.cache:
            hit = self.cache.get("crossref_doi", doi)
            if hit is not None:
                return hit or None
        data = self._get(f"{API}/works/{urllib.parse.quote(doi, safe='')}")
        info = _extract_venue(data.get("message") if data else None)
        if self.cache:
            self.cache.set("crossref_doi", doi, info or {})
        return info

    def venue_by_title(self, title: str) -> dict[str, Any] | None:
        """没有 DOI 时按标题模糊查，只接受标题高度吻合的结果。"""
        key = normalize_title(title)
        if not key:
            return None
        if self.cache:
            hit = self.cache.get("crossref_title", key)
            if hit is not None:
                return hit or None
        params = urllib.parse.urlencode({
            "query.bibliographic": title, "rows": 3,
            "select": "title,container-title,DOI,type,ISSN,event",
        })
        data = self._get(f"{API}/works?{params}")
        info = None
        for item in ((data or {}).get("message") or {}).get("items") or []:
            cand_title = (item.get("title") or [""])[0]
            if _title_close(key, normalize_title(cand_title)):
                info = _extract_venue(item)
                if info:
                    break
        if self.cache:
            self.cache.set("crossref_title", key, info or {})
        return info

    def resolve_by_title(self, title: str) -> Paper | None:
        """按标题查出一个带载体信息的 Paper。

        给 Google 学术那一路用：Crossref 免费且没有每日额度，
        而 OpenAlex 的 title.search 是 search 类请求（$0.001/次），
        逐篇回查一百多篇就能把免费额度打爆。
        实测 30 条 Scholar 结果里 Crossref 能认出 27 条，漏的是预印本。
        """
        info = self.venue_by_title(title)
        if not info or not info.get("name"):
            return None
        return Paper(
            title=info.get("title") or title,
            norm_title=normalize_title(info.get("title") or title),
            doi=info.get("doi"),
            year=info.get("year"),
            url=f"https://doi.org/{info['doi']}" if info.get("doi") else None,
            venue_name=info["name"],
            venue_issn_l=info.get("issn_l"),
            venue_type=info.get("type"),
            providers={"crossref"},
        )

    # ------------------------------------------------------------------ 富化

    def fill_missing_venues(self, papers: list[Paper],
                            stop: Callable[[], bool] | None = None) -> int:
        """给没有载体信息的论文补上刊名/会议名，返回补全条数。"""
        pending = [p for p in papers if not p.venue_name or not p.venue_type]
        if not pending:
            return 0
        self.on_progress(f"Crossref 兜底补载体：{len(pending)} 篇待补")
        filled = 0
        for i, paper in enumerate(pending, 1):
            if stop and stop():
                break
            info = None
            if paper.doi:
                info = self.venue_by_doi(paper.doi)
            if info is None:
                info = self.venue_by_title(paper.title)
            if info and info.get("name"):
                paper.venue_name = info["name"]
                paper.venue_type = info.get("type") or paper.venue_type
                paper.venue_issn_l = paper.venue_issn_l or info.get("issn_l")
                filled += 1
            if i % 10 == 0 or i == len(pending):
                self.on_progress(f"Crossref 兜底：{i}/{len(pending)}，已补 {filled}")
        return filled


def _extract_venue(msg: dict[str, Any] | None) -> dict[str, Any] | None:
    if not msg:
        return None
    containers = msg.get("container-title") or []
    name = containers[0] if containers else None
    if not name:
        name = (msg.get("event") or {}).get("name")
    if not name:
        return None
    name = clean_journal_name(name)
    cr_type = (msg.get("type") or "").lower()
    vtype = _TYPE_MAP.get(cr_type) or map_venue_type(None, name)
    issns = msg.get("ISSN") or []
    # 顺带带回 DOI 和年份：跨源去重优先按 DOI 对齐，
    # 只有拿到 DOI 才能把 Scholar 的条目和 OpenAlex 的条目认成同一篇。
    year = None
    for field in ("published", "published-print", "published-online", "issued"):
        parts = ((msg.get(field) or {}).get("date-parts") or [[]])[0]
        if parts and isinstance(parts[0], int):
            year = parts[0]
            break
    titles = msg.get("title") or []
    return {
        "name": name,
        "type": vtype,
        "issn_l": issns[0] if issns else None,
        "doi": normalize_doi(msg.get("DOI")),
        "year": year,
        "title": clean_journal_name(titles[0]) if titles else None,
    }


def _title_close(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.8
