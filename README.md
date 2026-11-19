# DevInsight AI

科技产业情报平台：从中国与全球公开、合规的数据源持续采集科技新闻、官方发布、开源动态与论文，
把原始信息转化为可检索、可追溯的情报。

当前进度：**阶段 0（工程骨架）+ 阶段 1（来源注册与 RSS）+ 阶段 2（多源 API）+ 阶段 3（正文补全与去重）**
已跑通，即手册 14.1 要求的「第一条真实数据闭环」：

```
9 个启用源（3 RSS + HN + 3 GitHub 仓库 + arXiv）
  -> Connector 抓取 / 解析 / 清洗 / 分类 / 增量
  -> PostgreSQL documents（幂等入库）
  -> engagement 回填热度 -> enrich 按热度抓正文 + SimHash -> dedup 判转载 / 聚事件
  -> FastAPI GET /api/v1/documents（支持 sort=engagement）
  -> Next.js /intelligence 情报流（按时间 / 按热度、事件簇、抓取状态）
```

## 技术栈

Next.js 16 + React 19 + TypeScript ｜ FastAPI + SQLAlchemy 2 + Alembic ｜
PostgreSQL 16 + pgvector ｜ Redis + Celery ｜（后续阶段）LangGraph + LLM API

## 快速开始

前置：Docker、Python 3.12+、Node.js 22+。依赖已装在仓库根 `.venv` 与 `apps/web/node_modules`。

```bash
# 1. 起基础设施
make up

# 2. 建表 + 写入来源
make migrate
make seed

# 3. 真实采集一轮（9 个启用来源：3 个 RSS + HN + 3 个 GitHub 仓库 + arXiv）
make sync
make status        # 看健康状态与最近批次
make docs          # 看最新入库情报

# 4. 补正文与判重（阶段 3）
make engagement    # 回填 HN 热度；存量数据必须先跑，否则 enrich 退化成按时间挑
make refingerprint # 用新解析逻辑修正存量（默认只处理 RSS；ALL=1 覆盖 API 源，会耗额度）
make enrich        # 按热度抓正文、算 SimHash、补摘要（默认 30 条，每源 10 条）
make dedup         # 判转载 + 聚同事件（默认 30 天窗口）

# 采集完顺手补一轮摘要（HN 这类上游只给标题的源必须这么做）
cd apps/api && python -m app.cli sync --source hackernews_en --enrich 40

# 5. 启动服务（两个终端）
make api           # http://localhost:8000/docs
make web           # http://localhost:3000/intelligence

# 可选：定时采集
make worker        # Celery Worker
make beat          # 每 5 分钟派发到期来源
```

## 已实现接口

| Method | Path | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 数据库/Redis 健康，DB 不可用返回 503 |
| GET | `/api/v1/sources` | 来源列表（含健康状态、最近一批结果） |
| GET | `/api/v1/sources/{id}` | 来源详情 |
| GET | `/api/v1/sources/{id}/health` | 最近 10 次 run + 20 条健康日志 |
| POST | `/api/v1/admin/sources/{id}/sync` | 手动触发同步（优先入 Celery，broker 不可用则进程内执行） |
| GET | `/api/v1/documents` | 情报列表：`q/source/region/language/topic/content_type/from/to/sort/limit/cursor` |
| GET | `/api/v1/documents/{id}` | 文档详情 |
| GET | `/api/v1/meta/topics` | 六大领域字典 |
| GET | `/api/v1/meta/content-types` | 内容类型分布（只列库里真实存在的类型，带条数） |
| GET | `/api/v1/stats` | 总览统计（按来源/地区/领域） |

统一响应：`{ data, meta: { request_id, timestamp, total, next_cursor }, error }`。

## 目录结构

