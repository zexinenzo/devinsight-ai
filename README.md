# DevInsight AI

科技产业情报平台：从中国与全球公开、合规的数据源持续采集科技新闻、官方发布、开源动态与论文，
把原始信息转化为可检索、可追溯的情报。

当前进度：**阶段 0（工程骨架）+ 阶段 1（来源注册与 RSS）+ 阶段 2（多源 API）+ 阶段 3（正文补全与去重）**
已跑通，即手册 14.1 要求的「第一条真实数据闭环」：

```
11 个启用源（6 RSS + HN + 3 GitHub 仓库 + arXiv）
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

# 3. 真实采集一轮（11 个启用来源：6 个 RSS + HN + 3 个 GitHub 仓库 + arXiv）
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

# 可选：让整条链路自己转（worker + beat 都起来之后，上面 3/4 步不再需要人手跑）
make worker        # Celery Worker
make beat          # 见下方「自动跑的三件事」
```

### 自动跑的四个任务

只起 `worker + beat` 就够了。这几条在 `celery_app.conf.beat_schedule` 里：

| 任务 | 周期 | 作用 | 不跑会怎样 |
|---|---|---|---|
| `dispatch-due-sources` | 5 分钟 | 按各源 `poll_interval_minutes` 派发采集 | 没有新数据 |
| `enrich-hot-batch` | 30 分钟 | 按热度补 20 条正文（每源 5 条） | 新条目永远停在 `pending`，列表没摘要 |
| `refresh-hot-scores` | 6 小时 | 回访 HN 条目，刷新 `score` | 「按热度抓」退化成「按发布顺序抓」——入库那一刻 `score` 恒为 1 |
| `dedup-recent` | 1 小时 | 重跑判重与同事件聚类 | 新抓到的条目游离在簇外，前端显示的是上一轮的结论 |

有两条设计取舍值得说明：

- **`enrich` 一次只跑 20 条**，不是「把队列清空」。一次抓 400 条要十几分钟，期间占满连接池、拖慢同步；
  拆成小批后，新故事半小时内有摘要，而失败条目本来就该等退避。
- **`refresh-hot-scores` 排在 `enrich` 之后不是必须的**，但两者顺序影响很大：热度刷新在前的那个周期，
  候选排序才反映真实关注度。现在这个配置意味着一条故事发布 6 小时后才可能被判定为「热」——
  这是有意的，太早抓等于抓一堆后来没人看的东西。

## 已实现接口

| Method | Path | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 数据库/Redis 健康，DB 不可用返回 503 |
| GET | `/api/v1/sources` | 来源列表（含健康状态、最近一批结果） |
| GET | `/api/v1/sources/{id}` | 来源详情 |
| GET | `/api/v1/sources/{id}/health` | 最近 10 次 run + 20 条健康日志 |
| POST | `/api/v1/admin/sources/{id}/sync` | 手动触发同步（优先入 Celery，broker 不可用则进程内执行） |
| GET | `/api/v1/documents` | 情报列表：`q/source/region/language/topic/content_type/from/to/sort/limit/cursor/mode` |
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

## 授权分级与落库长度

`rights_mode` 决定一条正文能存多少字。它曾经是**没有接通的**：`sources` 表只有
`rights_note`（一句给人看的说明），没有机器可读的字段，`SourceSpec.from_source()`
也从来没把库里的值传下去。所谓「全文不入库」一直靠注释和自觉维持，不是靠代码——
所有源都落在 `metadata_excerpt` 的默认值上，看起来合规，其实只是因为没有任何源
能表达另一种可能。

现在三档，写在 `sources.rights_mode` 里：

| 取值 | 含义 | 落库长度 | 当前使用 |
|---|---|---|---|
| `metadata_excerpt` | 默认版权保留 | 600 字 | 36氪、InfoQ 中英文、少数派、HN、arXiv、GitHub |
| `cc_by_nc_nd` | 权利人明示 CC 署名-非商用-非衍生 | 20000 字 | 阮一峰的网络日志 |
| `full_text` / `public_domain` | 已获授权或已进入公有领域 | 20000 字 | 暂无 |

