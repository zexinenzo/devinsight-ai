"""运维命令行（不依赖 Celery，便于首次跑通与排障）。

用法（工作目录 apps/api，且已激活仓库根 .venv）：
  python -m app.cli seed                 # 写入内置来源
  python -m app.cli list                 # 查看来源与健康状态
  python -m app.cli sync --all           # 立刻采集所有启用来源
  python -m app.cli sync --due           # 只采集到期来源（与 Beat 逻辑一致）
  python -m app.cli sync --source 36kr_zh
  python -m app.cli docs --limit 10      # 查看最新入库情报
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import unicodedata
from datetime import timezone

from sqlalchemy import text

from app.core.logging import configure_logging
from app.db.session import dispose_engine, session_scope
from app.services import (
    content_service,
    dedup_service,
    document_service,
    embedding_service,
    ingestion_service,
    source_service,
    vector_index_service,
)
from app.services.embedding_service import EmbeddingUnavailable


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符占两列。

    直接用 f-string 的 `<20` 是按字符数补齐的，中英文混排必然错位，
    来源名一长（github_release_transformers）还会把后一列顶出去。
    """

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))



async def _seed() -> int:
    async with session_scope() as session:
        result = await source_service.ensure_seed_sources(session)
    print(f"来源注册完成：新增 {result['inserted']} 个，已存在 {result['existing']} 个")
    return 0


async def _list() -> int:
    async with session_scope() as session:
        sources = await source_service.list_sources(session)
        runs = await source_service.latest_runs(session)

    header = ("key", "名称", "启用", "健康", "上次成功", "最近一批")
    rows: list[tuple[str, ...]] = []
    for source in sources:
        run = runs.get(source.id)
        last_success = (
            source.last_success_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
            if source.last_success_at
            else "-"
        )
        run_text = f"{run.status} +{run.created_count}/={run.fetched_count}" if run else "-"
        rows.append(
            (source.source_key, source.name, str(source.enabled), source.health_status, last_success, run_text)
        )

    widths = [
        max(_display_width(header[index]), *(_display_width(row[index]) for row in rows)) if rows
        else _display_width(header[index])
        for index in range(len(header))
    ]

    def render(cells: tuple[str, ...]) -> str:
        return "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(cells)).rstrip()

    print(render(header))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print(render(row))
    return 0


async def _sync(source_key: str | None, all_sources: bool, due_only: bool, enrich_limit: int = 0) -> int:
    failures = 0
    async with session_scope() as session:
        if due_only:
            runs = await ingestion_service.sync_all_due_sources(session, limit=20, triggered_by="cli")
        elif all_sources:
            sources = await source_service.list_sources(session, enabled=True)
            runs = [await ingestion_service.sync_source(session, s, triggered_by="cli") for s in sources]
        elif source_key:
            source = await source_service.get_source_by_key(session, source_key)
            if source is None:
                print(f"找不到来源：{source_key}", file=sys.stderr)
                return 2
            runs = [await ingestion_service.sync_source(session, source, triggered_by="cli")]
        else:
            print("请指定 --source/--all/--due", file=sys.stderr)
            return 2

        for run in runs:
            failures += 1 if run.status == "failed" else 0

        # 采集只拿元数据。对 HN 这类「上游只给标题」的源，摘要要等正文抓完才有，
        # 所以采集完立刻补一轮，否则列表里新增的条目全是「尚未补全正文」。
        if enrich_limit:
            await content_service.run_enrich(session, limit=enrich_limit, per_source=enrich_limit)

    for run in runs:
        print(
            f"[{run.status}] run={run.id} 抓取={run.fetched_count} 新增={run.created_count} "
            f"跳过={run.skipped_count} 耗时={run.duration_ms}ms"
            + (f" 错误={run.error}" if run.error else "")
        )
    return 1 if failures and failures == len(runs) else 0


async def _docs(limit: int) -> int:
    async with session_scope() as session:
        page = await document_service.list_documents(session, limit=limit)
    print(f"共 {page.total} 条，展示最近 {len(page.items)} 条：")
    for document, source in page.items:
        published = document.published_at.strftime("%Y-%m-%d %H:%M") if document.published_at else "未知"
        print(f"- [{source.name}] {published} | {document.title[:60]} | {','.join(document.topics) or '-'}")
    return 0


async def _enrich(limit: int, per_source: int, concurrency: int) -> int:
    async with session_scope() as session:
        summary = await content_service.run_enrich(
            session, limit=limit, per_source=per_source, concurrency=concurrency
        )
    print(
        f"正文补全：候选={summary['selected']} 其中重试={summary['retried']} 成功={summary['fetched']} "
        f"无正文={summary['empty']} 失败={summary['failed']} "
        f"新得摘要={summary['summary_gained']} 存量修复={summary['summary_repaired']} "
        f"自帖归位={summary['self_posts_marked']} 耗时={summary['duration_ms']}ms"
    )
    # 索引计数单独一行：它和抓取成功数不是一回事（正文太短、模型没装都会让索引为 0），
    # 混在同一行里会让人以为「抓到了就等于索引了」
    if summary.get("index_error"):
        print(f"向量索引：跳过（{summary['index_error']}）")
    elif summary["indexed_documents"]:
        print(
            f"向量索引：文档={summary['indexed_documents']} 块={summary['indexed_chunks']}"
            "  ← 正文此刻还在内存里，这是它唯一能被索引的时机"
        )
    if summary["selected"]:
        print("下一步：make dedup  ← 有了指纹才能判转载与聚类")
    return 0