```
apps/api/            FastAPI 后端
  app/core/          配置、日志、错误
  app/db/            模型（sources / ingestion_runs / documents / source_health_logs）、会话
  app/connectors/    Connector 抽象、registry 分派、RSS / HN / GitHub Release / arXiv 实现、来源种子
  app/services/      采集、来源、文档查询、规则分类
  app/api/v1/        路由
  app/worker/        Celery 应用与任务
  migrations/        Alembic（0001_init、0002_source_sync_cursor）
  tests/             单元测试（URL/时间/文本/分类）与各 Connector 合约测试
apps/web/            Next.js 前端（/intelligence 情报流 + 类型/来源/领域筛选、/sources 来源状态）
```

## 关键设计决策

- **published_at 与 fetched_at 严格分离**：解析失败就留空，绝不用采集时间伪装发布时间。
- **幂等入库**：`UNIQUE(source_id, external_id)` + `ON CONFLICT DO NOTHING`，重复采集只计入 skipped，不覆盖旧数据。
- **URL 规范化在前**：剔除 utm/ref/spm 等追踪参数、去 fragment、统一尾斜杠，否则同一篇文章会重复入库。
- **来源状态由运行时决定**：`health_status` 依据连续失败次数推断（≥3 次为 failing），不在代码里硬编码"可用"。
- **合规优先**：媒体源只存标题、摘要、原文链接（`rights_mode=metadata_excerpt`），全文不入库。
- **失败不放大**：单个源采集失败只影响自己，指数退避重试并遵守 `Retry-After`，4xx 不重试。
- **游标只在成功时推进**：写回 `sources.sync_cursor` 发生在采集成功之后；失败分支直接返回，游标保持不动，下一轮从同一位置重试 —— 否则一次网络抖动就会永久跳过一批条目。
- **限流不等于故障**：额度耗尽说明源是健康的，只是此刻不该再抓。这类错误只把来源记成 degraded，不累加 `consecutive_failures`，避免小配额的源被误判成 failing。

## 数据源与增量策略

| 来源类型 | 实现 | 增量依据 | 配额 |
|---|---|---|---|
| RSS / Atom | `connectors/rss.py` | 无（靠 `UNIQUE(source_id, external_id)` 去重） | 无硬限制 |
| Hacker News | `connectors/hackernews.py` | `max_item_id`，候选按 id **升序**处理 | 无公布配额 |
| GitHub Release | `connectors/github_releases.py` | `release_id` + ETag 条件请求 | 匿名 60 次/小时，认证 5000 次/小时 |
| arXiv | `connectors/arxiv.py` | `latest_published` + `submittedDate` 窗口查询 | 1 请求 / 3 秒（硬限制） |

增量两个容易做错的地方：

- **截断方向**：一轮抓不完时按 id 升序取前 N 个，游标只推进到确实处理过的位置。若按降序取"最新 N 个"，被挤掉的较旧条目就永远进不来了。
- **上游分页也是一种截断**：页大小 20 条，两轮之间新增超过一页时，被挤出第 1 页的条目比游标新、却已经不在响应里了。所以翻页直到出现 id ≤ 游标的条目为止（上限 3 页），单靠下游排序挡不住这一层。
- **首次基线**：只取最新一页作为基线，更早的历史不回补 —— 游标只向前走，缺失的是过去而不是将来。

页大小取 20 不是随手定的：vLLM 一个 release 的 Notes 常有几十 KB，20 条一页约 700KB，`per_page=100` 要传 3.6MB，单次轮询从 6 秒变成近 30 秒。

GitHub 建议在 `.env` 里填 `GITHUB_TOKEN`（匿名 60 次/小时是所有仓库共享的全局额度）。

**304 的命中率取决于两次轮询之间有没有人下载**，不是"某个仓库永远命中/永远不命中"。实测连续三轮：`vllm` 是 `[200, 304, 200]`（第二轮间隔内恰好没人下载，第三轮下载数 +1 就失效了），`ollama` 与 `transformers` 是 `[200, 304, 304]`。所以 304 只能当"顺手省一次"的红利：间隔 9 秒时三个仓库全命中，间隔 120 分钟时 vllm 这种每天几万次下载的仓库大概率每轮都是 200。真正的成本控制是页大小和轮询间隔。

