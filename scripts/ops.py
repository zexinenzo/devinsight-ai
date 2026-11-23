#!/usr/bin/env python
"""统一运维入口（只读，不写库、不调模型）。

把 2026-11-23 会话里散落的 12 个一次性脚本收成一处。那次会话为查数据写了
check_repeat / check_pool_mix / check_summary_len / check_recent / get_ids /
body_hist 等脚本，全部靠在两个目录间手工搬运，下个会话就找不到了。

子命令：
    pool    待抽池按源分布：谁还有正文可抽、谁只有摘要、谁彻底空
    dups    同主语同谓词组：多源证据 vs 写法漂移，需要人眼分开看
    dirty   两处脏状态：有事实未标记 / 已标记无正文
    find    按文字查事实，输出完整 UUID（repair_facts.py 只认完整 UUID）
    stats   当前库规模，输出可直接贴进文档的 markdown 表

用法（仓库根目录）：
    .venv/bin/python scripts/ops.py pool
    .venv/bin/python scripts/ops.py find 星曜
    .venv/bin/python scripts/ops.py stats > /tmp/numbers.md

生成标记 retro-2026-11-23（重跑补丁时靠它判断这份文件归我维护）。

三条设计约束，都是 2026-11-23 那次会话的代价换来的：
  · 只读。写库一律走 repair_facts.py / extract_facts.py，这里不提供。
  · 连不上库只打一行提示，不打栈。那次会话 44 次 ConnectionRefused，
    每次都刷一屏 traceback，掩盖了「只是 WSL 没起来」这个简单事实。
  · 启动先校验依赖的表和列，缺什么直接说，不等 SQL 报错。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"

# 本脚本用到的表与列。新增查询时同步加到这里，schema 漂移会在启动时被叫住。
REQUIRED = {
    "documents": {
        "id", "source_id", "facts_extracted_at",
        "content_text", "summary", "content_status",
    },
    "fact_claims": {
        "id", "document_id", "subject_id", "predicate", "object_text",
        "valid_from", "valid_from_source", "retracted_at", "recorded_at",
    },
    "entities": {"id", "name"},
}


def display_width(value: str) -> int:
    """中日韩字符按 2 列算，其余按 1 列。"""
    total = 0
    for char in str(value):
        total += 2 if "\u1100" <= char <= "\uffdc" else 1
    return total


def pad(value, width: int) -> str:
    text = "-" if value is None else str(value)
    return text + " " * max(width - display_width(text), 0)


def cut(value, width: int) -> str:
    text = "-" if value is None else str(value)
    if display_width(text) <= width:
        return text
    out = ""
    for char in text:
        if display_width(out + char) > width - 1:
            break
        out += char
    return out + "…"


def rule(ch: str = "-", width: int = 100) -> None:
    print(ch * width)


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------

async def open_session():
    """返回 (session_scope, dispose_engine)。导入放在函数里，`--help` 不需要能连库。"""
    sys.path.insert(0, str(API_DIR))
    try:
        from app.db.session import dispose_engine, session_scope  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        # 用系统 python 跑而不是 .venv/bin/python 时最常见的表现，一句话说清
        print(f"导入失败：{exc}", file=sys.stderr)
        print(
            "在仓库根用 .venv/bin/python scripts/ops.py <子命令>"
            "（依赖装在 .venv 里，系统 python 没有）",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    return session_scope, dispose_engine


def db_hint(exc: BaseException) -> str:
    text = str(exc)
    if "Connect call failed" in text or "Connection refused" in text:
        return (
            "数据库连不上（127.0.0.1:5432 拒绝连接）。\n"
            "  WSL 重启后 Postgres 不会自动起，先执行：\n"
            "    sudo service postgresql start && pg_isready -h 127.0.0.1 -p 5432"
        )
    return f"查询失败：{exc}"


async def check_schema(session) -> list[str]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "select table_name, column_name from information_schema.columns "
                "where table_schema = 'public'"
            )
        )
    ).all()
    have: dict[str, set[str]] = {}
    for table, column in rows:
        have.setdefault(table, set()).add(column)
    problems = []
    for table, columns in REQUIRED.items():
        if table not in have:
            problems.append(f"缺表 {table}")
            continue
        missing = sorted(columns - have[table])
        if missing:
            problems.append(f"{table} 缺列 {missing}")
    return problems


async def source_table(session) -> str | None:
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' and table_name in ('sources', 'source') "
                "order by case when table_name = 'sources' then 0 else 1 end limit 1"
            )
        )
    ).scalar()
    return row


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

POOL_TOTAL = """
select count(*) as total,
       count(*) filter (where facts_extracted_at is null) as pending,
       count(*) filter (where facts_extracted_at is not null) as done,
       count(*) filter (where coalesce(content_text, '') <> '') as has_body,
       count(*) filter (where content_text is null) as no_body
