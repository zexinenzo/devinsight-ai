"""FastAPI 应用入口。

启动：uvicorn app.main:app --reload --port 8000（工作目录 apps/api）
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info("api_startup", extra={"source_id": settings.app_name})
    yield
    await dispose_engine()
    logger.info("api_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="DevInsight AI API",
        description="科技产业情报平台：来源注册、采集、标准文档与情报流",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """每个请求一个 request_id，贯穿日志与错误响应，便于排查。"""

        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")
    # 根路径 /health 指向同一实现：容器探针、uptime 监控默认都打这里，
    # 不写进 OpenAPI，避免文档里出现两条一模一样的健康检查。
    app.include_router(health_router, include_in_schema=False)

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": settings.app_name,
            "version": "0.1.0",
            "docs": "/docs",
            "health": "/api/v1/health",
        }

    return app


app = create_app()
