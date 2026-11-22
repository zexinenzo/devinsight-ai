"""阶段 8：事实审计、撤回与实体合并。

    .venv/bin/python scripts/repair_facts.py audit                       # 只读，人看的表格
    .venv/bin/python scripts/repair_facts.py audit --format csv --out .tmp/audit.csv
    .venv/bin/python scripts/repair_facts.py retract --id <uuid> --reason "…（附原文）"
    .venv/bin/python scripts/repair_facts.py retract --id <uuid> --undo   # 恢复采信
    .venv/bin/python scripts/repair_facts.py merge --id <uuid> --into <uuid>       # 合并实体
    .venv/bin/python scripts/repair_facts.py merge --id <uuid> --undo             # 解除合并
    .venv/bin/python scripts/repair_facts.py supersede --list                    # 谁该被闭合
    .venv/bin/python scripts/repair_facts.py supersede --id <旧> --by <新>        # 闭合区间
    .venv/bin/python scripts/repair_facts.py supersede --id <旧> --by <新> --undo
    .venv/bin/python scripts/repair_facts.py recheck                        # 撤回有没有接替者
    .venv/bin/python scripts/repair_facts.py recheck --link                 # 登记唯一确定的那些

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

**`supersede` 补的是时间轴的右半边。** `valid_to` 从建表起就能改，但没有写入方，
于是每条事实都是「至今成立」——两个 CEO 同时在职，`as_of` 只能两个都返回。
这条不能撤回：撤回的意思是「我们不再声称」，而前任在任是事实，撤掉它等于让
`as_of` 说「那天不是他」。所以这里做的是**闭合区间**，要写 `valid_to` +
`superseded_by` + `valid_to_reason` 三列（迁移 0013 的 CHECK 要求有日期必有理），
`--undo` 把三列一起清回去。

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

**`recheck` 是撤回的下半场。** 撤回只说了「这条是错的」，没回答「那对的在哪」。
改完词表做定向重跑（`extract_facts.py --only-retracted`）之后，新记录会落在同文档、
同主语、同谓词的位置上，但唯一约束含 `object_text`，所以新行只是新行——和旧的错行
没有任何联系。`recheck` 负责把这条联系找出来，判据与设计稿第 6 节一致：同位置 +
旧记录被判违规 + 新记录证据 `exact`，三条不齐就留空。`--link` 只写
`retracted_replaced_by` 一列，事实行一个字不动。

**它故意不替你判断的地方**：候选 0 条时，「模型这次什么都没给」和「模型又给了旧宾语、
被唯一约束挡掉」在数据库里长得一模一样；候选多条的对应关系同样定不准。两种都打印
并留空，因为在这两处猜一次的代价，是把「知道是错的」换成「不知道对不对」。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.db.models import Document, Entity, FactClaim
from app.db.session import session_scope
from app.services.fact_extraction import evidence_match
from app.services.predicate_specs import PREDICATE_SPECS
from app.services.timeline_service import (
    DATE_SOURCE_LABELS,
    MAX_MERGE_DEPTH,
    MULTI_VALUED_PREDICATES,
    PREDICATES,
    build_entity_group,
)
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

    检查哪几条由 `predicate_specs.PredicateSpec.object_type` 决定，不在这里按谓词名
    分支。原来这里写着 `if predicate == "founded" / "ceo" / "funded"`——那三行和
    prompt 里「宾语该填什么」是同一份知识的两遍手抄，而 prompt 只对 benchmark 明说
    过宾语类型。**类型写在谓词的定义里，就该从定义里读出来**，否则加一个谓词
    就多一处可能忘记同步的判断。

    仍然只覆盖「有形状可查」的三类：年份/机构、人名、金额。许可证明明有形状，
    但它归 ① 档管（与谓词无关的那个位置不能放许可证），不重复判。
    """

    spec = PREDICATE_SPECS.get(predicate)
    if spec is None:
        return None
    text = object_text.strip()

    if spec.object_type == "year_or_org":
        # 创立的宾语应该是年份或机构。填人名不是错在「人不能创立公司」，
        # 而是错在这个位置该记「何时/由谁做主语」，把它填到宾语上会让
        # 时间线变成「OpenAI 创立了 Sam Altman」。
        if YEAR_RE.match(text) or COMPANY_HINT_RE.search(text):
            return None
        return f"{predicate}_object_not_{spec.object_type}"

    if spec.object_type == "person":
        if looks_like_person(text):
            return None
        return f"{predicate}_object_not_{spec.object_type}"

    if spec.object_type == "amount":
        if AMOUNT_OR_ROUND_RE.search(text) or FINANCING_EVENT_RE.search(text):
            return None
        if DEBT_FACILITY_RE.search(text):
            return "funded_object_is_debt_facility"
        # 剩下的多半是宾语填了投资方/被投方（`英伟达 --funded--> 矽品精密`），
        # 方向反了或者槽位错位。统一的理由就够：人工看一眼 evidence 就知道是哪种
        return f"{predicate}_object_is_not_amount"

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