配额耗尽时 GitHub 返回的是 **403 而不是 429**，所以 `github_releases.py` 按 `x-ratelimit-remaining: 0` 区分"额度耗尽"和"仓库不存在/无权限"——两者状态码相同，只看 message 文案会随 GitHub 改文案而失效。

arXiv 是另一类约束：它**不给任何退避线索**。超限时 Google Frontend 直接返回 `429` + 纯文本 `Rate exceeded.`，没有 `Retry-After`、没有 `x-ratelimit-*`，实测确认。所以只能反过来设计：

- 翻页之间强制 `sleep(3)`（官方明确要求 1 请求 / 3 秒），生产里正常只发 1 个请求，多页只在长时间停机后出现；
- 429 / 403 / 503 一律抛 `RateLimitedError` —— 503 对 arXiv 意味着过载维护，不是来源坏了，记成 failing 会误导；
- 轮询间隔给到 240 分钟。这个源的价值是不漏新论文，不是比别人快几分钟；
- 条目 id 去掉版本号（`2611.23229v3` → `2611.23229`），否则同一篇论文的每个版本都会成为一条独立情报。代价是修订不可见，换来的是列表不被重复版本淹没。

**增量不用"翻页找旧条目"，而是用时间窗口**。游标之后拼 `submittedDate:[游标 TO 999912312359]`，服务端只返回这一段，`opensearch:totalResults` 直接给出该段总篇数，翻页按这个数收敛。

这个改动是被实测数据逼出来的：`cs.AI` 单日提交 192 篇，三个分类合计更多。原来靠"翻页直到看见游标之前的条目"来确认覆盖，页数上限一卡（3 页 × 50 = 150 篇），停机超过 8 小时就会把中间那批新论文**永久丢掉**——arXiv 没有按 id 区间回溯的接口，丢了就再也拿不回来。窗口查询让"要取多少"由服务端算，不用猜。

两个必须注意的细节：

- **括号不能省**。`search_query` 常写成 `cat:cs.AI OR cat:cs.LG OR cat:cs.CL`，不包括号时 `AND submittedDate:[...]` 只约束最后一个 OR 分支，前两个分类等于没被窗口过滤，会把全部历史论文拉回来。
- **窗口起点往前留 1 分钟**。实测窗口边界粒度到分钟且包含该分钟（起点 18:59 时 `18:59:27` 的条目也在结果里），留 1 分钟是为了防同一分钟内的提交被舍掉；多传几条只会被唯一约束挡掉。

翻不完时不会丢数据：游标只推进到**本次实际处理批次的最新位置**，下一轮窗口从那里重新开始接着取。实测把游标手动退到 24 小时前，一轮取完 200 条后游标停在 `11-17 14:17`（而不是当天最新的 `18:59`），下一轮会自动从 `14:17` 继续追。日志里的 `arxiv_window_not_exhausted` 记录的就是"本轮没追完"，出现它说明积压，不说明出错。

arXiv 还要求 User-Agent 里能联系到人，否则限流阈值更严。`.env` 里的 `HTTP_USER_AGENT` 默认带一个占位邮箱，正式跑之前建议改成真实地址。

## 正文补全与去重（阶段 3）

### 为什么不是「全都抓」

抓正文要下载 HTML、解析 DOM、清洗、算指纹，单条成本比采集元数据高一个量级。对全部条目都抓，
总成本等于条目数乘以单页成本，而其中大部分内容既没人看、也不会被检索到。

「热门」必须落成可排序的字段，否则代码里没有任何东西能执行这条规则。热度只取上游自带的互动数据：

| 源 | 信号 | 归一 |
|---|---|---|
| Hacker News | `score` + `descendants` | `log1p(score)` |
| GitHub Release | 附件 `download_count` 之和 | `log1p(downloads)` |
| arXiv / RSS | 无 | 返回 `None`（**不是 0**），排序时走 `nulls_last` 兜底 |

不做跨源归一：HN 几百票、GitHub 几十万次下载，硬压到同一区间会变成「下载量永远第一」。
实际选取按源分组取前 N（`row_number() over (partition by source_id)`），只需要源内排序正确。

