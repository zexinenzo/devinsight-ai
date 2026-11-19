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

from app.core.logging import configure_logging
from app.db.session import dispose_engine, session_scope
from app.services import content_service, dedup_service, document_service, ingestion_service, source_service


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
        f"正文补全：候选={summary['selected']} 成功={summary['fetched']} "
        f"无正文={summary['empty']} 失败={summary['failed']} "
        f"新得摘要={summary['summary_gained']} 存量修复={summary['summary_repaired']} "
        f"自帖归位={summary['self_posts_marked']} 耗时={summary['duration_ms']}ms"
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
            return 2
        finally:
            await dispose_engine()

    return asyncio.run(runner())


if __name__ == "__main__":
    raise SystemExit(main())
