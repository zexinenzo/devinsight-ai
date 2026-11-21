# DevInsight AI

科技产业情报平台：从中国与全球公开、合规的数据源持续采集科技新闻、官方发布、开源动态与论文，
把原始信息转化为可检索、可追溯的情报。

当前进度：**阶段 0–7 已跑通**——工程骨架 / 来源注册与 RSS / 多源 API /
正文补全与去重 / 向量化检索与精排 / 实时搜索 / Agent 研究助手 / 实体时间线。
每个阶段都有实测记录，指标与边界都写在对应章节里，没有实测支撑的说法一律标注为「未验证」。
手册 14.1 要求的「第一条真实数据闭环」在阶段 3 已达成：

```
11 个启用源（6 RSS + HN + 3 GitHub 仓库 + arXiv）
  -> Connector 抓取 / 解析 / 清洗 / 分类 / 增量
  -> PostgreSQL documents（幂等入库）
  -> engagement 回填热度 -> enrich 按热度抓正文 + SimHash -> dedup 判转载 / 聚事件
  -> FastAPI GET /api/v1/documents（支持 sort=engagement）
  -> FastAPI GET /api/v1/evidence（本地证据 + 实时证据 + 可信度画像）
  -> FastAPI GET /api/v1/research（模型规划 -> 调工具 -> 带引用作答，SSE 流式）
  -> Next.js /intelligence 情报流（按时间 / 按热度、事件簇、抓取状态）
```

## 技术栈

Next.js 16 + React 19 + TypeScript ｜ FastAPI + SQLAlchemy 2 + Alembic ｜
PostgreSQL 16 + pgvector ｜ Redis + Celery ｜（后续阶段）LangGraph + LLM API

## 快速开始

前置：Docker、Python 3.14、Node.js 22+。依赖已装在仓库根 `.venv` 与 `apps/web/node_modules`。
Python 固定在 **3.14**，不是「3.12+」：`requirements.txt` 就是在这台 Linux venv 上 freeze 出来的，
里面带 `uvloop` / `librt` 这类 Linux-only 包；`onnxruntime` / `tokenizers` 的 wheel 又按 CPython
主次版本切分。换个版本不会立刻报错，而是等到某天向量检索 503 才发现。重建方式见下方「环境与 CI」。

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
cd apps/api && ../.venv/bin/python -m app.cli sync --source hackernews_en --enrich 40

# 5. 启动服务（两个终端）
make api           # http://localhost:8000/docs
make web           # http://localhost:3000/intelligence

# 可选：让整条链路自己转（worker + beat 都起来之后，上面 3/4 步不再需要人手跑）
make worker        # Celery Worker
make beat          # 见下方「自动跑的三件事」
```

### 环境与 CI

```bash
make venv    # 重建 .venv：python3.14 + requirements.txt，一步到位
make lint    # ruff check .（全仓，与 CI 同一条命令）
make test    # 后端单元测试，220 个用例，不需要数据库
```

四条约束，都是踩过之后写下来的：

**1. 这台机器没有 `python` 命令。** Ubuntu 26.04 只给 `python3`，`python` 是个空名（除非装
`python-is-python3`）。所以本文所有命令一律写 `.venv/bin/python`（在仓库根）或
`../.venv/bin/python`（在 `apps/api` 下），没有一条依赖 `python` 这个名字——照抄即可，不会
撞上 `Command 'python' not found`。

**2. 只在 WSL 里跑这套 Python。** `.venv/bin/python3.14` 是 Linux 的 ELF 可执行文件，Windows 侧的
python 执行不了它；反过来，用 Windows 的 python 去跑这份代码时，`asyncpg` / `uvloop` 这类包在
Windows 上根本装不上，测试能过只是因为它们恰好被 mock 掉了。**跨系统跑同一份代码，"过了"不代表
环境是对的。**

**3. ruff 规则集写在仓库里，不依赖本机版本。** 之前没有 `ruff.toml`，规则集完全取决于装的 ruff 版本：
0.17.5 的默认集带 `B` / `BLE` / `SIM` / `FURB` / `UP` / `I`，更早的版本只有 `E4/E7/E9/F`。
同一份代码在两台机器上「干净」与否可以不同，CI 更是无从复现。现在 `ruff.toml` 显式 `select`
规则集、`requirements.txt` pin 死 `ruff==0.17.5`、CI 启动时打印版本——三者任何一处漂了都能当场看见。

**4. CI 只跑 lint + 单测，不跑检索评测。** `.github/workflows/ci.yml` 是两条命令，15 分钟超时。
`scripts/eval_retrieval.py` 刻意没进去：它需要 pgvector + 1431 篇真实文档 + 736 MB 本地模型，
单次 6 分钟起。把它塞进 CI 只会得到一个天天红、然后被所有人忽略的门禁。指标靠本地跑 +
把结果写进 README，不靠 CI 兜。

### 配置 Agent（阶段 6）

`/api/v1/research` 需要一个 OpenAI 兼容的模型端点。**不配也能跑其余所有接口**，
它单独返回 503——和 embedding / rerank / web_search 一样，能力缺失显式报错。

```bash
# .env
LLM_PROVIDER=openrouter                      # 只是日志标签
LLM_BASE_URL=https://openrouter.ai/api/v1    # 真正决定打到哪里
LLM_API_KEY=sk-...
LLM_MODEL=deepseek/deepseek-v4.1-flash
```

做这个分离是有原因的：如果按 `provider` 隐式映射 URL，写错 provider 名会得到一个
连接超时，看起来像网络问题，实际是配置错了。宁可让 `base_url` 显式填。

实测可用（2026-11，都是各家文档原文）：

| 端点 | `LLM_BASE_URL` | 模型 |
|---|---|---|
| OpenRouter | `https://openrouter.ai/api/v1` | `deepseek/deepseek-v4.1-flash` |
| 智谱 GLM（免费档） | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| DeepSeek 官方 | `https://api.deepseek.com/v1` | `deepseek-chat` |