### 36氪：明确不放开，三条依据

一度以为 36氪 的 RSS 主动推送全文、性质接近公开分发，因此可以放开。重新核实后
这个推理是错的：

1. **用户协议 6.1 点名禁止**：「未经本平台许可，任何人不得擅自使用（包括但不限于
   通过任何机器人、蜘蛛等程序或设备监视、**复制**、传播、展示、镜像、上载、
   **下载**）36氪服务中的内容。」
2. **转载规则是默示拒绝 + 极高门槛**：「24 小时内未收到转载许可，则表示该篇文章
   不支持转载」；长期授权须「网站日活 100W 以上」或付费合作。
3. **RSS 全文推送不构成默示许可**，有直接判例：MidlevelU v. ACI（美国第十一巡回
   法院，2021）事实几乎与本项目一致（RSS 全文 → 聚合 → 摘要 + 原文索引），
   判侵权，赔 202,500 美元。判决书原话：「前门（爬虫）的默示许可，不等于后窗
   （RSS）的默示许可。」

还有一层：36氪 大量内容本身转载自 `（央视新闻）`、`（新浪财经）`、`（界面）`，
其转载规则第 4 条也要求特邀作者文章联系原作者。**拿到 36氪 授权也不覆盖原始权利人。**

顺带纠正一个统计错误：36氪 的 RSS **不是稳定的全文源**。30 条里中位数只有 215 字，
20 条不足 600 字，只有头条深度稿给全文（最长 9283 字）。之前记的「3414~14173 字」
是只看头条造成的抽样偏差——放开全文的收益上限也只有三分之一，而法律风险是明确的。

### 中文语料：真正的瓶颈不是长度，是条数

加源之前的分布：

| 语言 | 条数 | 占比 |
|---|---|---|
| 英文 | 981 | 97.0% |
| 中文 | 30 | 3.0% |

中文召回差的根因是**库里只有 30 条中文**，不是那 600 字的限制——检索不到的内容，
不是因为摘要太短，而是因为库里根本没有。所以这一轮的动作是加源而不是放开全文。

新增三个中文源：

| 源 | 授权依据 | 落库 | 实测 |
|---|---|---|---|
| 阮一峰的网络日志 | 页面明示 CC BY-NC-ND 3.0（`creativecommons.org/licenses/by-nc-nd/3.0/deed.zh`） | 全文 | 回填 390 篇，正文均值 6143 字 |
| InfoQ 中文 | 无开放授权声明，按默认版权 | 摘要 | RSS 只给「点击查看原文」，无摘要 |
| 少数派 | 同上 | 摘要 | 摘要 40~110 字 |

加源之后：中文 450 条，占全库 1431 条的 31.4%。

三个必须处理的坑：

- **InfoQ 中文的 `<description>` 清洗后就是「点击查看原文>」7 个字**。原样入库会让
  20 条记录共用同一句「摘要」，列表看起来像同一篇文章。现在由 `_usable_summary`
  识别为无效摘要并留空，让前端如实显示「该源未提供摘要」。
- **语言标签不统一**：少数派给 `zh-CN`，OpenAI News 给 `en-US`，其余是 `zh`/`en`。
  穷举 zh-* 别名会一直漏——`en-US` 那 200 条按 `language=en` 筛选时整批消失。
  现在统一取 BCP-47 主标签（`_normalize_language`），地区细分一律丢掉。
- 两个摘要源都进了 `NO_FETCH_SOURCES`：不是抓不到，是**不该抓**——它们只授权了
  RSS 摘要，没有页面抓取许可，抓了只是把 150 字换成 600 字。

### 回填 390 篇历史周刊

阮一峰的历史周刊不在 Atom feed 里（feed 只给最近 3 期），用 `scripts/backfill_ruanyifeng.py`
从月度归档页 `/blog/YYYY/MM/` 回填。这是本项目唯一能拿到的中文长文语料库。

```bash
cd apps/api
python ../scripts/backfill_ruanyifeng.py                                  # 全量回填（幂等，0.4s/请求）
BF_START=2026-10 BF_END=2026-11 python ../scripts/backfill_ruanyifeng.py  # 试跑两个月
python ../scripts/backfill_ruanyifeng.py --summaries-only                 # 改摘要规则后只重算，不重抓
```

