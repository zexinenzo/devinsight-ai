"""评测集补池（pooling）：把「从来没被任何一路召回」的相关文档找出来。

**为什么必须补这一步。**

现在的评测逻辑是：40 道题，每题人工标注「哪几篇算答案」，然后算 R@5。
所有指标的分母都是这个标注集。问题在于——标注集是**看着检索结果标出来的**：
当初滚轮往下翻看到的、觉得能回答问题的，才被记进 `expect`。于是：

    一篇高度相关、但从来没进过任何一路召回的文档，
    根本不在标注集里，于是它永远不构成漏检。

这就是自证偏差（self-selection bias）。它让 R@5 偏高，而且偏得不可知——
因为漏洞恰好落在「系统看不见的地方」，测不出来自己有多差。

IR 领域的标准解法是 pooling：把多个系统的 top-N 结果取并集，对并集里
**尚未标注**的文档逐条判定相关性，补进标注集，再重算指标。

**池深为什么是 20。**

评估深度 k 最大取 10（R@5 / R@10 / nDCG@5）。池深必须大于 k，否则
「没进池」和「没被检索到」无法区分。取 20 = 2 × k_max，是常见做法：
既覆盖了「本来能进前 10」的全部可能，又不至于把池子撑到判不完。

**六路信号的语义（都是生产路径上真实存在的一路）。**

| 路 | 代码 | 它认什么 |
|---|---|---|
| chunk | `_vector_hits` | 单块最相似：只要有一处提到就够 |
| doc | `_document_vector_hits` | 整篇平均最相似：整篇都在讲这个主题 |
| header | `_header_vector_hits` | 标题摘要块：这篇整体在讲什么 |
| keyword | `_keyword_hits` | 字面命中：专有名词、期号 |
| rrf | `list_documents(hybrid, rerank=False)` | 四路融合后的顺序 |
| reranked | `list_documents(hybrid, rerank=True)` | 交叉编码器投票后的顺序 |

后两路不是前四路的冗余：RRF 只看名次不看分数，精排又只在前 N 条里重排，
两者都会让「某篇本来召回到了、但被挤出可评估位置」的文档掉出视野。

用法：

    python scripts/pool_eval_candidates.py                  # 导出候选池与判定表
    python scripts/pool_eval_candidates.py --depth 30       # 加深池子（判定量线性上升）
    python scripts/pool_eval_candidates.py --no-rerank      # 跳过精排路（省时间，池会变小）

产出（默认写到 `apps/api/data/`）：

- `eval_pool.json`：机器可读，含每题每篇候选的六路名次，判定后用它回填
- `eval_pool_review.csv`：人工判定表，UTF-8-sig，Excel 直接打开不乱码，`relevant` 列留空
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))
# eval_retrieval 在同一个目录下，不是包，靠 path 直接 import 复用它的 ground truth 解析：
# 「expect 标题片段 → document_id」这套匹配规则只该有一份实现，两处各写一份必然漂移。
sys.path.insert(0, str(SCRIPTS_DIR))

from app.core.errors import AppError
from app.db.models import Document, Source
from app.db.session import session_scope
from app.services import document_service, embedding_service
from app.services.document_service import (
    _document_vector_hits,
    _header_vector_hits,
    _keyword_hits,
    _vector_hits,
)
from eval_retrieval import resolve_ground_truth
from sqlalchemy import select

QUESTIONS_PATH = API_DIR / "data" / "eval_questions.json"
DEFAULT_DEPTH = 20
ROUTES = ("chunk", "doc", "header", "keyword", "rrf", "reranked")

# 补池和线上查询走同一套过滤条件：不传过滤参数 = 全库检索。
# 分开写成一个 dict 而不是每处重复 6 个 None，是为了以后加过滤时只改一处。
NO_FILTERS: dict = {
    "source_key": None,
    "region": None,
    "language": None,
    "topic": None,
    "content_type": None,
    "published_from": None,
    "published_to": None,
    # 与 eval_retrieval 保持一致：转载副本不进候选池，否则池子被同一篇稿子灌满
    "include_reposts": False,
}


async def collect_pool(
    session, query: str, embedder, depth: int, with_rerank: bool
) -> tuple[dict[uuid.UUID, dict[str, int]], dict[uuid.UUID, int], list[str]]:
    """跑六路召回，返回 ({document_id: {路: 名次}}, {document_id: 命中块起点}, 不可用的路)。

    不可用的路要**显式返回**而不是吞掉：少一路就是少一片视野，
    池子会静默变小，而「池子小了」和「真的没有相关文档」在结果里长得一样。

    `starts` 单独返回：判相关性时需要看「系统到底命中了正文哪一段」。
    周刊一期讲五个话题，摘要只代表其中一个，只看摘要判「能不能回答」会漏判——
    而判定质量是整个补池工作的价值所在，宁可多存一个字段。
    """

    ranks: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    starts: dict[uuid.UUID, int] = {}
    unavailable: list[str] = []

    # 一次编码，四路共用。六路里只有两路（list_documents）内部会重复编码，
    # 那是生产函数的接口约定，不值得为了省一次编码去改它。
    query_vector = await asyncio.to_thread(embedder.embed_query, query)
    model = embedder.name

    chunk_hits = await _vector_hits(
        session,
        query_vector=query_vector,
        model=model,
        candidates=depth,
        **NO_FILTERS,
    )
    for position, (document_id, _distance, char_start) in enumerate(
        chunk_hits, start=1
    ):
        ranks[document_id]["chunk"] = position
        # 同一篇可能被多路命中，但 char_start 只由单块向量那一路提供：
        # 另外几路返回的是文档级顺序，没有「哪个位置最像」这个概念。
        starts[document_id] = char_start

    doc_ids = await _document_vector_hits(
        session, query_vector=query_vector, model=model, limit=depth, **NO_FILTERS
    )
    for position, document_id in enumerate(doc_ids, start=1):
        ranks[document_id]["doc"] = position

    header_ids = await _header_vector_hits(
        session, query_vector=query_vector, model=model, limit=depth, **NO_FILTERS
    )
    for position, document_id in enumerate(header_ids, start=1):
        ranks[document_id]["header"] = position

    keyword_ids = await _keyword_hits(session, q=query, limit=depth, **NO_FILTERS)
    for position, document_id in enumerate(keyword_ids, start=1):
        ranks[document_id]["keyword"] = position

    # 后两路走对外接口而不是内部函数：它们包含 RRF 融合与精排投票这两步
    # 「会改变名次」的逻辑，重写一遍就等于评估另一个系统。
    plan = [("rrf", False)] + ([("reranked", True)] if with_rerank else [])
    for route, use_rerank in plan:
        try:
            page = await document_service.list_documents(
                session, q=query, limit=depth, search_mode="hybrid", rerank=use_rerank
            )
        except AppError as error:
            # 典型情况：精排模型没装。生产接口会明确报 rerank_unavailable，
            # 这里如实记下来，而不是让这一路空着冒充「没有结果」。
            unavailable.append(f"{route}: {error.code}")
            continue
        for position, (document, _score) in enumerate(page.items, start=1):
            ranks[document.id][route] = position

    return dict(ranks), starts, unavailable


async def load_metadata(session, ids: set[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """一次查完所有候选的标题 / 来源 / 日期，避免判 1000 条时查 1000 次库。"""

    if not ids:
        return {}
    rows = (
        await session.execute(
            select(
                Document.id, Document.title, Source.source_key, Document.published_at
            )
            .join(Source, Source.id == Document.source_id)
            .where(Document.id.in_(list(ids)))
        )
    ).all()
    return {
        row[0]: {
            "title": row[1],
            "source": row[2],
            "published_at": row[3].date().isoformat() if row[3] else None,
        }
        for row in rows
    }


def best_rank(route_ranks: dict[str, int]) -> int:
    """一条候选在多路里的最好名次，用于判定表排序：先判最有希望的。"""

    return min(route_ranks.values()) if route_ranks else 10**9


async def main(args) -> int:
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = payload["questions"]

    try:
        embedder = embedding_service.get_embedder()
    except Exception as error:  # noqa: BLE001
        # 向量那三路全靠它。缺模型时四路里的三路会直接失效，池子会只剩字面命中，
        # 那种池子做出来的判定比不做更糟——宁可失败。
        print(f"向量模型不可用，无法补池：{error}")
        return 1

    pool_by_question: dict[str, dict] = {}
    per_question_rows: list[dict] = []
    route_missing: dict[str, int] = defaultdict(int)

    async with session_scope() as session:
        truth, unresolved = await resolve_ground_truth(session, questions)
        if unresolved:
            print("以下问题的 expect 在库里找不到对应文档，补池结果不可信：")
            for item in unresolved:
                print("  -", item)
            print("先修正 eval_questions.json，再补池。")
            return 1

        print(f"评测集 {len(questions)} 题 | 池深 {args.depth} | 六路并集\n")
        for index, question in enumerate(questions, start=1):
            ranks, starts, unavailable = await collect_pool(
                session,
                question["q"],
                embedder,
                args.depth,
                with_rerank=not args.no_rerank,
            )
            for item in unavailable:
                route_missing[item.split(":")[0]] += 1

            expected = truth[question["id"]]
            in_pool = set(ranks) & expected
            new_ids = set(ranks) - expected

            pool_by_question[question["id"]] = {
                "type": question["type"],
                "q": question["q"],
                "labelled": sorted(str(i) for i in expected),
                "candidates": {
                    str(document_id): route_ranks
                    for document_id, route_ranks in ranks.items()
                },
                # 命中块起点只对「单块向量那一路命中的文档」有意义；
                # 只被关键词或聚合路召回的文档没有这个值，判定时退回正文开头。
                "starts": {
                    str(document_id): start for document_id, start in starts.items()
                },
            }
            per_question_rows.append(
                {
                    "id": question["id"],
                    "type": question["type"],
                    "pool": len(ranks),
                    "labelled": len(expected),
                    "labelled_in_pool": len(in_pool),
                    "new": len(new_ids),
                    "unavailable": ",".join(unavailable),
                }
            )
            print(
                f"  [{index:>2}/{len(questions)}] {question['id']:<5}"
                f"池 {len(ranks):>3}  已标注 {len(expected):>2}"
                f"  入池 {len(in_pool):>2}  待判 {len(new_ids):>3}"
            )

        all_ids = {
            uuid.UUID(i) for q in pool_by_question.values() for i in q["candidates"]
        }
        meta = await load_metadata(session, all_ids)

    # ---- 汇总 ----
    total_pool = sum(r["pool"] for r in per_question_rows)
    total_truth = sum(r["labelled"] for r in per_question_rows)
    total_in_pool = sum(r["labelled_in_pool"] for r in per_question_rows)
    total_new = sum(r["new"] for r in per_question_rows)

    print("\n" + "=" * 68)
    print(f"候选条目合计（含重复）{total_pool}  |  去重后 {len(all_ids)} 篇文档")
    print(
        f"已标注 {total_truth} 篇，其中 {total_in_pool} 篇进了池"
        f"（覆盖率 {total_in_pool / total_truth:.1%}）"
    )
    print(f"待判定（池内未标注）{total_new} 条")
    if route_missing:
        print("\n以下路在部分题目上不可用，池子因此偏小，判定结论需注明：")
        for route, count in route_missing.items():
            print(f"  - {route}: {count} 题")
    print("=" * 68)

    write_outputs(
        version=payload.get("version"),
        revision=payload.get("revision"),
        depth=args.depth,
        with_rerank=not args.no_rerank,
        pool_by_question=pool_by_question,
        questions=questions,
        meta=meta,
        json_path=Path(args.json),
        csv_path=Path(args.csv),
    )
    print(f"\n候选池：{args.json}")
    print(f"判定表：{args.csv}（{total_new} 条，填 relevant 列：1 相关 / 0 不相关）")
    return 0


def write_outputs(
    *,
    version,
    revision,
    depth: int,
    with_rerank: bool,
    pool_by_question: dict,
    questions: list[dict],
    meta: dict,
    json_path: Path,
    csv_path: Path,
) -> None:
    """落盘。抽成同步函数是为了把阻塞 IO 挪出事件循环（ASYNC230），
    也顺手让 main 只剩「取数 + 打印」两件事。"""

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "version": version,
                "revision": revision,
                "depth": depth,
                "routes": list(ROUTES) if with_rerank else list(ROUTES[:-1]),
                "questions": pool_by_question,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 双击打开中文标题不乱码。普通 utf-8 在中文 Windows 上会花屏。
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "题号",
                "类型",
                "问题",
                "候选序号",
                "文档ID",
                "标题",
                "来源",
                "日期",
                "命中路",
                "relevant",
            ]
        )
        for question in questions:
            entry = pool_by_question[question["id"]]
            candidates = [
                (document_id, route_ranks)
                for document_id, route_ranks in entry["candidates"].items()
                if document_id not in entry["labelled"]
            ]
            # 按「最好名次」升序：同一题里先判最可能相关的，判到后面疲劳时
            # 剩下的都是明显不相关的，误判代价低。
            candidates.sort(key=lambda item: best_rank(item[1]))
            for order, (document_id, route_ranks) in enumerate(candidates, start=1):
                info = meta.get(uuid.UUID(document_id), {})
                routes = " ".join(
                    f"{route}#{rank}"
                    for route, rank in sorted(route_ranks.items(), key=lambda x: x[1])
                )
                writer.writerow(
                    [
                        question["id"],
                        question["type"],
                        question["q"],
                        order,
                        document_id,
                        info.get("title", ""),
                        info.get("source", ""),
                        info.get("published_at", ""),
                        routes,
                        "",
                    ]
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="评测集补池：导出待判定候选")
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="每路取多少条")
    parser.add_argument(
        "--no-rerank", action="store_true", help="跳过精排路（省时间，池会变小）"
    )
    parser.add_argument(
        "--json",
        default=str(API_DIR / "data" / "eval_pool.json"),
        help="候选池 JSON 输出路径",
    )
    parser.add_argument(
        "--csv",
        default=str(API_DIR / "data" / "eval_pool_review.csv"),
        help="判定表 CSV 输出路径",
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
