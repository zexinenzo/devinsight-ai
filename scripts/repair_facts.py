"""阶段 8：事实审计——只读，不写库。

    .venv/bin/python scripts/repair_facts.py audit                      # 人看的表格
    .venv/bin/python scripts/repair_facts.py audit --format csv          # 给 Excel 的 CSV
    .venv/bin/python scripts/repair_facts.py audit --format csv --out .tmp/audit.csv

**这个脚本为什么不写库。** 判定与执行必须分开，否则就成了「让模型给自己判卷」——
判定结果一旦能直接落库，就没有第二双眼睛看过它。撤回动作留给
`repair_facts.py retract --id ... --reason ...`（下一步实现），中间这一步是人。

三档判定见 `docs/阶段8-数据修复设计.md` 第 5 节。核心事实是：238 条里能自动判定的只有
2 条，不到 1%。所以这个脚本的产出不是「一份待删清单」，而是**一份按可疑程度排好序的
人工队列**——它的价值在于把 238 条缩到十几条，不在于替你决定。

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
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.db.models import Entity, FactClaim
from app.db.session import session_scope
from app.services.timeline_service import DATE_SOURCE_LABELS, PREDICATES
from sqlalchemy import select

# ① 形状：许可证标识符。没有任何谓词把许可证声明为合法宾语——
# 许可证是「以什么条件开源」，是条件不是产物。
# 注意这里不豁免 open_sourced：豁免它，恰好会放过唯一真实的违规
# （`Fardesk --open_sourced--> Apache-2.0`），而那句证据里本来就写了许可证。
LICENSE_RE = re.compile(
    r"^(?:Apache|MIT|BSD|GPL|AGPL|LGPL|MPL|EPL|ISC|Unlicense|CC[- ]BY)"
    r"[- ]?\d?(?:\.\d)?(?:[- ]only|[- ]or[- ]later)?$",
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
OTHER_TITLE_RE = re.compile(
    # 不用 \b：中文字符在 Python 正则里也算 \w，`原CFO Colette` 的 `\bCFO` 会匹配失败。
    # 这个 bug 让真实的 CFO→CEO 错误从 ③ 档漏了过去，而误报了「Fed Chairman」那条——
    # 后者恰恰不算错，Chairman 就是该机构的一把手。
    r"(?<![A-Za-z])(?:CFO|CTO|COO|CMO|CPO)(?![A-Za-z])"
    r"|chief\s+(?:financial|technology|operating|marketing|product)\s+officer"
    r"|首席(?:财务|技术|运营|市场)官",
    re.IGNORECASE,
)
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
    if LICENSE_RE.match(object_text.strip()):
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

    # 证据里出现了别的 C 级头衔，且宾语紧跟其后——典型是 CFO 被记成 CEO
    if predicate == "ceo" and OTHER_TITLE_RE.search(evidence):
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


async def main() -> int:
    parser = argparse.ArgumentParser(description="事实审计与修复（当前只有只读的 audit）")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="只读扫描，输出可疑事实队列")
    audit.add_argument("--format", choices=("table", "csv"), default="table")
    audit.add_argument("--out", default="", help="写到文件（CSV 默认输出到 stdout）")
    audit.add_argument("--limit", type=int, default=0, help="只扫描前 N 条事实，0 表示全部")

    args = parser.parse_args()

    if args.command != "audit":
        parser.error(f"未知命令 {args.command}")

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
        "本脚本不写库。① 档也只报告——撤回要单独执行，并且必须附理由；"
        "②③ 档请对着 evidence 过一遍原文再决定。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
