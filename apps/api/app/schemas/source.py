"""来源相关的响应模型。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class IngestionRunOut(ORMModel):
    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    fetched_count: int
    created_count: int
    skipped_count: int
    http_status: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    triggered_by: str


class SourceOut(ORMModel):
    id: uuid.UUID
    source_key: str
    name: str
    canonical_domain: str
    source_type: str
    region: str
    language: str | None = None
    access_method: str
    base_url: str
    credibility_tier: str
    rights_note: str | None = None
    enabled: bool
    priority: int
    poll_interval_minutes: int
    health_status: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int
    last_http_status: int | None = None
    parser_version: str
    last_run: IngestionRunOut | None = None


class SourceHealthLogOut(ORMModel):
    checked_at: datetime
    status_code: int | None = None
    latency_ms: int | None = None
    ok: bool
    error_message: str | None = None


class SourceHealthOut(BaseModel):
    source: SourceOut
    recent_runs: list[IngestionRunOut]
    recent_logs: list[SourceHealthLogOut]


class SyncAccepted(BaseModel):
    source_id: uuid.UUID
    task_id: str | None = None
    execution: str  # queued（进 Celery）/ inline（broker 不可用时进程内执行）
    message: str