回填踩的三个坑，都是「数据看着正常、其实全错」那一类：

- **摘要 166 篇一模一样**：`clean_html` 的 `separator=" "` 把块级元素连成一行，段落边界
  丢了，「取第一段」实际取到全篇开头 300 字，而周刊开头每期都是同一段套话。改成按
  块级元素逐个取文本、用 `\n` 拼接。
- **126 篇摘要是封面图说明**：版式固定为「套话 → 封面图小节（图 + 图注）→ 正文首段」，
  图注七八十字，长度门槛拦不住。改成按位置跳过整个 `封面图` 小节。注意图注结句标记
  不统一——新版写「（ via ）」、老版写「（ 出处 ）」，只匹配 via 会漏掉 2019~2022 全部文章。
- **回填完再同步一次，最近 3 期又各多一条**：`external_id` 口径不同（Atom 的 `tag:`
  标识 vs URL），`canonical_url` 也不同（feed 给 `http://`、归档页给 `https://`）。
  判重键改成「去掉 scheme 的 URL」，并落成数据库唯一索引：

  ```sql
  CREATE UNIQUE INDEX uq_documents_source_canonical_url
      ON documents (source_id, regexp_replace(canonical_url, '^https?://', ''));
  ```

  `ingestion_service` 的 `on_conflict_do_nothing` 同时去掉冲突目标，任何唯一约束被撞都跳过。
  只在脚本里判重是不够的——下一次同步就会把重复插回来。


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
| `failed` | 网络错误 / 4xx / 429，原因写进 `content_error` | 按原因分两类，见下 |

混成一个 `failed` 会让「失败率」失去意义。抓到的正文按 `rights_mode` 决定落库长度：媒体源只存 600 字符
（全文只用于在内存里算指纹与提摘要），`full_text` / `public_domain` 存到 20000 字符。

### 失败重试：分两类，不是「一律不重试」

`failed` 曾经是终态，理由是「失败大多是 403 反爬，重试只是替对方刷日志」。这个结论只对了一半——
实测 228 条失败里，136 条是本机代理对特定域名的 502，代理规则一改就能通，但它们永远不会再被挑中。
所以现在按**错误的确定性**分开处理：

| 错误 | 例子 | 处理 |
|---|---|---|
| 确定性 | `HTTP 401/403/404/405/410`、`非 HTML 内容` | `content_attempts` 直接记满 3，永久跳过 |
| 环境性 | `HTTP 5xx`、`HTTP 429`、`ProxyError`、`ConnectError`、`ReadTimeout` | 记一次失败，退避 **6h → 24h → 72h**，三次用完为止 |

判断结果落进 `content_attempts` 这一列，而不是留在 SQL 里靠 `LIKE 'HTTP 4%'` 匹配错误串：
错误格式会随 `_download` 的实现变化，而写库点是唯一知道「这次到底为什么失败」的地方。
筛选时只比数字（`content_attempts < 3` 且退避已过），查询因此保持简单。

存量迁移同理：0005 会把已有的 4xx 失败直接标成预算耗尽，否则迁移后第一轮 enrich
会把那 55 条反爬条目全部重打一遍。

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

### 实测：1431 条语料上的抓取结果

| 源 | 状态 | 摘要覆盖率 |
|---|---|---|
| Hacker News | 372 `fetched` / 40 `from_feed` / 228 `failed` / 15 `empty` | 417 / 655（64%） |
| arXiv | 50 条 `from_feed` | 50 / 50（均值 1453 字） |
| GitHub Release ×3 | 60 条 `from_feed` | 60 / 60，只有 Notes 摘要，正文在 GitHub |
| OpenAI News | 200 条 `from_feed` | 199 / 200，正文页 Cloudflare `HTTP 403` |
| 36氪 | 30 条 `from_feed` | 30 / 30，正文只算指纹不入库（`rights_mode=metadata_excerpt`） |
| InfoQ 英文 | 16 条 `from_feed` | 16 / 16，正文页 `HTTP 405` |
| 阮一峰周刊 | 390 条（全部由历史归档回填） | 390 / 390，正文均值 6143 字 |
| InfoQ 中文 | 20 条 `from_feed` | 0 / 20，feed 只给「点击查看原文」 |
| 少数派 | 10 条 `from_feed` | 10 / 10 |

