"""阶段 7：实体时间线查询。

这里只有四个函数，但每一个都在回答检索回答不了的问题：

- `entity_timeline`：这家公司在某段时间里发生过什么
- `as_of`：「在 2024 年 3 月，这条事实成立吗」——双时间表唯一能给出的查询
- `find_overlaps`：时间线上有没有两件互相矛盾的事同时成立
- `normalize_key`：把「OpenAI」「Open AI」收敛成同一个键

`as_of` 是整章的重点。单时间表（只有「当前状态」）根本问不出这个问题，
因为它把历史状态覆盖掉了。

**一个必须如实返回的盲区**：没有 `valid_from` 的事实无法参与「某时点是否成立」
的判断，只能单独列出来。假装它成立或者假装它不成立，都是在编造。
`TimelineResult.undated` 就是为此存在的——宁可说「有 6 条事实时间未知，未纳入判断」，
也不能让调用方以为时间线是完整的。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, Entity, FactClaim

# 受控谓词表。**谓词不能由模型自由生成**，否则同一个动作会散成十种写法
# （released / launched / unveiled / 推出 / 发布），按谓词聚合的时间线就拼不起来。
# 加新谓词要同时改这里和抽取 prompt，两边不一致时抽取脚本会在落库前报错。
#
# 约定：**主语是公司或产品，不是人**。「OpenAI 的 CEO 是 Sam Altman」记成
# subject=OpenAI, predicate=ceo, object=Sam Altman。这样查一家公司能一次拿到
# 历任 CEO，而且两个 CEO 的任职区间同时成立时能被 `find_overlaps` 抓出来。
PREDICATES: dict[str, str] = {
    "released": "发布产品或模型",
    "released_version": "发布具体版本号",
    "announced": "宣布但未交付",
    "discontinued": "下线或停服",
    "acquired": "收购（主语是收购方）",
    "partnered_with": "建立合作",
    "ceo": "CEO 由谁担任",
    "founded": "创立",
    "funded": "获得融资",
    "layoff": "裁员",
    "open_sourced": "开源",
    "benchmark": "基准测试成绩",
}

KINDS = ("company", "product", "person", "model", "org")

# 日期可信度的展示文案。这三档不是修饰，是「这个日期能不能当依据」的判定
DATE_SOURCE_LABELS: dict[str, str] = {
    "stated": "文中明说",
    "published": "以发文时间兜底",
    "unknown": "时间未知",
}


def normalize_key(name: str) -> str:
    """实体比对键：小写、只留字母数字与汉字。

    空格、连字符、中英文括号全部丢弃，所以 `GPT-4o` / `gpt 4o` / `GPT4o`
    都会落成 `gpt4o`。这一步不做，时间线会被同一个实体的不同写法切成几段，
    而人看到的「这家公司做过什么」永远缺一块。
    """

    return "".join(ch for ch in name.lower() if ch.isalnum())


@dataclass(slots=True)
class TimelineEntry:
    fact_id: uuid.UUID
    predicate: str
    predicate_label: str
    object_text: str
    valid_from: date | None
    valid_to: date | None
    date_source: str
    confidence: float | None
    evidence_text: str | None
    document_id: uuid.UUID
    document_title: str
    document_url: str
    published_at: datetime | None


@dataclass(slots=True)
class TimelineResult:
    entity_id: uuid.UUID
    entity_name: str
    entity_kind: str
    entries: list[TimelineEntry] = field(default_factory=list)
    # 没有日期的条目：无法排序，也判不了「某时点是否成立」，单独放
    undated: list[TimelineEntry] = field(default_factory=list)
    # 同一谓词下区间相交的事实（两个 CEO 同时在职之类），是数据冲突信号
    overlaps: list[str] = field(default_factory=list)

    @property
    def dated_count(self) -> int:
        return len(self.entries)

    @property
    def undated_count(self) -> int:
        return len(self.undated)


async def resolve_entity(
    session: AsyncSession, name: str, kind: str | None = None
) -> Entity | None:
    """按名字找实体。先走 canonical_key 精确匹配（有索引），再全表兜别名。

    全表兜底看着低效，但实体总量在千级，一次扫描是可接受的代价；反过来，
    为了省这一次扫描而漏掉别名，代价是「查 OpenAI 查不到 OpenAI Inc.」——
    用户会认为整个功能坏了。
    """

    key = normalize_key(name)
    if not key:
        return None

    stmt = select(Entity).where(Entity.canonical_key == key)
    if kind:
        stmt = stmt.where(Entity.kind == kind)
    found = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if found is not None:
        return found

    rows = (await session.execute(select(Entity))).scalars().all()
    for entity in rows:
        if normalize_key(entity.name) == key:
            return entity
        if any(normalize_key(alias) == key for alias in (entity.aliases or [])):
            return entity
    return None


async def entity_timeline(
    session: AsyncSession,
    name: str,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
) -> TimelineResult | None:
    """某实体的时间线。有日期的按时间倒序，无日期的单独归入 `undated`。"""

    entity = await resolve_entity(session, name)
    if entity is None:
        return None

    stmt = (
        select(FactClaim, Document)
        .join(Document, Document.id == FactClaim.document_id)
        .where(FactClaim.subject_id == entity.id)
        .order_by(FactClaim.valid_from.desc().nullslast(), FactClaim.recorded_at.desc())
        .limit(limit)
    )
    if start is not None:
        # 起点过滤要放行 NULL：时间未知的事实不应该被静默丢弃，它属于 undated
        stmt = stmt.where(or_(FactClaim.valid_from.is_(None), FactClaim.valid_from >= start))
    if end is not None:
        stmt = stmt.where(or_(FactClaim.valid_from.is_(None), FactClaim.valid_from <= end))

    rows = (await session.execute(stmt)).all()

    result = TimelineResult(
        entity_id=entity.id, entity_name=entity.name, entity_kind=entity.kind
    )
    for fact, document in rows:
        entry = _to_entry(fact, document)
        if fact.valid_from is None:
            result.undated.append(entry)
        else:
            result.entries.append(entry)

    result.overlaps = find_overlaps(result.entries)
    return result


async def as_of(
    session: AsyncSession, name: str, on: date, *, limit: int = 200
) -> TimelineResult | None:
    """某实体的「当日快照」：只返回在 `on` 这一天成立的事实。

    判定用的是**现实时间**：`valid_from <= on < valid_to`（`valid_to` 为 NULL 表示至今）。
    注意这是半开区间——`valid_to` 当天已经不再成立。选半开而不是闭区间，是因为
    同一个职位前后两任的交接日只有一天，闭区间会让两任在那天同时在职，
    制造一条假冲突。
    """

    entity = await resolve_entity(session, name)
    if entity is None:
        return None

    stmt = (
        select(FactClaim, Document)
        .join(Document, Document.id == FactClaim.document_id)
        .where(
            FactClaim.subject_id == entity.id,
            FactClaim.valid_from.is_not(None),
            FactClaim.valid_from <= on,
            or_(FactClaim.valid_to.is_(None), FactClaim.valid_to > on),
        )
        .order_by(FactClaim.valid_from.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    result = TimelineResult(
        entity_id=entity.id, entity_name=entity.name, entity_kind=entity.kind
    )
    for fact, document in rows:
        result.entries.append(_to_entry(fact, document))

    # 无日期的事实无法判断「当时是否成立」，单独列出而不是丢掉。
    # 这一句决定了 as_of 是「有据的回答」还是「编造的回答」。
    undated_stmt = (
        select(FactClaim, Document)
        .join(Document, Document.id == FactClaim.document_id)
        .where(FactClaim.subject_id == entity.id, FactClaim.valid_from.is_(None))
        .limit(limit)
    )
    for fact, document in (await session.execute(undated_stmt)).all():
        result.undated.append(_to_entry(fact, document))

    result.overlaps = find_overlaps(result.entries)
    return result


def find_overlaps(entries: list[TimelineEntry]) -> list[str]:
    """同一谓词下两个不同宾语的区间相交 = 数据冲突。

    典型形态是两个 CEO 的任职区间重叠，或两个版本号被说成「当前最新」。
    这不一定是谁错了——可能是报道口径不同（「代理 CEO」和「正式 CEO」），
    但它是**必须暴露的信号**：时间线上出现两条并列矛盾，
    比缺一条更危险，因为看起来都像真的。
    """

    problems: list[str] = []
    by_predicate: dict[str, list[TimelineEntry]] = {}
    for entry in entries:
        by_predicate.setdefault(entry.predicate, []).append(entry)

    for predicate, group in by_predicate.items():
        if len(group) < 2:
            continue
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                if left.object_text == right.object_text:
                    # 同宾语的重复报道不算冲突，那是同一件事的多源报道
                    continue
                if _intersects(left, right):
                    problems.append(
                        f"{predicate}：'{left.object_text}'（{_span(left)}）"
                        f" 与 '{right.object_text}'（{_span(right)}）区间重叠"
                    )
    return problems


def _intersects(left: TimelineEntry, right: TimelineEntry) -> bool:
    left_end = left.valid_to or date.max
    right_end = right.valid_to or date.max
    left_start = left.valid_from or date.min
    right_start = right.valid_from or date.min
    return left_start < right_end and right_start < left_end


def _span(entry: TimelineEntry) -> str:
    start = entry.valid_from.isoformat() if entry.valid_from else "?"
    end = entry.valid_to.isoformat() if entry.valid_to else "至今"
    return f"{start} → {end}"


def _to_entry(fact: FactClaim, document: Document) -> TimelineEntry:
    return TimelineEntry(
        fact_id=fact.id,
        predicate=fact.predicate,
        predicate_label=PREDICATES.get(fact.predicate, fact.predicate),
        object_text=fact.object_text,
        valid_from=fact.valid_from,
        valid_to=fact.valid_to,
        date_source=fact.valid_from_source,
        confidence=fact.confidence,
        evidence_text=fact.evidence_text,
        document_id=document.id,
        document_title=document.title,
        document_url=document.canonical_url,
        published_at=document.published_at,
    )