# --- supersede：闭合区间（时间轴的右半边）-----------------------------------


@dataclass(slots=True)
class FactView:
    """判定一条事实需要的几个字段。

    单独拆出来只有一个理由：`plan_closure` 必须能不连库被测试。写库那条路径出错
    就是改坏历史数据，而它能被测试的唯一方式是把判定从 SQL 旁边摘出来。
    """

    id: uuid.UUID
    predicate: str
    object_text: str
    valid_from: date | None
    valid_to: date | None


@dataclass(slots=True)
class ClosurePlan:
    cutoff: date | None = None
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    noop: bool = False


def plan_closure(
    old: FactView,
    new: FactView | None,
    *,
    on: date | None = None,
    reason: str = "",
    today: date | None = None,
) -> ClosurePlan:
    """算出这条事实该在哪天结束，以及该不该拒绝这次闭合。

    判据只有一句话：**闭合日要落在「旧的不再成立」和「新的开始成立」之间有据可查
    的那一天**。没有依据时宁可报错——`valid_to` 填错一天不会报错，只会在某个历史
    时点答错，而那一天没有人会去查。
    """
    if new is not None:
        if new.id == old.id:
            return ClosurePlan(error="不能拿一条事实去取代它自己。")
        if new.predicate != old.predicate:
            return ClosurePlan(
                error=(
                    f"两条的谓词不同（{old.predicate} / {new.predicate}）。"
                    "「世界变了」只发生在同一个谓词上：CEO 换人闭合上一条 ceo，"
                    "不能用一条融资记录去闭合一条人事记录。"
                )
            )

    cutoff = on if on is not None else (new.valid_from if new is not None else None)
    if cutoff is None:
        return ClosurePlan(
            error=(
                "不知道该闭合到哪天：被取代的那条没有 valid_from。"
                "用 --on YYYY-MM-DD 显式指定，并把它写进 --reason。"
            )
        )

    if old.valid_to == cutoff:
        return ClosurePlan(cutoff=cutoff, reason=reason, noop=True)
    if old.valid_to is not None:
        return ClosurePlan(
            error=(
                f"这条已经闭合在 {old.valid_to.isoformat()}，不能直接改成 "
                f"{cutoff.isoformat()}——先 --undo 再重来，免得改了一次没人知道。"
            )
        )

    if (
        new is not None
        and new.valid_from is not None
        and old.valid_from is not None
        and new.valid_from == old.valid_from
        and cutoff == old.valid_from
    ):
        # 两条的起始日是同一��。这多半不是巧合，而是两条都用了「发文时间兜底」
        # （`valid_from_source = published`）——报道里写 `former CEO` 时，
        # 它的真实上任日比发文日早得多，可库里只有发文日。
        # 这时闭合会在同一天造出空区间，而**硬填一个日期等于编造交接日**。
        return ClosurePlan(
            error=(
                f"两条的起始日都是 {old.valid_from.isoformat()}，无法用它把区间分开——"
                "这个日期多半是发文时间兜底，不是真实的交接日。"
                "确实知道卸任日就 --on 指定；不知道就留着这条冲突，"
                "编一个日期比留一条能被看见的冲突更糟。"
            )
        )

    if old.valid_from is not None and cutoff <= old.valid_from:
        return ClosurePlan(
            error=(
                f"闭合日 {cutoff.isoformat()} 不晚于这条的起始日 "
                f"{old.valid_from.isoformat()}，区间会退化成空——迁移 0009 的 period "
                "CHECK 会拒，它拒得对。"
            )
        )

    warnings: list[str] = []
    if old.valid_from is None:
        warnings.append(
            "这条没有 valid_from，闭合它不会改变 as_of 的结果（它本来就不参与判定）"
        )
    if new is not None and new.valid_from is not None and cutoff != new.valid_from:
        warnings.append(
            f"--on {cutoff.isoformat()} 与被取代那条的起始日 "
            f"{new.valid_from.isoformat()} 不同：两者之间会留一段「谁都不在任」的空档"
        )
    if new is not None and new.valid_to is not None and cutoff > new.valid_to:
        return ClosurePlan(
            error=(
                f"闭合日 {cutoff.isoformat()} 落在新记录结束之后"
                f"（{new.valid_to.isoformat()}），说明这两条根本不是前后任。"
            )
        )
    if today is not None and cutoff > today:
        warnings.append(
            f"闭合日 {cutoff.isoformat()} 在未来，确认这是已知的卸任日而不是推测"
        )

    if not reason:
        if new is None:
            return ClosurePlan(
                error="没有 --by 就必须给 --reason：没有指针可以指认依据，全靠这句话。"
            )
        new_from = new.valid_from.isoformat() if new.valid_from else "日期未知"
        reason = f"被 {new.object_text}（{new_from}）取代"

    return ClosurePlan(cutoff=cutoff, reason=reason, warnings=warnings)


