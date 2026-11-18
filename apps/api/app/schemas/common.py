"""统一响应封装。

所有接口都返回 {data, meta, error}：前端只需处理一种结构，
分页信息统一放 meta，错误码稳定可编程判断。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from fastapi import Request
from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Meta(BaseModel):
    request_id: str
    timestamp: datetime
    total: int | None = None
    next_cursor: str | None = None


class Envelope(BaseModel, Generic[T]):
    data: T | None
    meta: Meta
    error: dict[str, Any] | None = None


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def envelope(request: Request, data: Any, *, total: int | None = None, next_cursor: str | None = None) -> dict:
    return {
        "data": data,
        "meta": {
            "request_id": request_id_of(request),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "next_cursor": next_cursor,
        },
        "error": None,
    }


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
