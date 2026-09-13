# 期刊选择助手

按研究方向反查候选投稿期刊：先搜同主题的论文，再看这些论文都发在哪，按期刊聚合排序。

思路来自 [nickchen121/cyd-selected-journal](https://github.com/nickchen121/cyd-selected-journal)，
这一版把数据源和工程结构整体换掉了。

## 为什么这个思路成立

没有任何公开数据库能回答「研究 XX 主题该投哪个刊」。但同行的投稿选择就是答案：
把同主题的论文捞出来，按载体聚合，发文最多的那几本就是这个方向的主战场。

## 和原项目的差别

| | 原项目 | 这一版 |
|---|---|---|
| 主数据源 | selenium 爬百度学术 + 逐篇抓详情页 | OpenAlex 官方 API |
| 依赖浏览器 | 必须手动下载 chromedriver 并填路径 | 不需要（仅 selenium 后端可选） |
| Google 学术 | 无 | 有，三种后端可切换 |
| 载体识别失败 | 直接丢弃 | Crossref 兜底按 DOI 补 |
| 期刊指标 | letpub 分区 + CiteScore | OpenAlex（两年均被引、h-index、DOAJ、APC、出版国）+ letpub（分区、IF、审稿周期、录用比例，默认开启） |
| CCF 等级 | 无 | 本地静态匹配 CCF 2022 版目录，标 A/B/C，默认开启 |
| 会议审稿周期 | 无（letpub 不收会议） | 常见会议的固定截稿-通知周期本地参考表 |
| 会议 | 按年份拆成多条 | 同一会议各届合并成一行 |
| 去重 | 无 | 按 DOI，退化到规范化标题 |
| 预印本库 | arXiv 以数量霸榜 | 默认剔除 |
| 缓存 | 无 | SQLite，重复查询提速约 15 倍 |
| 排序 | 排序信息编码进字典 key 后按字符串排 | 6 种排序可选（含 CCF 等级） |
| 输出 | 单个 JSON | xlsx / html / json / md |
| 界面 | 爬取跑在主线程，作者注明「程序会假死」 | 工作线程 + 队列 + 轮询，可随时停止 |

## 安装

```bash
pip install -r requirements.txt
```

Python 3.10 以上（用到 `str | None` 写法）。只装 `requests` 也能跑，
不装 `openpyxl` 就没有 Excel 输出，不装 `selenium` 就用不了 selenium 后端。

## 用法

### 图形界面

```bash
python gui.py
```

填关键词、选数据源、点「开始检索」。letpub 富化和 CCF 目录匹配默认都是勾上的。
抓取在后台线程跑，界面不会卡；中途可以点「停止」，已抓到的结果照常输出。

### 命令行

```bash
# 标题里含 "video captioning"，2021 年以后
python -m journal_picker -a "video captioning" --from 2021

# 标题含 "video captioning"，且含 transformer 或 attention 之一
python -m journal_picker -a "video captioning" -o transformer -o attention

# 同时用 OpenAlex 和 Google 学术（letpub / CCF 匹配默认都是开的）
python -m journal_picker -a "knowledge graph" -s openalex -s scholar

# 按 CCF 等级排序（A 类优先），关掉 letpub 提速
python -m journal_picker -a "graph neural network" --sort ccf --no-letpub

# 按分区排序，输出到指定目录
python -m journal_picker -a "graph neural network" --sort quartile -O ./out
```

`python -m journal_picker --help` 有全部参数。

常用几个：

- `--scope` 检索范围。`title` 仅标题（默认，对应原项目的 `intitle:`，最精准）、
  `abstract` 标题+摘要、`fulltext` 全文（命中多但噪声大）
- `-n` 最多分析多少篇论文，默认 200。这个数直接决定耗时
- `--sort` 排序：`papers` 发文量（默认）、`impact` 影响力、`quartile` 分区优先、
  `citations` 总被引、`ccf` CCF 等级优先（A>B>C>未收录）、`name` 刊名
- `--no-letpub` 关掉 letpub 富化（默认是开的，会补分区/IF/审稿周期/录用比例，
  但会变慢且可能被限流）
- `--no-ccf` 关掉 CCF 目录匹配（默认是开的，纯本地字符串匹配，不发请求，
  基本不影响耗时）
- `--mailto` 填你的邮箱，OpenAlex 和 Crossref 会给更宽松的配额

## 输出

默认在 `./output` 下生成同名的四个文件：

- `.xlsx` 三个工作表：期刊汇总（带筛选和冻结窗格）、论文明细、运行信息
- `.html` 单文件，点表头可排序，每行能展开该刊命中的论文，CCF 等级会用不同颜色标出
- `.json` 完整结构化数据，方便二次处理
- `.md` 适合贴到笔记里

用 `-f` 可以只要其中几种，比如 `-f xlsx -f html`。

## 数据源说明

### OpenAlex（默认，推荐）

免费、不需要注册、不会封 IP。检索和期刊指标都来自这里。

额度是按美元预算算的，不同请求单价差很多：

| 请求类型 | 单价 | 无 key 每日 $0.1 能用 |
|---|---|---|
| search（`title.search:` 这类检索） | $0.001 | 约 100 次 |
| list（`filter=openalex:` / `issn:`，用于取期刊指标） | $0.0001 | 约 1000 次 |
| singleton（`/works/W123`） | 免费 | 不限 |

一次检索只花 2-4 个 search 请求（每次最多带回 100 篇），日常够用。
要更多可以去 <https://openalex.org/settings/api> 免费申请 key，额度是 10 倍，
用 `--openalex-key` 传入。

额度用尽时返回的 429 是硬限制 —— 响应头里 `Retry-After` 是将近 20 小时
（UTC 零点重置），退避重试纯属白等。程序会识别这种 429 并立刻停用检索类请求，
在报告里给出提示，同时保留还能用的便宜请求（额度是一个共享池，
search 用尽时 list 往往还能跑）。

另外 OpenAlex 对匿名 search 请求还有一层独立的瞬时限流，响应体也是
`Rate limit exceeded`，但 `Retry-After` 只有几十秒，跟上面的硬额度是两回事，
程序按 `Retry-After` 时长退避重试即可自行恢复，不会误判成额度耗尽。

期刊指标里的「两年均被引」是 OpenAlex 的 `2yr_mean_citedness`，
口径接近影响因子，但**不是**官方 JIF，别直接当 IF 用。

### Google 学术

没有官方 API，反爬很严格，所以做了三个后端：

| 后端 | 说明 |
|---|---|
| `http`（默认） | 直连 `scholar.google.com`。本地能上 Google 时最省事，翻页之间随机等 5-12 秒 |
| `serpapi` | 走 [SerpApi](https://serpapi.com/) 的 Google Scholar 接口，要 API key，但稳定不封 |
| `selenium` | 打开可见的 Chrome，遇到验证码手动点一下，程序会自动继续 |

Google 学术只负责「发现论文」。它的来源行里刊名是截断的
（形如 `…on Pattern Analysis …`），直接用会把一个期刊拆成一堆碎片，
所以刊名一律靠标题回查得到规范名称。回查顺序是：

1. Crossref —— 免费、没有每日额度。实测 30 条 Scholar 结果能认出 27 条，
   漏的是预印本（本来也要剔除）
2. OpenAlex —— 只在 Crossref 没认出来时补刀

这个顺序是必须的。OpenAlex 的标题回查属于 search 类请求（$0.001/次），
逐篇回查一百多篇论文会直接把无 key 的当日额度打爆 —— 这是实测踩过的坑。
换成 Crossref 优先之后，一次 120 篇的双源检索只花 2-4 个 search 请求。

如果本地访问不了 Google，程序会明确提示，不会静默返回空结果。
常见的国内镜像站基本都是 JS 渲染，`http` 后端抓不到，
要用 `--scholar-base` 指定镜像的话得配 `--scholar-backend selenium`。

### Crossref（兜底，默认开启）

承担两个角色：

- Google 学术那一路的主力刊名解析器（见上）
- OpenAlex 缺载体时的兜底。OpenAlex 有一部分记录没有 `primary_location.source`，
  IEEE 系会议论文尤其明显（实测 Vid2Seq、Streaming Dense Video Captioning
  这类 CVPR 论文连 `locations` 都是空的），按 DOI 问一下 `container-title`
  就能补回完整会议名

`--no-crossref` 可关，但关掉之后 Google 学术只能靠 OpenAlex 回查刊名，
很容易打爆额度，程序会在报告里提示这一点。

### letpub（默认开启）

补中科院分区、JCR 影响因子、CiteScore、审稿周期、录用比例、SCI 收录、学科分类。

这是第三方网站，随时可能改版或限流，所以整个模块是尽力而为：
拿不到就留空，不影响主流程。请求间隔默认 8 秒（早期版本是 2 秒，
实测那个速度大约每 11 个请求就会触发一次限流），限流响应的特征是
HTTP 200 但只有几百字节的空壳页，程序会识别并退避重试（等待时间随
连续失败次数指数增长，上限 60 秒），被限流时不写入缓存，下次重跑还能补上。
即便调大了间隔，同一 IP 短时间内查太多本刊仍可能撞上限流，
这是 letpub 那边的频率控制导致的，不是程序 bug；耐心等退避重试，
或者分批、隔一段时间再跑，一般都能补齐。

报告里这一列标为「分区(letpub)」而不是断言中科院分区，
因为 letpub 页面上这一列现在的表头是「新锐期刊分区表」。

用 `--no-letpub` 关掉；关掉会明显变快，但也就没有分区/IF/审稿周期/录用比例了。

letpub 完全不收录会议，只覆盖 SCI 期刊。检索出的会议在 letpub 相关列上
永远是空的，这不是 bug，是它本身的覆盖边界（会议的等级和周期看 CCF 那两列）。

### CCF 目录匹配（默认开启，本地静态，不联网）

给聚合结果标 CCF 推荐目录里的等级（A/B/C）和所属类目，覆盖计算机体系结构、
网络、安全、软件工程、数据库、理论、图形学多媒体、人工智能、人机交互、
交叉综合十个大类，会议和期刊都有。

数据来自 CCF《中国计算机学会推荐国际学术会议和期刊目录》2022 年版的公开整理版本，
内置在 `journal_picker/data/ccf_2022.py`，不发网络请求，匹配几乎不耗时。

这是**静态快照**：CCF 大约每隔几年发一版新目录，如果之后出了更新版本，
这个模块不会自动跟着更新，需要手动重新整理数据文件。报告里会注明用的是哪一年的版本。

匹配是按刊名/会议名做字符串匹配（缩写精确匹配 -> 全名精确匹配 -> 模糊匹配兜底），
没匹配上不代表这个刊不重要，可能是名字写法跟目录里不完全一致，或者不在目录收录范围内。

`--no-ccf` 可关。

### 会议固定审稿周期（本地静态参考表）

这是回应「显示平均投稿时间」这个需求时要澄清的一点：期刊和会议的「审稿周期」
根本是两种不同性质的数字，不能用同一套逻辑处理。

- **期刊**：letpub 的「审稿周期」是统计用户实际投稿耗时算出来的，这是唯一
  有这个数据的来源，OpenAlex 完全不追踪这个
- **会议**：没有任何数据源会给会议算「平均审稿周期」，因为会议是固定截稿日
  + 固定通知日的官方日程表，不是统计出来的。所以这里维护了一份小的本地参考表
  （`journal_picker/data/conference_cycles.py`），收录了几十个常见 CCF 会议
  历年 CFP 的典型截稿-通知间隔

报告里这一列单独标「(固定)」，跟期刊的「审稿周期(letpub统计)」分列展示，
避免把两种性质不同的数字混在一起比较——前者是日程表定的，后者是统计出来的。
这份表只覆盖比较常见的会议，没收录的会议这一列是空的。

## 缓存

默认放在 `~/.journal_picker/cache.sqlite3`，有效期 30 天。
缓存 OpenAlex 期刊指标、标题回查结果、Crossref 结果、letpub 结果、
Google 学术页面。同一个方向反复调参时几乎不再发请求
（实测同一条查询二次运行从 127 秒降到 8 秒）。CCF 匹配和会议周期表是本地静态数据，
不经过缓存，每次都是即时计算。

`--no-cache` 禁用，`--cache` 换路径，`--cache-ttl` 改有效期。

## 代码结构

```
journal_picker/
  models.py               Paper / Journal / JournalMetrics / SearchQuery 数据类
  normalize.py            刊名清洗、标题与 DOI 规范化、会议系列归并
  cache.py                SQLite 键值缓存
  openalex.py             OpenAlex 检索 + 期刊指标 + 标题回查
  scholar.py              Google 学术三后端 + 页面解析
  crossref.py             按 DOI/标题补载体
  letpub.py               分区/IF/审稿周期富化
  ccf.py                  CCF 目录匹配 + 会议固定周期查询
  data/ccf_2022.py        CCF 2022 版目录静态数据
  data/conference_cycles.py  常见会议固定截稿-通知周期参考表
  pipeline.py             编排：检索 -> 合并去重 -> 补载体 -> 聚合 -> 取指标 -> 标 CCF -> 排序
  report.py               xlsx / html / json / md 输出
  cli.py                  命令行
gui.py                    图形界面
```

网络层、聚合层、输出层是分开的。`merge_papers`、`aggregate`、
`filter_by_types`、`sort_journals`、`match_ccf` 都是纯函数，不碰网络，可以单独测。

## 已知限制

- 「两年均被引」不是官方影响因子，分区来自 letpub 而非官方名单，
  选刊时请以期刊官网和单位认可的名单为准
- CCF 目录是 2022 年版静态快照，不会随官方改版自动更新
- Google 学术随时可能改版或封 IP。这是它的固有问题，不是配置问题
- 会议的影响力指标基本是空的：OpenAlex 不给会议算两年均被引
  （CVPR 2022 有 23 万被引，这个字段也是 0），程序已把这种 0 当作「无数据」置空
- 会议的「固定审稿周期」只是历年 CFP 的典型间隔，每年具体日期会变，
  遇到多轮投稿制的会议（USENIX Security、CCS、ICSE 等）给的是「一轮」的周期
- 检索词是原样送进 API 的，不做同义词扩展。方向名称有多种叫法时，
  建议用 `-o` 把几种写法都列上
- 刊名以 OpenAlex 的 `display_name` 为准，它自己的数据里有些刊是小写的
  （`Genome biology`、`Journal of medical imaging`）。没有用启发式改大小写，
  因为那会破坏 `IEEE`、`eLife`、`PLoS ONE` 这类本来就该混写的名字
- OpenAlex 的中文期刊用英文刊名，所以「国内刊」是按出版国判断的，不是按刊名