def parse_date_arg(value: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"--on 应为 YYYY-MM-DD，收到 {value!r}") from exc


def candidate_pairs(views: list[FactView]) -> list[tuple[FactView, FactView]]:
    """把同一实体同一谓词下「都还开着」的几条，按时间排成前后任，只配相邻两条。

    三个 CEO 依次是 A → B → C，需要闭合的是 (A,B) 和 (B,C) 两对，不是 (A,C)——
    拿 A 去对 C 会得到「A 被 C 取代」，而 B 那段任期就凭空消失了。所以是 zip 相邻，
    不是两两组合：`C(n,2)` 在三个宾语时就开始产假。
    """

    # 同一宾语留一条：同一件事被两篇报道写了两行（唯一约束含 subject_id，
    # 合并前挡不住跨写法重复），那不是两个人同时在任
    unique = {view.object_text: view for view in views}
    ordered = sorted(
        unique.values(), key=lambda view: (view.valid_from or date.max, view.object_text)
    )
    # strict=False 是故意的：左边永远比右边长一条，长度不等正是这里要的语义
    return list(zip(ordered, ordered[1:], strict=False))


def tie_message(old: FactView, new: FactView) -> str:
    """两条起始日相同时的说明。返回空串表示不是这种情况。

    这里要说的不是「闭合日算不出来」，而是**这一对根本没有前后任关系可言**：
    日期相同的时候，`candidate_pairs` 是按 `object_text` 字母序排的，于是打印出来的
    箭头方向纯属巧合——`ceo` 那组就排成了「Colette Kress → Jensen Huang」，
    而事实方向正好相反。给一条方向错的箭头，比不给箭头更糟：人会照着它去闭合。

    同时要给对药方。实跑 `--list` 的 7 对候选里 6 对是这个形态，其中
    `released_version` 的三组（nginx 1.30.7/1.31.9、DaisyRose 的三种量化权重、
    GPT-6.2 的 Astra/Sol）根本不是换代，是同时存在——把它们往 `--on` 上引，
    等于教人给并存的东西编一个结束日。所以第二句留着一个出口：漏掉的多值谓词
    会在这里显形，而不是被 `--on` 掩盖过去。修完名单后只剩 `ceo` 一个实例。
    """

    if old.valid_from is None or old.valid_from != new.valid_from:
        return ""
    day = old.valid_from.isoformat()
    return (
        f"起始日都是 {day}，排不出谁是谁的前任，箭头方向不作数。"
        "分两种谓词看：`ceo` 这种「同一时刻只能有一个人」的，说明库里缺真实交接日"
        "（用 --on 指定，别猜）；`released_version` 这种「可以同时存在多条」的，"
        "本来就不该被两两比较（加进 MULTI_VALUED_PREDICATES 后它会消失）。"
    )