### 新列的存量数据是个坑

`engagement_score` 是阶段 3 新加的列，此前入库的 611 条全是 `NULL`。`NULL` 被 `nulls_last`
统一推到末尾，于是「按热度抓」实际执行的是「按时间抓」。增量同步也补不上——这些条目已存在，会被唯一约束跳过。

所以有独立的一步 `make engagement`：回访 HN 的 item 接口刷新 `score`/`descendants`。实测 255 条、8 并发、
**10.2 秒**回填 250 条（其余 5 条分值本身最新）。热度**只增不减**：HN 的分数会随时间下降，
而「值不值得抓」应该看历史最高关注度。

只回填 HN：GitHub 的下载量只能靠拉整个 release 响应拿到，匿名配额 60 次/小时，给 60 条各发一次就会
耗尽额度、连带影响正常同步。

### 抓取：三态而不是两态

| 状态 | 含义 | 重试 |
|---|---|---|
| `fetched` | 拿到正文或描述 | — |
| `empty` | 页面确实没有可抽取的正文（SPA、付费墙、纯图片） | 无意义，永久跳过 |
| `failed` | 网络错误 / 4xx / 429，原因写进 `content_error` | 手动改回 `pending` 才重试 |

混成一个 `failed` 会让「失败率」失去意义。抓到的正文按 `rights_mode` 决定落库长度：媒体源只存 600 字符
（全文只用于在内存里算指纹与提摘要），`full_text` / `public_domain` 存到 20000 字符。

### 判重：两个阈值都是实测出来的

| 判据 | 输入 | 阈值 | 结论 |
|---|---|---|---|
| SimHash 汉明距离 | 正文 | ≤ 10 / 64 位 | 转载，`dup_of` 指向最早那条 |
| 标题 token Jaccard | 标题 | ≥ 0.35、48h 内、**必须跨源** | 同事件，聚成一簇 |

实测依据：中文正文转载（改标题、加尾注、约 20 个 bigram 变动）距离是 **8**，无关文章 **34 以上**，
所以阈值取 10 而不是教科书的 3——守着 3 会把真实转载全部漏掉。事件聚类方面，同事件不同措辞的
Jaccard 是 **0.44~0.67**，无关事件是 **0.07**，取 0.35 落在空档中间。

两个反直觉的约束都是撞出来的：

- **必须跨源**：只按标题相似度聚，OpenAI 同一天的两条公告（ChatGPT Images / ChatGPT for Financial
  Services）会被聚成「同一事件」。这个簇要回答的是「这件事有几家媒体在报」，同源发两次不算。
- **`release` 不参与聚类**：GitHub 相邻版本标题只差最后一位数字，Jaccard 0.9 以上，
  `v0.40.8`~`v0.40.11` 会被聚成一起事件。这是定义问题，调阈值救不了。

先判转载再聚类，顺序不能反：否则转载副本会被当成「另一家媒体的报道」，把簇大小撑虚。

### 实测：1011 条语料上的抓取结果

| 源 | 状态 | 摘要覆盖率 |
|---|---|---|
| Hacker News | 372 `fetched` / 40 `from_feed` / 228 `failed` / 15 `empty` | 417 / 655（64%） |
| arXiv | 50 条 `from_feed` | 50 / 50（均值 1453 字） |
| GitHub Release ×3 | 60 条 `from_feed` | 60 / 60，只有 Notes 摘要，正文在 GitHub |
| 36氪 | 30 条 `from_feed` | 30 / 30，**RSS 自带全文**（3414~14173 字），但 `rights_note` 规定全文不入库，只用来算指纹 |
| InfoQ | 16 条 `from_feed` | 16 / 16，正文页 `HTTP 405` |
| OpenAI News | 200 条 `from_feed` | 199 / 200，正文页 Cloudflare `HTTP 403` |

全库 1011 条，772 条有摘要（76%），363 条有指纹。

