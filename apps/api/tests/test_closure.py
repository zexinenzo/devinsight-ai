"""阶段 8 第 9 步：闭合区间（`valid_to` 的写入方）。

`valid_to` 从 0009 起就在表里，触发器也一直允许改它，但**没有写入方**——于是 270 条
事实全是「至今成立」，`英伟达` 名下两个 CEO 同时在职，`as_of` 只能两个都返回。
补这一列不难，难的是别让它变成第二个 `aliases`：有列、能写、没人写，等于没有。

所以这里钉两件事：

1. **闭合日的判据**（`plan_closure`，纯函数）。写错一天不报错，只在某个历史时点
   答错，而那一天没有人会去查——所以判据必须能脱离数据库被穷举。
2. **闭合之后冲突真的消失，且两条事实都还在**。设计稿第 9 步的验收标准就是这个：
   冲突从 1 条降到 0 条，不是一个前任被撤掉。

另有 4 条迁移与 ORM 的一致性断言，理由与 `test_retraction.py` 相同：命名约定会给
CHECK 补 `ck_<表名>_` 前缀，手写全名会静默叠成 `ck_x_ck_x_y`，而只在 `alembic
upgrade head` 那一刻才炸。
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path

from app.db.models import FactClaim
from app.services.timeline_service import TimelineEntry, find_overlaps
from sqlalchemy import CheckConstraint

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "repair_facts.py"
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / (
    "0013_fact_claim_valid_to_reason.py"
)


def _load_script():
    """按路径加载脚本；`sys.modules` 要先登记再 exec（`@dataclass(slots=True)` 依赖它）。"""
    name = "repair_facts_closure"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


repair = _load_script()


def _norm(text: str) -> str:
    return " ".join(text.split())


def fact(
    predicate: str = "ceo",
    object_text: str = "Colette Kress",
    start: date | None = date(2026, 11, 18),
    end: date | None = None,
):
    return repair.FactView(
        id=uuid.uuid4(),
        predicate=predicate,
        object_text=object_text,
        valid_from=start,
        valid_to=end,
    )


# --- 迁移 0013 与 ORM 的一致性 ---------------------------------------------


def test_valid_to_reason_exists_in_orm_and_migration() -> None:
    sql = _norm(MIGRATION.read_text(encoding="utf-8"))
    assert "valid_to_reason" in FactClaim.__table__.columns
    assert "valid_to_reason" in sql


def test_closure_check_constraint_matches_migration() -> None:
    """有 `valid_to` 就必须有理由。这条 CHECK 是这一列存在的全部意义：
    没有它，`valid_to` 就是一个谁都能徒手改的日期。"""
    found = [
        _norm(str(c.sqltext))
        for c in FactClaim.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_fact_claims_closure"
    ]
    assert found, "ORM 里没有 ck_fact_claims_closure"
    assert found[0] in _norm(MIGRATION.read_text(encoding="utf-8"))


def test_valid_to_reason_is_nullable() -> None:
    """既有 270 行的 valid_to 全是 NULL，这一列不能要求回填历史。"""
    assert FactClaim.__table__.columns["valid_to_reason"].nullable is True


def test_retraction_and_closure_checks_are_separate() -> None:
    """两条 CHECK 一个在认识轴、一个在世界轴。合并成一条，就等于把「记错了」
    和「世界变了」压成同一个状态——那正是 0010 的迁移说明里警告过的事。"""
    names = {
        c.name for c in FactClaim.__table__.constraints if isinstance(c, CheckConstraint)
    }
    assert {"ck_fact_claims_retraction", "ck_fact_claims_closure"} <= names


# --- plan_closure：闭合日的判据 --------------------------------------------


def test_cutoff_defaults_to_successor_start() -> None:
    """默认取被取代那条的 `valid_from`：两条区间正好接上，一天的假冲突都不留。

    设计稿第 9 步的验收（冲突 1 → 0）完全依赖这个默认值。换成「今天」也能跑，
    但会留下一段没人解释的空档。
    """
    old = fact(object_text="Jensen Huang", start=date(2020, 1, 1))
    new = fact(start=date(2026, 11, 18))

    plan = repair.plan_closure(old, new)
    assert plan.error == ""
    assert plan.cutoff == date(2026, 11, 18)


def test_explicit_on_wins_and_warns_about_the_gap() -> None:
    """显式给日期时不会静默采纳，而是提示会在两条之间留一段空档。"""
    old = fact(start=date(2020, 1, 1))
    new = fact(start=date(2026, 11, 18))

    plan = repair.plan_closure(old, new, on=date(2026, 1, 1))
    assert plan.cutoff == date(2026, 1, 1)
    assert any("空档" in w for w in plan.warnings)


def test_missing_date_is_an_error_not_a_guess() -> None:
    """两条都没有日期时不能默认「今天」：那是拿采集时间冒充世界时间，
    正是 `valid_from` 当初宁可留空也不做的事。"""
    plan = repair.plan_closure(fact(start=None), fact(start=None))
    assert plan.error
    assert "--on" in plan.error


def test_cutoff_must_be_after_start() -> None:
    """闭合日早于或等于起始日会造出空区间，库里的 period CHECK 会拒。
    要在发语句之前拒，否则报错离原因很远。

    两条起始日刻意**不同**：相同的话会先命中下面那条「同日」的分支，
    断言照样绿，但测的已经不是区间退化这件事了。
    """
    old = fact(start=date(2026, 11, 1))
    plan = repair.plan_closure(old, fact(start=date(2026, 11, 18)), on=date(2026, 11, 1))
    assert plan.error
    assert "不晚于" in plan.error


def test_same_start_date_gets_a_tailored_message() -> None:
    """两条起始日相同，是这条功能最尴尬的一种边界。

    它几乎必然来自「发文时间兜底」：报道写 `former CEO` 时，此人的真实上任日
    早于发文日，而库里只有发文日。此时用继任者的起始日去闭合旧行会让区间退化，
    而**随手填一个更晚的日期等于编造交接日**——比留一条看得见的冲突更糟。

    所以这里要专门告诉人这件事，而不是抛一句通用的「区间退化」让人自己去猜。
    """
    same = date(2026, 11, 18)
    plan = repair.plan_closure(fact(start=same), fact(start=same))
    assert plan.error
    assert "编" in plan.error or "冲突" in plan.error


def test_same_start_date_can_still_be_split_with_an_explicit_date() -> None:
    """真的知道卸任日时，起始日相同并不妨碍闭合——只要日期严格更晚。"""
    same = date(2026, 11, 18)
    plan = repair.plan_closure(fact(start=same), fact(start=same), on=date(2027, 1, 5))
    assert plan.error == ""
    assert plan.cutoff == date(2027, 1, 5)



def test_predicate_must_match() -> None:
    """用融资记录去闭合人事记录，语义上根本不是一回事。"""
    plan = repair.plan_closure(fact(predicate="ceo"), fact(predicate="funded"))
    assert plan.error
    assert "谓词" in plan.error


def test_a_fact_cannot_supersede_itself() -> None:
    same = fact()
    plan = repair.plan_closure(same, same)
    assert plan.error
    assert "自己" in plan.error


def test_already_closed_with_another_date_is_refused() -> None:
    """已经闭合过的不允许直接改日期：改一次就等于悄悄改写了一段历史，
    而 `--undo` 至少留下「有人先解开再重来」的痕迹。"""
    old = fact(start=date(2020, 1, 1), end=date(2025, 1, 1))
    plan = repair.plan_closure(old, fact(start=date(2026, 11, 18)))
    assert plan.error
    assert "--undo" in plan.error


def test_same_cutoff_is_a_noop_not_an_error() -> None:
    """重跑同一条命令不该报错，也不该再写一次库。"""
    old = fact(start=date(2020, 1, 1), end=date(2026, 11, 18))
    plan = repair.plan_closure(old, fact(start=date(2026, 11, 18)))
    assert plan.error == ""
    assert plan.noop is True


def test_new_must_not_already_be_over_when_cutoff_lands() -> None:
    """闭合日落在新记录结束之后，说明这两条压根不是前后任。"""
    new = fact(start=date(2026, 1, 1), end=date(2026, 6, 1))
    plan = repair.plan_closure(fact(start=date(2020, 1, 1)), new, on=date(2026, 9, 1))
    assert plan.error
    assert "之后" in plan.error


def test_reason_is_built_from_the_pointer() -> None:
    """有 `--by` 时理由自动生成：指针本身就是最硬的依据，文字只是给人看的样子。"""
    plan = repair.plan_closure(fact(start=date(2020, 1, 1)), fact(start=date(2026, 11, 18)))
    assert "Colette Kress" in plan.reason
    assert "2026-11-18" in plan.reason


def test_without_pointer_a_reason_is_mandatory() -> None:
    """没有指针可以指认时，理由不能也留空——否则这行日期就没人能追问。"""
    plan = repair.plan_closure(fact(start=date(2020, 1, 1)), None, on=date(2026, 11, 18))
    assert plan.error
    assert "--reason" in plan.error

    ok = repair.plan_closure(
        fact(start=date(2020, 1, 1)), None, on=date(2026, 11, 18), reason="董事会公告卸任"
    )
    assert ok.error == ""
    assert ok.reason == "董事会公告卸任"


def test_undated_row_gets_a_warning_because_closing_changes_nothing() -> None:
    """没有 valid_from 的行本来就不参与 as_of（它在 undated 里）。闭合它不改变任何
    答案，但也不该拒绝执行——只是要说明白。"""
    plan = repair.plan_closure(fact(start=None), fact(start=date(2026, 11, 18)))
    assert plan.error == ""
    assert any("valid_from" in w for w in plan.warnings)


def test_future_cutoff_warns() -> None:
    """已知的卸任日可以写，但要说出来：它和「已经发生的卸任」不是一回事。"""
    plan = repair.plan_closure(
        fact(start=date(2020, 1, 1)),
        fact(start=date(2027, 1, 1)),
        today=date(2026, 11, 21),
    )
    assert plan.error == ""
    assert any("未来" in w for w in plan.warnings)


# --- 验收：冲突消失，两条事实都还在 ----------------------------------------


def _entry(object_text: str, start: date, end: date | None = None) -> TimelineEntry:
    return TimelineEntry(
        fact_id=uuid.uuid4(),
        predicate="ceo",
        predicate_label="",
        object_text=object_text,
        valid_from=start,
        valid_to=end,
        date_source="stated",
        confidence=0.9,
        evidence_text=None,
        document_id=uuid.uuid4(),
        document_title="标题",
        document_url="https://example.com/a",
        published_at=None,
    )


def test_closing_the_predecessor_removes_the_conflict_without_dropping_a_fact() -> None:
    """设计稿第 9 步的验收原话：CEO 冲突从 1 条降到 0 条，**且两条事实都还在**。

    这个「且」是重点。撤回也能让冲突归零，但代价是丢一个真实的人事状态；
    闭合区间让冲突归零，两条都留着——一个前任、一个现任。
    """
    entries = [
        _entry("Jensen Huang", date(2020, 1, 1)),
        _entry("Colette Kress", date(2026, 11, 18)),
    ]
    assert len(find_overlaps(entries)) == 1

    closed = [
        _entry("Jensen Huang", date(2020, 1, 1), date(2026, 11, 18)),
        _entry("Colette Kress", date(2026, 11, 18)),
    ]
    assert find_overlaps(closed) == []
    assert len(closed) == 2
    assert {entry.object_text for entry in closed} == {"Jensen Huang", "Colette Kress"}


def test_closing_a_day_late_leaves_a_one_day_fake_conflict() -> None:
    """闭合日填晚一天，冲突就还在——这正是「填错一天不报错，只在某一天答错」的形态。

    留着这条测试，是因为它是 `--on` 唯一的失败模式，而且失败得毫无声息：
    时间线上看起来一切正常，只有那天两个人的任职区间重叠。
    """
    entries = [
        _entry("Jensen Huang", date(2020, 1, 1), date(2026, 11, 19)),
        _entry("Colette Kress", date(2026, 11, 18)),
    ]
    problems = find_overlaps(entries)
    assert len(problems) == 1
    assert "2026-11-18" in problems[0]


# --- supersede --list：候选对怎么配 -----------------------------------------


def test_candidates_pair_only_neighbours_not_every_combination() -> None:
    """三条依次在任，要闭合的是 (A,B) 与 (B,C)，不是 (A,C)。

    拿 A 去对 C 会写出「A 被 C 取代」，中间 B 那段任期就凭空消失——而它不会报错，
    时间线上只剩两行，看起来还挺整齐。
    """
    views = [
        fact(object_text="A", start=date(2020, 1, 1)),
        fact(object_text="B", start=date(2024, 1, 1)),
        fact(object_text="C", start=date(2026, 1, 1)),
    ]
    pairs = repair.candidate_pairs(views)
    assert [(old.object_text, new.object_text) for old, new in pairs] == [
        ("A", "B"),
        ("B", "C"),
    ]


def test_candidates_drop_duplicate_object_text() -> None:
    """同一个宾语两行不是「两个人同时在任」，是同一件事被两篇报道写了两遍。

    唯一约束含 `subject_id`，跨写法（`英伟达` / `Nvidia`）的重复在合并前挡不住，
    所以这一步必须在配对之前去重，否则会配出「英伟达 → 英伟达」这种自我闭合。
    """
    views = [
        fact(object_text="Jensen Huang", start=date(2026, 11, 18)),
        fact(object_text="Jensen Huang", start=date(2026, 11, 19)),
        fact(object_text="Colette Kress", start=date(2026, 11, 20)),
    ]
    pairs = repair.candidate_pairs(views)
    assert len(pairs) == 1
    assert pairs[0][1].object_text == "Colette Kress"


def test_candidates_put_unknown_start_last() -> None:
    """没有起始日的那条排最后：它不能被当作「新的开始成立」，cutoff 无从取。

    排到最后，它会出现在 (已知, 未知) 这一对里，`plan_closure` 会要求 `--on`——
    报错位置正好是这里，而不是在写库时才因为空区间被 CHECK 拒。
    """
    views = [
        fact(object_text="unknown", start=None),
        fact(object_text="known", start=date(2026, 1, 1)),
    ]
    pairs = repair.candidate_pairs(views)
    assert pairs[0][0].object_text == "known"
    plan = repair.plan_closure(pairs[0][0], pairs[0][1])
    assert plan.error and "--on" in plan.error


def test_pad_counts_cjk_as_two_columns() -> None:
    """表格对齐靠显示宽度，不靠字符个数；否则中文名那一列整体右移。"""
    assert repair._pad("英伟达", 8) == "英伟达  "
    assert repair._pad("Nvidia", 8) == "Nvidia  "