验证一次：

```bash
curl "localhost:8000/api/v1/research/limits"       # ready=true 说明配好了
curl -N "localhost:8000/api/v1/research?q=OpenAI%20最近发布了什么&max_turns=3"
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
| GET | `/api/v1/documents` | 情报列表：`q/source/region/language/topic/content_type/from/to/sort/limit/cursor/mode/rerank` |
| GET | `/api/v1/documents/{id}` | 文档详情 |
| GET | `/api/v1/evidence` | 证据集：`q/days/local_limit/web_limit/include_web/mode`，返回本地 + 实时证据与 `verification` 画像 |
| GET | `/api/v1/research` | Agent 研究：`q/max_turns/max_tool_calls/max_web_calls/max_runtime_seconds`，SSE 返回阶段事件与带引用的答案 |
| GET | `/api/v1/research/limits` | Agent 的四档预算与模型状态（供前端在发起前展示） |
| GET | `/api/v1/meta/topics` | 六大领域字典 |
| GET | `/api/v1/meta/content-types` | 内容类型分布（只列库里真实存在的类型，带条数） |
| GET | `/api/v1/stats` | 总览统计（按来源/地区/领域） |

统一响应：`{ data, meta: { request_id, timestamp, total, next_cursor }, error }`。

## 目录结构

```
apps/api/            FastAPI 后端
  app/core/          配置、日志、错误
  app/db/            模型（sources / ingestion_runs / documents / document_chunks / source_health_logs）、会话
  app/connectors/    Connector 抽象、registry 分派、RSS / HN / GitHub Release / arXiv 实现、来源种子
  app/services/      采集、来源、文档查询、切块与向量化、精排、规则分类
  app/api/v1/        路由
  app/worker/        Celery 应用与任务
  migrations/        Alembic（0001_init … 0008_document_chunks）
  tests/             单元测试（URL/时间/文本/分类/切块/检索/重排）与各 Connector 合约测试
  data/              评测集（eval_questions.json，40 题四类）
scripts/             模型下载（embedding / reranker）、检索评测、历史回填
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
| Hacker News | `connectors/hackernews.py` | `seen_ids` 集合（榜上未见才抓），截断按**榜单序（热度）** | 无公布配额 |
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
../.venv/bin/python ../scripts/backfill_ruanyifeng.py                                  # 全量回填（幂等，0.4s/请求）
BF_START=2026-10 BF_END=2026-11 ../.venv/bin/python ../scripts/backfill_ruanyifeng.py  # 试跑两个月
../.venv/bin/python ../scripts/backfill_ruanyifeng.py --summaries-only                 # 改摘要规则后只重算，不重抓
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

`q` 参数匹配的是标题和摘要。实测一句只出现在正文第 1,500 字的话
（出自《每周分享第 46 期》，全文 9,670 字）：

```
GET /api/v1/documents?q=流浪猫只需要刷脸就能进入猫窝 它能准确识别174种不同种类的猫咪
  mode=keyword → 0 条
  mode=vector  → 3 条，目标文档排第 1（相似度 0.917，char_start=1215）
  mode=hybrid  → 3 条，目标文档排第 1（相似度 0.917）
```

这不是模型问题，是输入问题：标题摘要里没有的字，`ILIKE` 永远搜不到。片段兜底
（把问句切成二元组）能救一部分，但这句话切成片段后在全库里命中几千条，
排不到前面——**字面匹配在长正文上结构性地失效**，这正是向量检索存在的理由。

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
.venv/bin/python scripts/fetch_embedding_model.py
.venv/bin/python scripts/fetch_embedding_model.py --check   # 只检查不下载

# 2. 用已入库正文补索引（存量补课；新条目随 enrich 自动索引）
cd apps/api && ../.venv/bin/python -m app.cli embed --limit 500
#    换模型后必须重算：../.venv/bin/python -m app.cli embed --reindex
#    只补标题摘要块（每篇一条，61 秒）：../.venv/bin/python -m app.cli embed --headers-only

