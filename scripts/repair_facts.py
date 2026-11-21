"""阶段 8：事实审计、撤回与实体合并。

    .venv/bin/python scripts/repair_facts.py audit                       # 只读，人看的表格
    .venv/bin/python scripts/repair_facts.py audit --format csv --out .tmp/audit.csv
    .venv/bin/python scripts/repair_facts.py retract --id <uuid> --reason "…（附原文）"
    .venv/bin/python scripts/repair_facts.py retract --id <uuid> --undo   # 恢复采信
    .venv/bin/python scripts/repair_facts.py merge --id <uuid> --into <uuid>       # 合并实体
    .venv/bin/python scripts/repair_facts.py merge --id <uuid> --undo             # 解除合并

**`audit` 为什么只读。** 判定与执行必须分开，否则就成了「让模型给自己判卷」——
判定结果一旦能直接落库，就没有第二双眼睛看过它。中间那一步是人。

**`retract` 为什么是撤回不是删除。** 见 `docs/阶段8-数据修复设计.md` 第 3 节：
行没了，证据链跟着没了，时间线少一条的表现是「这件事没发生」而不是「这里有个洞」。
所以只填 `retracted_at` 三列，恢复就是把它们置回 NULL——这条可逆性由 `--undo` 直接兑现。
撤回必须附理由（数据库有 CHECK），理由里最好带原文：没有依据的撤回本身
就是一次不可审计的判断，和当初模型编造事实是同一种病。

**`merge` 为什么写指针而不改 `subject_id`。** 触发器直接拒改 `subject_id`；而且改完
就再也查不到「这条事实当初挂在哪种写法下」。指针可逆，事实一行不动，`--undo` 就还原。
`entities.aliases` 那一列就是反例：它存在、也被读，但**没人往里写**，于是等于没有。

三档判定见设计稿第 5 节。核心事实是：270 条里能自动判定的只有 3 条，刚过 1%。所以
`audit` 的产出不是「一份待删清单」，而是**一份按可疑程度排好序的人工队列**——
它的价值在于把 270 条缩到十几条，不在于替你决定。

三档的区别不在难度，在判据来源：

- ① 形状：宾语是 `Apache-2.0`，任何谓词下它都不该出现在宾语位。这不是判断题，是格式检查。
- ② 宾语形状可疑：`founded` 的宾语填了人名——不用看原文就能起疑，但也不能自动撤。
- ③ 证据指证谓词：`英伟达 --ceo--> Colette Kress`，宾语完全正常，只有把 `predicate`
  与 `evidence_text` 摆在一起才看得出她是 CFO。**模型自己的举证材料反过来指证它。**

**刻意不判的两类**：泛称宾语（`--benchmark--> leaderboard`）读起来别扭但不改变事实判断，
数量还大，正确处理是在抽取期用 `object_type` 挡掉，不为它回补历史；`benchmark` 谓词整条
不进任何档。

**② 的判据是启发式，会误报。** 比如中文四字的人名与四字产品名无法区分（`黄仁勋` 与
`通义千问` 都会命中）。这是可接受的：队列是只读的，误报的代价是人工多看一眼；
而漏报的代价是一条错事实留在时间线上，没人知道它错。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.db.models import Entity, FactClaim
from app.db.session import session_scope
from app.services.timeline_service import DATE_SOURCE_LABELS, MAX_MERGE_DEPTH, PREDICATES
from sqlalchemy import func, select

# ① 形状：许可证标识符。没有任何谓词把许可证声明为合法宾语——
# 许可证是「以什么条件开源」，是条件不是产物。
# 注意这里不豁免 open_sourced：豁免它，恰好会放过唯一真实的违规
# （`Fardesk --open_sourced--> Apache-2.0`），而那句证据里本来就写了许可证。
# 匹配许可证**开头**，不锚定结尾：宾语里常跟着一小段限定语
# （`AGPL-3.0 单二进制`、`Apache-2.0 许可证`），锚死了就会把它们全放过。
# 实例：`Windshift --open_sourced--> AGPL-3.0 单二进制` 在锚定的写法下逃过了 ① 档，
# 和已经撤回的 `Windshift --> AGPL-3.0` 是同一个错误，只因为多了三个字。
LICENSE_RE = re.compile(
    r"^(?:Apache|MIT|BSD|GPL|AGPL|LGPL|MPL|EPL|ISC|Unlicense|CC[- ]BY)"
    r"[- ]?v?\d?(?:\.\d+)?(?:[- ]only|[- ]or[- ]later)?",
    re.IGNORECASE,
)
# 许可证后面允许跟的限定语白名单。用白名单而不是「长度很短就算」：
# 一旦按长度放宽，`MIT License Manager` 这种产品名就会被误撤，
# 而 ① 档是唯一会自动执行的档位，误撤没有第二双眼睛拦得住。
LICENSE_TAIL_RE = re.compile(
    r"^(?:[ \t]*(?:license|licence|许可证|协议|授权|条款|版本|"
    r"单|二进制|binary|only|开源|代码|源码|组件|repo|repository))*$",
    re.IGNORECASE,
)

# ② 的形状线索。都是「看起来像什么」，不是「是什么」。
COMPANY_HINT_RE = re.compile(
    r"(Inc\b|Ltd\b|LLC\b|Corp\b|Corporation|GmbH|Co\.|公司|集团|实验室|研究院|大学|团队)",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
AMOUNT_OR_ROUND_RE = re.compile(
    r"(?:[$￥€¥]\s?\d"
    r"|[\d.]+\s*(?:million|billion|万|亿)"
    # 「近亿元融资」「约五千万」这类没有阿拉伯数字的写法：`亿`/`万` 前面可以只有量词
    r"|(?:近|约|超|逾|数)\s*[\d.]*\s*(?:万|亿)"
    r"|Series\s+[A-Z]|[A-H]轮|天使轮|种子轮|Pre-?IPO)",
    re.IGNORECASE,
)
# 融资的「形态」而不只是「金额」。`funded` 没有单独的金额字段，宾语槽位同时承载
# 「融了多少」和「以什么方式融」——IPO 和「B轮」在这一点上是一回事。
FINANCING_EVENT_RE = re.compile(
    r"(?:IPO|initial public offering|首次公开募股|募资|上市)",
    re.IGNORECASE,
)
# 贷款承诺函是债务工具，不是股权融资。分开给理由，是因为处置方式不同：
# 前者多半是谓词用错，后者是宾语填错了人。
DEBT_FACILITY_RE = re.compile(r"(?:贷款|授信|承诺函|额度)", re.IGNORECASE)

# ③ 证据里的反证词。命中不代表这条一定错，只代表「宾语和谓词对不上，去看一眼原文」。
_C_TITLE = (
    r"(?:CFO|CTO|COO|CMO|CPO)"
    r"|chief\s+(?:financial|technology|operating|marketing|product)\s+officer"
    r"|首席(?:财务|技术|运营|市场)官"
)
OTHER_TITLE_RE = re.compile(
    # 不用 \b：中文字符在 Python 正则里也算 \w，`原CFO Colette` 的 `\bCFO` 会匹配失败。
    rf"(?<![A-Za-z])(?:{_C_TITLE})(?![A-Za-z])",
    re.IGNORECASE,
)
# 「原CFO」「前 CTO」「former CFO」里的头衔本身是过去式——它出现在证据里恰好说明
# 此人现在**不是**那个职位，不能当反证。实跑教训：`这是原CFO Colette Kress接任临时CEO
# 以来` 被判成「把 CFO 记成 CEO」，而原文说的是她刚接任 CEO，方向正好相反。
# 这类假阳性比漏报更伤：它让整条 ③ 档规则失去可信度（0 命中，唯一输出是误报）。
PAST_TITLE_RE = re.compile(rf"(?:原|前|前任|former|ex[-\s])\s*(?:{_C_TITLE})", re.IGNORECASE)
ACQ_REVERSED_RE = re.compile(
    r"(?:acquired\s+by|was\s+acquired|is\s+acquired|被.{1,8}收购|收购方是)",
    re.IGNORECASE,
)

TIER_LABELS = {
    "1_shape": "① 形状不符（可自动撤回）",
    "2_object_shape": "② 宾语形状可疑（人工队列）",
    "3_evidence": "③ 证据与谓词冲突（人工队列）",
}


def is_shape_violation(object_text: str) -> str | None:
    """返回撤回原因，或 None。只判形状，不判语义。

    刻意不接 `predicate` 参数：一旦按谓词分支，它就变成判断题了，而 ① 档
    唯一的正当性就是「这个位置不能放这种东西」，与谓词无关。
    """
    text = object_text.strip()
    match = LICENSE_RE.match(text)
    if match and LICENSE_TAIL_RE.match(text[match.end() :]):
        return "object_is_license"
    return None


def looks_like_person(text: str) -> bool:
    """启发式，不是 NER。只用来把可疑项排到队列前面，不用于写库判定。

    会误报：中文 2～4 字的人名与产品名无法区分（`黄仁勋` / `通义千问` 都命中）。
    会漏报：`黄仁勋（Jensen Huang）` 这种带括号的写法。
    """
    cleaned = text.strip().strip(".,，。")
    if not cleaned or any(ch.isdigit() for ch in cleaned):
        return False
    if COMPANY_HINT_RE.search(cleaned):
        return False
    # OpenAI / IBM 这类全大写缩写是机构或产品，不是人名
    if re.fullmatch(r"[A-Z]{2,}", cleaned):
        return False
    words = cleaned.split()
    if len(words) in (2, 3) and all(word[:1].isupper() for word in words):
        return True
    return bool(re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", cleaned))


def object_shape_reason(predicate: str, object_text: str) -> str | None:
    """② 档：不看原文，只看宾语长什么形状就该起疑的情形。

    只覆盖三个谓词。不追求完备——完备等于给 12 个谓词各写一套类型系统，
    那是第 7 节 `object_type` 的活，不该在回补历史时重做一遍。
    """
    text = object_text.strip()

    if predicate == "founded":
        # 创立的宾语应该是年份或机构。填人名不是错在「人不能创立公司」，
        # 而是错在这个位置该记「何时/由谁做主语」，把它填到宾语上会让
        # 时间线变成「OpenAI 创立了 Sam Altman」。
        if YEAR_RE.match(text) or COMPANY_HINT_RE.search(text):
            return None
        return "founded_object_not_year_or_org"

    if predicate == "ceo":
        if looks_like_person(text):
            return None
        return "ceo_object_not_person"

    if predicate == "funded":
        if AMOUNT_OR_ROUND_RE.search(text) or FINANCING_EVENT_RE.search(text):
            return None
        if DEBT_FACILITY_RE.search(text):
            return "funded_object_is_debt_facility"
        # 剩下的多半是宾语填了投资方/被投方（`英伟达 --funded--> 矽品精密`），
        # 方向反了或者槽位错位。统一的理由就够：人工看一眼 evidence 就知道是哪种
        return "funded_object_is_not_amount"

    return None


def evidence_reason(predicate: str, object_text: str, evidence: str | None) -> str | None:
    """③ 档：宾语形状正常，错在「谓词说的和原文说的不是一回事」。

    这是整份审计里唯一能碰到语义的地方，也只能做到「提示去看原文」。
    """
    if not evidence:
        return None

    # 证据里出现了别的 C 级头衔——典型是 CFO 被记成 CEO。
    # 先剥掉「原/前/former」修饰过的头衔：那些是过去式，不能当反证。
    if predicate == "ceo" and OTHER_TITLE_RE.search(PAST_TITLE_RE.sub(" ", evidence)):
        return "evidence_mentions_other_c_level_title"

    # 收购方向写反：主语应当是被收购的那一方才对
    if predicate == "acquired" and ACQ_REVERSED_RE.search(evidence):
        return "acquisition_direction_maybe_reversed"

    return None


@dataclass(slots=True)
class AuditRow:
    tier: str
    fact_id: str
    subject: str
    predicate: str
    object_text: str
    valid_from: str
    date_source: str
    reason: str
    evidence: str


CSV_HEADER = [
    "tier",
    "fact_id",
    "subject",
    "predicate",
    "object_text",
    "valid_from",
    "date_source",
    "reason",
    "evidence",
]


def classify(
    subject: str,
    predicate: str,
    object_text: str,
    evidence: str | None,
) -> tuple[str, str] | None:
    """返回 (档位, 原因)，或 None 表示这条不进队列。

    主语与宾语相同的自反条目**不在这里判**：抽取阶段已经丢掉了，
    等到这里再判等于承认当初那道闸门没生效。
    """
    shape = is_shape_violation(object_text)
    if shape:
        return "1_shape", shape

    # benchmark 整条不进：宾语多是 leaderboard / 各种榜单泛称，
    # 读起来别扭但不改变「谁在什么时候做了什么」的判断
    if predicate == "benchmark":
        return None

    shape_reason = object_shape_reason(predicate, object_text)
    if shape_reason:
        return "2_object_shape", shape_reason
    ev = evidence_reason(predicate, object_text, evidence)
    if ev:
        return "3_evidence", ev

    return None


async def fetch_rows(session, limit: int | None) -> list[AuditRow]:
    stmt = (
        select(
            FactClaim.id,
            FactClaim.predicate,
            FactClaim.object_text,
            FactClaim.valid_from,
            FactClaim.valid_from_source,
            FactClaim.evidence_text,
            Entity.name.label("subject"),
        )
        .join(Entity, Entity.id == FactClaim.subject_id)
        # 已撤回的不再审计：它们已经从查询里消失，再审一次只会重复同一条结论
        .where(FactClaim.retracted_at.is_(None))
        .order_by(FactClaim.recorded_at, FactClaim.id)
    )
    if limit:
        stmt = stmt.limit(limit)

    rows: list[AuditRow] = []
    for row in (await session.execute(stmt)).all():
        verdict = classify(row.subject, row.predicate, row.object_text, row.evidence_text)
        if verdict is None:
            continue
        tier, reason = verdict
        rows.append(
            AuditRow(
                tier=tier,
                fact_id=str(row.id),
                subject=row.subject,
                predicate=row.predicate,
                object_text=row.object_text,
                valid_from=row.valid_from.isoformat() if row.valid_from else "",
                date_source=DATE_SOURCE_LABELS.get(row.valid_from_source, row.valid_from_source),
                reason=reason,
                evidence=(row.evidence_text or "").replace("\n", " ")[:160],
            )
        )
    # ① 在最前：它不需要人判断，先看一眼就能批量撤
    rows.sort(key=lambda r: (r.tier, r.subject, r.predicate))
    return rows


def render_table(rows: list[AuditRow]) -> str:
    if not rows:
        return "没有命中任何一档。这不等于数据干净——只是没有「形状」层面的问题。"

    lines = [
        f"{'档':<3}{'主语':<18}{'谓词':<14}{'宾语':<24}{'原因'}",
        "-" * 96,
    ]
    for row in rows:
        lines.append(
            f"{row.tier[:1]:<3}{row.subject[:16]:<18}{row.predicate[:12]:<14}"
            f"{row.object_text[:22]:<24}{row.reason}"
        )
    return "\n".join(lines)


def render_csv(rows: list[AuditRow]) -> str:
    buffer = []
    writer = csv.writer(_ListSink(buffer), lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow([getattr(row, name) for name in CSV_HEADER])
    return "".join(buffer)


class _ListSink:
    """`csv.writer` 只要求 write()，不必绕道 StringIO 再拼一次换行。"""

    def __init__(self, buffer: list[str]) -> None:
        self._buffer = buffer

    def write(self, text: str) -> int:
        self._buffer.append(text)
        return len(text)


def summarize(rows: list[AuditRow]) -> str:
    counts = {tier: sum(1 for r in rows if r.tier == tier) for tier in TIER_LABELS}
    unknown_predicates = sorted({r.predicate for r in rows if r.predicate not in PREDICATES})
    parts = [f"{TIER_LABELS[tier]}：{count}" for tier, count in counts.items()]
    text = "命中 " + " / ".join(parts)
    if unknown_predicates:
        parts_unknown = ", ".join(unknown_predicates)
        text += f"\n⚠ 有谓词不在词表里：{parts_unknown}（词表与库不一致，先查抽取版本）"
    return text


async def run_audit(args) -> int:
    async with session_scope() as session:
        rows = await fetch_rows(session, args.limit or None)

    body = render_table(rows) if args.format == "table" else render_csv(rows)

    if args.out:
        # utf-8-sig：Excel 直接双击打开时不会把中文显示成乱码
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(body, encoding="utf-8-sig")
        print(f"已写出 {len(rows)} 行到 {args.out}")
    else:
        print(body)

    # 统计走 stderr：CSV 模式要保证 stdout 是干净的表格数据，能直接重定向
    print(summarize(rows), file=sys.stderr)
    print(
        "audit 不写库。① 档也只报告——撤回要单独执行 retract，且必须附理由；"
        "②③ 档请对着 evidence 过一遍原文再决定。",
        file=sys.stderr,
    )
    return 0


@dataclass(slots=True)
class RetractPlan:
    """`retract` 的参数在碰数据库之前就该验完。

    单独拆出来是为了它能被测试：真正的写库路径要连数据库，而「没给理由就拒」
    恰恰是最不能靠手工试的一条——它是这道防线的全部。
    """

    ids: list[uuid.UUID]
    replaced_by: uuid.UUID | None
    reason: str
    error: str = ""


def parse_retract_args(args) -> RetractPlan:
    reason = (args.reason or "").strip()
    if not args.undo and not reason:
        return RetractPlan(
            ids=[],
            replaced_by=None,
            reason="",
            error=(
                "撤回必须给理由。--reason 写清楚依据，最好把原文那句贴进来——"
                "没有理由的撤回是一次不可审计的判断，和当初模型编造事实是同一种病。"
            ),
        )

    try:
        ids = [uuid.UUID(item) for item in args.ids]
    except ValueError as exc:
        return RetractPlan(ids=[], replaced_by=None, reason=reason, error=f"id 不是合法 UUID：{exc}")

    replaced_by = None
    if args.replaced_by:
        try:
            replaced_by = uuid.UUID(args.replaced_by)
        except ValueError as exc:
            return RetractPlan(
                ids=ids,
                replaced_by=None,
                reason=reason,
                error=f"--replaced-by 不是合法 UUID：{exc}",
            )

    return RetractPlan(ids=ids, replaced_by=replaced_by, reason=reason)


async def run_retract(args) -> int:
    """撤回，不删除。

    **不删除的理由在判错，不在洁癖**：行没了，证据链跟着没了，时间线少一条的表现是
    「这件事没发生」，而不是「这里有个洞」。判错的概率乘以批量，就是几十条真实信息
    被抹掉且无人知道少了什么。所以这里只填 `retracted_at` 三列，恢复就是把它们置回 NULL。
    """
    plan = parse_retract_args(args)
    if plan.error:
        print(plan.error, file=sys.stderr)
        return 2

    ids = plan.ids
    replaced_by = plan.replaced_by
    reason = plan.reason

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(FactClaim, Entity.name)
                .join(Entity, Entity.id == FactClaim.subject_id)
                .where(FactClaim.id.in_(ids))
            )
        ).all()
        found = {fact.id: (fact, subject) for fact, subject in rows}

        if replaced_by is not None and await session.get(FactClaim, replaced_by) is None:
            print(f"⚠ --replaced-by 指向的事实不存在：{replaced_by}", file=sys.stderr)
            return 2

        changed = 0
        for index, item in enumerate(ids, start=1):
            if item not in found:
                print(f"⚠ 库里没有这条 id：{item}", file=sys.stderr)
                continue
            fact, subject = found[item]
            print(f"[{index}] {subject} --{fact.predicate}--> {fact.object_text}")
            print(f"     id={fact.id}")
            print(f"     证据：{(fact.evidence_text or '（模型没有提供 evidence）')[:120]}")

            if args.undo:
                if fact.retracted_at is None:
                    print("     当前是采信状态，跳过")
                    continue
                if not args.dry_run:
                    fact.retracted_at = None
                    fact.retracted_reason = None
                    fact.retracted_replaced_by = None
                changed += 1
                print("     → 已恢复采信" + ("（dry-run，未写库）" if args.dry_run else ""))
                continue

            if fact.retracted_at is not None:
                print(f"     已撤回（{fact.retracted_reason}），跳过")
                continue

            if not args.dry_run:
                fact.retracted_at = func.now()
                fact.retracted_reason = reason
                if replaced_by is not None:
                    fact.retracted_replaced_by = replaced_by
            changed += 1
            print("     → 已撤回" + ("（dry-run，未写库）" if args.dry_run else ""))

    verb = "恢复" if args.undo else "撤回"
    print(f"{verb} {changed} 条。", file=sys.stderr)
    if not args.undo and not args.dry_run and changed:
        print(
            "验证方式：撤回一条 CEO 后，`as_of(那天)` 的回答不能变——"
            "变了对，说明 valid_to 与 retracted_at 被混用了。",
            file=sys.stderr,
        )
    return 0


@dataclass(slots=True)
class MergePlan:
    """`merge` 的参数校验，同样在碰数据库之前做完，理由与 `RetractPlan` 一样。"""

    dup: uuid.UUID | None
    root: uuid.UUID | None
    error: str = ""


def parse_merge_args(args) -> MergePlan:
    try:
        dup = uuid.UUID(args.id)
        root = uuid.UUID(args.into)
    except ValueError as exc:
        return MergePlan(dup=None, root=None, error=f"id 不是合法 UUID：{exc}")

    if dup == root:
        return MergePlan(dup=dup, root=root, error="不能把一个实体合并到它自己。")
    return MergePlan(dup=dup, root=root)


def cycle_error(dup: uuid.UUID, root: uuid.UUID, parent: dict[uuid.UUID, uuid.UUID]) -> str:
    """`dup → root` 这条边加上之后会不会成环。

    自指由数据库 CHECK 拦住，环路拦不住——CHECK 看不到别的行。而环路一旦写进库，
    向上找根的查询就会靠深度上限硬停，那时已经没人知道哪一行是真的根了。
    所以在写入前算一遍：从 root 沿链往上走，撞见 dup 就是成环。
    """

    seen = {root}
    current = root
    for _ in range(MAX_MERGE_DEPTH):
        nxt = parent.get(current)
        if nxt is None:
            return ""
        if nxt == dup:
            return (
                f"这条边会成环：{root} 已经（直接或间接）并到了 {dup} 名下。"
                "先把那一头解开，或者把两边都指向真正的根。"
            )
        if nxt in seen:
            return f"目标 {root} 所在的链本身已经有环，先修它再合并。"
        seen.add(nxt)
        current = nxt
    return f"目标 {root} 的合并链超过 {MAX_MERGE_DEPTH} 层，不再往里加。"


async def run_merge(args) -> int:
    """把 `--id` 这一行并入 `--into` 那一行：写一个指针，事实一行不动。

    **不 UPDATE `fact_claims.subject_id`**：触发器直接拒改；而且改完就再也查不到
    「这条事实当初挂在哪种写法下」，合并变成不可逆。指针可逆——清空就还原。
    """
    plan = parse_merge_args(args)
    if plan.error:
        print(plan.error, file=sys.stderr)
        return 2
    assert plan.dup is not None and plan.root is not None

    async with session_scope() as session:
        dup = await session.get(Entity, plan.dup)
        root = await session.get(Entity, plan.root)
        if dup is None or root is None:
            missing = plan.dup if dup is None else plan.root
            print(f"⚠ 库里没有这个实体：{missing}", file=sys.stderr)
            return 2

        all_rows = list((await session.execute(select(Entity))).scalars().all())
        parent = {e.id: e.merged_into for e in all_rows if e.merged_into is not None}

        if args.undo:
            if dup.merged_into is None:
                print(f"{dup.name} 没有合并指针，跳过。", file=sys.stderr)
                return 0
            if not args.dry_run:
                dup.merged_into = None
            print(
                f"{dup.name} → 独立实体" + ("（dry-run，未写库）" if args.dry_run else ""),
                file=sys.stderr,
            )
            return 0

        error = cycle_error(plan.dup, plan.root, parent)
        if error:
            print(error, file=sys.stderr)
            return 2

        counts = dict(
            (
                await session.execute(
                    select(FactClaim.subject_id, func.count()).group_by(FactClaim.subject_id)
                )
            ).all()
        )
        print(f"并入：{dup.name}（{dup.kind}，{counts.get(dup.id, 0)} 条事实）")
        print(f"  到：{root.name}（{root.kind}，{counts.get(root.id, 0)} 条事实）")
        print(f"  合并后查询将覆盖 {counts.get(dup.id, 0) + counts.get(root.id, 0)} 条")

        if dup.merged_into == plan.root:
            print("已经指向这个根了，不用改。", file=sys.stderr)
            return 0
        if root.merged_into is not None:
            # 链式是允许的（合并分批做），但要说出来：真正的根还在上面一层
            print(
                f"注：{root.name} 自己也并到了别的实体名下，这是一个中间节点。"
                "查询会沿链找到真正的根，行为正确，但下次可以直接 --into 那个根。",
                file=sys.stderr,
            )

        if not args.dry_run:
            dup.merged_into = plan.root
        print(
            f"{dup.name} → {root.name}" + ("（dry-run，未写库）" if args.dry_run else ""),
            file=sys.stderr,
        )
        return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="事实审计与修复（audit 只读；retract 撤回不删除；merge 合并实体）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="只读扫描，输出可疑事实队列")
    audit.add_argument("--format", choices=("table", "csv"), default="table")
    audit.add_argument("--out", default="", help="写到文件（CSV 默认输出到 stdout）")
    audit.add_argument("--limit", type=int, default=0, help="只扫描前 N 条事实，0 表示全部")

    retract = sub.add_parser("retract", help="撤回一条或多条事实（不删除，必须附理由）")
    retract.add_argument("--id", dest="ids", nargs="+", required=True, help="一条或多条事实 id")
    retract.add_argument("--reason", default="", help="为什么撤：依据是什么，最好附原文")
    retract.add_argument(
        "--replaced-by",
        dest="replaced_by",
        default="",
        help="被哪条新事实替代（只在能确定对应关系时填，定不准就留空）",
    )
    retract.add_argument("--undo", action="store_true", help="恢复采信（把三列清回 NULL）")
    retract.add_argument("--dry-run", action="store_true", help="只打印，不写库")

    merge = sub.add_parser("merge", help="把一个实体并入另一个（写指针，不改事实）")
    merge.add_argument("--id", default="", help="被合并掉的那一行（别名写法）")
    merge.add_argument("--into", default="", help="保留下来的根")
    merge.add_argument("--undo", action="store_true", help="解除合并，让 --id 那行恢复独立")
    merge.add_argument("--dry-run", action="store_true", help="只打印，不写库")

    args = parser.parse_args()
    if args.command == "audit":
        return await run_audit(args)
    if args.command == "merge":
        return await run_merge(args)
    return await run_retract(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
