"""阶段 7：从文档中抽取结构化事实（实体 + 谓词 + 时间）。

用法（从仓库根执行；这台机器的 `python` 不存在，只有 `python3`，
所以解释器一律走仓库根的 `.venv`）：

    # 先看抽成什么样，不写库
    make extract
    # 等价于
    .venv/bin/python scripts/extract_facts.py --limit 5 --dry-run

    # 确认后写库
    make extract ARGS="--limit 50 --apply"

    # 只看某个时间段、带关键词的文章
    make extract ARGS="--since 2026-08-01 --keyword GPT --apply"

解析与校验在 `app.services.fact_extraction`（纯函数，有测试）；这里只负责挑文档、
调模型、落库。五个取舍值得说：

**1. 谓词闭集。** prompt 里给词表，落库前再校验。模型偶尔发明 `launched` / `推出`，
放进去之后同一个动作会散成多种写法，按谓词聚合的时间线就拼不起来——查
「OpenAI 发布过什么」会漏掉一半。

**2. 日期来源分三档。** `stated`（文中明说）/ `published`（用发文时间兜底）/
`unknown`。把发文时间当事实发生时间会系统性制造假时间线：一篇 2026 年的回顾文章
提到「2024 年发布 GPT-4o」，兜底就把这条挪到了 2026 年。

**3. 幂等靠唯一约束。** 重跑同一批文档不会产生重复行（`uq_fact_claims_identity`），
脚本可以放心中断再跑。先查后插在并发下本来也不可靠。

**4. 宾语粒度在代码层再兜一道。** prompt 要求「名词短语、20 字以内」并给了反例，
但实测里模型仍会输出 `在覆盖 24 个法律领域的评测中排名第一` 这种整句，以及
`Mode 开源了 Mode` 这种自反三元组。超长（>40 字）和主语=宾语的直接丢弃：
进了时间线的脏事实无法自动识别，而丢弃只要重跑就能补回来。

**5. 证据要能被原文验证。** 每条 fact 的 `evidence` 会跟原文做子串比对，分
`exact` / `partial` / `missing`。这不是锦上添花：抽取里最难发现的一类错误是
三元组看着对、证据读着通顺、但原文里根本没有这句话。`missing` 占比不为零就
说明模型在编证据，这批结果不该 `--apply`。

**6. 空数组和解析失败要分开。** `[]` 是模型如实回答「本文没有可抽的事实」，
`None` 是模型说的话根本解析不出 JSON。合并成 `[]` 的后果是：一批文档抽不出东西时，
你分不清是语料没料还是模型没干活。首跑 20 篇里 10 篇 0 条、同时 0 条被拒，
这个「太干净」本身就是可疑信号——所以现在每篇 0 条都会单独打印，解析失败的也单独计数。

分开之后第二跑立刻暴露了真问题：8 篇不是 JSON，而且正好是上一轮抽得出事实的那几篇。
所以又多两件事：解析失败时**把模型自己的错误输出放回上下文再要一次**（温度已经是 0，
原样重发只会得到同一段废话），以及**把原始输出的前 120 字打出来**——
只报「无法解析」等于把最需要的信息丢掉，写前言、尾逗号、空串三种情况的修法完全不同。

**7. 候选集要记住「抽过没有」，而且不能靠事实行来记。** 最早 `pick_documents` 只有
「有正文 + 按发布时间倒序取最新 N 篇」两个条件，于是每轮都把同一批文档重抽一遍：
上一轮 238 条抽出里 90 条是重复，38% 的调用白烧。更贵的是那 342 篇「无事实」文档——
它们一行事实都没写，用 `fact_claims` 有没有行来判断永远是「没抽过」，每轮重判一次。
所以状态记在 `documents.facts_extracted_at` 上，且**只记确定回答**：抽到事实、或明确
「本文没有」都算；截断/空响应/格式错不算，否则一篇被截断的文档会被永久跳过。
改提示词或换模型要全量重来，用 `--reprocess` 显式打开，不要让默认行为悄悄变成重抽。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.db.models import Document, Entity, FactClaim
from app.db.session import session_scope
from app.services.fact_extraction import (
    ExtractedFact,
    build_prompt,
    classify_unparsed,
    evidence_match,
    try_parse_facts_json,
)
from app.services.fact_extraction import (
    to_fact as parse_raw_fact,
)
from app.services.llm_service import ChatMessage, LLMUnavailable, require_llm
from app.services.timeline_service import PREDICATES, normalize_key
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

MAX_BODY_CHARS = 3000


@dataclass(slots=True)
class Stats:
    documents: int = 0
    facts: int = 0
    written: int = 0
    duplicated: int = 0
    rejected_predicate: int = 0
    rejected_reflexive: int = 0
    rejected_too_long: int = 0
    malformed: int = 0
    unparsed: int = 0
    unparsed_truncated: int = 0
    unparsed_empty: int = 0
    unparsed_format: int = 0
    retried: int = 0
    recovered: int = 0
    empty_documents: int = 0
    undated: int = 0
    evidence_exact: int = 0
    evidence_partial: int = 0
    evidence_missing: int = 0
    # 本次被记上 facts_extracted_at 的文档数：下轮不会再被选中
    marked: int = 0


async def resolve_or_create_entity(session, name: str, kind: str) -> Entity | None:
    key = normalize_key(name)
    if not key:
        return None

    stmt = pg_insert(Entity).values(name=name, kind=kind, canonical_key=key)
    stmt = stmt.on_conflict_do_nothing(constraint="uq_entities_kind_canonical")
    await session.execute(stmt)
    # ON CONFLICT DO NOTHING 冲突时不返回行，所以再查一次。
    # 合成 CTE 也行，但可读性差得多；这张表有索引，代价可忽略。
    return (
        await session.execute(
            select(Entity).where(Entity.canonical_key == key, Entity.kind == kind).limit(1)
        )
    ).scalar_one_or_none()


def build_documents_query(args):
    """候选文档的取法。单独拎出来是为了能在不连数据库的情况下断言它的形状。

    默认排除 `facts_extracted_at IS NOT NULL`：模型已经把这篇判定过了，
    再送一次只会得到同一段输出，然后撞上唯一约束。`--reprocess` 打开时才全量重抽
    （改提示词或换模型时用，那是唯一需要重抽的场景）。
    """
    stmt = select(Document).where(
        or_(Document.content_text.is_not(None), Document.summary.is_not(None))
    )
    if not getattr(args, "reprocess", False):
        stmt = stmt.where(Document.facts_extracted_at.is_(None))
    if args.since:
        stmt = stmt.where(Document.published_at >= datetime.fromisoformat(args.since))
    if args.keyword:
        pattern = f"%{args.keyword}%"
        stmt = stmt.where(or_(Document.title.ilike(pattern), Document.summary.ilike(pattern)))
    # 有正文的优先：摘要常常只有「某公司发布了新产品」，缺日期和细节
    stmt = stmt.order_by(Document.published_at.desc().nullslast()).limit(args.limit)
    return stmt


async def pick_documents(session, args) -> list[Document]:
    return list((await session.execute(build_documents_query(args))).scalars().all())


def document_text(document: Document) -> str:
    body = document.content_text or document.summary or ""
    head = f"标题：{document.title}\n"
    if document.published_at:
        head += f"发布时间：{document.published_at.date().isoformat()}\n"
    return head + "\n正文：\n" + body[:MAX_BODY_CHARS]


RETRY_HINT = (
    "你的上一条回复不是合法的 JSON 数组，按现在的格式整篇文章的结果都会被丢弃。"
    "重新回答：只输出 JSON 数组本身，不要任何解释文字；本文没有可抽的事实就输出 []。"
)

# 单次抽取的 max_tokens 上限。全局值（1200）是给对话回答定的，用在抽取上不够：
# 一篇长文抽七八条事实，每条含中文 evidence，很容易超过 1200 token，表现是
# JSON 从中间被砍断——看着像「模型不听话」，实际是配额。477 篇里 6 篇如此。
EXTRACT_MAX_TOKENS = 4000


@dataclass(slots=True)
class Outcome:
    """一次抽取的结果。四个字段对应四种不同的判读。"""

    facts: list[dict] | None
    attempts: int
    content: str
    finish_reason: str


async def extract_one(client, document: Document) -> Outcome:
    """返回抽取结果（含失败的细节）。

    `facts is None` 与 `[]` 必须分开：`[]` 是「这篇没有可抽的事实」，`None` 是
    「模型说的话没法解析」。合并的后果是——一批文档抽不出东西时，你分不清
    是语料没料还是模型没干活，而这两种情况要采取的动作完全相反。

    `attempts` 量「格式不稳」有多严重；`content` 与 `finish_reason` 是**唯一**
    能解释「为什么解析不出来」的东西。只报「无法解析」等于把最需要的信息丢掉：
    被 max_tokens 砍断、上游返回空串、模型写了前言，三种情况的修法完全不同。
    """

    messages = [
        ChatMessage(role="system", content=build_prompt(PREDICATES)),
        ChatMessage(role="user", content=document_text(document)),
    ]
    last = ""
    finish_reason = ""
    for attempt in range(1, 3):
        result = await client.complete(
            messages, temperature=0.0, max_tokens=EXTRACT_MAX_TOKENS
        )
        last = result.content or ""
        finish_reason = result.finish_reason or ""
        facts = try_parse_facts_json(last)
        if facts is not None:
            return Outcome(facts, attempt, last, finish_reason)
        # 被截断时不要重试：同样的 max_tokens 只会再砍一次，白花一次调用。
        if classify_unparsed(finish_reason, last) == "truncated":
            return Outcome(None, attempt, last, finish_reason)
        # 重试时把模型自己的错误输出放回上下文，再明确要求一次。
        # 比原样重发有效得多：温度已经是 0，重发大概率得到同一段废话。
        messages = [
            *messages,
            ChatMessage(role="assistant", content=last[:2000]),
            ChatMessage(role="user", content=RETRY_HINT),
        ]
    return Outcome(None, 2, last, finish_reason)


def resolve_date_source(fact: ExtractedFact, document: Document) -> tuple[date | None, str]:
    """返回 (落库用的日期, 来源档位)。

    模型没给日期时用发布时间兜底，但档位必须是 `published`——时间线上要能看出
    「这个日期是推的」。两者都没有就只能留空，进 `undated`。
    """

    if fact.valid_from is not None:
        return fact.valid_from, "stated"
    if document.published_at is not None:
        return document.published_at.date(), "published"
    return None, "unknown"


async def main() -> int:
    parser = argparse.ArgumentParser(description="从文档抽取结构化事实")
    parser.add_argument("--limit", type=int, default=10, help="最多处理多少篇文档")
    parser.add_argument("--since", default="", help="只处理该日期之后发布的文档（YYYY-MM-DD）")
    parser.add_argument("--keyword", default="", help="标题或摘要包含该关键词")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="只打印抽取结果，不写库")
    group.add_argument("--apply", action="store_true", help="写库")
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="连已判定过的文档一起重抽（只在改了提示词或换了模型时用；默认跳过）",
    )
    args = parser.parse_args()

    try:
        client = require_llm()
    except LLMUnavailable as exc:
        print(f"LLM 不可用：{exc}", file=sys.stderr)
        print("先在 .env 里配好 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL", file=sys.stderr)
        return 2

    stats = Stats()
    async with session_scope() as session:
        documents = await pick_documents(session, args)
        scope = "含已判定过的（--reprocess）" if args.reprocess else "跳过已判定过的"
        print(f"待处理文档 {len(documents)} 篇（limit={args.limit}，{scope}）")

        for index, document in enumerate(documents, start=1):
            stats.documents += 1
            try:
                outcome = await extract_one(client, document)
            except Exception as exc:  # noqa: BLE001
                # 单篇失败不能打断整批：抽取是长任务，一篇超时丢掉 49 篇的成果，
                # 重跑要再花一次钱。
                print(f"[{index}] {document.title[:40]} 抽取失败：{type(exc).__name__}: {exc}")
                continue

            if outcome.attempts > 1:
                stats.retried += 1
                if outcome.facts is not None:
                    stats.recovered += 1

            if outcome.facts is None:
                stats.unparsed += 1
                kind = classify_unparsed(outcome.finish_reason, outcome.content)
                if kind == "truncated":
                    stats.unparsed_truncated += 1
                    why = f"输出被 max_tokens 截断（finish_reason=length，上限 {EXTRACT_MAX_TOKENS}）"
                elif kind == "empty":
                    stats.unparsed_empty += 1
                    why = f"上游返回空字符串（finish_reason={outcome.finish_reason or '空'}）"
                else:
                    stats.unparsed_format += 1
                    why = "输出不是合法的 JSON 数组"
                print(f"[{index}] {document.title[:40]} 抽取失败：{why}")
                if kind != "truncated":
                    preview = re.sub(r"\s+", " ", outcome.content).strip()[:120]
                    print(f"      原始输出：{preview or '（空字符串）'}")
                continue
            # 模型已经给出确定回答，这篇不必再送一次。放在「无事实」分支之前是有意的：
            # 那 342 篇无事实文档没有任何事实行可查，这个时间戳是它们唯一的痕迹。
            # 上面失败（facts is None）的分支刻意不回填——那篇还没被判定过，下轮必须重试，
            # 回填等于把一篇被截断的文档永久跳过。
            if args.apply:
                document.facts_extracted_at = func.now()
                stats.marked += 1

            if not outcome.facts:
                stats.empty_documents += 1
                print(f"[{index}] {document.title[:40]} 本文无可抽事实")
                continue
            raw_facts = outcome.facts

            # 证据核对用完整原文，不用发给模型的那 3000 字截断版：
            # 截断之外的部分如果被模型引用了，用截断版校验会误判成「编造」。
            source_full = f"{document.title}\n{document.content_text or document.summary or ''}"

            for raw in raw_facts:
                fact, reason = parse_raw_fact(raw)
                if fact is None:
                    if reason == "predicate":
                        stats.rejected_predicate += 1
                    elif reason == "reflexive":
                        stats.rejected_reflexive += 1
                    elif reason == "too_long":
                        stats.rejected_too_long += 1
                    else:
                        stats.malformed += 1
                    continue

                stats.facts += 1
                valid_from, date_source = resolve_date_source(fact, document)
                if date_source != "stated":
                    stats.undated += 1

                match = evidence_match(fact.evidence, source_full)
                if match == "exact":
                    stats.evidence_exact += 1
                    mark = ""
                elif match == "partial":
                    stats.evidence_partial += 1
                    mark = "  ⚠ 证据仅部分匹配"
                else:
                    stats.evidence_missing += 1
                    mark = "  ⚠ 证据在原文中找不到"

                print(
                    f"[{index}] {fact.subject} --{fact.predicate}--> {fact.object_text}"
                    f"  ({valid_from or '无日期'}/{date_source}){mark}"
                )
                if match != "exact":
                    # 只标「找不到」等于把最需要的信息丢掉。模型可能是没给证据、
                    # 把英文原文意译成中文、也可能是凭空编了一句——三种情况的修法
                    # 完全不同，所以证据本身就打出来，别让人再猜。
                    shown = fact.evidence or "（模型没有提供 evidence 字段）"
                    print(f"      证据：{shown[:120]}")
                if not args.apply:
                    continue

                subject = await resolve_or_create_entity(session, fact.subject, fact.subject_kind)
                if subject is None:
                    stats.malformed += 1
                    continue

                stmt = pg_insert(FactClaim).values(
                    subject_id=subject.id,
                    predicate=fact.predicate,
                    object_text=fact.object_text,
                    valid_from=valid_from,
                    valid_from_source=date_source,
                    document_id=document.id,
                    evidence_text=fact.evidence,
                    confidence=fact.confidence,
                )
                stmt = stmt.on_conflict_do_nothing(constraint="uq_fact_claims_identity")
                outcome = await session.execute(stmt)
                if outcome.rowcount:
                    stats.written += 1
                else:
                    stats.duplicated += 1

            if args.apply and index % 10 == 0:
                await session.commit()
                print(f"  -- 已提交 {index} 篇 --")

        if args.apply:
            await session.commit()

    # asdict 而不是 vars：Stats 是 slots=True 的 dataclass，没有 __dict__，
    # vars() 会直接抛 TypeError——这个 bug 直到第一次真跑才暴露，因为它只在
    # 全部抽取跑完、打印统计那一行才触发。
    print(
        "\n完成：文档 {documents} 篇，抽出 {facts} 条，入库 {written} 条，"
        "重复 {duplicated} 条，谓词不合规 {rejected_predicate} 条，"
        "自反 {rejected_reflexive} 条，宾语过长 {rejected_too_long} 条，"
        "字段缺失 {malformed} 条，无事实 {empty_documents} 篇，"
        "重试 {retried} 篇（救回 {recovered} 篇），"
        "抽取失败 {unparsed} 篇（截断 {unparsed_truncated} / 空响应 {unparsed_empty} / "
        "格式 {unparsed_format}），"
        "非明说日期 {undated} 条，"
        "证据 exact/partial/missing = {evidence_exact}/{evidence_partial}/{evidence_missing}，"
        "已标记 {marked} 篇（下轮不再重抽）".format(
            **asdict(stats)
        )
    )
    if stats.unparsed:
        print(
            f"有 {stats.unparsed} 篇抽取失败，原因分三类，处置办法不同："
            f"截断 {stats.unparsed_truncated} 篇（改 EXTRACT_MAX_TOKENS 或缩短输入）、"
            f"空响应 {stats.unparsed_empty} 篇（上游故障或被拦，换 provider 重试）、"
            f"格式 {stats.unparsed_format} 篇（prompt 或换模型）"
        )
    if stats.rejected_predicate:
        print("谓词不合规说明模型在造词：先看是不是词表少了常用动作，别急着放宽校验")
    if stats.rejected_reflexive or stats.rejected_too_long:
        print(
            "自反/宾语过长被丢弃：说明 prompt 的规则 3、4 没被遵守。"
            "比例高就换更强的模型，而不是把校验放宽——放进去的每条都会污染时间线"
        )
    if stats.evidence_missing:
        print(
            f"有 {stats.evidence_missing} 条证据在原文里找不到：模型在编证据。"
            "这批不要 --apply，先换模型或缩短单篇输入再试"
        )
    if stats.undated:
        # dry-run 时什么都没写，别用「已入库」把自己骗过去
        verb = "已按 published 档落库" if args.apply else "会按 published 档落库"
        print(f"非明说日期{verb}，时间线上会显示为「以发文时间兜底」")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