# 3. 检索
curl "localhost:8000/api/v1/documents?q=乌兰察布的数据中心&mode=vector"
curl "localhost:8000/api/v1/documents?q=乌兰察布的数据中心&mode=hybrid"
#    不要精排（省 0.6 秒）加 &rerank=false；要求必须精排加 &rerank=true
```

`mode` 三档：`keyword`（默认，ILIKE）／`vector`（纯向量）／`hybrid`（RRF 融合）。
命中结果多一个 `match` 字段：

```json
"match": { "mode": "hybrid", "similarity": 0.917, "char_start": 1215, "rerank_score": 4.31 }
```

`rerank_score` 是 cross-encoder 给的原始 logit，只用于解释排序，**不要当概率看**：
int8 量化会改变它的绝对量级，跨模型、跨语料都不可比。响应 `meta.reranked` 说明这次
排序有没有真的经过精排——模型没下载时它是 `false`，而不是让调用方以为排过。

### 语义检索不支持游标翻页

时间排序有稳定的游标断点（`(published_at, id)` 全序），相似度排序没有：第 2 页的
相似度断点和第 1 页的游标不是同一个坐标系。所以 `mode=vector` 带 `cursor` 会直接
返回 `400 cursor_with_vector_mode`，而不是编一个看起来能翻页、实际会漏数据的游标。

### 实测

| 指标 | 数值 |
|---|---|
| 可索引文档（正文 > 400 字） | 737 |
| 已索引文档 | 737（100%） |
| 正文块 | 8,714 |
| 标题摘要块 | 1,431（每篇一条，`chunk_index = -1`） |
| 每篇块数 | 平均 11.8，最多 32，最少 2 |
| 块大小 | 400 字 / 重叠 80 字（`chunk_text`，按句子边界断） |
| 只索引到 600 字片段 | 338 篇（授权级别所限） |
| 全量重建耗时 | 正文约 23 分钟（0.18 秒/块），标题块 61 秒 |
| 重排耗时 | 40 条候选约 0.6 秒（15 ms/条） |
| 正文原句查询 | `keyword` 0 条 → `vector` 1 条（目标文档，similarity 0.917，`char_start=1215`） |
| 专有名词查询 | `keyword` 命中标题第 1 名，`hybrid` 把它抬到融合结果第 2 位 |

**已知短板（实测，不是推测）**：`topic` 类 `R@5` 补池后只有 0.340（补池前 0.674，见下节），三分之二主题问题凑不齐前 5 位。精排能补一部分排序，但补不了召回——评测中曾有 10/24 篇标准答案落在前 50 名之外，那种缺口任何重排都救不了，只能靠新增召回信号（标题通道就是这么加上去的）。

**换模型必须带 `--reindex`**：`replace` 只删当前模型名下的块，旧模型的向量不会被任何写入路径带走，会永久留在表里（覆盖率虚高、存储白占）。`--reindex` 会先调 `purge_stale_models` 清掉并打印数量。

### 检索质量：40 题评测集

`apps/api/data/eval_questions.json` 是 40 道题，四类各测一种能力：`fact`（单篇具体事实）、`topic`（多篇主题）、`named`（专有名词，这一档 keyword 本该赢）、`en`（英文，单独报告不与中文混算）。

```bash
.venv/bin/python scripts/eval_retrieval.py                              # 三种模式全跑
.venv/bin/python scripts/eval_retrieval.py --mode hybrid --no-doc-agg    # 关掉聚合路做 A/B
.venv/bin/python scripts/eval_retrieval.py --mode hybrid --no-rerank     # 关掉重排做 A/B
.venv/bin/python scripts/eval_retrieval.py --verbose                     # 看未命中明细
```

实时搜索的价值用另一套指标量（答案每天变，R@5 不适用）：

```bash
.venv/bin/python scripts/eval_evidence.py --local-profile   # 本地时效画像，不联网
.venv/bin/python scripts/eval_evidence.py --lang zh         # 只跑中文问题（消耗额度）
.venv/bin/python scripts/eval_evidence.py --replay          # 回放录制响应，不消耗额度
```

| 模式 | R@5 | R@10 | MRR | nDCG@5 |
|---|---|---|---|---|
| keyword | 0.460 | 0.550 | 0.444 | 0.426 |
| vector | 0.788 | 0.826 | 0.804 | 0.750 |
| **hybrid** | **0.820** | **0.820** | **0.871** | **0.814** |

分类型看，`hybrid` 在 `fact` 上全项 1.000、`named` 0.833、`en` 0.744，最弱的一档是 `topic` 的 `R@5 = 0.674`——主题类问题仍有三分之一凑不齐前 5 位。

**但这张表的分母是偏的，上表是补池前的数。** 标准答案是人看着检索结果标的，一篇高度相关却从未被六路召回的文档不在标注集里，就永远不算漏检——`R@5` 系统性偏高，且偏高多少不可知。按 IR 标准做法做了补池（六路 top-20 并集 → 1767 条逐条判定，人工抽检一致率 89.9%、分歧以人判为准），最终新增 42 条相关项（59 → 101），重测：

| 类型 | R@5 补池前 | R@5 补池后 |
|---|---|---|
| `fact` | 1.000 | 0.910 |
| `topic` | 0.674 | **0.340** |
| `named` | 0.833 | 0.833 |
| `en` | 0.744 | 0.689 |
| **合计** | **0.820** | **0.678** |

`MRR` 不变（0.879），`nDCG@5` 0.824 → 0.722。掉分不是系统变差，是尺子变严：补池只增加相关项，分母涨了而分子几乎不动。42 条新增里 38 条在 `topic`（抽检中 8 条模型判宽的分歧也全在 `topic`）——**人标主题题时只标"最像的那个答案"，这正是 `topic` 类长期偏高的机制**。补池修不了池外盲区（六路并集只覆盖全库 47%），这一项如实留在局限里。

```bash
.venv/bin/python scripts/pool_eval_candidates.py     # 六路取并集，约 8 分钟
.venv/bin/python scripts/pool_judge.py export        # 判定工作表（不含名次、按 md5 排序）
.venv/bin/python scripts/pool_judge.py merge --labels apps/api/data/pool_labels
.venv/bin/python scripts/eval_retrieval.py --mode hybrid --qrels apps/api/data/eval_pool_labelled.json
```

这套评测量出并修掉了三个真问题：

1. **keyword 对自然语言问句是结构性失效**（`R@5` 只有 0.100）。根因是整句 `ILIKE '%一句话%'`——14 个字的问句不可能整体出现在标题里，注定 0 结果，与"正文没索引"无关。加上片段兜底（中文二元组均匀采样 + 命中数阈值）后升到 0.460。
2. **单块信号对主题型是噪声**（`topic` 只有 0.465，而 `fact` 有 0.923）。补一路"文档级前 3 块平均距离"信号参与 RRF，`topic R@10` 从 0.583 到 0.694。
3. **召回缺口靠标题通道补，不靠精排**。诊断发现 `topic` 类 24 篇标准答案里 10 篇（42%）根本不在前 50 名内——这种缺口重排完全救不了。给每篇文档的标题+摘要单独建一条向量块（`chunk_index = -1`）后，池外从 10 篇降到 3 篇。

## 精排（cross-encoder）

召回用的是双塔模型：文档在入库时就被压成向量，之后再也看不到 query。384 个浮点数装不下"这句话到底在问什么"，实测里前 3 名相似度只差 0.008。精排换成 `bge-reranker-base` 的 int8 ONNX（266 MB），把 query 和候选拼成 `<s> query </s></s> passage </s>` 送进同一注意力窗口，直接输出相关性分数。

```bash
.venv/bin/python scripts/fetch_reranker.py    # 266 MB，走国内镜像
curl "localhost:8000/api/v1/documents?q=开源对一个国家意味着什么&mode=hybrid"
# meta.reranked = true，每条的 match.rerank_score 是原始 logit
```

**关键设计：重排结果作为第五路参与 RRF，不直接接管排序。** 这一点是踩坑换来的——让重排直接覆盖顺序时，原本排第 1–3 的正确答案被压到 6–11 名（`t06` 1→9、`t09` 2→10、`t03` 3→11），`topic R@5` 从 0.625 掉到 0.528。原因是单个片段的证据不足以判断"整篇在讲什么"，而它一旦判错就全错。改成 5 路投票后判错会被其他 4 路投回来：

| 版本 | R@5 | R@10 | MRR | nDCG@5 |
|---|---|---|---|---|
| 两路（keyword + 单块向量） | 0.767 | 0.767 | 0.789 | 0.739 |
| 三路（+ 文档聚合） | 0.767 | 0.826 | 0.789 | 0.739 |
| 四路（+ 标题摘要） | 0.805 | 0.820 | 0.856 | 0.794 |
| 五路（+ 重排**接管**排序） | 0.776 | 0.820 | 0.849 | 0.776 |
| 五路（+ 重排**投票**） | **0.820** | **0.820** | **0.871** | **0.814** |

**喂给模型的文本是三段拼接：标题 + 摘要 + 命中块原文。** 少一段就掉一类查询：只用标题+摘要，`fact R@5` 从 1.000 掉到 0.923（答案那个数字在正文深处，摘要里没有）；只用标题+命中片段，`topic R@5` 从 0.625 掉到 0.507（片段是"某一处提到"，丢了整体主题）。

命中块原文不需要额外存储：`documents.content_text` 本来就在库里，块的位置 `char_start` 也在，按字符区间切出来就是它。`document_chunks` 不存原文是为了不重复存同一份正文，不是拿不到。

**评测集的边界要说清楚**：ground truth 是从标题筛出来再人工复核的，没做 pooling（把各系统 top-10 取并集再人工判定），所以漏标的正确答案会被算成"未命中"。这些数字**用于比较同一系统不同版本是可靠的，不能当绝对水平引用**。

## 实时搜索（阶段 5）

### 问题：本地库能答「库里有什么」，答不了「今天有什么」

阶段 4 解决的是「正文里的字搜不到」，但另一条边界一直在：**库里只有采集过的源**。
11 个启用源的时效画像（`.venv/bin/python scripts/eval_evidence.py --local-profile`，不联网）：

```
source                     tier  docs  last7     newest
hackernews_en              C      655    655         0d
36kr_zh                    B       30     30         0d
sspai_zh                   B       10     10         0d
infoq_en                   B       16     16         0d
infoq_zh                   B       20     20        -1d
openai_news_en             A      200     23         1d
arxiv_cs_ai                B       50     50         1d
github_release_vllm        A       20      1         2d
github_release_transformers A      20      1         5d
ruanyifeng_zh              A      390      1         6d
github_release_ollama      A       20      0         8d
```

看起来不差——只有 1 个源超过 7 天。但把中英分开，真正的差距出来了：

| | 存量 | 最近 7 天 | 占比变化 |
|---|---|---|---|
| 中文 | 450（31.4%） | 61（7.6%） | **降到四分之一** |
| 英文 | 981 | 746 | 升到 64% |

中文占存量的三成，却只占增量的不到一成。**不是没覆盖，是在变旧**——4 个中文源
里 390 条来自每周一篇的周刊，36氪 的 30 条是最近才开始采的。同一批时效性问题，
本地能给的最新证据：

| 查询 | 本地最新命中 | web |
|---|---|---|
| `OpenAI latest model release` | 0 天 | 1 天 |
| `大模型 发布` | 90 天 | 有窗口内结果 |
| `开源模型 更新` | 110 天 | 同上 |
| `AI 监管 政策` | 110 天 | 同上 |
| `芯片 出口 限制` | 171 天 | 同上 |

英文侧本地已经是当天（HN 每天几十条），中文侧是三个月到半年。排序再准也变不出
没入库的语料——这是阶段 5 存在的唯一理由。

### 三个「搜索 API 不可信」的具体形态

**1. 默认不给你时间。** `topic=general` 的响应里没有任何日期字段。同一句 query，
分数最高（0.79）的一条是 2023 年 GPT-4 开放 API 的旧闻——它确实相关，但拿它回答
「最新进展」就是错的，而且从响应里看不出它有多旧。`topic=news` + `days=7` 才带
`published_date`，且真的按窗口过滤（实测返回全部落在 3 天内）。所以默认走 news：
**拿得到日期比拿得到覆盖面重要**。

**2. 官方域名下有用户空间。** 实测结果里有一条 `community.openai.com`，标题写着
「GPT-6.2 Astra and Sol are now available - Announcements」。按域名后缀匹配它会命中
`openai.com` 被判成一手源，但它其实是用户论坛。拦截规则必须在大类匹配**之前**：

```python
_UGC_SUBDOMAIN_PREFIXES = ("community.", "forum.", "discuss.", "bbs.", "club.", "support.")
```

**3. 聚合站会转载一手稿。** 同一次搜索里，`finance.yahoo.com` 那条的内容来自
Reuters。按域名它是 C 档聚合站，按内容它是一手通讯社的报道。域名分级只能反映
「这个站点自己有没有采编能力」，不能反映「这一条的具体来源」——所以 `tier` 只用于
排序和展示，**响应里永远不出现「已核实」**。

### 不按混合分排序

本地分数是余弦相似度，web 分数是上游相关性，两者不在一个量纲。强行归一化成一个
"总分"看起来专业，实际是用未知冒充已知。这里改为显式优先级链：

```python
(date_verified, published_at, score)   # 无日期的排最后，不是按相关性往前挤
```

理由：一条不知道日期的结果无法承担「补充时效」这个用途。把它排到带日期的结果
前面，等于用未知冒充已知。

### 接口

```bash
# 完整证据集：本地 8 条 + 实时 8 条，附可信度画像
curl "localhost:8000/api/v1/evidence?q=OpenAI%20announcement&days=7"

