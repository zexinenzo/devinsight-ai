"""判定工作表：把候选池导成「可读、无名次、乱序」的清单，并把判定结果合并回来。

**为什么工作表里没有名次、顺序还打乱。**

判定要回答的是「这篇文档的内容能不能实质回答这个问题」，与「它排第几」无关。
可如果清单按名次排序，判定的人（不管是人还是模型）会不自觉地给前排放宽标准：
排第 1 的「大概相关」就判相关，排第 18 的「大概相关」就判不相关。
那测出来的就不是相关集，而是排序的回声——pooling 的全部意义会被这一步毁掉。

所以顺序按 `md5(题号:文档id)` 排：与名次无关、每次重跑完全一致、可复现。

**为什么判定单元是「短 id」而不是序号。**

序号错一位，后面全错，而且错得看不出来。8 位短 id 在 672 篇里唯一（脚本会校验），
写错一位只会匹配不到，不会静默错位。

用法：

    python scripts/pool_judge.py export              # 全部 40 题，每批 5 题
    python scripts/pool_judge.py export --size 8     # 换批大小
    python scripts/pool_judge.py stats               # 只看规模与证据完整度
    python scripts/pool_judge.py merge --labels apps/api/data/eval_pool_labels.json

判定标准（写进工作表表头，防止跨批次漂移）：

    1 = 文档内容能实质回答该问题：直接给出答案、给出核心论据，
        或是同一事件的关键报道（多源报道都算答案）
    0 = 只提到关键词、讲相邻话题但不回答问题、体裁不符（目录页/导航页）
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import random
import sys
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

from app.db.models import Document, Source
from app.db.session import session_scope
from sqlalchemy import select

POOL_PATH = API_DIR / "data" / "eval_pool.json"
BATCH_DIR = API_DIR / "data" / "pool_batches"
REVIEW_SAMPLE_PATH = API_DIR / "data" / "pool_review_sample.csv"
LABELLED_PATH = API_DIR / "data" / "eval_pool_labelled.json"
LABELS_DIR = API_DIR / "data" / "pool_labels"

# 证据片段的长度。判定需要「这段原文在讲什么」，120 字够看清一段话的语义；
# 前导 20 字是为了让截断的句子不那么断裂。
EVIDENCE_CHARS = 120
EVIDENCE_LEAD = 20

CRITERIA = """判定标准：
1 = 文档内容能实质回答该问题（直接给答案 / 给核心论据 / 是同一事件的关键报道）
0 = 只提到关键词、讲相邻话题但不回答、体裁不符（目录页、导航页、纯链接聚合）
"""


def short_id(document_id: str) -> str:
    """8 位短 id：uuid 去掉横线取前 8 位。"""

    return document_id.replace("-", "")[:8]


def order_key(question_id: str, document_id: str) -> str:
    """与名次无关的稳定排序键。用 md5 而不是 random：重跑顺序必须一致。"""

    return hashlib.md5(f"{question_id}:{document_id}".encode()).hexdigest()


def load_pool() -> dict:
    if not POOL_PATH.exists():
        raise SystemExit(
            f"找不到候选池 {POOL_PATH}，先跑 scripts/pool_eval_candidates.py"
        )
    return json.loads(POOL_PATH.read_text(encoding="utf-8"))


async def load_meta(ids: set[uuid.UUID]) -> dict[str, dict]:
    """标题 + 摘要 + 正文。正文留着，导出时按命中位置切一段出来。"""

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    Document.id,
                    Document.title,
                    Document.summary,
                    Document.content_text,
                    Source.source_key,
                )
                .join(Source, Source.id == Document.source_id)
                .where(Document.id.in_(list(ids)))
            )
        ).all()
    return {
        str(row[0]): {
            "title": row[1],
            "summary": " ".join((row[2] or "").split()),
            "body": " ".join((row[3] or "").split()),
            "source": row[4],
        }
        for row in rows
    }


def evidence_text(info: dict, start: int | None) -> str:
    """拼判定要看的证据：优先取「系统命中的那段正文」，没有正文则退回摘要。

    为什么以命中块为准而不是摘要：周刊一期讲五个话题，摘要写的是当期主话题，
    而问题可能命中正文里另一个小节。摘要看着完全无关、正文那一段恰好答得上，
    是这批语料里最常见的形态。反过来只看摘要会系统性漏判——
    而漏判的正是补池要抓的那类文档。
    """

    body = info.get("body") or ""
    if body and start is not None and start > 0:
        return body[max(0, start - EVIDENCE_LEAD) : start + EVIDENCE_CHARS]
    # 没有命中位置时退回摘要，而不是正文开头：周刊每期的正文开头是
    # 「这里记录每周值得分享的科技内容」这类固定模板，看了等于没看。
    summary = info.get("summary") or ""
    if summary:
        return summary[:EVIDENCE_CHARS]
    return body[:EVIDENCE_CHARS] if body else "(无正文无摘要，仅标题可判)"


def pending_of(entry: dict) -> list[str]:
    """一题里所有未标注的候选（保持池里的 key 顺序，排序交给 order_key）。"""

    return [k for k in entry["candidates"] if k not in entry["labelled"]]


# 实体类问题要查的字面词。为什么不让程序从 query 里自动切词：
# 「GPT-4 参数量」自动切出来的是「GPT-4」和「参数量」，而「参数量」在讲
# 任何模型尺寸的文章里都有，命中了也不能说明它答的是 GPT-4。这里手动写死，
# 因为「哪个词才算实体」是判定标准的一部分，不该由分词器决定。
ENTITY_TERMS: dict[str, list[str]] = {
    "n01": ["Strix Halo"],
    "n02": ["何小鹏"],
    "n03": ["李飞飞"],
    "n04": ["Product Hunt"],
    "n05": ["GPT-4"],
    "n06": ["阮一峰"],
}

# 字面命中的上下文窗口。比向量命中块短：查实体名只要看清
# 「这句话在讲它什么」，不需要一整段。
ENTITY_LEAD = 40
ENTITY_CHARS = 120


def literal_context(body: str, terms: list[str]) -> tuple[str, str] | None:
    """在正文里找实体名，返回 (命中的词, 上下文)。找不到返回 None。"""

    for term in terms:
        index = body.find(term)
        if index >= 0:
            snippet = body[max(0, index - ENTITY_LEAD) : index + ENTITY_CHARS]
            return term, snippet
    return None


def check_short_ids(pool: dict) -> None:
    """短 id 必须唯一，否则判定结果会张冠李戴。"""

    seen: dict[str, str] = {}
    for entry in pool["questions"].values():
        for document_id in entry["candidates"]:
            key = short_id(document_id)
            if key in seen and seen[key] != document_id:
                raise SystemExit(
                    f"短 id 冲突：{key} 同时对应 {seen[key]} 和 {document_id}"
                )
            seen[key] = document_id


async def do_export(args) -> int:
    pool = load_pool()
    check_short_ids(pool)

    all_ids = {
        uuid.UUID(document_id)
        for entry in pool["questions"].values()
        for document_id in entry["candidates"]
    }
    meta = await load_meta(all_ids)

    items = list(pool["questions"].items())
    chunks = [items[i : i + args.size] for i in range(0, len(items), args.size)]
    BATCH_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for index, chunk in enumerate(chunks, start=1):
        lines = [f"# 批次 {index}/{len(chunks)}", CRITERIA, ""]
        count = 0
        for question_id, entry in chunk:
            pending = sorted(pending_of(entry), key=lambda d: order_key(question_id, d))
            lines.append(f"## {question_id} | {entry['type']} | {entry['q']}")
            lines.append(
                f"待判 {len(pending)} 条（已标注 {len(entry['labelled'])} 篇，不在本表）"
            )
            lines.append("")
            for document_id in pending:
                info = meta.get(document_id, {})
                title = (info.get("title") or "(无标题)").strip()
                start = entry.get("starts", {}).get(document_id)
                # 单行一条：标题在前给上下文，后面接命中块。省掉换行不是为了紧凑，
                # 是因为 1767 条要分 8 批读完，每条省一行就等于少读 1767 行。
                lines.append(
                    f"[{short_id(document_id)}] {title} | {evidence_text(info, start)}"
                )
                count += 1
            lines.append("")
        total += count
        path = BATCH_DIR / f"batch{index:02d}.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(
            f"批次 {index}/{len(chunks)}：{len(chunk)} 题 / {count} 条 -> {path.name}"
        )

    print(
        f"\n合计 {total} 条，分 {len(chunks)} 批。判定结果写到 eval_pool_labels.json："
    )
    print('  {"f01": {"7ef84e15": 1, "eb389c3e": 0, ...}, ...}')
    return 0


async def do_stats(args) -> int:
    pool = load_pool()
    check_short_ids(pool)
    all_ids = {
        uuid.UUID(document_id)
        for entry in pool["questions"].values()
        for document_id in entry["candidates"]
    }
    meta = await load_meta(all_ids)

    rows = []
    for question_id, entry in pool["questions"].items():
        pending = pending_of(entry)
        no_summary = sum(1 for d in pending if not meta.get(d, {}).get("summary"))
        rows.append(
            {
                "id": question_id,
                "type": entry["type"],
                "labelled": len(entry["labelled"]),
                "pool": len(entry["candidates"]),
                "pending": len(pending),
                "no_summary": no_summary,
            }
        )

    header = f"{'题号':<6}{'类型':<7}{'已标注':>7}{'池':>5}{'待判':>6}{'无摘要':>7}"
    print(header)
    print("-" * 40)
    for row in rows:
        print(
            f"{row['id']:<6}{row['type']:<7}{row['labelled']:>7}"
            f"{row['pool']:>5}{row['pending']:>6}{row['no_summary']:>7}"
        )
    print("-" * 40)
    print(
        f"合计待判 {sum(r['pending'] for r in rows)} 条，"
        f"其中 {sum(r['no_summary'] for r in rows)} 条无摘要（只能凭标题判，结论需注明）"
    )
    return 0


async def do_entity(args) -> int:
    """实体类问题改用「字面命中」取证，导出到单独的文件。

    为什么不复用 export 的向量命中块：实体名（人名、产品名）在向量空间里
    几乎没有可分的语义质量，最相似的那一块往往是噪声——问「何小鹏」，
    最相似块却是「小鹏汽车发布电动载人飞行器」，讲的是产品不是人。
    照这种东西判定只会一路判 0，把「没召回到」写成「不相关」。

    字面命中是可核验的：实体名在不在正文里、在哪一句、那句话以它为主语还是
    只是列表里带过，一眼能看。代价是漏掉别名（「小鹏」「XPeng」），
    所以无字面命中的条目仍然保留在表里，标成「无字面命中」让人自己看一眼标题。
    """

    pool = load_pool()
    check_short_ids(pool)
    all_ids = {
        uuid.UUID(document_id)
        for entry in pool["questions"].values()
        for document_id in entry["candidates"]
    }
    meta = await load_meta(all_ids)

    question_ids = (
        args.qids.split(",")
        if args.qids
        else [q for q in pool["questions"] if q in ENTITY_TERMS]
    )
    path = BATCH_DIR / "entities.txt"
    lines = ["# 实体类问题：字面命中取证", CRITERIA, ""]
    hits = 0
    total = 0
    for question_id in question_ids:
        entry = pool["questions"][question_id]
        terms = ENTITY_TERMS.get(question_id)
        if not terms:
            print(f"跳过 {question_id}：没登记实体词")
            continue
        pending = sorted(pending_of(entry), key=lambda d: order_key(question_id, d))
        body_hits: list[str] = []
        no_hit: list[str] = []
        for document_id in pending:
            info = meta.get(document_id, {})
            title = (info.get("title") or "(无标题)").strip()
            found = literal_context(info.get("body") or "", terms)
            if found:
                term, snippet = found
                body_hits.append(f"[{short_id(document_id)}] {title} | {term} → {snippet}")
            else:
                no_hit.append(
                    f"[{short_id(document_id)}] {title} | 无字面命中（仅标题可判）"
                )
        total += len(pending)
        hits += len(body_hits)
        lines.append(
            f"## {question_id} | {entry['q']} | 字面命中 {len(body_hits)} / {len(pending)}"
        )
        lines.append("")
        lines.extend(body_hits)
        lines.append("")
        lines.extend(no_hit)
        lines.append("")

    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(question_ids)} 题 / {total} 条，字面命中 {hits} 条 -> {path}")
    return 0


def load_labels(path: Path) -> dict:
    """读判定结果。既接受单个 JSON，也接受目录（分批写、最后合并）。

    判定是分 8 批做的，每批一个文件：一次性写 1767 条的大 JSON 容易在中途
    写坏或漏写，而「漏写」在合并时会表现成「某题没判过」，和被明确拒绝区分得开。
    """

    if path.is_dir():
        merged: dict = {}
        for file in sorted(path.glob("*.json")):
            merged.update(json.loads(file.read_text(encoding="utf-8")))
        return merged
    return json.loads(path.read_text(encoding="utf-8"))


async def do_label(args) -> int:
    """把「整题同判」的结论落盘，省掉手工抄 200 多个短 id。

    只用于「已经逐条看过、结论全是同一个值」的题。抄写 225 个 8 位 id
    出错率远高于看错一条：错一位在 merge 里表现成「缺一条 + 多一条」，
    还得回头查是哪条。这里让程序从池子里直接取 id，人只对结论负责。
    """

    pool = load_pool()
    check_short_ids(pool)
    path = LABELS_DIR / f"batch{args.batch:02d}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    for question_id in args.qids.split(","):
        entry = pool["questions"].get(question_id)
        if entry is None:
            raise SystemExit(f"池子里没有 {question_id}")
        pending = pending_of(entry)
        mapping = {short_id(d): args.value for d in pending}
        # 例外项：整题同判之后单独抬出来的那几条。短 id 必须真在池子里，
        # 否则是抄错了——静默忽略会让「我以为标了 1」变成实际标 0。
        for key in (args.ones or "").split(","):
            key = key.strip()
            if not key:
                continue
            if key not in mapping:
                raise SystemExit(f"{question_id}: 短 id {key} 不在待判列表里")
            mapping[key] = 1 if args.value == 0 else 0
        existing[question_id] = mapping
        ones = sum(1 for v in mapping.values() if v == 1)
        print(f"{question_id}: 待判 {len(pending)} 条，相关 {ones} 条")

    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        # 显式 LF：Windows 上默认写 CRLF，而项目在 WSL 里跑，
        # 混进来的 CRLF 会让下一次 diff 显示"整个文件都改了"。
        newline="\n",
    )
    print(f"-> {path.name}（{len(existing)} 题）")
    return 0


async def do_merge(args) -> int:
    """校验判定完整性 → 生成抽检表 → 汇总新增相关项。

    完整性是硬校验：缺一条就整体失败，不允许「判了一半」被当成判完。
    因为漏判的条目会被当成「不相关」参与重算，指标会静默偏高——
    这正是补池要消灭的那类错误，不能自己再犯一次。
    """

    pool = load_pool()
    check_short_ids(pool)
    labels = load_labels(Path(args.labels))

    expected: dict[str, dict[str, str]] = {}
    for question_id, entry in pool["questions"].items():
        expected[question_id] = {short_id(d): d for d in pending_of(entry)}

    problems: list[str] = []
    for question_id, mapping in expected.items():
        got = labels.get(question_id)
        if got is None:
            problems.append(f"{question_id}: 没有判定结果")
            continue
        missing = set(mapping) - set(got)
        extra = set(got) - set(mapping)
        if missing:
            problems.append(
                f"{question_id}: 缺 {len(missing)} 条（{sorted(missing)[:3]}…）"
            )
        if extra:
            problems.append(f"{question_id}: 多出 {len(extra)} 条，短 id 对不上池子")
        for key, value in got.items():
            if value not in (0, 1):
                problems.append(f"{question_id}/{key}: 判定值 {value!r} 不是 0/1")
    if problems:
        print("判定结果不完整，拒绝合并：")
        for item in problems[:20]:
            print("  -", item)
        return 1

    all_ids = {
        uuid.UUID(document_id)
        for entry in pool["questions"].values()
        for document_id in entry["candidates"]
    }
    meta = await load_meta(all_ids)

    new_relevant = []
    for question_id, mapping in expected.items():
        for key, document_id in mapping.items():
            if labels[question_id][key] == 1:
                new_relevant.append(
                    {
                        "qid": question_id,
                        "type": pool["questions"][question_id]["type"],
                        "q": pool["questions"][question_id]["q"],
                        "doc_id": document_id,
                        "short": key,
                        "label": 1,
                        **meta.get(document_id, {}),
                    }
                )

    # ---- 抽检表：全部判 1 + 固定种子抽 40 条判 0 ----
    zeros = [
        {
            "qid": question_id,
            "type": pool["questions"][question_id]["type"],
            "q": pool["questions"][question_id]["q"],
            "doc_id": document_id,
            "short": key,
            "label": 0,
            **meta.get(document_id, {}),
        }
        for question_id, mapping in expected.items()
        for key, document_id in mapping.items()
        if labels[question_id][key] == 0
    ]
    random.Random(args.seed).shuffle(zeros)
    sample = new_relevant + zeros[: args.zeros]

    with REVIEW_SAMPLE_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "题号",
                "类型",
                "问题",
                "短ID",
                "标题",
                "摘要",
                "模型判定",
                "你的判定(1/0)",
                "备注",
            ]
        )
        for item in sample:
            writer.writerow(
                [
                    item["qid"],
                    item["type"],
                    item["q"],
                    item["short"],
                    item.get("title", ""),
                    (item.get("summary") or "")[:120],
                    "相关" if item["label"] == 1 else "不相关",
                    "",
                    "",
                ]
            )

    LABELLED_PATH.write_text(
        json.dumps(
            {
                "labels": labels,
                "new_relevant": new_relevant,
                "counts": {
                    "judged": sum(len(v) for v in labels.values()),
                    "new_relevant": len(new_relevant),
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )

    # ---- 汇总 ----
    by_type: dict[str, int] = {}
    for item in new_relevant:
        by_type[item["type"]] = by_type.get(item["type"], 0) + 1

    print(f"判定完成 {sum(len(v) for v in labels.values())} 条")
    print(f"新增相关 {len(new_relevant)} 条（按类型：{by_type}）")
    print(f"抽检表 {len(sample)} 条 -> {REVIEW_SAMPLE_PATH.name}")
    print(f"完整标注 -> {LABELLED_PATH.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="候选池判定工作表")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="导出分批判定工作表")
    p_export.add_argument("--size", type=int, default=5, help="每批多少题")
    p_export.set_defaults(func=do_export)

    p_stats = sub.add_parser("stats", help="只看规模与证据完整度")
    p_stats.set_defaults(func=do_stats)

    p_entity = sub.add_parser("entity", help="实体类问题按字面命中取证")
    p_entity.add_argument("--qids", default="", help="逗号分隔的题号，默认全部实体题")
    p_entity.set_defaults(func=do_entity)

    p_label = sub.add_parser("label", help="整题同判时直接落盘")
    p_label.add_argument("--qids", required=True, help="逗号分隔的题号")
    p_label.add_argument("--value", type=int, required=True, choices=[0, 1])
    p_label.add_argument("--batch", type=int, required=True, help="写到哪个批次文件")
    p_label.add_argument("--ones", default="", help="例外项短 id，逗号分隔（会被置为相反值）")
    p_label.set_defaults(func=do_label)

    p_merge = sub.add_parser("merge", help="校验判定结果并生成抽检表")
    p_merge.add_argument(
        "--labels", default=str(API_DIR / "data" / "eval_pool_labels.json")
    )
    p_merge.add_argument("--zeros", type=int, default=40, help="抽多少条判 0 的做复核")
    p_merge.add_argument("--seed", type=int, default=20261119)
    p_merge.set_defaults(func=do_merge)

    args = parser.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