async def _engagement(days: int, limit: int, concurrency: int) -> int:
    async with session_scope() as session:
        summary = await content_service.refresh_engagement(
            session, within_days=days, limit=limit, concurrency=concurrency
        )
    print(
        f"热度回填：扫描={summary['scanned']} 更新={summary['updated']} "
        f"无变化={summary['unchanged']} 取不到={summary['unavailable']}"
    )
    if summary["updated"]:
        print("下一步：make enrich  ← 现在按热度挑，抓的是真热门")
    return 0


async def _refingerprint(include_api: bool) -> int:
    async with session_scope() as session:
        summary = await content_service.backfill_fingerprints(session, include_api=include_api)
    print(
        f"重解析回填：源={summary.get('sources', 0)} 匹配={summary.get('matched', 0)} "
        f"补指纹={summary.get('fingerprint_added', 0)} 摘要变全={summary.get('summary_improved', 0)} "
        f"标记来源={summary.get('status_marked', 0)} 源失败={summary.get('source_failed', 0)}"
    )
    if summary.get("fingerprint_added"):
        print("下一步：make dedup  ← 中文语料现在也有指纹了")
    return 0


async def _dedup(days: int, threshold: int, event_threshold: float) -> int:
    async with session_scope() as session:
        result = await dedup_service.run_dedup(
            session, within_days=days, threshold=threshold, event_threshold=event_threshold
        )
    print(f"去重完成：标记转载={result['reposts']} 聚簇改动={result['clustered']}")
    return 0


async def _embed(limit: int, reindex: bool) -> int:
    """用已入库正文补向量索引，并报告覆盖情况。"""

    stale: dict[str, int] = {}
    model_name = ""
    try:
        async with session_scope() as session:
            model_name = embedding_service.get_embedder().name
            if reindex:
                # 重建前先清掉别的模型留下的块。`replace` 只删当前模型名下的旧块，
                # 换过模型的那部分旧向量不会被任何写入路径碰到，会一直留在表里。
                stale = await vector_index_service.purge_stale_models(
                    session, keep_model=model_name
                )
            stats = await vector_index_service.index_from_stored_content(
                session, limit=limit, only_missing=not reindex
            )
            coverage = await _embedding_coverage(session)
    except EmbeddingUnavailable as error:
        # 不吞掉原因：这条命令失败时，用户最需要知道的是「模型没下载」还是「库连不上」
        print(f"向量化不可用：{error}")
        return 1

    if stale:
        detail = "、".join(f"{name}={count} 块" for name, count in stale.items())
        print(f"清理旧向量：{detail}（不属于当前模型 {model_name}，向量空间不同不能混用）")
    print(
        f"向量索引：文档={stats['documents']} 块={stats['chunks']} "
        f"太短跳过={stats['skipped_short']}"
    )
    print(
        f"覆盖情况：可索引文档={coverage['eligible']} 已建索引={coverage['indexed']} "
        f"（{coverage['ratio']:.1%}）其中仅片段={coverage['excerpt_only']}，库里块数={coverage['chunks']}"
    )
    if coverage["eligible"] > coverage["indexed"]:
        print("提示：还有文档没索引，跑 python -m app.cli embed --limit 500 继续")
    if coverage["excerpt_only"]:
        print(
            "提示：有 "
            f"{coverage['excerpt_only']} 篇只索引到了 600 字片段（授权级别所限，原文已丢弃）。"
            "要覆盖全文只能重新抓取——索引会随 enrich 自动补上"
        )
    return 0