# 只看本地（不联网，web_status=disabled）
curl "localhost:8000/api/v1/evidence?q=大模型&include_web=false"
```

```json
{
  "data": {
    "local": [{"channel": "local", "source_tier": "C", "freshness": "today", "char_start": 1215, ...}],
    "web": [{"channel": "web", "source_tier": "A", "published_at": "2026-11-16T08:00:00Z",
             "date_verified": true, "in_local": true, "freshness": "this_week", ...}],
    "verification": {
      "web_total": 3, "unique_domains": 2, "overlap_with_local": 1,
      "date_unverified": 0, "tier_counts": {"A": 1, "C": 2}, "mean_age_days": 1.7
    },
    "web_status": "ok"
  }
}
```

`verification` 里三个数字是给程序做判断用的，不是装饰：

- `unique_domains` 按**可注册域名**去重（`finance.yahoo.com` 与 `news.yahoo.com` 算一家），
  它回答「几家在说同一件事」；
- `overlap_with_local` 高说明所谓「新信息」大部分是已知内容被再次看到；
- `date_unverified > 0` 时，任何「最新」措辞都该降级成「近期」。

### 三种状态必须长得不一样

| 情况 | HTTP | 语义 |
|---|---|---|
| 要求实时证据但没配 provider | 503 `web_search_unavailable` | 这个部署没联网，改配置可解 |
| 配了但上游限流/超时/5xx | 502 `upstream_unavailable` | 上游此刻不行，等一会儿可能好 |
| `include_web=false` | 200 + `web_status=disabled` | 本来就没要 |

第一条不能返回「200 + 空 web 列表」：调用方会认为「今天全网没有相关新闻」，
而事实是这个部署根本没联网。这两种情况的运维动作完全相反。

### 诚实边界

- **keyless 是每日额度，不是每小时。** 上游 429 的错误码是 `daily_cap_reached`
  （约 30 次/天）。写文档时按第三方评测写成「每小时」，是跑出 429 才纠正过来的。
- **中文查询的真实补全能力还没验证。** 上面的中英对照里 web 一列来自**录制响应回放**
  （额度当天用尽），而回放不区分 query。所以「本地中文 90–171 天」是硬数据，
  「web 能补上中文」还不是。要有 key 之后跑 `--limit 8` 才能下结论。
- **没做事实核查。** `unique_domains` 是声源广度，不是「几家独立证实」。两个站
  转载同一篇通稿会被算成两个声源。
- **没做 pooling。** 评测指标用于比较同一系统的不同版本，不能当绝对水平引用。

完整推导见教程第 6 章：provider 契约怎么定（6.2）、为什么两边分数不能混排（6.6）、
域名分级与事实核查的边界（6.7）、错误码为什么必须分 503 与 502（6.8）。

## Agent 研究助手（阶段 6）

一个问题的完整流程：

```
GET /api/v1/research?q=本周 AI 领域有什么重要发布
  -> started        问题、模型、四档预算
  -> turn_started   第几轮，工具是否可用
  -> tool_call      调了 search_local / search_web，原始参数（模型生成的，可能是错的）
  -> tool_result    是否成功、耗时、累计证据（前端按 index 合并）
  -> answer         带 [编号] 的答案
  -> done           证据、引用校验、轮数、耗时、收尾原因
