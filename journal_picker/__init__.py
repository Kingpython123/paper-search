"""journal_picker —— 按研究方向反查候选投稿期刊。

思路沿用 nickchen121/cyd-selected-journal：
先搜同主题的论文，再看这些论文都发在哪，按期刊聚合排序。

与原项目的主要差别：
- 主数据源换成 OpenAlex 官方 API，不再爬百度学术，也就不需要 chromedriver
- 新增 Google 学术检索源（抓取 + 标题回查 OpenAlex 得到规范刊名）
- 期刊指标来自 OpenAlex（两年期均被引 ≈ IF、h-index、DOAJ、APC），
  中科院分区/审稿周期由 letpub 可选富化
- SQLite 缓存，重复查询几乎零成本
- 爬取跑在独立线程，GUI 不再假死
"""

__version__ = "2.0.0"

from .models import Journal, JournalMetrics, Paper, SearchQuery

__all__ = ["Journal", "JournalMetrics", "Paper", "SearchQuery", "__version__"]
