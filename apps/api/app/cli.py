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
from app.services import document_service, ingestion_service, source_service


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


async def _sync(source_key: str | None, all_sources: bool, due_only: bool) -> int:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="DevInsight AI 运维命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="写入内置来源注册表")
    sub.add_parser("list", help="查看来源与健康状态")

    sync_parser = sub.add_parser("sync", help="执行采集")
    sync_parser.add_argument("--source", help="只采该 source_key")
    sync_parser.add_argument("--all", action="store_true", help="采集所有启用来源")
    sync_parser.add_argument("--due", action="store_true", help="只采集到期来源")

    docs_parser = sub.add_parser("docs", help="查看最新入库情报")
    docs_parser.add_argument("--limit", type=int, default=10)
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
                return await _sync(args.source, args.all, args.due)
            if args.command == "docs":
                return await _docs(args.limit)
            return 2
        finally:
            await dispose_engine()

    return asyncio.run(runner())


if __name__ == "__main__":
    raise SystemExit(main())
