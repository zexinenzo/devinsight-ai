"""实体类问题的覆盖率审计：全库有多少篇提到这个实体，其中多少篇进了候选池。

**为什么需要这个数。**

补池能把「候选池里的漏判」修掉，但修不了「压根没进池的文档」——
六路各取 top-20，并集只覆盖全库 47%，剩下 53% 无论判定多认真都不会被看见。
对实体名这类查询，这个问题尤其致命：向量空间里人名、产品名几乎没有可分的
语义质量，召回退化成噪声，真正的相关文档可能一篇都没进 top-20。

于是出现一种很难看的情形：判定表里 45 条全是无关文档，一路判 0，
看起来「这题没有新增相关项」，实际上相关文档在池子外面，一眼都没被看见。
这个脚本把这个数摆出来——**池外相关文档数**才是补池之后仍然存在的盲区。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from app.db.models import Document
from app.db.session import session_scope
from pool_judge import ENTITY_TERMS, short_id
from sqlalchemy import select


def load_json(name: str) -> dict:
    return json.loads((API_DIR / "data" / name).read_text(encoding="utf-8"))


async def count_hits(terms: list[str]) -> list[tuple[str, str, str]]:
    """返回该实体在正文/标题里出现的全部文档：(id, 标题, 命中位置)。"""

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Document.id, Document.title, Document.content_text)
            )
        ).all()

    found = []
    for document_id, title, body in rows:
        text = f"{title or ''}\n{body or ''}"
        for term in terms:
            if term in text:
                found.append((str(document_id), title or "", term))
                break
    return found


async def main() -> int:
    pool = load_json("eval_pool.json")["questions"]
    questions = load_json("eval_questions.json")["questions"]
    expect = {q["id"]: q.get("expect", []) for q in questions}

    header = f"{'题号':<6}{'实体':<14}{'全库':>5}{'池内':>5}{'已标注':>7}{'池外':>5}"
    print(header)
    print("-" * 46)

    detail: list[dict] = []
    for question_id, terms in ENTITY_TERMS.items():
        entry = pool.get(question_id)
        if entry is None:
            continue
        hits = await count_hits(terms)
        pool_ids = set(entry["candidates"])
        labelled = set(entry["labelled"])
        in_pool = [h for h in hits if h[0] in pool_ids]
        in_labelled = [h for h in hits if h[0] in labelled]
        outside = [h for h in hits if h[0] not in pool_ids]
        print(
            f"{question_id:<6}{terms[0]:<14}{len(hits):>5}{len(in_pool):>5}"
            f"{len(in_labelled):>7}{len(outside):>5}"
        )
        detail.append(
            {
                "qid": question_id,
                "entity": terms[0],
                "corpus_hits": len(hits),
                "in_pool": len(in_pool),
                "in_labelled": len(in_labelled),
                "outside_pool": [
                    {"short": short_id(h[0]), "title": h[1]} for h in outside
                ],
                "expect": expect.get(question_id, []),
            }
        )

    print("-" * 46)
    print("池外 = 全库提及该实体、但六路 top-20 一篇都没召回的文档数")
    out = API_DIR / "data" / "entity_coverage.json"
    out.write_text(
        json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"明细 -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