全库 1431 条，1172 条有摘要（82%），753 条有指纹。

**HN 的 64% 是本阶段的主要修复。** 修之前是 11%（28/255），三个原因叠在一起：上游只给标题（`item.text` 只对 Ask HN 自帖有值）；抓到的正文被写进 `content_text` 却没写进 `summary`，而前端只读 `summary`；227 条待抓的一直没轮到处理。补摘要的优先级是「feed 摘要 > 页面 `og:description` > 正文首段」，正文首段要跳过署名行、与标题相同的段落、以及「INSIDE Deutsche Bank」这类界面残渣。

228 条失败里 136 条是**本机代理对特定域名的策略问题**（reuters.com / ft.com / theguardian.com 稳定 502，github.com 重试后能通），55 条是站点反爬（403/405）。这两类都跟解析逻辑无关，换 UA 也救不了——对照实验见正文 4.13。

这张表在「先分清『本来就不该抓』和『真失败』」之前是另一副样子。36氪原本全标 `empty`，
看起来像抓不到。真实原因是正文按授权级别（`metadata_excerpt`）在入口处就截断了，落库为空——
不是源站不给。而它的订阅源也确实**不是稳定的全文源**：30 条中位数 215 字，只有头条深度稿给全文。
**先分清「本来就不该抓」和「真失败」，失败率这个指标才有意义。**

判重结果 0 条转载、0 个簇。第一轮跑出 2 条「转载」，查下来是假阳性：两篇无关的 arXiv 论文，
abs 页的模板文字（分类路径、编号、Submitted on、View PDF、Abstract:）占了正文一半以上，
稀释指纹后距离被压到 10 位——正好卡在阈值边界。加了**跨域约束**之后清零：转载必须是
「同一篇稿子被不同站点转走」，同域相似只能说明共用站点模板。

753 条有指纹的文档里，跨域且正文高度相似的组合一个都没有，所以**真实语料里仍然没有「转载正例」**，
这条能力目前只有构造数据的单测支撑。

## 向量化检索（阶段 4）

### 问题：关键词检索够不着正文

`q` 参数目前匹配的是标题和摘要。实测一句正文里的话：

```
GET /api/v1/documents?q=乌兰察布 数据中心&mode=keyword   → 0 条
GET /api/v1/documents?q=乌兰察布 数据中心&mode=vector    → 2 条（相似度 0.23 / 0.22）
```

那篇文章的标题是《机房来了，写字楼走了》，"乌兰察布"只出现在正文里。这不是模型问题，
是输入问题：标题里没有的字，`ILIKE` 永远搜不到。

### 但正文不能入库

`metadata_excerpt` 级别的源只允许存 600 字（见「授权分级与落库长度」）。把全文塞进
`content_text` 会让性质从"我们自己看"变成"替代原站"，这正是前面花了很大力气才划清的线。

于是做法是：**正文在内存里只待一次，切成块、算成向量，然后把文字丢掉**。
库里留下的是 384 个浮点数加一个字符区间，没有一句可读的原文：

```sql
CREATE TABLE document_chunks (
    document_id uuid    NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index integer NOT NULL,
    char_start  integer NOT NULL,   -- 命中位置，用于告诉用户"在全文第 1200 字附近"
    char_end    integer NOT NULL,
    embedding   vector(384) NOT NULL,
    model       text    NOT NULL,   -- 换模型等于换向量空间，必须逐行记录
    UNIQUE (document_id, model, chunk_index)
);
CREATE INDEX ix_document_chunks_embedding
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
```

所以检索能定位到"这篇文章第 1200 字附近讲过这个"，展示仍然只给标题、摘要和原文链接。
**这张表不能被用来还原文章，这是设计目标，不是缺陷。**

### 三个绕不开的选择