async def _embedding_coverage(session) -> dict:
    """索引覆盖率。

    分子分母都只算「正文长到值得索引」的文档，短讯进分母会让覆盖率永远上不去，
    看着像索引坏了，其实那批本来就只需要关键词检索。

    `excerpt_only` 单独统计：这些文档的正文在库里只有 600 字（授权级别所限），
    索引到的是片段而不是全文。把它和全文索引混在一个数字里，
    会让人误以为「覆盖率 100% 就等于全文都能搜」。

    所有 `EXISTS` 都带 `c.model = :model`。不带的话，换模型时旧模型的块会让覆盖率
    显示成 100%，而检索实际上一条都命中不了——统计口径和检索口径必须一致。
    """

    model_name = embedding_service.get_embedder().name
    row = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) FILTER (WHERE length(d.content_text) > :min_chars) AS eligible,
                    count(*) FILTER (
                        WHERE length(d.content_text) > :min_chars
                          AND EXISTS (
                              SELECT 1 FROM document_chunks c
                               WHERE c.document_id = d.id AND c.model = :model
                          )
                    ) AS indexed,
                    count(*) FILTER (
                        WHERE length(d.content_text) > :min_chars
                          AND length(d.content_text) <= :excerpt_chars
                          AND EXISTS (
                              SELECT 1 FROM document_chunks c
                               WHERE c.document_id = d.id AND c.model = :model
                          )
                    ) AS excerpt_only
                  FROM documents d
                """
            ),
            {
                "min_chars": embedding_service.CHUNK_CHARS,
                "excerpt_chars": content_service.EXCERPT_CHARS,
                "model": model_name,
            },
        )
    ).one()
    chunks = (
        await session.execute(
            text("SELECT count(*) FROM document_chunks WHERE model = :model"),
            {"model": model_name},
        )
    ).scalar_one()
    eligible, indexed, excerpt_only = int(row[0]), int(row[1]), int(row[2])
    return {
        "eligible": eligible,
        "indexed": indexed,
        "excerpt_only": excerpt_only,
        "ratio": (indexed / eligible) if eligible else 0.0,
        "chunks": int(chunks),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="DevInsight AI 运维命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="写入内置来源注册表")
    sub.add_parser("list", help="查看来源与健康状态")

    sync_parser = sub.add_parser("sync", help="执行采集")
    sync_parser.add_argument("--source", help="只采该 source_key")
    sync_parser.add_argument("--all", action="store_true", help="采集所有启用来源")
    sync_parser.add_argument("--due", action="store_true", help="只采集到期来源")
    sync_parser.add_argument(
        "--enrich",
        type=int,
        metavar="N",
        help="采集完立刻按热度补全 N 条正文与摘要（不指定则只采集元数据）",
    )

    docs_parser = sub.add_parser("docs", help="查看最新入库情报")
    docs_parser.add_argument("--limit", type=int, default=10)

    enrich_parser = sub.add_parser("enrich", help="按热度补全正文与指纹")
    enrich_parser.add_argument("--limit", type=int, default=30, help="本轮最多处理多少条")
    enrich_parser.add_argument("--per-source", type=int, default=10, help="每个源最多占多少名额")
    enrich_parser.add_argument("--concurrency", type=int, default=4, help="同时下载几页")

    engagement_parser = sub.add_parser("engagement", help="回填 HN 热度（热度是抓正文的排序依据）")
    engagement_parser.add_argument("--days", type=int, default=14, help="只处理最近 N 天的条目")
    engagement_parser.add_argument("--limit", type=int, default=300, help="本轮最多回填多少条")
    engagement_parser.add_argument("--concurrency", type=int, default=8, help="同时请求几个条目")

    dedup_parser = sub.add_parser("dedup", help="标记转载并聚合同事件报道")
    dedup_parser.add_argument("--days", type=int, default=14, help="只处理最近 N 天的条目")
    dedup_parser.add_argument(
        "--threshold", type=int, default=dedup_service.REPOST_HAMMING, help="转载判定的汉明距离上限"
    )
    dedup_parser.add_argument(
        "--event-threshold",
        type=float,
        default=dedup_service.EVENT_JACCARD,
        help="同事件标题相似度阈值（实测：同事件 0.44~0.67，无关 0.07）",
    )

    refingerprint_parser = sub.add_parser(
        "refingerprint",
        help="重拉上游，用新解析逻辑修正存量条目（sync 会跳过已存在条目）",
    )
    refingerprint_parser.add_argument(
        "--all-sources",
        action="store_true",
        help="覆盖 API 源（HN / GitHub / arXiv），会消耗真实 API 额度",
    )

    embed_parser = sub.add_parser(
        "embed",
        help="用已入库正文建向量索引（只对存了全文的源有效）",
    )
    embed_parser.add_argument("--limit", type=int, default=500, help="本次最多索引多少篇")
    embed_parser.add_argument(
        "--reindex",
        action="store_true",
        help="连已有索引的文档一起重算（换模型后必须跑）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)

    async def runner() -> int:
        try:
            if args.command == "seed":
                return await _seed()
            if args.command == "list":
                return await _list()
            if args.command == "sync":
                return await _sync(args.source, args.all, args.due, args.enrich or 0)
            if args.command == "docs":
                return await _docs(args.limit)
            if args.command == "enrich":
                return await _enrich(args.limit, args.per_source, args.concurrency)
            if args.command == "engagement":
                return await _engagement(args.days, args.limit, args.concurrency)
            if args.command == "dedup":
                return await _dedup(args.days, args.threshold, args.event_threshold)
            if args.command == "refingerprint":
                return await _refingerprint(args.all_sources)
            if args.command == "embed":
                return await _embed(args.limit, args.reindex)
            return 2
        finally:
            await dispose_engine()

    return asyncio.run(runner())


if __name__ == "__main__":
    raise SystemExit(main())
