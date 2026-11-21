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

from sqlalchemy import func, or_, select
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

# 多值谓词：同一时刻可以同时成立多个宾语，彼此不是替代关系。
#
# **名单之外的谓词一律按「单值」处理。** 这个默认方向是刻意的：漏报的表现是
# 「时间线上两条矛盾并存却没人提示」，比多报危险得多，所以未知谓词宁可误报。
#
# 这个名单是合并 NVIDIA / 英伟达 之后才补的。合并前两组事实各自算冲突，
# 合并后 `partnered_with` 的 6 条合作方进了同一次两两比较，一次产出 15 条
# 「冲突」——台积电、富士康、安靠、矽品、景硕、三星电机当然可以同时都是伙伴。
# 而这 18 条里真正的冲突只有 3 条，且渲染层只显示前 5 条，于是 15 条噪音
# **把 3 条真信号全部挤出了视野**。多报的代价不是「多看几行」，是真信号被埋。
MULTI_VALUED_PREDICATES: frozenset[str] = frozenset(
    {
        "released",  # 可以发布多个产品
        # 一个项目同时有多个在维护的版本线：nginx 1.30.7（stable）与 1.31.9（mainline）
        # 并行发布，两条都成立。同一模型的多种量化权重（BF16 / FP8 / NVFP4）、
        # 同一系列的多个变体（Astra / Sol）同理——它们不是前后任，是同时存在。
        "released_version",
        "announced",  # 可以宣布多件事
        "discontinued",  # 可以下线多个功能或产品线
        "acquired",  # 可以收购多家公司
        "partnered_with",  # 合作关系天然多值
        "funded",  # 可以多轮融资
        "layoff",  # 可以多次裁员
        "open_sourced",  # 可以开源多个仓库
        "benchmark",  # 多个基准成绩并存
    }
)

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
    # 被撤回的条数（只报数量，不列内容）。撤回的条目一律不出现在 entries /
    # undated / overlaps 里，但**数量必须露出来**：否则「我们撤了一条」和
    # 「这条从来没被记录过」在调用方看来完全一样，而前者恰恰需要交代。
    retracted_count: int = 0
    # 这次查询实际覆盖了哪些写法。`NVIDIA` 和 `英伟达` 是两行实体，合并之后
    # 一次查询拿到两行的事实——**必须把这件事说出来**，否则「这家公司只有 3 条
    # 事实」和「它其实有 9 条，只是另外 6 条挂在另一种写法下」看起来一样。
    merged_names: list[str] = field(default_factory=list)

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


async def _count_retracted(
    session: AsyncSession, entity_ids: list[uuid.UUID]
) -> int:
    """只数撤回条数，不取内容。

    查询层把撤回项全部过滤掉了，于是「我们撤了一条」和「从来没记过」在结果里
    长得一模一样。必须把数量说出来，否则 as_of 会安静地少掉一条本该交代的信息——
    而「安静地少一条」正是这个项目最忌讳的错误。

    按整组 id 计数而不是按根 id：合并之后，撤回在子写法名下的一条记录同样是
    「我们撤过」，只数根会把这句话漏掉。
    """

    total = await session.scalar(
        select(func.count())
        .select_from(FactClaim)
        .where(
            FactClaim.subject_id.in_(entity_ids),
            FactClaim.retracted_at.is_not(None),
        )
    )
    return int(total or 0)


# 合并链的深度上限。自指由 CHECK 拦住了，多跳环路（A→B→A）只能在这里兜：
# 超过深度就停在当前行，把它当根——不抛异常，因为一个坏指针不该让整条时间线
# 变成 500；但也不无限跟，那才是真正的危险。
MAX_MERGE_DEPTH = 8


@dataclass(slots=True)
class EntityGroup:
    """一次查询要覆盖的一组实体：一个根，加上被合并到它名下的所有写法。"""

    root: Entity
    members: list[Entity]

    @property
    def ids(self) -> list[uuid.UUID]:
        return [member.id for member in self.members]

    @property
    def merged_names(self) -> list[str]:
        """除根以外的写法。用来在结果里交代「这个数字覆盖了哪些名字」。"""
        return [member.name for member in self.members if member.id != self.root.id]


def _root_id(start: uuid.UUID, parent: dict[uuid.UUID, uuid.UUID]) -> uuid.UUID:
    """沿 `merged_into` 向上找根。环路与超深都停在当前行，不抛。"""

    seen = {start}
    current = start
    for _ in range(MAX_MERGE_DEPTH):
        nxt = parent.get(current)
        if nxt is None or nxt in seen:
            return current
        seen.add(nxt)
        current = nxt
    return current


def build_entity_group(entity: Entity, rows: list[Entity]) -> EntityGroup:
    """纯函数版本：给定起点与全部实体行，算出它所在的合并组。

    单独拆出来是因为项目没有测试库。合并的难点全在**闭包怎么算**（链式、环路、
    跨写法），不在 SQL 上——把这段放成纯函数，就能不连库钉住它。
    """

    by_id = {row.id: row for row in rows}
    # 兜底：调用方给的 rows 里可能不含起点（例如只查了子行）
    by_id.setdefault(entity.id, entity)
    parent = {
        row.id: row.merged_into for row in by_id.values() if row.merged_into is not None
    }

    root_id = _root_id(entity.id, parent)
    root = by_id.get(root_id, entity)
    others = [
        row
        for row in by_id.values()
        if row.id != root_id and _root_id(row.id, parent) == root_id
    ]
    # 排序只为让结果稳定（SQL 里 IN 的顺序会影响执行计划，不影响语义）
    others.sort(key=lambda row: row.name)
    return EntityGroup(root=root, members=[root, *others])


