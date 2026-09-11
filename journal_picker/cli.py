"""命令行入口：python -m journal_picker.cli ...

举例：

    # 标题里同时含 "video captioning"，2021 年之后，只用 OpenAlex
    python -m journal_picker.cli -a "video captioning" --from 2021

    # 标题含 "video captioning" 且含 transformer 或 attention 之一
    python -m journal_picker.cli -a "video captioning" -o transformer -o attention

    # 同时用 OpenAlex 和 Google 学术（letpub / CCF 匹配默认都是开的）
    python -m journal_picker.cli -a "knowledge graph" -s openalex -s scholar

    # 按 CCF 等级排序（A 类优先），关掉 letpub 加速
    python -m journal_picker.cli -a "graph neural network" --sort ccf --no-letpub

    # 只看 1 区期刊排前面，输出到指定目录
    python -m journal_picker.cli -a "graph neural network" --sort quartile -O ./out
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cache import Cache
from .models import SearchQuery
from .pipeline import Pipeline, PipelineResult, SORT_KEYS
from .report import write_all


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，打印刊名里的特殊字符会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="journal_picker",
        description="按研究方向反查候选投稿期刊：先搜同主题论文，再看这些论文都发在哪",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_argument_group("检索条件")
    g.add_argument("-a", "--all", dest="keywords_all", action="append", default=[],
                   metavar="词",
                   help="必须全部命中的关键词，可重复给多个（AND）")
    g.add_argument("-o", "--any", dest="keywords_any", action="append", default=[],
                   metavar="词",
                   help="命中任意一个即可的关键词，可重复给多个（OR）")
    g.add_argument("--scope", choices=["title", "abstract", "fulltext"],
                   default="title",
                   help="检索范围：title 仅标题（默认，最精准）"
                        "/ abstract 标题+摘要 / fulltext 全文（噪声大）")
    g.add_argument("--from", dest="year_from", type=int, default=None,
                   metavar="年份", help="起始年份，如 2021")
    g.add_argument("--to", dest="year_to", type=int, default=None,
                   metavar="年份", help="截止年份")
    g.add_argument("-n", "--max-papers", type=int, default=200,
                   help="最多分析多少篇论文（默认 200）")
    g.add_argument("--types", default="journal,conference",
                   help="纳入的载体类型，逗号分隔：journal,conference（默认两者都要）")

    s = p.add_argument_group("数据源")
    s.add_argument("-s", "--source", dest="providers", action="append",
                   choices=["openalex", "scholar"], default=[],
                   help="检索源，可重复。默认只用 openalex")
    s.add_argument("--scholar-backend", choices=["http", "serpapi", "selenium"],
                   default="http",
                   help="Google 学术后端：http 直连（默认）/ serpapi 需 key / selenium 可人工过验证码")
    s.add_argument("--serpapi-key", default=None, help="SerpApi 的 API key")
    s.add_argument("--scholar-base", default=None,
                   help="自定义 Google 学术地址（镜像站，多数镜像是 JS 渲染，需配 selenium 后端）")
    s.add_argument("--scholar-delay", default="5,12", metavar="最小,最大",
                   help="Google 学术翻页随机延时秒数（默认 5,12；调小容易被封）")
    s.add_argument("--no-crossref", action="store_true",
                   help="关闭 Crossref 兜底（OpenAlex 缺载体时就不再补了）")
    s.add_argument("--no-letpub", action="store_true",
                   help="关闭 letpub 富化（默认开启，会补分区/IF/审稿周期/录用比例，"
                        "但会变慢且可能被限流）")
    s.add_argument("--no-ccf", action="store_true",
                   help="关闭 CCF 目录匹配（默认开启，本地静态匹配不发请求，基本不影响速度）")
    s.add_argument("--mailto", default=None,
                   help="你的邮箱，会带进 User-Agent，OpenAlex/Crossref 会给更宽松的配额")
    s.add_argument("--openalex-key", default=None,
                   help="OpenAlex API key（可选，免费申请后每日额度是无 key 的 10 倍）")

    o = p.add_argument_group("输出")
    o.add_argument("-O", "--outdir", default="./output", help="输出目录（默认 ./output）")
    o.add_argument("--basename", default=None, help="输出文件名（不含扩展名），默认带时间戳")
    o.add_argument("-f", "--format", dest="formats", action="append",
                   choices=["xlsx", "html", "json", "md"], default=[],
                   help="输出格式，可重复。默认全部")
    o.add_argument("--sort", dest="sort_key", choices=list(SORT_KEYS),
                   default="papers",
                   help="排序方式：" + "；".join(f"{k}={v}" for k, v in SORT_KEYS.items()))
    o.add_argument("--top", type=int, default=20, help="终端里打印前几名（默认 20）")

    c = p.add_argument_group("缓存")
    c.add_argument("--cache", default=None,
                   help="缓存文件路径（默认 ~/.journal_picker/cache.sqlite3）")
    c.add_argument("--no-cache", action="store_true", help="禁用缓存")
    c.add_argument("--cache-ttl", type=int, default=30 * 24 * 3600,
                   help="缓存有效期秒数（默认 30 天）")
    c.add_argument("-q", "--quiet", action="store_true", help="不打印过程日志")
    return p


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = build_parser().parse_args(argv)

    if not args.keywords_all and not args.keywords_any:
        print("至少要给一个检索词：-a \"关键词\" 或 -o \"关键词\"", file=sys.stderr)
        return 2

    try:
        lo, hi = (float(x) for x in args.scholar_delay.split(","))
    except ValueError:
        print("--scholar-delay 格式应为「最小,最大」，例如 5,12", file=sys.stderr)
        return 2

    query = SearchQuery(
        keywords_all=args.keywords_all,
        keywords_any=args.keywords_any,
        scope=args.scope,
        year_from=args.year_from,
        year_to=args.year_to,
        max_papers=args.max_papers,
        providers=args.providers or ["openalex"],
        include_types=[t.strip() for t in args.types.split(",") if t.strip()],
    )

    log = (lambda msg: None) if args.quiet else (lambda msg: print(msg, flush=True))

    cache = Cache(path=args.cache, ttl=args.cache_ttl,
                  enabled=not args.no_cache)
    pipe = Pipeline(
        cache=cache, mailto=args.mailto, openalex_key=args.openalex_key,
        scholar_backend=args.scholar_backend, serpapi_key=args.serpapi_key,
        scholar_base=args.scholar_base, scholar_delay=(lo, hi),
        use_crossref=not args.no_crossref, use_letpub=not args.no_letpub,
        use_ccf=not args.no_ccf,
        on_progress=log,
    )

    log(f"检索：{query.describe()}")
    log(f"数据源：{'+'.join(query.providers)}")
    try:
        result = pipe.run(query, sort_key=args.sort_key)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    finally:
        pipe.close()

    if not result.journals:
        print("没有命中任何期刊。可以试试放宽条件："
              "--scope abstract、去掉年份限制，或换个更常用的关键词。", file=sys.stderr)
        cache.close()
        return 1

    _print_table(result, args.top)

    files = write_all(result, args.outdir, args.basename,
                      args.formats or ["xlsx", "html", "json", "md"])
    print("\n已生成：")
    for path in files:
        print(f"  {Path(path).resolve()}")
    if result.stats.warnings:
        print("\n提示：")
        for w in result.stats.warnings:
            print(f"  · {w}")
    cache.close()
    return 0


def _print_table(result: PipelineResult, top: int) -> None:
    stats = result.stats
    print(f"\n共 {stats.papers_after_merge} 篇论文，聚合出 {stats.journals} 个候选载体，"
          f"耗时 {stats.elapsed:.1f}s")
    if stats.unresolved_papers:
        print(f"（其中 {stats.unresolved_papers} 篇没能确定载体，已归入「未识别载体」）")
    if stats.ccf_hits:
        print(f"（其中 {stats.ccf_hits} 个载体命中 CCF 目录）")

    head = (f"{'#':>3}  {'类型':<4} {'CCF':>3} {'刊名':<50} {'命中':>4} {'被引':>6} "
            f"{'分区':>4} {'IF':>6} {'2yr':>6}  审稿/会议周期")
    print("\n" + head)
    print("-" * len(head))
    for i, j in enumerate(result.journals[:top], 1):
        m = j.metrics
        vt = {"journal": "期刊", "conference": "会议"}.get(j.venue_type, "其它")
        name = j.name if len(j.name) <= 50 else j.name[:47] + "..."
        # 会议展示固定周期参考值（标注「固定」），期刊展示 letpub 的统计周期，
        # 两种不同性质的数字不能混在一起看
        if j.venue_type == "conference" and m.conf_cycle_months is not None:
            cycle = f"约{m.conf_cycle_months:g}个月(固定)"
        else:
            cycle = m.review_period or "-"
        print(f"{i:>3}  {vt:<4} {m.ccf_rank or '-':>3} {name:<50} {j.paper_count:>4} "
              f"{j.total_citations:>6} "
              f"{(str(m.cas_quartile) + '区') if m.cas_quartile else '-':>4} "
              f"{m.letpub_if if m.letpub_if is not None else '-':>6} "
              f"{round(m.impact_2yr, 2) if m.impact_2yr is not None else '-':>6}  "
              f"{cycle}")
    if len(result.journals) > top:
        print(f"... 其余 {len(result.journals) - top} 个见输出文件")


if __name__ == "__main__":
    raise SystemExit(main())