**HN 的 64% 是本阶段的主要修复。** 修之前是 11%（28/255），三个原因叠在一起：上游只给标题（`item.text` 只对 Ask HN 自帖有值）；抓到的正文被写进 `content_text` 却没写进 `summary`，而前端只读 `summary`；227 条待抓的一直没轮到处理。补摘要的优先级是「feed 摘要 > 页面 `og:description` > 正文首段」，正文首段要跳过署名行、与标题相同的段落、以及「INSIDE Deutsche Bank」这类界面残渣。

228 条失败里 136 条是**本机代理对特定域名的策略问题**（reuters.com / ft.com / theguardian.com 稳定 502，github.com 重试后能通），55 条是站点反爬（403/405）。这两类都跟解析逻辑无关，换 UA 也救不了——对照实验见正文 4.13。

这张表在「4.7 上游给过全文，只是被我们自己截掉了」之前是另一副样子。36氪原本全标 `empty`，
看起来像抓不到——实际上它的订阅源一次就给整篇正文，是我们用 `summary_max_chars=600`
在入口处截掉的。**先分清「本来就不该抓」和「真失败」，失败率这个指标才有意义。**

判重结果 0 条转载、0 个簇。第一轮跑出 2 条「转载」，查下来是假阳性：两篇无关的 arXiv 论文，
abs 页的模板文字（分类路径、编号、Submitted on、View PDF、Abstract:）占了正文一半以上，
稀释指纹后距离被压到 10 位——正好卡在阈值边界。加了**跨域约束**之后清零：转载必须是
「同一篇稿子被不同站点转走」，同域相似只能说明共用站点模板。

363 条有指纹的文档里，跨域且正文高度相似的组合一个都没有，所以**真实语料里仍然没有「转载正例」**，
这条能力目前只有构造数据的单测支撑。

## 环境排障：WSL 里 docker 报 `Exec format error`

现象：`make up` 直接失败，报 `docker.exe: Exec format error`、`Error 126`。

三个独立根因，可能同时存在：

**1. PATH 里 `/Docker/host/bin` 抢占了 `docker` 这个名字（最常见）**

Docker Desktop 会往 PATH 里塞 `/Docker/host/bin`，那里的 `docker` 是转发到 Windows `docker.exe` 的脚本，必须先有下面的 WSLInterop 才能跑。而它同时也注入了 Linux 原生客户端（`/usr/bin/docker` → `cli-tools/usr/bin/docker`）和 compose 插件（`/usr/local/lib/docker/cli-plugins/docker-compose`），这两者**不依赖 interop**。

```bash
type -a docker        # 首行若是 /Docker/host/bin/docker，就是这个问题
ls -l /usr/bin/docker # 确认原生客户端确实存在
```

修复：把 `/Docker/host/bin` 从 PATH 中摘掉即可（本质是让原生客户端胜出，比修 interop 更稳，重启不丢）：

```bash
PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vx '/Docker/host/bin' | paste -sd: -)"
export PATH && hash -r
```

**2. WSLInterop 未注册**
WSL 执行 Windows `.exe` 依赖 `binfmt_misc` 里的 `WSLInterop` 条目，缺失时任何 `.exe` 都会报 `Exec format error`。

```bash
ls /proc/sys/fs/binfmt_misc/          # 只有 register、status 就是丢了
cat /proc/sys/fs/binfmt_misc/WSLInterop
```

修复（临时生效，重启 WSL 会重新注册）：

```bash
sudo sh -c 'echo ":WSLInterop:M::MZ::/init:PF" > /proc/sys/fs/binfmt_misc/register'
cat /proc/sys/fs/binfmt_misc/WSLInterop   # 应显示 enabled
```

回滚：`echo -1 | sudo tee /proc/sys/fs/binfmt_misc/WSLInterop`

**3. Docker Desktop 的 CLI 注入不全**
`/usr/bin/docker` 是指向 `/mnt/wsl/docker-desktop/cli-tools/usr/bin/docker` 的符号链接；若 `cli-tools` 节点没挂上，链接就是悬空的。修复：Windows 侧 `wsl --shutdown`，等 8 秒后重开 Ubuntu 终端，让 Docker Desktop 重新注入。