async def resolve_entity_group(
    session: AsyncSession, name: str, kind: str | None = None
) -> EntityGroup | None:
    """按名字找到**一组**实体：它自己 + 所有被合并到同一根下的写法。

    为什么要多扫一次全表：合并信息只存在于 `merged_into` 这一列上，而它可能挂在
    组里任意一行（`OpenAI Inc` 指向 `OpenAI`，`NVIDIA` 指向 `英伟达`），从起点出发
    单点查不出全貌。实体在千级，`resolve_entity` 本来就有一次全表兜底，这里沿用
    同一个取舍——代价是常数，收益是「查一个名字不会只拿到一半事实」。
    """

    entity = await resolve_entity(session, name, kind)
    if entity is None:
        return None
    rows = list((await session.execute(select(Entity))).scalars().all())
    return build_entity_group(entity, rows)


async def entity_timeline(
    session: AsyncSession,
    name: str,
    *,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
) -> TimelineResult | None:
    """某实体的时间线。有日期的按时间倒序，无日期的单独归入 `undated`。

    被撤回的事实（`retracted_at` 非空）一律不返回：撤回的含义是「我们不再声称」，
    它不该继续参与时间线排序，也不该进 `find_overlaps` 制造假冲突。

    查询按**合并组**而不是单个 id 走：`NVIDIA` 与 `英伟达` 在 `normalize_key` 下
    必然分成两行，两行各有各的事实。只查一行会得到一份看起来完整、实际缺一半的
    时间线——这种「安静地缺一半」比直接报错更糟。
    """

    group = await resolve_entity_group(session, name)
    if group is None:
        return None
    entity = group.root

    stmt = (
        select(FactClaim, Document)
        .join(Document, Document.id == FactClaim.document_id)
        .where(
            FactClaim.subject_id.in_(group.ids),
            FactClaim.retracted_at.is_(None),
        )
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
        entity_id=entity.id,
        entity_name=entity.name,
        entity_kind=entity.kind,
        merged_names=group.merged_names,
    )
    for fact, document in rows:
        entry = _to_entry(fact, document)
        if fact.valid_from is None:
            result.undated.append(entry)
        else:
            result.entries.append(entry)

    result.retracted_count = await _count_retracted(session, group.ids)
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

    被撤回的事实不参与判定（`retracted_at` 非空）。这不是「删掉它」，而是
    「不再拿它当回答依据」——两者在 `as_of` 上的区别非常具体：撤回一条错的
    CEO 记录之后，那天的答案是「没有记录」，而不是「那时不是他」。

    同样按合并组查询。这里合并的收益比 `entity_timeline` 更直接：
    `NVIDIA` 名下记着「Colette Kress 任 CEO」，`英伟达` 名下记着「黄仁勋任 CEO」，
    不合并的话两边各自都「没有冲突」，`as_of` 也不会报重叠——**冲突被写法切开了**。
    """

    group = await resolve_entity_group(session, name)
    if group is None:
        return None
    entity = group.root
    group_ids = group.ids

    stmt = (
        select(FactClaim, Document)
        .join(Document, Document.id == FactClaim.document_id)
        .where(
            FactClaim.subject_id.in_(group_ids),
            FactClaim.retracted_at.is_(None),
            FactClaim.valid_from.is_not(None),
            FactClaim.valid_from <= on,
            or_(FactClaim.valid_to.is_(None), FactClaim.valid_to > on),
        )
        .order_by(FactClaim.valid_from.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    result = TimelineResult(
        entity_id=entity.id,
        entity_name=entity.name,
        entity_kind=entity.kind,
        merged_names=group.merged_names,
    )
    for fact, document in rows:
        result.entries.append(_to_entry(fact, document))

    # 无日期的事实无法判断「当时是否成立」，单独列出而不是丢掉。
    # 这一句决定了 as_of 是「有据的回答」还是「编造的回答」。
    undated_stmt = (
        select(FactClaim, Document)
        .join(Document, Document.id == FactClaim.document_id)
        .where(
            FactClaim.subject_id.in_(group_ids),
            FactClaim.retracted_at.is_(None),
            FactClaim.valid_from.is_(None),
        )
        .limit(limit)
    )
    for fact, document in (await session.execute(undated_stmt)).all():
        result.undated.append(_to_entry(fact, document))

    result.retracted_count = await _count_retracted(session, group_ids)
    result.overlaps = find_overlaps(result.entries)
    return result


def find_overlaps(entries: list[TimelineEntry]) -> list[str]:
    """同一谓词下两个不同宾语的区间相交 = 数据冲突。

    典型形态是两个 CEO 的任职区间重叠，或两个版本号被说成「当前最新」。
    这不一定是谁错了——可能是报道口径不同（「代理 CEO」和「正式 CEO」），
    但它是**必须暴露的信号**：时间线上出现两条并列矛盾，
    比缺一条更危险，因为看起来都像真的。

    只对**单值谓词**判定：`partnered_with` 有六个合作方不是冲突，一家公司
    当然可以同时和台积电、富士康合作。名单见 `MULTI_VALUED_PREDICATES`。
    """

    problems: list[str] = []
    by_predicate: dict[str, list[TimelineEntry]] = {}
    for entry in entries:
        by_predicate.setdefault(entry.predicate, []).append(entry)

    for predicate, group in by_predicate.items():
        if len(group) < 2:
            continue
        if predicate in MULTI_VALUED_PREDICATES:
            # 多值谓词的空隙里也会藏着真问题（同一件事的中英文两条重复报道），
            # 但那需要判「宾语是不是同义」——语义问题，规则判不了，留给人工队列。
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
