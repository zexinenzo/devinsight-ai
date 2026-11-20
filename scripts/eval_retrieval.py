"""检索质量评测：用固定的问题集量出每种检索模式的召回与排序指标。

这个脚本存在的理由，是"感觉搜得还行"不能用来做决策。

上一阶段收尾时留下的判断是"口语提问 top-1 只对 1/3"，那是拿三条查询手测出来的，
既无法复现，也无法判断改动到底是变好还是变坏。要改排序，先得有一把尺子。

**指标为什么是这四个**

- `R@5` / `R@10`（Recall）：正确答案有没有进入前 5 / 前 10 条。检索的第一要务是
  "不漏"，用户翻两页能看见就算成功。多答案题（一个主题对应几篇）按比例算。
- `MRR`：第一条正确答案排在多高。它衡量"用户要不要往下翻"，
  对单答案题特别敏感。
- `nDCG@5`：相关性按位置折损。两个系统 R@5 都是 1.0 时，MRR 也一样时，
  nDCG 还能分出"正确的那篇排第 1 还是第 5"。

**ground truth 怎么来的**

`apps/api/data/eval_questions.json`，40 条，四类：

- `fact`：单篇具体事实（"GPT-3 有多少参数"），答案唯一，考定位能力
- `topic`：多篇主题（"互联网创业还有机会吗"），答案 2–4 篇，考主题召回
- `named`：专有名词，这一档 keyword 本该赢——如果它输了，说明索引坏了而不是排序坏了
- `en`：英文语料，单独报告。**不与中文混算**：HN 的正文只有 600 字片段，
  和阮一峰的 6000 字全文不是一回事，混在一起算出来的平均值没有解释力

标注原则是"内容能实质回答该问题"才算命中，仅仅提到某个关键词不算。
问题措辞刻意避开标题原词，否则测出来的是标题匹配，不是语义检索。

用法：

    python scripts/eval_retrieval.py                    # 三种模式全跑
    python scripts/eval_retrieval.py --mode hybrid      # 只跑一个
    python scripts/eval_retrieval.py --verbose          # 打印每题命中详情
    python scripts/eval_retrieval.py --save out.json    # 存结果，用于跨版本对比
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.db.session import session_scope
from app.services import document_service
from sqlalchemy import text

QUESTIONS_PATH = API_DIR / "data" / "eval_questions.json"
MODES = ("keyword", "vector", "hybrid")
TOP_K = 10

# "第 46 期" 不能写成 LIKE '%第 46 期%'：那会同时匹配第 146、246、346 期。
# 前面加 (?<!\d) 把"紧跟在数字后面的"排除掉，才是一期一答。
_PERIOD_RE = re.compile(r"第\s*(\d+)\s*期")


def _title_matcher(pattern: str) -> re.Pattern[str]:
    """把 expect 里的标题片段编译成精确匹配的正则。"""

    m = _PERIOD_RE.search(pattern)
    if m:
        # 期号类：按数字精确匹配，避免 46 匹配到 146
        return re.compile(rf"(?<!\d){int(m.group(1))}\s*期")
    return re.compile(re.escape(pattern), re.IGNORECASE)


async def resolve_ground_truth(session, questions: list[dict]) -> dict[str, set]:
    """把标题片段解析成 document_id，并报告解析不出来的条目。

    解析失败必须显式报错，不能静默跳过：一条 expect 落空会让这道题
    "永远不可能被答对"，指标会莫名其妙地低，而原因藏在数据里。
    """

    titles = {
        row[0]: row[1]
        for row in (await session.execute(text("SELECT id, title FROM documents"))).all()
    }

    truth: dict[str, set] = {}
    unresolved: list[str] = []
    for question in questions:
        matchers = [_title_matcher(p) for p in question["expect"]]
        hits = {doc_id for doc_id, title in titles.items() if any(m.search(title) for m in matchers)}
        if not hits:
            unresolved.append(f'{question["id"]}: {question["expect"]}')
        truth[question["id"]] = hits
    return truth, unresolved


def load_qrels_overlay(path: Path) -> dict[str, set]:
    """读补池补出来的相关项，按题号聚成 doc_id 集合。

    两种形状都接受：补池脚本的原生输出（`{"new_relevant": [{qid, doc_id}]}`）
    和手写的 `{qid: [doc_id]}`。直接吃原生输出，少一道手抄——
    每抄一次 36 位 uuid 就多一次静默写错的机会，而写错的后果是
    「这条永远不可能被召回」，指标偏低却看不出原因。

    为什么做成「叠加层」而不是直接改 eval_questions.json：
    人工写的 expect 和模型初判的补池项验证强度不同，混在一个文件里就分不开了。
    分开存，报告才能同时给出「补池前 / 补池后」两个数，抽检推翻某几条时也能单独回滚。
    """

    payload = json.loads(path.read_text(encoding="utf-8"))
    if "new_relevant" in payload:
        overlay: dict[str, set] = defaultdict(set)
        for item in payload["new_relevant"]:
            overlay[item["qid"]].add(item["doc_id"])
        return dict(overlay)
    return {qid: set(ids) for qid, ids in payload.items()}


def recall_at(ranked: list, truth: set, k: int) -> float:
    if not truth:
        return 0.0
    return len(set(ranked[:k]) & truth) / len(truth)


def mrr(ranked: list, truth: set) -> float:
    for position, doc_id in enumerate(ranked, start=1):
        if doc_id in truth:
            return 1.0 / position
    return 0.0


def ndcg_at(ranked: list, truth: set, k: int) -> float:
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, doc_id in enumerate(ranked[:k], start=1)
        if doc_id in truth
    )
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(truth), k) + 1))
    return dcg / ideal if ideal else 0.0


async def run_mode(
    session, questions: list[dict], truth: dict, mode: str, rerank: bool | None = None
) -> list[dict]:
    results = []
    for question in questions:
        page = await document_service.list_documents(
            session, q=question["q"], limit=TOP_K, search_mode=mode, rerank=rerank
        )
        ranked = [document.id for document, _ in page.items]
        expected = truth[question["id"]]
        results.append(
            {
                "id": question["id"],
                "type": question["type"],
                "q": question["q"],
                "ranked": [str(i) for i in ranked],
                "expect": [str(i) for i in expected],
                "r5": recall_at(ranked, expected, 5),
                "r10": recall_at(ranked, expected, 10),
                "mrr": mrr(ranked, expected),
                "ndcg5": ndcg_at(ranked, expected, 5),
                # 第一条命中的名次，1 表示直接命中，None 表示前 10 条都没有
                "first_hit": next(
                    (p for p, d in enumerate(ranked, start=1) if d in expected), None
                ),
                "returned": len(ranked),
            }
        )
    return results


def _agg(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "r5": 0.0, "r10": 0.0, "mrr": 0.0, "ndcg5": 0.0}
    return {
        "n": len(rows),
        "r5": sum(r["r5"] for r in rows) / len(rows),
        "r10": sum(r["r10"] for r in rows) / len(rows),
        "mrr": sum(r["mrr"] for r in rows) / len(rows),
        "ndcg5": sum(r["ndcg5"] for r in rows) / len(rows),
    }


def report(all_results: dict[str, list[dict]], verbose: bool) -> None:
    types = ["fact", "topic", "named", "en"]
    header = f'{"模式":<9}{"类型":<8}{"题数":>5}{"R@5":>8}{"R@10":>8}{"MRR":>8}{"nDCG@5":>9}'
    print(header)
    print("-" * len(header))
    for mode, rows in all_results.items():
        by_type = defaultdict(list)
        for row in rows:
            by_type[row["type"]].append(row)
        for type_name in types:
            stats = _agg(by_type.get(type_name, []))
            if stats["n"] == 0:
                continue
            print(
                f'{mode:<9}{type_name:<8}{stats["n"]:>5}'
                f'{stats["r5"]:>8.3f}{stats["r10"]:>8.3f}'
                f'{stats["mrr"]:>8.3f}{stats["ndcg5"]:>9.3f}'
            )
        total = _agg(rows)
        print(
            f'{"":<9}{"合计":<8}{total["n"]:>5}'
            f'{total["r5"]:>8.3f}{total["r10"]:>8.3f}'
            f'{total["mrr"]:>8.3f}{total["ndcg5"]:>9.3f}'
        )
        print("-" * len(header))

    if not verbose:
        return

    print("\n=== 逐题明细（只列前 10 条里没命中的） ===")
    for mode, rows in all_results.items():
        misses = [r for r in rows if r["first_hit"] is None]
        print(f"\n[{mode}] 未命中 {len(misses)}/{len(rows)} 题")
        for row in misses[:12]:
            print(f'  {row["id"]:<5}{row["q"][:44]:<46} 期望 {len(row["expect"])} 篇')


async def main(args) -> int:
    payload = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = payload["questions"]
    modes = args.mode or list(MODES)

    # argparse 的两个开关合成服务层的三态参数
    args.rerank = True if args.require_rerank else (False if args.no_rerank else None)
    if args.rerank is not None:
        print(f"（rerank={'强制开启' if args.rerank else '关闭'}）\n")

    if args.no_doc_agg:
        # 关掉第三路信号，用来回答「这个提升到底是不是聚合带来的」。
        # 直接在模块上替换，不动生产代码——评测脚本不应该为了做对照
        # 在生产路径里留一个 if 分支。
        async def _disabled(*_args, **_kwargs):
            return []

        document_service._document_vector_hits = _disabled
        print("（已关闭文档级聚合信号，用于 A/B 对照）\n")

    async with session_scope() as session:
        truth, unresolved = await resolve_ground_truth(session, questions)
        if unresolved:
            print("以下问题的 ground truth 在库里找不到对应文档，评测结果不可信：")
            for item in unresolved:
                print("  -", item)
            print("先修正 eval_questions.json 里的标题片段，再跑评测。")
            return 1

        if args.qrels:
            overlay = load_qrels_overlay(Path(args.qrels))
            added = 0
            touched = 0
            for question_id, ids in overlay.items():
                if question_id not in truth:
                    print(f"  警告：补池里的 {question_id} 不在评测集中，已跳过")
                    continue
                fresh = ids - truth[question_id]
                if fresh:
                    touched += 1
                truth[question_id] |= fresh
                added += len(fresh)
            # 把「分母变了」这件事明说出来。补池只会增加相关项、不会减少，
            # 所以任何指标下降都不是系统变差了，而是尺子变严了——
            # 这句话必须跟数字一起出现，否则下一个人会以为检索退化了。
            print(
                f"补池相关项：{touched} 题新增 {added} 条"
                f"（共 {sum(len(v) for v in truth.values())} 条相关项）\n"
            )

        all_results = {}
        for mode in modes:
            all_results[mode] = await run_mode(
                session, questions, truth, mode, rerank=args.rerank
            )

    print(f"\n评测集：{len(questions)} 题 | TOP_K={TOP_K} | 库内文档 "
          f"{(await _doc_count())} 篇\n")
    report(all_results, args.verbose)

    if args.save:
        out = Path(args.save)
        out.write_text(
            json.dumps(
                {"questions": payload["version"], "top_k": TOP_K, "results": all_results},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已保存：{out}")
    return 0


async def _doc_count() -> int:
    async with session_scope() as session:
        return int((await session.execute(text("SELECT count(*) FROM documents"))).scalar_one())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="检索质量评测")
    parser.add_argument("--mode", action="append", choices=MODES, help="只跑指定模式，可重复")
    parser.add_argument("--verbose", action="store_true", help="打印未命中题目的明细")
    parser.add_argument(
        "--no-doc-agg", action="store_true", help="关闭文档级聚合信号，用于对照实验"
    )
    # 三态：不传=跟随配置（有模型就重排），--no-rerank=强制不重排（对照组），
    # --require-rerank=强制重排（模型缺失会直接失败，用来确认评测真的用上了模型）
    parser.add_argument(
        "--no-rerank", action="store_true", help="不重排，作为 cross-encoder 的对照组"
    )
    parser.add_argument(
        "--require-rerank",
        action="store_true",
        help="强制要求重排，模型缺失时直接报错而不是静默跳过",
    )
    parser.add_argument("--save", help="把结果存成 JSON，便于跨版本对比")
    parser.add_argument(
        "--qrels",
        help="补池补出来的相关项（pool_judge merge 的输出），叠加到 ground truth 上重算",
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