**应急通道**
容器已在运行时不需要修 docker 客户端：

- `make up` 会检测 5432 / 6379 是否已监听，已在跑就跳过启动
- `make migrate` / `seed` / `sync` / `status` / `api` 完全不依赖 docker 客户端

## 已知边界（后续阶段处理）

- 主题分类目前是确定性关键词规则，存在少量误判（如"标准版"命中政策类）；后续阶段引入模型分类与人工校正。
- 关键词检索为 `ILIKE`；全文检索 / 向量检索在知识库阶段用 `tsvector` + pgvector 补上。
- **中文媒体这条线只有 600 字摘要可检索**：36氪的 RSS 其实给了全文（3414~14173 字），但
  `rights_note` 规定「全文不入库」，所以全文只用于算 SimHash 指纹、不进 `content_text`；
  InfoQ `405`、OpenAI `403`（Cloudflare，换 UA 实测无效）。要做中文全文检索，只有三条路：
  上无头浏览器、换有正文授权的中文源、或重新评估 36氪全文的授权边界。在此之前，中文召回
  质量会明显低于英文——这是输入问题，不是模型问题。
- Hacker News 本身不提供摘要（外链故事的正文在别人站上）。阶段 3 的 `make enrich` 会按热度抓外站正文，抓到后按「页面描述 > 正文首段」补 `summary`，覆盖率从 11% 提到 64%。剩下的 36% 卡在两处：**本机代理对部分域名稳定 502**（reuters / ft / guardian / youtube），以及站点反爬 403/405。前者是运行环境问题，把代理切到全局模式后重抓可以救回一部分；后者无解。
- 抓取失败后**不自动重试**：实测失败原因绝大多数是 403/405 反爬，重试只会重复打同一请求。需要重抓时用 SQL 把 `content_status` 改回 `pending`（`content_error like 'ProxyError%'` 这类可以先试试，代理恢复后能救回）。
- 采集不会自动补摘要：`make sync` 只拿元数据。对 HN 这类上游只给标题的源，要在同一轮里跑 `sync --enrich N`，否则每轮同步都会新攒一批 `pending`。
- 真实语料里还没有「转载正例」：判重阈值（汉明 10 / Jaccard 0.35）的支撑目前来自构造数据的单测。363 条真实指纹里跨域相似组合为 0，第一轮跑出的 2 条「转载」是 arXiv 模板文字造成的假阳性，已由跨域约束修掉。接入更多中文媒体源之后需要重新校准。
- GitHub Release Notes 里的 `**` 强调标记刻意不清洗：去标记的正则会把 `**kwargs`、`__init__`、`use_cache` 这类标识符改坏，而它们正是版本说明的主要信息载体。排版层渲染前需自行处理。
- arXiv 的分页与窗口已在真实接口上验证（翻 6 页取 300 条约 46 秒、未触发 429），但限流是概率性的：真被限流时整轮抛 `RateLimitedError` 降级，不会写入半截批次。
- arXiv 自带的分类（`cs.AI` / `cs.LG`）未写入 `topics`：它与项目的主题词表是两套体系，混用会让前端按主题筛选时出现只有论文才有的 slug。要按 arXiv 分类筛选需要单独加字段。
- 关键词分类器对论文标题几乎没有区分度：实测 50 篇 arXiv 论文全部判为 `ai-models`。这是阶段 5 换模型分类的直接动因。
- DeepMind、TechCrunch 已注册但默认停用，尚未做健康检查。
- GitHub 分页上限 3 页（60 条）。两轮之间新增超过 60 个版本时仍会出现空档，实际不会发生；真发生只能靠人工补。
- 未做 user/鉴权（单用户本地版），`/admin` 端点暂无权限校验。

## 参考

工程手册见 `执行摘要.pdf`（数据源清单、评分公式、分阶段验收标准）。
