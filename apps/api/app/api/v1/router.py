"""v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import documents, evidence, health, meta, sources

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(sources.router)
api_router.include_router(documents.router)
api_router.include_router(evidence.router)
api_router.include_router(meta.router)
