# DevInsight AI

科技产业情报平台：从中国与全球公开、合规的数据源持续采集科技新闻、官方发布、开源动态与论文，
把原始信息转化为可检索、可追溯的情报。

当前进度：**阶段 0（工程骨架）+ 阶段 1（来源注册与 RSS）**已跑通，即手册 14.1 要求的
「第一条真实数据闭环」：

```
36氪 RSS / InfoQ RSS / OpenAI News RSS
  -> RSS Connector（解析 / 清洗 / 去重 / 分类）
  -> PostgreSQL documents
  -> FastAPI GET /api/v1/documents
  -> Next.js /intelligence 情报流
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

# 3. 真实采集一轮（3 个启用来源）
make sync
make status        # 看健康状态与最近批次
make docs          # 看最新入库情报

# 4. 启动服务（两个终端）
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
| GET | `/api/v1/documents` | 情报列表：`q/source/region/language/topic/from/to/limit/cursor` |
| GET | `/api/v1/documents/{id}` | 文档详情 |
| GET | `/api/v1/meta/topics` | 六大领域字典 |
| GET | `/api/v1/stats` | 总览统计（按来源/地区/领域） |

统一响应：`{ data, meta: { request_id, timestamp, total, next_cursor }, error }`。

## 目录结构

```
apps/api/            FastAPI 后端
  app/core/          配置、日志、错误
  app/db/            模型（sources / ingestion_runs / documents / source_health_logs）、会话
  app/connectors/    Connector 抽象、RSS 实现、来源种子数据
  app/services/      采集、来源、文档查询、规则分类
  app/api/v1/        路由
  app/worker/        Celery 应用与任务
  migrations/        Alembic（0001_init）
  tests/             单元测试（URL/时间/文本/分类 + RSS 合约）
apps/web/            Next.js 前端（/intelligence 情报流、/sources 来源状态）
```

## 关键设计决策

- **published_at 与 fetched_at 严格分离**：解析失败就留空，绝不用采集时间伪装发布时间。
- **幂等入库**：`UNIQUE(source_id, external_id)` + `ON CONFLICT DO NOTHING`，重复采集只计入 skipped，不覆盖旧数据。
- **URL 规范化在前**：剔除 utm/ref/spm 等追踪参数、去 fragment、统一尾斜杠，否则同一篇文章会重复入库。
- **来源状态由运行时决定**：`health_status` 依据连续失败次数推断（≥3 次为 failing），不在代码里硬编码"可用"。
- **合规优先**：媒体源只存标题、摘要、原文链接（`rights_mode=metadata_excerpt`），全文不入库。
- **失败不放大**：单个源采集失败只影响自己，指数退避重试并遵守 `Retry-After`，4xx 不重试。

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

- 主题分类目前是确定性关键词规则，存在少量误判（如"标准版"命中政策类）；阶段 5 引入模型分类与人工校正。
- 关键词检索为 `ILIKE`；全文检索 / 向量检索在阶段 5 用 `tsvector` + pgvector 补上。
- 仅 3 个来源启用；DeepMind、TechCrunch 已注册但默认停用，属阶段 2 范围。
- 未做 user/鉴权（单用户本地版），`/admin` 端点暂无权限校验。

## 参考

工程手册见 `执行摘要.pdf`（数据源清单、评分公式、分阶段验收标准）。