def _pad(text: str, width: int) -> str:
    """按终端显示宽度补空格。中文占两列，用 len() 算会把整张表撑歪。"""

    used = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(1, width - used)

async def run_supersede_list(args) -> int:
    """列出「同一实体、同一个单值谓词、两条以上同时开着」的候选，并直接给出命令。

    为什么要有这一步：闭合需要两个 uuid，而人拿到 uuid 的唯一路径是粘一条长 SQL、
    再从输出里抄两个 36 位字符串。抄反一次，被闭合的是**现任**的任期——它不会报错，
    只会在某天让 `as_of` 答错一天。候选对由脚本算，`--id/--by` 就是脚本打印的那两行。
    """

    async with session_scope() as session:
        entities = list((await session.execute(select(Entity))).scalars().all())
        by_id = {row.id: row for row in entities}
        facts = list(
            (
                await session.execute(
                    select(FactClaim)
                    .where(
                        FactClaim.retracted_at.is_(None),
                        FactClaim.valid_to.is_(None),
                        FactClaim.predicate.notin_(MULTI_VALUED_PREDICATES),
                    )
                    .order_by(FactClaim.valid_from, FactClaim.recorded_at)
                )
            )
            .scalars()
            .all()
        )

    # 按「合并组的根 + 谓词」聚合：合并只写指针，跨写法的同一件事仍挂在两个
    # subject_id 上，按 id 分组会把英伟达/Nvidia 那一对拆成两组、各自看都不像冲突
    groups: dict[tuple[uuid.UUID, str], list[FactView]] = {}
    for fact in facts:
        entity = by_id.get(fact.subject_id)
        if entity is None:
            continue
        root = build_entity_group(entity, entities).root
        groups.setdefault((root.id, fact.predicate), []).append(_view(fact))

    candidates = 0
    closable = 0
    for (root_id, predicate), views in sorted(
        groups.items(), key=lambda item: (item[0][1], by_id[item[0][0]].name)
    ):
        pairs = candidate_pairs(views)
        if not pairs:
            continue
        print(f"\n{predicate}  ·  {by_id[root_id].name}")
        ordered = sorted(
            {view.object_text: view for view in views}.values(),
            key=lambda view: (view.valid_from or date.max, view.object_text),
        )
        for view in ordered:
            print(
                f"  {_pad(view.object_text, 24)}"
                f"{_pad(_span_text(view.valid_from, view.valid_to), 26)}{view.id}"
            )
        for old, new in pairs:
            candidates += 1
            tied = tie_message(old, new)
            if tied:
                print(f"  ⚠ {old.object_text} / {new.object_text}：{tied}")
                continue
            plan = plan_closure(old, new, today=date.today())
            if plan.error:
                print(f"  ⚠ {old.object_text} → {new.object_text}：{plan.error}")
                continue
            print(f"  ✓ 可闭合在 {plan.cutoff.isoformat()}（{plan.reason}）")
            closable += 1
            print(
                "      .venv/bin/python scripts/repair_facts.py supersede "
                f"--id {old.id} --by {new.id} --dry-run"
            )

    if candidates == 0:
        print("没有候选：单值谓词上没有同时开着两条的实体。")
        return 0
    print(
        f"\n共 {candidates} 对候选，其中 {closable} 对能闭合。"
        "⚠ 的是「判断不了」，不是「工具坏了」——它们要么本来就并存，"
        "要么缺一个真实的交接日。"
    )
    return 0


def _view(fact: FactClaim) -> FactView:
    return FactView(
        id=fact.id,
        predicate=fact.predicate,
        object_text=fact.object_text,
        valid_from=fact.valid_from,
        valid_to=fact.valid_to,
    )


