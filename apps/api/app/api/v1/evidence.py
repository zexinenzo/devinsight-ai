"""证据端点：把一个问题的本地证据与实时证据放在一起，并标注各自的可信度。

**为什么单独开一个端点，而不是给 /documents 加 `include_web`。** `/documents`
回答的是「库里有什么」，它的每个字段（`dup_of`、`cluster_id`、`content_status`）
都建立在「已入库」这个前提上。实时搜索结果不在库里，塞进去会让一半字段恒为 null，
而且调用方必须自己从 `channel` 反推哪些字段可信。分开以后，`/evidence` 的契约
就是「给我一个问题的全部依据」，它天然是 Agent 的工具，不是列表页的数据源。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import AppError, UpstreamError
from app.schemas.common import envelope
from app.schemas.evidence import EvidenceBundleOut, EvidenceItem, VerificationOut
from app.services import evidence_service
from app.services.web_search_service import WebSearchFailed, WebSearchUnavailable

router = APIRouter(tags=["evidence"])


@router.get("/evidence", summary="证据集：本地检索 + 实时搜索")
async def get_evidence(
    request: Request,
    session: SessionDep,
    q: str = Query(..., min_length=1, max_length=200, description="要研究的问题或关键词"),
    days: int = Query(
        settings.web_search_days,
        ge=1,
        le=30,
        description="实时搜索的时间窗（天）。news 模式下是硬过滤，超出 1~30 会被上游拒绝",
    ),
    local_limit: int = Query(8, ge=1, le=30, description="本地证据条数"),
    web_limit: int = Query(
        settings.web_search_max_results,
        ge=1,
        le=20,
        description="实时证据条数。不是越大越好：每条都是一个待验证的域名",
    ),
    include_web: bool = Query(
        True,
        description=(
            "是否取实时证据。false 时只返回本地证据且 web_status=disabled；"
            "true 但服务端没配 provider 会返回 503，而不是悄悄返回空列表"
        ),
    ),
    mode: str = Query(
        "hybrid",
        pattern="^(keyword|vector|hybrid)$",
        description="本地检索方式，语义同 /documents 的 mode",
    ),
):
    try:
        bundle = await evidence_service.collect_evidence(
            session,
            q,
            days=days,
            local_limit=local_limit,
            web_limit=web_limit,
            include_web=include_web,
            search_mode=mode,
        )
    except WebSearchUnavailable as error:
        # 503：这个部署没有实时能力。「没配」和「挂了」必须能分开——
        # 前者要人改配置，后者等一会儿可能就好了。
        raise AppError(str(error), code="web_search_unavailable", status_code=503) from error
    except WebSearchFailed as error:
        # 502：配了，但上游此刻不行（限流 / 超时 / 5xx）
        raise UpstreamError(str(error)) from error

    payload = EvidenceBundleOut(
        query=bundle.query,
        local=[EvidenceItem.model_validate(item) for item in bundle.local],
        web=[EvidenceItem.model_validate(item) for item in bundle.web],
        verification=VerificationOut(
            web_total=bundle.verification.web_total,
            unique_domains=bundle.verification.unique_domains,
            overlap_with_local=bundle.verification.overlap_with_local,
            date_unverified=bundle.verification.date_unverified,
            tier_counts=bundle.verification.tier_counts,
            freshest_at=bundle.verification.freshest_at,
            oldest_at=bundle.verification.oldest_at,
            mean_age_days=bundle.verification.mean_age_days,
        ),
        local_mode=bundle.local_mode,
        local_reranked=bundle.local_reranked,
        web_status=bundle.web_status,
        web_provider=bundle.web_provider,
        web_topic=bundle.web_topic,
        web_days=bundle.web_days,
    )
    return envelope(
        request,
        payload.model_dump(mode="json"),
        # total 给的是证据总条数，不是库里命中的总数：调用方关心「这么多条能不能支撑一个判断」
        total=len(payload.local) + len(payload.web),
    )
