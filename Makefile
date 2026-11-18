# 本地开发一键命令。在仓库根目录执行，使用仓库根的 .venv。

SHELL := /bin/bash
PY := .venv/bin/python
API := apps/api
WEB := apps/web

.PHONY: help up down logs wait-db migrate seed sync status docs api worker beat web test lint fmt

help: ## 显示可用命令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## 启动 Postgres(pgvector) 与 Redis
	docker compose up -d
	docker compose ps

down: ## 停止容器（保留数据卷）
	docker compose down

logs: ## 跟踪数据库与 Redis 日志
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