from documents
"""

DUPS = """
select e.name as subject, f.predicate, count(*) as n,
       string_agg(f.object_text, ' | ' order by f.valid_from) as objects
from fact_claims f
join entities e on e.id = f.subject_id
where f.retracted_at is null
group by e.name, f.predicate
having count(*) > 1
order by n desc, e.name
"""

DIRTY_UNMARKED = """
select count(*) as docs, count(f.id) as facts
from documents d
join fact_claims f on f.document_id = d.id and f.retracted_at is null
where d.facts_extracted_at is null
"""

DIRTY_MARKED_NO_BODY = """
select count(*)
from documents
where facts_extracted_at is not null and coalesce(content_text, '') = ''
"""

FIND = """
select f.id::text, e.name, f.predicate, f.object_text,
       f.valid_from::date, f.valid_from_source,
       case when f.retracted_at is null then '有效' else '已撤回' end
from fact_claims f
join entities e on e.id = f.subject_id
where e.name ilike :q or f.object_text ilike :q
order by f.recorded_at desc
limit 50
"""

STATS_COUNTS = """
select count(*) as documents,
       count(*) filter (where facts_extracted_at is not null) as marked,
       count(*) filter (where content_text is null) as no_body,
       count(*) filter (where facts_extracted_at is null) as pending
from documents
"""

STATS_FACTS = """
select count(*) as total,
       count(*) filter (where retracted_at is null) as live,
       count(*) filter (where retracted_at is not null) as retracted