**为什么必须本地模型。** 把全文 POST 给第三方 embedding API，法律性质就从"我们自己存储"
变成"向第三方提供作品"，等于把 rights_mode 那道闸门绕过去了。本地推理不产生任何对外传输。

**为什么用 ONNX 而不是 sentence-transformers。** 同一个权重，torch 要多装 2 GB 以上。
这个项目只需要"文本进、向量出"，`onnxruntime` + `tokenizers` 一共几十兆。
模型选 `intfloat/multilingual-e5-small`（384 维）：语料是 981 英 / 450 中混排，
纯中文模型（如 `bge-small-zh`）嵌英文会失真，而 `bge-m3` 的 ONNX 要 2 GB 以上。

**为什么模型缺失时必须报错。** 降级成随机向量或字符哈希，检索会"有结果但都是错的"，
比直接失败难发现得多。所以模型没就绪时 `/documents?mode=vector` 直接返回
`503 embedding_unavailable`，不会悄悄退回关键词。测试用的 `HashingEmbedder`
只能通过 `EMBEDDING_BACKEND=hashing` 显式开启，且模型名会写进 `document_chunks.model`，
混进真索引能被 `select distinct model` 查出来。

### 索引在抓取那一刻建立，不在事后

`metadata_excerpt` 的源走完 `_store_text` 之后库里只剩 600 字，几万字随函数返回就被回收。
**那是原文唯一存在过的时刻**，所以 `run_enrich` 在提交之后立刻建索引；`make embed`
只能给"库里还留着全文"的源（阮一峰、arXiv、HN 抓到的正文）补课。

这条区分决定了索引质量的瓶颈：**不在模型，在你手上还有没有原文。**

### 用法

```bash
# 1. 下载模型（470 MB，官方站点在部分网络下 502，脚本默认走国内镜像）
python scripts/fetch_embedding_model.py
python scripts/fetch_embedding_model.py --check   # 只检查不下载

# 2. 用已入库正文补索引（存量补课；新条目随 enrich 自动索引）
cd apps/api && python -m app.cli embed --limit 500
#    换模型后必须重算：python -m app.cli embed --reindex

# 3. 检索
curl "localhost:8000/api/v1/documents?q=乌兰察布的数据中心&mode=vector"
curl "localhost:8000/api/v1/documents?q=乌兰察布的数据中心&mode=hybrid"
```

`mode` 三档：`keyword`（默认，ILIKE）／`vector`（纯向量）／`hybrid`（RRF 融合）。
命中结果多一个 `match` 字段：

```json
"match": { "mode": "vector", "similarity": 0.917, "char_start": 1215 }
```

### 语义检索不支持游标翻页

时间排序有稳定的游标断点（`(published_at, id)` 全序），相似度排序没有：第 2 页的
相似度断点和第 1 页的游标不是同一个坐标系。所以 `mode=vector` 带 `cursor` 会直接
返回 `400 cursor_with_vector_mode`，而不是编一个看起来能翻页、实际会漏数据的游标。

### 实测

| 指标 | 数值 |
|---|---|
| 可索引文档（正文 > 400 字） | 737 |
| 已索引文档 | 737（100%） |
| 已索引块 | 8,714 |
| 每篇块数 | 平均 11.8，最多 32，最少 2 |
| 块大小 | 400 字 / 重叠 80 字（`chunk_text`，按句子边界断） |
| 只索引到 600 字片段 | 338 篇（授权级别所限） |
| 全量重建耗时 | 约 23 分钟（CPU 单进程，约 0.18 秒/块） |
| 正文原句查询 | `keyword` 0 条 → `vector` 1 条（目标文档，similarity 0.917，`char_start=1215`） |
| 专有名词查询 | `keyword` 命中标题第 1 名，`hybrid` 把它抬到融合结果第 2 位 |

**已知短板（实测，不是推测）**：口语化提问能召回但 top-1 排不准，前 3 名相似度差只有 0.008。这套检索解决"找得到"，不解决"排得准"——产品上应把它当召回层，需要精排就在上面叠 rerank。

