"""letpub 期刊信息富化（可选）。

补的是 OpenAlex 没有、但国内投稿最看重的几项：
中科院分区、JCR 影响因子、审稿周期、录用难度、SCI 收录情况。

和原项目的差别：
1. 原代码 `re.findall('</style>.*?<tr>(.*?)</tr>', ...)[0]` 取的是第一行，
   现在这一行是广告行，取到的全是空值；这里改成定位含 journalid 详情链接的行。
2. 原代码按单元格位置取值，且靠「月/周/eeks」这种关键字猜列，
   页面一改版就错位；这里改成按标签（IF:、CiteScore:、N区）取值。
3. 原代码不校验返回的期刊是不是要查的那本，模糊匹配到别的刊也照抄；
   这里做刊名相似度校验，不匹配就返回 None，宁缺勿错。
4. 加了缓存和限速，同一本刊只问一次。

letpub 是第三方网站，随时可能改版或限流，所以这个模块整体是 best-effort：
拿不到就留空，绝不影响主流程。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import requests

from .cache import Cache
from .models import Journal
from .normalize import (
    clean_journal_name,
    contains_chinese,
    normalize_for_match,
    unescape,
)

SEARCH_URL = "https://www.letpub.com.cn/index.php?page=journalapp&view=search"
DETAIL_URL = "https://www.letpub.com.cn/index.php?journalid={jid}&page=journalapp&view=detail"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_JOURNAL_LINK = re.compile(
    r'href="[^"]*journalid=(\d+)[^"]*view=detail"[^>]*>(.*?)</a>', re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_ISSN = re.compile(r"\b(\d{4}-\d{3}[\dXx])\b")
_IF = re.compile(r"IF\s*[:：]\s*([\d.]+)")
_HINDEX = re.compile(r"h-index\s*[:：]\s*(\d+)")
_CITESCORE = re.compile(r"CiteScore\s*[:：]\s*([\d.]+)")
_QUARTILE = re.compile(r"([1-4])\s*区")
_MONTHS = re.compile(r"约?\s*([\d.]+)\s*个?月")
_WEEKS = re.compile(r"约?\s*([\d.]+)\s*(?:周|weeks?)", re.I)
# 学科分类必须在「带标签的原始 HTML」上匹配：原文是
# 大类：计算机科学<br><br>小类：计算机：人工智能
# 用 [^<]+ 正好被 <br> 截断；若先去标签再匹配会贪婪吞掉整行剩余内容。
_BIG_CAT = re.compile(r"大类\s*[:：]\s*([^<\n]{1,40})")
_SMALL_CAT = re.compile(r"小类\s*[:：]\s*([^<\n]{1,40})")
_SCI = re.compile(r"\b(SCIE?|SSCI|AHCI|ESCI)\b")
# 录用难度这一列可能是难度词，也可能是百分比（80%、约10.62%）
_ACCEPT_WORD = re.compile(r"(很难|较难|较易|容易|尚可|未知)")
_ACCEPT_PCT = re.compile(r"约?\s*(\d+(?:\.\d+)?%)")

# 正常结果页一定带这个锚点；限流时 letpub 会返回 200 + 几百字节的空壳页，
# 不认这种情况会把「被限流」误判成「查不到」，还顺手写进负缓存冻结一个月。
_RESULT_TABLE_MARK = "journallisttable"

ProgressFn = Callable[[str], None]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html or ""))).strip()


class LetpubClient:
    """letpub 检索客户端（best-effort）。"""

    def __init__(self, cache: Cache | None = None, min_interval: float = 2.0,
                 max_retries: int = 4, timeout: int = 25,
                 on_progress: ProgressFn | None = None) -> None:
        self.cache = cache
        self.blocked_count = 0
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.on_progress = on_progress or (lambda msg: None)
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _UA,
            "Referer": "https://www.letpub.com.cn/index.php?page=journalapp",
        })

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()

    # ------------------------------------------------------------------ 查询

    def lookup(self, journal_name: str, issn: str | None = None) -> dict[str, Any] | None:
        """查一本期刊。返回字段字典，查不到或不匹配返回 None。

        ISSN 走 letpub 专用的 searchissn 字段（把 ISSN 塞进 searchname 是查不到的，
        原项目就是这么干的，等于白发一次请求）。
        """
        name = clean_journal_name(journal_name)
        if not name and not issn:
            return None
        ck = normalize_for_match(name) or (issn or "")
        if self.cache:
            hit = self.cache.get("letpub", ck)
            if hit is not None:
                return hit or None

        info: dict[str, Any] | None = None
        throttled = False
        attempts: list[dict[str, str]] = []
        if issn:
            attempts.append({"searchissn": issn})
        if name:
            attempts.append({"searchname": name})

        for payload in attempts:
            html, blocked = self._search(payload)
            throttled = throttled or blocked
            if not html:
                continue
            info = self._pick_row(html, name, issn)
            if info:
                break

        # 被限流时不写负缓存，否则这本刊会「查不到」整整一个 TTL
        if self.cache and not (info is None and throttled):
            self.cache.set("letpub", ck, info or {})
        return info

    def _search(self, fields: dict[str, str]) -> tuple[str | None, bool]:
        """发一次检索。返回 (html, 是否被限流)。"""
        payload = {"searchname": "", "searchissn": "", "searchsort": "relevance"}
        payload.update(fields)
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.post(SEARCH_URL, data=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                self.on_progress(f"letpub 请求失败（{type(exc).__name__}），退避重试")
                time.sleep(min(3 * 2 ** attempt, 20))
                continue
            if resp.status_code != 200:
                self.on_progress(f"letpub 返回 {resp.status_code}，退避重试")
                time.sleep(min(3 * 2 ** attempt, 20))
                continue
            # 页面是 UTF-8，但服务端不一定在响应头里声明
            resp.encoding = "utf-8"
            body = resp.text
            if _RESULT_TABLE_MARK not in body:
                # 典型限流响应：HTTP 200 但只有几百字节的空壳
                self.blocked_count += 1
                wait = min(5 * 2 ** attempt, 30)
                self.on_progress(
                    f"letpub 疑似限流（响应仅 {len(body)} 字节），等待 {wait}s 后重试")
                time.sleep(wait)
                continue
            return body, False
        return None, True

    # ------------------------------------------------------------------ 解析

    @classmethod
    def _pick_row(cls, html: str, want_name: str,
                  want_issn: str | None) -> dict[str, Any] | None:
        """在结果表里挑出确实对应目标期刊的那一行。"""
        want_norm = normalize_for_match(want_name)
        best: tuple[float, dict[str, Any]] | None = None

        for row in _ROW.findall(html):
            link = _JOURNAL_LINK.search(row)
            if not link:
                continue  # 广告行、表头行都没有详情链接
            jid, name_html = link.group(1), link.group(2)
            row_name = clean_journal_name(_text(name_html))
            if not row_name:
                continue
            row_text = _text(row)
            row_issn = _ISSN.search(row_text)
            row_issn = row_issn.group(1) if row_issn else None

            score = cls._match_score(want_norm, row_name, want_issn, row_issn)
            if score <= 0:
                continue
            parsed = cls._parse_row(jid, row_name, row, row_text, row_issn)
            if best is None or score > best[0]:
                best = (score, parsed)
        return best[1] if best else None

    @staticmethod
    def _match_score(want_norm: str, row_name: str,
                     want_issn: str | None, row_issn: str | None) -> float:
        """给候选行打分。ISSN 相同直接判定为同一本刊。"""
        if want_issn and row_issn and want_issn.lower() == row_issn.lower():
            return 10.0
        rn = normalize_for_match(row_name)
        if not rn or not want_norm:
            return 0.0
        if rn == want_norm:
            return 5.0
        wa, wb = set(want_norm.split()), set(rn.split())
        if not wa or not wb:
            return 0.0
        jac = len(wa & wb) / len(wa | wb)
        # 低于 0.8 认为不是同一本刊，宁缺勿错
        return jac if jac >= 0.8 else 0.0

    @staticmethod
    def _parse_row(jid: str, row_name: str, row_html: str, row_text: str,
                   row_issn: str | None) -> dict[str, Any]:
        """按标签而非列位置提取字段。"""
        quart = _QUARTILE.search(row_text)
        if_m = _IF.search(row_text)
        cs_m = _CITESCORE.search(row_text)
        h_m = _HINDEX.search(row_text)

        review = None
        months = _MONTHS.search(row_text)
        weeks = _WEEKS.search(row_text)
        if months:
            review = f"约 {months.group(1)} 个月"
        elif weeks:
            review = f"约 {weeks.group(1)} 周"

        big = _BIG_CAT.search(row_html)
        small = _SMALL_CAT.search(row_html)
        cats = []
        if big:
            cats.append("大类：" + big.group(1).strip())
        if small:
            cats.append("小类：" + small.group(1).strip())

        sci = sorted({m.group(1).upper() for m in _SCI.finditer(row_text)})
        accept_m = _ACCEPT_WORD.search(row_text) or _ACCEPT_PCT.search(row_text)

        # letpub 对没有影响因子的刊（新刊、非 SCI）会显示 IF: 0，
        # 照抄成 0.0 会让人以为「这刊 IF 真的是 0」，还会把排序带乱。
        letpub_if = float(if_m.group(1)) if if_m else None
        if letpub_if == 0:
            letpub_if = None
        letpub_cs = float(cs_m.group(1)) if cs_m else None
        if letpub_cs == 0:
            letpub_cs = None

        return {
            "letpub_name": row_name,
            "issn": row_issn,
            "cas_quartile": int(quart.group(1)) if quart else None,
            "letpub_if": letpub_if,
            "letpub_citescore": letpub_cs,
            "letpub_h_index": int(h_m.group(1)) if h_m else None,
            "review_period": review,
            "acceptance": accept_m.group(1) if accept_m else None,
            "sci_index": "/".join(sci) or None,
            "cas_category": " ".join(cats) or None,
            "letpub_url": DETAIL_URL.format(jid=jid),
        }

    # ------------------------------------------------------------------ 批量富化

    def enrich(self, journals: list[Journal],
               only_english: bool = True,
               stop: Callable[[], bool] | None = None) -> int:
        """给聚合后的期刊列表补 letpub 字段，返回成功条数。

        默认只查英文期刊：letpub 收录的是 SCI 期刊，
        查中文期刊和会议基本是白跑一趟还拖慢速度。
        """
        # 用「刊名是否含汉字」而不是 Journal.is_chinese 来判断能不能查 letpub：
        # letpub 是按英文刊名索引的，国内出版但用英文刊名的 SCI 期刊
        # （Chinese Journal of Lasers 这类）它是收录的，不该跳过。
        targets = [
            j for j in journals
            if j.venue_type == "journal"
            and not (only_english and contains_chinese(j.name))
        ]
        if not targets:
            return 0
        self.on_progress(f"letpub 富化：{len(targets)} 本期刊待查")
        done = 0
        for i, journal in enumerate(targets, 1):
            if stop and stop():
                break
            try:
                info = self.lookup(journal.name, journal.issn_l)
            except Exception as exc:  # letpub 是外部依赖，不能让它带崩主流程
                self.on_progress(f"letpub 查询 {journal.name[:30]} 出错：{exc}")
                info = None
            if info:
                m = journal.metrics
                m.cas_quartile = info.get("cas_quartile")
                m.letpub_if = info.get("letpub_if")
                m.letpub_citescore = info.get("letpub_citescore")
                m.review_period = info.get("review_period")
                m.acceptance = info.get("acceptance")
                m.sci_index = info.get("sci_index")
                m.cas_category = info.get("cas_category")
                m.letpub_url = info.get("letpub_url")
                done += 1
            if i % 5 == 0 or i == len(targets):
                self.on_progress(f"letpub 富化：{i}/{len(targets)}，命中 {done}")
        return done