```

### 为什么自己写循环，不用 LangGraph

三个理由，按重要性排：

1. **控制流要看得见。** 预算检查、工具拒绝、强制收尾，这三件事发生在循环的
   具体位置。放进图里之后，读代码的人要先在脑子里把图展开，才能回答
   「配额用尽时会发生什么」。而这个项目最需要的恰恰是这件事能被一眼回答。
2. **依赖成本。** LangGraph 会带进 langchain-core 一整条链，而这里要的功能
   只有「循环 + 工具调用 + 状态记录」，`while` 加一个列表就够。
3. **测试要能注入。** 全套编排测试用桩 LLM 跑，23 条用例 2 秒跑完，不连模型。
   框架抽象层越厚，这种测试越难写。

### 三档预算，以及为什么是 5 轮

| 项 | 默认 | 超了会怎样 |
|---|---|---|
| `max_agent_turns` | 5 | 最后一轮禁用工具，强制作答 |
| `max_research_tool_calls` | 12 | 拒绝执行，把「配额用尽」作为 tool 消息回传 |
| `max_web_search_calls` | 4 | 同上，但只拦实时搜索（它花额度） |
| `max_agent_runtime_seconds` | 180 | 剩余调用全部拒绝，再请求一次收尾 |

**拒绝也是要回传的。** 模型请求了工具，如果直接跳过，下一次请求会因为
「assistant 请求了工具却没有结果」被上游拒掉。所以每条被拒的调用都要补一条
`role: tool` 的消息说明原因——模型看到「配额用尽」会停下来，看到异常不会。

5 轮不是拍的：一轮对应一次模型请求，而免费档实测 RPM 5。再大就会在单次研究
内部撞 429。换付费档后调 `MAX_AGENT_TURNS` 即可。

### 为什么不用流式调模型

流式下 `tool_calls` 的 `arguments` 是分片下发的，要自己累积再拼 JSON。拼错的表现
是工具参数缺字段，报错位置却在工具内部。而用户真正想看的「过程」是阶段事件
（在查什么、拿到几条），这些由我们自己发，不依赖 token 流。所以最终答案一次性
返回，SSE 里作为单个 `answer` 事件推出。

### 引用校验：让「编证据」可被发现

答案里的每个 `[n]` 都会和证据池比对，返回三个数：

```json
{"cited": [2,3,4], "unknown": [7], "coverage": 0.32, "evidence_total": 19}
```

- `unknown` 非空 = 模型引用了不存在的编号。**不静默修正**——改答案就等于掩盖错误，
  只记日志并在 `done` 里暴露出来。
- `coverage` 的分母是「提供过的全部证据」，不是「答案里出现的编号」（后者恒为 1）；
  覆盖率低说明检索给的多余，或证据太长模型读不完。

实测三次运行：`unknown` 全为空，`coverage` 分别 1.00 / 0.35 / 0.32。
覆盖率差异来自查询：问题越宽（「本周有什么发布」），命中的无关周刊文章越多。

### 性能：从 87 秒到 25 秒的三次修正

第一版跑一个问题要 **87 秒**，其中检索占了大头。三次定位：

| 问题 | 现象 | 修正 |
|---|---|---|
| 精排候选沿用列表页的 40 条 | 每次检索 5.8 秒，一次研究调 4 次 | Agent 传 `rerank_candidates=12` → 1.7 秒 |
| ONNX padding 宽度按「本批最长」动态取 | **同一 query 1.6 秒，换一个 query 12 秒** | 宽度吸附到 5 个固定档位，内存规划只发生 5 次 |
| 证据池上限 16 | 第一次检索 `limit=10` 就占掉大半，后续 `dropped: 31` | 上限提到 20，并在工具结果里说明「另有 N 条未纳入」 |

第二条最值得记：ONNX Runtime 的 memory pattern 按输入 shape 缓存，shape 每次
变一点就等于每次重新规划。**症状是「慢」，但真正的线索是「重复同一请求很快」**——
只测一次或只测同一 query，永远发现不了。

最终：3 轮 4 次工具 **17.8 秒**，4 轮 4 次工具 **24.9 秒**。

### 踩到的三个真 bug（都是「单测全绿、真跑才炸」）

1. **`verification.independent_sources` 不存在**，实际字段是 `unique_domains`。
   按记忆写字段名，AttributeError 被工具层的兜底捕获成 `ok=False`，
   编排测试全绿——因为那些测试用的是假工具，绕过了 handler 本身。
   补了两条直接调 handler 的测试。
2. **`max_turns or 默认值` 会把显式传入的 `0` 吞成 180 秒**。用 `is None` 判断。
3. **`elapsed_ms` 全是 0**，因为异常路径返回的是新构造的对象。
   修 bug 的过程中先做了「把 `content` 透到事件里」，才看见真正的错误信息。

### 诚实边界

- **模型只能回答证据内的问题。** 实时额度耗尽时它会说「无法回答，缺少 X」，
  而不是编一段。这是 prompt 约束住的，不是模型自觉——实测三次都遵守。
- **免费档 RPM 5 是硬约束。** 默认 5 轮刚好卡在边缘，并发两个研究会互相挤。
- **`llm_*` 不配就 503，其余接口不受影响。** 和 embedding / rerank / web_search
  一致：能力缺失显式报错，不静默降级。
- **没有做多轮对话。** 一次研究是一个独立的问答，不保留上文。要做对话式，
  得先解决历史裁剪——上下文一长，证据就得让位。

完整推导见教程第 7 章。

## 实体时间线（阶段 7）

把文档拆成「谁在什么时候做了什么」，落进 `entities` 与 `fact_claims` 两张表。
它不是检索引擎的补充，是另一种索引：文档索引回答「哪篇文章相关」，事实索引回答
「这家公司做过什么」。

```bash
make extract                                            # 默认 5 篇 dry-run
make extract ARGS="--limit 20 --keyword GPT --dry-run"  # 抽一批看质量
make extract ARGS="--limit 500 --apply"                 # 写库；幂等，可中断再跑
```

**三道闸门，缺一条就不入库：**

1. **证据逐字可核验**——模型给的原文片段会跟原文做子串比对，分 exact / partial / missing。
   partial 与 missing 不为 0 就不要 `--apply`。
2. **谓词闭集**——12 个谓词写死在 `timeline_service.PREDICATES`，落库前再校验一次。
   模型自造 `launched` / `推出` 会被拒。
3. **日期三档**——`stated`（文中明说）/ `published`（发文时间兜底）/ `unknown`。
   兜底必须在响应里显式标出，不能让人把「我们看到的时间」当「事情发生的时间」。

**500 篇实测**（`--limit 500 --apply`）：抽出 238 条（模型原始输出 241 条，2 条自反、
1 条宾语过长被拒），入库 148 条，重复 90 条，无事实 342 篇，抽取失败 13 篇
（全为 max_tokens 截断），非明说日期 167 条，**证据 exact/partial/missing = 238/0/0**。

### 合并：同一实体的多种写法

`normalize_key` 不做跨语言对齐，`NVIDIA` 和 `英伟达` 必然是两行，两行各有各的事实。
别名解决「用哪个名字能命中」，不解决「命中之后拿到谁的事实」——所以用指针合并：

```bash
.venv/bin/python scripts/repair_facts.py merge --id <被并的行> --into <根> --dry-run
.venv/bin/python scripts/repair_facts.py merge --id <被并的行> --into <根>
.venv/bin/python scripts/repair_facts.py merge --id <被并的行> --undo
```

**不改 `fact_claims.subject_id`**：触发器直接拒改，而且改完就再也查不到「这条事实当初
挂在哪种写法下」。指针可逆，事实一行不动。查询侧 `subject_id IN (组)`，沿 `merged_into`
向上找根、向下收写法，深度上限 8 防环；结果里会点名合并了哪几种写法。

### 修复：撤回，不删除

```bash
.venv/bin/python scripts/repair_facts.py audit                        # 只读，按可疑程度分三档
.venv/bin/python scripts/repair_facts.py audit --format csv --out .tmp/audit.csv
.venv/bin/python scripts/repair_facts.py retract --id <uuid> --reason "…"   # 加 --dry-run 预演
.venv/bin/python scripts/repair_facts.py retract --id <uuid> --undo         # 恢复
```

`audit` 只做 `SELECT`。判定与执行必须分开——判定结果一旦能直接落库，就没有第二双眼睛看过它。

**撤回不是删除。** `retracted_at` 表达「我们不再声称这条」，`valid_to` 仍然只表达「这件事
在现实世界里什么时候不再成立」。两个轴混用会让 `as_of("2025-11-19")` 系统性撒谎。查询层
一律过滤撤回项，但 `retracted_count` 必须报出来——**内容不露、数量要露**。

全量 270 条审计结果：① 形状不符（宾语是许可证）3 条、② 宾语槽位错位（`founded` 填人名、
`funded` 填投资方）8 条、③ 证据指证谓词 1 条。① 全部撤回；② 里 4 条 `founded` 填人名的
已按同一判据撤回，其余 4 条 `funded` 待人工；③ 那 1 条事后证明是**假阳性**——原文写的是
「原CFO Colette Kress 接任临时CEO」，`原CFO` 恰恰是「曾经是 CFO」，规则把方向读反了。
**能机械判定的刚过 1%**，所以判错的代价必须接近零，这是选撤回而不是删除的唯一理由。

**冲突数不等于错误数。** 实体合并后 `英伟达` 一次报出 18 条冲突，逐条读原文后只有 1 条是真错：
15 条是 `partnered_with` 的多值谓词噪音，2 条是同一件事的中英文重复，1 条是 `valid_to` 缺失
（黄仁勋卸任、Kress 接任临时 CEO，两条都真但都写着「至今」）。这个数字本身不区分这四种性质。

### 闭合：时间轴的右半边

```bash
.venv/bin/python scripts/repair_facts.py supersede --list                     # 谁该被闭合（只读）
.venv/bin/python scripts/repair_facts.py supersede --id <旧> --by <新> --dry-run
.venv/bin/python scripts/repair_facts.py supersede --id <旧> --by <新>
.venv/bin/python scripts/repair_facts.py supersede --id <旧> --by <新> --undo
```

`--list` 不是方便，是必须：`--id` 与 `--by` 抄反了**不会报错**——两条记录都合法，
脚本照样打印「已闭合」，只在某个历史时点让 `as_of` 答错一天。候选对因此由脚本算：
按合并组聚合（合并只写指针，`英伟达`/`Nvidia` 那对 CEO 仍挂在两个 id 上）、只取单值
谓词（`partnered_with` 六条会产出 15 条假候选）、只配相邻两条（三条依次在任要闭合
`(A,B)` 与 `(B,C)`，拿 A 对 C 会让中间那段任期消失）。

`valid_to` 从建表起就在表里、触发器也一直允许改，**但没有任何写入方**——于是每条事实都是
「至今成立」。补齐它的时候要一起补齐「为什么在这天结束」：迁移 0013 加了 `valid_to_reason`
和一条 CHECK（`valid_to IS NULL OR valid_to_reason IS NOT NULL`）。没有这条约束，它就变成
第二个 `aliases`，只是方向反过来：不是没人写，而是谁都能写一个看着像事实的日期。

闭合日默认取**继任者的 `valid_from`**：「旧的不再成立」与「新的开始成立」是同一个分界，
取同一个日期，两条区间正好接上，冲突归零而不留空档。`--on` 可以覆盖，但会警告会留出空档。
五种情形会被拒：谓词不同、两条都没有起始日、闭合日不晚于起始日、旧行已闭合在别的日期、
以及**两条起始日相同**（那多半是发文时间兜底，编一个日期比留一条看得见的冲突更糟）。

同组判定比的是**合并组**而不是 `subject_id`——合并写的只是指针，外键不动，跨写法的那一对
CEO 仍然挂在两个 id 上。`--undo` 一次清三列（`valid_to` / `valid_to_reason` /
`superseded_by`），只清日期会留下一条「有指针但没结束日」的记录。


### 诚实边界

- **时间线没有 REST 端点。** 只通过 Agent 工具（`entity_timeline` / `facts_as_of`）暴露，
  实体名容错交给模型用自然语言兜。直接 curl 是没有的。
- **70% 的日期是发文时间兜底**（167/238）。能按实体聚合看「做过什么」，不能按时间排序讲
  「这一周发生了什么」——排序依据七成是采集时间。
- **12 个谓词只覆盖产品与技术事件**，没有财经入口。准备金率、加息、财报扭亏、变更交易单位
  这类中文快讯会被判成「无事实」，是漏不是空。
- **对称谓词只挂一侧**：`A --partnered_with--> B` 记一条，查 B 时看不到 A。
  `fact_claims.object_entity_id` 预留了位置但没填——填之前必须先有实体合并
  （已完成：`entities.merged_into`），否则宾语会解析到两个等价行中的任意一个，
  「有些能双向」会变成「有些双向到 A 行、有些双向到 B 行」。
- **`founded` / `open_sourced` 的宾语类型未约束。** 宾语是许可证的 3 条、`founded` 填人名的
  4 条已撤回；`深微芯创 --funded--> 深产投` 这类（宾语是投资方、贷款承诺函）仍在库里，
  属人工队列——判定它们的疑点在语义不在形状，代码判不了。
- **职务类事实的 `valid_to` 还需要人判一次。** 有了 `supersede` 之后它不再是「只能看着」，
  但也不会自动补：闭合日默认取继任者的起始日，而这对记录往往都来自发文时间兜底——
  两条起始日相同时脚本直接拒，因为硬填一个日期等于编造交接日。所以「前任与现任同时在任」
  仍可能出现，此时该补结束日，不是撤回任何一条。
- **抽取有过状态位，但只回填了一部分。** `documents.facts_extracted_at` 让「抽过」被记住，
  产过事实的文档已回填；历史上那批「无事实」文档没有痕迹可反推，会在下一轮被再判一次。

完整推导见教程第 8 章。

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

- **实时搜索的 provider 是单点的**。keyless 有每日额度（约 30 次/天，429 错误码
  `daily_cap_reached`），且能力上只有一家。没有 failover 链，也没有缓存层——
  同一个 query 重复请求会重复消耗额度（上游即使命中它自己的缓存也照扣）。
- **中文实时检索能力未验证**。本地库中文时效是 90–171 天（已验证），但实时搜索
  能否补上中文，要等有 API key 后跑 live 评测；回放数据不区分 query。
- **没做事实核查**。`unique_domains` 是声源广度，不是「N 家独立证实」。
- 主题分类目前是确定性关键词规则，存在少量误判（如"标准版"命中政策类）；后续阶段引入模型分类与人工校正。
- 关键词检索为 `ILIKE`（标题 + 摘要）；正文层的召回由向量检索补上（`mode=vector|hybrid`，
  见「向量化检索」）。中文分词仍未做，`tsvector` 只用了 `simple` 配置。
- **精排喂给模型的是「标题 + 摘要 + 命中块原文」，不是完整正文**。命中块按 `char_start`
  从 `content_text` 切出 260 字，摘要 140 字——受 512 token 限制。主题类查询若答案分散在
  全文各处，这一段可能不足以让模型判对。要根本解决只能提高上下文长度（换更大的
  cross-encoder）或改成多块分别打分再聚合，两者都超出当前 CPU 预算。
- **Agent 是单轮的**。一次研究是一个独立问答，不保留上文；也没有做历史裁剪，
  所以还不支持对话式追问。
- **Agent 的工具只有两个**（`search_local` / `search_web`）。加第三个工具要先想清楚
  「它失败时模型怎么知道」，否则多一个工具只是多一个坏掉的输入。
- **免费档 RPM 5 会限制并发**。两个研究同时跑就会撞 429；默认的 5 轮预算刚好卡在
  单请求的边缘。要并发必须换付费档。
- **评测集没有做 pooling**，绝对值不能当基准引用：漏标的正确答案会被算成未命中，
  所以真实 Recall 只会比这里更高。它适合比较版本之间的差异，不适合对外宣称"召回率 82%"。
- 评测语料只覆盖两个真正可索引的源（阮一峰 390 篇全文 + HN 347 篇 600 字片段）。
  HN 那批文档只有两块，`DOC_AGG_TOP_N=3` 等于全取，聚合信号在英文侧会退化成整体平均，
  这也是 `en MRR` 在加聚合路后掉 0.056 的原因。
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
- 关键词分类器对论文标题几乎没有区分度：实测 50 篇 arXiv 论文全部判为 `ai-models`。这是后续阶段换模型分类的直接动因。
- DeepMind、TechCrunch 已注册但默认停用，尚未做健康检查。
- GitHub 分页上限 3 页（60 条）。两轮之间新增超过 60 个版本时仍会出现空档，实际不会发生；真发生只能靠人工补。
- 未做 user/鉴权（单用户本地版），`/admin` 端点暂无权限校验。

## 参考

工程手册见 `执行摘要.pdf`（数据源清单、评分公式、分阶段验收标准）。
