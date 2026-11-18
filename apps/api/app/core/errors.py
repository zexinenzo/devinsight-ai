"""统一错误类型与响应结构。

错误响应固定为 {code, message, request_id}，前端不依赖具体文案。
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务异常基类，带稳定错误码。"""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UpstreamError(AppError):
    """外部来源不可用（超时、HTTP 错误、解析失败）。"""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_unavailable"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def register_exception_handlers(app) -> None:
    from app.core.logging import get_logger

    logger = get_logger(__name__)

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("app_error", extra={"request_id": _request_id(request)})
        return JSONResponse(
            status_code=exc.status_code,
            content={"data": None, "meta": {"request_id": _request_id(request)}, "error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(HTTPException)
    async def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "data": None,
                "meta": {"request_id": _request_id(request)},
                "error": {"code": f"http_{exc.status_code}", "message": str(exc.detail)},
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra={"request_id": _request_id(request)})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "data": None,
                "meta": {"request_id": _request_id(request)},
                "error": {"code": "internal_error", "message": "服务器内部错误，请稍后重试"},
            },
        )