**换模型必须带 `--reindex`**：`replace` 只删当前模型名下的块，旧模型的向量不会被任何写入路径带走，会永久留在表里（覆盖率虚高、存储白占）。`--reindex` 会先调 `purge_stale_models` 清掉并打印数量。

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
- 关键词检索为 `ILIKE`（标题 + 摘要）；正文层的召回由向量检索补上（`mode=vector|hybrid`，
  见「向量化检索」）。中文分词仍未做，`tsvector` 只用了 `simple` 配置。
- **中文线里只有阮一峰能存全文**（CC BY-NC-ND 3.0，390 篇、均值 6143 字）。36氪 走
  `rights_mode=metadata_excerpt`，正文只用于算 SimHash 指纹、不进 `content_text`；InfoQ 中文
  与少数派只授权了 RSS 摘要，所以进了 `NO_FETCH_SOURCES`。InfoQ 英文 `405`、OpenAI `403`
  （Cloudflare，换 UA 实测无效）是另一类问题——抓不到，不是不该抓。
- 「没有代码效力的合规要求等于没有要求」：这条曾经真实发生过——`rights_mode` 列和
  `SourceSpec.from_source()` 的传参都缺失，所有源都吃 dataclass 默认值，「全文不入库」只是
  `rights_note` 里的一句注释。现在判据在字段里，落库长度由 `content_service._store_text` 执行。
- Hacker News 本身不提供摘要（外链故事的正文在别人站上）。阶段 3 的 `make enrich` 会按热度抓外站正文，抓到后按「页面描述 > 正文首段」补 `summary`，覆盖率从 11% 提到 64%。剩下的 36% 卡在两处：**本机代理对部分域名稳定 502**（reuters / ft / guardian / youtube），以及站点反爬 403/405。前者是运行环境问题，把代理切到全局模式后重抓可以救回一部分；后者无解。
- 抓取失败按原因重试：环境性错误（代理 502 / 超时 / 5xx / 429）退避 6h → 24h → 72h 后自动回到候选，确定性错误（4xx、非 HTML）直接记满次数不再重试。**代理规则改好之后不必手动改库**，等退避到期即可；想立刻重试就把对应记录的 `content_attempts` 置 0、`content_fetched_at` 往前挪。
- 采集不会在同一分钟内补摘要：`make sync` 只拿元数据。`make sync-enrich N` 或 beat 里的 `enrich-hot-batch`（每 30 分钟一批 20 条）负责补，所以新条目最迟半小时有摘要。
- 真实语料里还没有「转载正例」：判重阈值（汉明 10 / Jaccard 0.35）的支撑目前来自构造数据的单测。753 条真实指纹里跨域相似组合为 0，第一轮跑出的 2 条「转载」是 arXiv 模板文字造成的假阳性，已由跨域约束修掉。接入更多中文媒体源之后需要重新校准。
- GitHub Release Notes 里的 `**` 强调标记刻意不清洗：去标记的正则会把 `**kwargs`、`__init__`、`use_cache` 这类标识符改坏，而它们正是版本说明的主要信息载体。排版层渲染前需自行处理。
- arXiv 的分页与窗口已在真实接口上验证（翻 6 页取 300 条约 46 秒、未触发 429），但限流是概率性的：真被限流时整轮抛 `RateLimitedError` 降级，不会写入半截批次。
- arXiv 自带的分类（`cs.AI` / `cs.LG`）未写入 `topics`：它与项目的主题词表是两套体系，混用会让前端按主题筛选时出现只有论文才有的 slug。要按 arXiv 分类筛选需要单独加字段。
- 关键词分类器对论文标题几乎没有区分度：实测 50 篇 arXiv 论文全部判为 `ai-models`。这是阶段 5 换模型分类的直接动因。
- DeepMind、TechCrunch 已注册但默认停用，尚未做健康检查。
- GitHub 分页上限 3 页（60 条）。两轮之间新增超过 60 个版本时仍会出现空档，实际不会发生；真发生只能靠人工补。
- 未做 user/鉴权（单用户本地版），`/admin` 端点暂无权限校验。

## 参考

工程手册见 `执行摘要.pdf`（数据源清单、评分公式、分阶段验收标准）。
