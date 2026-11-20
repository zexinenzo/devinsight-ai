# 本地开发一键命令。在仓库根目录执行，使用仓库根的 .venv。

SHELL := /bin/bash
# 用绝对路径引用解释器：相对路径会让 Python 解析出的 sys.prefix 带 ../..，
# Python 3.14 会为此刷 RuntimeWarning，污染每条命令的输出。
ROOT := $(CURDIR)
PY := $(ROOT)/.venv/bin/python
API := $(ROOT)/apps/api
WEB := $(ROOT)/apps/web

.PHONY: help check-docker up down logs wait-db migrate seed sync sync-enrich status docs engagement refingerprint enrich dedup extract api worker beat web test lint fmt venv

help: ## 显示可用命令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

check-docker: ## 确认 Docker 客户端可用（内部使用）
	@docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || { \
		echo "✗ 连不上 Docker 引擎：docker 客户端不可用"; \
		echo "  WSL 下最常见原因是 WSLInterop 未注册，见 README「Docker 客户端故障排查」"; \
		exit 1; \
	}

up: ## 启动 Postgres(pgvector) 与 Redis（已在运行则自动跳过）
	@if docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then \
		docker compose up -d && docker compose ps; \
	elif (exec 3<>/dev/tcp/127.0.0.1/5432) 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/6379) 2>/dev/null; then \
		echo "✓ 5432 / 6379 已在监听，容器已运行，跳过启动"; \
	else \
		echo "✗ Docker 客户端不可用，且 5432 / 6379 都没有监听"; \
		echo "  容器没起来就无法继续，先修 docker 客户端：见 README「Docker 客户端故障排查」"; \
		exit 1; \
	fi

down: check-docker ## 停止容器（保留数据卷）
	docker compose down

logs: check-docker ## 跟踪数据库与 Redis 日志
	docker compose logs -f db redis

migrate: ## 执行数据库迁移到最新版本
	cd $(API) && $(PY) -m alembic upgrade head

seed: ## 写入内置来源注册表（幂等）
	cd $(API) && $(PY) -m app.cli seed

sync: ## 立刻采集所有启用来源
	cd $(API) && $(PY) -m app.cli sync --all

sync-enrich: ## 采集并顺手补一轮摘要（N=40 指定条数；不补的话 HN 新条目全是「缺摘要」）
	cd $(API) && $(PY) -m app.cli sync --all --enrich $(or $(N),40)

status: ## 查看来源健康与最近批次
	cd $(API) && $(PY) -m app.cli list

docs: ## 查看最新入库情报
	cd $(API) && $(PY) -m app.cli docs --limit 15

engagement: ## 回填 HN 热度（阶段 3 新增列的存量补数，热度决定抓谁）
	cd $(API) && $(PY) -m app.cli engagement

enrich: ## 按热度补全正文与指纹（每源各占名额，不是全局排序）
	cd $(API) && $(PY) -m app.cli enrich

refingerprint: ## 重拉上游修正存量条目（ALL=1 时覆盖 API 源，会消耗额度）
	cd $(API) && $(PY) -m app.cli refingerprint $(if $(ALL),--all-sources,)

dedup: ## 标记转载（SimHash）并聚合同事件报道（标题相似度）
	cd $(API) && $(PY) -m app.cli dedup

# 默认只跑 5 篇 dry-run：抽一批看质量，比一次抽 500 篇再发现全被谓词校验打回便宜得多。
# 需要写库时用 ARGS="--limit 50 --apply" 覆盖。
extract: ## 抽取实体与事实（默认 5 篇 dry-run；ARGS="--limit 50 --apply" 写库）
	cd $(ROOT) && $(PY) scripts/extract_facts.py $(if $(ARGS),$(ARGS),--limit 5 --dry-run)

api: ## 启动 FastAPI（热重载，:8000）
	cd $(API) && $(PY) -m uvicorn app.main:app --reload --port 8000

worker: ## 启动 Celery Worker
	cd $(API) && $(PY) -m celery -A app.worker.celery_app:celery_app worker -l info

beat: ## 启动 Celery Beat（每 5 分钟派发到期来源）
	cd $(API) && $(PY) -m celery -A app.worker.celery_app:celery_app beat -l info

web: ## 启动 Next.js（:3000）
	cd $(WEB) && npm run dev

test: ## 运行后端单元测试
	cd $(API) && $(PY) -m pytest

# 与 CI 完全一致：CI 在仓库根跑 ruff check .，这里就不能只查 apps/api，
# 否则 scripts/ 与 docs/tools/ 的问题要等推送后才发现。
lint: ## ruff 静态检查（全仓，与 CI 一致）
	$(PY) -m ruff check .

fmt: ## ruff 自动格式化（全仓，与 CI 一致）
	$(PY) -m ruff format .

venv: ## 重建 .venv（python3.14 + requirements.txt；依赖清单即唯一事实来源）
	@test -x /usr/bin/python3.14 || { \
		echo "✗ 找不到 /usr/bin/python3.14"; \
		echo "  这个项目固定用 3.14：requirements.txt 是在同一版本下 freeze 的，"; \
		echo "  换版本会让 onnxruntime / tokenizers 的 wheel 对不上。"; \
		exit 1; \
	}
	/usr/bin/python3.14 -m venv $(ROOT)/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r $(ROOT)/requirements.txt
	@echo "✓ venv 就绪：$$($(PY) --version) / ruff $$($(PY) -m ruff --version | awk '{print $$2}')"