async def run_supersede(args) -> int:
    """给一条事实补上结束日：`valid_to` + `valid_to_reason` + `superseded_by`。

    **为什么这不是撤回**：撤回表达「我们不再声称这条」，属于认识轴；前任 CEO 曾经
    在任是事实，撤掉它，`as_of("2026-03-01")` 会回答「当时不是他」。两个轴一旦混用，
    `as_of` 就开始系统性地撒谎——而那正是双时间表唯一存在的理由。
    """
    try:
        on = parse_date_arg(args.on) if args.on else None
        old_id = uuid.UUID(args.id)
        new_id = uuid.UUID(args.by) if args.by else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async with session_scope() as session:
        old = await session.get(FactClaim, old_id)
        if old is None:
            print(f"⚠ 库里没有这条 id：{old_id}", file=sys.stderr)
            return 2
        new = await session.get(FactClaim, new_id) if new_id is not None else None
        if new_id is not None and new is None:
            print(f"⚠ --by 指向的事实不存在：{new_id}", file=sys.stderr)
            return 2

        if args.undo:
            if old.valid_to is None:
                print("这条本来就没闭合，跳过。", file=sys.stderr)
                return 0
            kept = old.superseded_by
            print(f"重开区间：{old.predicate} → {old.object_text}")
            print(f"     原闭合于 {old.valid_to.isoformat()}（{old.valid_to_reason or '无理由'}）")
            if not args.dry_run:
                old.valid_to = None
                old.valid_to_reason = None
                if new is not None and kept == new.id:
                    old.superseded_by = None
            print("     → 已恢复为「至今」" + ("（dry-run，未写库）" if args.dry_run else ""))
            return 0

        plan = plan_closure(
            _view(old), _view(new) if new is not None else None,
            on=on, reason=args.reason, today=date.today(),
        )
        if plan.error:
            print(plan.error, file=sys.stderr)
            return 2

        if new is not None and old.subject_id != new.subject_id:
            # 合并之后同一个实体的两行仍然是两个 subject_id（指针不改外键），
            # 所以这里要比合并组，不能比 id。比 id 会把唯一真实的用例挡在外面。
            entity = await session.get(Entity, old.subject_id)
            if entity is None:
                # 外键是 CASCADE，理论上不会发生；发生了说明数据被绕过 ORM 改过，
                # 这时硬算分组会拿 None 去查 name 并抛 AttributeError，报错离原因很远
                print(f"⚠ 主语实体不存在：{old.subject_id}", file=sys.stderr)
                return 2
            rows = list((await session.execute(select(Entity))).scalars().all())
            group = build_entity_group(entity, rows)
            if new.subject_id not in group.ids:
                print(
                    f"两条事实挂在不同的实体上（{entity.name} 与被取代那条的主语）。"
                    "先 merge 让它们同组，再来闭合——跨实体的「取代」不是世界变了，"
                    "是记录挂错了地方。",
                    file=sys.stderr,
                )
                return 2

        print(f"闭合：{old.predicate} → {old.object_text}")
        print(f"     区间 {_span_text(old.valid_from, plan.cutoff)}")
        if new is not None:
            print(f"  由：{new.predicate} → {new.object_text}")
            print(f"     区间 {_span_text(new.valid_from, new.valid_to)}")
        print(f"  理由：{plan.reason}")
        for warning in plan.warnings:
            print(f"  ⚠ {warning}", file=sys.stderr)

        if plan.noop:
            print("已经是这个闭合日，不用改。", file=sys.stderr)
            return 0

        assert plan.cutoff is not None
        if not args.dry_run:
            old.valid_to = plan.cutoff
            old.valid_to_reason = plan.reason
            if new is not None:
                old.superseded_by = new.id
        print(
            "     → 已闭合" + ("（dry-run，未写库）" if args.dry_run else ""),
            file=sys.stderr,
        )
        before = plan.cutoff - timedelta(days=1)
        print(
            f"验证：as_of({before.isoformat()}) 只该有旧的那条；"
            f"as_of({plan.cutoff.isoformat()}) 只该有新的那条（右端不含当日）。",
            file=sys.stderr,
        )
        return 0