from fact_claims
"""

STATS_SOURCE_KIND = """
select valid_from_source, count(*)
from fact_claims where retracted_at is null
group by 1 order by 2 desc
"""

STATS_PREDICATE = """
select predicate, count(*)
from fact_claims where retracted_at is null
group by 1 order by 2 desc limit 10
"""


async def cmd_pool(session) -> None:
    from sqlalchemy import text

    total = (await session.execute(text(POOL_TOTAL))).one()
    print("一、全库文档")
    rule()
    print(
        pad("总数", 12) + pad("已标记", 12) + pad("待抽", 12)
        + pad("有正文", 12) + "无正文"
    )
    print(
        pad(total[0], 12) + pad(total[2], 12) + pad(total[1], 12)
        + pad(total[3], 12) + str(total[4])
    )

    table = await source_table(session)
    if not table:
        print()
        print("（找不到 sources 表，跳过按源分布）")
        return

    sql = f"""
    select coalesce(s.name, '(未知)') as source,
           count(*) as pending,
           count(*) filter (where coalesce(d.content_text, '') <> '') as has_body,
           count(*) filter (
               where coalesce(d.content_text, '') = ''
                 and coalesce(d.summary, '') <> ''
           ) as summary_only,
           count(*) filter (
               where coalesce(d.content_text, '') = ''
                 and coalesce(d.summary, '') = ''
           ) as empty
    from documents d
    left join {table} s on s.id = d.source_id
    where d.facts_extracted_at is null
    group by 1
    order by pending desc
    """
    rows = (await session.execute(text(sql))).all()

    print()
    print(f"二、待抽池按源（{len(rows)} 个源）")
    rule()
    print(pad("源", 30) + pad("待抽", 8) + pad("有正文", 10) + pad("仅摘要", 10) + "全空")
    rule()
    for row in rows:
        print(
            pad(cut(row[0], 28), 30) + pad(row[1], 8)
            + pad(row[2], 10) + pad(row[3], 10) + str(row[4])
        )

    print()
    print("判断：")
    print("  · 「有正文」为 0 的源，门槛不是问题——它本来就只出摘要，事实密度低")
    print("  · 「全空」不为 0 的源，每篇都会花一次调用换来一句「本文无可抽事实」")


async def cmd_dups(session) -> None:
    from sqlalchemy import text

    rows = (await session.execute(text(DUPS))).all()
    print(f"同主语同谓词组：{len(rows)} 组（只看未撤回）")
    rule()
    if not rows:
        print("无")
        return
    for row in rows:
        objects = str(row[3]).split(" | ")
        # 三种解读差别很大，先归类再让人看
        distinct = len(set(objects))
        if distinct == 1:
            kind = "字符级完全重复"
        elif distinct < len(objects):
            kind = "部分重复"
        else:
            kind = "并列项（可能都对）"
        print(f"{cut(row[0], 22)} --{row[1]}-->  {row[2]} 条  [{kind}]")
        for part in objects:
            print(f"      · {cut(part, 80)}")
    print()
    print("判断：")
    print("  · 并列项（英伟达 partnered_with 六家供应商）不用动")
    print("  · 字符级完全重复 / 部分重复（同一事件两种写法、中英各一条）该收一条")


async def cmd_dirty(session) -> None:
    from sqlalchemy import text

    unmarked = (await session.execute(text(DIRTY_UNMARKED))).one()
    marked_no_body = (await session.execute(text(DIRTY_MARKED_NO_BODY))).scalar_one()

    print("脏状态（两处，都不报错，但会让抽取空转）")
    rule()
    print(f"  有事实但未标记：{unmarked[1]} 条事实 / {unmarked[0]} 篇文档")
    print(f"      后果：每轮抽取都会重新选中它们，而唯一约束拦不住宾语写法漂移")
    print(f"      处置：mark_docs_with_facts.py --apply（把已抽过的标记住）")
    print()
    print(f"  已标记但无正文：{marked_no_body} 篇")
    print(f"      后果：enrich 补上正文也不会重抽（标记已经写死）")
    print(f"      处置：extract_facts.py --reset-no-body --apply")


async def cmd_find(session, keyword: str) -> None:
    from sqlalchemy import text

    rows = (await session.execute(text(FIND), {"q": f"%{keyword}%"})).all()
    print(f"「{keyword}」命中 {len(rows)} 条")
    rule()
    if not rows:
        print("无")
        return
    for row in rows:
        # 完整 UUID。repair_facts.py 只认它，给前缀等于让人再跑一趟。
        print(row[0])
        print(
            f"    {row[1]} --{row[2]}--> {cut(row[3], 40)}  "
            f"({row[4]}/{row[5]})  {row[6]}"
        )


async def cmd_stats(session) -> None:
    from sqlalchemy import text
    from datetime import datetime, timezone

    docs = (await session.execute(text(STATS_COUNTS))).one()
    facts = (await session.execute(text(STATS_FACTS))).one()
    sources = (await session.execute(text(STATS_SOURCE_KIND))).all()
    predicates = (await session.execute(text(STATS_PREDICATE))).all()
    dups = (await session.execute(text(DUPS))).all()

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"<!-- scripts/ops.py stats 生成，不要手改数字，重跑覆盖（{stamp}） -->")
    print()
    print("| 指标 | 数值 |")
    print("| --- | --- |")
    print(f"| 文档总数 | {docs[0]} |")
    print(f"| 已抽取标记 | {docs[1]} |")
    print(f"| 待抽取 | {docs[3]} |")
    print(f"| 无正文 | {docs[2]} |")
    print(f"| 事实总数 | {facts[0]} |")
    print(f"| 有效事实 | {facts[1]} |")
    print(f"| 已撤回 | {facts[2]} |")
    print(f"| 同主语同谓词组 | {len(dups)} |")
    print()
    print("日期来源（仅有效事实）：")
    print()
    print("| 来源 | 条数 |")
    print("| --- | --- |")
    for row in sources:
        print(f"| {row[0]} | {row[1]} |")
    print()
    print("谓词分布（前 10）：")
    print()
    print("| 谓词 | 条数 |")
    print("| --- | --- |")
    for row in predicates:
        print(f"| {row[0]} | {row[1]} |")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

async def run(args) -> int:
    session_scope, dispose_engine = await open_session()

    try:
        async with session_scope() as session:
            problems = await check_schema(session)
            if problems:
                print("schema 与预期不符，先别跑查询：", file=sys.stderr)
                for problem in problems:
                    print(f"  · {problem}", file=sys.stderr)
                print("（迁移没跑完？cd apps/api && alembic upgrade head）", file=sys.stderr)
                return 1

            if args.cmd == "pool":
                await cmd_pool(session)
            elif args.cmd == "dups":
                await cmd_dups(session)
            elif args.cmd == "dirty":
                await cmd_dirty(session)
            elif args.cmd == "find":
                await cmd_find(session, args.keyword)
            elif args.cmd == "stats":
                await cmd_stats(session)
    except OSError as exc:
        print(db_hint(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        # SQLAlchemy 会把 asyncpg 的错误包一层，ConnectionRefused 就不再是 OSError
        if "connect" in str(exc).lower():
            print(db_hint(exc), file=sys.stderr)
            return 1
        raise
    finally:
        try:
            await dispose_engine()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ops.py",
        description="只读运维查询：pool / dups / dirty / find / stats",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pool", help="待抽池按源分布")
    sub.add_parser("dups", help="同主语同谓词组，按重复类型归类")
    sub.add_parser("dirty", help="两处脏状态：有事实未标记 / 已标记无正文")
    find = sub.add_parser("find", help="按文字查事实，输出完整 UUID")
    find.add_argument("keyword", help="匹配主语或宾语，不区分大小写")
    sub.add_parser("stats", help="库规模 markdown，可直接贴进文档")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
