"""按需展开某几条候选的原文，用于判定时的二次取证。

**为什么会需要第二次取证。**

工作表里每条只给 120 字证据，够判「明显无关」，不够判「疑似同一事件的另一篇报道」。
后者是 topic / 英文类问题里最费神的判断：标题都像，内容得看它讲的是哪一起事件。
只凭 120 字下判断，两个方向都会错——把不同事件当同一事件补进相关集，
和把同一事件漏掉一样污染指标，只是偏的方向不同。

用法：

    python scripts/show_doc.py --qid e03 --shorts 126d21f5,58c6d6a7 --term bank
    python scripts/show_doc.py --qid t11 --shorts 2ae12a22 --chars 800

`--term` 有就给关键词附近的上下文（判定「是不是同一件事」要看的就是这段），
没有就给正文开头。关键词在正文里找不到时，如实说找不到——
这本身就是信息：说明这篇没在讲那个词。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from app.db.models import Document
from app.db.session import session_scope
from pool_judge import short_id
from sqlalchemy import select


def load_pool() -> dict:
    path = API_DIR / "data" / "eval_pool.json"
    return json.loads(path.read_text(encoding="utf-8"))["questions"]


async def fetch(ids: list[uuid.UUID]) -> dict[str, dict]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Document.id,
                    Document.title,
                    Document.summary,
                    Document.content_text,
                ).where(Document.id.in_(ids))
            )
        ).all()
    return {
        str(row[0]): {
            "title": row[1] or "(无标题)",
            "summary": " ".join((row[2] or "").split()),
            "body": " ".join((row[3] or "").split()),
        }
        for row in rows
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="展开候选原文")
    parser.add_argument("--qid", required=True)
    parser.add_argument("--shorts", required=True, help="逗号分隔的短 id")
    parser.add_argument("--term", default="", help="定位到这个关键词附近")
    parser.add_argument("--chars", type=int, default=500)
    parser.add_argument("--lead", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0, help="从第几个字符开始")
    args = parser.parse_args()

    pool = load_pool()
    entry = pool.get(args.qid)
    if entry is None:
        raise SystemExit(f"池子里没有 {args.qid}")

    by_short = {short_id(d): d for d in entry["candidates"]}
    wanted = [s.strip() for s in args.shorts.split(",") if s.strip()]
    missing = [s for s in wanted if s not in by_short]
    if missing:
        raise SystemExit(f"这些短 id 不在 {args.qid} 的池子里：{missing}")

    meta = await fetch([uuid.UUID(by_short[s]) for s in wanted])
    for key in wanted:
        info = meta.get(by_short[key], {})
        print(f"=== [{key}] {info.get('title')}")
        body = info.get("body") or ""
        if not body:
            print("  (无正文，摘要见下)")
            print("  " + (info.get("summary") or "(无摘要)"))
            print()
            continue
        if args.term:
            index = body.find(args.term)
            if index < 0:
                print(f"  正文里没有「{args.term}」，开头 200 字：")
                print("  " + body[:200])
                print()
                continue
            print(
                "  "
                + body[max(0, index - args.lead) : index + args.chars]
            )
        else:
            print("  " + body[args.offset : args.offset + args.chars])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
