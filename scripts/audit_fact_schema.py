"""存量审计：库里哪些行违反了阶段 9 的形状约定。

只读，不改数据。清存量要逐条判断「撤回还是补新行」，那是 `repair_facts.py` 的活。

    .venv/bin/python scripts/audit_fact_schema.py
    .venv/bin/python scripts/audit_fact_schema.py --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.db.models import Document, Entity, FactClaim  # noqa: E402
from app.db.session import check_database, session_scope  # noqa: E402
from app.services.predicate_specs import PREDICATE_SPECS, SPECS  # noqa: E402
from app.services.predicate_specs import object_shape_problem  # noqa: E402
from sqlalchemy import select  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="审计存量事实的形状合规性")
    parser.add_argument("--limit", type=int, default=20, help="每类最多打印多少条")
    args = parser.parse_args()

    problem = await check_database()
    if problem:
        print(problem, file=sys.stderr)
        return 2

    closed = {spec.key for spec in SPECS}
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(FactClaim, Entity.name, Document.title)
                .join(Entity, Entity.id == FactClaim.subject_id)
                .join(Document, Document.id == FactClaim.document_id)
                .where(FactClaim.retracted_at.is_(None))
                .order_by(FactClaim.recorded_at.desc())
            )
        ).all()

    unknown_predicate = []
    wrong_type = []
    bad_shape = []

    for fact, subject, title in rows:
        if fact.predicate not in closed:
            unknown_predicate.append((fact, subject, title))
            continue
        expected = PREDICATE_SPECS[fact.predicate].object_type
        # 存量行的 object_type 是迁移回填的，理论上要么等于期望值、要么为空。
        # 空不报——那是「迁移没映射上」，不是「类型写错了」。
        if fact.object_type and fact.object_type != expected:
            wrong_type.append((fact, subject, title))
        if object_shape_problem(fact.predicate, fact.object_text):
            bad_shape.append((fact, subject, title))

    total = len(rows)
    print(f"未撤回事实 {total} 条\n")
    print(f"谓词不在闭集      {len(unknown_predicate)} 条")
    print(f"宾语类型与规格不符 {len(wrong_type)} 条")
    print(f"宾语形状不合规    {len(bad_shape)} 条  ← 第一阶段只拦新写入，这些是存量")

    def show(title: str, items) -> None:
        if not items:
            return
        print(f"\n{title}")
        for fact, subject, doc_title in items[: args.limit]:
            reason = object_shape_problem(fact.predicate, fact.object_text) or "-"
            print(
                f"  {subject} --{fact.predicate}--> {fact.object_text[:40]}"
                f"  ({fact.valid_from or '无日期'}) {reason}"
            )
        if len(items) > args.limit:
            print(f"  ……还有 {len(items) - args.limit} 条")

    show("谓词不在闭集：", unknown_predicate)
    show("宾语类型与规格不符：", wrong_type)
    show("宾语形状不合规：", bad_shape)

    if bad_shape:
        print(
            "\n清理方式（逐条判断，不要批量删）：\n"
            "  .venv/bin/python scripts/repair_facts.py retract --id <uuid> --reason '<为什么错>'\n"
            "撤回不释放唯一槽位，所以同一文档里再抽出同样的错宾语仍会被挡住——"
            "这是设计，不是缺陷。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
