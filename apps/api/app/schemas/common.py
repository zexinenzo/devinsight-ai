"""统一响应封装。

所有接口都返回 {data, meta, error}：前端只需处理一种结构，
分页信息统一放 meta，错误码稳定可编程判断。
"""

from __future__ import annotations

from datetime import UTC, datetime
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


def envelope(
    request: Request,
    data: Any,
    *,
    total: int | None = None,
    next_cursor: str | None = None,
    extra: dict | None = None,
) -> dict:
    """统一响应外壳。

    `extra` 只在使用它的端点上出现，不写成固定字段：语义检索要报告
    `reranked`（这次结果有没有经过 cross-encoder 精排），关键词检索没有这个概念，
    给它一个恒为 False 的字段只会让人误以为「本来该重排却没排」。
    """

    meta: dict[str, Any] = {
        "request_id": request_id_of(request),
        "timestamp": datetime.now(UTC).isoformat(),
        "total": total,
        "next_cursor": next_cursor,
    }
    if extra:
        meta.update(extra)
    return {"data": data, "meta": meta, "error": None}


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