# --- recheck：定向重跑之后，哪条新事实接替了哪条旧事实 ----------------------


@dataclass(frozen=True, slots=True)
class ReplacementCandidate:
    """一条「同文档、同主语组、同谓词」的现存记录，可能接替了被撤回的那条。"""

    id: uuid.UUID
    object_text: str
    evidence_text: str | None
    match: str
    valid_from: date | None

    @property
    def exact(self) -> bool:
        return self.match == "exact"


def pick_replacement(
    old_object: str, candidates: list[ReplacementCandidate]
) -> tuple[ReplacementCandidate | None, str]:
    """从候选里挑唯一的接替者。返回 (选中或 None, 说明)。

    判定就是设计稿第 6 节那三条：第 1 条（同一文档 + 同一主语 + 同一谓词）由调用方
    的查询保证，第 2 条（旧记录确实被判违规）由「只传撤回过的行」保证，这里管剩下的
    第 3 条——**替换方必须比被替换方更硬**。

    三条不齐就**只插入、不登记替换**。代价是留着一批「被撤回了但没人接替」的记录，
    收益是永远不会因为重抽而把一条本来对的记录说成已被取代。后者不可恢复。
    """

    if not candidates:
        return None, (
            "同位置上没有新记录。两种可能含义相反：模型这次在这个（文档·主语·谓词）"
            "上一个字都没给，或者它给的还是旧宾语、被唯一约束挡掉了。"
            "后者在重跑日志里会有一行「已存在，未写入」——回去看日志，别在这里猜。"
            "如果还没跑过定向重跑，这一步本来就会全是这一种：先跑 "
            "`extract_facts.py --only-retracted --apply`。"
        )

    if len(candidates) > 1:
        listing = "、".join(f"`{item.object_text}`" for item in candidates)
        return None, (
            f"有 {len(candidates)} 条候选（{listing}），定不了哪条接替了 `{old_object}`。"
            "定不准就留空，需要人工确认后用 retract --id … --replaced-by … 指定。"
        )

    only = candidates[0]
    if not only.exact:
        return None, (
            f"唯一候选 `{only.object_text}` 的证据只有 {only.match}，不满足第 3 条。"
            "拿一条无法在原文里核对的记录去接替一条已判定违规的记录，"
            "等于把错误从「知道是错的」换成「不知道对不对」——那比留着更糟。"
        )

    return only, ""


