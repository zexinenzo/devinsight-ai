# 本地开发一键命令。在仓库根目录执行，使用仓库根的 .venv。

SHELL := /bin/bash
PY := .venv/bin/python
API := apps/api
WEB := apps/web

.PHONY: help check-docker up down logs wait-db migrate seed sync status docs api worker beat web test lint fmt

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
	cd $(API) && ../../$(PY) -m alembic upgrade head

seed: ## 写入内置来源注册表（幂等）
	cd $(API) && ../../$(PY) -m app.cli seed

sync: ## 立刻采集所有启用来源
	cd $(API) && ../../$(PY) -m app.cli sync --all

status: ## 查看来源健康与最近批次
	cd $(API) && ../../$(PY) -m app.cli list

docs: ## 查看最新入库情报
	cd $(API) && ../../$(PY) -m app.cli docs --limit 15

api: ## 启动 FastAPI（热重载，:8000）
	cd $(API) && ../../$(PY) -m uvicorn app.main:app --reload --port 8000

worker: ## 启动 Celery Worker
	cd $(API) && ../../$(PY) -m celery -A app.worker.celery_app:celery_app worker -l info

beat: ## 启动 Celery Beat（每 5 分钟派发到期来源）
	cd $(API) && ../../$(PY) -m celery -A app.worker.celery_app:celery_app beat -l info

web: ## 启动 Next.js（:3000）
	cd $(WEB) && npm run dev

test: ## 运行后端单元测试
	cd $(API) && ../../$(PY) -m pytest

lint: ## ruff 静态检查
	cd $(API) && ../../$(PY) -m ruff check .

fmt: ## ruff 自动格式化
	cd $(API) && ../../$(PY) -m ruff format .
