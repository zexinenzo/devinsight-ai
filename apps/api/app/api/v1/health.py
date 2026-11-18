"""健康检查：数据库与 Redis 依赖是否可用。"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.deps import SessionDep
from app.core.config import settings
from app.schemas.common import envelope

router = APIRouter(tags=["health"])


async def _check_db(session: SessionDep) -> tuple[bool, str | None, int]:
    started = time.perf_counter()
    try:
        await session.execute(text("select 1"))
        return True, None, int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # 健康检查要报告失败原因，而不是抛 500
        return False, f"{type(exc).__name__}: {exc}"[:300], int((time.perf_counter() - started) * 1000)


async def _check_redis() -> tuple[bool, str | None, int]:
    started = time.perf_counter()
    client = Redis.from_url(settings.redis_url, socket_timeout=2)
    try:
        await asyncio.wait_for(client.ping(), timeout=2)
        return True, None, int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], int((time.perf_counter() - started) * 1000)
    finally:
        await client.aclose()


@router.get("/health", summary="服务健康检查")
async def health(request: Request, session: SessionDep):
    db_ok, db_error, db_ms = await _check_db(session)
    redis_ok, redis_error, redis_ms = await _check_redis()

    status = "ok" if (db_ok and redis_ok) else ("degraded" if db_ok else "down")
    payload = {
        "status": status,
        "app": settings.app_name,
        "env": settings.app_env,
        "checks": {
            "database": {"ok": db_ok, "latency_ms": db_ms, "error": db_error},
            "redis": {"ok": redis_ok, "latency_ms": redis_ms, "error": redis_error},
        },
    }
    body = envelope(request, payload)
    # 数据库不可用视为不健康，直接给 503，方便容器/CI 判定
    return JSONResponse(status_code=200 if db_ok else 503, content=body)