async def run_recheck(args) -> int:
    """复核撤回记录：有没有新记录接替它，以及接替得硬不硬。

    为什么需要这个子命令：`retracted_replaced_by` 从迁移 0010 起就在表里，但没有任何
    自动化去填它。填它的唯一方式是人工抄两个 uuid —— 这个项目里抄 uuid 已经错了三次
    （括号粘贴、抄反、终端卡死），所以判定必须由脚本算，人只负责看结论。

    `--link` 只写**唯一且证据 exact** 的那些；其余一律打印并把判断留给人。
    """

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(FactClaim, Document, Entity)
                .join(Document, Document.id == FactClaim.document_id)
                .join(Entity, Entity.id == FactClaim.subject_id)
                .where(FactClaim.retracted_at.is_not(None))
                .order_by(FactClaim.retracted_at.desc(), FactClaim.object_text)
            )
        ).all()

        if not rows:
            print("库里没有撤回记录，没有要复核的东西。")
            return 0

        document_ids = {fact.document_id for fact, _, _ in rows}
        live = list(
            (
                await session.execute(
                    select(FactClaim).where(
                        FactClaim.document_id.in_(document_ids),
                        FactClaim.retracted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        entities = list((await session.execute(select(Entity))).scalars().all())
        by_id = {entity.id: entity for entity in entities}

        linked = 0
        ambiguous = 0
        unmatched = 0
        skipped = 0

        for fact, document, subject in rows:
            print(f"\n{subject.name} --{fact.predicate}--> {fact.object_text}")
            print(f"  id={fact.id}")
            print(f"  撤回理由：{fact.retracted_reason}")
            print(f"  文档：《{(document.title or '')[:40]}》")

            if fact.retracted_replaced_by is not None:
                print(f"  已登记接替者：{fact.retracted_replaced_by}")
                skipped += 1
                continue

            root = build_entity_group(subject, entities).root
            # 证据核对要用文档原文。`content_text` 为空时退回 summary——抽取时也是这么拼的，
            # 两处不一致会让「当初判 exact 的」在这里变成 missing，凭空造出一批拒绝。
            source = f"{document.title}\n{document.content_text or document.summary or ''}"

            candidates: list[ReplacementCandidate] = []
            for other in live:
                if other.document_id != fact.document_id:
                    continue
                if other.predicate != fact.predicate:
                    continue
                entity = by_id.get(other.subject_id)
                if entity is None:
                    continue
                if build_entity_group(entity, entities).root.id != root.id:
                    continue
                candidates.append(
                    ReplacementCandidate(
                        id=other.id,
                        object_text=other.object_text,
                        evidence_text=other.evidence_text,
                        match=evidence_match(other.evidence_text, source),
                        valid_from=other.valid_from,
                    )
                )

            for cand in candidates:
                mark = "✓" if cand.exact else "·"
                print(
                    f"  {mark} {_pad(cand.object_text, 24)}"
                    f"{_pad(cand.match, 9)}{cand.id}"
                )

            chosen, reason = pick_replacement(fact.object_text, candidates)
            if chosen is None:
                print(f"  ⚠ {reason}")
                if candidates:
                    ambiguous += 1
                else:
                    unmatched += 1
                continue

            print(f"  → 接替者：{chosen.object_text}")
            if args.link:
                fact.retracted_replaced_by = chosen.id
            linked += 1

    verb = "已写库" if args.link else "只读，未写库"
    print(
        f"\n共 {len(rows)} 条撤回：{linked} 条能确定接替者，"
        f"{ambiguous} 条多候选定不了，{unmatched} 条同位置没有新记录"
        + (f"，{skipped} 条先前已登记" if skipped else "")
        + f"。（{verb}）"
    )
    if not args.link and linked:
        print(
            "确认无误后加 --link 重跑一次，把这些接替关系登记进 "
            "`retracted_replaced_by`——只登记，不改任何事实行。"
        )
    return 0


def _span_text(start: date | None, end: date | None) -> str:
    return f"{start.isoformat() if start else '?'} → {end.isoformat() if end else '至今'}"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="事实审计与修复（audit 只读；retract 撤回不删除；merge 合并实体；"
        "supersede 闭合区间；recheck 复核撤回有没有接替者）"
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

    supersede = sub.add_parser(
        "supersede", help="闭合一条事实的区间（补 valid_to，不撤回）"
    )
    supersede.add_argument("--id", default="", help="被闭合的那条（旧的）")
    supersede.add_argument("--by", default="", help="取代它的那条（新的）")
    supersede.add_argument(
        "--on",
        default="",
        help="闭合到哪天 YYYY-MM-DD。省略时取 --by 那条的 valid_from——"
        "「旧的不再成立」和「新的开始成立」是同一个分界，这样两条区间正好接上",
    )
    supersede.add_argument("--reason", default="", help="为什么在这天结束（省略时由 --by 生成）")
    supersede.add_argument("--undo", action="store_true", help="重新打开区间（三列一起清回 NULL）")
    supersede.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    supersede.add_argument(
        "--list",
        dest="list_candidates",
        action="store_true",
        help="只读：列出该被闭合的候选对，并打印可复制的 supersede 命令",
    )

    recheck = sub.add_parser(
        "recheck",
        help="复核撤回记录：有没有新记录接替它（定向重跑之后用；默认只读）",
    )
    recheck.add_argument(
        "--link",
        action="store_true",
        help="把「唯一且证据 exact」的接替关系写进 retracted_replaced_by（只写这一列）",
    )

    args = parser.parse_args()
    if args.command == "audit":
        return await run_audit(args)
    if args.command == "merge":
        return await run_merge(args)
    if args.command == "recheck":
        return await run_recheck(args)
    if args.command == "supersede":
        if args.list_candidates:
            return await run_supersede_list(args)
        return await run_supersede(args)
    return await run_retract(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
